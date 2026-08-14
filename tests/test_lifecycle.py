from unittest.mock import MagicMock

import libvirt
import pytest

from mini_vps.config import POOL_NAME, SEED_POOL_NAME
from mini_vps.lifecycle import (
    _lease_ipv4,
    ensure_network_active,
    provision,
    teardown,
    wait_for_ip,
)

# --- ensure_network_active ---


@pytest.mark.parametrize(("is_active", "creates"), [(False, True), (True, False)])
def test_ensure_network_active_starts_only_inactive_network(is_active, creates):
    conn = MagicMock()
    net = MagicMock()
    net.isActive.return_value = is_active
    conn.networkLookupByName.return_value = net

    ensure_network_active(conn, {"networks": ["default"]})

    assert net.create.called is creates


def test_ensure_network_active_checks_every_network():
    conn = MagicMock()
    nets = {"seg1": MagicMock(), "seg2": MagicMock()}
    nets["seg1"].isActive.return_value = False
    nets["seg2"].isActive.return_value = True
    conn.networkLookupByName.side_effect = lambda name: nets[name]

    ensure_network_active(conn, {"networks": ["seg1", "seg2"]})

    conn.networkLookupByName.assert_any_call("seg1")
    conn.networkLookupByName.assert_any_call("seg2")
    nets["seg1"].create.assert_called_once()
    nets["seg2"].create.assert_not_called()


def test_ensure_network_active_handles_network_attachment_elements():
    conn = MagicMock()
    nets = {"default": MagicMock(), "seg1": MagicMock()}
    nets["default"].isActive.return_value = False
    nets["seg1"].isActive.return_value = False
    conn.networkLookupByName.side_effect = lambda name: nets[name]

    ensure_network_active(
        conn,
        {"networks": ["default", {"name": "seg1", "address": "192.168.201.10/24"}]},
    )

    conn.networkLookupByName.assert_any_call("default")
    conn.networkLookupByName.assert_any_call("seg1")
    nets["default"].create.assert_called_once()
    nets["seg1"].create.assert_called_once()


# --- provision ---


@pytest.fixture
def stub_provision_deps(monkeypatch):
    # provision が呼ぶ協調オブジェクトを既定のスタブへ差し替える。
    # 検査したいものだけを各テストで上書きする。返すのは build_domain_xml のモック。
    monkeypatch.setattr("mini_vps.lifecycle.ensure_network_active", MagicMock())
    monkeypatch.setattr("mini_vps.lifecycle.read_pubkey", lambda: "ssh-ed25519 AAAA")
    monkeypatch.setattr("mini_vps.lifecycle.build_nwfilter_xml", lambda s: "<filter/>")
    monkeypatch.setattr("mini_vps.lifecycle._filter_name", lambda s: "minivps-web-1")
    monkeypatch.setattr(
        "mini_vps.lifecycle.create_overlay_volume", lambda c, s: "/overlay.qcow2"
    )
    monkeypatch.setattr(
        "mini_vps.lifecycle.build_seed_iso",
        lambda c, s, pubkey, secrets=None: "/seed.iso",
    )
    build_domain_xml = MagicMock(return_value="<domain/>")
    monkeypatch.setattr("mini_vps.lifecycle.build_domain_xml", build_domain_xml)
    return build_domain_xml


def test_provision_defines_nwfilter_when_filters_present(stub_provision_deps):
    conn = MagicMock()
    spec = {"name": "web-1", "filters": [{"port": 22, "protocol": "tcp"}]}

    provision(conn, spec)

    conn.nwfilterDefineXML.assert_called_once_with("<filter/>")
    stub_provision_deps.assert_called_once_with(
        spec, "/overlay.qcow2", "/seed.iso", filter_name="minivps-web-1"
    )
    conn.defineXML.assert_called_once_with("<domain/>")


def test_provision_skips_nwfilter_when_absent(stub_provision_deps):
    conn = MagicMock()
    spec = {"name": "web-1"}

    provision(conn, spec)

    conn.nwfilterDefineXML.assert_not_called()
    stub_provision_deps.assert_called_once_with(
        spec, "/overlay.qcow2", "/seed.iso", filter_name=None
    )


def test_provision_passes_secrets_to_build_seed_iso(monkeypatch, stub_provision_deps):
    conn = MagicMock()
    spec = {"name": "web-1"}
    secrets = {"AI_ENGINE_TOKEN": "sk-abc"}
    build_seed_mock = MagicMock(return_value="/seed.iso")
    monkeypatch.setattr("mini_vps.lifecycle.build_seed_iso", build_seed_mock)

    provision(conn, spec, secrets=secrets)

    build_seed_mock.assert_called_once_with(
        conn, spec, "ssh-ed25519 AAAA", secrets=secrets
    )


def test_provision_builds_seed_before_overlay(monkeypatch, stub_provision_deps):
    conn = MagicMock()
    spec = {"name": "web-1"}
    call_order = []
    monkeypatch.setattr(
        "mini_vps.lifecycle.create_overlay_volume",
        lambda c, s: call_order.append("overlay") or "/overlay.qcow2",
    )
    monkeypatch.setattr(
        "mini_vps.lifecycle.build_seed_iso",
        lambda c, s, pubkey, secrets=None: call_order.append("seed") or "/seed.iso",
    )

    provision(conn, spec)

    # secrets 不足による StartupScriptError を、overlay volume 作成という
    # コストのかかる処理の前に検知するための順序(フェイルファスト)
    assert call_order == ["seed", "overlay"]


# --- _lease_ipv4 ---


@pytest.mark.parametrize(
    ("ifaces", "expected"),
    [
        (
            {
                "vnet0": {
                    "addrs": [
                        {"type": libvirt.VIR_IP_ADDR_TYPE_IPV4, "addr": "10.0.0.5"}
                    ]
                }
            },
            "10.0.0.5",
        ),
        (
            {
                "vnet0": {
                    "addrs": [
                        {"type": libvirt.VIR_IP_ADDR_TYPE_IPV6, "addr": "fe80::1"}
                    ]
                }
            },
            None,
        ),
        (
            {
                "vnet0": {
                    "addrs": [
                        {"type": libvirt.VIR_IP_ADDR_TYPE_IPV6, "addr": "fe80::1"}
                    ]
                },
                "vnet1": {
                    "addrs": [
                        {"type": libvirt.VIR_IP_ADDR_TYPE_IPV4, "addr": "10.0.0.9"}
                    ]
                },
            },
            "10.0.0.9",
        ),
    ],
)
def test_lease_ipv4_extracts_first_ipv4(ifaces, expected):
    dom = MagicMock()
    dom.interfaceAddresses.return_value = ifaces

    assert _lease_ipv4(dom) == expected


# --- wait_for_ip ---


def test_wait_for_ip_returns_once_available(monkeypatch):
    dom = MagicMock()
    monkeypatch.setattr(
        "mini_vps.lifecycle._lease_ipv4",
        MagicMock(side_effect=[None, None, "10.0.0.5"]),
    )
    sleep_mock = MagicMock()
    monkeypatch.setattr("mini_vps.lifecycle.time.sleep", sleep_mock)

    assert wait_for_ip(dom, timeout=120) == "10.0.0.5"
    assert sleep_mock.call_count == 2


def test_wait_for_ip_times_out_without_sleeping(monkeypatch):
    dom = MagicMock()
    monkeypatch.setattr("mini_vps.lifecycle._lease_ipv4", MagicMock(return_value=None))
    sleep_mock = MagicMock()
    monkeypatch.setattr("mini_vps.lifecycle.time.sleep", sleep_mock)

    # timeout=0 なのでループ本体に入らず即座に None を返す(time.time() 自体はpatch不要)
    assert wait_for_ip(dom, timeout=0) is None
    sleep_mock.assert_not_called()


def test_wait_for_ip_warns_on_timeout(monkeypatch, caplog):
    """無音のまま待ち続けるのではなく、諦めたことを WARNING で残す。"""
    dom = MagicMock()
    dom.name.return_value = "web-1"
    monkeypatch.setattr("mini_vps.lifecycle._lease_ipv4", MagicMock(return_value=None))
    monkeypatch.setattr("mini_vps.lifecycle.time.sleep", MagicMock())

    with caplog.at_level("WARNING", logger="mini_vps.lifecycle"):
        assert wait_for_ip(dom, timeout=0) is None

    assert caplog.records[-1].levelname == "WARNING"
    assert "DHCP リースを取得できなかった" in caplog.records[-1].getMessage()


# --- teardown ---


def test_teardown_destroys_and_undefines_active_domain():
    conn = MagicMock()
    dom = MagicMock()
    dom.name.return_value = "web-1"
    dom.isActive.return_value = True
    conn.listAllDomains.return_value = [dom]
    conn.lookupByName.return_value = dom
    conn.listAllNWFilters.return_value = []
    conn.listAllStoragePools.return_value = []

    teardown(conn, {"name": "web-1"})

    dom.destroy.assert_called_once()
    dom.undefineFlags.assert_called_once_with(libvirt.VIR_DOMAIN_UNDEFINE_NVRAM)


def test_teardown_skips_domain_when_absent():
    conn = MagicMock()
    conn.listAllDomains.return_value = []
    conn.listAllNWFilters.return_value = []
    conn.listAllStoragePools.return_value = []

    teardown(conn, {"name": "web-1"})

    conn.lookupByName.assert_not_called()


def test_teardown_undefines_nwfilter_when_present():
    conn = MagicMock()
    conn.listAllDomains.return_value = []
    nwfilter = MagicMock()
    nwfilter.name.return_value = "minivps-web-1"
    conn.listAllNWFilters.return_value = [nwfilter]
    conn.listAllStoragePools.return_value = []

    teardown(conn, {"name": "web-1"})

    conn.nwfilterLookupByName.assert_called_once_with("minivps-web-1")
    conn.nwfilterLookupByName.return_value.undefine.assert_called_once()


def test_teardown_deletes_overlay_volume_when_present():
    conn = MagicMock()
    conn.listAllDomains.return_value = []
    conn.listAllNWFilters.return_value = []
    pool_entry = MagicMock()
    pool_entry.name.return_value = POOL_NAME
    conn.listAllStoragePools.return_value = [pool_entry]
    pool = MagicMock()
    conn.storagePoolLookupByName.return_value = pool
    vol = MagicMock()
    vol.name.return_value = "web-1.qcow2"
    pool.listAllVolumes.return_value = [vol]

    teardown(conn, {"name": "web-1"})

    pool.storageVolLookupByName.assert_called_once_with("web-1.qcow2")
    pool.storageVolLookupByName.return_value.delete.assert_called_once_with(0)


def test_teardown_deletes_seed_volume_when_present():
    conn = MagicMock()
    conn.listAllDomains.return_value = []
    conn.listAllNWFilters.return_value = []
    pool_entry = MagicMock()
    pool_entry.name.return_value = SEED_POOL_NAME
    conn.listAllStoragePools.return_value = [pool_entry]
    pool = MagicMock()
    conn.storagePoolLookupByName.return_value = pool
    vol = MagicMock()
    vol.name.return_value = "web-1-seed.iso"
    pool.listAllVolumes.return_value = [vol]

    teardown(conn, {"name": "web-1"})

    pool.storageVolLookupByName.assert_called_once_with("web-1-seed.iso")
    pool.storageVolLookupByName.return_value.delete.assert_called_once_with(0)


def test_teardown_skips_seed_delete_when_pool_absent():
    conn = MagicMock()
    conn.listAllDomains.return_value = []
    conn.listAllNWFilters.return_value = []
    conn.listAllStoragePools.return_value = []

    teardown(conn, {"name": "web-1"})

    conn.storagePoolLookupByName.assert_not_called()
