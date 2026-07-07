from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import mini_vps.api as api_module
from mini_vps.manager import (
    ServerConflict,
    ServerNotFound,
    ServerNotRunning,
    ServerRunning,
)
from mini_vps.startup_scripts import StartupScriptError


@pytest.fixture
def client(monkeypatch):
    """ServerManagerをMockに差し替えたTestClientを返す。

    FastAPI標準の TestClient + dependency_overrides の定型パターンなので、
    他ファイルと異なり fixture として共通化する。lifespan が libvirt.open を
    呼んでも実接続しないよう合わせて patch する。
    """
    mock_manager = MagicMock()
    monkeypatch.setattr("mini_vps.api.libvirt.open", lambda uri: MagicMock())
    api_module.app.dependency_overrides[api_module.get_manager] = lambda: mock_manager
    with TestClient(api_module.app) as test_client:
        yield test_client, mock_manager
    api_module.app.dependency_overrides.clear()


PUT_BODY = {
    "memory": 1024,
    "vcpus": 2,
    "base_image": "ubuntu-24.04.img",
    "disk": 10,
}


# --- list_servers ---


def test_list_servers_returns_manager_list(client):
    test_client, mock_manager = client
    mock_manager.list.return_value = ["web-1", "web-2"]

    response = test_client.get("/servers")

    assert response.status_code == 200
    assert response.json() == {"servers": ["web-1", "web-2"]}


# --- get_server ---


def test_get_server_returns_404_when_not_found(client):
    test_client, mock_manager = client
    mock_manager.get.side_effect = ServerNotFound("web-1")

    response = test_client.get("/servers/web-1")

    assert response.status_code == 404


# --- put_server ---


def test_put_server_returns_201_when_created(client):
    test_client, mock_manager = client
    mock_manager.create.return_value = ({"spec": {}, "status": {}}, True)

    response = test_client.put("/servers/web-1", json=PUT_BODY)

    assert response.status_code == 201


def test_put_server_returns_200_when_idempotent(client):
    test_client, mock_manager = client
    mock_manager.create.return_value = ({"spec": {}, "status": {}}, False)

    response = test_client.put("/servers/web-1", json=PUT_BODY)

    assert response.status_code == 200


def test_put_server_returns_409_on_conflict(client):
    test_client, mock_manager = client
    mock_manager.create.side_effect = ServerConflict("web-1")

    response = test_client.put("/servers/web-1", json=PUT_BODY)

    assert response.status_code == 409


def test_put_server_returns_409_when_running(client):
    """memory/vcpus/filters の差分収束は稼働中の VM には適用できない。"""
    test_client, mock_manager = client
    mock_manager.create.side_effect = ServerRunning("web-1")

    response = test_client.put("/servers/web-1", json=PUT_BODY)

    assert response.status_code == 409


def test_put_server_separates_secrets_from_spec_passed_to_manager(client):
    test_client, mock_manager = client
    mock_manager.create.return_value = ({"spec": {}, "status": {}}, True)

    response = test_client.put(
        "/servers/web-1",
        json={**PUT_BODY, "secrets": {"AI_ENGINE_TOKEN": "sk-abc"}},
    )

    assert response.status_code == 201
    called_spec, called_kwargs = mock_manager.create.call_args
    assert "secrets" not in called_spec[0]
    assert called_kwargs["secrets"] == {"AI_ENGINE_TOKEN": "sk-abc"}


def test_put_server_rejects_unknown_startup_script(client):
    test_client, mock_manager = client

    response = test_client.put(
        "/servers/web-1", json={**PUT_BODY, "startup_script": "no-such-template"}
    )

    assert response.status_code == 422
    mock_manager.create.assert_not_called()


def test_put_server_returns_422_on_startup_script_error(client):
    test_client, mock_manager = client
    mock_manager.create.side_effect = StartupScriptError(
        "secrets['AI_ENGINE_TOKEN'] が必須です"
    )

    response = test_client.put(
        "/servers/web-1",
        json={**PUT_BODY, "startup_script": "opencode-sakura-ai-engine"},
    )

    assert response.status_code == 422


# --- start_server ---


def test_start_server_returns_200(client):
    test_client, mock_manager = client
    mock_manager.start.return_value = {"spec": {}, "status": {}}

    response = test_client.post("/servers/web-1/start")

    assert response.status_code == 200
    mock_manager.start.assert_called_once_with("web-1")


def test_start_server_returns_404_when_not_found(client):
    test_client, mock_manager = client
    mock_manager.start.side_effect = ServerNotFound("web-1")

    response = test_client.post("/servers/web-1/start")

    assert response.status_code == 404


# --- stop_server ---


def test_stop_server_returns_200(client):
    test_client, mock_manager = client
    mock_manager.stop.return_value = {"spec": {}, "status": {}}

    response = test_client.post("/servers/web-1/stop")

    assert response.status_code == 200
    mock_manager.stop.assert_called_once_with("web-1", force=False)


def test_stop_server_forwards_force_from_body(client):
    test_client, mock_manager = client
    mock_manager.stop.return_value = {"spec": {}, "status": {}}

    response = test_client.post("/servers/web-1/stop", json={"force": True})

    assert response.status_code == 200
    mock_manager.stop.assert_called_once_with("web-1", force=True)


# --- restart_server ---


def test_restart_server_returns_200(client):
    test_client, mock_manager = client
    mock_manager.restart.return_value = {"spec": {}, "status": {}}

    response = test_client.post("/servers/web-1/restart")

    assert response.status_code == 200
    mock_manager.restart.assert_called_once_with("web-1", force=False)


def test_restart_server_forwards_force_from_body(client):
    test_client, mock_manager = client
    mock_manager.restart.return_value = {"spec": {}, "status": {}}

    response = test_client.post("/servers/web-1/restart", json={"force": True})

    assert response.status_code == 200
    mock_manager.restart.assert_called_once_with("web-1", force=True)


def test_restart_server_returns_409_when_not_running(client):
    test_client, mock_manager = client
    mock_manager.restart.side_effect = ServerNotRunning("web-1")

    response = test_client.post("/servers/web-1/restart")

    assert response.status_code == 409


# --- delete_server ---


def test_delete_server_returns_204(client):
    test_client, mock_manager = client

    response = test_client.delete("/servers/web-1")

    assert response.status_code == 204
    mock_manager.delete.assert_called_once_with("web-1")


# --- reinstall_server ---


def test_reinstall_server_returns_200(client):
    test_client, mock_manager = client
    mock_manager.reinstall.return_value = {"spec": {}, "status": {}}

    response = test_client.post("/servers/web-1/reinstall")

    assert response.status_code == 200
    mock_manager.reinstall.assert_called_once_with("web-1", secrets=None)


def test_reinstall_server_forwards_secrets_from_body(client):
    test_client, mock_manager = client
    mock_manager.reinstall.return_value = {"spec": {}, "status": {}}

    response = test_client.post(
        "/servers/web-1/reinstall",
        json={"secrets": {"AI_ENGINE_TOKEN": "sk-abc"}},
    )

    assert response.status_code == 200
    mock_manager.reinstall.assert_called_once_with(
        "web-1", secrets={"AI_ENGINE_TOKEN": "sk-abc"}
    )
