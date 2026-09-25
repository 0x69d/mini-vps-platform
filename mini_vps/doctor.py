"""ホストの前提の検査(doctor)と孤児リソースの回収(gc)。

孤児リソースとは、minivps の命名規則に従うのに対応する domain が無いものを指す。

- overlay 用プール(vps-pool)の `{vm}.qcow2` と、スナップショットの
  `{vm}.snap-*.qcow2`
- seed 用プール(vps-seeds)の `{vm}-seed.iso`
- nwfilter `minivps-{vm}`(HostProfile.supports_nwfilter のときだけ)

VM 名は `.` を含まない(spec.py の _NAME_PATTERN)ため、volume 名から VM 名を
一意に取り出せる。同名の domain が管理対象外であっても存在するなら、その domain が
使っている可能性があるため孤児とはみなさず、doctor で警告するだけにする。

作成途中の VM は seed ISO と overlay を domain の define より先に作る。検出した時点の
孤児をそのまま消すと進行中の create を壊すため、gc は孤児ごとに
`ServerManager._locked(vm)` を取ってから再判定して消す(create はロックを provision の
完了まで保持するので、ロックが取れた時点で domain が無ければ本当に孤児である)。

各関数が受け取る `mgr` は ServerManager(`conn`・`managed_specs()`・`_locked()` を
使う)。manager.py からの循環 import を避けるため型では参照しない。
"""

import dataclasses
import logging
import os
import re

import libvirt

from .config import BASE_POOL, POOL_NAME, SEED_POOL_NAME
from .platform_profile import NETWORK_LIBVIRT, HostProfile, get_profile
from .resources import _network_name

_LOGGER = logging.getLogger(__name__)

OK = "ok"
WARN = "warn"
ERROR = "error"

_VM = r"(?P<vm>[A-Za-z0-9][A-Za-z0-9_-]{0,62})"
_VOLUME_PATTERNS = {
    POOL_NAME: [
        ("overlay", re.compile(rf"^{_VM}\.qcow2$")),
        ("snapshot", re.compile(rf"^{_VM}\.snap-[^/]+\.qcow2$")),
    ],
    SEED_POOL_NAME: [("seed", re.compile(rf"^{_VM}-seed\.iso$"))],
}
_NWFILTER_PATTERN = re.compile(rf"^minivps-{_VM}$")


@dataclasses.dataclass(frozen=True)
class Resource:
    """minivps の命名規則に従うリソース1件。

    Attributes:
        kind: "overlay" / "snapshot" / "seed" / "nwfilter"。
        vm: 名前から取り出した VM 名。
        name: volume 名または nwfilter 名。
        pool: volume の属するプール名。nwfilter なら None。
    """

    kind: str
    vm: str
    name: str
    pool: str | None = None

    @property
    def label(self) -> str:
        """表示用の識別子(例: "vps-pool/web-1.qcow2"、"nwfilter/minivps-web-1")。"""
        return f"{self.pool or 'nwfilter'}/{self.name}"

    def as_dict(self) -> dict:
        """API / CLI 向けの dict を返す。"""
        return dataclasses.asdict(self)


def classify_volume(pool: str, name: str) -> Resource | None:
    """プール内の volume 名を minivps のリソースとして解釈する(該当しなければ None)。"""
    for kind, pattern in _VOLUME_PATTERNS.get(pool, []):
        m = pattern.match(name)
        if m:
            return Resource(kind=kind, vm=m["vm"], name=name, pool=pool)
    return None


def classify_nwfilter(name: str) -> Resource | None:
    """Nwfilter 名を minivps のリソースとして解釈する(該当しなければ None)。"""
    m = _NWFILTER_PATTERN.match(name)
    return Resource(kind="nwfilter", vm=m["vm"], name=name) if m else None


def split_orphans(
    resources: list[Resource], domain_names: set[str], managed_names: set[str]
) -> tuple[list[Resource], list[Resource]]:
    """リソースを「孤児」と「管理対象外の同名 domain が持つもの」に分ける(純粋関数)。

    Args:
        resources: minivps の命名規則に従うリソース。
        domain_names: 存在する全 domain(管理対象外を含む)の名前。
        managed_names: 管理対象 domain の名前。

    Returns:
        (orphans, shadowed)。orphans は同名の domain が1つも無いもの(gc の対象)、
        shadowed は同名の domain が管理対象外で存在するもの(gc しない)。
    """
    orphans = [r for r in resources if r.vm not in domain_names]
    shadowed = [
        r for r in resources if r.vm in domain_names and r.vm not in managed_names
    ]
    return orphans, shadowed


def _entry(level: str, check: str, detail: str) -> dict:
    """検査結果1件の dict を作る。"""
    return {"level": level, "check": check, "detail": detail}


def network_checks(
    refs: dict[str, list[str]], states: dict[str, tuple[bool, bool] | None]
) -> list[dict]:
    """管理 VM が参照する libvirt ネットワークの検査結果を返す(純粋関数)。

    Args:
        refs: ネットワーク名 → 参照している VM 名のリスト。
        states: ネットワーク名 → (active, autostart)。存在しなければ None。

    Returns:
        検査結果のリスト。無い: error、非アクティブ・autostart 無効: warn。
    """
    results = []
    for net in sorted(refs):
        vms = ", ".join(sorted(refs[net]))
        state = states.get(net)
        if state is None:
            results.append(_entry(ERROR, f"network:{net}", f"存在しない(参照: {vms})"))
            continue
        active, autostart = state
        problems = []
        if not active:
            problems.append("非アクティブ(VM の起動時に起動される)")
        if not autostart:
            problems.append("autostart が無効(ホスト再起動後に VM が起動できない)")
        if problems:
            results.append(_entry(WARN, f"network:{net}", "、".join(problems)))
        else:
            results.append(_entry(OK, f"network:{net}", "アクティブ・autostart 有効"))
    return results


def base_image_checks(
    refs: dict[str, list[str]], available: set[str] | None
) -> list[dict]:
    """管理 VM が参照する base image の検査結果を返す(純粋関数)。

    Args:
        refs: base image 名 → 参照している VM 名のリスト。
        available: `images` プールにある volume 名。プール自体が無ければ None。

    Returns:
        検査結果のリスト。無い base image は error(overlay の backing file が
        無くなり VM が起動・reinstall できない)。
    """
    if available is None:
        return [_entry(ERROR, f"pool:{BASE_POOL}", "base image 用プールが無い")]
    results = []
    for image in sorted(refs):
        vms = ", ".join(sorted(refs[image]))
        if image in available:
            results.append(_entry(OK, f"base_image:{image}", f"存在する(参照: {vms})"))
        else:
            results.append(
                _entry(ERROR, f"base_image:{image}", f"存在しない(参照: {vms})")
            )
    return results


def lock_dir_check(lock_dir: str, access=os.access) -> dict:
    """ロックディレクトリに書けるかを検査する。

    まだ無ければ、最も近い既存の親ディレクトリに作れるかで判定する(NameLocks は
    初回のロック取得時に makedirs する)。書けなければ name 単位ロックがプロセス内に
    限られ、CLI と API が別プロセスで同じ VM を並行操作しうるため warn にする。

    Args:
        lock_dir: HostProfile.lock_dir。
        access: os.access 互換の関数(テスト用の差し替え口)。

    Returns:
        検査結果。
    """
    path = lock_dir
    while not os.path.isdir(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    if access(path, os.W_OK | os.X_OK):
        detail = "書き込める" if path == lock_dir else f"未作成({path} に作成できる)"
        return _entry(OK, "lock_dir", f"{lock_dir}: {detail}")
    return _entry(
        WARN,
        "lock_dir",
        f"{lock_dir}: 書き込めない(プロセス間の直列化が効かない)",
    )


def accel_check(accel: str) -> dict:
    """アクセラレータの検査結果を返す(TCG は動くが遅いため warn)。"""
    if accel == "tcg":
        return _entry(
            WARN, "accelerator", "tcg(ハードウェア支援なし。KVM/HVF より大幅に遅い)"
        )
    return _entry(OK, "accelerator", accel)


def autostart_checks(expected: dict[str, bool], actual: dict[str, bool]) -> list[dict]:
    """Spec の autostart と実際の domain の autostart の食い違いを返す(純粋関数)。

    Args:
        expected: VM 名 → spec の autostart。
        actual: VM 名 → `dom.autostart()` の真偽。

    Returns:
        食い違いごとの warn(同じ spec で create/PUT し直せば収束する)。
        食い違いが無ければ ok を1件。
    """
    results = [
        _entry(
            WARN,
            f"autostart:{vm}",
            f"spec は {expected[vm]} だが domain は {actual[vm]}",
        )
        for vm in sorted(expected)
        if vm in actual and expected[vm] != actual[vm]
    ]
    return results or [_entry(OK, "autostart", "spec と domain が一致")]


def orphan_checks(orphans: list[Resource], shadowed: list[Resource]) -> list[dict]:
    """孤児リソースの検査結果を返す(純粋関数)。"""
    results = [
        _entry(WARN, f"orphan:{r.label}", f"domain {r.vm} が無い(gc --apply で回収)")
        for r in orphans
    ]
    results += [
        _entry(
            WARN,
            f"unmanaged:{r.label}",
            f"管理対象外の domain {r.vm} が同名のため gc しない",
        )
        for r in shadowed
    ]
    return results or [_entry(OK, "orphans", "孤児リソースなし")]


def _existing_pools(conn) -> set[str]:
    """定義済みのストレージプール名を返す。"""
    return {p.name() for p in conn.listAllStoragePools()}


def collect_resources(conn, profile: HostProfile) -> list[Resource]:
    """命名規則に従う volume と nwfilter を libvirt から集める。"""
    resources = []
    pools = _existing_pools(conn)
    for pool_name in (POOL_NAME, SEED_POOL_NAME):
        if pool_name not in pools:
            continue
        pool = conn.storagePoolLookupByName(pool_name)
        for vol in pool.listAllVolumes():
            resource = classify_volume(pool_name, vol.name())
            if resource is not None:
                resources.append(resource)
    if profile.supports_nwfilter:
        for f in conn.listAllNWFilters():
            resource = classify_nwfilter(f.name())
            if resource is not None:
                resources.append(resource)
    return resources


def find_orphans(mgr) -> tuple[list[Resource], list[Resource]]:
    """孤児リソースと、管理対象外の同名 domain が持つリソースを返す。

    Returns:
        split_orphans() と同じ (orphans, shadowed)。
    """
    conn = mgr.conn
    resources = collect_resources(conn, get_profile())
    domain_names = {d.name() for d in conn.listAllDomains()}
    managed_names = set(mgr.managed_specs())
    return split_orphans(resources, domain_names, managed_names)


def _network_states(conn, names) -> dict[str, tuple[bool, bool] | None]:
    """ネットワーク名 → (active, autostart)。存在しなければ None。"""
    states = {}
    for name in names:
        try:
            net = conn.networkLookupByName(name)
        except libvirt.libvirtError as e:
            if e.get_error_code() == libvirt.VIR_ERR_NO_NETWORK:
                states[name] = None
                continue
            raise
        states[name] = (bool(net.isActive()), bool(net.autostart()))
    return states


def _domain_autostarts(conn, names) -> dict[str, bool]:
    """VM 名 → 実際の autostart。検査中に消えた domain は含めない。"""
    actual = {}
    for name in names:
        try:
            actual[name] = bool(conn.lookupByName(name).autostart())
        except libvirt.libvirtError as e:
            if e.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN:
                raise
    return actual


def _base_image_names(conn) -> set[str] | None:
    """`images` プールの volume 名。プールが無ければ None。"""
    if BASE_POOL not in _existing_pools(conn):
        return None
    pool = conn.storagePoolLookupByName(BASE_POOL)
    pool.refresh(0)
    return {v.name() for v in pool.listAllVolumes()}


def run_checks(mgr) -> list[dict]:
    """ホストの前提と孤児リソースを検査し、結果のリストを返す。

    Returns:
        {level: "ok"|"warn"|"error", check, detail} のリスト。
    """
    conn = mgr.conn
    profile = get_profile()
    specs = mgr.managed_specs()
    results = []

    if profile.network_mode == NETWORK_LIBVIRT:
        net_refs: dict[str, list[str]] = {}
        for vm, spec in specs.items():
            for net in spec.get("networks") or ["default"]:
                net_refs.setdefault(_network_name(net), []).append(vm)
        results += network_checks(net_refs, _network_states(conn, net_refs))

    image_refs: dict[str, list[str]] = {}
    for vm, spec in specs.items():
        if spec.get("base_image"):
            image_refs.setdefault(spec["base_image"], []).append(vm)
    results += base_image_checks(image_refs, _base_image_names(conn))

    results.append(lock_dir_check(profile.lock_dir))
    results.append(accel_check(profile.accel))

    expected = {vm: bool(spec.get("autostart", True)) for vm, spec in specs.items()}
    results += autostart_checks(expected, _domain_autostarts(conn, expected))

    results += orphan_checks(*find_orphans(mgr))
    return results


def has_error(results: list[dict]) -> bool:
    """検査結果に error が1件でもあれば True。"""
    return any(r["level"] == ERROR for r in results)


def _domain_exists(conn, name: str) -> bool:
    """管理対象かに関わらず、同名の domain が存在するか。"""
    try:
        conn.lookupByName(name)
    except libvirt.libvirtError as e:
        if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
            return False
        raise
    return True


_GONE_CODES = (
    libvirt.VIR_ERR_NO_STORAGE_POOL,
    libvirt.VIR_ERR_NO_STORAGE_VOL,
    libvirt.VIR_ERR_NO_NWFILTER,
)


def _remove(conn, resource: Resource) -> bool:
    """リソースを1件消す。既に無ければ False を返す。"""
    try:
        if resource.pool is None:
            conn.nwfilterLookupByName(resource.name).undefine()
        else:
            pool = conn.storagePoolLookupByName(resource.pool)
            pool.storageVolLookupByName(resource.name).delete(0)
    except libvirt.libvirtError as e:
        if e.get_error_code() in _GONE_CODES:
            return False
        raise
    return True


def gc(mgr, apply: bool = False) -> dict:
    """孤児リソースを回収する。既定は dry-run で、消す予定を返すだけ。

    apply=True のときは VM 名ごとに `mgr._locked(vm)` を取り、ロックの内側で
    「同名の domain が無い」ことを再判定してから消す。進行中の create は provision
    の完了までロックを保持するため、ロックが取れた時点で domain があればその
    リソースは使われており、消さずに skipped へ回す。1件の削除に失敗しても残りは続ける。

    Args:
        mgr: ServerManager。
        apply: True なら実際に消す。

    Returns:
        applied(bool)・orphans・removed・skipped のキーを持つ dict。orphans は
        検出した孤児、removed は消したもの、skipped は再判定で消さなかったもの
        (reason 付き)。dry-run では removed と skipped は空。
    """
    orphans, _shadowed = find_orphans(mgr)
    result = {
        "applied": apply,
        "orphans": [r.as_dict() for r in orphans],
        "removed": [],
        "skipped": [],
    }
    if not apply:
        return result

    by_vm: dict[str, list[Resource]] = {}
    for r in orphans:
        by_vm.setdefault(r.vm, []).append(r)

    for vm in sorted(by_vm):
        with mgr._locked(vm):
            if _domain_exists(mgr.conn, vm):
                for r in by_vm[vm]:
                    result["skipped"].append(
                        {**r.as_dict(), "reason": "domain が作成された"}
                    )
                continue
            for r in by_vm[vm]:
                try:
                    removed = _remove(mgr.conn, r)
                except libvirt.libvirtError as e:
                    _LOGGER.warning("%s: 孤児 %s の削除に失敗: %s", vm, r.label, e)
                    result["skipped"].append({**r.as_dict(), "reason": str(e)})
                    continue
                if removed:
                    _LOGGER.info("%s: 孤児 %s を削除", vm, r.label)
                    result["removed"].append(r.as_dict())
                else:
                    result["skipped"].append({**r.as_dict(), "reason": "既に無い"})
    return result
