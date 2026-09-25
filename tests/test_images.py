from unittest.mock import MagicMock

import libvirt
import pytest
from conftest import make_libvirt_error

from mini_vps.images import image_entry, list_images, volume_format
from mini_vps.manager import ServerManager

VOL_XML = """
<volume type='file'>
  <name>ubuntu-24.04.img</name>
  <capacity unit='bytes'>3758096384</capacity>
  <target>
    <path>/var/lib/libvirt/images/ubuntu-24.04.img</path>
    <format type='qcow2'/>
  </target>
</volume>
"""


def test_volume_format_reads_target_format():
    assert volume_format(VOL_XML) == "qcow2"


def test_volume_format_is_none_without_format():
    assert volume_format("<volume><target/></volume>") is None


def test_image_entry_lists_referencing_vms_sorted():
    specs = {
        "web-2": {"base_image": "ubuntu-24.04.img"},
        "web-1": {"base_image": "ubuntu-24.04.img"},
        "db-1": {"base_image": "debian-12.img"},
    }

    entry = image_entry("ubuntu-24.04.img", [0, 3758096384, 625262592], VOL_XML, specs)

    assert entry == {
        "name": "ubuntu-24.04.img",
        "virtual_bytes": 3758096384,
        "actual_bytes": 625262592,
        "format": "qcow2",
        "used_by": ["web-1", "web-2"],
    }


def _vol(name, info):
    vol = MagicMock()
    vol.name.return_value = name
    vol.info.return_value = info
    vol.XMLDesc.return_value = VOL_XML
    return vol


def test_list_images_sorts_by_name_and_refreshes():
    conn = MagicMock()
    pool = conn.storagePoolLookupByName.return_value
    pool.listAllVolumes.return_value = [
        _vol("ubuntu.img", [0, 3, 1]),
        _vol("debian.img", [0, 2, 1]),
    ]

    images = list_images(conn, {})

    assert [i["name"] for i in images] == ["debian.img", "ubuntu.img"]
    conn.storagePoolLookupByName.assert_called_once_with("images")
    pool.refresh.assert_called_once_with(0)


def test_list_images_is_empty_when_pool_missing():
    conn = MagicMock()
    conn.storagePoolLookupByName.side_effect = make_libvirt_error(
        libvirt.VIR_ERR_NO_STORAGE_POOL
    )

    assert list_images(conn, {}) == []


def test_list_images_reraises_other_errors():
    conn = MagicMock()
    conn.storagePoolLookupByName.side_effect = make_libvirt_error(
        libvirt.VIR_ERR_INTERNAL_ERROR
    )

    with pytest.raises(libvirt.libvirtError):
        list_images(conn, {})


def test_manager_images_passes_managed_specs(monkeypatch):
    conn = MagicMock()
    mgr = ServerManager(conn)
    specs = {"web-1": {"base_image": "ubuntu.img"}}
    monkeypatch.setattr(mgr, "managed_specs", lambda: specs)
    list_mock = MagicMock(return_value=[{"name": "ubuntu.img"}])
    monkeypatch.setattr("mini_vps.manager.list_images", list_mock)

    assert mgr.images() == [{"name": "ubuntu.img"}]
    list_mock.assert_called_once_with(conn, specs)
