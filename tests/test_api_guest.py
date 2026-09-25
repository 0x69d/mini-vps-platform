"""Web API の guest agent 系エンドポイント(exec / pause / resume / ssh)のテスト。"""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import mini_vps.api as api_module
from mini_vps.errors import GuestAgentUnavailable, GuestExecError
from mini_vps.manager import ServerNotFound, ServerNotRunning


@pytest.fixture
def client(monkeypatch):
    """ServerManager を Mock に差し替えた TestClient を返す(test_api.py と同じ形)。"""
    mock_manager = MagicMock()
    monkeypatch.setattr("mini_vps.api.libvirt.open", lambda uri: MagicMock())
    api_module.app.dependency_overrides[api_module.get_manager] = lambda: mock_manager
    with TestClient(api_module.app) as test_client:
        yield test_client, mock_manager
    api_module.app.dependency_overrides.clear()


EXEC_RESULT = {
    "pid": 42,
    "exit_code": 0,
    "signal": None,
    "stdout": "hello\n",
    "stderr": "",
    "truncated": False,
    "timed_out": False,
}


# --- exec ---


def test_exec_returns_result(client):
    test_client, mock_manager = client
    mock_manager.exec.return_value = EXEC_RESULT

    response = test_client.post(
        "/servers/web-1/exec",
        json={"argv": ["cat"], "stdin": "hi", "timeout": 5},
    )

    assert response.status_code == 200
    assert response.json() == EXEC_RESULT
    mock_manager.exec.assert_called_once_with("web-1", ["cat"], stdin="hi", timeout=5)


def test_exec_defaults(client):
    test_client, mock_manager = client
    mock_manager.exec.return_value = EXEC_RESULT

    test_client.post("/servers/web-1/exec", json={"argv": ["uptime"]})

    mock_manager.exec.assert_called_once_with(
        "web-1", ["uptime"], stdin=None, timeout=60
    )


@pytest.mark.parametrize(
    "body",
    [
        {"argv": []},
        {},
        {"argv": ["true"], "timeout": 0},
        {"argv": ["true"], "timeout": 3601},
    ],
)
def test_exec_rejects_invalid_body(client, body):
    test_client, mock_manager = client

    response = test_client.post("/servers/web-1/exec", json=body)

    assert response.status_code == 422
    mock_manager.exec.assert_not_called()


@pytest.mark.parametrize(
    ("exc", "status", "label"),
    [
        (GuestAgentUnavailable("web-1: x"), 409, "guest agent unavailable"),
        (ServerNotRunning("web-1"), 409, "server not running"),
        (ServerNotFound("web-1"), 404, "server not found"),
        (GuestExecError("web-1: no such file"), 422, "guest exec failed"),
    ],
)
def test_exec_normalizes_errors(client, exc, status, label):
    test_client, mock_manager = client
    mock_manager.exec.side_effect = exc

    response = test_client.post("/servers/web-1/exec", json={"argv": ["true"]})

    assert response.status_code == status
    assert response.json()["detail"].startswith(label)


# --- pause / resume ---


def test_pause_returns_200(client):
    test_client, mock_manager = client
    mock_manager.pause.return_value = {"spec": {}, "status": {"state": "paused"}}

    response = test_client.post("/servers/web-1/pause")

    assert response.status_code == 200
    mock_manager.pause.assert_called_once_with("web-1")


def test_resume_returns_200(client):
    test_client, mock_manager = client
    mock_manager.resume.return_value = {"spec": {}, "status": {"state": "running"}}

    response = test_client.post("/servers/web-1/resume")

    assert response.status_code == 200
    mock_manager.resume.assert_called_once_with("web-1")


def test_pause_stopped_returns_409(client):
    test_client, mock_manager = client
    mock_manager.pause.side_effect = ServerNotRunning("web-1")

    assert test_client.post("/servers/web-1/pause").status_code == 409


# --- ssh ---


def test_ssh_endpoint_returns_endpoint(client):
    test_client, mock_manager = client
    endpoint = {
        "host": "192.168.122.10",
        "port": 22,
        "user": "ubuntu",
        "identity_file": "/root/.ssh/minivps_ed25519",
    }
    mock_manager.ssh_endpoint.return_value = endpoint

    response = test_client.get("/servers/web-1/ssh")

    assert response.status_code == 200
    assert response.json() == endpoint
    mock_manager.ssh_endpoint.assert_called_once_with("web-1")
