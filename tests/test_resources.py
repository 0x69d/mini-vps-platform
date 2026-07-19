import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from mini_vps.config import POOL_NAME, POOL_XML
from mini_vps.resources import (
    _build_network_config,
    _build_static_routes_fragment,
    _filter_name,
    _has_static_network,
    _mac_for_interface,
    _network_name,
    build_domain_xml,
    build_nwfilter_xml,
    build_seed_iso,
    create_overlay_volume,
    ensure_pool,
    resize_domain_xml,
    set_domain_filterref_xml,
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
        "networks": ["default"],
        "static_routes": [],
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


# --- _build_static_routes_fragment ---


def test_build_static_routes_fragment_writes_systemd_unit():
    spec = _spec(
        static_routes=[
            {"destination": "192.168.202.0/24", "via": "192.168.201.1"},
            {"destination": "192.168.203.0/24", "via": "192.168.201.1"},
        ]
    )
    fragment = _build_static_routes_fragment(spec)

    assert len(fragment["write_files"]) == 1
    unit_content = fragment["write_files"][0]["content"]
    assert (
        fragment["write_files"][0]["path"]
        == "/etc/systemd/system/minivps-static-routes.service"
    )
    assert "ExecStart=-ip route replace 192.168.202.0/24 via 192.168.201.1" in (
        unit_content
    )
    assert "ExecStart=-ip route replace 192.168.203.0/24 via 192.168.201.1" in (
        unit_content
    )
    assert "After=network-online.target" in unit_content


def test_build_static_routes_fragment_enables_unit_via_runcmd():
    spec = _spec(
        static_routes=[{"destination": "192.168.202.0/24", "via": "192.168.201.1"}]
    )
    fragment = _build_static_routes_fragment(spec)

    assert fragment["runcmd"] == [
        "systemctl daemon-reload",
        "systemctl enable --now minivps-static-routes.service",
    ]


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


def test_build_domain_xml_generates_one_interface_per_network():
    xml = build_domain_xml(
        _spec(networks=["seg1", "seg2"]), "/overlay.qcow2", "/seed.iso"
    )
    assert xml.count("<interface") == 2
    assert "source network='seg1'" in xml
    assert "source network='seg2'" in xml


def test_build_domain_xml_multi_nic_output_is_well_formed_xml():
    # {interfaces} の連結が壊れていないこと(count だけでは検知できない
    # インデント崩れ・タグの閉じ忘れ等)を ElementTree のパース成否で確認する。
    xml = build_domain_xml(
        _spec(networks=["seg1", "seg2"]),
        "/overlay.qcow2",
        "/seed.iso",
        filter_name="minivps-web-1",
    )
    root = ET.fromstring(xml)
    interfaces = root.findall("devices/interface")
    assert len(interfaces) == 2
    assert [i.find("source").get("network") for i in interfaces] == ["seg1", "seg2"]
    assert all(i.find("filterref").get("filter") == "minivps-web-1" for i in interfaces)


def test_build_domain_xml_applies_filter_to_all_interfaces():
    xml = build_domain_xml(
        _spec(networks=["seg1", "seg2"]),
        "/overlay.qcow2",
        "/seed.iso",
        filter_name="minivps-web-1",
    )
    assert xml.count("<filterref filter='minivps-web-1'/>") == 2


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


def test_build_domain_xml_requires_networks_key():
    spec = _spec()
    del spec["networks"]
    with pytest.raises(KeyError):
        build_domain_xml(spec, "/overlay.qcow2", "/seed.iso")


def test_build_domain_xml_embeds_deterministic_mac_per_interface():
    xml = build_domain_xml(
        _spec(networks=["seg1", "seg2"]), "/overlay.qcow2", "/seed.iso"
    )
    mac0 = _mac_for_interface("web-1", 0)
    mac1 = _mac_for_interface("web-1", 1)
    assert mac0 != mac1
    assert f"<mac address='{mac0}'/>" in xml
    assert f"<mac address='{mac1}'/>" in xml


# --- _mac_for_interface / _network_name / _has_static_network ---


def test_mac_for_interface_is_deterministic():
    assert _mac_for_interface("web-1", 0) == _mac_for_interface("web-1", 0)


def test_mac_for_interface_differs_by_name_and_index():
    assert _mac_for_interface("web-1", 0) != _mac_for_interface("web-1", 1)
    assert _mac_for_interface("web-1", 0) != _mac_for_interface("web-2", 0)


def test_mac_for_interface_uses_locally_administered_prefix():
    assert _mac_for_interface("web-1", 0).startswith("52:54:00:")


def test_network_name_extracts_from_string():
    assert _network_name("seg1") == "seg1"


def test_network_name_extracts_from_attachment_dict():
    assert _network_name({"name": "seg1", "address": "192.168.201.10/24"}) == "seg1"


def test_has_static_network_false_for_all_dhcp():
    assert _has_static_network(_spec(networks=["default", "seg1"])) is False


def test_has_static_network_true_when_any_attachment_present():
    spec = _spec(networks=["default", {"name": "seg1", "address": "192.168.201.10/24"}])
    assert _has_static_network(spec) is True


# --- _build_network_config ---


def test_build_network_config_lists_dhcp_and_static_by_mac():
    spec = _spec(
        name="web-1",
        networks=["default", {"name": "seg1", "address": "192.168.201.10/24"}],
    )
    config = _build_network_config(spec)

    ethernets = config["network"]["ethernets"]
    assert config["network"]["version"] == 2
    assert len(ethernets) == 2
    dhcp_entry = ethernets["eth0"]
    static_entry = ethernets["eth1"]
    assert dhcp_entry == {
        "match": {"macaddress": _mac_for_interface("web-1", 0)},
        "dhcp4": True,
    }
    assert static_entry["match"] == {"macaddress": _mac_for_interface("web-1", 1)}
    assert static_entry["addresses"] == ["192.168.201.10/24"]


def test_build_network_config_uses_routes_not_gateway4():
    spec = _spec(
        name="web-1",
        networks=[
            {
                "name": "seg1",
                "address": "192.168.201.10/24",
                "gateway": "192.168.201.1",
            }
        ],
    )
    config = _build_network_config(spec)
    entry = config["network"]["ethernets"]["eth0"]
    assert entry["routes"] == [{"to": "default", "via": "192.168.201.1"}]
    assert "gateway4" not in entry


def test_build_network_config_omits_routes_when_gateway_absent():
    spec = _spec(
        name="web-1", networks=[{"name": "seg1", "address": "192.168.201.10/24"}]
    )
    config = _build_network_config(spec)
    entry = config["network"]["ethernets"]["eth0"]
    assert "routes" not in entry


def test_build_network_config_includes_nameservers_when_set():
    spec = _spec(
        name="web-1",
        networks=[
            {
                "name": "seg1",
                "address": "192.168.201.10/24",
                "nameservers": ["192.168.203.30"],
                "search": ["minivps.internal"],
            }
        ],
    )
    config = _build_network_config(spec)
    entry = config["network"]["ethernets"]["eth0"]
    assert entry["nameservers"] == {
        "addresses": ["192.168.203.30"],
        "search": ["minivps.internal"],
    }


def test_build_network_config_omits_nameservers_when_empty():
    spec = _spec(
        name="web-1",
        networks=[
            {
                "name": "seg1",
                "address": "192.168.201.10/24",
                "nameservers": [],
                "search": [],
            }
        ],
    )
    config = _build_network_config(spec)
    entry = config["network"]["ethernets"]["eth0"]
    assert "nameservers" not in entry


def test_build_network_config_includes_search_without_addresses():
    spec = _spec(
        name="web-1",
        networks=[
            {
                "name": "seg1",
                "address": "192.168.201.10/24",
                "nameservers": [],
                "search": ["minivps.internal"],
            }
        ],
    )
    config = _build_network_config(spec)
    entry = config["network"]["ethernets"]["eth0"]
    assert entry["nameservers"] == {"search": ["minivps.internal"]}


# --- resize_domain_xml ---

# dom.XMLDesc(VIR_DOMAIN_XML_INACTIVE) が返す実定義を模したフィクスチャ。
# uuid/mac は resize 前後で不変であることを検証する基準値。
_INACTIVE_DOMAIN_XML_WITH_CURRENT_MEMORY = """
<domain type='kvm'>
  <name>web-1</name>
  <uuid>4dc9c6c3-36ce-41b8-a33f-5421eb4e58a4</uuid>
  <memory unit='KiB'>1048576</memory>
  <currentMemory unit='KiB'>1048576</currentMemory>
  <vcpu placement='static'>2</vcpu>
  <devices>
    <interface type='network'>
      <mac address='52:54:00:12:34:56'/>
      <source network='default'/>
    </interface>
  </devices>
</domain>
"""

_INACTIVE_DOMAIN_XML_WITHOUT_CURRENT_MEMORY = """
<domain type='kvm'>
  <name>web-1</name>
  <uuid>4dc9c6c3-36ce-41b8-a33f-5421eb4e58a4</uuid>
  <memory unit='KiB'>1048576</memory>
  <vcpu>2</vcpu>
  <devices>
    <interface type='network'>
      <mac address='52:54:00:12:34:56'/>
    </interface>
  </devices>
</domain>
"""


def test_resize_domain_xml_updates_memory_and_vcpu():
    xml = resize_domain_xml(
        _INACTIVE_DOMAIN_XML_WITH_CURRENT_MEMORY, memory_kib=2097152, vcpus=4
    )
    assert '<memory unit="KiB">2097152</memory>' in xml
    assert '<currentMemory unit="KiB">2097152</currentMemory>' in xml
    # 既存属性(placement)は書き換え対象外なので保持される
    assert '<vcpu placement="static">4</vcpu>' in xml


def test_resize_domain_xml_adds_missing_current_memory():
    xml = resize_domain_xml(
        _INACTIVE_DOMAIN_XML_WITHOUT_CURRENT_MEMORY, memory_kib=2097152, vcpus=2
    )
    assert '<currentMemory unit="KiB">2097152</currentMemory>' in xml
    assert '<memory unit="KiB">2097152</memory>' in xml


def test_resize_domain_xml_preserves_uuid_and_mac():
    xml = resize_domain_xml(
        _INACTIVE_DOMAIN_XML_WITH_CURRENT_MEMORY, memory_kib=2097152, vcpus=4
    )
    assert "<uuid>4dc9c6c3-36ce-41b8-a33f-5421eb4e58a4</uuid>" in xml
    assert '<mac address="52:54:00:12:34:56" />' in xml


# --- set_domain_filterref_xml ---

# resize_domain_xml と同じ、dom.XMLDesc(VIR_DOMAIN_XML_INACTIVE) を模したフィクスチャ。
_INACTIVE_DOMAIN_XML_WITHOUT_FILTERREF = _INACTIVE_DOMAIN_XML_WITH_CURRENT_MEMORY

_INACTIVE_DOMAIN_XML_WITH_FILTERREF = """
<domain type='kvm'>
  <name>web-1</name>
  <uuid>4dc9c6c3-36ce-41b8-a33f-5421eb4e58a4</uuid>
  <memory unit='KiB'>1048576</memory>
  <currentMemory unit='KiB'>1048576</currentMemory>
  <vcpu placement='static'>2</vcpu>
  <devices>
    <interface type='network'>
      <mac address='52:54:00:12:34:56'/>
      <source network='default'/>
      <filterref filter='minivps-web-1'/>
    </interface>
  </devices>
</domain>
"""


def test_set_domain_filterref_xml_adds_when_absent():
    xml = set_domain_filterref_xml(
        _INACTIVE_DOMAIN_XML_WITHOUT_FILTERREF, "minivps-web-1"
    )
    assert '<filterref filter="minivps-web-1" />' in xml


def test_set_domain_filterref_xml_removes_when_present():
    xml = set_domain_filterref_xml(_INACTIVE_DOMAIN_XML_WITH_FILTERREF, None)
    assert "filterref" not in xml


def test_set_domain_filterref_xml_replaces_existing_name():
    xml = set_domain_filterref_xml(
        _INACTIVE_DOMAIN_XML_WITH_FILTERREF, "minivps-web-1-v2"
    )
    assert xml.count("<filterref") == 1
    assert '<filterref filter="minivps-web-1-v2" />' in xml


def test_set_domain_filterref_xml_is_noop_when_absent_and_none():
    xml = set_domain_filterref_xml(_INACTIVE_DOMAIN_XML_WITHOUT_FILTERREF, None)
    assert "filterref" not in xml


def test_set_domain_filterref_xml_preserves_uuid_and_mac():
    xml = set_domain_filterref_xml(_INACTIVE_DOMAIN_XML_WITH_FILTERREF, None)
    assert "<uuid>4dc9c6c3-36ce-41b8-a33f-5421eb4e58a4</uuid>" in xml
    assert '<mac address="52:54:00:12:34:56" />' in xml


# --- set_domain_filterref_xml(複数 interface) ---

_INACTIVE_DOMAIN_XML_WITH_TWO_INTERFACES = """
<domain type='kvm'>
  <name>web-1</name>
  <uuid>4dc9c6c3-36ce-41b8-a33f-5421eb4e58a4</uuid>
  <memory unit='KiB'>1048576</memory>
  <currentMemory unit='KiB'>1048576</currentMemory>
  <vcpu placement='static'>2</vcpu>
  <devices>
    <interface type='network'>
      <mac address='52:54:00:12:34:56'/>
      <source network='seg1'/>
    </interface>
    <interface type='network'>
      <mac address='52:54:00:12:34:57'/>
      <source network='seg2'/>
    </interface>
  </devices>
</domain>
"""


def test_set_domain_filterref_xml_adds_to_all_interfaces():
    xml = set_domain_filterref_xml(
        _INACTIVE_DOMAIN_XML_WITH_TWO_INTERFACES, "minivps-web-1"
    )
    assert xml.count("<filterref") == 2


def test_set_domain_filterref_xml_removes_from_all_interfaces():
    with_filters = set_domain_filterref_xml(
        _INACTIVE_DOMAIN_XML_WITH_TWO_INTERFACES, "minivps-web-1"
    )
    xml = set_domain_filterref_xml(with_filters, None)
    assert "filterref" not in xml


# --- ensure_pool (Mock) ---


def test_ensure_pool_returns_existing_active_pool_without_starting():
    conn = MagicMock()
    existing = MagicMock()
    existing.name.return_value = POOL_NAME
    conn.listAllStoragePools.return_value = [existing]
    pool = conn.storagePoolLookupByName.return_value
    pool.isActive.return_value = True

    result = ensure_pool(conn, POOL_NAME, POOL_XML)

    assert result is pool
    pool.create.assert_not_called()


def test_ensure_pool_starts_existing_inactive_pool():
    conn = MagicMock()
    existing = MagicMock()
    existing.name.return_value = POOL_NAME
    conn.listAllStoragePools.return_value = [existing]
    pool = conn.storagePoolLookupByName.return_value
    pool.isActive.return_value = False

    ensure_pool(conn, POOL_NAME, POOL_XML)

    pool.create.assert_called_once_with(0)


def test_ensure_pool_defines_new_pool_when_absent():
    conn = MagicMock()
    conn.listAllStoragePools.return_value = []
    pool = conn.storagePoolDefineXML.return_value

    result = ensure_pool(conn, POOL_NAME, POOL_XML)

    assert result is pool
    conn.storagePoolDefineXML.assert_called_once_with(POOL_XML, 0)
    pool.build.assert_called_once_with(0)
    pool.create.assert_called_once_with(0)
    pool.setAutostart.assert_called_once_with(1)


# --- create_overlay_volume (Mock) ---


def test_create_overlay_volume_deletes_existing_before_recreate(monkeypatch):
    conn = MagicMock()
    base_pool = conn.storagePoolLookupByName.return_value
    base_pool.storageVolLookupByName.return_value.path.return_value = "/images/base.img"
    pool = MagicMock()
    monkeypatch.setattr("mini_vps.resources.ensure_pool", lambda c, n, x: pool)
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
    monkeypatch.setattr("mini_vps.resources.ensure_pool", lambda c, n, x: pool)
    pool.listAllVolumes.return_value = []

    create_overlay_volume(conn, _spec())

    pool.storageVolLookupByName.assert_not_called()
    pool.createXML.assert_called_once()


# --- build_seed_iso (Mock) ---


def _seed_pool_mock(monkeypatch, existing_names=()):
    """ensure_seed_pool をモック化し、指定名の volume が既存であるプールを返す。"""
    pool = MagicMock()
    monkeypatch.setattr("mini_vps.resources.ensure_seed_pool", lambda c: pool)
    existing_vols = []
    for existing_name in existing_names:
        vol = MagicMock()
        vol.name.return_value = existing_name
        existing_vols.append(vol)
    pool.listAllVolumes.return_value = existing_vols
    pool.createXML.return_value.path.return_value = "/seeds/web-1-seed.iso"
    return pool


def _fake_run_writes_dummy_iso(cmd, check):
    # subprocess.run はまだ一時ディレクトリが存在するタイミングで呼ばれるため、
    # ここで書き出さないと with ブロックを抜けた時点でファイルごと削除されてしまう。
    # cmd の末尾3要素は常に output/user-data/meta-data(-N の有無に依存しない)。
    Path(cmd[-3]).write_bytes(b"dummy-iso-bytes")


def test_build_seed_iso_writes_expected_cloud_init_content(monkeypatch):
    captured = {}

    def fake_run(cmd, check):
        captured["user_data"] = Path(cmd[2]).read_text()
        captured["meta_data"] = Path(cmd[3]).read_text()
        captured["cmd"] = cmd
        _fake_run_writes_dummy_iso(cmd, check)

    monkeypatch.setattr("mini_vps.resources.subprocess.run", fake_run)
    conn = MagicMock()
    pool = _seed_pool_mock(monkeypatch)

    spec = _spec(name="web-1", hostname="web-1", user="ubuntu")
    seed_path = build_seed_iso(conn, spec, "ssh-ed25519 AAAA...")

    assert captured["cmd"][0] == "cloud-localds"
    assert "ssh-ed25519 AAAA..." in captured["user_data"]
    assert "web-1" in captured["meta_data"]
    assert seed_path == pool.createXML.return_value.path.return_value


def test_build_seed_iso_deletes_existing_seed_before_recreate(monkeypatch):
    monkeypatch.setattr("mini_vps.resources.subprocess.run", _fake_run_writes_dummy_iso)
    conn = MagicMock()
    pool = _seed_pool_mock(monkeypatch, existing_names=["web-1-seed.iso"])

    build_seed_iso(conn, _spec(name="web-1"), "ssh-ed25519 AAAA...")

    pool.storageVolLookupByName.assert_called_once_with("web-1-seed.iso")
    pool.storageVolLookupByName.return_value.delete.assert_called_once_with(0)


def test_build_seed_iso_skips_delete_when_seed_absent(monkeypatch):
    monkeypatch.setattr("mini_vps.resources.subprocess.run", _fake_run_writes_dummy_iso)
    conn = MagicMock()
    pool = _seed_pool_mock(monkeypatch)

    build_seed_iso(conn, _spec(name="web-1"), "ssh-ed25519 AAAA...")

    pool.storageVolLookupByName.assert_not_called()


def test_build_seed_iso_omits_write_files_when_no_startup_script(monkeypatch):
    captured = {}

    def fake_run(cmd, check):
        captured["user_data"] = Path(cmd[2]).read_text()
        _fake_run_writes_dummy_iso(cmd, check)

    monkeypatch.setattr("mini_vps.resources.subprocess.run", fake_run)
    conn = MagicMock()
    _seed_pool_mock(monkeypatch)

    build_seed_iso(conn, _spec(name="web-1"), "ssh-ed25519 AAAA...")

    parsed = yaml.safe_load(captured["user_data"])
    assert "write_files" not in parsed
    assert "runcmd" not in parsed


def test_build_seed_iso_includes_write_files_and_runcmd_when_startup_script_set(
    monkeypatch,
):
    captured = {}

    def fake_run(cmd, check):
        captured["user_data"] = Path(cmd[2]).read_text()
        _fake_run_writes_dummy_iso(cmd, check)

    monkeypatch.setattr("mini_vps.resources.subprocess.run", fake_run)
    conn = MagicMock()
    _seed_pool_mock(monkeypatch)

    spec = _spec(name="web-1", startup_script="opencode-sakura-ai-engine")
    build_seed_iso(
        conn, spec, "ssh-ed25519 AAAA...", secrets={"AI_ENGINE_TOKEN": "sk-abc"}
    )

    parsed = yaml.safe_load(captured["user_data"])
    assert "write_files" in parsed
    assert "runcmd" in parsed
    assert "sk-abc" in captured["user_data"]


def test_build_seed_iso_includes_static_routes_unit_when_set(monkeypatch):
    captured = {}

    def fake_run(cmd, check):
        captured["user_data"] = Path(cmd[2]).read_text()
        _fake_run_writes_dummy_iso(cmd, check)

    monkeypatch.setattr("mini_vps.resources.subprocess.run", fake_run)
    conn = MagicMock()
    _seed_pool_mock(monkeypatch)

    spec = _spec(
        name="web-1",
        static_routes=[{"destination": "192.168.202.0/24", "via": "192.168.201.1"}],
    )
    build_seed_iso(conn, spec, "ssh-ed25519 AAAA...")

    parsed = yaml.safe_load(captured["user_data"])
    assert "write_files" in parsed
    assert "runcmd" in parsed
    assert "minivps-static-routes.service" in captured["user_data"]
    assert "192.168.202.0/24" in captured["user_data"]


def test_build_seed_iso_combines_startup_script_and_static_routes(monkeypatch):
    captured = {}

    def fake_run(cmd, check):
        captured["user_data"] = Path(cmd[2]).read_text()
        _fake_run_writes_dummy_iso(cmd, check)

    monkeypatch.setattr("mini_vps.resources.subprocess.run", fake_run)
    conn = MagicMock()
    _seed_pool_mock(monkeypatch)

    spec = _spec(
        name="web-1",
        startup_script="opencode-sakura-ai-engine",
        static_routes=[{"destination": "192.168.202.0/24", "via": "192.168.201.1"}],
    )
    build_seed_iso(
        conn, spec, "ssh-ed25519 AAAA...", secrets={"AI_ENGINE_TOKEN": "sk-abc"}
    )

    parsed = yaml.safe_load(captured["user_data"])
    # 両方のフラグメントが連結されて write_files/runcmd に入っていること
    assert len(parsed["write_files"]) == 3  # opencode 2件 + static-routes 1件
    assert len(parsed["runcmd"]) == 9  # opencode 7件 + static-routes 2件
    assert "sk-abc" in captured["user_data"]
    assert "minivps-static-routes.service" in captured["user_data"]


def test_build_seed_iso_propagates_missing_secret_error_before_cloud_localds(
    monkeypatch,
):
    run_mock = MagicMock()
    monkeypatch.setattr("mini_vps.resources.subprocess.run", run_mock)
    conn = MagicMock()
    _seed_pool_mock(monkeypatch)

    spec = _spec(name="web-1", startup_script="opencode-sakura-ai-engine")

    with pytest.raises(StartupScriptError):
        build_seed_iso(conn, spec, "ssh-ed25519 AAAA...", secrets=None)

    # secrets 不足を検知した時点で失敗するため、cloud-localds は一切呼ばれない
    run_mock.assert_not_called()


# --- build_seed_iso: network-config(-N) ---


def test_build_seed_iso_omits_dash_n_when_all_networks_are_dhcp(monkeypatch):
    captured = {}

    def fake_run(cmd, check):
        captured["cmd"] = cmd
        _fake_run_writes_dummy_iso(cmd, check)

    monkeypatch.setattr("mini_vps.resources.subprocess.run", fake_run)
    conn = MagicMock()
    _seed_pool_mock(monkeypatch)

    build_seed_iso(conn, _spec(networks=["default"]), "ssh-ed25519 AAAA...")

    assert "-N" not in captured["cmd"]
    assert len(captured["cmd"]) == 4


def test_build_seed_iso_passes_dash_n_and_network_config_when_static_present(
    monkeypatch,
):
    captured = {}

    def fake_run(cmd, check):
        captured["cmd"] = cmd
        nc_index = cmd.index("-N") + 1
        captured["network_config"] = yaml.safe_load(Path(cmd[nc_index]).read_text())
        _fake_run_writes_dummy_iso(cmd, check)

    monkeypatch.setattr("mini_vps.resources.subprocess.run", fake_run)
    conn = MagicMock()
    _seed_pool_mock(monkeypatch)

    spec = _spec(
        name="web-1",
        networks=[{"name": "seg1", "address": "192.168.201.10/24"}],
    )
    build_seed_iso(conn, spec, "ssh-ed25519 AAAA...")

    assert "-N" in captured["cmd"]
    # output/user-data/meta-data は -N があっても常に末尾3要素であること
    assert captured["cmd"][0] == "cloud-localds"
    assert len(captured["cmd"]) == 6
    assert captured["network_config"] == _build_network_config(spec)


def test_build_seed_iso_network_config_covers_all_nics_including_dhcp(monkeypatch):
    captured = {}

    def fake_run(cmd, check):
        nc_index = cmd.index("-N") + 1
        captured["network_config"] = yaml.safe_load(Path(cmd[nc_index]).read_text())
        _fake_run_writes_dummy_iso(cmd, check)

    monkeypatch.setattr("mini_vps.resources.subprocess.run", fake_run)
    conn = MagicMock()
    _seed_pool_mock(monkeypatch)

    spec = _spec(
        name="web-1",
        networks=["default", {"name": "seg1", "address": "192.168.201.10/24"}],
    )
    build_seed_iso(conn, spec, "ssh-ed25519 AAAA...")

    ethernets = captured["network_config"]["network"]["ethernets"]
    assert len(ethernets) == 2
    assert ethernets["eth0"]["dhcp4"] is True
    assert ethernets["eth1"]["addresses"] == ["192.168.201.10/24"]


def test_build_seed_iso_network_config_contains_nameservers(monkeypatch):
    captured = {}

    def fake_run(cmd, check):
        nc_index = cmd.index("-N") + 1
        captured["network_config"] = yaml.safe_load(Path(cmd[nc_index]).read_text())
        _fake_run_writes_dummy_iso(cmd, check)

    monkeypatch.setattr("mini_vps.resources.subprocess.run", fake_run)
    conn = MagicMock()
    _seed_pool_mock(monkeypatch)

    spec = _spec(
        name="web-1",
        networks=[
            {
                "name": "seg1",
                "address": "192.168.201.10/24",
                "nameservers": ["192.168.203.30"],
                "search": ["minivps.internal"],
            }
        ],
    )
    build_seed_iso(conn, spec, "ssh-ed25519 AAAA...")

    entry = captured["network_config"]["network"]["ethernets"]["eth0"]
    assert entry["nameservers"] == {
        "addresses": ["192.168.203.30"],
        "search": ["minivps.internal"],
    }
