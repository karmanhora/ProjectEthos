"""Integration tests for the Postgres repository.

These write to a real database, so they run only when both
`ETHOS_TEST_DATABASE_URL` is set and `ETHOS_ALLOW_INTEGRATION_WRITES=1`. Both
default to unset, which means a plain `pytest` run skips every test here.

Each test uses its own environment id and removes its rows afterwards, so a
shared database can be used repeatedly without the tests disturbing each other.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from uuid import UUID

import psycopg
import pytest

from ethos.config import Settings
from ethos.errors import NotFoundError
from ethos.models import TaskStatus
from ethos.postgres_repository import PostgresRepository
from ethos.runtime import DeterministicIdFactory, FixedClock
from ethos.service import ActionRequest, EnvironmentService
from ethos.state import verify_event_chain

pytestmark = pytest.mark.integration

SETTINGS = Settings.from_env()

if not (
    SETTINGS.test_database_url
    and SETTINGS.admin_database_url
    and SETTINGS.allow_integration_writes
):
    pytest.skip(
        "integration tests need ETHOS_TEST_DATABASE_URL, "
        "ETHOS_ADMIN_DATABASE_URL and ETHOS_ALLOW_INTEGRATION_WRITES=1",
        allow_module_level=True,
    )

DSN = SETTINGS.test_database_url
ADMIN_DSN = SETTINGS.admin_database_url
NOW = datetime(2026, 11, 11, 12, 0, 0, tzinfo=timezone.utc)
ID_NAMESPACE = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")


@pytest.fixture()
def environment_id():
    return uuid.uuid4()


@pytest.fixture()
def service(environment_id):
    repository = PostgresRepository(DSN)
    return EnvironmentService(
        repository,
        environment_id,
        clock=FixedClock(NOW, timedelta(seconds=1)),
        ids=DeterministicIdFactory(ID_NAMESPACE),
    )


@pytest.fixture(autouse=True)
def cleanup(environment_id):
    yield
    # Administrative credential: the runtime role has no DELETE by design, so
    # teardown must never run as ethos_app.
    with psycopg.connect(ADMIN_DSN) as connection:
        for table in (
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
        ):
            connection.execute(
                f"delete from {table} where environment_id = %s", (environment_id,)
            )
            connection.commit()
        try:
            connection.execute(
                "delete from environments where id = %s", (environment_id,)
            )
            connection.commit()
        except psycopg.errors.InsufficientPrivilege:
            # ethos_app deliberately holds no DELETE on environments: taking an
            # environment out of service is a status change, not a deletion.
            connection.rollback()


def act(service, action_type, actor, parameters, key):
    return service.execute(
        ActionRequest(
            action_type=action_type,
            actor_user_id=actor,
            parameters=parameters,
            idempotency_key=key,
        )
    )


def test_unknown_environment_is_not_found():
    repository = PostgresRepository(DSN)
    with pytest.raises(NotFoundError):
        repository.load(uuid.uuid4())


def test_bootstrap_round_trips_through_postgres(service):
    result = service.bootstrap(
        name="demo",
        org_name="Acme",
        org_slug="acme",
        owner_handle="ada",
        owner_display_name="Ada Lovelace",
    )
    loaded = service.repository.load(service.environment_id)
    assert loaded.version == result.state.version
    assert loaded.users.keys() == result.state.users.keys()

    head = service.repository.head(service.environment_id)
    assert head.last_event_digest == result.event.event_digest


def test_actions_and_chain_verification_against_postgres(service):
    boot = service.bootstrap(
        name="demo",
        org_name="Acme",
        org_slug="acme",
        owner_handle="ada",
        owner_display_name="Ada Lovelace",
    )
    owner = boot.event.actor_user_id
    project = act(service, "project.create", owner, {"key": "ENG", "name": "Eng"}, "k1")
    project_id = next(iter(project.state.projects))
    task = act(
        service, "task.create", owner, {"project_id": str(project_id), "title": "T"}, "k2"
    )
    task_id = next(iter(task.state.tasks))
    done = act(
        service, "task.status.set", owner, {"task_id": str(task_id), "status": "done"}, "k3"
    )
    assert done.state.tasks[task_id].status is TaskStatus.DONE

    repository = service.repository
    head = repository.head(service.environment_id)
    report = verify_event_chain(
        repository.events(service.environment_id),
        head_sequence=head.last_event_sequence,
        head_digest=head.last_event_digest,
        current_state=repository.load(service.environment_id),
    )
    assert report.valid, report.divergence
    assert report.events_checked == 4

    # The entity tables are a projection of the same ChangeSets, so they must
    # agree with the log - including the completion constraint.
    with psycopg.connect(DSN) as connection:
        rows = connection.execute(
            "select status, completed_at is not null from tasks "
            "where environment_id = %s",
            (service.environment_id,),
        ).fetchall()
    assert rows == [("done", True)]


def test_duplicate_idempotency_key_is_rejected_by_the_database(service):
    boot = service.bootstrap(
        name="demo",
        org_name="Acme",
        org_slug="acme",
        owner_handle="ada",
        owner_display_name="Ada Lovelace",
    )
    owner = boot.event.actor_user_id
    act(service, "project.create", owner, {"key": "ENG", "name": "Eng"}, "same")
    # The service would normally replay here; a fresh service has no cache of the
    # key, so this exercises the unique constraint itself.
    fresh = EnvironmentService(
        PostgresRepository(DSN),
        service.environment_id,
        clock=FixedClock(NOW + timedelta(hours=1), timedelta(seconds=1)),
        ids=DeterministicIdFactory(ID_NAMESPACE, start=1000),
    )
    replayed = fresh.execute(
        ActionRequest(
            action_type="project.create",
            actor_user_id=owner,
            parameters={"key": "ENG", "name": "Eng"},
            idempotency_key="same",
        )
    )
    assert replayed.replayed is True


def test_app_role_cannot_delete_an_environment(service):
    service.bootstrap(
        name="demo",
        org_name="Acme",
        org_slug="acme",
        owner_handle="ada",
        owner_display_name="Ada Lovelace",
    )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with psycopg.connect(DSN) as connection:
            connection.execute(
                "delete from environments where id = %s", (service.environment_id,)
            )
