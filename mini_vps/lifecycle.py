"""VM のプロビジョニングと削除。"""

import time

import libvirt

from .config import POOL_NAME, SEED_POOL_NAME
from .resources import (
    _filter_name,
    _network_name,
    build_domain_xml,
    build_nwfilter_xml,
    build_seed_iso,
    create_overlay_volume,
)
from .spec import read_pubkey


def ensure_network_active(conn, spec) -> None:
    """VM スペックが参照する networks それぞれについて、非アクティブなら起動する。"""
    for network in spec["networks"]:
        net = conn.networkLookupByName(_network_name(network))
        if not net.isActive():
            net.create()


def provision(conn, spec, secrets: dict[str, str] | None = None) -> libvirt.virDomain:
    """VM を定義し、未起動の domain を返す。

    nwfilter(任意) → seed → overlay → domain XML → defineXML の順に処理する。
    起動は呼び出し側が行う(起動前に metadata を付与するため)。seed を overlay
    より先に作るのは、secrets 不足を安価に検知するため。
    """
    ensure_network_active(conn, spec)

    filter_name = None
    if spec.get("filters") is not None:
        conn.nwfilterDefineXML(build_nwfilter_xml(spec))
        filter_name = _filter_name(spec)

    seed_path = build_seed_iso(conn, spec, read_pubkey(), secrets=secrets)
    overlay_path = create_overlay_volume(conn, spec)
    xml = build_domain_xml(spec, overlay_path, seed_path, filter_name=filter_name)
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
    """DHCP リースをポーリングし、IPv4 が確定するまで待つ(タイムアウト時は None)。"""
    start_time = time.time()
    while time.time() - start_time < timeout:
        ip = _lease_ipv4(dom)
        if ip is not None:
            return ip
        time.sleep(2)
    return None


def teardown(conn, spec) -> None:
    """VM を後始末する(spec は name キーのみ参照する)。

    destroy → undefine → nwfilter 削除 → overlay volume 削除 → seed ISO 削除の順。
    """
    # domain
    if spec["name"] in {d.name() for d in conn.listAllDomains()}:
        dom = conn.lookupByName(spec["name"])
        if dom.isActive():
            dom.destroy()
        # UEFI ドメインは per-VM の nvram ファイルを持つため、フラグ無しの undefine()
        # だと失敗する。このフラグは nvram の無い(legacy BIOS の)ドメインに対しては
        # no-op なので、既存ドメインとの後方互換は保たれる。
        dom.undefineFlags(libvirt.VIR_DOMAIN_UNDEFINE_NVRAM)

    # nwfilter は使用中(domain にアタッチ中)は undefine できないため、domain の
    # undefine 後、かつ domain ブロックとは独立に判定する(provision 内で
    # nwfilterDefineXML だけ成功し以降が失敗したロールバック経路でも回収できるように)。
    filter_name = _filter_name(spec)
    if filter_name in {f.name() for f in conn.listAllNWFilters()}:
        conn.nwfilterLookupByName(filter_name).undefine()

    # overlay volume
    vol_name = f"{spec['name']}.qcow2"
    if POOL_NAME in {p.name() for p in conn.listAllStoragePools()}:
        pool = conn.storagePoolLookupByName(POOL_NAME)
        if vol_name in {v.name() for v in pool.listAllVolumes()}:
            pool.storageVolLookupByName(vol_name).delete(0)

    # seed
    seed_vol_name = f"{spec['name']}-seed.iso"
    if SEED_POOL_NAME in {p.name() for p in conn.listAllStoragePools()}:
        seed_pool = conn.storagePoolLookupByName(SEED_POOL_NAME)
        if seed_vol_name in {v.name() for v in seed_pool.listAllVolumes()}:
            seed_pool.storageVolLookupByName(seed_vol_name).delete(0)
