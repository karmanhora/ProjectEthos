"""The commit pipeline: validate, authorize, apply, record, persist.

One accepted action produces exactly one event, one new state version and one
atomically appended log record. A refused action also produces an event - with
no changes and no new version - because a refusal is a fact the world model has
to be able to learn from.

Every timestamp comes from the injected Clock and every identifier from the
injected IdFactory, so a run is reproducible from its inputs alone.
"""
from __future__ import annotations

from typing import Any, NoReturn
from uuid import UUID

from pydantic import Field

from ethos.actions import (
    ActionContext,
    get_action,
    parse_parameters,
    with_environment,
)
from ethos.errors import (
    FailureCode,
    IdempotencyKeyReused,
    ProtocolError,
    RuleViolation,
    VersionConflict,
)
from ethos.models import (
    ChangeOp,
    ChangeSet,
    EntityChange,
    EntityType,
    Environment,
    EnvironmentEvent,
    EnvironmentRules,
    EnvironmentSnapshot,
    EnvironmentState,
    EnvironmentUser,
    Organization,
    OrgRole,
    digest_of,
)
from ethos.repository import EnvironmentHead, EnvironmentRepository
from ethos.runtime import (
    GENESIS_EVENT_DIGEST,
    GENESIS_STATE_HASH,
    Clock,
    FrozenModel,
    IdFactory,
    SystemClock,
    UuidFactory,
    canonical_json,
    sha256_hex,
    to_plain,
)
from ethos.state import apply_changes, diff, state_hash

__all__ = [
    "ActionRequest",
    "ExecutionResult",
    "EnvironmentService",
    "request_fingerprint",
]


class ActionRequest(FrozenModel):
    action_type: str
    actor_user_id: UUID
    parameters: dict[str, Any] = {}
    idempotency_key: str = Field(min_length=1, max_length=200)
    expected_version: int | None = None


class ExecutionResult(FrozenModel):
    event: EnvironmentEvent
    state: EnvironmentState
    replayed: bool = False


def request_fingerprint(
    action_type: str,
    actor_user_id: UUID,
    parameters: Any,
    expected_version: int | None,
) -> str:
    """Identifies the *shape* of a request, so one idempotency key can never
    silently serve a different action.

    `expected_version` is part of it: a retry that asserts a different version is
    a different request, and replaying the first result would tell the caller its
    assertion held when it did not.
    """
    return sha256_hex(
        canonical_json(
            {
                "action_type": action_type,
                "actor_user_id": actor_user_id,
                "parameters": parameters,
                "expected_version": expected_version,
            }
        )
    )


class EnvironmentService:
    def __init__(
        self,
        repository: EnvironmentRepository,
        environment_id: UUID,
        *,
        clock: Clock | None = None,
        ids: IdFactory | None = None,
    ) -> None:
        self._repository = repository
        self._environment_id = environment_id
        self._clock = clock or SystemClock()
        self._ids = ids or UuidFactory()

    @property
    def repository(self) -> EnvironmentRepository:
        return self._repository

    @property
    def environment_id(self) -> UUID:
        return self._environment_id

    def state(self) -> EnvironmentState:
        return self._repository.load(self._environment_id)

    def head(self) -> EnvironmentHead:
        return self._repository.head(self._environment_id)

    # --- bootstrap ------------------------------------------------------

    def bootstrap(
        self,
        *,
        name: str,
        org_name: str,
        org_slug: str,
        owner_handle: str,
        owner_display_name: str,
        description: str | None = None,
    ) -> ExecutionResult:
        """Create the environment, its organization and its first owner.

        This is the only operation that is not itself an action: it has no
        actor and starts from genesis.
        """
        head = self._repository.head(self._environment_id)
        if head.last_event_sequence != 0:
            raise ProtocolError("this environment has already been bootstrapped")

        now = self._clock.now()
        environment = Environment(
            id=self._environment_id,
            name=name,
            description=description,
            config=EnvironmentRules(),
            version=1,
            created_at=now,
            updated_at=now,
        )
        organization = Organization(
            id=self._ids.new_id(),
            environment_id=self._environment_id,
            name=org_name,
            slug=org_slug,
            created_at=now,
        )
        owner = EnvironmentUser(
            id=self._ids.new_id(),
            environment_id=self._environment_id,
            org_id=organization.id,
            handle=owner_handle.lower(),
            display_name=owner_display_name,
            org_role=OrgRole.OWNER,
            created_at=now,
        )
        changes = ChangeSet(
            changes=[
                EntityChange(
                    op=ChangeOp.CREATE,
                    entity_type=EntityType.ENVIRONMENT,
                    entity_id=environment.id,
                    after=to_plain(environment),
                ),
                EntityChange(
                    op=ChangeOp.CREATE,
                    entity_type=EntityType.ORGANIZATION,
                    entity_id=organization.id,
                    after=to_plain(organization),
                ),
                EntityChange(
                    op=ChangeOp.CREATE,
                    entity_type=EntityType.ENVIRONMENT_USER,
                    entity_id=owner.id,
                    after=to_plain(owner),
                ),
            ]
        )
        state = apply_changes(None, changes, version=1)

        parameters = {"name": name, "org_name": org_name, "org_slug": org_slug}
        event = self._build_event(
            sequence=1,
            action_type="environment.bootstrap",
            actor_user_id=owner.id,
            origin="bootstrap",
            parameters=parameters,
            changes=changes,
            status="succeeded",
            failure_code=None,
            failure_reason=None,
            failure_details=None,
            prev_state_version=0,
            prev_state_hash=GENESIS_STATE_HASH,
            resulting_state_version=1,
            resulting_state_hash=state_hash(state),
            idempotency_key="bootstrap",
            request_fingerprint=request_fingerprint(
                "environment.bootstrap", owner.id, parameters, None
            ),
            prev_event_digest=GENESIS_EVENT_DIGEST,
            created_at=now,
        )
        new_head = EnvironmentHead(
            environment_id=self._environment_id,
            version=1,
            last_event_sequence=1,
            last_event_digest=event.event_digest,
        )
        self._repository.append(state=state, head=new_head, event=event)
        self._save_snapshot(state, new_head, kind="bootstrap")
        return ExecutionResult(event=event, state=state)

    # --- execution ------------------------------------------------------

    def execute(self, request: ActionRequest) -> ExecutionResult:
        spec = get_action(request.action_type)
        parsed = parse_parameters(spec, request.parameters)
        normalized = to_plain(parsed)
        fingerprint = request_fingerprint(
            request.action_type,
            request.actor_user_id,
            normalized,
            request.expected_version,
        )

        recorded = self._repository.find_event(
            self._environment_id, request.idempotency_key
        )
        if recorded is not None:
            return self._replay(recorded, fingerprint)

        state = self._repository.load(self._environment_id)
        head = self._repository.head(self._environment_id)

        if (
            request.expected_version is not None
            and request.expected_version != head.version
        ):
            raise VersionConflict(
                f"expected version {request.expected_version}, "
                f"current version is {head.version}",
                details={"expected": request.expected_version, "actual": head.version},
            )

        actor = state.users.get(request.actor_user_id)
        now = self._clock.now()

        if actor is None or not actor.is_active:
            return self._record_failure(
                state,
                head,
                request,
                normalized,
                fingerprint,
                now,
                actor_user_id=None,
                code=FailureCode.ACTOR_NOT_FOUND,
                reason=(
                    f"actor {request.actor_user_id} does not exist or is inactive"
                ),
                details={"actor_user_id": str(request.actor_user_id)},
            )

        assert actor is not None  # narrowed by _record_failure raising

        context = ActionContext(
            state=state, actor=actor, now=now, ids=self._ids, params=parsed
        )
        try:
            candidate = spec.handler(context)
        except RuleViolation as exc:
            return self._record_failure(
                state,
                head,
                request,
                normalized,
                fingerprint,
                now,
                actor_user_id=actor.id,
                code=exc.failure_code,
                reason=exc.message,
                details=exc.details,
            )

        version = head.version + 1
        resulting = with_environment(candidate, version=version, updated_at=now)
        event = self._build_event(
            sequence=head.last_event_sequence + 1,
            action_type=request.action_type,
            actor_user_id=actor.id,
            origin="api",
            parameters=normalized,
            changes=diff(state, resulting),
            status="succeeded",
            failure_code=None,
            failure_reason=None,
            failure_details=None,
            prev_state_version=head.version,
            prev_state_hash=state_hash(state),
            resulting_state_version=version,
            resulting_state_hash=state_hash(resulting),
            idempotency_key=request.idempotency_key,
            request_fingerprint=fingerprint,
            prev_event_digest=head.last_event_digest,
            created_at=now,
        )
        new_head = EnvironmentHead(
            environment_id=self._environment_id,
            version=version,
            last_event_sequence=event.sequence,
            last_event_digest=event.event_digest,
        )
        stored = self._repository.append(
            state=resulting, head=new_head, event=event
        )
        if stored.id != event.id:
            # A concurrent request with the same key committed first. Its event
            # is authoritative; replay it rather than report a conflict.
            return self._replay(stored, fingerprint)
        self._save_snapshot(resulting, new_head, kind="auto")
        return ExecutionResult(event=event, state=resulting)

    # --- internals ------------------------------------------------------

    def _build_event(self, **values: Any) -> EnvironmentEvent:
        values["id"] = self._ids.new_id()
        values["environment_id"] = self._environment_id
        return EnvironmentEvent(**values, event_digest=digest_of(values))

    def _record_failure(
        self,
        state: EnvironmentState,
        head: EnvironmentHead,
        request: ActionRequest,
        normalized: Any,
        fingerprint: str,
        now: Any,
        *,
        actor_user_id: UUID | None,
        code: FailureCode,
        reason: str,
        details: dict[str, Any],
    ) -> ExecutionResult:
        """Persist the refusal, then surface it to the caller."""
        event = self._build_event(
            sequence=head.last_event_sequence + 1,
            action_type=request.action_type,
            actor_user_id=actor_user_id,
            origin="api",
            parameters=normalized,
            changes=ChangeSet(),
            status="failed",
            failure_code=code.value,
            failure_reason=reason,
            failure_details=details or None,
            prev_state_version=head.version,
            prev_state_hash=state_hash(state),
            resulting_state_version=None,
            resulting_state_hash=None,
            idempotency_key=request.idempotency_key,
            request_fingerprint=fingerprint,
            prev_event_digest=head.last_event_digest,
            created_at=now,
        )
        new_head = EnvironmentHead(
            environment_id=self._environment_id,
            version=head.version,
            last_event_sequence=event.sequence,
            last_event_digest=event.event_digest,
        )
        stored = self._repository.append(state=state, head=new_head, event=event)
        if stored.id != event.id:
            return self._replay(stored, fingerprint)
        raise RuleViolation(reason, failure_code=code, details=details or {})

    def _replay(
        self, recorded: EnvironmentEvent, fingerprint: str
    ) -> ExecutionResult:
        if recorded.request_fingerprint != fingerprint:
            raise IdempotencyKeyReused(
                f"idempotency key {recorded.idempotency_key!r} was used for a "
                "different request",
                details={"idempotency_key": recorded.idempotency_key},
            )
        if recorded.status == "failed":
            code = (
                FailureCode(recorded.failure_code)
                if recorded.failure_code
                else FailureCode.ACTION_FAILED
            )
            raise RuleViolation(
                recorded.failure_reason or "action failed",
                failure_code=code,
                details=recorded.failure_details or {},
            )
        return ExecutionResult(
            event=recorded,
            state=self._repository.load(self._environment_id),
            replayed=True,
        )

    def _save_snapshot(
        self, state: EnvironmentState, head: EnvironmentHead, *, kind: str
    ) -> None:
        if kind == "auto":
            interval = state.environment.config.snapshot_interval
            if interval <= 0 or head.last_event_sequence % interval != 0:
                return
        snapshot = EnvironmentSnapshot(
            id=self._ids.new_id(),
            environment_id=self._environment_id,
            version=state.version,
            last_event_sequence=head.last_event_sequence,
            state=state,
            state_hash=state_hash(state),
            kind=kind,
            created_at=self._clock.now(),
        )
        self._repository.save_snapshot(snapshot)
