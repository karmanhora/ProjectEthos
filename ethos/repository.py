"""Persistence boundary.

The protocol is written against snapshot + replay so that the Postgres
implementation has the same shape as the in-memory one: `load` always rebuilds
state from the log rather than returning a cached mutable object, and `append`
is the single atomic commit point.

Phase 1 ships only the in-memory implementation, which is what lets the whole
system be exercised without a database or credentials.
"""
from __future__ import annotations

from threading import RLock
from typing import Protocol
from uuid import UUID

from pydantic import Field

from ethos.errors import NotFoundError, ProtocolError, VersionConflict
from ethos.models import EnvironmentEvent, EnvironmentSnapshot, EnvironmentState
from ethos.runtime import GENESIS_EVENT_DIGEST, FrozenModel
from ethos.state import replay

__all__ = [
    "EnvironmentHead",
    "EnvironmentRepository",
    "InMemoryRepository",
    "VerificationView",
    "genesis_head",
]


class EnvironmentHead(FrozenModel):
    """Where the log currently ends. Stored beside the log, never inside the
    hashed state - see the note on `models.Environment`."""

    environment_id: UUID
    version: int = Field(ge=0)
    last_event_sequence: int = Field(ge=0)
    last_event_digest: str = Field(min_length=64, max_length=64)


def genesis_head(environment_id: UUID) -> EnvironmentHead:
    return EnvironmentHead(
        environment_id=environment_id,
        version=0,
        last_event_sequence=0,
        last_event_digest=GENESIS_EVENT_DIGEST,
    )


class VerificationView(FrozenModel):
    """Head, log and state read from one snapshot.

    Verifying integrity against separately-read parts can report a divergence
    that never existed: an event can be committed between reading the head and
    reading the log. All three must come from one consistent read.
    """

    head: EnvironmentHead
    events: list[EnvironmentEvent] = []
    state: EnvironmentState


class EnvironmentRepository(Protocol):
    def head(self, environment_id: UUID) -> EnvironmentHead: ...

    def load(self, environment_id: UUID) -> EnvironmentState: ...

    def events(
        self, environment_id: UUID, *, after_sequence: int = 0
    ) -> list[EnvironmentEvent]: ...

    def find_event(
        self, environment_id: UUID, idempotency_key: str
    ) -> EnvironmentEvent | None: ...

    def latest_snapshot(self, environment_id: UUID) -> EnvironmentSnapshot | None: ...

    def append(
        self,
        *,
        state: EnvironmentState,
        head: EnvironmentHead,
        event: EnvironmentEvent,
    ) -> EnvironmentEvent:
        """Commit one event.

        Returns the event that is actually stored. When another writer already
        committed the same idempotency key, that stored event is returned
        instead of the one offered here, so the caller can replay its outcome.
        """
        ...

    def verification_view(self, environment_id: UUID) -> VerificationView: ...

    def save_snapshot(self, snapshot: EnvironmentSnapshot) -> None: ...


class InMemoryRepository:
    """Reference implementation. The lock makes a commit atomic."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._heads: dict[UUID, EnvironmentHead] = {}
        self._events: dict[UUID, list[EnvironmentEvent]] = {}
        self._idempotency: dict[tuple[UUID, str], EnvironmentEvent] = {}
        self._snapshots: dict[UUID, list[EnvironmentSnapshot]] = {}

    # --- reads ----------------------------------------------------------

    def head(self, environment_id: UUID) -> EnvironmentHead:
        with self._lock:
            return self._heads.get(environment_id, genesis_head(environment_id))

    def events(
        self, environment_id: UUID, *, after_sequence: int = 0
    ) -> list[EnvironmentEvent]:
        with self._lock:
            return [
                event
                for event in self._events.get(environment_id, ())
                if event.sequence > after_sequence
            ]

    def latest_snapshot(self, environment_id: UUID) -> EnvironmentSnapshot | None:
        with self._lock:
            snapshots = self._snapshots.get(environment_id)
            if not snapshots:
                return None
            return max(snapshots, key=lambda snapshot: snapshot.last_event_sequence)

    def find_event(
        self, environment_id: UUID, idempotency_key: str
    ) -> EnvironmentEvent | None:
        with self._lock:
            return self._idempotency.get((environment_id, idempotency_key))

    def load(self, environment_id: UUID) -> EnvironmentState:
        snapshot = self.latest_snapshot(environment_id)
        start = snapshot.last_event_sequence if snapshot is not None else 0
        events = self.events(environment_id, after_sequence=start)
        if snapshot is None and not events:
            raise NotFoundError(
                f"environment {environment_id} does not exist",
                details={"environment_id": str(environment_id)},
            )
        return replay(events, snapshot=snapshot)

    # --- writes ---------------------------------------------------------

    def append(
        self,
        *,
        state: EnvironmentState,
        head: EnvironmentHead,
        event: EnvironmentEvent,
    ) -> EnvironmentEvent:
        """Commit one event. Every check here is an internal invariant, so a
        violation means the service produced an impossible transition."""
        with self._lock:
            environment_id = event.environment_id
            if not (
                state.environment_id == environment_id == head.environment_id
            ):
                raise ProtocolError("event, state and head disagree on the environment")

            key = (environment_id, event.idempotency_key)
            stored = self._idempotency.get(key)
            if stored is not None:
                # A concurrent duplicate lost the race. Returning the stored
                # event lets the caller replay it rather than report a conflict.
                return stored

            if event.event_digest != event.compute_digest():
                raise ProtocolError(
                    f"event {event.sequence} digest does not match its contents"
                )

            existing = self._events.get(environment_id, [])
            previous = existing[-1] if existing else None
            base_version = self._heads.get(
                environment_id, genesis_head(environment_id)
            ).version

            if previous is None:
                if event.sequence != 1:
                    raise ProtocolError("the first event must have sequence 1")
                if event.prev_state_version != 0:
                    raise ProtocolError("the first event must start from version 0")
                if event.prev_event_digest != GENESIS_EVENT_DIGEST:
                    raise ProtocolError("the first event must chain from genesis")
            else:
                if (
                    event.sequence != previous.sequence + 1
                    or event.prev_state_version != base_version
                ):
                    # Checked before the digest: a stale writer built its event
                    # from an older head, so its chain link legitimately differs.
                    # That is a retryable conflict, not a broken invariant.
                    raise VersionConflict(
                        "the environment changed while this action was being applied",
                        details={
                            "environment_id": str(environment_id),
                            "expected_version": event.prev_state_version,
                            "current_version": base_version,
                        },
                    )
                if event.prev_event_digest != previous.event_digest:
                    raise ProtocolError(
                        f"event {event.sequence} does not chain to its predecessor"
                    )

            expected_version = (
                event.resulting_state_version
                if event.status == "succeeded"
                else base_version
            )
            if event.status == "failed":
                if event.resulting_state_version is not None:
                    raise ProtocolError("a failed event must not record a new version")
                if event.changes.changes:
                    raise ProtocolError("a failed event must not record changes")
            if head.version != expected_version:
                raise ProtocolError(
                    f"head version {head.version} does not match the committed state "
                    f"{expected_version}"
                )
            if head.last_event_sequence != event.sequence:
                raise ProtocolError("head sequence does not match the appended event")
            if head.last_event_digest != event.event_digest:
                raise ProtocolError("head digest does not match the appended event")
            if state.version != expected_version:
                raise ProtocolError(
                    "state version does not match the version the event records"
                )

            self._events.setdefault(environment_id, []).append(event)
            self._idempotency[key] = event
            self._heads[environment_id] = head
            return event

    def verification_view(self, environment_id: UUID) -> VerificationView:
        """Head, log and state under one lock, so they cannot disagree."""
        with self._lock:
            events = list(self._events.get(environment_id, ()))
            snapshot = self.latest_snapshot(environment_id)
            if snapshot is None and not events:
                raise NotFoundError(
                    f"environment {environment_id} does not exist",
                    details={"environment_id": str(environment_id)},
                )
            start = snapshot.last_event_sequence if snapshot is not None else 0
            state = replay(
                [event for event in events if event.sequence > start],
                snapshot=snapshot,
            )
            return VerificationView(
                head=self._heads.get(environment_id, genesis_head(environment_id)),
                events=events,
                state=state,
            )

    def save_snapshot(self, snapshot: EnvironmentSnapshot) -> None:
        with self._lock:
            snapshots = self._snapshots.setdefault(snapshot.environment_id, [])
            for index, existing in enumerate(snapshots):
                if existing.version == snapshot.version:
                    snapshots[index] = snapshot
                    return
            snapshots.append(snapshot)
