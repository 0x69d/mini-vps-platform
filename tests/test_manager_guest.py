"""ServerManager の guest agent 系操作(exec/pause/resume/ssh_endpoint)のテスト。"""

import contextlib
import logging
from unittest.mock import MagicMock

import libvirt
import pytest
from conftest import make_libvirt_error

from mini_vps.guest_agent import ExecResult
from mini_vps.manager import (
    GuestAgentUnavailable,
    ServerManager,
    ServerNotRunning,
    _status_of,
)
from mini_vps.resources import _mac_for_interface
from mini_vps.spec import ServerSpec

SPEC = ServerSpec(
    name="web-1", memory=1024, vcpus=2, base_image="ubuntu-24.04.img", disk=10
).model_dump()

USER_MODE_XML = """\
<domain type='hvf' xmlns:qemu='http://libvirt.org/schemas/domain/qemu/1.0'>
  <name>web-1</name>
  <qemu:commandline>
    <qemu:arg value='-netdev'/>
    <qemu:arg value='user,id=minivps0,hostfwd=tcp:127.0.0.1:2203-:22'/>
  </qemu:commandline>
</domain>
"""

LIBVIRT_NET_XML = "<domain type='kvm'><name>web-1</name></domain>"


def _dom(state=libvirt.VIR_DOMAIN_RUNNING, xml=LIBVIRT_NET_XML):
    dom = MagicMock()
    dom.name.return_value = "web-1"
    dom.state.return_value = [state, 1]
    dom.isActive.return_value = state in (
        libvirt.VIR_DOMAIN_RUNNING,
        libvirt.VIR_DOMAIN_PAUSED,
    )
    dom.XMLDesc.return_value = xml
    return dom


@pytest.fixture
def mgr_with_dom(monkeypatch):
    """_lookup / _read_spec を差し替えた ServerManager と domain の組を返す。"""

    def _make(dom):
        mgr = ServerManager(MagicMock())
        monkeypatch.setattr("mini_vps.manager._lookup", lambda c, n: dom)
        monkeypatch.setattr("mini_vps.manager._read_spec", lambda d: SPEC)
        mgr.get = MagicMock(return_value={"spec": SPEC, "status": {}})
        return mgr

    return _make


def _forbid_lock(mgr):
    """name ロックを取ったら失敗させる(exec 等がロックを取らないことの検証用)。"""

    @contextlib.contextmanager
    def _locked(name):
        raise AssertionError("lock must not be taken")
        yield  # pragma: no cover

    mgr._locked = _locked


# --- exec ---


def _result(**overrides):
    values = {
        "pid": 42,
        "exit_code": 0,
        "signal": None,
        "stdout": b"out\n",
        "stderr": b"",
        "truncated": False,
        "timed_out": False,
    }
    values.update(overrides)
    return ExecResult(**values)


def test_exec_returns_decoded_result_without_lock(mgr_with_dom, monkeypatch):
    dom = _dom()
    mgr = mgr_with_dom(dom)
    _forbid_lock(mgr)
    exec_mock = MagicMock(return_value=_result(stderr=b"\xffbad"))
    monkeypatch.setattr("mini_vps.manager.guest_agent.exec_command", exec_mock)

    result = mgr.exec("web-1", ["cat"], stdin="日本語", timeout=5)

    exec_mock.assert_called_once_with(dom, ["cat"], stdin="日本語".encode(), timeout=5)
    assert result == {
        "pid": 42,
        "exit_code": 0,
        "signal": None,
        "stdout": "out\n",
        "stderr": "�bad",
        "truncated": False,
        "timed_out": False,
    }


def test_exec_passes_bytes_stdin_through(mgr_with_dom, monkeypatch):
    dom = _dom()
    mgr = mgr_with_dom(dom)
    exec_mock = MagicMock(return_value=_result())
    monkeypatch.setattr("mini_vps.manager.guest_agent.exec_command", exec_mock)

    mgr.exec("web-1", ["cat"], stdin=b"\x00\x01")

    assert exec_mock.call_args.kwargs["stdin"] == b"\x00\x01"


@pytest.mark.parametrize(
    "state", [libvirt.VIR_DOMAIN_SHUTOFF, libvirt.VIR_DOMAIN_PAUSED]
)
def test_exec_rejects_non_running_domain(mgr_with_dom, monkeypatch, state):
    mgr = mgr_with_dom(_dom(state))
    exec_mock = MagicMock()
    monkeypatch.setattr("mini_vps.manager.guest_agent.exec_command", exec_mock)

    with pytest.raises(ServerNotRunning):
        mgr.exec("web-1", ["true"])

    exec_mock.assert_not_called()


def test_exec_logs_only_name_command_and_exit_code(mgr_with_dom, monkeypatch, caplog):
    mgr = mgr_with_dom(_dom())
    monkeypatch.setattr(
        "mini_vps.manager.guest_agent.exec_command",
        MagicMock(return_value=_result(stdout=b"OUTPUT-SECRET", exit_code=3)),
    )

    with caplog.at_level(logging.DEBUG, logger="mini_vps"):
        mgr.exec("web-1", ["mysql", "--password=ARG-SECRET"], stdin="STDIN-SECRET")

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "web-1" in text
    assert "mysql" in text
    assert "exit_code=3" in text
    for secret in ("ARG-SECRET", "STDIN-SECRET", "OUTPUT-SECRET"):
        assert secret not in text


def test_exec_logs_timeout_as_warning(mgr_with_dom, monkeypatch, caplog):
    mgr = mgr_with_dom(_dom())
    monkeypatch.setattr(
        "mini_vps.manager.guest_agent.exec_command",
        MagicMock(return_value=_result(exit_code=None, stdout=b"", timed_out=True)),
    )

    with caplog.at_level(logging.WARNING, logger="mini_vps"):
        result = mgr.exec("web-1", ["sleep", "100"])

    assert result["timed_out"] is True
    assert any("タイムアウト" in r.getMessage() for r in caplog.records)


# --- pause / resume ---


def test_pause_suspends_running_domain(mgr_with_dom):
    dom = _dom()
    mgr = mgr_with_dom(dom)

    mgr.pause("web-1")

    dom.suspend.assert_called_once()
    mgr.get.assert_called_once_with("web-1")


def test_pause_is_idempotent_when_already_paused(mgr_with_dom):
    dom = _dom(libvirt.VIR_DOMAIN_PAUSED)
    mgr = mgr_with_dom(dom)

    mgr.pause("web-1")

    dom.suspend.assert_not_called()


def test_pause_rejects_stopped_domain(mgr_with_dom):
    dom = _dom(libvirt.VIR_DOMAIN_SHUTOFF)
    mgr = mgr_with_dom(dom)

    with pytest.raises(ServerNotRunning):
        mgr.pause("web-1")

    dom.suspend.assert_not_called()


def test_resume_resumes_paused_domain(mgr_with_dom):
    dom = _dom(libvirt.VIR_DOMAIN_PAUSED)
    mgr = mgr_with_dom(dom)

    mgr.resume("web-1")

    dom.resume.assert_called_once()
    mgr.get.assert_called_once_with("web-1")


def test_resume_is_idempotent_when_running(mgr_with_dom):
    dom = _dom()
    mgr = mgr_with_dom(dom)

    mgr.resume("web-1")

    dom.resume.assert_not_called()


def test_resume_rejects_stopped_domain(mgr_with_dom):
    dom = _dom(libvirt.VIR_DOMAIN_SHUTOFF)
    mgr = mgr_with_dom(dom)

    with pytest.raises(ServerNotRunning):
        mgr.resume("web-1")


def test_pause_and_resume_take_name_lock(mgr_with_dom):
    mgr = mgr_with_dom(_dom())
    _forbid_lock(mgr)

    with pytest.raises(AssertionError):
        mgr.pause("web-1")
    with pytest.raises(AssertionError):
        mgr.resume("web-1")


# --- ssh_endpoint ---


def test_ssh_endpoint_user_mode_uses_forwarded_port(mgr_with_dom, monkeypatch):
    monkeypatch.setenv("HOME", "/home/alice")
    mgr = mgr_with_dom(_dom(xml=USER_MODE_XML))
    _forbid_lock(mgr)

    assert mgr.ssh_endpoint("web-1") == {
        "host": "127.0.0.1",
        "port": 2203,
        "user": "ubuntu",
        "identity_file": "/home/alice/.ssh/minivps_ed25519",
    }


def test_ssh_endpoint_libvirt_network_uses_ip_port_22(mgr_with_dom, monkeypatch):
    monkeypatch.setenv("HOME", "/home/alice")
    mgr = mgr_with_dom(_dom())
    monkeypatch.setattr("mini_vps.manager._lease_ipv4", lambda d: "192.168.122.10")

    endpoint = mgr.ssh_endpoint("web-1")

    assert endpoint["host"] == "192.168.122.10"
    assert endpoint["port"] == 22


def test_ssh_endpoint_without_ip_is_agent_unavailable(mgr_with_dom, monkeypatch):
    mgr = mgr_with_dom(_dom())
    monkeypatch.setattr("mini_vps.manager._lease_ipv4", lambda d: None)
    monkeypatch.setattr(
        "mini_vps.manager.guest_agent.agent_ipv4", MagicMock(return_value=None)
    )

    with pytest.raises(GuestAgentUnavailable):
        mgr.ssh_endpoint("web-1")


@pytest.mark.parametrize(
    "state", [libvirt.VIR_DOMAIN_SHUTOFF, libvirt.VIR_DOMAIN_PAUSED]
)
def test_ssh_endpoint_rejects_non_running_domain(mgr_with_dom, state):
    mgr = mgr_with_dom(_dom(state, xml=USER_MODE_XML))

    with pytest.raises(ServerNotRunning):
        mgr.ssh_endpoint("web-1")


# --- _status_of の guest agent フォールバック ---


def test_status_of_falls_back_to_guest_agent(monkeypatch):
    monkeypatch.setattr("mini_vps.manager._lease_ipv4", lambda d: None)
    agent_mock = MagicMock(return_value="10.0.2.15")
    monkeypatch.setattr("mini_vps.manager.guest_agent.agent_ipv4", agent_mock)
    dom = _dom()

    assert _status_of(dom, SPEC) == {"state": "running", "ip": "10.0.2.15"}
    agent_mock.assert_called_once_with(dom, {_mac_for_interface("web-1", 0)})


def test_status_of_prefers_lease_over_agent(monkeypatch):
    monkeypatch.setattr("mini_vps.manager._lease_ipv4", lambda d: "192.168.122.10")
    agent_mock = MagicMock()
    monkeypatch.setattr("mini_vps.manager.guest_agent.agent_ipv4", agent_mock)

    assert _status_of(_dom(), SPEC)["ip"] == "192.168.122.10"
    agent_mock.assert_not_called()


@pytest.mark.parametrize(
    "exc",
    [
        GuestAgentUnavailable("web-1"),
        ServerNotRunning("web-1"),
        make_libvirt_error(libvirt.VIR_ERR_RPC),
    ],
)
def test_status_of_swallows_agent_errors(monkeypatch, exc):
    monkeypatch.setattr("mini_vps.manager._lease_ipv4", lambda d: None)
    monkeypatch.setattr(
        "mini_vps.manager.guest_agent.agent_ipv4", MagicMock(side_effect=exc)
    )

    assert _status_of(_dom(), SPEC) == {"state": "running", "ip": None}


def test_status_of_does_not_ask_agent_when_not_running(monkeypatch):
    agent_mock = MagicMock()
    monkeypatch.setattr("mini_vps.manager.guest_agent.agent_ipv4", agent_mock)

    assert _status_of(_dom(libvirt.VIR_DOMAIN_PAUSED), SPEC) == {
        "state": "paused",
        "ip": None,
    }
    agent_mock.assert_not_called()
