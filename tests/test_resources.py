import io
import xml.etree.ElementTree as ET
from unittest.mock import MagicMock

import pycdlib
import pytest
import yaml
from conftest import macos_profile

from mini_vps.config import BALLOON_STATS_PERIOD_SECONDS, POOL_NAME, QEMU_XML_NS
from mini_vps.platform_profile import detect
from mini_vps.resources import (
    _build_network_config,
    _build_static_routes_fragment,
    _filter_name,
    _has_static_network,
    _mac_for_interface,
    _network_name,
    allocate_ssh_port,
    build_domain_xml,
    build_nwfilter_xml,
    build_seed_iso,
    build_seed_iso_bytes,
    create_overlay_volume,
    ensure_pool,
    render_seed_files,
    resize_domain_xml,
    set_domain_filterref_xml,
    ssh_forward_port,
)
from mini_vps.startup_scripts import StartupScriptError

POOL_XML = "<pool type='dir'><name>vps-pool</name></pool>"


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


def _domain(spec=None, **kwargs):
    xml = build_domain_xml(spec or _spec(), "/overlay.qcow2", "/seed.iso", **kwargs)
    return ET.fromstring(xml)


def test_build_domain_xml_converts_memory_to_kib():
    memory = _domain().find("memory")
    assert memory.get("unit") == "KiB"
    assert memory.text == "1048576"


def test_build_domain_xml_embeds_paths_and_fields():
    xml = build_domain_xml(_spec(), "/lab/web-1.qcow2", "/lab/web-1-seed.iso")
    root = ET.fromstring(xml)
    assert root.find("name").text == "web-1"
    assert root.find("vcpu").text == "2"
    sources = [d.find("source").get("file") for d in root.findall("devices/disk")]
    assert sources == ["/lab/web-1.qcow2", "/lab/web-1-seed.iso"]
    assert root.find("devices/interface/source").get("network") == "default"


def test_build_domain_xml_without_filter_omits_filterref():
    assert _domain().find("devices/interface/filterref") is None


def test_build_domain_xml_with_filter_adds_filterref():
    root = _domain(filter_name="minivps-web-1")
    assert root.find("devices/interface/filterref").get("filter") == "minivps-web-1"


def test_build_domain_xml_generates_one_interface_per_network_with_filter():
    root = _domain(_spec(networks=["seg1", "seg2"]), filter_name="minivps-web-1")
    interfaces = root.findall("devices/interface")
    assert [i.find("source").get("network") for i in interfaces] == ["seg1", "seg2"]
    assert all(i.find("filterref").get("filter") == "minivps-web-1" for i in interfaces)
    assert all(i.find("model").get("type") == "virtio" for i in interfaces)


def test_build_domain_xml_uses_kvm_host_model_q35_on_linux_kvm():
    root = _domain()
    assert root.get("type") == "kvm"
    assert root.find("cpu").get("mode") == "host-model"
    os_type = root.find("os/type")
    assert (os_type.get("arch"), os_type.get("machine")) == ("x86_64", "q35")


def test_build_domain_xml_uses_uefi_firmware():
    root = _domain()
    assert root.find("os").get("firmware") == "efi"
    assert root.find("os/loader").get("secure") == "no"


def test_build_domain_xml_includes_rng_clock_pm_and_discard():
    root = _domain()
    assert root.find("devices/rng").get("model") == "virtio"
    assert root.find("clock").get("offset") == "utc"
    assert root.find("pm/suspend-to-mem").get("enabled") == "no"
    assert root.find("pm/suspend-to-disk").get("enabled") == "no"
    assert root.find("devices/disk/driver").get("discard") == "unmap"


def test_build_domain_xml_uses_direct_io_on_linux():
    driver = _domain().find("devices/disk/driver")
    assert (driver.get("cache"), driver.get("io")) == ("none", "native")


def test_build_domain_xml_enables_memballoon_stats_period():
    stats = _domain().find("devices/memballoon/stats")
    assert stats.get("period") == str(BALLOON_STATS_PERIOD_SECONDS)


def test_build_domain_xml_adds_guest_agent_channel():
    channel = _domain().find("devices/channel")
    assert channel.get("type") == "unix"
    assert channel.find("target").get("name") == "org.qemu.guest_agent.0"


def test_build_domain_xml_attaches_seed_as_sata_cdrom_on_x86():
    seed = _domain().findall("devices/disk")[1]
    assert seed.get("device") == "cdrom"
    assert seed.find("target").get("bus") == "sata"
    assert seed.find("readonly") is not None


def test_build_domain_xml_requires_networks_key():
    spec = _spec()
    del spec["networks"]
    with pytest.raises(KeyError):
        build_domain_xml(spec, "/overlay.qcow2", "/seed.iso")


def test_build_domain_xml_embeds_deterministic_mac_per_interface():
    root = _domain(_spec(networks=["seg1", "seg2"]))
    macs = [i.find("mac").get("address") for i in root.findall("devices/interface")]
    assert macs == [_mac_for_interface("web-1", 0), _mac_for_interface("web-1", 1)]


def test_build_domain_xml_uses_qemu_tcg_with_maximum_cpu_without_kvm():
    profile = detect(system="Linux", machine="x86_64", env={}, kvm_available=False)
    root = _domain(profile=profile)
    assert root.get("type") == "qemu"
    assert root.find("cpu").get("mode") == "maximum"


def test_build_domain_xml_on_macos_uses_hvf_aarch64_virt():
    root = _domain(profile=macos_profile(), ssh_port=2201)
    assert root.get("type") == "hvf"
    assert root.find("cpu").get("mode") == "host-passthrough"
    os_type = root.find("os/type")
    assert (os_type.get("arch"), os_type.get("machine")) == ("aarch64", "virt")
    # io='native' は Linux 専用なので付けない
    driver = root.find("devices/disk/driver")
    assert driver.get("io") is None
    assert driver.get("cache") is None


def test_build_domain_xml_on_aarch64_attaches_seed_as_readonly_virtio_disk():
    root = _domain(profile=macos_profile(), ssh_port=2201)
    seed = root.findall("devices/disk")[1]
    assert seed.get("device") == "disk"
    assert seed.find("target").get("bus") == "virtio"
    assert seed.find("readonly") is not None


def test_build_domain_xml_user_mode_forwards_ssh_via_qemu_commandline():
    xml = build_domain_xml(
        _spec(), "/o.qcow2", "/s.iso", profile=macos_profile(), ssh_port=2207
    )
    root = ET.fromstring(xml)
    assert root.findall("devices/interface") == []
    args = [a.get("value") for a in root.iter(f"{{{QEMU_XML_NS}}}arg")]
    assert args[0] == "-netdev"
    assert "hostfwd=tcp:127.0.0.1:2207-:22" in args[1]
    assert f"mac={_mac_for_interface('web-1', 0)}" in args[3]
    assert ssh_forward_port(xml) == 2207


def test_build_domain_xml_user_mode_requires_ssh_port():
    with pytest.raises(ValueError):
        build_domain_xml(_spec(), "/o.qcow2", "/s.iso", profile=macos_profile())


def test_ssh_forward_port_is_none_for_libvirt_networking():
    xml = build_domain_xml(_spec(), "/o.qcow2", "/s.iso")
    assert ssh_forward_port(xml) is None


def test_ssh_forward_port_survives_resize_roundtrip():
    xml = build_domain_xml(
        _spec(), "/o.qcow2", "/s.iso", profile=macos_profile(), ssh_port=2210
    )
    resized = resize_domain_xml(xml, 2048 * 1024, 4)
    assert ssh_forward_port(resized) == 2210
    assert "qemu:commandline" in resized


# --- allocate_ssh_port ---


def test_allocate_ssh_port_skips_used_and_busy_ports():
    port = allocate_ssh_port({2201}, (2201, 2205), is_free=lambda p: p != 2202)
    assert port == 2203


def test_allocate_ssh_port_raises_when_exhausted():
    with pytest.raises(RuntimeError):
        allocate_ssh_port({2201, 2202}, (2201, 2202), is_free=lambda p: True)


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


# --- render_seed_files / build_seed_iso_bytes ---


def _read_iso(iso_bytes):
    """Seed ISO を pycdlib で読み戻し、(ラベル, {Rock Ridge 名: 中身}) を返す。"""
    iso = pycdlib.PyCdlib()
    iso.open_fp(io.BytesIO(iso_bytes))
    label = iso.pvd.volume_identifier.decode().strip()
    files = {}
    for child in iso.list_children(rr_path="/"):
        if child.is_dot() or child.is_dotdot():
            continue
        name = child.rock_ridge.name().decode()
        out = io.BytesIO()
        iso.get_file_from_iso_fp(out, rr_path=f"/{name}")
        files[name] = out.getvalue()
    iso.close()
    return label, files


def _user_data(files):
    text = files["user-data"].decode()
    assert text.startswith("#cloud-config\n")
    return yaml.safe_load(text)


def test_build_seed_iso_bytes_has_cidata_label_and_long_names():
    files = {"user-data": b"#cloud-config\n", "meta-data": b"instance-id: x\n"}
    label, read_back = _read_iso(build_seed_iso_bytes(files))
    assert label == "cidata"
    assert read_back == files


def test_render_seed_files_contains_pubkey_and_hostname():
    files = render_seed_files(_spec(), "ssh-ed25519 AAAA...")
    assert set(files) == {"user-data", "meta-data"}
    user_data = _user_data(files)
    assert user_data["users"][0]["ssh_authorized_keys"] == ["ssh-ed25519 AAAA..."]
    assert b"local-hostname: web-1" in files["meta-data"]


def test_render_seed_files_installs_and_starts_guest_agent():
    user_data = _user_data(render_seed_files(_spec(), "k"))
    assert user_data["packages"] == ["qemu-guest-agent"]
    assert "qemu-guest-agent" in " ".join(user_data["runcmd"][0])


def test_render_seed_files_omits_write_files_when_no_startup_script():
    user_data = _user_data(render_seed_files(_spec(), "k"))
    assert "write_files" not in user_data
    assert len(user_data["runcmd"]) == 1  # guest agent の起動のみ


def test_render_seed_files_includes_startup_script_with_secrets():
    spec = _spec(startup_script="opencode-sakura-ai-engine")
    files = render_seed_files(spec, "k", secrets={"AI_ENGINE_TOKEN": "sk-abc"})
    user_data = _user_data(files)
    assert "write_files" in user_data
    assert b"sk-abc" in files["user-data"]


def test_render_seed_files_includes_static_routes_unit():
    spec = _spec(
        static_routes=[{"destination": "192.168.202.0/24", "via": "192.168.201.1"}]
    )
    files = render_seed_files(spec, "k")
    assert b"minivps-static-routes.service" in files["user-data"]
    assert b"192.168.202.0/24" in files["user-data"]


def test_render_seed_files_combines_startup_script_and_static_routes():
    spec = _spec(
        startup_script="opencode-sakura-ai-engine",
        static_routes=[{"destination": "192.168.202.0/24", "via": "192.168.201.1"}],
    )
    files = render_seed_files(spec, "k", secrets={"AI_ENGINE_TOKEN": "sk-abc"})
    user_data = _user_data(files)
    assert len(user_data["write_files"]) == 3  # opencode 2件 + static-routes 1件
    # guest agent 1件 + opencode 7件 + static-routes 2件
    assert len(user_data["runcmd"]) == 10


def test_render_seed_files_raises_on_missing_secret():
    spec = _spec(startup_script="opencode-sakura-ai-engine")
    with pytest.raises(StartupScriptError):
        render_seed_files(spec, "k", secrets=None)


def test_render_seed_files_omits_network_config_when_all_dhcp():
    assert "network-config" not in render_seed_files(_spec(), "k")


def test_render_seed_files_network_config_covers_all_nics_when_static_present():
    spec = _spec(
        networks=[
            "default",
            {
                "name": "seg1",
                "address": "192.168.201.10/24",
                "nameservers": ["192.168.203.30"],
                "search": ["minivps.internal"],
            },
        ],
    )
    files = render_seed_files(spec, "k")
    config = yaml.safe_load(files["network-config"])
    assert config == _build_network_config(spec)
    ethernets = config["network"]["ethernets"]
    assert ethernets["eth0"]["dhcp4"] is True
    assert ethernets["eth1"]["nameservers"] == {
        "addresses": ["192.168.203.30"],
        "search": ["minivps.internal"],
    }


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


def _stream_conn():
    """送られたバイト列を記録する stream を返す libvirt 接続の Mock。"""
    conn = MagicMock()
    sent = bytearray()
    stream = conn.newStream.return_value

    def _send(data):
        sent.extend(data)
        return len(data)

    stream.send.side_effect = _send
    return conn, sent


def test_build_seed_iso_uploads_iso_and_returns_path(monkeypatch):
    conn, sent = _stream_conn()
    pool = _seed_pool_mock(monkeypatch)

    path = build_seed_iso(conn, _spec(), "ssh-ed25519 AAAA...")

    assert path == "/seeds/web-1-seed.iso"
    label, files = _read_iso(bytes(sent))
    assert label == "cidata"
    assert b"ssh-ed25519 AAAA..." in files["user-data"]
    # capacity と upload 長は ISO の実サイズに一致する
    vol_xml = pool.createXML.call_args.args[0]
    assert f"<capacity unit='bytes'>{len(sent)}</capacity>" in vol_xml
    pool.createXML.return_value.upload.assert_called_once_with(
        conn.newStream.return_value, 0, len(sent), 0
    )
    conn.newStream.return_value.finish.assert_called_once()


def test_build_seed_iso_deletes_existing_seed_before_recreate(monkeypatch):
    conn, _ = _stream_conn()
    pool = _seed_pool_mock(monkeypatch, existing_names=["web-1-seed.iso"])

    build_seed_iso(conn, _spec(), "k")

    pool.storageVolLookupByName.assert_called_once_with("web-1-seed.iso")
    pool.storageVolLookupByName.return_value.delete.assert_called_once_with(0)


def test_build_seed_iso_skips_delete_when_seed_absent(monkeypatch):
    conn, _ = _stream_conn()
    pool = _seed_pool_mock(monkeypatch)

    build_seed_iso(conn, _spec(), "k")

    pool.storageVolLookupByName.assert_not_called()


def test_build_seed_iso_aborts_stream_on_send_failure(monkeypatch):
    conn = MagicMock()
    conn.newStream.return_value.send.side_effect = OSError("broken pipe")
    _seed_pool_mock(monkeypatch)

    with pytest.raises(OSError):
        build_seed_iso(conn, _spec(), "k")

    conn.newStream.return_value.abort.assert_called_once()
    conn.newStream.return_value.finish.assert_not_called()


def test_build_seed_iso_fails_before_touching_pool_on_missing_secret(monkeypatch):
    conn = MagicMock()
    pool = _seed_pool_mock(monkeypatch)
    spec = _spec(startup_script="opencode-sakura-ai-engine")

    with pytest.raises(StartupScriptError):
        build_seed_iso(conn, spec, "k", secrets=None)

    pool.createXML.assert_not_called()
