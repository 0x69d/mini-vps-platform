"""base image(`images` プールの volume)の一覧。

どの base image がどの管理対象 VM から参照されているかを示し、消してよい image と
消すと VM が起動できなくなる image(overlay の backing file)を見分けられるようにする。
参照の判定は spec(metadata)の `base_image` で行う。
"""

import logging
import xml.etree.ElementTree as ET

import libvirt

from .config import BASE_POOL

_LOGGER = logging.getLogger(__name__)


def volume_format(xml_text: str) -> str | None:
    """Storage volume XML の `<target><format type=...>` を返す(無ければ None)。"""
    fmt = ET.fromstring(xml_text).find("./target/format")
    return fmt.get("type") if fmt is not None else None


def image_entry(name: str, info: list, xml_text: str, specs: dict[str, dict]) -> dict:
    """Base image 1件分の表示用 dict を組み立てる(純粋関数)。

    Args:
        name: volume 名(= spec の base_image)。
        info: `vol.info()` の戻り値 [type, capacity, allocation](バイト)。
        xml_text: `vol.XMLDesc(0)`。
        specs: 管理対象 VM の name → spec。

    Returns:
        name / virtual_bytes / actual_bytes / format / used_by を持つ dict。
        used_by はこの image を base_image に指定している VM 名(昇順)。
    """
    return {
        "name": name,
        "virtual_bytes": int(info[1]),
        "actual_bytes": int(info[2]),
        "format": volume_format(xml_text),
        "used_by": sorted(
            vm for vm, spec in specs.items() if spec.get("base_image") == name
        ),
    }


def list_images(conn, specs: dict[str, dict]) -> list[dict]:
    """`images` プールの base image を名前順に列挙する。

    プールが無ければ空リストを返す(doctor がエラーとして報告する)。

    Args:
        conn: libvirt 接続。
        specs: 管理対象 VM の name → spec(ServerManager.managed_specs())。

    Returns:
        image_entry() の dict のリスト。
    """
    try:
        pool = conn.storagePoolLookupByName(BASE_POOL)
    except libvirt.libvirtError as e:
        if e.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_POOL:
            _LOGGER.warning("ストレージプール %s が無い", BASE_POOL)
            return []
        raise
    pool.refresh(0)
    entries = [
        image_entry(vol.name(), vol.info(), vol.XMLDesc(0), specs)
        for vol in pool.listAllVolumes()
    ]
    return sorted(entries, key=lambda e: e["name"])
