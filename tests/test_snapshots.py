import contextlib
import json
import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, call

import libvirt
import pytest
from conftest import linux_kvm_profile, make_libvirt_error
from fastapi.testclient import TestClient
from pydantic import ValidationError

import mini_vps.api as api_module
from mini_vps import cli, errors, snapshots
from mini_vps.errors import (
    PlatformUnsupported,
    ServerConflict,
    ServerNotRunning,
    SnapshotNotFound,
)
from mini_vps.manager import ServerManager
from mini_vps.resources import build_domain_xml
from mini_vps.spec import ServerSpec, validate_snapshot_name

POOL_DIR = "/var/lib/libvirt/vps-pool"
SEED_PATH = "/var/lib/libvirt/seeds/web-1-seed.iso"


def _spec(**overrides):
    base = {
        "name": "web-1",
        "memory": 1024,
        "vcpus": 2,
        "base_image": "ubuntu-24.04.img",
        "disk": 10,
    }
    base.update(overrides)
    return ServerSpec(**base).model_dump()


def _domain_xml(root_file="web-1.qcow2", backing=None, arch="x86_64"):
    """build_domain_xml の出力のルートディスクを root_file へ向けた domain XML。

    backing を渡すと、libvirt がスナップショット後に出力するような <backingStore> を
    vda に付ける。
    """
    machine = "virt" if arch == "aarch64" else "q35"
    profile = linux_kvm_profile(arch=arch, machine=machine)
    xml = build_domain_xml(
        _spec(), f"{POOL_DIR}/{root_file}", SEED_PATH, profile=profile
    )
    if backing is None:
        return xml
    root = ET.fromstring(xml)
    for disk in root.findall("devices/disk"):
        if disk.find("target").get("dev") == "vda":
            store = ET.SubElement(disk, "backingStore", type="file")
            ET.SubElement(store, "format", type="qcow2")
            ET.SubElement(store, "source", file=f"{POOL_DIR}/{backing}")
    return ET.tostring(root, encoding="unicode")


def _root_disk(xml):
    root = ET.fromstring(xml)
    for disk in root.findall("devices/disk"):
        if disk.find("target").get("dev") == "vda":
            return disk
    raise AssertionError("vda がない")


def _snap_xml(
    name,
    parent=None,
    created=1_700_000_000,
    state="disk-snapshot",
    root_file=None,
    external=True,
):
    """libvirt の virDomainSnapshotGetXMLDesc が返す形のスナップショット XML。"""
    root_file = root_file or f"{POOL_DIR}/web-1.snap-{name}.qcow2"
    parent_xml = f"<parent><name>{parent}</name></parent>" if parent else ""
    if external:
        vda = (
            "<disk name='vda' snapshot='external' type='file'>"
            f"<driver type='qcow2'/><source file='{root_file}'/></disk>"
        )
    else:
        vda = "<disk name='vda' snapshot='internal'/>"
    return (
        f"<domainsnapshot><name>{name}</name><state>{state}</state>{parent_xml}"
        f"<creationTime>{created}</creationTime><memory snapshot='no'/>"
        f"<disks>{vda}<disk name='sda' snapshot='no'/></disks>"
        # スナップショット XML に埋め込まれた domain のディスクと取り違えないこと。
        "<domain><devices><disk><target dev='vda'/>"
        "<source file='/elsewhere/other.qcow2'/></disk></devices></domain>"
        "</domainsnapshot>"
    )


def _snapshot(name, parent=None, current=False, **kwargs):
    snap = MagicMock()
    snap.getName.return_value = name
    snap.getXMLDesc.return_value = _snap_xml(name, parent=parent, **kwargs)
    snap.isCurrent.return_value = 1 if current else 0
    return snap


def _dom(chain=(), state=libvirt.VIR_DOMAIN_SHUTOFF, xml=None, **snap_kwargs):
    """chain(古い順のスナップショット名)を一直線に持つ domain の Mock を返す。"""
    dom = MagicMock()
    snaps = {}
    parent = None
    for index, name in enumerate(chain):
        snaps[name] = _snapshot(
            name,
            parent=parent,
            current=index == len(chain) - 1,
            created=1_700_000_000 + index,
            **snap_kwargs,
        )
        parent = name
    dom.listAllSnapshots.return_value = list(snaps.values())

    def _lookup(name, flags=0):
        if name not in snaps:
            raise make_libvirt_error(libvirt.VIR_ERR_NO_DOMAIN_SNAPSHOT)
        return snaps[name]

    dom.snapshotLookupByName.side_effect = _lookup
    dom.state.return_value = [state, 0]
    dom.isActive.return_value = state != libvirt.VIR_DOMAIN_SHUTOFF
    dom.XMLDesc.return_value = xml or _domain_xml(
        f"web-1.snap-{chain[-1]}.qcow2" if chain else "web-1.qcow2"
    )
    dom.snaps = snaps
    return dom


def _conn(volumes=(), version=10_000_000):
    conn = MagicMock()
    conn.getLibVersion.return_value = version
    pool = MagicMock()
    vols = []
    for vol_name in volumes:
        vol = MagicMock()
        vol.name.return_value = vol_name
        vols.append(vol)
    pool.listAllVolumes.return_value = vols
    conn.storagePoolLookupByName.return_value = pool
    return conn, pool


# --- 命名 ---


def test_snapshot_file_name_follows_contract():
    assert snapshots.snapshot_file_name("web-1", "before-upgrade") == (
        "web-1.snap-before-upgrade.qcow2"
    )


@pytest.mark.parametrize(
    ("file_name", "expected"),
    [
        ("web-1.snap-a.qcow2", True),
        ("web-1.qcow2", False),
        ("web-10.snap-a.qcow2", False),
        ("web-1.snap-.qcow2", False),
        ("web-1.snap-a.iso", False),
        ("web-1-seed.iso", False),
    ],
)
def test_is_snapshot_file(file_name, expected):
    assert snapshots.is_snapshot_file("web-1", file_name) is expected


# --- スナップショット名の検証(spec.py) ---


@pytest.mark.parametrize("name", ["a", "before-upgrade", "step_2", "A1"])
def test_validate_snapshot_name_accepts(name):
    assert validate_snapshot_name(name) == name


@pytest.mark.parametrize(
    "name", ["", "a.b", ".hidden", "a/b", "-a", "a b", "x" * 64, "a;rm"]
)
def test_validate_snapshot_name_rejects(name):
    with pytest.raises(ValidationError):
        validate_snapshot_name(name)


# --- libvirt の版 ---


def test_libvirt_version_splits_integer():
    conn, _ = _conn(version=9_009_000)
    assert snapshots.libvirt_version(conn) == (9, 9, 0)


def test_require_libvirt_version_rejects_old_with_reason():
    conn, _ = _conn(version=8_000_000)
    with pytest.raises(PlatformUnsupported, match=r"9\.0\.0.*8\.0\.0"):
        snapshots.require_libvirt_version(conn, (9, 0, 0), "削除")


def test_require_libvirt_version_accepts_equal():
    conn, _ = _conn(version=9_000_000)
    snapshots.require_libvirt_version(conn, (9, 0, 0), "削除")


# --- build_snapshot_xml ---


def test_build_snapshot_xml_targets_only_root_disk_on_x86():
    xml = snapshots.build_snapshot_xml(
        "a", _domain_xml(), f"{POOL_DIR}/web-1.snap-a.qcow2"
    )

    root = ET.fromstring(xml)
    assert root.findtext("name") == "a"
    assert root.find("memory").get("snapshot") == "no"
    disks = {d.get("name"): d for d in root.findall("disks/disk")}
    assert set(disks) == {"vda", "sda"}
    assert disks["vda"].get("snapshot") == "external"
    assert disks["vda"].find("driver").get("type") == "qcow2"
    assert disks["vda"].find("source").get("file") == (f"{POOL_DIR}/web-1.snap-a.qcow2")
    # seed(cdrom)は対象外にする。
    assert disks["sda"].get("snapshot") == "no"
    assert disks["sda"].find("source") is None


def test_build_snapshot_xml_excludes_aarch64_seed_disk():
    xml = snapshots.build_snapshot_xml(
        "a", _domain_xml(arch="aarch64"), f"{POOL_DIR}/web-1.snap-a.qcow2"
    )

    disks = {d.get("name"): d for d in ET.fromstring(xml).findall("disks/disk")}
    assert disks["vda"].get("snapshot") == "external"
    assert disks["vdb"].get("snapshot") == "no"


# --- parse_snapshot_xml ---


def test_parse_snapshot_xml_extracts_fields():
    info = snapshots.parse_snapshot_xml(
        _snap_xml("b", parent="a", created=1_700_000_000)
    )

    assert info == {
        "name": "b",
        "created_at": "2023-11-14T22:13:20+00:00",
        "parent": "a",
        "state": "disk-snapshot",
        "root_file": f"{POOL_DIR}/web-1.snap-b.qcow2",
    }


def test_parse_snapshot_xml_root_has_no_parent():
    info = snapshots.parse_snapshot_xml(_snap_xml("a", state="shutoff"))
    assert info["parent"] is None
    assert info["state"] == "shutoff"


def test_parse_snapshot_xml_internal_snapshot_has_no_root_file():
    assert (
        snapshots.parse_snapshot_xml(_snap_xml("a", external=False))["root_file"]
        is None
    )


# --- linear_chain ---


def test_linear_chain_empty():
    assert snapshots.linear_chain({}, None) == []


def test_linear_chain_orders_root_first():
    parents = {"c": "b", "a": None, "b": "a"}
    assert snapshots.linear_chain(parents, "c") == ["a", "b", "c"]


def test_linear_chain_rejects_branch():
    # a から b と x の2本に分かれている(libvirt の revert で作られうる形)。
    with pytest.raises(ValueError, match="辿れない"):
        snapshots.linear_chain({"a": None, "b": "a", "x": "a"}, "b")


def test_linear_chain_rejects_current_not_at_tip():
    with pytest.raises(ValueError):
        snapshots.linear_chain({"a": None, "b": "a"}, "a")


def test_linear_chain_rejects_missing_current():
    with pytest.raises(ValueError):
        snapshots.linear_chain({"a": None}, None)


def test_linear_chain_rejects_missing_parent():
    with pytest.raises(ValueError):
        snapshots.linear_chain({"b": "gone"}, "b")


# --- root_disk_source / set_root_disk_source_xml ---


def test_root_disk_source_reads_vda():
    xml = _domain_xml("web-1.snap-a.qcow2")
    assert snapshots.root_disk_source(xml) == f"{POOL_DIR}/web-1.snap-a.qcow2"


def test_set_root_disk_source_xml_rewrites_source_and_drops_backing_store():
    xml = _domain_xml("web-1.snap-b.qcow2", backing="web-1.snap-a.qcow2")

    out = snapshots.set_root_disk_source_xml(xml, f"{POOL_DIR}/web-1.snap-a.qcow2")

    disk = _root_disk(out)
    assert disk.find("source").get("file") == f"{POOL_DIR}/web-1.snap-a.qcow2"
    # 古い chain の記述を残さない(空要素にもしない)。
    assert disk.find("backingStore") is None
    # seed やほかの要素は変えない。
    root = ET.fromstring(out)
    seed = [
        d for d in root.findall("devices/disk") if d.find("target").get("dev") == "sda"
    ][0]
    assert seed.find("source").get("file") == SEED_PATH
    assert root.findtext("memory") == str(1024 * 1024)


# --- create ---


def test_create_takes_external_disk_only_snapshot_next_to_root_disk():
    conn, pool = _conn()
    dom = _dom(state=libvirt.VIR_DOMAIN_RUNNING)
    created = _snapshot("a", current=True)
    dom.snapshotCreateXML.return_value = created

    info = snapshots.create(conn, dom, "web-1", "a")

    xml, flags = dom.snapshotCreateXML.call_args.args
    assert flags == (
        libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_DISK_ONLY
        | libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_ATOMIC
    )
    vda = [
        d for d in ET.fromstring(xml).findall("disks/disk") if d.get("name") == "vda"
    ][0]
    assert vda.find("source").get("file") == f"{POOL_DIR}/web-1.snap-a.qcow2"
    # libvirt がプールの API を通さずに作ったファイルを一覧に載せる。
    pool.refresh.assert_called_with(0)
    assert info["name"] == "a"
    assert info["current"] is True
    assert "root_file" not in info


def test_create_places_next_overlay_beside_current_snapshot_overlay():
    conn, _ = _conn()
    dom = _dom(chain=["a"])
    dom.snapshotCreateXML.return_value = _snapshot("b", parent="a", current=True)

    snapshots.create(conn, dom, "web-1", "b")

    xml = dom.snapshotCreateXML.call_args.args[0]
    assert f"{POOL_DIR}/web-1.snap-b.qcow2" in xml


def test_create_with_quiesce_adds_flag_when_running():
    conn, _ = _conn()
    dom = _dom(state=libvirt.VIR_DOMAIN_RUNNING)
    dom.snapshotCreateXML.return_value = _snapshot("a", current=True)

    snapshots.create(conn, dom, "web-1", "a", quiesce=True)

    flags = dom.snapshotCreateXML.call_args.args[1]
    assert flags & libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_QUIESCE


@pytest.mark.parametrize(
    "state", [libvirt.VIR_DOMAIN_SHUTOFF, libvirt.VIR_DOMAIN_PAUSED]
)
def test_create_with_quiesce_rejects_non_running(state):
    conn, _ = _conn()
    dom = _dom(state=state)

    with pytest.raises(ServerNotRunning):
        snapshots.create(conn, dom, "web-1", "a", quiesce=True)

    dom.snapshotCreateXML.assert_not_called()


def test_create_rejects_duplicate_name():
    conn, _ = _conn()
    dom = _dom(chain=["a"])

    with pytest.raises(ServerConflict, match="既に存在"):
        snapshots.create(conn, dom, "web-1", "a")

    dom.snapshotCreateXML.assert_not_called()


def test_create_rejects_leftover_overlay_file():
    conn, _ = _conn(volumes=["web-1.qcow2", "web-1.snap-a.qcow2"])
    dom = _dom()

    with pytest.raises(ServerConflict, match="web-1.snap-a.qcow2"):
        snapshots.create(conn, dom, "web-1", "a")

    dom.snapshotCreateXML.assert_not_called()


def test_create_rejects_snapshot_made_outside_mini_vps():
    conn, _ = _conn()
    dom = _dom(chain=["a"], root_file=f"{POOL_DIR}/web-1.1700000000")

    with pytest.raises(ServerConflict, match="mini-vps の外"):
        snapshots.create(conn, dom, "web-1", "b")


# --- list_snapshots ---


def test_list_snapshots_sorted_by_creation_time():
    dom = MagicMock()
    dom.listAllSnapshots.return_value = [
        _snapshot("b", parent="a", current=True, created=1_700_000_100),
        _snapshot("a", created=1_700_000_000, state="shutoff"),
    ]

    result = snapshots.list_snapshots(dom)

    assert [s["name"] for s in result] == ["a", "b"]
    assert result[0] == {
        "name": "a",
        "created_at": "2023-11-14T22:13:20+00:00",
        "parent": None,
        "state": "shutoff",
        "current": False,
    }
    assert result[1]["current"] is True


def test_list_snapshots_follows_chain_when_created_in_same_second():
    dom = MagicMock()
    dom.listAllSnapshots.return_value = [
        _snapshot("a", parent="z", current=True, created=1_700_000_000),
        _snapshot("z", created=1_700_000_000),
    ]

    assert [s["name"] for s in snapshots.list_snapshots(dom)] == ["z", "a"]


def test_list_snapshots_falls_back_to_time_order_when_branched():
    dom = MagicMock()
    dom.listAllSnapshots.return_value = [
        _snapshot("x", parent="a", created=1_700_000_200),
        _snapshot("b", parent="a", current=True, created=1_700_000_100),
        _snapshot("a", created=1_700_000_000),
    ]

    assert [s["name"] for s in snapshots.list_snapshots(dom)] == ["a", "b", "x"]


# --- revert ---


def test_revert_to_older_snapshot_discards_newer_ones_and_restarts():
    conn, pool = _conn(
        volumes=[
            "web-1.qcow2",
            "web-1.snap-a.qcow2",
            "web-1.snap-b.qcow2",
            "web-1.snap-c.qcow2",
        ]
    )
    dom = _dom(chain=["a", "b", "c"], state=libvirt.VIR_DOMAIN_RUNNING)
    dom.XMLDesc.return_value = _domain_xml(
        "web-1.snap-c.qcow2", backing="web-1.snap-b.qcow2"
    )
    order = MagicMock()
    order.attach_mock(dom.destroy, "destroy")
    order.attach_mock(conn.defineXML, "defineXML")
    order.attach_mock(pool.createXML, "createXML")
    order.attach_mock(dom.create, "create")
    before_start = MagicMock()
    order.attach_mock(before_start, "before_start")

    result = snapshots.revert(conn, dom, _spec(), "a", before_start=before_start)

    assert result == {"reverted_to": "a", "discarded": ["b", "c"]}
    # 1. 強制停止 → 2. ディスクを a の overlay へ → 4. 作り直し → 5. 起動
    assert [c[0] for c in order.mock_calls] == [
        "destroy",
        "defineXML",
        "createXML",
        "before_start",
        "create",
    ]
    defined = _root_disk(conn.defineXML.call_args.args[0])
    assert defined.find("source").get("file") == f"{POOL_DIR}/web-1.snap-a.qcow2"
    assert defined.find("backingStore") is None
    # 3. 新しいスナップショットのメタデータを新しい順に消す(ファイルは消さない)
    metadata_only = libvirt.VIR_DOMAIN_SNAPSHOT_DELETE_METADATA_ONLY
    dom.snaps["c"].delete.assert_called_once_with(metadata_only)
    dom.snaps["b"].delete.assert_called_once_with(metadata_only)
    dom.snaps["a"].delete.assert_not_called()
    # 4. 上の層から順にファイルを消し、a の overlay を空で作り直す
    assert [c.args[0] for c in pool.storageVolLookupByName.call_args_list] == [
        "web-1.snap-c.qcow2",
        "web-1.snap-b.qcow2",
        "web-1.snap-a.qcow2",
    ]
    vol_xml = pool.createXML.call_args.args[0]
    assert "<name>web-1.snap-a.qcow2</name>" in vol_xml
    assert f"<path>{POOL_DIR}/web-1.qcow2</path>" in vol_xml
    assert "<capacity unit='GiB'>10</capacity>" in vol_xml


def test_revert_to_middle_snapshot_backs_onto_parent_overlay():
    conn, pool = _conn()
    dom = _dom(chain=["a", "b", "c"])

    result = snapshots.revert(conn, dom, _spec(), "b")

    assert result["discarded"] == ["c"]
    vol_xml = pool.createXML.call_args.args[0]
    assert "<name>web-1.snap-b.qcow2</name>" in vol_xml
    assert f"<path>{POOL_DIR}/web-1.snap-a.qcow2</path>" in vol_xml


def test_revert_to_current_snapshot_recreates_its_overlay_only():
    conn, pool = _conn(volumes=["web-1.qcow2", "web-1.snap-a.qcow2"])
    dom = _dom(chain=["a"])

    result = snapshots.revert(conn, dom, _spec(), "a")

    assert result["discarded"] == []
    dom.snaps["a"].delete.assert_not_called()
    pool.storageVolLookupByName.assert_called_once_with("web-1.snap-a.qcow2")
    pool.createXML.assert_called_once()


def test_revert_collects_overlays_left_by_interrupted_revert():
    """前回の revert がメタデータ削除の途中で落ちて残った c の層も回収する。"""
    conn, pool = _conn(
        volumes=[
            "web-1.qcow2",
            "web-1.snap-a.qcow2",
            "web-1.snap-b.qcow2",
            "web-1.snap-c.qcow2",
            "web-10.snap-z.qcow2",
        ]
    )
    # c のメタデータは消えたが、a・b のメタデータと c のファイルは残っている。
    dom = _dom(chain=["a", "b"])

    snapshots.revert(conn, dom, _spec(), "b")

    deleted = [c.args[0] for c in pool.storageVolLookupByName.call_args_list]
    assert deleted == ["web-1.snap-b.qcow2", "web-1.snap-c.qcow2"]


def test_revert_keeps_stopped_vm_stopped():
    conn, _ = _conn()
    dom = _dom(chain=["a"], state=libvirt.VIR_DOMAIN_SHUTOFF)
    before_start = MagicMock()

    snapshots.revert(conn, dom, _spec(), "a", before_start=before_start)

    dom.destroy.assert_not_called()
    dom.create.assert_not_called()
    dom.createWithFlags.assert_not_called()
    before_start.assert_not_called()


def test_revert_keeps_paused_vm_paused():
    conn, _ = _conn()
    dom = _dom(chain=["a"], state=libvirt.VIR_DOMAIN_PAUSED)

    snapshots.revert(conn, dom, _spec(), "a")

    dom.destroy.assert_called_once()
    dom.createWithFlags.assert_called_once_with(libvirt.VIR_DOMAIN_START_PAUSED)
    dom.create.assert_not_called()


def test_revert_raises_snapshot_not_found_before_touching_vm():
    conn, _ = _conn()
    dom = _dom(chain=["a"], state=libvirt.VIR_DOMAIN_RUNNING)

    with pytest.raises(SnapshotNotFound):
        snapshots.revert(conn, dom, _spec(), "missing")

    dom.destroy.assert_not_called()
    conn.defineXML.assert_not_called()


def test_revert_refuses_branched_snapshots_before_touching_vm():
    conn, _ = _conn()
    dom = _dom(state=libvirt.VIR_DOMAIN_RUNNING)
    snaps = {
        "a": _snapshot("a"),
        "b": _snapshot("b", parent="a", current=True),
        "x": _snapshot("x", parent="a"),
    }
    dom.listAllSnapshots.return_value = list(snaps.values())
    dom.snapshotLookupByName.side_effect = lambda n, f=0: snaps[n]

    with pytest.raises(ServerConflict, match="一直線"):
        snapshots.revert(conn, dom, _spec(), "a")

    dom.destroy.assert_not_called()


# --- delete ---


def test_delete_rejects_old_libvirt_before_lookup():
    conn, _ = _conn(version=8_000_000)
    dom = _dom(chain=["a"])

    with pytest.raises(PlatformUnsupported):
        snapshots.delete(conn, dom, "web-1", "a")

    dom.snapshotLookupByName.assert_not_called()


def test_delete_merges_via_libvirt_and_refreshes_pool():
    conn, pool = _conn()
    dom = _dom(chain=["a", "b"], state=libvirt.VIR_DOMAIN_RUNNING)
    before = MagicMock()

    snapshots.delete(conn, dom, "web-1", "a", before_offline_merge=before)

    dom.snaps["a"].delete.assert_called_once_with(0)
    before.assert_not_called()
    pool.refresh.assert_called_with(0)
    # commit 済みファイルの削除は libvirt に任せる。
    pool.storageVolLookupByName.assert_not_called()


def test_delete_prepares_offline_merge_when_stopped():
    conn, _ = _conn()
    dom = _dom(chain=["a"], state=libvirt.VIR_DOMAIN_SHUTOFF)
    order = MagicMock()
    before = MagicMock()
    order.attach_mock(before, "before")
    order.attach_mock(dom.snaps["a"].delete, "delete")

    snapshots.delete(conn, dom, "web-1", "a", before_offline_merge=before)

    assert [c[0] for c in order.mock_calls] == ["before", "delete"]


def test_delete_raises_snapshot_not_found():
    conn, _ = _conn()
    dom = _dom(chain=["a"])

    with pytest.raises(SnapshotNotFound):
        snapshots.delete(conn, dom, "web-1", "missing")


# --- discard_all ---


def test_discard_all_is_noop_without_snapshots():
    conn, pool = _conn(volumes=["web-1.qcow2"])
    dom = _dom()

    assert snapshots.discard_all(conn, dom, "web-1") == []

    dom.XMLDesc.assert_not_called()
    conn.defineXML.assert_not_called()
    pool.storageVolLookupByName.assert_not_called()


def test_discard_all_drops_metadata_repoints_disk_and_deletes_files():
    conn, pool = _conn(
        volumes=["web-1.qcow2", "web-1.snap-a.qcow2", "web-1.snap-b.qcow2"]
    )
    dom = _dom(chain=["a", "b"])
    dom.XMLDesc.return_value = _domain_xml(
        "web-1.snap-b.qcow2", backing="web-1.snap-a.qcow2"
    )

    assert snapshots.discard_all(conn, dom, "web-1") == ["a", "b"]

    metadata_only = libvirt.VIR_DOMAIN_SNAPSHOT_DELETE_METADATA_ONLY
    dom.snaps["a"].delete.assert_called_once_with(metadata_only)
    dom.snaps["b"].delete.assert_called_once_with(metadata_only)
    dom.XMLDesc.assert_called_once_with(libvirt.VIR_DOMAIN_XML_INACTIVE)
    defined = _root_disk(conn.defineXML.call_args.args[0])
    assert defined.find("source").get("file") == f"{POOL_DIR}/web-1.qcow2"
    assert defined.find("backingStore") is None
    assert [c.args[0] for c in pool.storageVolLookupByName.call_args_list] == [
        "web-1.snap-a.qcow2",
        "web-1.snap-b.qcow2",
    ]


def test_discard_all_removes_orphan_files_without_metadata():
    conn, pool = _conn(volumes=["web-1.qcow2", "web-1.snap-a.qcow2"])
    dom = _dom()

    assert snapshots.discard_all(conn, dom, "web-1") == []

    # source は既に web-1.qcow2 なので domain は定義し直さない。
    conn.defineXML.assert_not_called()
    pool.storageVolLookupByName.assert_called_once_with("web-1.snap-a.qcow2")


# --- errors ---


def test_snapshot_not_found_maps_to_404_and_exit_10():
    mapping = errors.lookup(SnapshotNotFound("web-1/a"))
    assert (mapping.http_status, mapping.exit_code, mapping.label) == (
        404,
        10,
        "snapshot not found",
    )


# --- ServerManager ---


@pytest.fixture
def managed(monkeypatch):
    """_lookup / _read_spec を差し替えた ServerManager と domain を返す。"""
    conn = MagicMock()
    mgr = ServerManager(conn)
    dom = MagicMock()
    spec = _spec()
    monkeypatch.setattr("mini_vps.manager._lookup", lambda c, n: dom)
    monkeypatch.setattr("mini_vps.manager._read_spec", lambda d: spec)
    return mgr, conn, dom, spec


def test_manager_snapshot_create_validates_name_before_lookup(monkeypatch):
    mgr = ServerManager(MagicMock())
    lookup = MagicMock()
    monkeypatch.setattr("mini_vps.manager._lookup", lookup)

    with pytest.raises(ValidationError):
        mgr.snapshot_create("web-1", "a.b")

    lookup.assert_not_called()


def test_manager_snapshot_create_delegates_under_lock(managed, monkeypatch):
    mgr, conn, dom, _ = managed
    create = MagicMock(return_value={"name": "a"})
    monkeypatch.setattr("mini_vps.manager.snapshots.create", create)
    locked = MagicMock(side_effect=lambda name: contextlib.nullcontext())
    monkeypatch.setattr(mgr, "_locked", locked)

    assert mgr.snapshot_create("web-1", "a", quiesce=True) == {"name": "a"}

    locked.assert_called_once_with("web-1")
    create.assert_called_once_with(conn, dom, "web-1", "a", quiesce=True)


def test_manager_snapshot_list_does_not_lock(managed, monkeypatch):
    mgr, _, dom, _ = managed
    monkeypatch.setattr(
        "mini_vps.manager.snapshots.list_snapshots", MagicMock(return_value=[])
    )
    monkeypatch.setattr(mgr, "_locked", MagicMock(side_effect=AssertionError))

    assert mgr.snapshot_list("web-1") == []


def test_manager_snapshot_revert_starts_network_before_restart(managed, monkeypatch):
    mgr, conn, dom, spec = managed
    ensure = MagicMock()
    monkeypatch.setattr("mini_vps.manager.ensure_network_active", ensure)

    def _revert(c, d, s, snap, before_start):
        before_start()
        return {"reverted_to": snap, "discarded": []}

    monkeypatch.setattr("mini_vps.manager.snapshots.revert", _revert)
    mgr.get = MagicMock(return_value={"spec": spec, "status": {"state": "running"}})

    result = mgr.snapshot_revert("web-1", "a")

    ensure.assert_called_once_with(conn, spec)
    assert result == {
        "reverted_to": "a",
        "discarded": [],
        "spec": spec,
        "status": {"state": "running"},
    }


def test_manager_snapshot_delete_prepares_network_for_offline_merge(
    managed, monkeypatch
):
    mgr, conn, dom, spec = managed
    ensure = MagicMock()
    monkeypatch.setattr("mini_vps.manager.ensure_network_active", ensure)

    def _delete(c, d, vm, snap, before_offline_merge):
        before_offline_merge()

    monkeypatch.setattr("mini_vps.manager.snapshots.delete", _delete)

    assert mgr.snapshot_delete("web-1", "a") is None
    ensure.assert_called_once_with(conn, spec)


def test_manager_reinstall_discards_snapshots_before_recreating_overlay(
    managed, monkeypatch
):
    mgr, conn, dom, spec = managed
    dom.isActive.return_value = True
    monkeypatch.setattr("mini_vps.manager.read_pubkey", lambda: "ssh-ed25519 AAAA")
    monkeypatch.setattr("mini_vps.manager.build_seed_iso", MagicMock())
    monkeypatch.setattr("mini_vps.manager.ensure_network_active", MagicMock())
    order = MagicMock()
    discard = MagicMock(return_value=["a"])
    overlay = MagicMock()
    order.attach_mock(dom.destroy, "destroy")
    order.attach_mock(discard, "discard_all")
    order.attach_mock(overlay, "create_overlay_volume")
    order.attach_mock(dom.create, "create")
    monkeypatch.setattr("mini_vps.manager.snapshots.discard_all", discard)
    monkeypatch.setattr("mini_vps.manager.create_overlay_volume", overlay)
    mgr.get = MagicMock(return_value={"spec": spec, "status": {}})

    mgr.reinstall("web-1")

    assert order.mock_calls == [
        call.destroy(),
        call.discard_all(conn, dom, "web-1"),
        call.create_overlay_volume(conn, spec),
        call.create(),
    ]


def test_create_converge_keeps_snapshot_disk_source(monkeypatch):
    """収束(XMLDesc の差分編集)がスナップショット後のディスクの source を壊さない。"""
    conn = MagicMock()
    mgr = ServerManager(conn)
    dom = MagicMock()
    dom.isActive.return_value = False
    dom.XMLDesc.return_value = _domain_xml(
        "web-1.snap-b.qcow2", backing="web-1.snap-a.qcow2"
    )
    old_spec = _spec()
    new_spec = _spec(memory=2048, filters=[{"port": 22, "protocol": "tcp"}])
    monkeypatch.setattr("mini_vps.manager._find_domain", lambda c, n: dom)
    monkeypatch.setattr("mini_vps.manager._is_managed", lambda d: True)
    monkeypatch.setattr("mini_vps.manager._read_spec", lambda d: old_spec)
    monkeypatch.setattr("mini_vps.manager._write_spec", MagicMock())
    mgr.get = MagicMock(return_value={"spec": new_spec, "status": {}})

    mgr.create(new_spec)

    disk = _root_disk(conn.defineXML.call_args.args[0])
    assert disk.find("source").get("file") == f"{POOL_DIR}/web-1.snap-b.qcow2"
    store = disk.find("backingStore")
    assert store.find("source").get("file") == f"{POOL_DIR}/web-1.snap-a.qcow2"


# --- CLI ---


def _factory(mgr):
    return lambda: contextlib.nullcontext(mgr)


def test_cli_snapshot_create_passes_quiesce(capsys):
    mgr = MagicMock()
    mgr.snapshot_create.return_value = {"name": "a"}

    code = cli.main(
        ["snapshot", "create", "web-1", "a", "--quiesce"],
        manager_factory=_factory(mgr),
    )

    assert code == 0
    mgr.snapshot_create.assert_called_once_with("web-1", "a", quiesce=True)
    assert json.loads(capsys.readouterr().out) == {"name": "a"}


def test_cli_snapshot_list_prints_json(capsys):
    mgr = MagicMock()
    mgr.snapshot_list.return_value = [{"name": "a"}]

    code = cli.main(["snapshot", "list", "web-1"], manager_factory=_factory(mgr))

    assert code == 0
    assert json.loads(capsys.readouterr().out) == {"snapshots": [{"name": "a"}]}


def test_cli_snapshot_revert_and_delete(capsys):
    mgr = MagicMock()
    mgr.snapshot_revert.return_value = {"reverted_to": "a", "discarded": []}

    assert (
        cli.main(["snapshot", "revert", "web-1", "a"], manager_factory=_factory(mgr))
        == 0
    )
    assert (
        cli.main(["snapshot", "delete", "web-1", "a"], manager_factory=_factory(mgr))
        == 0
    )

    mgr.snapshot_revert.assert_called_once_with("web-1", "a")
    mgr.snapshot_delete.assert_called_once_with("web-1", "a")
    assert "deleted snapshot: web-1/a" in capsys.readouterr().out


def test_cli_snapshot_not_found_exits_10(capsys):
    mgr = MagicMock()
    mgr.snapshot_revert.side_effect = SnapshotNotFound("web-1/a")

    code = cli.main(["snapshot", "revert", "web-1", "a"], manager_factory=_factory(mgr))

    assert code == 10
    assert "snapshot not found" in capsys.readouterr().err


def test_cli_snapshot_invalid_name_exits_1(capsys):
    mgr = ServerManager(MagicMock())

    code = cli.main(
        ["snapshot", "create", "web-1", "a.b"], manager_factory=_factory(mgr)
    )

    assert code == 1


# --- Web API ---


@pytest.fixture
def client(monkeypatch):
    mock_manager = MagicMock()
    monkeypatch.setattr("mini_vps.api.libvirt.open", lambda uri: MagicMock())
    api_module.app.dependency_overrides[api_module.get_manager] = lambda: mock_manager
    with TestClient(api_module.app) as test_client:
        yield test_client, mock_manager
    api_module.app.dependency_overrides.clear()


def test_api_create_snapshot_returns_201(client):
    test_client, mgr = client
    mgr.snapshot_create.return_value = {"name": "a"}

    response = test_client.post(
        "/servers/web-1/snapshots", json={"name": "a", "quiesce": True}
    )

    assert response.status_code == 201
    assert response.json() == {"name": "a"}
    mgr.snapshot_create.assert_called_once_with("web-1", "a", quiesce=True)


def test_api_create_snapshot_rejects_invalid_name(client):
    test_client, mgr = client

    response = test_client.post("/servers/web-1/snapshots", json={"name": "a.b"})

    assert response.status_code == 422
    mgr.snapshot_create.assert_not_called()


def test_api_create_snapshot_conflict_is_409(client):
    test_client, mgr = client
    mgr.snapshot_create.side_effect = ServerConflict("web-1: a")

    response = test_client.post("/servers/web-1/snapshots", json={"name": "a"})

    assert response.status_code == 409


def test_api_list_snapshots(client):
    test_client, mgr = client
    mgr.snapshot_list.return_value = [{"name": "a"}]

    response = test_client.get("/servers/web-1/snapshots")

    assert response.status_code == 200
    assert response.json() == {"snapshots": [{"name": "a"}]}


def test_api_revert_snapshot(client):
    test_client, mgr = client
    mgr.snapshot_revert.return_value = {"reverted_to": "a", "discarded": ["b"]}

    response = test_client.post("/servers/web-1/snapshots/a/revert")

    assert response.status_code == 200
    mgr.snapshot_revert.assert_called_once_with("web-1", "a")


def test_api_revert_missing_snapshot_is_404(client):
    test_client, mgr = client
    mgr.snapshot_revert.side_effect = SnapshotNotFound("web-1/a")

    response = test_client.post("/servers/web-1/snapshots/a/revert")

    assert response.status_code == 404
    assert response.json()["detail"].startswith("snapshot not found")


def test_api_delete_snapshot_returns_204(client):
    test_client, mgr = client

    response = test_client.delete("/servers/web-1/snapshots/a")

    assert response.status_code == 204
    mgr.snapshot_delete.assert_called_once_with("web-1", "a")


def test_api_delete_snapshot_rejects_invalid_path(client):
    test_client, mgr = client

    response = test_client.delete("/servers/web-1/snapshots/a.b")

    assert response.status_code == 422
    mgr.snapshot_delete.assert_not_called()


def test_api_delete_on_old_libvirt_is_422(client):
    test_client, mgr = client
    mgr.snapshot_delete.side_effect = PlatformUnsupported("libvirt 9.0.0")

    response = test_client.delete("/servers/web-1/snapshots/a")

    assert response.status_code == 422
