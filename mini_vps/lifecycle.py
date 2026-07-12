"""VM のプロビジョニングと削除。"""

import time

import libvirt

from .config import POOL_NAME, SEED_POOL_NAME
from .resources import (
    _filter_name,
    build_domain_xml,
    build_nwfilter_xml,
    build_seed_iso,
    create_overlay_volume,
    is_ovs_network_xml,
)
from .spec import read_pubkey


class FiltersUnsupported(Exception):
    """VM spec の filters が対象ネットワーク上では実施できないことを表す。

    nwfilter は ebtables/Linux ブリッジ前提のため、OVS ブリッジ接続の
    ネットワークでは定義できても実際には効かない(素通しになる)。黙って
    無防備な VM を作らないために fail-loud に拒否する。将来 OVS フロー
    (OpenFlow)ベースのフィルタを実装する場合は ensure_filters_enforceable
    の中で方式を選択・適用する(呼び出し側は変更しない)。
    """


def ensure_network_active(conn, spec) -> None:
    """VM スペックが参照する network が非アクティブなら起動する。"""
    net = conn.networkLookupByName(spec.get("network", "default"))
    if not net.isActive():
        net.create()


def is_ovs_network(conn, name: str) -> bool:
    """指定名の libvirt ネットワークが OVS ブリッジ接続かを判定する。

    未定義のネットワーク名は libvirtError をそのまま伝播させる
    (ensure_network_active と同じ意味論)。
    """
    return is_ovs_network_xml(conn.networkLookupByName(name).XMLDesc(0))


def ensure_filters_enforceable(conn, spec) -> None:
    """VM spec の filters が対象ネットワーク上で実際に効くことを保証する。

    filters 未指定(None)なら何もしない。filters=[] は「全 inbound 拒否」
    という有効なフィルタ指定のため検証対象に含める。

    Raises:
        FiltersUnsupported: filters 指定があり network が OVS 接続の場合。
    """
    if spec.get("filters") is None:
        return
    network = spec.get("network", "default")
    if is_ovs_network(conn, network):
        raise FiltersUnsupported(
            f"filters cannot be enforced on OVS network: {network}"
        )


def provision(conn, spec, secrets: dict[str, str] | None = None) -> libvirt.virDomain:
    """VM を定義し、未起動の domain を返す。

    nwfilter(任意) → seed → overlay → domain XML → defineXML の順に処理する。
    起動は呼び出し側が行う(起動前に metadata を付与するため)。seed を overlay
    より先に作るのは、secrets 不足を安価に検知するため。同様に filters の
    実施可能性はリソースを一切作る前に検証する。

    Raises:
        FiltersUnsupported: filters 指定があり network が OVS 接続の場合。
    """
    ensure_filters_enforceable(conn, spec)
    ensure_network_active(conn, spec)

    filter_name = None
    if spec.get("filters") is not None:
        conn.nwfilterDefineXML(build_nwfilter_xml(spec))
        filter_name = _filter_name(spec)

    seed_path = build_seed_iso(conn, spec, read_pubkey(), secrets=secrets)
    overlay_path = create_overlay_volume(conn, spec)
    xml = build_domain_xml(spec, overlay_path, seed_path, filter_name=filter_name)
    return conn.defineXML(xml)


def _first_ipv4(ifaces: dict) -> str | None:
    """インターフェース一覧(interfaceAddresses の戻り値)から最初の IPv4 を返す。

    SRC_AGENT はゲストの全インターフェース(loopback 含む)を返すため、
    インターフェース名 "lo" と 127.0.0.0/8 のアドレスを除外する。
    """
    for name, iface in ifaces.items():
        if name == "lo":
            continue
        for addr in iface["addrs"] or []:
            if addr["type"] == libvirt.VIR_IP_ADDR_TYPE_IPV4 and not addr[
                "addr"
            ].startswith("127."):
                return addr["addr"]
    return None


def _agent_ipv4(dom: libvirt.virDomain) -> str | None:
    """qemu-guest-agent 経由で IPv4 を1回だけ取得する。

    agent 未接続・未応答のエラーコードは環境により揺れるため、コードを判別せず
    「agent 経路の失敗 = None」と扱う(呼び出し側がリースへフォールバックする)。
    """
    try:
        ifaces = dom.interfaceAddresses(
            libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_AGENT
        )
    except libvirt.libvirtError:
        return None
    return _first_ipv4(ifaces)


def _lease_ipv4(dom: libvirt.virDomain) -> str | None:
    """DHCP リースから IPv4 を1回だけ取得する。

    libvirt が NIC(MAC) に紐づくリースだけを返すため、古いリースを掴まない。
    """
    ifaces = dom.interfaceAddresses(libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE)
    return _first_ipv4(ifaces)


def get_domain_ipv4(dom: libvirt.virDomain) -> str | None:
    """qemu-guest-agent 優先・リースフォールバックで IPv4 を1回だけ取得する。

    OVS ブリッジ接続の VM は libvirt が DHCP リースを持たないため agent が
    唯一の情報源。一方、チャネルを持たない既存 VM や agent 起動前のブート初期は
    libvirt 管理ネットワーク上ならリースで取得できるため、両者を直列に試す。
    """
    return _agent_ipv4(dom) or _lease_ipv4(dom)


def wait_for_ip(dom: libvirt.virDomain, timeout=300) -> str | None:
    """IPv4 が確定するまでポーリングして待つ(タイムアウト時は None)。

    OVS セグメントでは cloud-init による qemu-guest-agent の導入完了後に
    初めて IP を観測できるため、既定タイムアウトはリースのみだった頃の
    120 秒より長い 300 秒とする。
    """
    start_time = time.time()
    while time.time() - start_time < timeout:
        ip = get_domain_ipv4(dom)
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
