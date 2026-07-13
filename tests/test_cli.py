import contextlib
import json
from unittest.mock import MagicMock

import pytest

from mini_vps import cli
from mini_vps.lifecycle import FiltersUnsupported
from mini_vps.manager import (
    ServerConflict,
    ServerNotFound,
    ServerNotRunning,
    ServerRunning,
)
from mini_vps.startup_scripts import StartupScriptError

SPEC_YAML = """\
name: web-1
memory: 1024
vcpus: 2
base_image: ubuntu-24.04.img
disk: 10
"""


@pytest.fixture
def mock_manager():
    return MagicMock()


def _factory(mgr):
    """main() の manager_factory に注入する、mgr を素通しするコンテキストマネージャ。"""
    return lambda: contextlib.nullcontext(mgr)


# --- _parse_startup_params ---


def test_parse_startup_params_builds_dict():
    assert cli._parse_startup_params(["A=1", "B=2"]) == {"A": "1", "B": "2"}


def test_parse_startup_params_keeps_first_only_split():
    assert cli._parse_startup_params(["A=1=2=3"]) == {"A": "1=2=3"}


def test_parse_startup_params_rejects_missing_equals():
    with pytest.raises(StartupScriptError):
        cli._parse_startup_params(["NOVALUE"])


# --- list ---


def test_list_prints_names_one_per_line(mock_manager, capsys):
    mock_manager.list.return_value = ["web-1", "web-2"]

    exit_code = cli.main(["list"], manager_factory=_factory(mock_manager))

    assert exit_code == 0
    assert capsys.readouterr().out == "web-1\nweb-2\n"


# --- get ---


def test_get_prints_json(mock_manager, capsys):
    mock_manager.get.return_value = {"spec": {"name": "web-1"}, "status": {}}

    exit_code = cli.main(["get", "web-1"], manager_factory=_factory(mock_manager))

    assert exit_code == 0
    mock_manager.get.assert_called_once_with("web-1")
    assert json.loads(capsys.readouterr().out) == mock_manager.get.return_value


def test_get_returns_exit_code_2_when_not_found(mock_manager, capsys):
    mock_manager.get.side_effect = ServerNotFound("web-1")

    exit_code = cli.main(["get", "web-1"], manager_factory=_factory(mock_manager))

    assert exit_code == 2
    assert "web-1" in capsys.readouterr().err


# --- status ---


def test_status_prints_json(mock_manager, capsys):
    mock_manager.status.return_value = {"state": "running", "ip": "192.0.2.1"}

    exit_code = cli.main(["status", "web-1"], manager_factory=_factory(mock_manager))

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == mock_manager.status.return_value


# --- start ---


def test_start_prints_json(mock_manager, capsys):
    mock_manager.start.return_value = {"spec": {}, "status": {}}

    exit_code = cli.main(["start", "web-1"], manager_factory=_factory(mock_manager))

    assert exit_code == 0
    mock_manager.start.assert_called_once_with("web-1")
    assert json.loads(capsys.readouterr().out) == mock_manager.start.return_value


def test_start_returns_exit_code_2_when_not_found(mock_manager):
    mock_manager.start.side_effect = ServerNotFound("web-1")

    exit_code = cli.main(["start", "web-1"], manager_factory=_factory(mock_manager))

    assert exit_code == 2


# --- stop ---


def test_stop_prints_json(mock_manager, capsys):
    mock_manager.stop.return_value = {"spec": {}, "status": {}}

    exit_code = cli.main(["stop", "web-1"], manager_factory=_factory(mock_manager))

    assert exit_code == 0
    mock_manager.stop.assert_called_once_with("web-1", force=False)
    assert json.loads(capsys.readouterr().out) == mock_manager.stop.return_value


def test_stop_forwards_force_flag(mock_manager):
    mock_manager.stop.return_value = {"spec": {}, "status": {}}

    exit_code = cli.main(
        ["stop", "web-1", "--force"], manager_factory=_factory(mock_manager)
    )

    assert exit_code == 0
    mock_manager.stop.assert_called_once_with("web-1", force=True)


# --- restart ---


def test_restart_prints_json(mock_manager, capsys):
    mock_manager.restart.return_value = {"spec": {}, "status": {}}

    exit_code = cli.main(["restart", "web-1"], manager_factory=_factory(mock_manager))

    assert exit_code == 0
    mock_manager.restart.assert_called_once_with("web-1", force=False)
    assert json.loads(capsys.readouterr().out) == mock_manager.restart.return_value


def test_restart_forwards_force_flag(mock_manager):
    mock_manager.restart.return_value = {"spec": {}, "status": {}}

    exit_code = cli.main(
        ["restart", "web-1", "--force"], manager_factory=_factory(mock_manager)
    )

    assert exit_code == 0
    mock_manager.restart.assert_called_once_with("web-1", force=True)


def test_restart_returns_exit_code_4_when_not_running(mock_manager, capsys):
    mock_manager.restart.side_effect = ServerNotRunning("web-1")

    exit_code = cli.main(["restart", "web-1"], manager_factory=_factory(mock_manager))

    assert exit_code == 4
    assert "web-1" in capsys.readouterr().err


# --- delete ---


def test_delete_calls_manager_and_prints_message(mock_manager, capsys):
    exit_code = cli.main(["delete", "web-1"], manager_factory=_factory(mock_manager))

    assert exit_code == 0
    mock_manager.delete.assert_called_once_with("web-1")
    assert "web-1" in capsys.readouterr().out


def test_delete_returns_exit_code_2_when_not_found(mock_manager):
    mock_manager.delete.side_effect = ServerNotFound("web-1")

    exit_code = cli.main(["delete", "web-1"], manager_factory=_factory(mock_manager))

    assert exit_code == 2


# --- reinstall ---


def test_reinstall_prints_json(mock_manager, capsys):
    mock_manager.reinstall.return_value = {"spec": {}, "status": {}}

    exit_code = cli.main(["reinstall", "web-1"], manager_factory=_factory(mock_manager))

    assert exit_code == 0
    mock_manager.reinstall.assert_called_once_with("web-1", secrets=None)
    assert json.loads(capsys.readouterr().out) == mock_manager.reinstall.return_value


# --- create ---


def test_create_reads_yaml_file_and_calls_manager(mock_manager, tmp_path):
    mock_manager.create.return_value = ({"spec": {}, "status": {}}, True)
    spec_file = tmp_path / "vm.yaml"
    spec_file.write_text(SPEC_YAML)

    exit_code = cli.main(
        ["create", str(spec_file)], manager_factory=_factory(mock_manager)
    )

    assert exit_code == 0
    called_spec = mock_manager.create.call_args[0][0]
    assert called_spec["name"] == "web-1"
    assert called_spec["memory"] == 1024


def test_create_returns_exit_code_3_on_conflict(mock_manager, tmp_path):
    mock_manager.create.side_effect = ServerConflict("web-1")
    spec_file = tmp_path / "vm.yaml"
    spec_file.write_text(SPEC_YAML)

    exit_code = cli.main(
        ["create", str(spec_file)], manager_factory=_factory(mock_manager)
    )

    assert exit_code == 3


def test_create_returns_exit_code_5_when_running(mock_manager, tmp_path):
    """可変フィールド差分の収束は稼働中の VM には適用できない。"""
    mock_manager.create.side_effect = ServerRunning("web-1")
    spec_file = tmp_path / "vm.yaml"
    spec_file.write_text(SPEC_YAML)

    exit_code = cli.main(
        ["create", str(spec_file)], manager_factory=_factory(mock_manager)
    )

    assert exit_code == 5


def test_create_returns_exit_code_6_when_filters_unsupported(mock_manager, tmp_path):
    """OVS 接続ネットワークと filters の併用は終了コード 6 で拒否される。"""
    mock_manager.create.side_effect = FiltersUnsupported(
        "filters cannot be enforced on OVS network: seg1"
    )
    spec_file = tmp_path / "vm.yaml"
    spec_file.write_text(SPEC_YAML)

    exit_code = cli.main(
        ["create", str(spec_file)], manager_factory=_factory(mock_manager)
    )

    assert exit_code == 6


def test_create_returns_exit_code_1_when_file_missing(mock_manager, tmp_path):
    missing = tmp_path / "missing.yaml"

    exit_code = cli.main(
        ["create", str(missing)], manager_factory=_factory(mock_manager)
    )

    assert exit_code == 1


def test_create_returns_exit_code_1_when_spec_invalid(mock_manager, tmp_path):
    spec_file = tmp_path / "vm.yaml"
    spec_file.write_text("name: web-1\n")  # 必須キー(memory 等)が無い

    exit_code = cli.main(
        ["create", str(spec_file)], manager_factory=_factory(mock_manager)
    )

    assert exit_code == 1
    mock_manager.create.assert_not_called()


# --- --startup-param ---


def test_create_forwards_startup_params_as_secrets(mock_manager, tmp_path):
    mock_manager.create.return_value = ({"spec": {}, "status": {}}, True)
    spec_file = tmp_path / "vm.yaml"
    spec_file.write_text(SPEC_YAML)

    exit_code = cli.main(
        [
            "create",
            str(spec_file),
            "--startup-param",
            "AI_ENGINE_TOKEN=sk-abc",
        ],
        manager_factory=_factory(mock_manager),
    )

    assert exit_code == 0
    called_secrets = mock_manager.create.call_args.kwargs["secrets"]
    assert called_secrets == {"AI_ENGINE_TOKEN": "sk-abc"}


def test_create_keeps_equals_sign_in_startup_param_value(mock_manager, tmp_path):
    mock_manager.create.return_value = ({"spec": {}, "status": {}}, True)
    spec_file = tmp_path / "vm.yaml"
    spec_file.write_text(SPEC_YAML)

    cli.main(
        ["create", str(spec_file), "--startup-param", "AI_ENGINE_TOKEN=sk=a=b"],
        manager_factory=_factory(mock_manager),
    )

    called_secrets = mock_manager.create.call_args.kwargs["secrets"]
    assert called_secrets == {"AI_ENGINE_TOKEN": "sk=a=b"}


def test_create_returns_exit_code_1_on_malformed_startup_param(mock_manager, tmp_path):
    spec_file = tmp_path / "vm.yaml"
    spec_file.write_text(SPEC_YAML)

    exit_code = cli.main(
        ["create", str(spec_file), "--startup-param", "no-equals-sign"],
        manager_factory=_factory(mock_manager),
    )

    assert exit_code == 1
    mock_manager.create.assert_not_called()


def test_reinstall_forwards_startup_params_as_secrets(mock_manager):
    mock_manager.reinstall.return_value = {"spec": {}, "status": {}}

    exit_code = cli.main(
        ["reinstall", "web-1", "--startup-param", "AI_ENGINE_TOKEN=sk-abc"],
        manager_factory=_factory(mock_manager),
    )

    assert exit_code == 0
    mock_manager.reinstall.assert_called_once_with(
        "web-1", secrets={"AI_ENGINE_TOKEN": "sk-abc"}
    )
