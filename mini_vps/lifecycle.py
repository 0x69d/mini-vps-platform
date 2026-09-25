"""VM のプロビジョニングと削除。"""

import logging
import time

import libvirt

from .config import POOL_NAME, SEED_POOL_NAME
from .platform_profile import NETWORK_USER, get_profile
from .resources import (
    _filter_name,
    _network_name,
    allocate_ssh_port,
    build_domain_xml,
    build_nwfilter_xml,
    build_seed_iso,
    create_overlay_volume,
    ssh_forward_port,
)
from .snapshots import is_snapshot_file
from .spec import read_pubkey

_LOGGER = logging.getLogger(__name__)


def ensure_network_active(conn, spec) -> None:
    """VM スペックが参照する networks それぞれについて、非アクティブなら起動する。

    user-mode ネットワーク(macOS)では libvirt のネットワークを使わないため何もしない。
    """
    if get_profile().network_mode == NETWORK_USER:
        return
    for network in spec["networks"]:
        name = _network_name(network)
        net = conn.networkLookupByName(name)
        if not net.isActive():
            net.create()
            _LOGGER.info("network %s を起動した", name)


def provision(conn, spec, secrets: dict[str, str] | None = None) -> libvirt.virDomain:
    """VM を定義し、未起動の domain を返す。

    nwfilter(任意) → seed → overlay → domain XML → defineXML → autostart の順に
    処理する。起動前に metadata を付与するため、起動は呼び出し側が行う。seed を
    overlay より先に作るのは、secrets 不足を安価に検知するため。

    user-mode ネットワーク(macOS)では SSH を転送するホストポートをここで割り当て、
    domain XML に書き込む。
    """
    name = spec["name"]
    profile = get_profile()
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

    ssh_port = None
    if profile.network_mode == NETWORK_USER:
        ssh_port = allocate_ssh_port(used_ssh_ports(conn), profile.ssh_port_range)
        _LOGGER.info("%s: SSH を 127.0.0.1:%d へ転送", name, ssh_port)

    xml = build_domain_xml(
        spec,
        overlay_path,
        seed_path,
        filter_name=filter_name,
        profile=profile,
        ssh_port=ssh_port,
    )
    dom = conn.defineXML(xml)
    _LOGGER.info("%s: domain を define", name)
    dom.setAutostart(1 if spec.get("autostart", True) else 0)
    return dom


def used_ssh_ports(conn) -> set[int]:
    """全 domain(管理外・停止中を含む)に割り当て済みの SSH 転送ポートを集める。"""
    ports = set()
    for dom in conn.listAllDomains():
        port = ssh_forward_port(dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE))
        if port is not None:
            ports.add(port)
    return ports


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
    overlay volume には `{name}.qcow2` のほか、スナップショットの overlay
    `{name}.snap-*.qcow2`(snapshots.py)も含む。
    """
    name = spec["name"]

    # domain
    if name in {d.name() for d in conn.listAllDomains()}:
        dom = conn.lookupByName(name)
        if dom.isActive():
            dom.destroy()
        # UEFI ドメインは per-VM の nvram ファイルを持つため、フラグ無しの undefine()
        # だと失敗する。このフラグは nvram の無い(legacy BIOS の)ドメインに対しては
        # no-op なので、既存ドメインとの後方互換は保たれる。スナップショットの
        # メタデータが残っている domain も、SNAPSHOTS_METADATA が無いと undefine に
        # 失敗する(スナップショットが無ければ no-op)。
        dom.undefineFlags(
            libvirt.VIR_DOMAIN_UNDEFINE_NVRAM
            | libvirt.VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA
        )
        _LOGGER.info("%s: domain を undefine", name)

    # nwfilter は使用中(domain にアタッチ中)は undefine できないため、domain の
    # undefine 後、かつ domain ブロックとは独立に判定する。provision 内で
    # nwfilterDefineXML だけ成功し以降が失敗したロールバック経路でも回収するため。
    filter_name = _filter_name(spec)
    if get_profile().supports_nwfilter and filter_name in {
        f.name() for f in conn.listAllNWFilters()
    }:
        conn.nwfilterLookupByName(filter_name).undefine()
        _LOGGER.info("%s: nwfilter %s を削除", name, filter_name)

    # overlay volume(スナップショットの overlay を含む)。スナップショットの overlay は
    # libvirt がプールの API を通さずに作るため、refresh してから一覧を取る。
    if POOL_NAME in {p.name() for p in conn.listAllStoragePools()}:
        pool = conn.storagePoolLookupByName(POOL_NAME)
        pool.refresh(0)
        vol_names = sorted(
            v.name()
            for v in pool.listAllVolumes()
            if v.name() == f"{name}.qcow2" or is_snapshot_file(name, v.name())
        )
        for vol_name in vol_names:
            pool.storageVolLookupByName(vol_name).delete(0)
            _LOGGER.info("%s: overlay volume %s を削除", name, vol_name)

    # seed
    seed_vol_name = f"{name}-seed.iso"
    if SEED_POOL_NAME in {p.name() for p in conn.listAllStoragePools()}:
        seed_pool = conn.storagePoolLookupByName(SEED_POOL_NAME)
        if seed_vol_name in {v.name() for v in seed_pool.listAllVolumes()}:
            seed_pool.storageVolLookupByName(seed_vol_name).delete(0)
            _LOGGER.info("%s: seed ISO %s を削除", name, seed_vol_name)
