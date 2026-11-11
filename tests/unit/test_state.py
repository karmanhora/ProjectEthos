from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from ethos.models import (
    ChangeOp,
    ChangeSet,
    EntityChange,
    EntityType,
    Environment,
    EnvironmentEvent,
    EnvironmentRules,
    EnvironmentState,
    Task,
    TaskStatus,
    digest_of,
)
from ethos.runtime import GENESIS_EVENT_DIGEST, GENESIS_STATE_HASH, to_plain
from ethos.state import (
    apply_changes,
    diff,
    replay,
    state_hash,
    verify_event_chain,
)

NOW = datetime(2026, 11, 11, 12, 0, 0, tzinfo=timezone.utc)
ENV_ID = UUID("11111111-1111-4111-8111-111111111111")
PROJECT_ID = UUID("44444444-4444-4444-8444-444444444444")
USER_ID = UUID("55555555-5555-4555-8555-555555555555")
TASK_ID = UUID("33333333-3333-4333-8333-333333333333")
TASK_ID_2 = UUID("66666666-6666-4666-8666-666666666666")


def make_environment(version: int = 1) -> Environment:
    return Environment(
        id=ENV_ID,
        name="demo",
        config=EnvironmentRules(),
        version=version,
        created_at=NOW,
        updated_at=NOW,
    )


def make_task(task_id: UUID = TASK_ID, status: TaskStatus = TaskStatus.TODO) -> Task:
    return Task(
        id=task_id,
        environment_id=ENV_ID,
        project_id=PROJECT_ID,
        title="write tests",
        status=status,
        created_by_id=USER_ID,
        created_at=NOW,
        updated_at=NOW,
    )


def make_state(*, version: int = 1, **kwargs) -> EnvironmentState:
    return EnvironmentState(
        environment=make_environment(version=version),
        **kwargs,
    )


def bootstrap_changes(environment: Environment) -> ChangeSet:
    return ChangeSet(
        changes=[
            EntityChange(
                op=ChangeOp.CREATE,
                entity_type=EntityType.ENVIRONMENT,
                entity_id=environment.id,
                after=to_plain(environment),
            )
        ]
    )


def make_event(
    *,
    sequence: int,
    prev_digest: str,
    prev_version: int,
    prev_hash: str,
    changes: ChangeSet,
    status: str = "succeeded",
    resulting_version: int | None = None,
    resulting_hash: str | None = None,
) -> EnvironmentEvent:
    fields = {
        "id": uuid4(),
        "environment_id": ENV_ID,
        "sequence": sequence,
        "action_type": "environment.bootstrap" if sequence == 1 else "task.create",
        "actor_user_id": None,
        "origin": "bootstrap" if sequence == 1 else "api",
        "parameters": {},
        "changes": changes,
        "status": status,
        "failure_code": None if status == "succeeded" else "RULE_VIOLATION",
        "failure_reason": None if status == "succeeded" else "refused",
        "failure_details": None,
        "prev_state_version": prev_version,
        "prev_state_hash": prev_hash,
        "resulting_state_version": resulting_version,
        "resulting_state_hash": resulting_hash,
        "idempotency_key": f"key-{sequence:08d}",
        "request_fingerprint": "b" * 64,
        "prev_event_digest": prev_digest,
        "created_at": NOW,
    }
    fields["event_digest"] = digest_of(fields)
    return EnvironmentEvent(**fields)


# --- hashing ----------------------------------------------------------------


def test_state_hash_is_stable_and_order_independent():
    a = make_state(tasks={TASK_ID: make_task(TASK_ID), TASK_ID_2: make_task(TASK_ID_2)})
    b = make_state(tasks={TASK_ID_2: make_task(TASK_ID_2), TASK_ID: make_task(TASK_ID)})
    assert state_hash(a) == state_hash(b)
    assert len(state_hash(a)) == 64


def test_state_hash_changes_when_content_changes():
    a = make_state()
    b = make_state(tasks={TASK_ID: make_task()})
    assert state_hash(a) != state_hash(b)


# --- diff / apply -----------------------------------------------------------


def test_diff_detects_create():
    before = make_state()
    after = make_state(tasks={TASK_ID: make_task()})
    changes = diff(before, after)
    assert len(changes.changes) == 1
    change = changes.changes[0]
    assert change.op is ChangeOp.CREATE
    assert change.entity_type is EntityType.TASK
    assert change.after is not None


def test_diff_detects_update_with_exact_fields():
    before = make_state(tasks={TASK_ID: make_task()})
    after = make_state(tasks={TASK_ID: make_task(status=TaskStatus.DONE)})
    changes = diff(before, after)
    assert len(changes.changes) == 1
    change = changes.changes[0]
    assert change.op is ChangeOp.UPDATE
    fields = {f.field for f in change.fields}
    assert "status" in fields
    assert "title" not in fields


def test_diff_detects_delete():
    before = make_state(tasks={TASK_ID: make_task()})
    after = make_state()
    changes = diff(before, after)
    assert changes.changes[0].op is ChangeOp.DELETE
    assert changes.changes[0].before is not None


def test_apply_reproduces_target_state():
    before = make_state()
    after = make_state(tasks={TASK_ID: make_task()})
    applied = apply_changes(before, diff(before, after))
    assert state_hash(applied) == state_hash(after)


def test_apply_update_reproduces_target_state():
    before = make_state(tasks={TASK_ID: make_task()})
    after = make_state(tasks={TASK_ID: make_task(status=TaskStatus.DONE)})
    applied = apply_changes(before, diff(before, after))
    assert state_hash(applied) == state_hash(after)


def test_diff_of_applied_changes_is_empty():
    before = make_state()
    after = make_state(tasks={TASK_ID: make_task()})
    applied = apply_changes(before, diff(before, after))
    assert len(diff(applied, after).changes) == 0


def test_apply_bootstrap_from_nothing():
    environment = make_environment()
    state = apply_changes(None, bootstrap_changes(environment), version=1)
    assert state.version == 1
    assert state.environment.name == "demo"


def test_apply_rejects_delete_of_missing_entity():
    changes = ChangeSet(
        changes=[
            EntityChange(
                op=ChangeOp.DELETE,
                entity_type=EntityType.TASK,
                entity_id=TASK_ID,
                before={},
            )
        ]
    )
    with pytest.raises(ValueError):
        apply_changes(make_state(), changes)


# --- replay / verification --------------------------------------------------


def build_chain():
    env1 = make_environment(version=1)
    state1 = EnvironmentState(environment=env1)
    e1 = make_event(
        sequence=1,
        prev_digest=GENESIS_EVENT_DIGEST,
        prev_version=0,
        prev_hash=GENESIS_STATE_HASH,
        changes=bootstrap_changes(env1),
        resulting_version=1,
        resulting_hash=state_hash(state1),
    )

    state2 = EnvironmentState(
        environment=make_environment(version=2),
        tasks={TASK_ID: make_task()},
    )
    e2 = make_event(
        sequence=2,
        prev_digest=e1.event_digest,
        prev_version=1,
        prev_hash=state_hash(state1),
        changes=diff(state1, state2),
        resulting_version=2,
        resulting_hash=state_hash(state2),
    )
    return state1, state2, e1, e2


def test_replay_reproduces_state():
    state1, state2, e1, e2 = build_chain()
    assert state_hash(replay([e1, e2])) == state_hash(state2)


def test_full_chain_verifies():
    _, state2, e1, e2 = build_chain()
    report = verify_event_chain(
        [e1, e2],
        head_sequence=2,
        head_digest=e2.event_digest,
        current_state=state2,
    )
    assert report.valid, report.divergence
    assert report.final_version == 2
    assert report.final_hash == state_hash(state2)


def test_trailing_failed_event_deletion_is_detected():
    _, state2, e1, e2 = build_chain()
    failed = make_event(
        sequence=3,
        prev_digest=e2.event_digest,
        prev_version=2,
        prev_hash=state_hash(state2),
        changes=ChangeSet(),
        status="failed",
    )
    head_sequence = 3
    head_digest = failed.event_digest

    report = verify_event_chain(
        [e1, e2, failed],
        head_sequence=head_sequence,
        head_digest=head_digest,
        current_state=state2,
    )
    assert report.valid

    truncated = verify_event_chain(
        [e1, e2],
        head_sequence=head_sequence,
        head_digest=head_digest,
        current_state=state2,
    )
    assert not truncated.valid
    assert truncated.divergence.step == "head"
    assert truncated.divergence.code == "TRAILING_EVENTS_DELETED"


def test_tampered_event_digest_is_detected():
    _, state2, e1, e2 = build_chain()
    payload = to_plain(e2)
    payload["event_digest"] = "0" * 64
    tampered = EnvironmentEvent(**payload)

    report = verify_event_chain(
        [e1, tampered],
        head_sequence=2,
        head_digest=tampered.event_digest,
        current_state=state2,
    )
    assert not report.valid
    assert report.divergence.step == "digest"
    assert report.divergence.code == "EVENT_DIGEST_MISMATCH"


def test_failed_event_must_not_advance_state():
    state1, state2, e1, e2 = build_chain()
    bogus = make_event(
        sequence=3,
        prev_digest=e2.event_digest,
        prev_version=2,
        prev_hash=state_hash(state2),
        changes=diff(state2, state1),
        status="failed",
    )
    report = verify_event_chain(
        [e1, e2, bogus],
        head_sequence=3,
        head_digest=bogus.event_digest,
        current_state=state2,
    )
    assert not report.valid
    assert report.divergence.code == "FAILED_EVENT_MUTATED_STATE"
