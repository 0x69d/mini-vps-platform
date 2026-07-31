"""VM のプロビジョニングと削除。"""

import logging
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

_LOGGER = logging.getLogger(__name__)


def ensure_network_active(conn, spec) -> None:
    """VM スペックが参照する networks それぞれについて、非アクティブなら起動する。"""
    for network in spec["networks"]:
        name = _network_name(network)
        net = conn.networkLookupByName(name)
        if not net.isActive():
            net.create()
            _LOGGER.info("network %s を起動した", name)


def provision(conn, spec, secrets: dict[str, str] | None = None) -> libvirt.virDomain:
    """VM を定義し、未起動の domain を返す。

    nwfilter(任意) → seed → overlay → domain XML → defineXML の順に処理する。
    起動前に metadata を付与するため、起動は呼び出し側が行う。seed を overlay
    より先に作るのは、secrets 不足を安価に検知するため。
    """
    name = spec["name"]
    ensure_network_active(conn, spec)

    filter_name = None
    if spec.get("filters") is not None:
        conn.nwfilterDefineXML(build_nwfilter_xml(spec))
        filter_name = _filter_name(spec)
        _LOGGER.info("%s: nwfilter %s を定義", name, filter_name)

    seed_path = build_seed_iso(conn, spec, read_pubkey(), secrets=secrets)
    _LOGGER.info("%s: seed ISO を生成 %s", name, seed_path)

    overlay_path = create_overlay_volume(conn, spec)
    _LOGGER.info("%s: overlay volume を作成 %s", name, overlay_path)

    xml = build_domain_xml(spec, overlay_path, seed_path, filter_name=filter_name)
    dom = conn.defineXML(xml)
    _LOGGER.info("%s: domain を define", name)
    return dom


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
    name = dom.name()
    _LOGGER.debug("%s: DHCP リースの待機を開始 timeout=%ds", name, timeout)
    start_time = time.time()
    while time.time() - start_time < timeout:
        ip = _lease_ipv4(dom)
        if ip is not None:
            _LOGGER.info(
                "%s: IPv4 が確定 %s (%.1fs)", name, ip, time.time() - start_time
            )
            return ip
        time.sleep(2)
    _LOGGER.warning("%s: %ds 待っても DHCP リースを取得できなかった", name, timeout)
    return None


def teardown(conn, spec) -> None:
    """VM を後始末する(spec は name キーのみ参照する)。

    destroy → undefine → nwfilter 削除 → overlay volume 削除 → seed ISO 削除の順。
    """
    name = spec["name"]

    # domain
    if name in {d.name() for d in conn.listAllDomains()}:
        dom = conn.lookupByName(name)
        if dom.isActive():
            dom.destroy()
        # UEFI ドメインは per-VM の nvram ファイルを持つため、フラグ無しの undefine()
        # だと失敗する。このフラグは nvram の無い(legacy BIOS の)ドメインに対しては
        # no-op なので、既存ドメインとの後方互換は保たれる。
        dom.undefineFlags(libvirt.VIR_DOMAIN_UNDEFINE_NVRAM)
        _LOGGER.info("%s: domain を undefine", name)

    # nwfilter は使用中(domain にアタッチ中)は undefine できないため、domain の
    # undefine 後、かつ domain ブロックとは独立に判定する。provision 内で
    # nwfilterDefineXML だけ成功し以降が失敗したロールバック経路でも回収するため。
    filter_name = _filter_name(spec)
    if filter_name in {f.name() for f in conn.listAllNWFilters()}:
        conn.nwfilterLookupByName(filter_name).undefine()
        _LOGGER.info("%s: nwfilter %s を削除", name, filter_name)

    # overlay volume
    vol_name = f"{name}.qcow2"
    if POOL_NAME in {p.name() for p in conn.listAllStoragePools()}:
        pool = conn.storagePoolLookupByName(POOL_NAME)
        if vol_name in {v.name() for v in pool.listAllVolumes()}:
            pool.storageVolLookupByName(vol_name).delete(0)
            _LOGGER.info("%s: overlay volume %s を削除", name, vol_name)

    # seed
    seed_vol_name = f"{name}-seed.iso"
    if SEED_POOL_NAME in {p.name() for p in conn.listAllStoragePools()}:
        seed_pool = conn.storagePoolLookupByName(SEED_POOL_NAME)
        if seed_vol_name in {v.name() for v in seed_pool.listAllVolumes()}:
            seed_pool.storageVolLookupByName(seed_vol_name).delete(0)
            _LOGGER.info("%s: seed ISO %s を削除", name, seed_vol_name)
