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


def test_ensure_network_active_starts_inactive_network():
    conn = MagicMock()
    net = MagicMock()
    net.isActive.return_value = False
    conn.networkLookupByName.return_value = net

    ensure_network_active(conn, {"network": "default"})

    net.create.assert_called_once()


def test_ensure_network_active_skips_when_already_active():
    conn = MagicMock()
    net = MagicMock()
    net.isActive.return_value = True
    conn.networkLookupByName.return_value = net

    ensure_network_active(conn, {"network": "default"})

    net.create.assert_not_called()


# --- provision ---


def test_provision_defines_nwfilter_when_filters_present(monkeypatch):
    conn = MagicMock()
    spec = {"name": "web-1", "filters": [{"port": 22, "protocol": "tcp"}]}
    monkeypatch.setattr("mini_vps.lifecycle.ensure_network_active", MagicMock())
    monkeypatch.setattr("mini_vps.lifecycle.build_nwfilter_xml", lambda s: "<filter/>")
    monkeypatch.setattr("mini_vps.lifecycle._filter_name", lambda s: "minivps-web-1")
    monkeypatch.setattr(
        "mini_vps.lifecycle.create_overlay_volume", lambda c, s: "/overlay.qcow2"
    )
    monkeypatch.setattr(
        "mini_vps.lifecycle.build_seed_iso",
        lambda c, s, pubkey, secrets=None: "/seed.iso",
    )
    monkeypatch.setattr("mini_vps.lifecycle.read_pubkey", lambda: "ssh-ed25519 AAAA")
    build_domain_xml_mock = MagicMock(return_value="<domain/>")
    monkeypatch.setattr("mini_vps.lifecycle.build_domain_xml", build_domain_xml_mock)

    provision(conn, spec)

    conn.nwfilterDefineXML.assert_called_once_with("<filter/>")
    build_domain_xml_mock.assert_called_once_with(
        spec, "/overlay.qcow2", "/seed.iso", filter_name="minivps-web-1"
    )
    conn.defineXML.assert_called_once_with("<domain/>")


def test_provision_skips_nwfilter_when_absent(monkeypatch):
    conn = MagicMock()
    spec = {"name": "web-1"}
    monkeypatch.setattr("mini_vps.lifecycle.ensure_network_active", MagicMock())
    monkeypatch.setattr(
        "mini_vps.lifecycle.create_overlay_volume", lambda c, s: "/overlay.qcow2"
    )
    monkeypatch.setattr(
        "mini_vps.lifecycle.build_seed_iso",
        lambda c, s, pubkey, secrets=None: "/seed.iso",
    )
    monkeypatch.setattr("mini_vps.lifecycle.read_pubkey", lambda: "ssh-ed25519 AAAA")
    build_domain_xml_mock = MagicMock(return_value="<domain/>")
    monkeypatch.setattr("mini_vps.lifecycle.build_domain_xml", build_domain_xml_mock)

    provision(conn, spec)

    conn.nwfilterDefineXML.assert_not_called()
    build_domain_xml_mock.assert_called_once_with(
        spec, "/overlay.qcow2", "/seed.iso", filter_name=None
    )


def test_provision_passes_secrets_to_build_seed_iso(monkeypatch):
    conn = MagicMock()
    spec = {"name": "web-1"}
    secrets = {"AI_ENGINE_TOKEN": "sk-abc"}
    monkeypatch.setattr("mini_vps.lifecycle.ensure_network_active", MagicMock())
    monkeypatch.setattr(
        "mini_vps.lifecycle.create_overlay_volume", lambda c, s: "/overlay.qcow2"
    )
    build_seed_mock = MagicMock(return_value="/seed.iso")
    monkeypatch.setattr("mini_vps.lifecycle.build_seed_iso", build_seed_mock)
    monkeypatch.setattr("mini_vps.lifecycle.read_pubkey", lambda: "ssh-ed25519 AAAA")
    monkeypatch.setattr(
        "mini_vps.lifecycle.build_domain_xml", MagicMock(return_value="<domain/>")
    )

    provision(conn, spec, secrets=secrets)

    build_seed_mock.assert_called_once_with(
        conn, spec, "ssh-ed25519 AAAA", secrets=secrets
    )


def test_provision_builds_seed_before_overlay(monkeypatch):
    conn = MagicMock()
    spec = {"name": "web-1"}
    call_order = []
    monkeypatch.setattr("mini_vps.lifecycle.ensure_network_active", MagicMock())
    monkeypatch.setattr(
        "mini_vps.lifecycle.create_overlay_volume",
        lambda c, s: call_order.append("overlay") or "/overlay.qcow2",
    )
    monkeypatch.setattr(
        "mini_vps.lifecycle.build_seed_iso",
        lambda c, s, pubkey, secrets=None: call_order.append("seed") or "/seed.iso",
    )
    monkeypatch.setattr("mini_vps.lifecycle.read_pubkey", lambda: "ssh-ed25519 AAAA")
    monkeypatch.setattr(
        "mini_vps.lifecycle.build_domain_xml", MagicMock(return_value="<domain/>")
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
