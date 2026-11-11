"""State hashing, diffing, delta application, replay and integrity checks.

Nothing here performs I/O. `apply_changes` folds a recorded ChangeSet into a new
state and is the single mechanism behind both live commits and verification
replay - which is why changing a business rule cannot rewrite history.
"""
from __future__ import annotations

from typing import Any, Literal, Sequence

from ethos.models import (
    ChangeOp,
    ChangeSet,
    EntityChange,
    EntityType,
    EnvironmentEvent,
    EnvironmentSnapshot,
    EnvironmentState,
    FieldChange,
)
from ethos.runtime import (
    GENESIS_EVENT_DIGEST,
    GENESIS_STATE_HASH,
    FrozenModel,
    canonical_json,
    sha256_hex,
    to_plain,
)

__all__ = [
    "STATE_COLLECTIONS",
    "state_hash",
    "diff",
    "apply_changes",
    "replay",
    "Divergence",
    "ChainReport",
    "verify_event_chain",
]


# Entity type -> EnvironmentState attribute name.
STATE_COLLECTIONS: dict[EntityType, str] = {
    EntityType.ORGANIZATION: "organizations",
    EntityType.ENVIRONMENT_USER: "users",
    EntityType.TEAM: "teams",
    EntityType.TEAM_MEMBERSHIP: "team_memberships",
    EntityType.PROJECT: "projects",
    EntityType.PROJECT_MEMBERSHIP: "project_memberships",
    EntityType.TASK: "tasks",
    EntityType.TASK_ASSIGNMENT: "task_assignments",
    EntityType.TASK_DEPENDENCY: "task_dependencies",
}

COLLECTION_NAMES: tuple[str, ...] = tuple(STATE_COLLECTIONS.values())


def state_hash(state: EnvironmentState) -> str:
    """Content hash of a whole environment. Sorted keys, no whitespace."""
    return sha256_hex(canonical_json(state))


def _field_changes(before: Any, after: Any) -> list[FieldChange]:
    old = to_plain(before)
    new = to_plain(after)
    changes: list[FieldChange] = []
    for name in type(before).model_fields:
        if old[name] != new[name]:
            changes.append(FieldChange(field=name, before=old[name], after=new[name]))
    return changes


def _sorted(changes: list[EntityChange]) -> list[EntityChange]:
    return sorted(
        changes,
        key=lambda c: (c.entity_type.value, str(c.entity_id), c.op.value),
    )


def diff(before: EnvironmentState, after: EnvironmentState) -> ChangeSet:
    """Structured, lossless difference between two states.

    `create` and `delete` carry the full entity; `update` carries exactly the
    differing fields with both values, so the result can be replayed without
    reading any other source.
    """
    changes: list[EntityChange] = []

    if before.environment != after.environment:
        fields = _field_changes(before.environment, after.environment)
        if fields:
            changes.append(
                EntityChange(
                    op=ChangeOp.UPDATE,
                    entity_type=EntityType.ENVIRONMENT,
                    entity_id=after.environment.id,
                    fields=fields,
                )
            )

    for entity_type, attribute in STATE_COLLECTIONS.items():
        old = getattr(before, attribute)
        new = getattr(after, attribute)

        for key in sorted(set(old) - set(new), key=str):
            changes.append(
                EntityChange(
                    op=ChangeOp.DELETE,
                    entity_type=entity_type,
                    entity_id=key,
                    before=to_plain(old[key]),
                )
            )
        for key in sorted(set(new) - set(old), key=str):
            changes.append(
                EntityChange(
                    op=ChangeOp.CREATE,
                    entity_type=entity_type,
                    entity_id=key,
                    after=to_plain(new[key]),
                )
            )
        for key in sorted(set(old) & set(new), key=str):
            fields = _field_changes(old[key], new[key])
            if fields:
                changes.append(
                    EntityChange(
                        op=ChangeOp.UPDATE,
                        entity_type=entity_type,
                        entity_id=key,
                        fields=fields,
                    )
                )

    return ChangeSet(changes=_sorted(changes))


def _empty_work() -> dict[str, Any]:
    work: dict[str, Any] = {"environment": None}
    for name in COLLECTION_NAMES:
        work[name] = {}
    return work


def _to_work(state: EnvironmentState | None) -> dict[str, Any]:
    if state is None:
        return _empty_work()
    plain = to_plain(state)
    work: dict[str, Any] = {"environment": plain["environment"]}
    for name in COLLECTION_NAMES:
        work[name] = dict(plain[name])
    return work


def _apply_one(current: dict[str, Any] | None, change: EntityChange) -> dict[str, Any]:
    if change.op is ChangeOp.CREATE:
        if current is not None:
            raise ValueError(
                f"{change.entity_type.value} {change.entity_id} already exists"
            )
        if change.after is None:
            raise ValueError(f"create for {change.entity_id} has no payload")
        return dict(change.after)

    if current is None:
        raise ValueError(
            f"{change.entity_type.value} {change.entity_id} does not exist"
        )
    if change.op is ChangeOp.UPDATE:
        updated = dict(current)
        for field_change in change.fields:
            updated[field_change.field] = field_change.after
        return updated
    raise ValueError("delete must be handled by the caller")


def apply_changes(
    state: EnvironmentState | None,
    changes: ChangeSet,
    *,
    version: int | None = None,
) -> EnvironmentState:
    """Replay a recorded ChangeSet. `state=None` means before genesis."""
    work = _to_work(state)

    for change in changes.changes:
        if change.entity_type is EntityType.ENVIRONMENT:
            work["environment"] = _apply_one(work["environment"], change)
            continue

        bucket = work[STATE_COLLECTIONS[change.entity_type]]
        key = str(change.entity_id)

        if change.op is ChangeOp.DELETE:
            if key not in bucket:
                raise ValueError(
                    f"{change.entity_type.value} {key} does not exist"
                )
            del bucket[key]
            continue

        bucket[key] = _apply_one(bucket.get(key), change)

    if work["environment"] is None:
        raise ValueError("change set did not create an environment")

    if version is not None:
        work["environment"]["version"] = version

    return EnvironmentState.model_validate(work)


def replay(
    events: Sequence[EnvironmentEvent],
    *,
    snapshot: EnvironmentSnapshot | None = None,
) -> EnvironmentState:
    """Rebuild state from a snapshot plus the events after it."""
    state = snapshot.state if snapshot is not None else None
    start = snapshot.last_event_sequence if snapshot is not None else 0

    for event in events:
        if event.sequence <= start:
            continue
        if event.status != "succeeded":
            continue
        if event.resulting_state_version is None:
            raise ValueError(f"succeeded event {event.sequence} has no resulting version")
        state = apply_changes(
            state, event.changes, version=event.resulting_state_version
        )

    if state is None:
        raise ValueError("no successful events to replay")
    return state


class Divergence(FrozenModel):
    step: Literal["head", "digest", "state", "reconcile"]
    code: str
    sequence: int | None = None
    expected: str | None = None
    actual: str | None = None


class ChainReport(FrozenModel):
    valid: bool
    events_checked: int
    deep: bool
    final_version: int
    final_hash: str
    head: dict[str, Any]
    divergence: Divergence | None = None


def _report(
    *,
    valid: bool,
    checked: int,
    deep: bool,
    version: int,
    hash_: str,
    head_sequence: int,
    head_digest: str,
    divergence: Divergence | None,
) -> ChainReport:
    return ChainReport(
        valid=valid,
        events_checked=checked,
        deep=deep,
        final_version=version,
        final_hash=hash_,
        head={"sequence": head_sequence, "digest": head_digest},
        divergence=divergence,
    )


def verify_event_chain(
    events: Sequence[EnvironmentEvent],
    *,
    head_sequence: int,
    head_digest: str,
    current_state: EnvironmentState | None = None,
    deep: bool = True,
) -> ChainReport:
    """Three-pass verification: head, digest chain, state replay + reconciliation.

    Pass 0 (O(1)) catches trailing deletion, which state reconciliation cannot:
    deleting a trailing failure leaves the state unchanged.
    Pass 1 (O(E)) recomputes every event digest, including failed events, over
    all immutable metadata.
    Pass 2/3 replay recorded deltas and compare against the live state.
    """
    ordered = tuple(events)
    total = len(ordered)

    # --- Pass 0: stored head vs log tail --------------------------------
    if not ordered:
        if head_sequence != 0:
            return _report(
                valid=False, checked=0, deep=deep, version=0,
                hash_=GENESIS_STATE_HASH, head_sequence=head_sequence,
                head_digest=head_digest,
                divergence=Divergence(step="head", code="LOG_EMPTY_HEAD_NONZERO"),
            )
    else:
        tail = ordered[-1]
        head_code: str | None = None
        if head_sequence > tail.sequence:
            head_code = "TRAILING_EVENTS_DELETED"
        elif head_sequence < tail.sequence:
            head_code = "HEAD_BEHIND_LOG"
        elif head_digest != tail.event_digest:
            head_code = "HEAD_DIGEST_MISMATCH"
        if head_code is not None:
            return _report(
                valid=False, checked=total, deep=deep, version=0,
                hash_=GENESIS_STATE_HASH, head_sequence=head_sequence,
                head_digest=head_digest,
                divergence=Divergence(
                    step="head", code=head_code, sequence=tail.sequence,
                    expected=tail.event_digest, actual=head_digest,
                ),
            )

    # --- Pass 1: digest chain over every event -------------------------
    previous_digest = GENESIS_EVENT_DIGEST
    expected_sequence = 1
    for event in ordered:
        if event.sequence != expected_sequence:
            return _report(
                valid=False, checked=total, deep=deep, version=0,
                hash_=GENESIS_STATE_HASH, head_sequence=head_sequence,
                head_digest=head_digest,
                divergence=Divergence(
                    step="digest", code="SEQUENCE_GAP", sequence=event.sequence,
                    expected=str(expected_sequence), actual=str(event.sequence),
                ),
            )
        if event.prev_event_digest != previous_digest:
            return _report(
                valid=False, checked=total, deep=deep, version=0,
                hash_=GENESIS_STATE_HASH, head_sequence=head_sequence,
                head_digest=head_digest,
                divergence=Divergence(
                    step="digest", code="PREV_DIGEST_MISMATCH",
                    sequence=event.sequence, expected=previous_digest,
                    actual=event.prev_event_digest,
                ),
            )
        recomputed = event.compute_digest()
        if recomputed != event.event_digest:
            return _report(
                valid=False, checked=total, deep=deep, version=0,
                hash_=GENESIS_STATE_HASH, head_sequence=head_sequence,
                head_digest=head_digest,
                divergence=Divergence(
                    step="digest", code="EVENT_DIGEST_MISMATCH",
                    sequence=event.sequence, expected=event.event_digest,
                    actual=recomputed,
                ),
            )
        previous_digest = event.event_digest
        expected_sequence += 1

    # --- Pass 2: state transitions -------------------------------------
    state: EnvironmentState | None = None
    version = 0
    running_hash = GENESIS_STATE_HASH

    for event in ordered:
        if event.status == "failed":
            if (
                event.resulting_state_version is not None
                or event.resulting_state_hash is not None
                or len(event.changes.changes) > 0
            ):
                return _report(
                    valid=False, checked=total, deep=deep, version=version,
                    hash_=running_hash, head_sequence=head_sequence,
                    head_digest=head_digest,
                    divergence=Divergence(
                        step="state", code="FAILED_EVENT_MUTATED_STATE",
                        sequence=event.sequence,
                    ),
                )
            if event.prev_state_version != version or event.prev_state_hash != running_hash:
                return _report(
                    valid=False, checked=total, deep=deep, version=version,
                    hash_=running_hash, head_sequence=head_sequence,
                    head_digest=head_digest,
                    divergence=Divergence(
                        step="state", code="FAILED_EVENT_PREV_MISMATCH",
                        sequence=event.sequence, expected=running_hash,
                        actual=event.prev_state_hash,
                    ),
                )
            continue

        if event.prev_state_version != version:
            return _report(
                valid=False, checked=total, deep=deep, version=version,
                hash_=running_hash, head_sequence=head_sequence,
                head_digest=head_digest,
                divergence=Divergence(
                    step="state", code="PREV_VERSION_MISMATCH",
                    sequence=event.sequence, expected=str(version),
                    actual=str(event.prev_state_version),
                ),
            )
        if event.prev_state_hash != running_hash:
            return _report(
                valid=False, checked=total, deep=deep, version=version,
                hash_=running_hash, head_sequence=head_sequence,
                head_digest=head_digest,
                divergence=Divergence(
                    step="state", code="PREV_HASH_MISMATCH",
                    sequence=event.sequence, expected=running_hash,
                    actual=event.prev_state_hash,
                ),
            )
        if event.resulting_state_version != version + 1:
            return _report(
                valid=False, checked=total, deep=deep, version=version,
                hash_=running_hash, head_sequence=head_sequence,
                head_digest=head_digest,
                divergence=Divergence(
                    step="state", code="VERSION_STEP_INVALID",
                    sequence=event.sequence, expected=str(version + 1),
                    actual=str(event.resulting_state_version),
                ),
            )

        if event.resulting_state_version is None:
            raise ValueError("unreachable: version step validated above")

        if deep:
            state = apply_changes(
                state, event.changes, version=event.resulting_state_version
            )
            running_hash = state_hash(state)
            if running_hash != event.resulting_state_hash:
                return _report(
                    valid=False, checked=total, deep=deep,
                    version=event.resulting_state_version, hash_=running_hash,
                    head_sequence=head_sequence, head_digest=head_digest,
                    divergence=Divergence(
                        step="state", code="CONTENT_HASH_MISMATCH",
                        sequence=event.sequence,
                        expected=event.resulting_state_hash, actual=running_hash,
                    ),
                )
        else:
            running_hash = event.resulting_state_hash or running_hash

        version = event.resulting_state_version

    # --- Pass 3: reconciliation against the live state ------------------
    if deep and current_state is not None:
        if version != current_state.version:
            return _report(
                valid=False, checked=total, deep=deep, version=version,
                hash_=running_hash, head_sequence=head_sequence,
                head_digest=head_digest,
                divergence=Divergence(
                    step="reconcile", code="LIVE_VERSION_MISMATCH",
                    expected=str(current_state.version), actual=str(version),
                ),
            )
        live_hash = state_hash(current_state)
        if running_hash != live_hash:
            return _report(
                valid=False, checked=total, deep=deep, version=version,
                hash_=running_hash, head_sequence=head_sequence,
                head_digest=head_digest,
                divergence=Divergence(
                    step="reconcile", code="LIVE_HASH_MISMATCH",
                    expected=live_hash, actual=running_hash,
                ),
            )
        if state is not None and to_plain(state) != to_plain(current_state):
            return _report(
                valid=False, checked=total, deep=deep, version=version,
                hash_=running_hash, head_sequence=head_sequence,
                head_digest=head_digest,
                divergence=Divergence(
                    step="reconcile", code="LIVE_CONTENT_MISMATCH",
                ),
            )

    return _report(
        valid=True, checked=total, deep=deep, version=version,
        hash_=running_hash, head_sequence=head_sequence,
        head_digest=head_digest, divergence=None,
    )
