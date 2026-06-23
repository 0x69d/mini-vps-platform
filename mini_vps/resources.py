import os
import subprocess
import tempfile

import libvirt

from .config import (
    BASE_POOL,
    DOMAIN_XML_TEMPLATE,
    LAB_DIR,
    META_DATA_TEMPLATE,
    OVERLAY_VOL_XML_TEMPLATE,
    POOL_NAME,
    POOL_XML,
    USER_DATA_TEMPLATE,
)


def ensure_pool(conn, name) -> "libvirt.virStoragePool":
    """
    name のプールが無ければ define→build→create→autostart して用意する(冪等)。
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
    """base_image を backing にした overlay volume を専用プールに作り、その path を返す。"""
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


def build_seed_iso(spec, pubkey) -> str:
    """
    spec から #cloud-config/meta-data を組み、cloud-localds で lab/ に seed.iso を作って path を返す。
    """
    user_data = USER_DATA_TEMPLATE.format(
        hostname=spec["hostname"], user=spec["user"], pubkey=pubkey
    )
    meta_data = META_DATA_TEMPLATE.format(name=spec["name"], hostname=spec["hostname"])
    seed_path = f"{LAB_DIR}/{spec['name']}-seed.iso"

    with tempfile.NamedTemporaryFile(mode="w", delete=False) as ud_file:
        ud_file.write(user_data)
        ud_file_path = ud_file.name

    with tempfile.NamedTemporaryFile(mode="w", delete=False) as md_file:
        md_file.write(meta_data)
        md_file_path = md_file.name

    subprocess.run(["cloud-localds", seed_path, ud_file_path, md_file_path], check=True)

    os.remove(ud_file_path)
    os.remove(md_file_path)

    return seed_path


def build_domain_xml(spec, overlay_path, seed_path) -> str:
    """
    spec + overlay/seed パスから ドメインXML文字列を組んで返す。
    """
    memory_kib = spec["memory"] * 1024
    xml = DOMAIN_XML_TEMPLATE.format(
        name=spec["name"],
        memory_kib=memory_kib,
        vcpus=spec["vcpus"],
        overlay_path=overlay_path,
        seed_path=seed_path,
        network=spec.get("network", "default"),
    )
    return xml
