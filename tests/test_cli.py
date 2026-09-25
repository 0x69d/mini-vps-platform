import contextlib
import json
import logging
from unittest.mock import MagicMock

import libvirt
import pytest

from mini_vps import cli
from mini_vps.manager import (
    InsufficientCapacity,
    PlatformUnsupported,
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


# --- ログ設定 ---


@pytest.fixture
def captured_log_level(monkeypatch):
    """cli が configure_logging に渡したレベルを記録する。"""
    captured = {}
    monkeypatch.setattr(
        cli, "configure_logging", lambda level: captured.update(level=level)
    )
    return captured


def test_no_verbose_flag_leaves_level_to_environment(mock_manager, captured_log_level):
    """-v 無しでは明示レベルを渡さず、環境変数と既定値の判断に委ねる。"""
    mock_manager.list.return_value = []

    cli.main(["list"], manager_factory=_factory(mock_manager))

    assert captured_log_level["level"] is None


def test_single_verbose_selects_info(mock_manager, captured_log_level):
    mock_manager.list.return_value = []

    cli.main(["-v", "list"], manager_factory=_factory(mock_manager))

    assert captured_log_level["level"] == "INFO"


def test_double_verbose_selects_debug(mock_manager, captured_log_level):
    mock_manager.list.return_value = []

    cli.main(["-vv", "list"], manager_factory=_factory(mock_manager))

    assert captured_log_level["level"] == "DEBUG"


def test_logs_go_to_stderr_not_stdout(mock_manager, capsys):
    """-v を付けても stdout はコマンド結果だけを保ち、ログは stderr へ流す。"""

    def _list():
        logging.getLogger("mini_vps.manager").info("診断メッセージ")
        return ["web-1"]

    mock_manager.list.side_effect = _list

    logger = logging.getLogger("mini_vps")
    saved_handlers, saved_level = list(logger.handlers), logger.level
    logger.handlers[:] = []
    try:
        exit_code = cli.main(["-v", "list"], manager_factory=_factory(mock_manager))
    finally:
        logger.handlers[:] = saved_handlers
        logger.setLevel(saved_level)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == "web-1\n"
    assert "診断メッセージ" in captured.err


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


def test_get_returns_exit_code_3_when_not_found(mock_manager, capsys):
    mock_manager.get.side_effect = ServerNotFound("web-1")

    exit_code = cli.main(["get", "web-1"], manager_factory=_factory(mock_manager))

    assert exit_code == 3
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


def test_start_returns_exit_code_3_when_not_found(mock_manager):
    mock_manager.start.side_effect = ServerNotFound("web-1")

    exit_code = cli.main(["start", "web-1"], manager_factory=_factory(mock_manager))

    assert exit_code == 3


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


def test_restart_returns_exit_code_5_when_not_running(mock_manager, capsys):
    mock_manager.restart.side_effect = ServerNotRunning("web-1")

    exit_code = cli.main(["restart", "web-1"], manager_factory=_factory(mock_manager))

    assert exit_code == 5
    assert "web-1" in capsys.readouterr().err


# --- delete ---


def test_delete_calls_manager_and_prints_message(mock_manager, capsys):
    exit_code = cli.main(["delete", "web-1"], manager_factory=_factory(mock_manager))

    assert exit_code == 0
    mock_manager.delete.assert_called_once_with("web-1")
    assert "web-1" in capsys.readouterr().out


def test_delete_returns_exit_code_3_when_not_found(mock_manager):
    mock_manager.delete.side_effect = ServerNotFound("web-1")

    exit_code = cli.main(["delete", "web-1"], manager_factory=_factory(mock_manager))

    assert exit_code == 3


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


def test_create_returns_exit_code_4_on_conflict(mock_manager, tmp_path):
    mock_manager.create.side_effect = ServerConflict("web-1")
    spec_file = tmp_path / "vm.yaml"
    spec_file.write_text(SPEC_YAML)

    exit_code = cli.main(
        ["create", str(spec_file)], manager_factory=_factory(mock_manager)
    )

    assert exit_code == 4


def test_create_returns_exit_code_6_when_running(mock_manager, tmp_path):
    """可変フィールド差分の収束は稼働中の VM には適用できない。"""
    mock_manager.create.side_effect = ServerRunning("web-1")
    spec_file = tmp_path / "vm.yaml"
    spec_file.write_text(SPEC_YAML)

    exit_code = cli.main(
        ["create", str(spec_file)], manager_factory=_factory(mock_manager)
    )

    assert exit_code == 6


def test_create_returns_exit_code_8_when_platform_unsupported(
    mock_manager, tmp_path, capsys
):
    mock_manager.create.side_effect = PlatformUnsupported("web-1: filters")
    spec_file = tmp_path / "vm.yaml"
    spec_file.write_text(SPEC_YAML)

    exit_code = cli.main(
        ["create", str(spec_file)], manager_factory=_factory(mock_manager)
    )

    assert exit_code == 8
    assert "error: platform unsupported: web-1: filters" in capsys.readouterr().err


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


# --- 終了コード ---


def test_libvirt_error_returns_exit_code_7_with_message(mock_manager, capsys):
    """操作中の libvirtError は traceback ではなく error 行と専用コードにする。"""
    mock_manager.list.side_effect = libvirt.libvirtError("internal error")

    exit_code = cli.main(["list"], manager_factory=_factory(mock_manager))

    assert exit_code == 7
    assert "internal error" in capsys.readouterr().err


def test_libvirt_error_from_connection_returns_exit_code_7(monkeypatch, capsys):
    """libvirtd 停止時の接続失敗も 7 にする(既定 factory 経由)。

    接続の確立は _run_command の try の中でしか行われない。ここを try の外へ
    出すリファクタは traceback を復活させるため、既定 factory で回帰を止める。
    """
    monkeypatch.setattr(cli, "register_quiet_error_handler", lambda: None)
    monkeypatch.setattr(
        cli.libvirt,
        "open",
        MagicMock(side_effect=libvirt.libvirtError("failed to connect")),
    )

    exit_code = cli.main(["list"])

    assert exit_code == 7
    assert "failed to connect" in capsys.readouterr().err


def test_usage_error_keeps_exit_code_2_reserved_for_typer(mock_manager):
    """2 は Click の UsageError 専用。ドメイン例外と衝突しないことを守る。"""
    exit_code = cli.main(
        ["get", "web-1", "--no-such-flag"], manager_factory=_factory(mock_manager)
    )

    assert exit_code == 2


# --- image list / doctor / gc ---


def test_image_list_prints_table(mock_manager, capsys):
    mock_manager.images.return_value = [
        {
            "name": "ubuntu-24.04.img",
            "virtual_bytes": 3758096384,
            "actual_bytes": 625262592,
            "format": "qcow2",
            "used_by": ["web-1", "web-2"],
        },
        {
            "name": "old.img",
            "virtual_bytes": 1024,
            "actual_bytes": 512,
            "format": None,
            "used_by": [],
        },
    ]

    exit_code = cli.main(["image", "list"], manager_factory=_factory(mock_manager))

    assert exit_code == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["NAME", "VIRTUAL", "ACTUAL", "FORMAT", "USED_BY"]
    assert lines[1].split() == [
        "ubuntu-24.04.img",
        "3.5GiB",
        "596.3MiB",
        "qcow2",
        "web-1,web-2",
    ]
    assert lines[2].split() == ["old.img", "1.0KiB", "512B", "-", "-"]


def test_image_list_prints_nothing_without_images(mock_manager, capsys):
    mock_manager.images.return_value = []

    exit_code = cli.main(["image", "list"], manager_factory=_factory(mock_manager))

    assert exit_code == 0
    assert capsys.readouterr().out == ""


def test_doctor_exits_zero_with_warnings_only(mock_manager, capsys):
    mock_manager.doctor.return_value = [
        {"level": "ok", "check": "lock_dir", "detail": "書き込める"},
        {"level": "warn", "check": "accelerator", "detail": "tcg"},
    ]

    exit_code = cli.main(["doctor"], manager_factory=_factory(mock_manager))

    assert exit_code == 0
    assert capsys.readouterr().out.splitlines() == [
        "ok     lock_dir: 書き込める",
        "warn   accelerator: tcg",
    ]


def test_doctor_exits_nonzero_on_error(mock_manager, capsys):
    mock_manager.doctor.return_value = [
        {"level": "error", "check": "network:seg1", "detail": "存在しない"}
    ]

    exit_code = cli.main(["doctor"], manager_factory=_factory(mock_manager))

    assert exit_code == 1
    assert "error  network:seg1: 存在しない" in capsys.readouterr().out


def test_gc_defaults_to_dry_run(mock_manager, capsys):
    mock_manager.gc.return_value = {
        "applied": False,
        "orphans": [
            {"kind": "overlay", "vm": "gone", "name": "gone.qcow2", "pool": "vps-pool"},
            {"kind": "nwfilter", "vm": "gone", "name": "minivps-gone", "pool": None},
        ],
        "removed": [],
        "skipped": [],
    }

    exit_code = cli.main(["gc"], manager_factory=_factory(mock_manager))

    assert exit_code == 0
    mock_manager.gc.assert_called_once_with(apply=False)
    assert capsys.readouterr().out.splitlines() == [
        "would remove: vps-pool/gone.qcow2",
        "would remove: nwfilter/minivps-gone",
    ]


def test_gc_apply_prints_removed_and_skipped(mock_manager, capsys):
    orphan = {"kind": "seed", "vm": "a", "name": "a-seed.iso", "pool": "vps-seeds"}
    mock_manager.gc.return_value = {
        "applied": True,
        "orphans": [orphan, dict(orphan, vm="b", name="b-seed.iso")],
        "removed": [orphan],
        "skipped": [dict(orphan, vm="b", name="b-seed.iso", reason="既に無い")],
    }

    exit_code = cli.main(["gc", "--apply"], manager_factory=_factory(mock_manager))

    assert exit_code == 0
    mock_manager.gc.assert_called_once_with(apply=True)
    assert capsys.readouterr().out.splitlines() == [
        "removed: vps-seeds/a-seed.iso",
        "skipped: vps-seeds/b-seed.iso (既に無い)",
    ]


def test_gc_reports_no_orphans(mock_manager, capsys):
    mock_manager.gc.return_value = {
        "applied": False,
        "orphans": [],
        "removed": [],
        "skipped": [],
    }

    cli.main(["gc"], manager_factory=_factory(mock_manager))

    assert capsys.readouterr().out.strip() == "no orphans"


def test_create_returns_exit_code_11_when_capacity_is_insufficient(
    mock_manager, tmp_path, capsys
):
    mock_manager.create.side_effect = InsufficientCapacity("web-1: vCPU が多すぎる")
    spec_file = tmp_path / "vm.yaml"
    spec_file.write_text(SPEC_YAML)

    exit_code = cli.main(
        ["create", str(spec_file)], manager_factory=_factory(mock_manager)
    )

    assert exit_code == 11
    assert (
        "error: insufficient capacity: web-1: vCPU が多すぎる"
        in capsys.readouterr().err
    )
