"""CLI の guest agent 系コマンド(exec / ssh / console / pause / resume)のテスト。"""

import contextlib
import io
import json
from unittest.mock import MagicMock

import pytest

from mini_vps import cli
from mini_vps.errors import GuestAgentUnavailable, GuestExecError
from mini_vps.guest_agent import STDIN_LIMIT_BYTES
from mini_vps.manager import ServerNotFound, ServerNotRunning

EXEC_RESULT = {
    "pid": 42,
    "exit_code": 0,
    "signal": None,
    "stdout": "hello\n",
    "stderr": "",
    "truncated": False,
    "timed_out": False,
}

ENDPOINT = {
    "host": "127.0.0.1",
    "port": 2203,
    "user": "ubuntu",
    "identity_file": "/home/alice/.ssh/minivps_ed25519",
}


@pytest.fixture
def mock_manager():
    return MagicMock()


def _factory(mgr):
    return lambda: contextlib.nullcontext(mgr)


@pytest.fixture
def execvp(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr(cli.os, "execvp", mock)
    return mock


# --- exec ---


def test_exec_prints_json_by_default(mock_manager, capsys):
    mock_manager.exec.return_value = EXEC_RESULT

    exit_code = cli.main(
        ["exec", "web-1", "--", "ls", "-la", "/"],
        manager_factory=_factory(mock_manager),
    )

    assert exit_code == 0
    mock_manager.exec.assert_called_once_with(
        "web-1", ["ls", "-la", "/"], stdin=None, timeout=60
    )
    assert json.loads(capsys.readouterr().out) == EXEC_RESULT


def test_exec_accepts_command_without_separator(mock_manager):
    mock_manager.exec.return_value = EXEC_RESULT

    cli.main(["exec", "web-1", "uptime"], manager_factory=_factory(mock_manager))

    assert mock_manager.exec.call_args.args == ("web-1", ["uptime"])


def test_exec_options_after_name_before_separator(mock_manager):
    mock_manager.exec.return_value = EXEC_RESULT

    cli.main(
        ["exec", "web-1", "--timeout", "5", "--raw", "--", "ls", "--raw"],
        manager_factory=_factory(mock_manager),
    )

    # -- より後の --raw はゲストのコマンドの引数として渡る。
    mock_manager.exec.assert_called_once_with(
        "web-1", ["ls", "--raw"], stdin=None, timeout=5
    )


def test_exec_requires_command(mock_manager):
    exit_code = cli.main(["exec", "web-1"], manager_factory=_factory(mock_manager))

    assert exit_code == 2
    mock_manager.exec.assert_not_called()


def test_exec_raw_streams_output_and_uses_guest_exit_code(mock_manager, capsys):
    mock_manager.exec.return_value = {
        **EXEC_RESULT,
        "exit_code": 3,
        "stdout": "out",
        "stderr": "err",
    }

    exit_code = cli.main(
        ["exec", "--raw", "web-1", "--", "false"],
        manager_factory=_factory(mock_manager),
    )

    captured = capsys.readouterr()
    assert exit_code == 3
    assert captured.out == "out"
    assert captured.err == "err"


def test_exec_raw_signal_exit_code(mock_manager):
    mock_manager.exec.return_value = {**EXEC_RESULT, "exit_code": None, "signal": 9}

    exit_code = cli.main(
        ["exec", "--raw", "web-1", "--", "x"], manager_factory=_factory(mock_manager)
    )

    assert exit_code == 137


def test_exec_raw_timeout_exits_124_with_pid(mock_manager, capsys):
    mock_manager.exec.return_value = {
        **EXEC_RESULT,
        "exit_code": None,
        "stdout": "",
        "timed_out": True,
    }

    exit_code = cli.main(
        ["exec", "--raw", "web-1", "--", "sleep", "100"],
        manager_factory=_factory(mock_manager),
    )

    assert exit_code == 124
    assert "pid 42" in capsys.readouterr().err


def test_exec_raw_warns_on_truncation(mock_manager, capsys):
    mock_manager.exec.return_value = {**EXEC_RESULT, "truncated": True}

    cli.main(
        ["exec", "--raw", "web-1", "--", "cat", "big"],
        manager_factory=_factory(mock_manager),
    )

    assert "truncated" in capsys.readouterr().err


def test_exec_stdin_reads_bytes_from_stdin(mock_manager, monkeypatch):
    mock_manager.exec.return_value = EXEC_RESULT
    monkeypatch.setattr(cli.sys, "stdin", io.TextIOWrapper(io.BytesIO(b"\x00payload")))

    cli.main(
        ["exec", "--stdin", "web-1", "--", "cat"],
        manager_factory=_factory(mock_manager),
    )

    assert mock_manager.exec.call_args.kwargs["stdin"] == b"\x00payload"


def test_exec_stdin_reads_at_most_limit_plus_one(monkeypatch):
    stream = io.BytesIO(b"x" * (STDIN_LIMIT_BYTES + 10))
    monkeypatch.setattr(cli.sys, "stdin", io.TextIOWrapper(stream))

    assert len(cli._read_stdin_bytes()) == STDIN_LIMIT_BYTES + 1


@pytest.mark.parametrize(
    ("exc", "code", "label"),
    [
        (GuestAgentUnavailable("web-1: no channel"), 9, "guest agent unavailable"),
        (ServerNotRunning("web-1"), 5, "server not running"),
        (GuestExecError("web-1: no such file"), 1, "guest exec failed"),
    ],
)
def test_exec_normalizes_errors(mock_manager, capsys, exc, code, label):
    mock_manager.exec.side_effect = exc

    exit_code = cli.main(
        ["exec", "--raw", "web-1", "--", "true"],
        manager_factory=_factory(mock_manager),
    )

    assert exit_code == code
    assert f"error: {label}" in capsys.readouterr().err


# --- ssh ---


def test_ssh_execs_ssh_with_endpoint(mock_manager, execvp):
    mock_manager.ssh_endpoint.return_value = ENDPOINT

    cli.main(["ssh", "web-1"], manager_factory=_factory(mock_manager))

    expected = [
        "ssh",
        "-i",
        "/home/alice/.ssh/minivps_ed25519",
        "-p",
        "2203",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "ubuntu@127.0.0.1",
    ]
    execvp.assert_called_once_with("ssh", expected)


def test_ssh_passes_extra_arguments(mock_manager, execvp):
    mock_manager.ssh_endpoint.return_value = ENDPOINT

    cli.main(
        ["ssh", "web-1", "--", "-L", "8080:localhost:80", "uname", "-a"],
        manager_factory=_factory(mock_manager),
    )

    argv = execvp.call_args.args[1]
    assert argv[-4:] == ["-L", "8080:localhost:80", "uname", "-a"]


def test_ssh_print_only_shows_command(mock_manager, execvp, capsys):
    mock_manager.ssh_endpoint.return_value = ENDPOINT

    exit_code = cli.main(
        ["ssh", "--print", "web-1"], manager_factory=_factory(mock_manager)
    )

    assert exit_code == 0
    execvp.assert_not_called()
    out = capsys.readouterr().out.strip()
    assert out.startswith("ssh -i /home/alice/.ssh/minivps_ed25519 -p 2203 ")
    assert out.endswith("ubuntu@127.0.0.1")


def test_ssh_missing_binary_exits_1(mock_manager, execvp, capsys):
    mock_manager.ssh_endpoint.return_value = ENDPOINT
    execvp.side_effect = FileNotFoundError(2, "No such file or directory", "ssh")

    exit_code = cli.main(["ssh", "web-1"], manager_factory=_factory(mock_manager))

    assert exit_code == 1


# --- console ---


def test_console_execs_virsh_console(mock_manager, execvp):
    cli.main(["console", "web-1"], manager_factory=_factory(mock_manager))

    mock_manager.status.assert_called_once_with("web-1")
    execvp.assert_called_once_with(
        "virsh", ["virsh", "-c", "qemu:///system", "console", "web-1"]
    )


def test_console_rejects_unknown_server(mock_manager, execvp):
    mock_manager.status.side_effect = ServerNotFound("nope")

    exit_code = cli.main(["console", "nope"], manager_factory=_factory(mock_manager))

    assert exit_code == 3
    execvp.assert_not_called()


# --- pause / resume ---


def test_pause_prints_json(mock_manager, capsys):
    mock_manager.pause.return_value = {"spec": {}, "status": {"state": "paused"}}

    exit_code = cli.main(["pause", "web-1"], manager_factory=_factory(mock_manager))

    assert exit_code == 0
    mock_manager.pause.assert_called_once_with("web-1")
    assert json.loads(capsys.readouterr().out)["status"]["state"] == "paused"


def test_resume_prints_json(mock_manager, capsys):
    mock_manager.resume.return_value = {"spec": {}, "status": {"state": "running"}}

    exit_code = cli.main(["resume", "web-1"], manager_factory=_factory(mock_manager))

    assert exit_code == 0
    mock_manager.resume.assert_called_once_with("web-1")


def test_pause_not_running_exits_5(mock_manager):
    mock_manager.pause.side_effect = ServerNotRunning("web-1")

    assert cli.main(["pause", "web-1"], manager_factory=_factory(mock_manager)) == 5
