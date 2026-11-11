"""Postgres implementation of the repository protocol.

A commit is one transaction: the environment head is advanced and the event is
inserted together, or neither happens. That is what keeps the stored head and
the log from ever disagreeing, which every integrity check depends on.

The head update is conditional on the version and sequence the caller believed
it was appending to. A concurrent writer therefore loses the update and gets a
`VersionConflict` instead of silently appending to a stale head; the unique
idempotency constraint is the second line of defence for retried requests.

Reads always rebuild state from a snapshot plus the events after it. Nothing is
cached across calls, so a caller can never observe a partially applied commit.
"""
from __future__ import annotations

from typing import Any, Mapping

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ethos.errors import (
    IDEMPOTENCY_UNIQUE_CONSTRAINT,
    IdempotencyKeyReused,
    NotFoundError,
    ProtocolError,
    VersionConflict,
)
from ethos.models import (
    ChangeOp,
    ChangeSet,
    EntityType,
    EnvironmentEvent,
    EnvironmentSnapshot,
    EnvironmentState,
)
from ethos.repository import EnvironmentHead, VerificationView, genesis_head
from ethos.runtime import to_plain
from ethos.state import replay

__all__ = ["PostgresRepository"]

_EVENT_COLUMNS = (
    "id",
    "environment_id",
    "sequence",
    "action_type",
    "actor_user_id",
    "origin",
    "parameters",
    "changes",
    "status",
    "failure_code",
    "failure_reason",
    "failure_details",
    "prev_state_version",
    "prev_state_hash",
    "resulting_state_version",
    "resulting_state_hash",
    "idempotency_key",
    "request_fingerprint",
    "prev_event_digest",
    "event_digest",
    "created_at",
)

_SELECT_EVENTS = (
    f"select {', '.join(_EVENT_COLUMNS)} from environment_events "
    "where environment_id = %s and sequence > %s order by sequence"
)

_SELECT_EVENTS_ALL = (
    f"select {', '.join(_EVENT_COLUMNS)} from environment_events "
    "where environment_id = %s order by sequence"
)

_SELECT_EVENT_BY_KEY = (
    f"select {', '.join(_EVENT_COLUMNS)} from environment_events "
    "where environment_id = %s and idempotency_key = %s"
)

_INSERT_EVENT = (
    "insert into environment_events ("
    + ", ".join(_EVENT_COLUMNS)
    + ") values ("
    + ", ".join(["%s"] * len(_EVENT_COLUMNS))
    + ")"
)

_SELECT_HEAD = (
    "select version, last_event_sequence, last_event_digest "
    "from environments where id = %s"
)

_SELECT_SNAPSHOT = (
    "select id, environment_id, version, last_event_sequence, state, state_hash, "
    "kind, label, created_at from environment_snapshots "
    "where environment_id = %s order by last_event_sequence desc, version desc limit 1"
)

_INSERT_SNAPSHOT = (
    "insert into environment_snapshots "
    "(id, environment_id, version, last_event_sequence, state, state_hash, kind, "
    "label, created_at) values (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
    "on conflict (environment_id, version) do update set "
    "state = excluded.state, "
    "state_hash = excluded.state_hash, "
    "last_event_sequence = excluded.last_event_sequence, "
    "kind = excluded.kind, "
    "label = excluded.label"
)


def _event_from_row(row: Mapping[str, Any]) -> EnvironmentEvent:
    return EnvironmentEvent(
        id=row["id"],
        environment_id=row["environment_id"],
        sequence=row["sequence"],
        action_type=row["action_type"],
        actor_user_id=row["actor_user_id"],
        origin=row["origin"],
        parameters=row["parameters"] or {},
        changes=ChangeSet.model_validate(row["changes"]),
        status=row["status"],
        failure_code=row["failure_code"],
        failure_reason=row["failure_reason"],
        failure_details=row["failure_details"],
        prev_state_version=row["prev_state_version"],
        prev_state_hash=row["prev_state_hash"],
        resulting_state_version=row["resulting_state_version"],
        resulting_state_hash=row["resulting_state_hash"],
        idempotency_key=row["idempotency_key"],
        request_fingerprint=row["request_fingerprint"],
        prev_event_digest=row["prev_event_digest"],
        event_digest=row["event_digest"],
        created_at=row["created_at"],
    )


def _event_values(event: EnvironmentEvent) -> tuple[Any, ...]:
    return (
        event.id,
        event.environment_id,
        event.sequence,
        event.action_type,
        event.actor_user_id,
        event.origin,
        Jsonb(to_plain(event.parameters)),
        Jsonb(to_plain(event.changes)),
        event.status,
        event.failure_code,
        event.failure_reason,
        None if event.failure_details is None else Jsonb(to_plain(event.failure_details)),
        event.prev_state_version,
        event.prev_state_hash,
        event.resulting_state_version,
        event.resulting_state_hash,
        event.idempotency_key,
        event.request_fingerprint,
        event.prev_event_digest,
        event.event_digest,
        event.created_at,
    )


def _snapshot_from_row(row: Mapping[str, Any]) -> EnvironmentSnapshot:
    return EnvironmentSnapshot(
        id=row["id"],
        environment_id=row["environment_id"],
        version=row["version"],
        last_event_sequence=row["last_event_sequence"],
        state=EnvironmentState.model_validate(row["state"]),
        state_hash=row["state_hash"],
        kind=row["kind"],
        label=row["label"],
        created_at=row["created_at"],
    )


def _column_value(value: Any) -> Any:
    """`to_plain` yields JSON-compatible data; jsonb columns need a wrapper."""
    return Jsonb(value) if isinstance(value, (dict, list)) else value


# State is kept in the entity tables as well as in the log, so the database is
# queryable without replaying history. The mapping is explicit: a state
# collection name is not always a table name (`users` is `environment_users`).
_ENTITY_TABLES = {
    EntityType.ORGANIZATION: "organizations",
    EntityType.ENVIRONMENT_USER: "environment_users",
    EntityType.TEAM: "teams",
    EntityType.TEAM_MEMBERSHIP: "team_memberships",
    EntityType.PROJECT: "projects",
    EntityType.PROJECT_MEMBERSHIP: "project_memberships",
    EntityType.TASK: "tasks",
    EntityType.TASK_ASSIGNMENT: "task_assignments",
    EntityType.TASK_DEPENDENCY: "task_dependencies",
}


def _apply_change(
    connection: psycopg.Connection, change: Any, environment_id: Any
) -> None:
    """Fold one recorded entity change into its table.

    Driven by the ChangeSet, never by the state snapshot, so the tables and the
    log are written from the same evidence and cannot diverge.
    """
    table = _ENTITY_TABLES[change.entity_type]

    if change.op is ChangeOp.DELETE:
        connection.execute(
            f"delete from {table} where id = %s and environment_id = %s",
            (change.entity_id, environment_id),
        )
        return

    if change.op is ChangeOp.CREATE:
        payload = dict(change.after or {})
        columns = list(payload)
        connection.execute(
            f"insert into {table} ({', '.join(columns)}) values "
            f"({', '.join(['%s'] * len(columns))})",
            [_column_value(payload[column]) for column in columns],
        )
        return

    updates = {field.field: field.after for field in change.fields}
    if not updates:
        return
    columns = list(updates)
    connection.execute(
        f"update {table} set {', '.join(f'{c} = %s' for c in columns)} "
        "where id = %s and environment_id = %s",
        [_column_value(updates[column]) for column in columns]
        + [change.entity_id, environment_id],
    )


class PostgresRepository:
    """One connection per operation. A pool belongs here once concurrency does."""

    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError("PostgresRepository requires a connection string")
        self._dsn = dsn

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn, row_factory=dict_row)

    # --- reads ----------------------------------------------------------

    def head(self, environment_id: Any) -> EnvironmentHead:
        with self._connect() as connection:
            row = connection.execute(_SELECT_HEAD, (environment_id,)).fetchone()
        if row is None:
            return genesis_head(environment_id)
        return EnvironmentHead(
            environment_id=environment_id,
            version=row["version"],
            last_event_sequence=row["last_event_sequence"],
            last_event_digest=row["last_event_digest"],
        )

    def events(
        self, environment_id: Any, *, after_sequence: int = 0
    ) -> list[EnvironmentEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                _SELECT_EVENTS, (environment_id, after_sequence)
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    def find_event(
        self, environment_id: Any, idempotency_key: str
    ) -> EnvironmentEvent | None:
        with self._connect() as connection:
            row = connection.execute(
                _SELECT_EVENT_BY_KEY, (environment_id, idempotency_key)
            ).fetchone()
        return None if row is None else _event_from_row(row)

    def latest_snapshot(self, environment_id: Any) -> EnvironmentSnapshot | None:
        with self._connect() as connection:
            row = connection.execute(_SELECT_SNAPSHOT, (environment_id,)).fetchone()
        return None if row is None else _snapshot_from_row(row)

    def verification_view(self, environment_id: Any) -> VerificationView:
        """Head, log and state from one REPEATABLE READ snapshot.

        Read across three separate transactions they can disagree: an event
        committed between reading the head and reading the log looks exactly like
        a tampered chain. One snapshot makes the comparison meaningful.
        """
        connection = self._connect()
        try:
            connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            connection.read_only = True
            with connection:
                head_row = connection.execute(
                    _SELECT_HEAD, (environment_id,)
                ).fetchone()
                event_rows = connection.execute(
                    _SELECT_EVENTS_ALL, (environment_id,)
                ).fetchall()
                snapshot_row = connection.execute(
                    _SELECT_SNAPSHOT, (environment_id,)
                ).fetchone()
        finally:
            connection.close()

        events = [_event_from_row(row) for row in event_rows]
        if head_row is None and not events:
            raise NotFoundError(
                f"environment {environment_id} does not exist",
                details={"environment_id": str(environment_id)},
            )
        head = (
            genesis_head(environment_id)
            if head_row is None
            else EnvironmentHead(
                environment_id=environment_id,
                version=head_row["version"],
                last_event_sequence=head_row["last_event_sequence"],
                last_event_digest=head_row["last_event_digest"],
            )
        )
        snapshot = None if snapshot_row is None else _snapshot_from_row(snapshot_row)
        start = snapshot.last_event_sequence if snapshot is not None else 0
        state = replay(
            [event for event in events if event.sequence > start], snapshot=snapshot
        )
        return VerificationView(head=head, events=events, state=state)

    def load(self, environment_id: Any) -> EnvironmentState:
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
        environment = state.environment
        try:
            with self._connect() as connection, connection.transaction():
                # READ COMMITTED (the default) plus a per-environment advisory
                # lock. The advisory lock also covers bootstrap, where there is
                # no environment row to lock yet.
                connection.execute(
                    "select pg_advisory_xact_lock(hashtext(%s))",
                    (str(environment.id),),
                )
                # Checked under the lock: two simultaneous identical requests
                # produce one event and one replay, never a spurious conflict.
                existing = connection.execute(
                    _SELECT_EVENT_BY_KEY, (environment.id, event.idempotency_key)
                ).fetchone()
                if existing is not None:
                    return _event_from_row(existing)

                if event.sequence == 1:
                    connection.execute(
                        "insert into environments (id, name, description, config, "
                        "version, last_event_sequence, last_event_digest, status, "
                        "created_at, updated_at) "
                        "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (
                            environment.id,
                            environment.name,
                            environment.description,
                            Jsonb(to_plain(environment.config)),
                            head.version,
                            head.last_event_sequence,
                            head.last_event_digest,
                            environment.status,
                            environment.created_at,
                            environment.updated_at,
                        ),
                    )
                else:
                    # Lock the row before allocating the next sequence, so the
                    # head is read and advanced under one writer at a time.
                    head_row = connection.execute(
                        "select version, last_event_sequence, last_event_digest "
                        "from environments where id = %s for update",
                        (environment.id,),
                    ).fetchone()
                    if head_row is None:
                        raise ProtocolError(
                            f"environment {environment.id} has no row to append to"
                        )
                    if (
                        head_row["version"] != event.prev_state_version
                        or head_row["last_event_sequence"] != event.sequence - 1
                        or head_row["last_event_digest"] != event.prev_event_digest
                    ):
                        raise VersionConflict(
                            "the environment changed while this action was being "
                            "applied; retry with the current version",
                            details={
                                "environment_id": str(environment.id),
                                "expected_version": event.prev_state_version,
                                "current_version": head_row["version"],
                                "sequence": event.sequence,
                            },
                        )
                    cursor = connection.execute(
                        "update environments set version = %s, "
                        "last_event_sequence = %s, last_event_digest = %s, "
                        "updated_at = %s where id = %s",
                        (
                            head.version,
                            head.last_event_sequence,
                            head.last_event_digest,
                            environment.updated_at,
                            environment.id,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ProtocolError("environment row vanished mid-transaction")
                # Entities are written before the event: the event references
                # the actor, and on bootstrap the actor is created by it.
                for change in event.changes.changes:
                    if change.entity_type is EntityType.ENVIRONMENT:
                        # Written above, including the head columns, which are
                        # not part of the recorded change.
                        continue
                    _apply_change(connection, change, environment.id)
                connection.execute(_INSERT_EVENT, _event_values(event))
                return event
        except psycopg.errors.UniqueViolation as exc:
            if (exc.diag.constraint_name or "") == IDEMPOTENCY_UNIQUE_CONSTRAINT:
                # Last line of defence: the winner's event is authoritative.
                duplicate = self.find_event(environment.id, event.idempotency_key)
                if duplicate is not None:
                    return duplicate
                raise IdempotencyKeyReused(
                    f"idempotency key {event.idempotency_key!r} was already used",
                    details={"idempotency_key": event.idempotency_key},
                ) from exc
            raise

    def save_snapshot(self, snapshot: EnvironmentSnapshot) -> None:
        with self._connect() as connection, connection.transaction():
            connection.execute(
                _INSERT_SNAPSHOT,
                (
                    snapshot.id,
                    snapshot.environment_id,
                    snapshot.version,
                    snapshot.last_event_sequence,
                    Jsonb(to_plain(snapshot.state)),
                    snapshot.state_hash,
                    snapshot.kind,
                    snapshot.label,
                    snapshot.created_at,
                ),
            )
