from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from ethos.errors import (
    IdempotencyKeyReused,
    RuleViolation,
    ValidationError,
    VersionConflict,
)
from ethos.models import TaskStatus
from ethos.repository import InMemoryRepository
from ethos.runtime import (
    GENESIS_EVENT_DIGEST,
    DeterministicIdFactory,
    FixedClock,
)
from ethos.service import ActionRequest, EnvironmentService
from ethos.state import verify_event_chain

NOW = datetime(2026, 11, 11, 12, 0, 0, tzinfo=timezone.utc)
ENV_ID = UUID("11111111-1111-4111-8111-111111111111")
ID_NAMESPACE = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")


def build():
    repository = InMemoryRepository()
    service = EnvironmentService(
        repository,
        ENV_ID,
        clock=FixedClock(NOW, timedelta(seconds=1)),
        ids=DeterministicIdFactory(ID_NAMESPACE),
    )
    return service, repository


def bootstrapped():
    service, repository = build()
    result = service.bootstrap(
        name="demo",
        org_name="Acme",
        org_slug="acme",
        owner_handle="ada",
        owner_display_name="Ada Lovelace",
    )
    return service, repository, result


def act(service, action_type, actor, parameters, key, *, expected_version=None):
    return service.execute(
        ActionRequest(
            action_type=action_type,
            actor_user_id=actor,
            parameters=parameters,
            idempotency_key=key,
            expected_version=expected_version,
        )
    )


def assert_chain(service, repository):
    head = repository.head(ENV_ID)
    report = verify_event_chain(
        repository.events(ENV_ID),
        head_sequence=head.last_event_sequence,
        head_digest=head.last_event_digest,
        current_state=repository.load(ENV_ID),
    )
    assert report.valid, report.divergence
    return report


def create_project(service, owner, key="ENG", name="Engineering"):
    before = set(service.repository.load(ENV_ID).projects)
    result = act(
        service,
        "project.create",
        owner,
        {"key": key, "name": name},
        f"k-project-{key}",
    )
    return next(iter(set(result.state.projects) - before))


def create_user(service, owner, handle, role="member"):
    result = act(
        service,
        "user.create",
        owner,
        {"handle": handle, "display_name": handle.title(), "org_role": role},
        f"k-user-{handle}",
    )
    for user in result.state.users.values():
        if user.handle == handle:
            return user.id
    raise AssertionError(f"user {handle} was not created")


def create_task(service, owner, project_id, title):
    before = set(service.repository.load(ENV_ID).tasks)
    result = act(
        service,
        "task.create",
        owner,
        {"project_id": str(project_id), "title": title},
        f"k-task-{title}",
    )
    return next(iter(set(result.state.tasks) - before))


# --- bootstrap --------------------------------------------------------------


def test_bootstrap_records_genesis_event():
    _, repository, result = bootstrapped()
    assert result.event.sequence == 1
    assert result.event.origin == "bootstrap"
    assert result.event.prev_event_digest == GENESIS_EVENT_DIGEST
    assert result.state.version == 1
    assert len(result.state.organizations) == 1
    assert len(result.state.users) == 1
    head = repository.head(ENV_ID)
    assert (head.version, head.last_event_sequence) == (1, 1)
    assert head.last_event_digest == result.event.event_digest


def test_bootstrap_chain_verifies():
    service, repository, _ = bootstrapped()
    report = assert_chain(service, repository)
    assert report.events_checked == 1


# --- happy path -------------------------------------------------------------


def test_task_lifecycle_keeps_the_chain_valid():
    service, repository, boot = bootstrapped()
    owner = boot.event.actor_user_id
    project_id = create_project(service, owner)
    task_id = create_task(service, owner, project_id, "Ship it")

    act(
        service,
        "task.assign",
        owner,
        {"task_id": str(task_id), "user_id": str(owner)},
        "k-assign",
    )
    done = act(
        service,
        "task.status.set",
        owner,
        {"task_id": str(task_id), "status": "done"},
        "k-done",
    )

    assert done.state.tasks[task_id].status is TaskStatus.DONE
    assert done.state.tasks[task_id].completed_at == done.event.created_at
    assert done.state.version == 5
    report = assert_chain(service, repository)
    assert report.events_checked == 5


# --- refusals ---------------------------------------------------------------


def test_permission_denied_is_recorded_without_advancing_state():
    service, repository, boot = bootstrapped()
    owner = boot.event.actor_user_id
    viewer = create_user(service, owner, "vic", role="viewer")

    before = service.repository.load(ENV_ID)
    with pytest.raises(RuleViolation) as excinfo:
        act(service, "project.create", viewer, {"key": "OPS", "name": "Ops"}, "k-denied")

    assert excinfo.value.http_status == 403
    after = service.repository.load(ENV_ID)
    assert after.version == before.version
    events = repository.events(ENV_ID)
    assert events[-1].status == "failed"
    assert events[-1].failure_code == "PERMISSION_DENIED"
    assert events[-1].resulting_state_version is None
    assert_chain(service, repository)


def test_unknown_actor_is_recorded_as_a_failed_event():
    service, repository, _ = bootstrapped()
    with pytest.raises(RuleViolation) as excinfo:
        act(
            service,
            "project.create",
            uuid4(),
            {"key": "OPS", "name": "Ops"},
            "k-ghost",
        )
    assert excinfo.value.http_status == 404
    assert repository.events(ENV_ID)[-1].failure_code == "ACTOR_NOT_FOUND"
    assert service.repository.load(ENV_ID).version == 1


def test_dependency_cycle_is_rejected():
    service, repository, boot = bootstrapped()
    owner = boot.event.actor_user_id
    project_id = create_project(service, owner)
    first = create_task(service, owner, project_id, "first")
    second = create_task(service, owner, project_id, "second")

    act(
        service,
        "task.dependency.add",
        owner,
        {"task_id": str(first), "depends_on_task_id": str(second)},
        "k-dep-1",
    )
    version = service.repository.load(ENV_ID).version
    with pytest.raises(RuleViolation):
        act(
            service,
            "task.dependency.add",
            owner,
            {"task_id": str(second), "depends_on_task_id": str(first)},
            "k-dep-2",
        )
    assert service.repository.load(ENV_ID).version == version
    assert_chain(service, repository)


def test_completion_blocked_by_open_dependency():
    service, repository, boot = bootstrapped()
    owner = boot.event.actor_user_id
    project_id = create_project(service, owner)
    first = create_task(service, owner, project_id, "first")
    second = create_task(service, owner, project_id, "second")
    act(
        service,
        "task.dependency.add",
        owner,
        {"task_id": str(first), "depends_on_task_id": str(second)},
        "k-block-dep",
    )

    with pytest.raises(RuleViolation) as excinfo:
        act(
            service,
            "task.status.set",
            owner,
            {"task_id": str(first), "status": "done"},
            "k-blocked",
        )
    assert excinfo.value.details["blocking_task_id"] == str(second)

    act(
        service,
        "task.status.set",
        owner,
        {"task_id": str(second), "status": "done"},
        "k-unblock",
    )
    finished = act(
        service,
        "task.status.set",
        owner,
        {"task_id": str(first), "status": "done"},
        "k-finish",
    )
    assert finished.state.tasks[first].status is TaskStatus.DONE
    assert_chain(service, repository)


def test_max_assignees_is_enforced():
    service, repository, boot = bootstrapped()
    owner = boot.event.actor_user_id
    project_id = create_project(service, owner)
    bob = create_user(service, owner, "bob")
    act(
        service,
        "project.member.add",
        owner,
        {"project_id": str(project_id), "user_id": str(bob)},
        "k-bob-member",
    )
    task_id = create_task(service, owner, project_id, "shared")
    act(
        service,
        "task.assign",
        owner,
        {"task_id": str(task_id), "user_id": str(owner)},
        "k-owner-assign",
    )

    with pytest.raises(RuleViolation) as excinfo:
        act(
            service,
            "task.assign",
            owner,
            {"task_id": str(task_id), "user_id": str(bob)},
            "k-bob-assign",
        )
    assert excinfo.value.details["max_assignees_per_task"] == 1
    assert_chain(service, repository)


def test_assignee_must_be_a_project_member():
    service, repository, boot = bootstrapped()
    owner = boot.event.actor_user_id
    project_id = create_project(service, owner)
    bob = create_user(service, owner, "bob")
    task_id = create_task(service, owner, project_id, "outsider")

    with pytest.raises(RuleViolation) as excinfo:
        act(
            service,
            "task.assign",
            owner,
            {"task_id": str(task_id), "user_id": str(bob)},
            "k-outsider",
        )
    assert excinfo.value.details["user_id"] == str(bob)
    assert_chain(service, repository)


# --- request handling -------------------------------------------------------


def test_idempotent_replay_returns_the_same_event():
    service, repository, boot = bootstrapped()
    owner = boot.event.actor_user_id
    first = act(
        service, "project.create", owner, {"key": "ENG", "name": "Eng"}, "same-key"
    )
    second = act(
        service, "project.create", owner, {"key": "ENG", "name": "Eng"}, "same-key"
    )
    assert second.replayed is True
    assert second.event.id == first.event.id
    assert repository.head(ENV_ID).last_event_sequence == 2
    assert_chain(service, repository)


def test_idempotency_key_cannot_be_reused_for_a_different_request():
    service, _, boot = bootstrapped()
    owner = boot.event.actor_user_id
    act(service, "project.create", owner, {"key": "ENG", "name": "Eng"}, "same-key")
    with pytest.raises(IdempotencyKeyReused):
        act(
            service,
            "project.create",
            owner,
            {"key": "OPS", "name": "Ops"},
            "same-key",
        )


def test_expected_version_conflict_is_rejected():
    service, _, boot = bootstrapped()
    owner = boot.event.actor_user_id
    with pytest.raises(VersionConflict):
        act(
            service,
            "project.create",
            owner,
            {"key": "ENG", "name": "Eng"},
            "k-version",
            expected_version=99,
        )


def test_unknown_action_is_rejected():
    service, _, boot = bootstrapped()
    with pytest.raises(ValidationError):
        act(service, "nope.nope", boot.event.actor_user_id, {}, "k-unknown")


def test_invalid_parameters_are_rejected():
    service, _, boot = bootstrapped()
    with pytest.raises(ValidationError):
        act(service, "project.create", boot.event.actor_user_id, {}, "k-invalid")
