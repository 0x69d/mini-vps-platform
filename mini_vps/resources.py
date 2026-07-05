"""ストレージプール・volume・ISO・domain XML のリソース生成。"""

import os
import subprocess
import tempfile

import libvirt
import yaml

from .config import (
    BASE_POOL,
    DOMAIN_XML_TEMPLATE,
    META_DATA_TEMPLATE,
    NWFILTER_PORT_RULE_TEMPLATE,
    NWFILTER_XML_TEMPLATE,
    OVERLAY_VOL_XML_TEMPLATE,
    POOL_NAME,
    POOL_XML,
    SEED_DIR,
)
from .startup_scripts import render_startup_script


def ensure_pool(conn, name) -> libvirt.virStoragePool:
    """ストレージプールが無ければ作成し、アクティブ状態で返す(冪等)。

    define → build → create → autostart の順にセットアップする。
    """
    pools = {p.name() for p in conn.listAllStoragePools()}
    if name in pools:
        pool = conn.storagePoolLookupByName(name)
        if not pool.isActive():
            pool.create(0)
        return pool
    pool = conn.storagePoolDefineXML(POOL_XML, 0)
    pool.build(0)
    pool.create(0)
    pool.setAutostart(1)
    return pool


def create_overlay_volume(conn, spec) -> str:
    """専用プールに overlay volume を作成し、そのパスを返す。

    base image を backing store として使用する。既存の同名 volume は削除して再作成する。
    """
    base_pool = conn.storagePoolLookupByName(BASE_POOL)
    base_pool.refresh(0)
    base_path = base_pool.storageVolLookupByName(spec["base_image"]).path()

    pool = ensure_pool(conn, POOL_NAME)
    vol_name = f"{spec['name']}.qcow2"

    if vol_name in {v.name() for v in pool.listAllVolumes()}:
        pool.storageVolLookupByName(vol_name).delete(0)

    xml = OVERLAY_VOL_XML_TEMPLATE.format(
        name=spec["name"], disk=spec["disk"], base_path=base_path
    )
    return pool.createXML(xml, 0).path()


def _build_user_data(spec, pubkey, secrets: dict[str, str] | None) -> dict:
    """cloud-config の dict(YAML 化前)を組み立てる。

    hostname/users は常に含める。spec["startup_script"] が指定されていれば、
    対応するテンプレートをレンダリングして write_files/runcmd を追加する。
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
    startup_script = spec.get("startup_script")
    if startup_script:
        fragment = render_startup_script(startup_script, spec, secrets)
        data["write_files"] = fragment["write_files"]
        data["runcmd"] = fragment["runcmd"]
    return data


def build_seed_iso(spec, pubkey, secrets: dict[str, str] | None = None) -> str:
    """Seed ISO を生成してパスを返す。

    user-data と meta-data を一時ファイルに書き出し、cloud-localds で
    {name}-seed.iso を SEED_DIR に生成する。secrets はこの user-data 生成にのみ使う。
    """
    user_data = "#cloud-config\n" + yaml.safe_dump(
        _build_user_data(spec, pubkey, secrets), sort_keys=False
    )
    meta_data = META_DATA_TEMPLATE.format(name=spec["name"], hostname=spec["hostname"])
    seed_path = f"{SEED_DIR}/{spec['name']}-seed.iso"

    # libvirt driver は dynamic_ownership=1 が既定のため、一度でも起動した VM の
    # seed ISO は起動時に libvirt-qemu:kvm へ chown され、実行ユーザーからは
    # 上書き不可になる。cloud-localds は出力先へ直接書き込むため、生成前に
    # 既存ファイルを削除しておく(teardown() の seed ISO 削除と同じ前提)。
    if os.path.exists(seed_path):
        os.remove(seed_path)

    # TemporaryDirectory で囲むことで、cloud-localds が失敗しても
    # with を抜ける際に一時ファイルが確実に削除される。
    with tempfile.TemporaryDirectory() as tmp_dir:
        ud_file_path = os.path.join(tmp_dir, "user-data")
        md_file_path = os.path.join(tmp_dir, "meta-data")
        with open(ud_file_path, "w", encoding="utf-8") as ud_file:
            ud_file.write(user_data)
        with open(md_file_path, "w", encoding="utf-8") as md_file:
            md_file.write(meta_data)

        subprocess.run(
            ["cloud-localds", seed_path, ud_file_path, md_file_path], check=True
        )

    return seed_path


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

    filter_name はインターフェースに紐づける nwfilter 名(None なら付けない)。
    """
    memory_kib = spec["memory"] * 1024
    filterref = f"<filterref filter='{filter_name}'/>" if filter_name else ""
    xml = DOMAIN_XML_TEMPLATE.format(
        name=spec["name"],
        memory_kib=memory_kib,
        vcpus=spec["vcpus"],
        overlay_path=overlay_path,
        seed_path=seed_path,
        network=spec.get("network", "default"),
        filterref=filterref,
    )
    return xml
