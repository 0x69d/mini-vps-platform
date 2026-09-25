"""VM のディスクスナップショット(チェックポイント)。

UEFI(pflash の NVRAM)の VM では QEMU の internal snapshot が使えないため、ルート
ディスク(vda)だけの外部(external)disk-only スナップショットを使う。seed(x86 は
sda の cdrom、aarch64 は vdb)は snapshot='no' で対象外にする。メモリ状態は含まない。

## ファイルの並び(不変条件)

スナップショットは常に一直線に並べ、分岐させない。スナップショット S を取ると、
その時点のディスクは凍結され、以後の書き込みは overlay 用プールの
`{vm}.snap-{S}.qcow2` へ入る。したがって次が常に成り立つ。

- VM が今書き込んでいるファイル(アクティブ層)は `{vm}.snap-{current}.qcow2`
  (スナップショットが無ければ `{vm}.qcow2`)。
- スナップショット S の中身は `{vm}.snap-{S}.qcow2` の1つ下の層で、それは
  `{vm}.snap-{S の親}.qcow2`(親が無ければ `{vm}.qcow2`)。

ほかのモジュール(teardown・孤児検出)はこの命名 `{vm}.snap-{snap}.qcow2` を前提にする。

## libvirt に任せる操作と、自前で行う操作

- 作成: `virDomainSnapshotCreateXML`(DISK_ONLY | ATOMIC)。古くからある。
- 削除: `virDomainSnapshotDelete`。外部スナップショットの削除(block commit での
  マージ)は libvirt 9.0.0 以降。スナップショットの overlay をその下の層へ commit して
  ファイルを消すため、上の不変条件が保たれる。
- 巻き戻し(revert): 自前で行う。libvirt の外部スナップショットへの revert(9.9.0 以降)は
  次の理由で使わない。
  1. libvirt 10.0 は、稼働中に取った disk-only スナップショット(状態 disk-snapshot)への
     revert を "Invalid target domain state" で拒否する(11.1 で解消)。
  2. revert のたびに、seed を含む全ディスクへ `{元のファイル名}.{UNIX 時刻}` という
     名前の overlay を新しく作る。seed ISO が qcow2 の backing になり、命名の不変条件も
     崩れる。
  3. domain 定義全体をスナップショット時点に戻すため、metadata の spec や filterref
     まで巻き戻り、spec と実体が食い違う(削除済みの nwfilter を参照して起動できない
     こともある)。

  自前の revert は、対象より新しいスナップショットのメタデータとファイルを捨て、
  対象の overlay を空で作り直して domain のディスクをそこへ向けるだけで、domain 定義の
  ほかの部分には触れない。
"""

import datetime
import logging
import os
import xml.etree.ElementTree as ET
from collections.abc import Callable

import libvirt

from .config import (
    METADATA_KEY,
    METADATA_NS,
    OVERLAY_VOL_XML_TEMPLATE,
    POOL_NAME,
    QEMU_XML_NS,
)
from .errors import (
    PlatformUnsupported,
    ServerConflict,
    ServerNotRunning,
    SnapshotNotFound,
)

_LOGGER = logging.getLogger(__name__)

# domain XML を ElementTree で再シリアライズしても、qemu:commandline と
# minivps の metadata の接頭辞を ns0 などに書き換えないようにする。
ET.register_namespace("qemu", QEMU_XML_NS)
ET.register_namespace(METADATA_KEY, METADATA_NS)

# スナップショットの対象にするルートディスク(build_domain_xml が常にこの名前で作る)。
ROOT_DISK_TARGET = "vda"

# 外部スナップショットの削除(virDomainSnapshotDelete での block commit)に要る版。
MIN_DELETE_VERSION = (9, 0, 0)


def snapshot_file_name(vm: str, snap: str) -> str:
    """スナップショット snap を取った後の書き込み先 overlay のファイル名を返す。"""
    return f"{vm}.snap-{snap}.qcow2"


def is_snapshot_file(vm: str, file_name: str) -> bool:
    """file_name が VM vm のスナップショット overlay のファイル名か判定する。

    VM 名とスナップショット名はどちらも `.` を含まない(spec.py)ため、接頭辞
    `{vm}.snap-` で別の VM のファイルと取り違えることはない。
    """
    prefix = f"{vm}.snap-"
    return (
        file_name.startswith(prefix)
        and file_name.endswith(".qcow2")
        and len(file_name) > len(prefix) + len(".qcow2")
    )


def libvirt_version(conn) -> tuple[int, int, int]:
    """接続先の libvirt ライブラリの版を (major, minor, release) で返す。"""
    version = conn.getLibVersion()
    return (version // 1_000_000, version // 1_000 % 1_000, version % 1_000)


def require_libvirt_version(conn, minimum: tuple[int, int, int], what: str) -> None:
    """接続先の libvirt が minimum 以上でなければ PlatformUnsupported で拒否する。

    Raises:
        PlatformUnsupported: libvirt が古い場合。
    """
    current = libvirt_version(conn)
    if current < minimum:
        raise PlatformUnsupported(
            f"{what}には libvirt {'.'.join(map(str, minimum))} 以上が必要です"
            f"(このホストは {'.'.join(map(str, current))})"
        )


def _disk_target(disk: ET.Element) -> str | None:
    """Domain XML の <disk> 要素から target dev を返す。"""
    target = disk.find("target")
    return target.get("dev") if target is not None else None


def _root_disk(root: ET.Element) -> ET.Element:
    """Domain XML からルートディスク(vda)の <disk> 要素を返す。

    Raises:
        ServerConflict: ルートディスクが無い(mini-vps が作った domain でない)場合。
    """
    for disk in root.findall("devices/disk"):
        if _disk_target(disk) == ROOT_DISK_TARGET:
            return disk
    raise ServerConflict(f"ルートディスク {ROOT_DISK_TARGET} が domain にありません")


def root_disk_source(domain_xml: str) -> str:
    """Domain XML からルートディスク(vda)の source ファイルのパスを返す。"""
    source = _root_disk(ET.fromstring(domain_xml)).find("source")
    if source is None or not source.get("file"):
        raise ServerConflict(f"ルートディスク {ROOT_DISK_TARGET} に source が無い")
    return source.get("file")


def set_root_disk_source_xml(domain_xml: str, path: str) -> str:
    """Domain XML のルートディスク(vda)の source だけを path へ書き換えて返す。

    vda の <backingStore> は取り除く。libvirt が出力した backingStore は書き換え前の
    source の backing chain を表しており、残すと新しい source に古い chain を当てはめて
    しまう。要素が無ければ libvirt は起動時にイメージのヘッダから chain を調べ直す
    (空の <backingStore/> は「backing 無し」の意味になるため、空にもしない)。
    それ以外の要素・属性は変えない(resize_domain_xml と同じ最小差分編集)。
    """
    root = ET.fromstring(domain_xml)
    disk = _root_disk(root)
    source = disk.find("source")
    if source is None:
        source = ET.SubElement(disk, "source")
    source.set("file", path)
    for backing in disk.findall("backingStore"):
        disk.remove(backing)
    return ET.tostring(root, encoding="unicode")


def build_snapshot_xml(snap: str, domain_xml: str, overlay_path: str) -> str:
    """外部 disk-only スナップショットの XML を組み立てる(外部依存ゼロ)。

    domain XML の全ディスクを列挙し、ルートディスク(vda)だけを external にして
    overlay_path を書き込み先にする。それ以外(seed の sda / vdb)は snapshot='no'。
    libvirt の既定に任せると読み取り専用の seed まで外部スナップショットの対象になりうる
    ため、全ディスクを明示する。

    Args:
        snap: スナップショット名(spec.validate_snapshot_name で検証済み)。
        domain_xml: 対象 domain の XML(ディスクの一覧に使う)。
        overlay_path: スナップショット後の書き込み先 overlay の絶対パス。
    """
    root = ET.Element("domainsnapshot")
    ET.SubElement(root, "name").text = snap
    ET.SubElement(root, "memory", snapshot="no")
    disks = ET.SubElement(root, "disks")
    for disk in ET.fromstring(domain_xml).findall("devices/disk"):
        target = _disk_target(disk)
        if target is None:
            continue
        if target == ROOT_DISK_TARGET:
            el = ET.SubElement(disks, "disk", name=target, snapshot="external")
            ET.SubElement(el, "driver", type="qcow2")
            ET.SubElement(el, "source", file=overlay_path)
        else:
            ET.SubElement(disks, "disk", name=target, snapshot="no")
    ET.indent(root)
    return ET.tostring(root, encoding="unicode")


def parse_snapshot_xml(xml_text: str) -> dict:
    """スナップショットの XML から一覧に出す値を取り出す(外部依存ゼロ)。

    Returns:
        name・created_at(UTC の ISO 8601)・parent(無ければ None)・state
        (スナップショットを取ったときの VM の状態。稼働中・一時停止中に取ったものは
        "disk-snapshot"、停止中は "shutoff")・root_file(ルートディスクの外部
        overlay のパス。外部スナップショットでなければ None)を持つ dict。
    """
    root = ET.fromstring(xml_text)
    parent = root.findtext("parent/name")
    created = root.findtext("creationTime")
    created_at = (
        datetime.datetime.fromtimestamp(int(created), datetime.UTC).isoformat()
        if created
        else None
    )
    root_file = None
    for disk in root.findall("disks/disk"):
        if disk.get("name") == ROOT_DISK_TARGET and disk.get("snapshot") == "external":
            source = disk.find("source")
            root_file = source.get("file") if source is not None else None
    return {
        "name": root.findtext("name"),
        "created_at": created_at,
        "parent": parent,
        "state": root.findtext("state"),
        "root_file": root_file,
    }


def linear_chain(parents: dict[str, str | None], current: str | None) -> list[str]:
    """スナップショットを古い順に並べる。一直線でなければ ValueError(外部依存ゼロ)。

    Args:
        parents: スナップショット名から親の名前(根なら None)への dict。
        current: libvirt の current スナップショットの名前(無ければ None)。

    Returns:
        根から current までのスナップショット名のリスト。

    Raises:
        ValueError: current が末端でない・枝分かれがある・親が欠けている場合。
    """
    if not parents:
        return []
    if current is None or current not in parents:
        raise ValueError("current スナップショットが定まっていません")
    chain = []
    node = current
    while node is not None:
        if node in chain or node not in parents:
            raise ValueError(f"スナップショット {node!r} の親子関係が壊れています")
        chain.append(node)
        node = parents[node]
    if len(chain) != len(parents):
        others = sorted(set(parents) - set(chain))
        raise ValueError(f"current から辿れないスナップショットがあります: {others}")
    chain.reverse()
    return chain


def snapshot_info(snapshot) -> dict:
    """スナップショット1件(virDomainSnapshot)を一覧用の dict にする。"""
    info = parse_snapshot_xml(snapshot.getXMLDesc(0))
    del info["root_file"]
    info["current"] = bool(snapshot.isCurrent(0))
    return info


def _find_snapshot(dom, snap: str):
    """名前でスナップショットを引く。無ければ None。"""
    try:
        return dom.snapshotLookupByName(snap, 0)
    except libvirt.libvirtError as e:
        if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN_SNAPSHOT:
            return None
        raise


def _lookup_snapshot(dom, vm: str, snap: str):
    """名前でスナップショットを引く。無ければ SnapshotNotFound。"""
    snapshot = _find_snapshot(dom, snap)
    if snapshot is None:
        raise SnapshotNotFound(f"{vm}/{snap}")
    return snapshot


def _load_chain(dom, vm: str) -> tuple[dict[str, dict], list[str]]:
    """全スナップショットを読み、一直線で mini-vps の命名どおりであることを確かめる。

    Returns:
        (スナップショット名から parse_snapshot_xml の結果への dict,
        古い順の名前のリスト)。

    Raises:
        ServerConflict: virsh などで mini-vps の外から作った・巻き戻したために、
            一直線でない、または overlay の名前が命名規則と違う場合。自前の revert と
            libvirt の delete はどちらもこの前提の上でしか安全に動かないため拒否する。
    """
    infos = {}
    current = None
    for snapshot in dom.listAllSnapshots(0):
        info = parse_snapshot_xml(snapshot.getXMLDesc(0))
        infos[info["name"]] = info
        if snapshot.isCurrent(0):
            current = info["name"]
    try:
        chain = linear_chain({n: i["parent"] for n, i in infos.items()}, current)
    except ValueError as e:
        raise ServerConflict(
            f"{vm}: スナップショットが一直線に並んでいないため操作できません({e})"
        ) from None
    for name in chain:
        root_file = infos[name]["root_file"]
        if root_file is None or os.path.basename(root_file) != snapshot_file_name(
            vm, name
        ):
            raise ServerConflict(
                f"{vm}: スナップショット {name} は mini-vps の外で作られたため"
                "操作できません"
            )
    return infos, chain


def _vps_pool(conn):
    """Overlay 用プールを引いて refresh して返す(無ければ None)。

    スナップショットの overlay は libvirt がプールの API を通さずに作る・消すため、
    refresh しないと listAllVolumes に現れない(消えたものが残る)。非アクティブな
    プールは refresh も一覧もできないため、先に起動する(ensure_pool と同じ扱い)。
    """
    try:
        pool = conn.storagePoolLookupByName(POOL_NAME)
    except libvirt.libvirtError as e:
        if e.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_POOL:
            return None
        raise
    if not pool.isActive():
        pool.create(0)
    pool.refresh(0)
    return pool


def _volume_names(pool) -> set[str]:
    """プール内の volume 名の集合を返す。"""
    return {v.name() for v in pool.listAllVolumes()}


def _delete_volume(pool, vol_name: str) -> None:
    """プールに vol_name があれば削除する(無ければ何もしない)。"""
    if vol_name in _volume_names(pool):
        pool.storageVolLookupByName(vol_name).delete(0)


def create(conn, dom, vm: str, snap: str, quiesce: bool = False) -> dict:
    """ルートディスクの外部 disk-only スナップショットを作る。

    稼働中でも停止中でも取れる。稼働中に取ったものは電源を突然切ったときと同じ
    crash-consistent な状態になる。quiesce=True なら guest agent でゲストの
    ファイルシステムを凍結(fsfreeze)してから取るため、書きかけのデータが残らない。

    Args:
        conn: libvirt 接続。
        dom: 対象 domain。
        vm: VM 名。
        snap: スナップショット名(検証済み)。
        quiesce: guest agent でファイルシステムを凍結してから取るか。

    Returns:
        作ったスナップショットの一覧用 dict(snapshot_info)。

    Raises:
        ServerConflict: 同名のスナップショットか overlay ファイルが既にある場合。
        ServerNotRunning: quiesce=True なのに VM が稼働中(running)でない場合。
            libvirt は停止中の VM では quiesce を黙って無視し、一時停止中の VM では
            fsfreeze に失敗するため、ここで明示的に拒否する。
    """
    if _find_snapshot(dom, snap) is not None:
        raise ServerConflict(f"{vm}: スナップショット {snap} は既に存在します")
    _load_chain(dom, vm)
    if quiesce and dom.state()[0] != libvirt.VIR_DOMAIN_RUNNING:
        raise ServerNotRunning(f"{vm} (quiesce には稼働中の VM が必要です)")

    domain_xml = dom.XMLDesc(0)
    file_name = snapshot_file_name(vm, snap)
    overlay_path = os.path.join(
        os.path.dirname(root_disk_source(domain_xml)), file_name
    )
    pool = _vps_pool(conn)
    if pool is not None and file_name in _volume_names(pool):
        raise ServerConflict(
            f"{vm}: {file_name} が既にあります(前回の失敗の残骸の可能性があります)"
        )

    flags = (
        libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_DISK_ONLY
        | libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_ATOMIC
    )
    if quiesce:
        flags |= libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_QUIESCE
    snapshot = dom.snapshotCreateXML(
        build_snapshot_xml(snap, domain_xml, overlay_path), flags
    )
    if pool is not None:
        pool.refresh(0)
    _LOGGER.info("%s: スナップショット %s を作成 quiesce=%s", vm, snap, quiesce)
    return snapshot_info(snapshot)


def list_snapshots(dom) -> list[dict]:
    """スナップショットの一覧を古い順に返す。

    一直線に並んでいれば親子関係の順(根が先頭、current が末尾)に並べる。
    libvirt の作成時刻は秒単位で、同じ秒に取ったものの順序を決められないため。
    一直線でなければ(mini-vps の外で作られたものがある)作成時刻と名前の順にする。
    """
    infos = [snapshot_info(s) for s in dom.listAllSnapshots(0)]
    current = next((i["name"] for i in infos if i["current"]), None)
    try:
        chain = linear_chain({i["name"]: i["parent"] for i in infos}, current)
    except ValueError:
        return sorted(infos, key=lambda i: (i["created_at"] or "", i["name"]))
    by_name = {i["name"]: i for i in infos}
    return [by_name[name] for name in chain]


def revert(
    conn,
    dom,
    spec: dict,
    snap: str,
    before_start: Callable[[], None] | None = None,
) -> dict:
    """ルートディスクをスナップショット snap の時点へ巻き戻す。

    手順は次のとおり。VM の電源状態(稼働中・一時停止中・停止中)は巻き戻しの前後で
    保つ。disk-only スナップショットはメモリ状態を含まないため、稼働中の VM は
    強制停止(destroy)してから巻き戻し、ディスクから起動し直す(電源断からの復帰と同じ)。

    1. 稼働中なら destroy する。
    2. domain のルートディスクを `{vm}.snap-{snap}.qcow2` へ向ける(snap より新しい
       スナップショットがあれば、今はその overlay を指している)。
    3. snap より新しいスナップショットのメタデータを新しい順に消す
       (METADATA_ONLY。current は親へ移り、最後は snap が current になる)。
    4. snap より新しい overlay と snap の overlay のファイルを消し、snap の overlay を
       空で作り直す(backing は snap の中身である1つ下の層)。
    5. 元が稼働中なら起動し、一時停止中なら一時停止の状態で起動する。

    途中で失敗しても、同じ snap へもう一度 revert すれば続きから収束する(各手順は
    やり直しても結果が変わらない)。ただし失敗時に VM は停止したままになり、やり直した
    revert はその停止状態を保つ。

    Args:
        conn: libvirt 接続。
        dom: 対象 domain。
        spec: 対象 VM の spec(name と disk を使う)。
        snap: 巻き戻し先のスナップショット名(検証済み)。
        before_start: 5. で起動する直前に呼ぶ関数(ネットワークの起動など)。

    Returns:
        reverted_to(巻き戻し先)と discarded(捨てた新しいスナップショット名の
        リスト)を持つ dict。

    Raises:
        SnapshotNotFound: snap が無い場合。
        ServerConflict: スナップショットが一直線でない、または mini-vps の外で
            作られたものがある場合。
    """
    vm = spec["name"]
    _lookup_snapshot(dom, vm, snap)
    infos, chain = _load_chain(dom, vm)
    index = chain.index(snap)
    discarded = chain[index + 1 :]
    target_path = infos[snap]["root_file"]
    overlay_dir = os.path.dirname(target_path)
    backing_name = (
        snapshot_file_name(vm, chain[index - 1]) if index > 0 else f"{vm}.qcow2"
    )

    state = dom.state()[0]
    was_active = bool(dom.isActive())
    _LOGGER.info("%s: スナップショット %s へ巻き戻す discarded=%s", vm, snap, discarded)
    if was_active:
        dom.destroy()

    conn.defineXML(
        set_root_disk_source_xml(
            dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE), target_path
        )
    )
    for name in reversed(discarded):
        dom.snapshotLookupByName(name, 0).delete(
            libvirt.VIR_DOMAIN_SNAPSHOT_DELETE_METADATA_ONLY
        )

    pool = _vps_pool(conn)
    if pool is None:
        raise ServerConflict(f"{vm}: overlay 用プール {POOL_NAME} がありません")
    # snap より上の層と snap の層を上から消す。前回の revert が 3. の途中で失敗して
    # メタデータだけ先に消えた層も、残す層(snap より古いもの)以外は消して回収する。
    keep = {snapshot_file_name(vm, name) for name in chain[:index]}
    remove = [snapshot_file_name(vm, name) for name in [*reversed(discarded), snap]]
    remove += sorted(
        n
        for n in _volume_names(pool)
        if is_snapshot_file(vm, n) and n not in keep and n not in remove
    )
    for vol_name in remove:
        _delete_volume(pool, vol_name)
    pool.createXML(
        OVERLAY_VOL_XML_TEMPLATE.format(
            name=snapshot_file_name(vm, snap).removesuffix(".qcow2"),
            disk=spec["disk"],
            base_path=os.path.join(overlay_dir, backing_name),
        ),
        0,
    )

    if was_active:
        if before_start is not None:
            before_start()
        if state == libvirt.VIR_DOMAIN_PAUSED:
            dom.createWithFlags(libvirt.VIR_DOMAIN_START_PAUSED)
        else:
            dom.create()
    _LOGGER.info("%s: スナップショット %s へ巻き戻した", vm, snap)
    return {"reverted_to": snap, "discarded": discarded}


def delete(
    conn,
    dom,
    vm: str,
    snap: str,
    before_offline_merge: Callable[[], None] | None = None,
) -> None:
    """スナップショットを削除する(今のディスクの中身は変わらない)。

    libvirt の virDomainSnapshotDelete に任せる。libvirt は `{vm}.snap-{snap}.qcow2` を
    その下の層へ block commit してから消すため、ファイルの並びの不変条件は保たれる。
    消えるのは「snap の時点へ戻れること」だけで、VM が今見ているディスクの内容は
    変わらない。
    停止中の VM では、libvirt が commit のために QEMU を一時停止状態で起動して終われば
    止める。そのとき VM のネットワークが要るため、before_offline_merge を先に呼ぶ。

    Raises:
        PlatformUnsupported: libvirt が 9.0.0 より古い場合。
        SnapshotNotFound: snap が無い場合。
        ServerConflict: スナップショットが一直線でない、または mini-vps の外で
            作られたものがある場合。
    """
    require_libvirt_version(conn, MIN_DELETE_VERSION, "外部スナップショットの削除")
    snapshot = _lookup_snapshot(dom, vm, snap)
    _load_chain(dom, vm)
    if not dom.isActive() and before_offline_merge is not None:
        before_offline_merge()
    snapshot.delete(0)
    # commit 済みの overlay は libvirt が消す(VIR_DOMAIN_BLOCK_COMMIT_DELETE)。
    # こちらでは消さず、プールの一覧を実ファイルに合わせるだけにする。
    _vps_pool(conn)
    _LOGGER.info("%s: スナップショット %s を削除", vm, snap)


def discard_all(conn, dom, vm: str) -> list[str]:
    """停止中の VM のスナップショットを、中身をマージせずにすべて捨てる。

    reinstall(ディスクを base から作り直す)の前処理。メタデータを METADATA_ONLY で
    消し、ルートディスクを `{vm}.qcow2` へ向け直してから、`{vm}.snap-*.qcow2` を消す。
    スナップショットもそのファイルも無ければ domain には触れない。

    Args:
        conn: libvirt 接続。
        dom: 対象 domain(停止中であること)。
        vm: VM 名。

    Returns:
        捨てたスナップショット名のリスト(名前順)。
    """
    snapshots = list(dom.listAllSnapshots(0))
    pool = _vps_pool(conn)
    files = (
        sorted(n for n in _volume_names(pool) if is_snapshot_file(vm, n))
        if pool is not None
        else []
    )
    if not snapshots and not files:
        return []

    names = sorted(s.getName() for s in snapshots)
    for snapshot in snapshots:
        snapshot.delete(libvirt.VIR_DOMAIN_SNAPSHOT_DELETE_METADATA_ONLY)

    xml = dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
    current_source = root_disk_source(xml)
    base_path = os.path.join(os.path.dirname(current_source), f"{vm}.qcow2")
    if current_source != base_path:
        conn.defineXML(set_root_disk_source_xml(xml, base_path))

    for file_name in files:
        pool.storageVolLookupByName(file_name).delete(0)
    _LOGGER.info("%s: スナップショットをすべて破棄 snapshots=%s", vm, names)
    return names
