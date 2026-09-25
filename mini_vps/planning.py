"""spec の差分を分類する純粋関数群。

既存 VM の spec と新しい spec を比べ、「何もしない・その場で反映する・停止中なら
反映する・再作成が要る」のどれに当たるかを決める。libvirt には触れないため、
素の dict だけでテストできる。`ServerManager.create()` と `plan`/`apply` の両方が
この判定を共有する。
"""

import dataclasses
import enum

from .errors import PlatformUnsupported
from .platform_profile import NETWORK_USER, HostProfile


class ApplyMode(enum.StrEnum):
    """フィールドの変更をどう反映できるか。"""

    LIVE = "live"
    """稼働中でも停止中でも、停止させずに反映できる。"""
    OFFLINE = "offline"
    """停止中の VM にだけ反映できる(稼働中なら ServerRunning)。"""
    RECREATE = "recreate"
    """反映には VM の再作成が要る(ServerConflict)。"""


# フィールドごとの反映方式。ここに無いフィールドは RECREATE として扱う。
# networks はインターフェース XML の書き換えだけなら技術的には可能だが、
# 実運用への影響が大きいため RECREATE のままにする。static_routes と
# startup_script は cloud-init 由来(seed ISO 生成時にのみ反映)であり、
# domain XML の差分編集では反映できないため RECREATE。
FIELD_APPLY_MODES: dict[str, ApplyMode] = {
    "memory": ApplyMode.OFFLINE,
    "vcpus": ApplyMode.OFFLINE,
    "filters": ApplyMode.OFFLINE,
    "autostart": ApplyMode.LIVE,
}


class Action(enum.StrEnum):
    """plan_change() が決める操作。"""

    CREATE = "create"
    NOOP = "noop"
    CONVERGE = "converge"
    CONFLICT = "conflict"
    BLOCKED_RUNNING = "blocked_running"


@dataclasses.dataclass(frozen=True)
class Change:
    """1 VM 分の変更計画。

    Attributes:
        action: 実行する操作。
        diff_keys: 既存と新しい spec で値が異なるフィールド名(新規作成なら空)。
        recreate_keys: diff_keys のうち再作成が必要なもの。
        offline_keys: diff_keys のうち停止中にしか反映できないもの。
    """

    action: Action
    diff_keys: frozenset[str] = frozenset()
    recreate_keys: frozenset[str] = frozenset()
    offline_keys: frozenset[str] = frozenset()


def apply_mode(field: str) -> ApplyMode:
    """フィールド名の反映方式を返す(表に無ければ RECREATE)。"""
    return FIELD_APPLY_MODES.get(field, ApplyMode.RECREATE)


def diff_keys(old_spec: dict, new_spec: dict) -> frozenset[str]:
    """新しい spec のフィールドのうち、既存 spec と値が異なるものの名前を返す。

    new_spec に無いキーは比較しない。呼び出し側は両方を ServerSpec で正規化して
    から渡す前提で、正規化済みなら両者のキー集合は一致する。
    """
    return frozenset(k for k, v in new_spec.items() if old_spec.get(k) != v)


def plan_change(old_spec: dict | None, new_spec: dict, running: bool) -> Change:
    """既存 VM の spec と新しい spec から変更計画を立てる。

    Args:
        old_spec: 既存 VM の spec(欠落フィールドを既定値で補完済みのもの)。
            VM が無ければ None。
        new_spec: 適用したい spec。
        running: 既存 VM が稼働中か(old_spec が None なら無視される)。

    Returns:
        Change。CONFLICT は再作成が要る差分があること、BLOCKED_RUNNING は
        停止中にしか反映できない差分があるのに稼働中であることを表す。
    """
    if old_spec is None:
        return Change(Action.CREATE)

    keys = diff_keys(old_spec, new_spec)
    if not keys:
        return Change(Action.NOOP)

    recreate = frozenset(k for k in keys if apply_mode(k) is ApplyMode.RECREATE)
    offline = frozenset(k for k in keys if apply_mode(k) is ApplyMode.OFFLINE)
    if recreate:
        action = Action.CONFLICT
    elif offline and running:
        action = Action.BLOCKED_RUNNING
    else:
        action = Action.CONVERGE
    return Change(action, keys, recreate, offline)


def check_platform(spec: dict, profile: HostProfile) -> None:
    """VM の spec がこのホストのプラットフォームで実現できるかを確かめる。

    実現できない機能を黙って無視すると、利用者は守られているつもりで VM を使う
    ことになる(例: macOS で filters を指定しても何も遮断されない)。そのため
    作成・収束の前に拒否する。

    Raises:
        PlatformUnsupported: 実現できない機能が指定されている場合。
    """
    if profile.network_mode == NETWORK_USER:
        if spec["networks"] != ["default"]:
            raise PlatformUnsupported(
                f"{spec['name']}: このホスト({profile.os})は user-mode ネットワーク"
                "のみ対応のため、networks は [default] だけを指定できます"
                "(複数 NIC・セグメント・静的 IP は Linux ホストが必要です)"
            )
    if not profile.supports_nwfilter and spec.get("filters") is not None:
        raise PlatformUnsupported(
            f"{spec['name']}: このホスト({profile.os})は nwfilter が無いため "
            "filters を使えません"
        )
