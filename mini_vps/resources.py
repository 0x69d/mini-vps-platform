"""ストレージプール・volume・ISO・domain XML のリソース生成。"""

import hashlib
import io
import ipaddress
import logging
import re
import socket
import xml.etree.ElementTree as ET

import libvirt
import pycdlib
import yaml

from .config import (
    BALLOON_STATS_PERIOD_SECONDS,
    BASE_POOL,
    GUEST_AGENT_CHANNEL,
    META_DATA_TEMPLATE,
    NWFILTER_EGRESS_HEAD_RULES,
    NWFILTER_EGRESS_PRIORITY_START,
    NWFILTER_EGRESS_RULE_TEMPLATE,
    NWFILTER_EGRESS_TAIL_RULES,
    NWFILTER_INBOUND_ACCEPT_ALL_RULE,
    NWFILTER_INBOUND_DROP_RULE,
    NWFILTER_OUTBOUND_ACCEPT_ALL_RULE,
    NWFILTER_PORT_RULE_TEMPLATE,
    NWFILTER_XML_TEMPLATE,
    OVERLAY_VOL_XML_TEMPLATE,
    POOL_NAME,
    POOL_XML_TEMPLATE,
    QEMU_XML_NS,
    SEED_POOL_NAME,
    SEED_VOL_XML_TEMPLATE,
    STATIC_ROUTES_EXEC_LINE_TEMPLATE,
    STATIC_ROUTES_UNIT_NAME,
    STATIC_ROUTES_UNIT_PATH,
    STATIC_ROUTES_UNIT_TEMPLATE,
)
from .platform_profile import NETWORK_USER, HostProfile, get_profile
from .startup_scripts import render_startup_script

_LOGGER = logging.getLogger(__name__)

# resize_domain_xml などで ElementTree が XML を再シリアライズするとき、
# qemu:commandline の接頭辞を ns0 などに書き換えないようにする。
ET.register_namespace("qemu", QEMU_XML_NS)

# QEMU/libvirt が自動生成する MAC で慣習的に使う locally-administered なプレフィックス。
_MAC_PREFIX = "52:54:00"


def _mac_for_interface(name: str, index: int) -> str:
    """VM名とNICインデックスから決定的なMACアドレスを生成する。

    build_seed_iso() の network-config 生成が build_domain_xml() より先に走る
    ため、MACをlibvirtの自動生成に委ねられず、Python側で決定的に確定させる必要がある。
    組み込み hash() はプロセスごとにランダム化され、CLI/API/exporter が別プロセスで
    動くと決定性が壊れるため使えない。代わりに sha256 を使う。
    """
    digest = hashlib.sha256(f"{name}/{index}".encode()).digest()
    suffix = ":".join(f"{b:02x}" for b in digest[:3])
    return f"{_MAC_PREFIX}:{suffix}"


def _network_name(net) -> str:
    """NIC1件分の networks 要素からネットワーク名を取り出す。

    文字列(DHCP)ならそのまま返し、NetworkAttachment の dict(静的IP)なら
    name キーを返す。
    """
    return net if isinstance(net, str) else net["name"]


def _has_static_network(spec) -> bool:
    """spec["networks"] に静的IPを持つNIC(NetworkAttachment)が1つでもあるか判定する。"""
    return any(not isinstance(net, str) for net in spec["networks"])


def _build_network_config(spec) -> dict:
    """spec["networks"] から cloud-init network-config v2 の dict を組み立てる。

    静的IPを持つNICが1つでもある場合にのみ呼ばれる想定。network-config を
    cloud-init に渡すとそれが唯一の設定源になり、記載の無いNICは一切設定されなく
    なるため、DHCPの文字列要素も含めて全NICをMACマッチで列挙する。gatewayが
    指定されている場合のみ default route を追加する。nameservers / search も
    非空の場合のみ netplan v2 の nameservers に出力する。
    """
    ethernets = {}
    for index, net in enumerate(spec["networks"]):
        mac = _mac_for_interface(spec["name"], index)
        iface_key = f"eth{index}"
        if isinstance(net, str):
            ethernets[iface_key] = {"match": {"macaddress": mac}, "dhcp4": True}
        else:
            entry = {"match": {"macaddress": mac}, "addresses": [net["address"]]}
            if net.get("gateway"):
                entry["routes"] = [{"to": "default", "via": net["gateway"]}]
            nameservers = {}
            if net.get("nameservers"):
                nameservers["addresses"] = net["nameservers"]
            if net.get("search"):
                nameservers["search"] = net["search"]
            if nameservers:
                entry["nameservers"] = nameservers
            ethernets[iface_key] = entry
    return {"network": {"version": 2, "ethernets": ethernets}}


def ensure_pool(conn, name, xml) -> libvirt.virStoragePool:
    """ストレージプールが無ければ xml で作成し、アクティブ状態で返す(冪等)。

    define → build → create → autostart の順にセットアップする。
    """
    pools = {p.name() for p in conn.listAllStoragePools()}
    if name in pools:
        pool = conn.storagePoolLookupByName(name)
        if not pool.isActive():
            pool.create(0)
        return pool
    pool = conn.storagePoolDefineXML(xml, 0)
    pool.build(0)
    pool.create(0)
    pool.setAutostart(1)
    _LOGGER.info("ストレージプール %s を作成した", name)
    return pool


def ensure_seed_pool(conn) -> libvirt.virStoragePool:
    """Seed ISO 用の dir 型ストレージプールが無ければ作成し、アクティブ状態で返す。"""
    xml = POOL_XML_TEMPLATE.format(name=SEED_POOL_NAME, path=get_profile().seed_dir)
    return ensure_pool(conn, SEED_POOL_NAME, xml)


def ensure_vps_pool(conn) -> libvirt.virStoragePool:
    """Overlay volume 用の dir 型プールが無ければ作成し、アクティブ状態で返す。"""
    xml = POOL_XML_TEMPLATE.format(name=POOL_NAME, path=get_profile().pool_path)
    return ensure_pool(conn, POOL_NAME, xml)


def create_overlay_volume(conn, spec) -> str:
    """専用プールに overlay volume を作成し、そのパスを返す。

    base image を backing store として使用する。既存の同名 volume は削除して再作成する。
    """
    base_pool = conn.storagePoolLookupByName(BASE_POOL)
    base_pool.refresh(0)
    base_path = base_pool.storageVolLookupByName(spec["base_image"]).path()

    _LOGGER.debug("%s: base image %s", spec["name"], base_path)

    pool = ensure_vps_pool(conn)
    vol_name = f"{spec['name']}.qcow2"

    if vol_name in {v.name() for v in pool.listAllVolumes()}:
        pool.storageVolLookupByName(vol_name).delete(0)
        _LOGGER.debug("%s: 既存 overlay volume を削除して作り直す", spec["name"])

    xml = OVERLAY_VOL_XML_TEMPLATE.format(
        name=spec["name"], disk=spec["disk"], base_path=base_path
    )
    return pool.createXML(xml, 0).path()


def _build_static_routes_fragment(spec) -> dict:
    """static_routes から systemd ユニットの cloud-init フラグメントを組み立てる。

    ip route add ではなく再起動のたびに再適用する systemd ユニット化により、
    runcmd(初回起動時のみ実行)では失われる永続化を実現する。
    """
    exec_lines = "\n".join(
        STATIC_ROUTES_EXEC_LINE_TEMPLATE.format(
            destination=route["destination"], via=route["via"]
        )
        for route in spec["static_routes"]
    )
    unit_content = STATIC_ROUTES_UNIT_TEMPLATE.format(exec_lines=exec_lines)
    write_files = [
        {
            "path": STATIC_ROUTES_UNIT_PATH,
            "permissions": "0644",
            "content": unit_content,
        }
    ]
    runcmd = [
        "systemctl daemon-reload",
        f"systemctl enable --now {STATIC_ROUTES_UNIT_NAME}",
    ]
    return {"write_files": write_files, "runcmd": runcmd}


_GUEST_AGENT_START_CMD = [
    "sh",
    "-c",
    "systemctl enable --now qemu-guest-agent || systemctl start qemu-guest-agent",
]


def _build_user_data(spec, pubkey, secrets: dict[str, str] | None) -> dict:
    """cloud-config の dict(YAML 化前)を組み立てる。

    hostname/users と qemu-guest-agent の導入は常に含める。spec["startup_script"] と
    spec["static_routes"] はそれぞれ独立に write_files/runcmd フラグメントを生成し、
    guest agent の起動コマンドの後ろに連結する。

    qemu-guest-agent は exec(SSH 無しのコマンド実行)とゲストからの IP 取得に使う。
    Ubuntu の cloud image には入っていないため packages で導入する。Ubuntu の
    ユニットは udev 起動の static ユニットで enable が失敗しうるため、start に
    フォールバックする。
    """
    data = {
        "hostname": spec["hostname"],
        "users": [
            {
                "name": spec["user"],
                "sudo": "ALL=(ALL) NOPASSWD:ALL",
                "shell": "/bin/bash",
                "ssh_authorized_keys": [pubkey],
            }
        ],
    }

    data["packages"] = ["qemu-guest-agent"]

    write_files = []
    runcmd = [_GUEST_AGENT_START_CMD]

    startup_script = spec.get("startup_script")
    if startup_script:
        fragment = render_startup_script(startup_script, spec, secrets)
        write_files += fragment["write_files"]
        runcmd += fragment["runcmd"]

    if spec.get("static_routes"):
        fragment = _build_static_routes_fragment(spec)
        write_files += fragment["write_files"]
        runcmd += fragment["runcmd"]

    if write_files:
        data["write_files"] = write_files
    data["runcmd"] = runcmd
    return data


def render_seed_files(
    spec, pubkey, secrets: dict[str, str] | None = None
) -> dict[str, bytes]:
    """Seed ISO に入れる cloud-init のファイル群を組み立てる(外部依存ゼロ)。

    user-data と meta-data は常に含める。静的IPを持つNICが1つでもあれば、全NICを
    列挙した network-config も含める(無ければ cloud-init の既定の DHCP に任せる)。

    Returns:
        ファイル名("user-data" など)から中身への dict。secrets を含みうるため、
        戻り値をログに出してはならない。
    """
    user_data = "#cloud-config\n" + yaml.safe_dump(
        _build_user_data(spec, pubkey, secrets), sort_keys=False
    )
    meta_data = META_DATA_TEMPLATE.format(name=spec["name"], hostname=spec["hostname"])
    files = {
        "user-data": user_data.encode(),
        "meta-data": meta_data.encode(),
    }
    if _has_static_network(spec):
        files["network-config"] = yaml.safe_dump(
            _build_network_config(spec), sort_keys=False
        ).encode()
    return files


def build_seed_iso_bytes(files: dict[str, bytes]) -> bytes:
    """cloud-init NoCloud の seed ISO(ボリュームラベル cidata)をメモリ上で作る。

    cloud-localds(genisoimage -volid cidata -joliet -rock)と同じ構成を純 Python の
    pycdlib で作る。Linux の cloud-image-utils に依存しないため macOS でも動く。
    ISO9660 のファイル名は 8.3 形式に制限されるため、ゲストからは Rock Ridge / Joliet
    の長いファイル名("user-data" など)で見える。

    Args:
        files: ファイル名から中身への dict(render_seed_files の戻り値)。

    Returns:
        ISO イメージのバイト列。
    """
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=3, rock_ridge="1.09", vol_ident="cidata")
    for index, (file_name, content) in enumerate(sorted(files.items())):
        iso.add_fp(
            io.BytesIO(content),
            len(content),
            f"/FILE{index}.;1",
            rr_name=file_name,
            joliet_path=f"/{file_name}",
        )
    out = io.BytesIO()
    iso.write_fp(out)
    iso.close()
    return out.getvalue()


def build_seed_iso(conn, spec, pubkey, secrets: dict[str, str] | None = None) -> str:
    """Seed ISO を生成し、seed 用ストレージプールに配置してそのパスを返す。

    ISO はメモリ上で作り(build_seed_iso_bytes)、libvirt の volume API
    (createXML + upload)で seed 用プールへ配置する。一時ファイルを作らないため、
    secrets を含む user-data がホストのディスクに平文で残る時間が無い。
    secrets はこの user-data 生成にのみ使う。
    """
    files = render_seed_files(spec, pubkey, secrets)
    iso_bytes = build_seed_iso_bytes(files)
    vol_name = f"{spec['name']}-seed.iso"
    # ファイル名だけを出す。中身は secrets を含みうる。
    _LOGGER.debug("%s: seed ISO を生成 files=%s", spec["name"], sorted(files))

    pool = ensure_seed_pool(conn)
    if vol_name in {v.name() for v in pool.listAllVolumes()}:
        pool.storageVolLookupByName(vol_name).delete(0)

    vol = pool.createXML(
        SEED_VOL_XML_TEMPLATE.format(name=vol_name, capacity_bytes=len(iso_bytes)), 0
    )

    stream = conn.newStream(0)
    vol.upload(stream, 0, len(iso_bytes), 0)
    view = memoryview(iso_bytes)
    offset = 0
    try:
        while offset < len(view):
            sent = stream.send(view[offset : offset + _UPLOAD_CHUNK].tobytes())
            if sent <= 0:
                raise OSError(f"seed ISO の upload が進まない(sent={sent})")
            offset += sent
    except Exception:
        stream.abort()
        raise
    stream.finish()

    return vol.path()


_UPLOAD_CHUNK = 256 * 1024


def _filter_name(spec) -> str:
    """VM の name から決定的な nwfilter 名を作る。"""
    return f"minivps-{spec['name']}"


def needs_nwfilter(spec) -> bool:
    """VM 専用の nwfilter(と interface の filterref)が spec に必要かを返す。

    filters と egress がどちらも None のときだけ不要(inbound も outbound も全許可)。
    空リストは「全拒否」を意味するため必要側に入る(truthy 判定にしないこと)。
    provision・_converge・planning がこの1つの判定を共有する。
    """
    return spec.get("filters") is not None or spec.get("egress") is not None


def _egress_rule_xml(rule: dict, priority: int) -> str:
    """Egress ルール1件を nwfilter の <rule> に変換する。

    宛先が 0.0.0.0/0 のときは dstipaddr/dstipmask を省く(全宛先に一致させる)。
    dstipmask は libvirt の IP_MASK 型で、CIDR の接頭辞長(0-32)をそのまま書ける。
    """
    network = ipaddress.IPv4Network(rule["cidr"])
    attrs = ""
    if network.prefixlen != 0:
        attrs += (
            f" dstipaddr='{network.network_address}' dstipmask='{network.prefixlen}'"
        )
    if rule.get("port") is not None:
        attrs += f" dstportstart='{int(rule['port'])}'"
    return NWFILTER_EGRESS_RULE_TEMPLATE.format(
        action=rule.get("action", "accept"),
        priority=priority,
        protocol=rule.get("protocol", "all"),
        attrs=attrs,
    )


def build_nwfilter_xml(spec, uuid: str | None = None) -> str:
    """VM 専用の nwfilter XML を filters(inbound)と egress(outbound)から作る。

    呼び出し側で needs_nwfilter(spec) を確認済みであることが前提。uuid は既存の
    同名 filter を再定義するときに渡す(define_nwfilter 参照)。

    inbound: filters がリストなら宣言ポートだけ accept して残りを drop、None なら
    全 accept。outbound: egress が None なら全 accept(従来どおり)、リストなら
    戻り通信・DHCP を先に accept し、egress ルールを記述順に単調増加する priority で
    並べ、最後に既定 drop を置く。nwfilter は記述順ではなく priority 昇順で評価する
    ため、egress の順序は priority で表す。
    """
    filters = spec.get("filters")
    egress = spec.get("egress")

    rules = ""
    if filters is not None:
        rules += "".join(
            NWFILTER_PORT_RULE_TEMPLATE.format(protocol=f["protocol"], port=f["port"])
            for f in filters
        )
    elif egress is not None:
        rules += NWFILTER_INBOUND_ACCEPT_ALL_RULE

    if egress is None:
        rules += NWFILTER_OUTBOUND_ACCEPT_ALL_RULE
    else:
        rules += NWFILTER_EGRESS_HEAD_RULES
        rules += "".join(
            _egress_rule_xml(rule, NWFILTER_EGRESS_PRIORITY_START + index)
            for index, rule in enumerate(egress)
        )
        rules += NWFILTER_EGRESS_TAIL_RULES

    if filters is not None:
        rules += NWFILTER_INBOUND_DROP_RULE

    uuid_xml = f"\n  <uuid>{uuid}</uuid>" if uuid else ""
    return NWFILTER_XML_TEMPLATE.format(
        name=_filter_name(spec), uuid=uuid_xml, rules=rules
    )


def define_nwfilter(conn, spec) -> str:
    """VM 専用の nwfilter を定義(既にあれば同じ UUID で再定義)し、名前を返す。

    libvirt は UUID の無い定義を「新しい filter」とみなすため、同名の filter が既に
    あると "already exists with uuid" で拒否する。既存の filter の UUID を引き継いで
    再定義すれば、libvirt はその filter を更新し、参照している稼働中の VM の
    インターフェースにも反映する。
    """
    name = _filter_name(spec)
    uuid = None
    if name in {f.name() for f in conn.listAllNWFilters()}:
        uuid = conn.nwfilterLookupByName(name).UUIDString()
    conn.nwfilterDefineXML(build_nwfilter_xml(spec, uuid=uuid))
    return name


# libvirt の ARCH_IS_X86 に相当する arch 名。<pm> を出してよいかの判定に使う。
_X86_ARCHES = frozenset({"x86_64", "i686"})


def _sub(parent: ET.Element, tag: str, text: str | None = None, **attrs):
    """属性値が None のものを落として子要素を作る(ElementTree の小さな補助)。"""
    el = ET.SubElement(parent, tag, {k: v for k, v in attrs.items() if v is not None})
    if text is not None:
        el.text = text
    return el


def build_domain_xml(
    spec,
    overlay_path,
    seed_path,
    filter_name=None,
    profile: HostProfile | None = None,
    ssh_port: int | None = None,
) -> str:
    """Domain XML 文字列を ElementTree で組み立てて返す(外部依存ゼロ)。

    domain type・アーキテクチャ・machine・CPU・ネットワーク方式は HostProfile で
    決まる。libvirt ネットワーク方式では spec["networks"] の要素数だけ <interface>
    を生成し(複数NIC対応)、各NICには (name, index) から決定的に導出したMACを
    埋め込む。filter_name は全 interface に紐づける nwfilter 名。

    user-mode ネットワーク方式(macOS)では libvirt のネットワークを使わず、QEMU の
    -netdev user を qemu:commandline で直接渡し、ゲストの 22 番を
    127.0.0.1:ssh_port へ転送する。libvirt の <interface type='user'> は slirp
    バックエンドでポート転送を指定できないため。

    Args:
        spec: VM スペック。
        overlay_path: ルートディスク(overlay volume)のパス。
        seed_path: seed ISO のパス。
        filter_name: nwfilter 名(None ならフィルタ無し)。
        profile: HostProfile。None なら get_profile()。
        ssh_port: user-mode ネットワークで SSH を転送するホストポート。
    """
    profile = profile or get_profile()
    root = ET.Element("domain", type=profile.domain_type)
    _sub(root, "name", spec["name"])
    _sub(root, "memory", str(spec["memory"] * 1024), unit="KiB")
    _sub(root, "vcpu", str(spec["vcpus"]))
    if profile.cpu_mode:
        _sub(root, "cpu", mode=profile.cpu_mode)

    os_el = _sub(root, "os", firmware="efi")
    _sub(os_el, "type", "hvm", arch=profile.arch, machine=profile.machine)
    _sub(os_el, "loader", secure="no")
    _sub(os_el, "boot", dev="hd")

    features = _sub(root, "features")
    _sub(features, "acpi")
    _sub(root, "clock", offset="utc")
    # libvirt は x86 以外で <pm> を指定すると enabled='no' であっても
    # "setting ACPI S3/S4 not supported" で拒否する(qemu_validate.c)。
    # aarch64 の virt machine には S3/S4 がそもそも無いので、x86 のときだけ出す。
    if profile.arch in _X86_ARCHES:
        pm = _sub(root, "pm")
        _sub(pm, "suspend-to-mem", enabled="no")
        _sub(pm, "suspend-to-disk", enabled="no")

    devices = _sub(root, "devices")
    disk = _sub(devices, "disk", type="file", device="disk")
    _sub(
        disk,
        "driver",
        name="qemu",
        type="qcow2",
        discard="unmap",
        cache=profile.disk_cache,
        io=profile.disk_io,
    )
    _sub(disk, "source", file=overlay_path)
    _sub(disk, "target", dev="vda", bus="virtio")

    # aarch64 の virt machine には SATA(AHCI)が無いため、seed は読み取り専用の
    # virtio ディスクとして渡す。cloud-init はデバイス種別ではなくボリュームラベル
    # (cidata)で seed を見つけるため、どちらでも同じように読まれる。
    if profile.arch == "aarch64":
        seed = _sub(devices, "disk", type="file", device="disk")
        _sub(seed, "driver", name="qemu", type="raw")
        _sub(seed, "source", file=seed_path)
        _sub(seed, "target", dev="vdb", bus="virtio")
    else:
        seed = _sub(devices, "disk", type="file", device="cdrom")
        _sub(seed, "driver", name="qemu", type="raw")
        _sub(seed, "source", file=seed_path)
        _sub(seed, "target", dev="sda", bus="sata")
    _sub(seed, "readonly")

    # user-mode では networks == ["default"] であることを planning.check_platform が
    # 保証済みで、NIC は下の qemu:commandline で1枚だけ作る。
    networks = spec["networks"]
    if profile.network_mode != NETWORK_USER:
        for index, net in enumerate(networks):
            iface = _sub(devices, "interface", type="network")
            _sub(iface, "mac", address=_mac_for_interface(spec["name"], index))
            _sub(iface, "source", network=_network_name(net))
            _sub(iface, "model", type="virtio")
            if filter_name:
                _sub(iface, "filterref", filter=filter_name)

    channel = _sub(devices, "channel", type="unix")
    _sub(channel, "target", type="virtio", name=GUEST_AGENT_CHANNEL)

    rng = _sub(devices, "rng", model="virtio")
    _sub(rng, "backend", "/dev/urandom", model="random")
    balloon = _sub(devices, "memballoon", model="virtio")
    _sub(balloon, "stats", period=str(BALLOON_STATS_PERIOD_SECONDS))
    serial = _sub(devices, "serial", type="pty")
    _sub(serial, "target", port="0")
    console = _sub(devices, "console", type="pty")
    _sub(console, "target", type="serial", port="0")

    if profile.network_mode == NETWORK_USER:
        if ssh_port is None:
            raise ValueError("user-mode ネットワークには ssh_port が必要です")
        cmdline = ET.SubElement(root, f"{{{QEMU_XML_NS}}}commandline")
        mac = _mac_for_interface(spec["name"], 0)
        for value in (
            "-netdev",
            f"user,id={_USER_NETDEV_ID},hostfwd=tcp:127.0.0.1:{ssh_port}-:22",
            "-device",
            f"virtio-net-pci,netdev={_USER_NETDEV_ID},mac={mac}",
        ):
            ET.SubElement(cmdline, f"{{{QEMU_XML_NS}}}arg", value=value)

    ET.indent(root)
    return ET.tostring(root, encoding="unicode")


_USER_NETDEV_ID = "minivps0"
_HOSTFWD_SSH_RE = re.compile(r"hostfwd=tcp:127\.0\.0\.1:(\d+)-:22\b")


def ssh_forward_port(xml_text: str) -> int | None:
    """Domain XML から、user-mode ネットワークの SSH 転送ポートを読み取る。

    転送ポートは domain XML(qemu:commandline)そのものが真実源で、metadata には
    別に持たない。二重に持つと食い違いうるため。

    Returns:
        ホスト側のポート番号。user-mode ネットワークでなければ None。
    """
    root = ET.fromstring(xml_text)
    for arg in root.iter(f"{{{QEMU_XML_NS}}}arg"):
        match = _HOSTFWD_SSH_RE.search(arg.get("value", ""))
        if match:
            return int(match.group(1))
    return None


def _port_is_free(port: int) -> bool:
    """127.0.0.1 の TCP ポートが今 bind できるかを確かめる。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def allocate_ssh_port(
    used_ports: set[int], port_range: tuple[int, int], is_free=_port_is_free
) -> int:
    """SSH 転送用のホストポートを1つ選ぶ。

    既存 VM に割り当て済み(停止中の VM も含む)のポートと、今ほかのプロセスが
    使っているポートを避け、範囲の小さい順に最初の空きを返す。

    Raises:
        RuntimeError: 範囲内に空きが無い場合。
    """
    low, high = port_range
    for port in range(low, high + 1):
        if port not in used_ports and is_free(port):
            return port
    raise RuntimeError(f"SSH 転送用の空きポートがありません(範囲 {low}-{high})")


def resize_domain_xml(xml_text: str, memory_kib: int, vcpus: int) -> str:
    """Domain XML の <memory>/<currentMemory>/<vcpu> 要素のみを書き換えて返す。

    dom.XMLDesc(VIR_DOMAIN_XML_INACTIVE) が返す完全な定義XMLをそのまま受け取り、
    それ以外の要素・属性は一切変更しない外部依存ゼロの純粋関数。
    build_domain_xml と異なりテンプレートからの再構築ではなく既存定義への最小差分編集
    であり、MAC/UUID の意図しない再生成(IP変化・UUID衝突)を避けるための手段。
    <currentMemory> が既存になければ <memory> の直後に同じ unit で新規追加する。
    起動時メモリが旧値のまま残らないようにするため。
    """
    root = ET.fromstring(xml_text)

    memory_el = root.find("memory")
    memory_el.text = str(memory_kib)

    current_memory_el = root.find("currentMemory")
    if current_memory_el is None:
        current_memory_el = ET.Element(
            "currentMemory", unit=memory_el.get("unit", "KiB")
        )
        root.insert(list(root).index(memory_el) + 1, current_memory_el)
    current_memory_el.text = str(memory_kib)

    root.find("vcpu").text = str(vcpus)

    return ET.tostring(root, encoding="unicode")


def set_domain_filterref_xml(xml_text: str, filter_name: str | None) -> str:
    """Domain XML の <devices><interface> 配下の <filterref> のみを書き換えて返す。

    resize_domain_xml と同様の純粋関数。複数NICの場合は全 interface に対して
    同じ操作を適用する。
    filter_name が None なら既存の <filterref> を除去し、文字列なら
    <filterref filter='{filter_name}'/> を追加する。
    既存にあれば filter 属性だけ書き換える。
    「フィルタなし→あり」「あり→なし」「あり→あり(ルール内容のみ変更、filter 名は不変)」
    のいずれの遷移でも同じ呼び出し方でこの1関数を使う。
    """
    root = ET.fromstring(xml_text)

    for interface_el in root.findall("devices/interface"):
        filterref_el = interface_el.find("filterref")

        if filter_name is None:
            if filterref_el is not None:
                interface_el.remove(filterref_el)
        else:
            if filterref_el is None:
                filterref_el = ET.SubElement(interface_el, "filterref")
            filterref_el.set("filter", filter_name)

    return ET.tostring(root, encoding="unicode")


def live_filterref_updates(xml_text: str, filter_name: str | None) -> list[str]:
    """稼働中の domain XML から、filterref を付け外しした interface XML を作る。

    set_domain_filterref_xml の稼働中版。dom.XMLDesc(0)(live の定義)を受け取り、
    filterref が filter_name と異なる interface だけを、filterref を書き換えた
    <interface> 要素の XML として返す。呼び出し側はこれを1件ずつ
    dom.updateDeviceFlags(..., VIR_DOMAIN_AFFECT_LIVE) に渡す。

    live の interface XML(target dev・alias・PCI address・source の portid を含む)
    をそのまま往復させるのは、libvirt(qemuDomainChangeNet)が filterref 以外の
    差分を「稼働中に変更できない」として拒否するため(virsh domif-setlink と同じ
    手法)。既に filter_name と一致する interface は除くので、途中で失敗して
    再実行しても同じ結果に収束する。filterref に子要素(<parameter>)があっても
    付け替え時は落とす(minivps の nwfilter は変数を使わない)。

    Args:
        xml_text: 稼働中の domain XML。
        filter_name: 付ける nwfilter 名。None なら filterref を外す。

    Returns:
        更新が要る interface の XML のリスト(無ければ空)。
    """
    root = ET.fromstring(xml_text)
    updates = []
    for interface_el in root.findall("devices/interface"):
        filterref_el = interface_el.find("filterref")
        current = filterref_el.get("filter") if filterref_el is not None else None
        if current == filter_name:
            continue
        if filterref_el is not None:
            interface_el.remove(filterref_el)
        if filter_name is not None:
            ET.SubElement(interface_el, "filterref", filter=filter_name)
        interface_el.tail = None
        updates.append(ET.tostring(interface_el, encoding="unicode"))
    return updates
