import asyncio
import json
from unittest.mock import MagicMock

import libvirt
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mini_vps.errors import ServerNotFound
from mini_vps.mcp_server import build_server, destructive_allowed


def _run(coro):
    return asyncio.run(coro)


def _tool_names(server):
    return {t.name for t in _run(server.list_tools())}


def _call(server, tool, **arguments):
    result = _run(server.call_tool(tool, arguments))
    assert result.is_error is False, result.content[0].text
    return json.loads(result.content[0].text)


def _call_error(server, tool, **arguments):
    """ツールのエラーを文字列で返す(SDK の版により例外か is_error で返る)。"""
    try:
        result = _run(server.call_tool(tool, arguments))
    except ToolError as e:
        return str(e)
    assert result.is_error is True
    return result.content[0].text


@pytest.fixture
def manager():
    return MagicMock()


def test_destructive_tools_are_hidden_by_default(manager):
    names = _tool_names(build_server(manager))
    assert {"list_servers", "get_server", "create_server", "stop_server"} <= names
    assert "delete_server" not in names
    assert "reinstall_server" not in names


def test_destructive_tools_are_registered_when_allowed(manager):
    names = _tool_names(build_server(manager, allow_destructive=True))
    assert {"delete_server", "reinstall_server"} <= names


@pytest.mark.parametrize(
    ("env", "expected"),
    [({}, False), ({"MINIVPS_MCP_ALLOW_DESTRUCTIVE": "1"}, True)],
)
def test_destructive_allowed_reads_env(env, expected):
    assert destructive_allowed(env) is expected


def test_read_only_tools_are_annotated(manager):
    tools = {t.name: t for t in _run(build_server(manager).list_tools())}
    assert tools["list_servers"].annotations.read_only_hint is True
    assert tools["create_server"].annotations.idempotent_hint is True


def test_list_servers_delegates_to_manager(manager):
    manager.list.return_value = ["web-1", "agent-1"]
    assert _call(build_server(manager), "list_servers") == {
        "servers": ["web-1", "agent-1"]
    }


def test_create_server_validates_spec_and_reports_created(manager):
    manager.create.return_value = ({"spec": {}, "status": {}}, True)
    spec = {"memory": 1024, "vcpus": 2, "base_image": "ubuntu-24.04.img", "disk": 10}

    result = _call(
        build_server(manager),
        "create_server",
        name="agent-1",
        spec=spec,
        secrets={"AI_ENGINE_TOKEN": "sk"},
    )

    assert result["created"] is True
    passed_spec = manager.create.call_args.args[0]
    assert passed_spec["name"] == "agent-1"
    assert passed_spec["hostname"] == "agent-1"  # ServerSpec の補完を通っている
    assert manager.create.call_args.kwargs == {"secrets": {"AI_ENGINE_TOKEN": "sk"}}


def test_create_server_rejects_invalid_spec_without_calling_manager(manager):
    message = _call_error(
        build_server(manager), "create_server", name="agent-1", spec={"memory": -1}
    )
    assert "invalid input" in message
    manager.create.assert_not_called()


def test_manager_errors_use_error_table_labels(manager):
    manager.get.side_effect = ServerNotFound("ghost")
    message = _call_error(build_server(manager), "get_server", name="ghost")
    assert "server not found: ghost" in message


def test_libvirt_errors_are_reported_as_tool_errors(manager):
    manager.status.side_effect = libvirt.libvirtError("connection lost")
    message = _call_error(build_server(manager), "server_status", name="web-1")
    assert "libvirt error" in message


def test_delete_server_delegates_when_allowed(manager):
    result = _call(
        build_server(manager, allow_destructive=True), "delete_server", name="web-1"
    )
    assert result == {"deleted": "web-1"}
    manager.delete.assert_called_once_with("web-1")
