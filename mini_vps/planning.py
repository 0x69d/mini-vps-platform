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
from .resources import needs_nwfilter


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
# filters / egress は VM 専用 nwfilter のルールの変更で、libvirt は同名の nwfilter を
# 再定義すると稼働中の VM のインターフェースにも反映するため LIVE。ただし filter の
# 有無が変わる(filterref の付け外しが要る)変更は plan_change が
# FILTER_ATTACH_APPLY_MODE で上書きする(field_apply_mode 参照)。
FIELD_APPLY_MODES: dict[str, ApplyMode] = {
    "memory": ApplyMode.OFFLINE,
    "vcpus": ApplyMode.OFFLINE,
    "filters": ApplyMode.LIVE,
    "egress": ApplyMode.LIVE,
    "autostart": ApplyMode.LIVE,
}

# nwfilter の内容を決めるフィールド。
FILTER_FIELDS = frozenset({"filters", "egress"})

# filter の有無が変わる変更(interface の filterref の付け外し)の反映方式。
# libvirt の QEMU ドライバは updateDeviceFlags(AFFECT_LIVE) で渡された interface の
# filterref が変わっていれば、旧ルールを外して新ルールを当てる
# (qemuDomainChangeNet → qemuDomainChangeNetFilter。type='network' / 'bridge' /
# 'ethernet' の interface が対象)。manager はこれで稼働中の VM にも付け外しを
# 反映する。実環境で問題が出た場合はここを OFFLINE にすれば、稼働中の付け外しは
# ServerRunning で拒否され、停止中の差分編集だけが使われる。
FILTER_ATTACH_APPLY_MODE = ApplyMode.LIVE


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


def filter_attachment_changes(old_spec: dict, new_spec: dict) -> bool:
    """VM 専用 nwfilter の要否(interface に filterref を付けるか)が変わるかを返す。

    例: filters も egress も None の VM に egress を足す、filters だけの VM から
    filters を外す(egress が None なら filter ごと不要になる)。
    """
    return needs_nwfilter(old_spec) != needs_nwfilter(new_spec)


def field_apply_mode(field: str, old_spec: dict, new_spec: dict) -> ApplyMode:
    """既存と新しい spec の文脈で、フィールドの変更の反映方式を返す。

    FIELD_APPLY_MODES は静的な表なので、filters / egress のように「何から何へ
    変わるか」で方式が変わるフィールドはここで判定する。filter の有無が変わらない
    ルールの変更は LIVE(nwfilter の再定義だけで済む)、変わるなら
    FILTER_ATTACH_APPLY_MODE。
    """
    if field in FILTER_FIELDS and filter_attachment_changes(old_spec, new_spec):
        return FILTER_ATTACH_APPLY_MODE
    return apply_mode(field)


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

    modes = {k: field_apply_mode(k, old_spec, new_spec) for k in keys}
    recreate = frozenset(k for k, m in modes.items() if m is ApplyMode.RECREATE)
    offline = frozenset(k for k, m in modes.items() if m is ApplyMode.OFFLINE)
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
    if not profile.supports_nwfilter:
        for field in ("filters", "egress"):
            if spec.get(field) is not None:
                raise PlatformUnsupported(
                    f"{spec['name']}: このホスト({profile.os})は nwfilter が無いため "
                    f"{field} を使えません"
                )
