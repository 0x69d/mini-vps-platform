"""ストレージプール・volume・ISO・domain XML のリソース生成。"""

import os
import subprocess
import tempfile
import xml.etree.ElementTree as ET

import libvirt
import yaml

from .config import (
    BASE_POOL,
    DOMAIN_XML_TEMPLATE,
    INTERFACE_XML_TEMPLATE,
    META_DATA_TEMPLATE,
    NWFILTER_PORT_RULE_TEMPLATE,
    NWFILTER_XML_TEMPLATE,
    OVERLAY_VOL_XML_TEMPLATE,
    POOL_NAME,
    POOL_XML,
    SEED_POOL_NAME,
    SEED_POOL_XML,
    SEED_VOL_XML_TEMPLATE,
    STATIC_ROUTES_EXEC_LINE_TEMPLATE,
    STATIC_ROUTES_UNIT_NAME,
    STATIC_ROUTES_UNIT_PATH,
    STATIC_ROUTES_UNIT_TEMPLATE,
)
from .startup_scripts import render_startup_script


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
    return pool


def ensure_seed_pool(conn) -> libvirt.virStoragePool:
    """Seed ISO 用の dir 型ストレージプールが無ければ作成し、アクティブ状態で返す。"""
    return ensure_pool(conn, SEED_POOL_NAME, SEED_POOL_XML)


def create_overlay_volume(conn, spec) -> str:
    """専用プールに overlay volume を作成し、そのパスを返す。

    base image を backing store として使用する。既存の同名 volume は削除して再作成する。
    """
    base_pool = conn.storagePoolLookupByName(BASE_POOL)
    base_pool.refresh(0)
    base_path = base_pool.storageVolLookupByName(spec["base_image"]).path()

    pool = ensure_pool(conn, POOL_NAME, POOL_XML)
    vol_name = f"{spec['name']}.qcow2"

    if vol_name in {v.name() for v in pool.listAllVolumes()}:
        pool.storageVolLookupByName(vol_name).delete(0)

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


def _build_user_data(spec, pubkey, secrets: dict[str, str] | None) -> dict:
    """cloud-config の dict(YAML 化前)を組み立てる。

    hostname/users は常に含める。spec["startup_script"] と spec["static_routes"] は
    それぞれ独立に write_files/runcmd フラグメントを生成し、両方あれば連結する
    (同時に使える必要があるため)。どちらも無ければ write_files/runcmd キー自体を
    含めない。
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

    write_files = []
    runcmd = []

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
    if runcmd:
        data["runcmd"] = runcmd
    return data


def build_seed_iso(conn, spec, pubkey, secrets: dict[str, str] | None = None) -> str:
    """Seed ISO を生成し、seed 用ストレージプールに配置してそのパスを返す。

    user-data と meta-data を一時ファイルに書き出し、cloud-localds で
    一時ディレクトリ内に {name}-seed.iso を生成したうえで、libvirt の volume API
    (createXML + upload)で seed 用プールへ配置する。secrets はこの user-data
    生成にのみ使う。
    """
    user_data = "#cloud-config\n" + yaml.safe_dump(
        _build_user_data(spec, pubkey, secrets), sort_keys=False
    )
    meta_data = META_DATA_TEMPLATE.format(name=spec["name"], hostname=spec["hostname"])
    vol_name = f"{spec['name']}-seed.iso"

    # TemporaryDirectory で囲むことで、cloud-localds が失敗しても
    # with を抜ける際に一時ファイルが確実に削除される。
    with tempfile.TemporaryDirectory() as tmp_dir:
        ud_file_path = os.path.join(tmp_dir, "user-data")
        md_file_path = os.path.join(tmp_dir, "meta-data")
        iso_path = os.path.join(tmp_dir, "seed.iso")
        with open(ud_file_path, "w", encoding="utf-8") as ud_file:
            ud_file.write(user_data)
        with open(md_file_path, "w", encoding="utf-8") as md_file:
            md_file.write(meta_data)

        subprocess.run(
            ["cloud-localds", iso_path, ud_file_path, md_file_path], check=True
        )

        pool = ensure_seed_pool(conn)
        if vol_name in {v.name() for v in pool.listAllVolumes()}:
            pool.storageVolLookupByName(vol_name).delete(0)

        capacity = os.path.getsize(iso_path)
        vol = pool.createXML(
            SEED_VOL_XML_TEMPLATE.format(name=vol_name, capacity_bytes=capacity), 0
        )

        stream = conn.newStream(0)
        vol.upload(stream, 0, 0, 0)
        try:
            with open(iso_path, "rb") as iso_file:
                stream.sendAll(lambda st, nbytes, f: f.read(nbytes), iso_file)
        except Exception:
            stream.abort()
            raise
        stream.finish()

    return vol.path()


def _filter_name(spec) -> str:
    """VM の name から決定的な nwfilter 名を作る。"""
    return f"minivps-{spec['name']}"


def build_nwfilter_xml(spec) -> str:
    """spec["filters"] から VM 専用の nwfilter XML を組み立てて返す。

    呼び出し側で spec["filters"] is not None を確認済みであることが前提。
    """
    port_rules = "".join(
        NWFILTER_PORT_RULE_TEMPLATE.format(protocol=f["protocol"], port=f["port"])
        for f in spec["filters"]
    )
    return NWFILTER_XML_TEMPLATE.format(name=_filter_name(spec), port_rules=port_rules)


def build_domain_xml(spec, overlay_path, seed_path, filter_name=None) -> str:
    """Domain XML 文字列を組み立てて返す。

    spec["networks"] の要素数だけ <interface> を生成する(複数NIC対応)。
    filter_name は全 interface に紐づける nwfilter 名(None なら付けない、
    nwfilter は VM 全体の inbound 許可という意味論のため全 NIC に同一のものを付ける)。
    """
    memory_kib = spec["memory"] * 1024
    filterref = f"<filterref filter='{filter_name}'/>" if filter_name else ""
    interfaces = "".join(
        INTERFACE_XML_TEMPLATE.format(network=network, filterref=filterref)
        for network in spec["networks"]
    )
    xml = DOMAIN_XML_TEMPLATE.format(
        name=spec["name"],
        memory_kib=memory_kib,
        vcpus=spec["vcpus"],
        overlay_path=overlay_path,
        seed_path=seed_path,
        interfaces=interfaces,
    )
    return xml


def resize_domain_xml(xml_text: str, memory_kib: int, vcpus: int) -> str:
    """Domain XML の <memory>/<currentMemory>/<vcpu> 要素のみを書き換えて返す。

    dom.XMLDesc(VIR_DOMAIN_XML_INACTIVE) が返す完全な定義XML(MAC・UUID含む)を
    そのまま受け取り、それ以外の要素・属性は一切変更しない外部依存ゼロの純粋関数。
    build_domain_xml と異なりテンプレートからの再構築ではなく既存定義への最小差分編集
    であり、MAC/UUID の意図しない再生成(IP変化・UUID衝突)を避けるための手段。
    <currentMemory> が既存になければ <memory> の直後に同じ unit で新規追加する
    (起動時メモリが旧値のまま残らないようにするため)。
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

    resize_domain_xml と同様の純粋関数。複数NIC(<interface> 複数)の場合は
    全 interface に対して同じ操作を適用する(nwfilter は VM 全体の inbound 許可
    という意味論のため)。
    filter_name が None なら既存の <filterref> を除去し(無ければ何もしない)、
    文字列なら <filterref filter='{filter_name}'/> を追加する(既存にあれば
    filter 属性だけ書き換える)。「フィルタなし→あり」「あり→なし」
    「あり→あり(ルール内容のみ変更、filter 名は不変)」のいずれの遷移でも
    同じ呼び出し方でこの1関数を使える。
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
