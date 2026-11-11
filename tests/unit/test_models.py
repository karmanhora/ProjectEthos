from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from ethos.models import (
    ChangeOp,
    ChangeSet,
    EntityChange,
    EntityType,
    Environment,
    EnvironmentRules,
    EnvironmentState,
    EnvironmentUser,
    OrgRole,
    Organization,
    Task,
    TaskStatus,
    digest_of,
)
from ethos.runtime import FrozenMapping, to_plain

NOW = datetime(2026, 11, 11, 12, 0, 0, tzinfo=timezone.utc)
ENV_ID = UUID("11111111-1111-4111-8111-111111111111")
ORG_ID = UUID("22222222-2222-4222-8222-222222222222")
TASK_ID = UUID("33333333-3333-4333-8333-333333333333")
PROJECT_ID = UUID("44444444-4444-4444-8444-444444444444")
USER_ID = UUID("55555555-5555-4555-8555-555555555555")


class Row(str):
    """Sentinel type is unnecessary; Any fields accept UUIDs."""


def make_environment(version: int = 1) -> Environment:
    return Environment(
        id=ENV_ID,
        name="demo",
        config=EnvironmentRules(),
        version=version,
        last_event_sequence=1,
        last_event_digest="a" * 64,
        created_at=NOW,
        updated_at=NOW,
    )


def make_state(**overrides) -> EnvironmentState:
    base = {"environment": make_environment()}
    base.update(overrides)
    return EnvironmentState(**base)


def test_rules_defaults_are_the_phase_one_ruleset():
    rules = EnvironmentRules()
    assert rules.max_assignees_per_task == 1
    assert rules.prevent_dependency_cycles is True
    assert rules.require_assignee_project_member is True
    assert rules.block_completion_with_open_dependencies is True
    assert rules.snapshot_interval == 25
    assert rules.max_tasks_per_project is None


def test_entity_collections_are_frozen_mappings():
    org = Organization(
        id=ORG_ID, environment_id=ENV_ID, name="Acme", slug="acme", created_at=NOW
    )
    state = make_state(organizations={ORG_ID: org})
    assert isinstance(state.organizations, FrozenMapping)
    with pytest.raises((TypeError, AttributeError)):
        state.organizations[ORG_ID] = org


def test_environment_version_is_exposed_on_state():
    state = make_state()
    assert state.version == 1
    assert state.environment_id == ENV_ID


def test_task_status_enum_round_trip():
    task = Task(
        id=TASK_ID,
        environment_id=ENV_ID,
        project_id=PROJECT_ID,
        title="write tests",
        status=TaskStatus.IN_PROGRESS,
        created_by_id=USER_ID,
        created_at=NOW,
        updated_at=NOW,
    )
    assert to_plain(task)["status"] == "in_progress"


def test_change_set_summary_counts_by_type_and_op():
    changes = ChangeSet(
        changes=[
            EntityChange(
                op=ChangeOp.CREATE,
                entity_type=EntityType.TASK,
                entity_id=TASK_ID,
                after={"id": str(TASK_ID)},
            ),
            EntityChange(
                op=ChangeOp.UPDATE,
                entity_type=EntityType.TASK,
                entity_id=TASK_ID,
                fields=[]
            ),
        ]
    )
    assert changes.summary() == {"task.create": 1, "task.update": 1}
    assert changes.changed_entity_ids() == {"task": [str(TASK_ID), str(TASK_ID)]}


def test_digest_is_stable_and_covers_metadata():
    base = {
        "id": TASK_ID,
        "environment_id": ENV_ID,
        "sequence": 1,
        "action_type": "task.create",
        "actor_user_id": USER_ID,
        "origin": "api",
        "parameters": {"title": "x"},
        "changes": ChangeSet(),
        "status": "succeeded",
        "failure_code": None,
        "failure_reason": None,
        "failure_details": None,
        "prev_state_version": 0,
        "prev_state_hash": "a" * 64,
        "resulting_state_version": 1,
        "resulting_state_hash": "b" * 64,
        "idempotency_key": "key-00000001",
        "request_fingerprint": "c" * 64,
        "created_at": NOW,
        "prev_event_digest": "d" * 64,
    }
    first = digest_of(base)
    assert first == digest_of(dict(base))
    assert len(first) == 64

    changed_actor = dict(base, actor_user_id=None)
    assert digest_of(changed_actor) != first

    changed_params = dict(base, parameters={"title": "y"})
    assert digest_of(changed_params) != first


def test_organization_rejects_empty_name():
    with pytest.raises(ValidationError):
        Organization(
            id=ORG_ID, environment_id=ENV_ID, name="", slug="acme", created_at=NOW
        )


def test_user_has_no_authentication_fields():
    fields = set(EnvironmentUser.model_fields)
    assert "email" not in fields
    assert "password" not in fields
    assert "encrypted_password" not in fields


def test_role_defaults():
    user = EnvironmentUser(
        id=USER_ID,
        environment_id=ENV_ID,
        org_id=ORG_ID,
        handle="ada_l",
        display_name="Ada Lovelace",
        created_at=NOW,
    )
    assert user.org_role is OrgRole.MEMBER
    assert user.is_synthetic is True
