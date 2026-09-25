import base64
import json
from unittest.mock import MagicMock

import libvirt
import pytest
from conftest import make_libvirt_error

from mini_vps import errors, guest_agent
from mini_vps.errors import GuestAgentUnavailable, GuestExecError, ServerNotRunning
from mini_vps.guest_agent import (
    OUTPUT_LIMIT_BYTES,
    QGA_OUTPUT_MAX_BYTES,
    STDIN_LIMIT_BYTES,
    ExecResult,
    build_exec_arguments,
    build_request,
    decode_output,
    parse_exec_status,
    parse_reply,
    pick_ipv4,
    translate_error,
)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _reply(value) -> str:
    return json.dumps({"return": value})


# --- errors.ERROR_TABLE ---


def test_guest_agent_unavailable_maps_to_409_and_exit_9():
    mapping = errors.lookup(GuestAgentUnavailable("web-1"))
    assert (mapping.http_status, mapping.exit_code, mapping.label) == (
        409,
        9,
        "guest agent unavailable",
    )


def test_guest_exec_error_maps_to_input_error():
    mapping = errors.lookup(GuestExecError("x"))
    assert (mapping.http_status, mapping.exit_code) == (422, 1)


# --- build_request / parse_reply ---


def test_build_request_without_arguments():
    assert json.loads(build_request("guest-ping")) == {"execute": "guest-ping"}


def test_build_request_with_arguments():
    assert json.loads(build_request("guest-exec-status", {"pid": 7})) == {
        "execute": "guest-exec-status",
        "arguments": {"pid": 7},
    }


def test_parse_reply_returns_return_value():
    assert parse_reply('{"return": {"pid": 42}}') == {"pid": 42}


def test_parse_reply_rejects_reply_without_return():
    with pytest.raises(ValueError):
        parse_reply('{"foo": 1}')


# --- build_exec_arguments ---


def test_build_exec_arguments_splits_path_and_args():
    assert build_exec_arguments(["ls", "-la", "/"]) == {
        "path": "ls",
        "arg": ["-la", "/"],
        "capture-output": True,
    }


def test_build_exec_arguments_encodes_stdin_as_base64():
    args = build_exec_arguments(["cat"], stdin=b"hello\n")
    assert base64.b64decode(args["input-data"]) == b"hello\n"


def test_build_exec_arguments_passes_empty_stdin():
    # 空の stdin も「渡す」(EOF を即座に受け取る)として区別する。
    assert build_exec_arguments(["cat"], stdin=b"")["input-data"] == ""


def test_build_exec_arguments_rejects_empty_argv():
    with pytest.raises(GuestExecError):
        build_exec_arguments([])


def test_build_exec_arguments_rejects_oversized_stdin():
    with pytest.raises(GuestExecError):
        build_exec_arguments(["cat"], stdin=b"x" * (STDIN_LIMIT_BYTES + 1))


# --- decode_output ---


def test_decode_output_none_is_empty():
    assert decode_output(None, None) == (b"", False)


def test_decode_output_decodes_base64():
    assert decode_output(_b64(b"out"), None) == (b"out", False)


def test_decode_output_truncates_to_limit():
    assert decode_output(_b64(b"abcdef"), False, limit=4) == (b"abcd", True)


def test_decode_output_honours_agent_truncated_true():
    # QEMU 8.2 より後の qga は出力があれば値付きで常に返す。
    assert decode_output(_b64(b"out"), True) == (b"out", True)


def test_decode_output_treats_full_capture_as_truncated():
    # QEMU 8.2 までの qga は切り詰め時に値 false のキーを返すため、長さで判定する。
    raw = b"x" * QGA_OUTPUT_MAX_BYTES
    data, truncated = decode_output(_b64(raw), False)
    assert truncated is True
    assert len(data) == OUTPUT_LIMIT_BYTES


# --- parse_exec_status ---


def test_parse_exec_status_returns_none_while_running():
    assert parse_exec_status({"exited": False}, 7) is None


def test_parse_exec_status_decodes_finished_process():
    status = {
        "exited": True,
        "exitcode": 3,
        "out-data": _b64(b"hello\n"),
        "err-data": _b64(b"oops\n"),
    }
    assert parse_exec_status(status, 7) == ExecResult(
        pid=7,
        exit_code=3,
        signal=None,
        stdout=b"hello\n",
        stderr=b"oops\n",
        truncated=False,
        timed_out=False,
    )


def test_parse_exec_status_reports_signal():
    result = parse_exec_status({"exited": True, "signal": 9}, 7)
    assert result.exit_code is None
    assert result.signal == 9
    assert result.stdout == b""


def test_parse_exec_status_truncated_if_either_stream_truncated():
    status = {"exited": True, "exitcode": 0, "err-data": _b64(b"abcdef")}
    assert parse_exec_status(status, 7, limit=3).truncated is True


# --- pick_ipv4 ---

IPV4 = libvirt.VIR_IP_ADDR_TYPE_IPV4
IPV6 = libvirt.VIR_IP_ADDR_TYPE_IPV6


def _iface(hwaddr, *addrs):
    return {
        "hwaddr": hwaddr,
        "addrs": [{"type": t, "addr": a, "prefix": 24} for t, a in addrs],
    }


def test_pick_ipv4_skips_loopback_and_ipv6():
    ifaces = {
        "lo": _iface("00:00:00:00:00:00", (IPV4, "127.0.0.1")),
        "enp1s0": _iface(
            "52:54:00:aa:bb:cc", (IPV6, "fe80::1"), (IPV4, "192.168.122.10")
        ),
    }
    assert pick_ipv4(ifaces) == "192.168.122.10"


def test_pick_ipv4_skips_link_local():
    ifaces = {"eth0": _iface("52:54:00:aa:bb:cc", (IPV4, "169.254.1.1"))}
    assert pick_ipv4(ifaces) is None


def test_pick_ipv4_prefers_matching_mac():
    ifaces = {
        "docker0": _iface("02:42:ac:11:00:01", (IPV4, "172.17.0.1")),
        "enp1s0": _iface("52:54:00:AA:BB:CC", (IPV4, "192.168.122.10")),
    }
    assert pick_ipv4(ifaces, {"52:54:00:aa:bb:cc"}) == "192.168.122.10"


def test_pick_ipv4_falls_back_to_first_when_no_mac_matches():
    ifaces = {"docker0": _iface("02:42:ac:11:00:01", (IPV4, "172.17.0.1"))}
    assert pick_ipv4(ifaces, {"52:54:00:aa:bb:cc"}) == "172.17.0.1"


def test_pick_ipv4_empty_is_none():
    assert pick_ipv4({}) is None
    assert pick_ipv4(None) is None


# --- translate_error ---


def test_translate_error_not_running():
    exc = translate_error(make_libvirt_error(libvirt.VIR_ERR_OPERATION_INVALID), "w")
    assert isinstance(exc, ServerNotRunning)


def test_translate_error_missing_channel_mentions_recreate():
    exc = translate_error(
        make_libvirt_error(libvirt.VIR_ERR_ARGUMENT_UNSUPPORTED), "web-1"
    )
    assert isinstance(exc, GuestAgentUnavailable)
    assert "channel" in str(exc)
    assert "create" in str(exc)


@pytest.mark.parametrize(
    "code", [libvirt.VIR_ERR_AGENT_UNRESPONSIVE, libvirt.VIR_ERR_AGENT_UNSYNCED]
)
def test_translate_error_unresponsive_agent(code):
    exc = translate_error(make_libvirt_error(code), "web-1")
    assert isinstance(exc, GuestAgentUnavailable)
    assert "未導入" in str(exc)


def test_translate_error_disabled_command():
    err = libvirt.libvirtError(
        "internal error: unable to execute QEMU agent command 'guest-exec': "
        "The command guest-exec has been disabled for this instance"
    )
    err.get_error_code = lambda: libvirt.VIR_ERR_INTERNAL_ERROR
    exc = translate_error(err, "web-1")
    assert isinstance(exc, GuestAgentUnavailable)
    assert "無効化" in str(exc)


def test_translate_error_leaves_other_errors():
    err = make_libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR)
    assert translate_error(err, "web-1") is None
    assert translate_error(make_libvirt_error(libvirt.VIR_ERR_RPC), "w") is None


# --- ping / exec_command / agent_ipv4 ---


@pytest.fixture
def agent(monkeypatch):
    """libvirt_qemu.qemuAgentCommand を差し替え、送られた JSON を記録する。"""
    mock = MagicMock()
    monkeypatch.setattr(guest_agent.libvirt_qemu, "qemuAgentCommand", mock)
    return mock


def _sent(agent_mock) -> list[dict]:
    return [json.loads(c.args[1]) for c in agent_mock.call_args_list]


def test_ping_sends_guest_ping(agent):
    dom = MagicMock()
    agent.return_value = _reply({})

    guest_agent.ping(dom)

    assert _sent(agent) == [{"execute": "guest-ping"}]
    assert agent.call_args.args[0] is dom
    assert agent.call_args.args[2] == guest_agent.AGENT_CALL_TIMEOUT_SECONDS
    assert agent.call_args.args[3] == 0


def test_ping_translates_unavailable_agent(agent):
    agent.side_effect = make_libvirt_error(libvirt.VIR_ERR_AGENT_UNRESPONSIVE)
    with pytest.raises(GuestAgentUnavailable):
        guest_agent.ping(MagicMock())


def test_exec_command_polls_until_exited(agent):
    agent.side_effect = [
        _reply({"pid": 42}),
        _reply({"exited": False}),
        _reply({"exited": False}),
        _reply({"exited": True, "exitcode": 0, "out-data": _b64(b"hi\n")}),
    ]
    sleeps = []

    result = guest_agent.exec_command(
        MagicMock(),
        ["echo", "hi"],
        stdin=b"in",
        clock=lambda: 0.0,
        sleep=sleeps.append,
    )

    assert result.exit_code == 0
    assert result.stdout == b"hi\n"
    assert result.pid == 42
    sent = _sent(agent)
    assert sent[0]["execute"] == "guest-exec"
    assert sent[0]["arguments"]["path"] == "echo"
    assert sent[0]["arguments"]["arg"] == ["hi"]
    assert base64.b64decode(sent[0]["arguments"]["input-data"]) == b"in"
    assert sent[1:] == [{"execute": "guest-exec-status", "arguments": {"pid": 42}}] * 3
    # 間隔は倍々で伸びる。
    assert sleeps == [0.05, 0.1]


def test_exec_command_returns_timed_out_result(agent):
    agent.side_effect = [_reply({"pid": 42})] + [_reply({"exited": False})] * 10
    now = [0.0]

    def _sleep(seconds):
        now[0] += seconds

    result = guest_agent.exec_command(
        MagicMock(), ["sleep", "100"], timeout=0.2, clock=lambda: now[0], sleep=_sleep
    )

    assert result.timed_out is True
    assert result.exit_code is None
    assert result.pid == 42
    # 残り時間を超えて眠らない。
    assert now[0] == pytest.approx(0.2)


def test_exec_command_rejects_non_positive_timeout(agent):
    with pytest.raises(GuestExecError):
        guest_agent.exec_command(MagicMock(), ["true"], timeout=0)
    agent.assert_not_called()


def test_exec_command_maps_spawn_failure_to_guest_exec_error(agent):
    err = libvirt.libvirtError(
        "internal error: unable to execute QEMU agent command 'guest-exec': "
        "Failed to execute child process “nosuch” (No such file or directory)"
    )
    err.get_error_code = lambda: libvirt.VIR_ERR_INTERNAL_ERROR
    agent.side_effect = err
    dom = MagicMock()
    dom.name.return_value = "web-1"

    with pytest.raises(GuestExecError, match="nosuch"):
        guest_agent.exec_command(dom, ["nosuch"])


def test_exec_command_translates_missing_channel(agent):
    agent.side_effect = make_libvirt_error(libvirt.VIR_ERR_ARGUMENT_UNSUPPORTED)
    with pytest.raises(GuestAgentUnavailable):
        guest_agent.exec_command(MagicMock(), ["true"])


def test_exec_command_propagates_other_libvirt_errors(agent):
    agent.side_effect = make_libvirt_error(libvirt.VIR_ERR_RPC)
    with pytest.raises(libvirt.libvirtError):
        guest_agent.exec_command(MagicMock(), ["true"])


def test_exec_command_vm_stopped_during_poll_is_not_running(agent):
    agent.side_effect = [
        _reply({"pid": 42}),
        make_libvirt_error(libvirt.VIR_ERR_OPERATION_INVALID),
    ]
    with pytest.raises(ServerNotRunning):
        guest_agent.exec_command(
            MagicMock(), ["sleep", "10"], clock=lambda: 0.0, sleep=lambda s: None
        )


def test_agent_ipv4_reads_agent_source():
    dom = MagicMock()
    dom.interfaceAddresses.return_value = {
        "enp1s0": _iface("52:54:00:aa:bb:cc", (IPV4, "10.0.2.15"))
    }

    assert guest_agent.agent_ipv4(dom) == "10.0.2.15"
    dom.interfaceAddresses.assert_called_once_with(
        libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_AGENT
    )


def test_agent_ipv4_translates_errors():
    dom = MagicMock()
    dom.interfaceAddresses.side_effect = make_libvirt_error(
        libvirt.VIR_ERR_AGENT_UNRESPONSIVE
    )
    with pytest.raises(GuestAgentUnavailable):
        guest_agent.agent_ipv4(dom)
