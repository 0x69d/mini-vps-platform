from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from mini_vps.config import POOL_NAME, SEED_DIR
from mini_vps.resources import (
    _filter_name,
    build_domain_xml,
    build_nwfilter_xml,
    build_seed_iso,
    create_overlay_volume,
    ensure_pool,
)
from mini_vps.startup_scripts import StartupScriptError


def _spec(**overrides):
    spec = {
        "name": "web-1",
        "hostname": "web-1",
        "user": "ubuntu",
        "memory": 1024,
        "vcpus": 2,
        "base_image": "ubuntu-24.04.img",
        "disk": 10,
        "network": "default",
    }
    spec.update(overrides)
    return spec


# --- _filter_name ---


def test_filter_name_is_deterministic():
    assert _filter_name({"name": "web-1"}) == "minivps-web-1"


# --- build_nwfilter_xml ---


def test_build_nwfilter_xml_expands_each_rule():
    spec = _spec(
        filters=[{"port": 22, "protocol": "tcp"}, {"port": 53, "protocol": "udp"}]
    )
    xml = build_nwfilter_xml(spec)

    assert "filter name='minivps-web-1'" in xml
    assert "<tcp dstportstart='22'/>" in xml
    assert "<udp dstportstart='53'/>" in xml
    # ステートフルな戻り通信の許可と default drop が常に入る
    assert "ESTABLISHED,RELATED" in xml
    assert "action='drop'" in xml


def test_build_nwfilter_xml_with_empty_filters_has_no_port_rules():
    xml = build_nwfilter_xml(_spec(filters=[]))
    assert "dstportstart" not in xml
    # 空でも filter 骨格と default drop は成立する
    assert "filter name='minivps-web-1'" in xml
    assert "action='drop'" in xml


# --- build_domain_xml ---


def test_build_domain_xml_converts_memory_to_kib():
    xml = build_domain_xml(_spec(), "/overlay.qcow2", "/seed.iso")
    assert "<memory unit='KiB'>1048576</memory>" in xml


def test_build_domain_xml_embeds_paths_and_fields():
    xml = build_domain_xml(_spec(), "/lab/web-1.qcow2", "/lab/web-1-seed.iso")
    assert "<name>web-1</name>" in xml
    assert "<vcpu>2</vcpu>" in xml
    assert "source file='/lab/web-1.qcow2'" in xml
    assert "source file='/lab/web-1-seed.iso'" in xml
    assert "source network='default'" in xml


def test_build_domain_xml_without_filter_omits_filterref():
    xml = build_domain_xml(_spec(), "/overlay.qcow2", "/seed.iso")
    assert "filterref" not in xml


def test_build_domain_xml_with_filter_adds_filterref():
    xml = build_domain_xml(
        _spec(), "/overlay.qcow2", "/seed.iso", filter_name="minivps-web-1"
    )
    assert "<filterref filter='minivps-web-1'/>" in xml


def test_build_domain_xml_passes_through_host_cpu_features():
    xml = build_domain_xml(_spec(), "/overlay.qcow2", "/seed.iso")
    assert "<cpu mode='host-model'/>" in xml


def test_build_domain_xml_uses_uefi_firmware_and_q35_machine():
    xml = build_domain_xml(_spec(), "/overlay.qcow2", "/seed.iso")
    assert "<os firmware='efi'>" in xml
    assert "<loader secure='no'/>" in xml
    assert "machine='q35'" in xml


def test_build_domain_xml_includes_rng_clock_pm_and_discard():
    xml = build_domain_xml(_spec(), "/overlay.qcow2", "/seed.iso")
    assert "<rng model='virtio'>" in xml
    assert "<clock offset='utc'/>" in xml
    assert "<suspend-to-mem enabled='no'/>" in xml
    assert "<suspend-to-disk enabled='no'/>" in xml
    assert "discard='unmap'" in xml


def test_build_domain_xml_defaults_network_when_absent():
    spec = _spec()
    del spec["network"]
    xml = build_domain_xml(spec, "/overlay.qcow2", "/seed.iso")
    assert "source network='default'" in xml


# --- ensure_pool (Mock) ---


def test_ensure_pool_returns_existing_active_pool_without_starting():
    conn = MagicMock()
    existing = MagicMock()
    existing.name.return_value = POOL_NAME
    conn.listAllStoragePools.return_value = [existing]
    pool = conn.storagePoolLookupByName.return_value
    pool.isActive.return_value = True

    result = ensure_pool(conn, POOL_NAME)

    assert result is pool
    pool.create.assert_not_called()


def test_ensure_pool_starts_existing_inactive_pool():
    conn = MagicMock()
    existing = MagicMock()
    existing.name.return_value = POOL_NAME
    conn.listAllStoragePools.return_value = [existing]
    pool = conn.storagePoolLookupByName.return_value
    pool.isActive.return_value = False

    ensure_pool(conn, POOL_NAME)

    pool.create.assert_called_once_with(0)


def test_ensure_pool_defines_new_pool_when_absent():
    conn = MagicMock()
    conn.listAllStoragePools.return_value = []
    pool = conn.storagePoolDefineXML.return_value

    result = ensure_pool(conn, POOL_NAME)

    assert result is pool
    pool.build.assert_called_once_with(0)
    pool.create.assert_called_once_with(0)
    pool.setAutostart.assert_called_once_with(1)


# --- create_overlay_volume (Mock) ---


def test_create_overlay_volume_deletes_existing_before_recreate(monkeypatch):
    conn = MagicMock()
    base_pool = conn.storagePoolLookupByName.return_value
    base_pool.storageVolLookupByName.return_value.path.return_value = "/images/base.img"
    pool = MagicMock()
    monkeypatch.setattr("mini_vps.resources.ensure_pool", lambda c, n: pool)
    existing_vol = MagicMock()
    existing_vol.name.return_value = "web-1.qcow2"
    pool.listAllVolumes.return_value = [existing_vol]
    pool.createXML.return_value.path.return_value = "/vps-pool/web-1.qcow2"

    result = create_overlay_volume(conn, _spec())

    pool.storageVolLookupByName.assert_called_once_with("web-1.qcow2")
    pool.storageVolLookupByName.return_value.delete.assert_called_once_with(0)
    assert result == "/vps-pool/web-1.qcow2"


def test_create_overlay_volume_skips_delete_when_absent(monkeypatch):
    conn = MagicMock()
    base_pool = conn.storagePoolLookupByName.return_value
    base_pool.storageVolLookupByName.return_value.path.return_value = "/images/base.img"
    pool = MagicMock()
    monkeypatch.setattr("mini_vps.resources.ensure_pool", lambda c, n: pool)
    pool.listAllVolumes.return_value = []

    create_overlay_volume(conn, _spec())

    pool.storageVolLookupByName.assert_not_called()
    pool.createXML.assert_called_once()


# --- build_seed_iso (Mock) ---


def test_build_seed_iso_writes_expected_cloud_init_content(monkeypatch):
    captured = {}

    def fake_run(cmd, check):
        # subprocess.run はまだ一時ディレクトリが存在するタイミングで呼ばれるため、
        # ここで読まないと with ブロックを抜けた時点でファイルごと削除されてしまう。
        captured["user_data"] = Path(cmd[2]).read_text()
        captured["meta_data"] = Path(cmd[3]).read_text()
        captured["cmd"] = cmd

    monkeypatch.setattr("mini_vps.resources.subprocess.run", fake_run)
    monkeypatch.setattr("mini_vps.resources.os.path.exists", lambda p: False)

    spec = _spec(name="web-1", hostname="web-1", user="ubuntu")
    seed_path = build_seed_iso(spec, "ssh-ed25519 AAAA...")

    assert captured["cmd"][0] == "cloud-localds"
    assert "ssh-ed25519 AAAA..." in captured["user_data"]
    assert "web-1" in captured["meta_data"]
    assert seed_path == f"{SEED_DIR}/web-1-seed.iso"


def test_build_seed_iso_deletes_existing_seed_before_recreate(monkeypatch):
    monkeypatch.setattr("mini_vps.resources.os.path.exists", lambda p: True)
    remove_mock = MagicMock()
    monkeypatch.setattr("mini_vps.resources.os.remove", remove_mock)
    monkeypatch.setattr("mini_vps.resources.subprocess.run", MagicMock())

    seed_path = build_seed_iso(_spec(name="web-1"), "ssh-ed25519 AAAA...")

    remove_mock.assert_called_once_with(seed_path)


def test_build_seed_iso_skips_delete_when_seed_absent(monkeypatch):
    monkeypatch.setattr("mini_vps.resources.os.path.exists", lambda p: False)
    remove_mock = MagicMock()
    monkeypatch.setattr("mini_vps.resources.os.remove", remove_mock)
    monkeypatch.setattr("mini_vps.resources.subprocess.run", MagicMock())

    build_seed_iso(_spec(name="web-1"), "ssh-ed25519 AAAA...")

    remove_mock.assert_not_called()


def test_build_seed_iso_omits_write_files_when_no_startup_script(monkeypatch):
    captured = {}

    def fake_run(cmd, check):
        captured["user_data"] = Path(cmd[2]).read_text()

    monkeypatch.setattr("mini_vps.resources.subprocess.run", fake_run)
    monkeypatch.setattr("mini_vps.resources.os.path.exists", lambda p: False)

    build_seed_iso(_spec(name="web-1"), "ssh-ed25519 AAAA...")

    parsed = yaml.safe_load(captured["user_data"])
    assert "write_files" not in parsed
    assert "runcmd" not in parsed


def test_build_seed_iso_includes_write_files_and_runcmd_when_startup_script_set(
    monkeypatch,
):
    captured = {}

    def fake_run(cmd, check):
        captured["user_data"] = Path(cmd[2]).read_text()

    monkeypatch.setattr("mini_vps.resources.subprocess.run", fake_run)
    monkeypatch.setattr("mini_vps.resources.os.path.exists", lambda p: False)

    spec = _spec(name="web-1", startup_script="opencode-sakura-ai-engine")
    build_seed_iso(spec, "ssh-ed25519 AAAA...", secrets={"AI_ENGINE_TOKEN": "sk-abc"})

    parsed = yaml.safe_load(captured["user_data"])
    assert "write_files" in parsed
    assert "runcmd" in parsed
    assert "sk-abc" in captured["user_data"]


def test_build_seed_iso_propagates_missing_secret_error_before_cloud_localds(
    monkeypatch,
):
    run_mock = MagicMock()
    monkeypatch.setattr("mini_vps.resources.subprocess.run", run_mock)
    monkeypatch.setattr("mini_vps.resources.os.path.exists", lambda p: False)

    spec = _spec(name="web-1", startup_script="opencode-sakura-ai-engine")

    with pytest.raises(StartupScriptError):
        build_seed_iso(spec, "ssh-ed25519 AAAA...", secrets=None)

    # secrets 不足を検知した時点で失敗するため、cloud-localds は一切呼ばれない
    run_mock.assert_not_called()
