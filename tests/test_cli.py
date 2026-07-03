import contextlib
import json
from unittest.mock import MagicMock

import pytest

from mini_vps import cli
from mini_vps.manager import ServerConflict, ServerNotFound

SPEC_YAML = """\
name: web-1
memory: 1024
vcpus: 2
base_image: ubuntu-noble.img
disk: 10
"""


@pytest.fixture
def mock_manager():
    return MagicMock()


def _factory(mgr):
    """main() の manager_factory に注入する、mgr を素通しするコンテキストマネージャ。"""
    return lambda: contextlib.nullcontext(mgr)


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
    mock_manager.reinstall.assert_called_once_with("web-1")
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
