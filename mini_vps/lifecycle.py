"""VM のプロビジョニングと削除。"""

import os
import time

import libvirt

from .config import LAB_DIR, POOL_NAME
from .resources import build_domain_xml, build_seed_iso, create_overlay_volume
from .spec import read_pubkey


def provision(conn, spec) -> libvirt.virDomain:
    """VM を定義し、未起動の domain を返す。

    overlay → seed → domain XML → defineXML の順に処理する。
    起動は呼び出し側が行う(起動前に metadata を付与するため)。

    Args:
        conn: libvirt 接続オブジェクト。
        spec: VM スペックの dict。

    Returns:
        定義済み(未起動)の libvirt.virDomain オブジェクト。
    """
    net = conn.networkLookupByName(spec.get("network", "default"))

    if not net.isActive():
        net.create()

    overlay_path = create_overlay_volume(conn, spec)
    seed_path = build_seed_iso(spec, read_pubkey())
    xml = build_domain_xml(spec, overlay_path, seed_path)
    return conn.defineXML(xml)


def _lease_ipv4(dom: libvirt.virDomain) -> str | None:
    """DHCP リースから IPv4 を1回だけ取得する。

    libvirt が NIC(MAC) に紐づくリースだけを返すため、古いリースを掴まない。
    """
    ifaces = dom.interfaceAddresses(libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE)
    for iface in ifaces.values():
        for addr in iface["addrs"]:
            if addr["type"] == libvirt.VIR_IP_ADDR_TYPE_IPV4:
                return addr["addr"]
    return None


def wait_for_ip(dom: libvirt.virDomain, timeout=120) -> str | None:
    """DHCP リースをポーリングし、IPv4 が確定するまで待つ。

    Args:
        dom: 対象の libvirt.virDomain。
        timeout: 最大待機秒数。デフォルトは 120 秒。

    Returns:
        割り当てられた IPv4 アドレス文字列。タイムアウト時は None。
    """
    start_time = time.time()
    while time.time() - start_time < timeout:
        ip = _lease_ipv4(dom)
        if ip is not None:
            return ip
        time.sleep(2)
    return None


def teardown(conn, spec) -> None:
    """VM を後始末する。

    destroy → undefine → overlay volume 削除 → seed ISO 削除 の順に処理する。

    Args:
        conn: libvirt 接続オブジェクト。
        spec: VM スペックの dict。name キーのみ参照する。
    """
    # domain
    if spec["name"] in {d.name() for d in conn.listAllDomains()}:
        dom = conn.lookupByName(spec["name"])
        if dom.isActive():
            dom.destroy()
        dom.undefine()

    # overlay volume
    vol_name = f"{spec['name']}.qcow2"
    if POOL_NAME in {p.name() for p in conn.listAllStoragePools()}:
        pool = conn.storagePoolLookupByName(POOL_NAME)
        if vol_name in {v.name() for v in pool.listAllVolumes()}:
            pool.storageVolLookupByName(vol_name).delete(0)

    # seed
    seed_path = f"{LAB_DIR}/{spec['name']}-seed.iso"
    if os.path.exists(seed_path):
        os.remove(seed_path)
