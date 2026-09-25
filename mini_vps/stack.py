"""スタック(複数 VM の spec の組)の読み込み・plan・apply。

エージェントの住処は DNS・ルータ・エージェント VM 複数のように1台で完結しないことが
多い。スタックファイルに複数の spec をまとめ、差分の表示(plan)と一括適用(apply)を
行う。

自前 DB を持たない原則はそのまま守る。「どの VM がどのスタックに属するか」は各 VM の
spec の `stack` フィールド(libvirt metadata)にだけ載せ、`--prune` はそのラベルで
削除対象を探す。`depends_on` は適用順(トポロジカル順)と起動待ちの対象を決める。

構成は次の3段で、下の段ほど libvirt から遠い。

- `load_stack` / `resolve_stack`: スタックファイルの検証(純粋関数)。
- `compute_plan`: 既存 VM の観測値と突き合わせて変更計画を立てる(純粋関数)。
- `plan_stack` / `apply_stack`: `ServerManager` から観測値を集め、計画・適用する。

VM 単位の差分の分類は `planning.plan_change` を、VM 単位の作成・収束・削除は
`ServerManager` の既存メソッドをそのまま使う。
"""

import dataclasses
import logging
import time
from collections.abc import Callable, Iterable, Mapping

import yaml
from pydantic import BaseModel, Field

from . import errors
from .errors import ServerNotFound, StackError
from .planning import Action, plan_change
from .platform_profile import NETWORK_USER, get_profile
from .spec import _NAME_PATTERN, ServerSpec

_LOGGER = logging.getLogger(__name__)

DELETE = "delete"
"""prune で削除する VM の action(planning.Action に無い、スタック固有の操作)。"""

# apply を止める action。どちらも VM を壊すか止めないと反映できない。
_BLOCKING_ACTIONS = frozenset({Action.CONFLICT.value, Action.BLOCKED_RUNNING.value})

# 稼働していない(qemu プロセスが無い)とみなす状態。ServerManager.create() が
# 使う dom.isActive() と同じ区分にそろえる。
_INACTIVE_STATES = frozenset({"shutoff", "crashed"})

DEFAULT_WAIT_TIMEOUT = 300.0
"""apply --wait で依存先1台の起動を待つ秒数の既定値。"""

_WAIT_INTERVAL = 2.0


class StackDefinition(BaseModel):
    """スタックファイルの形(YAML と API の JSON body で共通)。

    各 server は ServerSpec そのもので、VM 単位の検証は spec.py に任せる。
    スタックをまたぐ検証(name の重複・stack の不一致・depends_on の循環)は
    resolve_stack() が行う。
    """

    stack: str = Field(pattern=_NAME_PATTERN)
    servers: list[ServerSpec] = Field(min_length=1)


@dataclasses.dataclass(frozen=True)
class Stack:
    """検証済みのスタック。

    Attributes:
        name: スタック名。
        servers: name → spec(stack 補完済みの ServerSpec.model_dump())。
            挿入順はトポロジカル順(依存される側が先)。
    """

    name: str
    servers: dict[str, dict]


def _topological_order(
    names: list[str], deps: Mapping[str, Iterable[str]]
) -> list[str] | None:
    """依存される側が先に来る順に names を並べる(循環があれば None)。

    names の外を指す依存は無視する。依存関係が決めない順序は names の順を保つ
    (ファイルに書いた順が結果の既定の順になる)。
    """
    member = set(names)
    pending = {n: {d for d in deps.get(n, ()) if d in member} for n in names}
    order: list[str] = []
    while pending:
        ready = next((n for n in names if n in pending and not pending[n]), None)
        if ready is None:
            return None
        order.append(ready)
        del pending[ready]
        for rest in pending.values():
            rest.discard(ready)
    return order


def _find_cycle(names: list[str], deps: Mapping[str, Iterable[str]]) -> list[str]:
    """循環している依存の経路を1つ返す(例: [a, b, a])。

    _topological_order() が None を返した後にだけ呼ぶ。残った各ノードは残った
    ノードへの依存を必ず1つ以上持つため、依存を辿れば必ずどこかで一周する。
    """
    member = set(names)
    remaining = set(names)
    # トポロジカルに並べられるノードを取り除き、循環に関わるものだけを残す。
    changed = True
    while changed:
        changed = False
        for n in list(remaining):
            if not any(d in remaining for d in deps.get(n, ()) if d in member):
                remaining.discard(n)
                changed = True
    start = next(n for n in names if n in remaining)
    path = [start]
    while True:
        nxt = next(d for d in deps[path[-1]] if d in remaining)
        if nxt in path:
            return path[path.index(nxt) :] + [nxt]
        path.append(nxt)


def resolve_stack(definition: StackDefinition) -> Stack:
    """StackDefinition のスタックをまたぐ検証をして Stack を返す。

    各 server の stack を補完し、name の重複と depends_on の循環を拒否する。
    スタック外を指す depends_on は既存の管理 VM を指している可能性があるため、
    ここでは拒否しない(compute_plan が既存 VM と突き合わせて検証する)。

    Raises:
        StackError: server が別の stack を名乗っている、name が重複している、
            または depends_on が循環している場合。
    """
    stack_name = definition.stack
    specs: dict[str, dict] = {}
    for server in definition.servers:
        spec = server.model_dump()
        if spec["stack"] is not None and spec["stack"] != stack_name:
            raise StackError(
                f"{spec['name']}: stack {spec['stack']!r} はスタックファイルの "
                f"stack {stack_name!r} と異なります"
            )
        if spec["name"] in specs:
            raise StackError(f"server の name が重複しています: {spec['name']}")
        spec["stack"] = stack_name
        specs[spec["name"]] = spec

    names = list(specs)
    deps = {n: specs[n]["depends_on"] for n in names}
    order = _topological_order(names, deps)
    if order is None:
        cycle = " -> ".join(_find_cycle(names, deps))
        raise StackError(f"depends_on が循環しています: {cycle}")
    return Stack(stack_name, {n: specs[n] for n in order})


def load_stack(text: str) -> Stack:
    """スタックファイル(YAML テキスト)を読み込み、検証済みの Stack を返す。

    Raises:
        pydantic.ValidationError: ファイルの形や個々の server の spec が不正な場合。
        StackError: スタックをまたぐ検証(resolve_stack 参照)に失敗した場合。
    """
    return resolve_stack(StackDefinition.model_validate(yaml.safe_load(text)))


@dataclasses.dataclass(frozen=True)
class PlanItem:
    """1 VM 分の計画。

    Attributes:
        name: VM の name。
        action: planning.Action の値、または prune による削除("delete")。
        fields: 既存 VM と値が異なるフィールド。
        recreate_fields: fields のうち再作成が要るもの(action が conflict の理由)。
        offline_fields: fields のうち停止中にしか反映できないもの。
        reason: 補足(別スタックの VM を奪おうとしている、など)。
    """

    name: str
    action: str
    fields: tuple[str, ...] = ()
    recreate_fields: tuple[str, ...] = ()
    offline_fields: tuple[str, ...] = ()
    reason: str | None = None

    def to_dict(self) -> dict:
        """CLI/API の出力用に dict 化する(空の補足は省く)。"""
        result: dict = {"name": self.name, "action": self.action}
        if self.fields:
            result["fields"] = list(self.fields)
        if self.recreate_fields:
            result["recreate_fields"] = list(self.recreate_fields)
        if self.offline_fields:
            result["offline_fields"] = list(self.offline_fields)
        if self.reason:
            result["reason"] = self.reason
        return result

    def describe(self) -> str:
        """拒否理由(apply など)に使う1行の説明を返す。"""
        if self.reason:
            return f"{self.name} ({self.action}: {self.reason})"
        keys = self.recreate_fields or self.offline_fields or self.fields
        return f"{self.name} ({self.action}: {', '.join(keys)})"


@dataclasses.dataclass(frozen=True)
class StackPlan:
    """スタック全体の計画。

    Attributes:
        stack: スタック名。
        items: 作成・収束の計画(トポロジカル順)に、prune の削除(逆順)が続く。
    """

    stack: str
    items: tuple[PlanItem, ...]

    @property
    def blockers(self) -> list[PlanItem]:
        """適用を止める計画(conflict / blocked_running)を返す。"""
        return [i for i in self.items if i.action in _BLOCKING_ACTIONS]

    def to_dict(self) -> dict:
        """CLI/API の出力用に dict 化する。"""
        return {"stack": self.stack, "changes": [i.to_dict() for i in self.items]}


def _is_running(observed: dict) -> bool:
    """ServerManager.get() の結果から、VM が稼働中(isActive 相当)かを返す。"""
    return observed["status"]["state"] not in _INACTIVE_STATES


def compute_plan(
    stack: Stack,
    managed: Mapping[str, dict | None],
    prune: bool = False,
) -> StackPlan:
    """スタックと既存の管理 VM の観測値から変更計画を立てる(純粋関数)。

    Args:
        stack: 検証済みのスタック。
        managed: 管理 VM の name → ServerManager.get() の結果(spec は ServerSpec で
            正規化済み)。スタックの server と prune の判定に要る VM は値を持つこと。
            それ以外(depends_on の参照先として存在だけ確かめる VM)は None でよい。
        prune: 同じ stack ラベルを持つがスタックに無い管理 VM を削除する計画を足すか。

    Returns:
        StackPlan。

    Raises:
        StackError: depends_on がスタックにも既存の管理 VM にも無い name を指す場合、
            または prune で削除する VM を残る VM が depends_on で参照している場合。
    """
    for name, spec in stack.servers.items():
        unknown = [
            d for d in spec["depends_on"] if d not in stack.servers and d not in managed
        ]
        if unknown:
            raise StackError(
                f"{name}: depends_on の参照先がスタックにも既存の管理 VM にも"
                f"ありません: {unknown}"
            )

    items: list[PlanItem] = []
    for name, spec in stack.servers.items():
        observed = managed.get(name)
        if observed is None:
            items.append(PlanItem(name, Action.CREATE.value))
            continue
        old_spec = observed["spec"]
        if old_spec.get("stack") not in (None, stack.name):
            # 別スタックの VM を奪うと、両方のスタックの apply が stack ラベルを
            # 奪い合い、prune が互いの VM を消しうる。
            items.append(
                PlanItem(
                    name,
                    Action.CONFLICT.value,
                    fields=("stack",),
                    reason=f"別のスタック {old_spec['stack']!r} に属する VM です",
                )
            )
            continue
        change = plan_change(old_spec, spec, running=_is_running(observed))
        items.append(
            PlanItem(
                name,
                change.action.value,
                tuple(sorted(change.diff_keys)),
                tuple(sorted(change.recreate_keys)),
                tuple(sorted(change.offline_keys)),
            )
        )

    if prune:
        items.extend(_plan_deletes(stack, managed))
    return StackPlan(stack.name, tuple(items))


def _plan_deletes(stack: Stack, managed: Mapping[str, dict | None]) -> list[PlanItem]:
    """削除(prune)する VM の計画を、依存する側が先に来る順(逆トポロジカル順)で返す。

    Raises:
        StackError: 削除する VM を、残る VM が depends_on で参照している場合。
    """
    doomed = [
        name
        for name, observed in managed.items()
        if name not in stack.servers
        and observed is not None
        and observed["spec"].get("stack") == stack.name
    ]
    if not doomed:
        return []

    # 残る VM の depends_on は、スタックの server なら新しい spec、それ以外は
    # metadata の spec を見る(apply 後に残る依存関係で判定する)。
    survivors: dict[str, list[str]] = {
        name: spec["depends_on"] for name, spec in stack.servers.items()
    }
    for name, observed in managed.items():
        if name not in survivors and name not in doomed and observed is not None:
            survivors[name] = observed["spec"].get("depends_on", [])
    for name, deps in survivors.items():
        broken = [d for d in deps if d in doomed]
        if broken:
            raise StackError(
                f"prune で削除する VM {broken} を {name} が depends_on で参照しています"
            )

    deps = {n: managed[n]["spec"].get("depends_on", []) for n in doomed}
    order = _topological_order(doomed, deps) or doomed
    return [PlanItem(name, DELETE) for name in reversed(order)]


def _observe(mgr, name: str) -> dict | None:
    """ServerManager.get() の結果を spec 正規化して返す(不在なら None)。

    metadata の spec はフィールド追加前の版で書かれている可能性がある。
    ServerManager.create() と同じく ServerSpec を通して欠落フィールドを補完し、
    同じ spec の再適用が差分扱いにならないようにする。
    """
    try:
        observed = mgr.get(name)
    except ServerNotFound:
        return None
    spec = ServerSpec(**observed["spec"]).model_dump()
    return {"spec": spec, "status": observed["status"]}


def plan_stack(mgr, stack: Stack, prune: bool = False) -> StackPlan:
    """既存の管理 VM を ServerManager から読み、スタックの変更計画を立てる。

    読み取り(list/get)だけを行い、何も変更しない。prune しない場合はスタックの
    server だけを読み、prune する場合は stack ラベルと依存関係を調べるため全管理 VM
    を読む。

    Args:
        mgr: ServerManager。
        stack: 検証済みのスタック。
        prune: compute_plan 参照。

    Returns:
        StackPlan。
    """
    names = mgr.list()
    managed: dict[str, dict | None] = {}
    for name in names:
        if prune or name in stack.servers:
            observed = _observe(mgr, name)
            if observed is None:
                continue  # list() と get() の間に削除された
            managed[name] = observed
        else:
            managed[name] = None
    plan = compute_plan(stack, managed, prune=prune)
    _LOGGER.info(
        "stack %s: plan %s",
        stack.name,
        ", ".join(f"{i.name}={i.action}" for i in plan.items),
    )
    return plan


def wait_until_ready(
    mgr,
    name: str,
    timeout: float = DEFAULT_WAIT_TIMEOUT,
    *,
    interval: float = _WAIT_INTERVAL,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """VM が status で running かつ IP を持つまで待ち、その status を返す。

    lifecycle.wait_for_ip と同じくポーリングで待つ。clock と sleep を差し替えれば
    テストで実時間を待たずに済む。user-mode ネットワーク(macOS)ではゲストの IP を
    libvirt から得られないため running だけを条件にする。静的 IP の VM は status が
    宣言値を返すため、running になった時点で条件を満たす(ゲスト内のサービスが
    応答するかまでは確かめない)。

    Raises:
        StackError: 停止中(待っても起動しない)、またはタイムアウトした場合。
    """
    need_ip = get_profile().network_mode != NETWORK_USER
    deadline = clock() + timeout
    while True:
        status = mgr.status(name)
        state = status["state"]
        if state == "running" and (status["ip"] is not None or not need_ip):
            _LOGGER.info("%s: 起動を確認 ip=%s", name, status["ip"])
            return status
        if state in _INACTIVE_STATES:
            raise StackError(
                f"{name} が停止しているため起動を待てません (state={state})"
            )
        if clock() >= deadline:
            raise StackError(
                f"{name} が {timeout:g} 秒以内に起動しませんでした"
                f" (state={state}, ip={status['ip']})"
            )
        sleep(interval)


def _describe_error(exc: Exception) -> str:
    """適用途中の例外を、入口層と同じラベル付きの1行にする。"""
    mapping = errors.lookup(exc)
    if mapping is not None:
        return f"{mapping.label}: {exc}"
    return f"{type(exc).__name__}: {exc}"


def apply_stack(
    mgr,
    stack: Stack,
    *,
    prune: bool = False,
    wait: bool = False,
    secrets: Mapping[str, Mapping[str, str]] | None = None,
    wait_timeout: float = DEFAULT_WAIT_TIMEOUT,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """スタックの計画を立て、トポロジカル順に適用する。

    計画に conflict / blocked_running が1つでもあれば、何も変更せずに拒否する。
    それ以外は各 server に ServerManager.create() を呼び(noop の VM にも呼ぶ。
    plan から apply までの間に変わっていても create() がロック内で判定し直す)、
    prune の削除は最後に逆トポロジカル順で ServerManager.delete() を呼ぶ。

    wait=True なら、server を作る前にその depends_on の VM(スタック内・既存の
    どちらも)が running かつ IP を持つまで待つ(wait_until_ready 参照)。依存される
    VM の作成直後ではなく、最初に依存する VM を作る直前に待つため、互いに依存しない
    VM の起動は並行に進む。

    途中で失敗したら、そこで止めて適用済みと未適用の VM を StackError で報告する。
    適用済みの VM は巻き戻さない(同じスタックを再 apply すれば続きから収束する)。

    Args:
        mgr: ServerManager。
        stack: 検証済みのスタック。
        prune: plan_stack 参照。
        wait: 依存先の起動を待つか。
        secrets: server の name → startup_script に渡す秘密情報。create() にだけ
            渡し、ログにも戻り値にも載せない。
        wait_timeout: 依存先1台あたりの待ち時間(秒)。
        clock: 待ちに使う時計(テスト用)。
        sleep: 待ちに使う sleep(テスト用)。

    Returns:
        {"stack": スタック名, "changes": [計画 + 適用後の status, ...]}。

    Raises:
        StackError: 計画が apply できない、secrets がスタックに無い server を指す、
            または適用途中で失敗した場合。
    """
    secrets = secrets or {}
    unknown = sorted(set(secrets) - set(stack.servers))
    if unknown:
        raise StackError(f"secrets の宛先がスタックにありません: {unknown}")

    plan = plan_stack(mgr, stack, prune=prune)
    blockers = plan.blockers
    if blockers:
        raise StackError(
            "適用できない変更があるため何も変更していません: "
            + "; ".join(i.describe() for i in blockers)
        )

    changes: list[dict] = []
    ready: set[str] = set()
    for index, item in enumerate(plan.items):
        try:
            if item.action == DELETE:
                mgr.delete(item.name)
                _LOGGER.info("stack %s: %s を削除", stack.name, item.name)
                changes.append(item.to_dict())
                continue
            spec = stack.servers[item.name]
            if wait:
                for dep in spec["depends_on"]:
                    if dep not in ready:
                        wait_until_ready(
                            mgr, dep, wait_timeout, clock=clock, sleep=sleep
                        )
                        ready.add(dep)
            result, _created = mgr.create(
                spec, secrets=dict(secrets.get(item.name, {})) or None
            )
        except Exception as e:
            applied = [c["name"] for c in changes]
            pending = [i.name for i in plan.items[index:]]
            _LOGGER.error(
                "stack %s: %s の適用に失敗 applied=%s pending=%s",
                stack.name,
                item.name,
                applied,
                pending,
            )
            raise StackError(
                f"{item.name} の適用に失敗しました ({_describe_error(e)})。"
                f"適用済み: {applied}、未適用: {pending}",
                applied=applied,
                pending=pending,
            ) from e
        _LOGGER.info("stack %s: %s を %s", stack.name, item.name, item.action)
        changes.append({**item.to_dict(), "status": result["status"]})
    return {"stack": stack.name, "changes": changes}
