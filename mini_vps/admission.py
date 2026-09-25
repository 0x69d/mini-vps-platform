"""作成前の容量チェック(admission control)。

ホスト1台に VM を約束しすぎないために、新規作成と memory/vcpus の拡張の前に
メモリ・vCPU・base image の仮想サイズを確かめ、足りなければ `InsufficientCapacity`
で拒否する。overlay 用プールの空きは thin provisioning のため警告に留める。

判定(`evaluate`)は素の値だけを受け取る純粋関数にし、libvirt から値を集める関数
(`host_capacity` など)と分ける。式と設定値は docs/operations.md を参照。
"""

import dataclasses
import logging
import math
import os
from collections.abc import Callable

import libvirt

from .config import BASE_POOL, POOL_NAME
from .errors import InsufficientCapacity

_LOGGER = logging.getLogger(__name__)

GIB = 1024**3

RESERVE_ENV_VAR = "MINIVPS_MEMORY_RESERVE_MIB"
OVERCOMMIT_ENV_VAR = "MINIVPS_MEMORY_OVERCOMMIT"

# 予約の既定値は「ホストメモリの 10%」と「2 GiB」の大きいほう。ホスト OS・libvirtd・
# QEMU プロセス自身のオーバーヘッドの分を VM に配らずに残す。
_DEFAULT_RESERVE_RATIO = 0.10
_DEFAULT_RESERVE_MIN_MIB = 2048


@dataclasses.dataclass(frozen=True)
class AdmissionSettings:
    """容量チェックの設定値。

    Attributes:
        memory_reserve_mib: ホスト用に残すメモリ(MiB)。None なら既定の式
            (ホストの 10% と 2 GiB の大きいほう)で決める。
        memory_overcommit: メモリのオーバーコミット率。1.0 なら約束の合計を
            (ホスト - 予約) までに抑える。
    """

    memory_reserve_mib: int | None = None
    memory_overcommit: float = 1.0


@dataclasses.dataclass(frozen=True)
class HostCapacity:
    """ホストの容量。

    Attributes:
        memory_mib: ホストの物理メモリ(MiB)。
        cpus: ホストの論理 CPU 数。
    """

    memory_mib: int
    cpus: int


@dataclasses.dataclass(frozen=True)
class Allocation:
    """管理対象 VM 1台分の割当(spec の値)。

    Attributes:
        name: VM 名。
        memory_mib: memory(MiB)。
        vcpus: vCPU 数。
        disk_gib: disk(GiB)。
    """

    name: str
    memory_mib: int
    vcpus: int
    disk_gib: int


@dataclasses.dataclass(frozen=True)
class Decision:
    """容量チェックの結果。

    Attributes:
        reasons: 拒否の理由。空なら受け入れる。
        warnings: 受け入れるが運用者に知らせたいこと(プールの空き不足など)。
    """

    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """拒否の理由が1つも無ければ True。"""
        return not self.reasons


def load_settings(env: dict | None = None) -> AdmissionSettings:
    """環境変数から AdmissionSettings を組み立てる。

    Args:
        env: 環境変数の dict。None なら os.environ。

    Returns:
        AdmissionSettings。未設定の項目は既定値。

    Raises:
        ValueError: 値が数値でない、または範囲外(予約が負、率が 0 以下)の場合。
    """
    env = os.environ if env is None else env
    reserve = None
    if env.get(RESERVE_ENV_VAR):
        reserve = int(env[RESERVE_ENV_VAR])
        if reserve < 0:
            raise ValueError(f"{RESERVE_ENV_VAR} must be >= 0: {reserve}")
    overcommit = 1.0
    if env.get(OVERCOMMIT_ENV_VAR):
        overcommit = float(env[OVERCOMMIT_ENV_VAR])
        if not overcommit > 0:
            raise ValueError(f"{OVERCOMMIT_ENV_VAR} must be > 0: {overcommit}")
    return AdmissionSettings(memory_reserve_mib=reserve, memory_overcommit=overcommit)


def memory_reserve_mib(host_memory_mib: int, settings: AdmissionSettings) -> int:
    """ホスト用に残すメモリ(MiB)を返す。"""
    if settings.memory_reserve_mib is not None:
        return settings.memory_reserve_mib
    return max(
        math.ceil(host_memory_mib * _DEFAULT_RESERVE_RATIO), _DEFAULT_RESERVE_MIN_MIB
    )


def memory_limit_mib(host_memory_mib: int, settings: AdmissionSettings) -> int:
    """VM に約束してよいメモリの合計の上限(MiB)を返す。

    式は `floor((ホスト - 予約) × オーバーコミット率)`。予約がホストを上回るときは 0。
    """
    usable = max(host_memory_mib - memory_reserve_mib(host_memory_mib, settings), 0)
    return math.floor(usable * settings.memory_overcommit)


def evaluate(
    host: HostCapacity,
    allocations: list[Allocation],
    spec: dict,
    settings: AdmissionSettings,
    *,
    base_image_bytes: int | None = None,
    pool_available_bytes: int | None = None,
) -> Decision:
    """新しい spec を受け入れられるかを判定する(純粋関数)。

    - メモリ: 管理対象 VM の memory の合計(稼働状態に関わらず全 VM。autostart で
      同時に起きうるため)+ 新 spec の memory が上限(`memory_limit_mib`)を超えたら拒否。
    - vCPU: 新 spec の vcpus がホストの論理 CPU 数を超えたら拒否(合計の
      オーバーコミットは許す)。
    - base image: 仮想サイズが spec の disk を超えたら拒否(overlay が base より
      小さいとゲストのファイルシステムが壊れる)。
    - overlay 用プール: 空きが disk の合計(既存 + 新規)を下回れば警告のみ。

    allocations に spec と同じ name の VM が含まれていれば(収束による拡張の場合)、
    二重計上しないよう除いてから数える。

    Args:
        host: ホストの容量。
        allocations: 管理対象 VM の割当。
        spec: 新しい spec(name/memory/vcpus/disk を参照する)。
        settings: 容量チェックの設定値。
        base_image_bytes: base image の仮想サイズ(バイト)。None なら検査しない。
        pool_available_bytes: overlay 用プールの空き(バイト)。None なら検査しない。

    Returns:
        Decision。
    """
    others = [a for a in allocations if a.name != spec["name"]]
    reasons = []
    warnings = []

    others_memory = sum(a.memory_mib for a in others)
    used = others_memory + spec["memory"]
    limit = memory_limit_mib(host.memory_mib, settings)
    if used > limit:
        reserve = memory_reserve_mib(host.memory_mib, settings)
        reasons.append(
            f"メモリが足りない: 割当の合計 {used} MiB(既存 {others_memory} + "
            f"この VM {spec['memory']})が上限 {limit} MiB を超える"
            f"(上限 = (ホスト {host.memory_mib} - 予約 {reserve}) × "
            f"オーバーコミット率 {settings.memory_overcommit:g})"
        )

    if spec["vcpus"] > host.cpus:
        reasons.append(
            f"vCPU が多すぎる: {spec['vcpus']} はホストの論理 CPU 数 "
            f"{host.cpus} を超える"
        )

    if base_image_bytes is not None and spec["disk"] * GIB < base_image_bytes:
        reasons.append(
            f"disk が小さすぎる: {spec['disk']} GiB は base image の仮想サイズ "
            f"{base_image_bytes / GIB:.1f} GiB を下回る"
        )

    if pool_available_bytes is not None:
        total_disk_gib = sum(a.disk_gib for a in others) + spec["disk"]
        if pool_available_bytes < total_disk_gib * GIB:
            warnings.append(
                f"overlay 用プールの空き {pool_available_bytes / GIB:.1f} GiB が "
                f"disk の合計 {total_disk_gib} GiB を下回る"
                "(thin provisioning のため作成は続ける)"
            )

    return Decision(reasons=tuple(reasons), warnings=tuple(warnings))


def needs_recheck(old_spec: dict, new_spec: dict) -> bool:
    """収束で memory か vcpus を増やすか(=容量チェックが要るか)を返す。"""
    return new_spec["memory"] > old_spec.get("memory", 0) or new_spec[
        "vcpus"
    ] > old_spec.get("vcpus", 0)


def allocations_from_specs(specs: dict[str, dict]) -> list[Allocation]:
    """管理対象 VM の spec(name → spec)から割当の一覧を作る。"""
    return [
        Allocation(
            name=name,
            memory_mib=int(spec.get("memory", 0)),
            vcpus=int(spec.get("vcpus", 0)),
            disk_gib=int(spec.get("disk", 0)),
        )
        for name, spec in specs.items()
    ]


def host_capacity(conn) -> HostCapacity:
    """ホストの容量を libvirt から取得する。

    `conn.getInfo()` は [model, memory(MiB), cpus, mhz, nodes, sockets, cores,
    threads] を返す。
    """
    info = conn.getInfo()
    return HostCapacity(memory_mib=int(info[1]), cpus=int(info[2]))


def _is_missing(e: libvirt.libvirtError) -> bool:
    """プールや volume が存在しないことを表す libvirtError か判定する。"""
    return e.get_error_code() in (
        libvirt.VIR_ERR_NO_STORAGE_POOL,
        libvirt.VIR_ERR_NO_STORAGE_VOL,
    )


def base_image_bytes(conn, base_image: str) -> int | None:
    """`images` プールにある base image の仮想サイズ(バイト)を返す。

    プールや volume が無ければ None(容量の問題ではないため、ここでは拒否せず
    provision の失敗に任せる)。
    """
    try:
        pool = conn.storagePoolLookupByName(BASE_POOL)
        # 後からコピーした base image を見落とさないよう、create_overlay_volume と
        # 同じく refresh してから引く。
        pool.refresh(0)
        return int(pool.storageVolLookupByName(base_image).info()[1])
    except libvirt.libvirtError as e:
        if _is_missing(e):
            return None
        raise


def pool_available_bytes(conn, pool_name: str = POOL_NAME) -> int | None:
    """ストレージプールの空き(バイト)を返す。プールが無ければ None。

    `pool.info()` は [state, capacity, allocation, available] を返す。
    """
    try:
        return int(conn.storagePoolLookupByName(pool_name).info()[3])
    except libvirt.libvirtError as e:
        if _is_missing(e):
            return None
        raise


def check_capacity(
    conn,
    spec: dict,
    managed_specs: Callable[[], dict[str, dict]],
    *,
    include_disk: bool = True,
    env: dict | None = None,
) -> Decision:
    """容量を検査し、足りなければ InsufficientCapacity を送出する。

    libvirt から値を集めて evaluate() に渡す。

    ServerManager.create() が name 単位ロックの内側で呼ぶ。警告はログ(WARNING)に
    出すだけで作成は続ける。ログに出すのは name と数値だけ(spec 本文は出さない)。

    Args:
        conn: libvirt 接続。
        spec: 新しい spec。
        managed_specs: 管理対象 VM の name → spec を返す呼び出し可能オブジェクト
            (ServerManager.managed_specs)。
        include_disk: base image とプールの空きも検査するか。収束(memory/vcpus の
            拡張)では disk が変わらないため False にする。
        env: 環境変数の dict(テスト用)。None なら os.environ。

    Returns:
        受け入れた場合の Decision(警告を含みうる)。

    Raises:
        InsufficientCapacity: 容量が足りない場合。
    """
    name = spec["name"]
    decision = evaluate(
        host_capacity(conn),
        allocations_from_specs(managed_specs()),
        spec,
        load_settings(env),
        base_image_bytes=base_image_bytes(conn, spec["base_image"])
        if include_disk
        else None,
        pool_available_bytes=pool_available_bytes(conn) if include_disk else None,
    )
    for warning in decision.warnings:
        _LOGGER.warning("%s: %s", name, warning)
    if not decision.ok:
        _LOGGER.info("%s: 容量不足で拒否", name)
        raise InsufficientCapacity(f"{name}: " + "; ".join(decision.reasons))
    _LOGGER.debug("%s: 容量チェックを通過", name)
    return decision
