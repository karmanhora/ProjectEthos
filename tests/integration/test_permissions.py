"""Proves the runtime role's restrictions against the database itself.

Reading the grants is not proof: a table-wide grant inherited from an earlier
migration would still be there. Each test attempts the operation as `ethos_app`
and requires the database to refuse it.

Setup and teardown use the administrative credential, because the runtime role
cannot delete rows - which is one of the things being tested.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from uuid import UUID

import psycopg
import pytest

from ethos.config import Settings
from ethos.postgres_repository import PostgresRepository
from ethos.runtime import DeterministicIdFactory, FixedClock
from ethos.service import ActionRequest, EnvironmentService

pytestmark = pytest.mark.integration

SETTINGS = Settings.from_env()

if not (
    SETTINGS.test_database_url
    and SETTINGS.admin_database_url
    and SETTINGS.allow_integration_writes
):
    pytest.skip(
        "permission tests need ETHOS_TEST_DATABASE_URL, "
        "ETHOS_ADMIN_DATABASE_URL and ETHOS_ALLOW_INTEGRATION_WRITES=1",
        allow_module_level=True,
    )

RUNTIME_DSN = SETTINGS.test_database_url
ADMIN_DSN = SETTINGS.admin_database_url
NOW = datetime(2026, 11, 11, 12, 0, 0, tzinfo=timezone.utc)
ID_NAMESPACE = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")

CHILD_TABLES = (
    "environment_snapshots",
    "environment_events",
    "task_dependencies",
    "task_assignments",
    "tasks",
    "project_memberships",
    "projects",
    "team_memberships",
    "teams",
    "environment_users",
    "organizations",
)


@pytest.fixture()
def environment():
    environment_id = uuid.uuid4()
    service = EnvironmentService(
        PostgresRepository(RUNTIME_DSN),
        environment_id,
        clock=FixedClock(NOW, timedelta(seconds=1)),
        ids=DeterministicIdFactory(ID_NAMESPACE),
    )
    boot = service.bootstrap(
        name="permissions",
        org_name="Acme",
        org_slug="acme",
        owner_handle="ada",
        owner_display_name="Ada Lovelace",
    )
    yield service, environment_id, boot
    with psycopg.connect(ADMIN_DSN) as connection:
        for table in CHILD_TABLES:
            connection.execute(
                f"delete from {table} where environment_id = %s", (environment_id,)
            )
        connection.execute(
            "delete from environments where id = %s", (environment_id,)
        )
        connection.commit()


def denied(sql: str, params: tuple = ()) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with psycopg.connect(RUNTIME_DSN) as connection:
            connection.execute(sql, params)


def _owner(service) -> UUID:
    return next(iter(service.repository.load(service.environment_id).users))


def _task(service) -> UUID:
    owner = _owner(service)
    project = service.execute(
        ActionRequest(
            action_type="project.create",
            actor_user_id=owner,
            parameters={"key": "ENG", "name": "Eng"},
            idempotency_key="perm-project",
        )
    )
    project_id = next(iter(project.state.projects))
    task = service.execute(
        ActionRequest(
            action_type="task.create",
            actor_user_id=owner,
            parameters={"project_id": str(project_id), "title": "T"},
            idempotency_key="perm-task",
        )
    )
    return next(iter(task.state.tasks))


# --- append-only tables -----------------------------------------------------


def test_runtime_cannot_update_events(environment):
    _, environment_id, _ = environment
    denied(
        "update environment_events set action_type = 'x' where environment_id = %s",
        (environment_id,),
    )


def test_runtime_cannot_delete_events(environment):
    _, environment_id, _ = environment
    denied(
        "delete from environment_events where environment_id = %s", (environment_id,)
    )


def test_runtime_cannot_update_snapshots(environment):
    _, environment_id, _ = environment
    denied(
        "update environment_snapshots set label = 'x' where environment_id = %s",
        (environment_id,),
    )


def test_runtime_cannot_delete_snapshots(environment):
    _, environment_id, _ = environment
    denied(
        "delete from environment_snapshots where environment_id = %s",
        (environment_id,),
    )


# --- no deletes at all ------------------------------------------------------


def test_runtime_cannot_delete_an_environment(environment):
    _, environment_id, _ = environment
    denied("delete from environments where id = %s", (environment_id,))


def test_runtime_cannot_delete_tasks(environment):
    _, environment_id, _ = environment
    denied("delete from tasks where environment_id = %s", (environment_id,))


# --- column-level updates ---------------------------------------------------


def test_runtime_cannot_backdate_a_task(environment):
    service, _, _ = environment
    task_id = _task(service)
    # created_at is not in the task UPDATE grant list.
    denied("update tasks set created_at = now() where id = %s", (task_id,))


def test_runtime_can_update_a_granted_column(environment):
    service, _, _ = environment
    task_id = _task(service)
    with psycopg.connect(RUNTIME_DSN) as connection:
        cursor = connection.execute(
            "update tasks set status = 'in_progress' where id = %s", (task_id,)
        )
        assert cursor.rowcount == 1
        connection.commit()
