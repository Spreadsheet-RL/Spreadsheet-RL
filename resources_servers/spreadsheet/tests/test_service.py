from fastapi.testclient import TestClient

from resources_servers.spreadsheet.service.app import ServiceConfig, create_app
from resources_servers.spreadsheet.tests.test_state import state  # noqa: F401


def client_for(tmp_path, state):
    key = tmp_path / "key"
    key.write_text("x" * 40)
    config = ServiceConfig(
        manifest=tmp_path / "manifest.jsonl",
        data_root=tmp_path / "data",
        state_root=tmp_path / "api-state",
        api_key_file=key,
    )
    return TestClient(create_app(config, store=state))


def test_service_auth_validation_idempotency_and_body_limit(tmp_path, state):
    # No lifespan here: state machinery uses deterministic test engines, not a
    # claim that Linux runtime readiness passed on the Mac.
    client = client_for(tmp_path, state)
    assert client.post("/sessions", json={"rollout_id": "r", "task_id": "task-A"}).status_code == 401
    client.headers["Authorization"] = "Bearer " + "x" * 40
    response = client.post("/sessions", json={"rollout_id": "r", "task_id": "task-A"})
    assert response.status_code == 200
    sid = response.json()["session_id"]
    assert client.post("/sessions", json={"rollout_id": "r", "task_id": "task-A"}).json()["session_id"] == sid
    assert client.post("/sessions", json={"rollout_id": "x", "task_id": "../oracle"}).status_code == 400
    oversized = client.post(f"/sessions/{sid}/operations", content=b"x" * (200 * 1024))
    assert oversized.status_code == 413
    bad = client.post(f"/sessions/{sid}/operations", json={"operation_id": "x", "action": "../verify"})
    assert bad.status_code == 400


def test_unknown_session_is_not_created_by_operation(tmp_path, state):
    client = client_for(tmp_path, state)
    client.headers["Authorization"] = "Bearer " + "x" * 40
    response = client.post(
        "/sessions/" + "f" * 32 + "/operations",
        json={
            "operation_id": "x",
            "action": "list_sheets",
            "arguments": {},
        },
    )
    assert response.status_code == 409
    assert not (state.root / ("f" * 32)).exists()
