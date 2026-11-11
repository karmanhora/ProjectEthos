from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from ethos.api.app import create_app
from ethos.config import Settings

BOOTSTRAP = {
    "name": "demo",
    "org_name": "Acme",
    "org_slug": "acme",
    "owner_handle": "ada",
    "owner_display_name": "Ada Lovelace",
}


@pytest.fixture()
def client():
    return TestClient(create_app(Settings(env="test")))


def boot(client) -> dict:
    response = client.post("/v1/environments", json=BOOTSTRAP)
    assert response.status_code == 201, response.text
    return response.json()


def act(client, environment_id, actor, action_type, parameters, key, **extra):
    return client.post(
        f"/v1/environments/{environment_id}/actions",
        json={
            "action_type": action_type,
            "actor_user_id": actor,
            "parameters": parameters,
            "idempotency_key": key,
            **extra,
        },
    )


def test_health_names_the_repository(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["repository"] == "InMemoryRepository"


def test_bootstrap_returns_the_environment_and_owner(client):
    body = boot(client)
    assert body["version"] == 1
    assert body["last_event_sequence"] == 1
    assert len(body["last_event_digest"]) == 64
    assert body["owner_user_id"]


def test_action_advances_the_version(client):
    body = boot(client)
    response = act(
        client,
        body["environment_id"],
        body["owner_user_id"],
        "project.create",
        {"key": "ENG", "name": "Engineering"},
        "k-project",
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["version"] == 2
    assert payload["event"]["sequence"] == 2
    assert payload["replayed"] is False
    assert len(payload["changed"]["project"]) == 1


def test_permission_denied_is_403_with_the_recorded_code(client):
    body = boot(client)
    created = act(
        client,
        body["environment_id"],
        body["owner_user_id"],
        "user.create",
        {"handle": "vic", "display_name": "Vic", "org_role": "viewer"},
        "k-user",
    )
    assert created.status_code == 200, created.text
    viewer = created.json()["changed"]["environment_user"][0]

    response = act(
        client,
        body["environment_id"],
        viewer,
        "project.create",
        {"key": "OPS", "name": "Ops"},
        "k-denied",
    )
    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "PERMISSION_DENIED"
    assert error["details"]["capability"] == "project.manage"


def test_unknown_action_is_422(client):
    body = boot(client)
    response = act(
        client, body["environment_id"], body["owner_user_id"], "nope.nope", {}, "k-x"
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_invalid_parameters_are_422(client):
    body = boot(client)
    response = act(
        client, body["environment_id"], body["owner_user_id"], "project.create", {}, "k-y"
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_version_conflict_is_409(client):
    body = boot(client)
    response = act(
        client,
        body["environment_id"],
        body["owner_user_id"],
        "project.create",
        {"key": "ENG", "name": "Eng"},
        "k-version",
        expected_version=99,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "VERSION_CONFLICT"


def test_unknown_environment_is_404(client):
    response = client.get(f"/v1/environments/{uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_events_and_integrity_agree(client):
    body = boot(client)
    act(
        client,
        body["environment_id"],
        body["owner_user_id"],
        "project.create",
        {"key": "ENG", "name": "Eng"},
        "k-project",
    )
    events = client.get(f"/v1/environments/{body['environment_id']}/events").json()
    assert [event["sequence"] for event in events] == [1, 2]

    report = client.get(
        f"/v1/environments/{body['environment_id']}/integrity"
    ).json()
    assert report["valid"] is True, report["divergence"]
    assert report["events_checked"] == 2
    assert report["final_version"] == 2


def test_idempotent_retry_returns_the_original_event(client):
    body = boot(client)
    first = act(
        client,
        body["environment_id"],
        body["owner_user_id"],
        "project.create",
        {"key": "ENG", "name": "Eng"},
        "k-same",
    ).json()
    second = act(
        client,
        body["environment_id"],
        body["owner_user_id"],
        "project.create",
        {"key": "ENG", "name": "Eng"},
        "k-same",
    ).json()
    assert second["replayed"] is True
    assert second["event"]["id"] == first["event"]["id"]
    assert second["version"] == first["version"]


def test_root_lists_the_api(client):
    response = client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["docs"] == "/docs"
    assert body["endpoints"]["run_action"].endswith("/actions")
