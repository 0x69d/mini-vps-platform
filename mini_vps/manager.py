"""VM 管理層。name を主キーとして操作する。

libvirt domain の <metadata> 要素に spec を埋め込むことで、自前 DB を持たずに
get / list を成立させる。各メソッドは lifecycle の実行部品の薄いラッパー。
"""

import contextlib
import logging
import xml.etree.ElementTree as ET

import libvirt
import yaml

from . import dns_registration, guest_agent
from .config import METADATA_KEY, METADATA_NS
from .errors import (  # noqa: F401  (manager から import する既存コード向けの再エクスポート)
    GuestAgentUnavailable,
    PlatformUnsupported,
    ServerConflict,
    ServerNotFound,
    ServerNotRunning,
    ServerRunning,
)
from .lifecycle import _lease_ipv4, ensure_network_active, provision, teardown
from .locks import NameLocks
from .planning import Action, check_platform, plan_change
from .platform_profile import get_profile
from .resources import (
    _filter_name,
    _mac_for_interface,
    build_nwfilter_xml,
    build_seed_iso,
    create_overlay_volume,
    resize_domain_xml,
    set_domain_filterref_xml,
    ssh_forward_port,
)
from .spec import ServerSpec, read_pubkey, ssh_identity_path

_LOGGER = logging.getLogger(__name__)

STATE_NAMES = {
    libvirt.VIR_DOMAIN_NOSTATE: "nostate",
    libvirt.VIR_DOMAIN_RUNNING: "running",
    libvirt.VIR_DOMAIN_BLOCKED: "blocked",
    libvirt.VIR_DOMAIN_PAUSED: "paused",
    libvirt.VIR_DOMAIN_SHUTDOWN: "shutdown",
    libvirt.VIR_DOMAIN_SHUTOFF: "shutoff",
    libvirt.VIR_DOMAIN_CRASHED: "crashed",
    libvirt.VIR_DOMAIN_PMSUSPENDED: "pmsuspended",
}


def _log_libvirt_error(ctx, err) -> None:
    """C 層から上がってきた libvirt エラーを DEBUG ログへ落とす。

    err は文字列ではなく (code, domain, message, level, str1, str2, str3,
    int1, int2) の9要素リストで、err[2] がメッセージ本文。リストのまま渡すと
    9フィールドがそのまま出力されるため message だけを取り出す。

    Args:
        ctx: registerErrorHandler に渡した任意のコンテキスト。使わない。
        err: libvirt のエラー情報リスト。
    """
    _LOGGER.debug("libvirt: %s", err[2])


def register_quiet_error_handler() -> None:
    """既定の libvirt エラーハンドラを DEBUG ログ出力に差し替える。

    VIR_ERR_NO_DOMAIN 等を正常系として Python 側で捕捉していても、libvirt は
    既定で全エラーを無条件に C 層から stderr へ出力する。`libvirt.open()` より前に
    一度呼び出すことでその出力を止める。Python 側の例外処理自体は変更しない。

    捨てるのではなく DEBUG へ落とすのは、_lookup / _is_managed が正常系として
    握りつぶすエラーに紛れて、本当に知りたい一次情報まで失われるのを避けるため。
    既定レベルでは表示されず、-vv で復元できる。list() は管理対象外 domain 1台
    ごとに1行出すため、DEBUG では相応の量になる。
    """
    libvirt.registerErrorHandler(_log_libvirt_error, None)


def _write_spec(dom, spec: dict) -> None:
    """VM スペックを YAML 化し、dom の <metadata> に書き込む。

    ElementTree でテキストノードを組むことで、spec 値の & < > が自動エスケープされる。
    新規作成時は起動前に書くため AFFECT_CONFIG だけで足り、起動時の live が CONFIG を
    引き継ぐ。稼働中の domain(autostart・stack など稼働中に反映できる差分の収束)では
    AFFECT_LIVE も付ける。_read_spec(flags=0 = AFFECT_CURRENT)は稼働中なら live 側を
    読むため、CONFIG だけを書き換えると次の停止まで古い spec が読み戻される。
    """
    el = ET.Element("spec")
    el.text = yaml.safe_dump(spec)
    flags = libvirt.VIR_DOMAIN_AFFECT_CONFIG
    if dom.isActive():
        flags |= libvirt.VIR_DOMAIN_AFFECT_LIVE
    dom.setMetadata(
        libvirt.VIR_DOMAIN_METADATA_ELEMENT,
        ET.tostring(el, encoding="unicode"),
        METADATA_KEY,
        METADATA_NS,
        flags,
    )


def _read_spec(dom) -> dict:
    """VM スペックを dom の <metadata> から読み戻す(未保有なら libvirtError)。"""
    raw = dom.metadata(libvirt.VIR_DOMAIN_METADATA_ELEMENT, METADATA_NS, 0)
    return yaml.safe_load(ET.fromstring(raw).text)


def _lookup(conn, name: str):
    """指定した name の管理対象 domain を返す。

    存在しない、または minivps の管理対象外(metadata 未保有)の場合は
    ServerNotFound に正規化する。これにより呼び出し側は libvirt の
    エラーコードを直接ハンドルせずに済む。
    """
    try:
        dom = conn.lookupByName(name)
    except libvirt.libvirtError as e:
        if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
            raise ServerNotFound(name) from e
        raise
    try:
        # list() の管理対象フィルタと同じ VIR_ERR_NO_DOMAIN_METADATA を基準にする。
        dom.metadata(libvirt.VIR_DOMAIN_METADATA_ELEMENT, METADATA_NS, 0)
    except libvirt.libvirtError as e:
        if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN_METADATA:
            raise ServerNotFound(name) from e
        raise
    return dom


def _find_domain(conn, name: str):
    """素の domain を返す。存在しなければ None。

    metadata で絞らず存在のみを見るため、管理対象外の同名 domain も検知でき、
    create() が既存リソースを巻き込んで破壊する事故を防げる。
    """
    try:
        return conn.lookupByName(name)
    except libvirt.libvirtError as e:
        if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
            return None
        raise


def _is_managed(dom) -> bool:
    """指定した domain が minivps の管理対象(spec metadata を保有)か判定する。"""
    try:
        dom.metadata(libvirt.VIR_DOMAIN_METADATA_ELEMENT, METADATA_NS, 0)
    except libvirt.libvirtError as e:
        if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN_METADATA:
            return False
        raise
    return True


def _static_ipv4(spec: dict) -> str | None:
    """spec["networks"]に静的アドレスを持つNICがあれば最初の1件のIPv4を返す。

    複数NICに静的アドレスがあっても最初の1件のみ。DHCPリース表示の
    「最初に見つかった1件のみ」という既存方針に合わせる。
    """
    for net in spec.get("networks", []):
        if isinstance(net, dict) and net.get("address"):
            return net["address"].split("/", 1)[0]
    return None


def _status_of(dom, spec: dict) -> dict:
    """VM の状態と IP のスナップショットを返す(IP は待たない)。

    spec に静的アドレスを持つ NIC が1つでもあれば、起動状態に関わらず spec 由来の
    アドレスを優先表示する。cloud-init が実際に適用したかは確認せず、宣言値を
    そのまま返す。無ければ従来通り起動中のときだけ DHCP リースを引き、リースも
    無ければ guest agent が報告するアドレスを使う(agent が使えなければ None)。
    """
    state = dom.state()[0]
    ip = _static_ipv4(spec)
    if ip is None and state == libvirt.VIR_DOMAIN_RUNNING:
        ip = _lease_ipv4(dom) or _agent_ipv4_or_none(dom, spec)
    return {"state": STATE_NAMES.get(state, "unknown"), "ip": ip}


def _agent_ipv4_or_none(dom, spec: dict) -> str | None:
    """Guest agent から IPv4 を引く。使えなければ None(例外は握りつぶす)。

    DHCP リースが無い VM(user-mode ネットワークなど)の IP 表示のためのフォール
    バック。agent が未導入・起動直後でも status/get を失敗させないよう、agent に
    起因する例外と libvirt のエラーはすべて None として扱う。VM の NIC の MAC を
    渡し、ゲスト内のブリッジ(docker0 など)のアドレスより優先させる。
    """
    name = dom.name()
    nic_count = len(spec.get("networks") or ["default"])
    macs = {_mac_for_interface(name, i) for i in range(nic_count)}
    try:
        return guest_agent.agent_ipv4(dom, macs)
    except (GuestAgentUnavailable, ServerNotRunning, libvirt.libvirtError) as e:
        _LOGGER.debug("%s: guest agent から IP を取得できない: %s", name, e)
        return None


def _require_running(dom, name: str) -> None:
    """Domain が稼働中(一時停止していない)でなければ ServerNotRunning を送出する。"""
    state = dom.state()[0]
    if state == libvirt.VIR_DOMAIN_PAUSED:
        raise ServerNotRunning(f"{name} (一時停止中。resume してください)")
    if state != libvirt.VIR_DOMAIN_RUNNING:
        raise ServerNotRunning(name)


class ServerManager:
    """VM の作成・取得・一覧・削除を行う管理層。

    書き込み系操作(create/delete/start/stop/restart/pause/resume/reinstall)は
    name 単位ロックで
    直列化し、同名への並行収束(check-then-act)の TOCTOU を防ぐ。ロックは
    プロセス間(fcntl.flock)でも効くため、CLI と API が別プロセスでも直列化される
    (locks.py 参照)。
    別 name 同士は並行のまま。get / list / status は libvirt 接続が個々の
    呼び出し単位でスレッドセーフなためロックを取らない。create() はロック内で
    self.get() を呼ぶので、読み取り側にロックを足すと非再帰 Lock で自己デッドロック
    する点に注意。

    Attributes:
        conn: libvirt 接続オブジェクト。
    """

    def __init__(self, conn, lock_dir: str | None = None):
        """ServerManager を作る。

        Args:
            conn: libvirt 接続。
            lock_dir: name 単位のプロセス間ロックを置くディレクトリ。None なら
                HostProfile.lock_dir を使う。
        """
        self.conn = conn
        self._locks = NameLocks(lock_dir or get_profile().lock_dir)

    @contextlib.contextmanager
    def _locked(self, name: str):
        """指定 name のロック(プロセス内 + プロセス間)を取得して保持する。

        Yields:
            None。
        """
        with self._locks.hold(name):
            yield

    def create(
        self, spec: dict, secrets: dict[str, str] | None = None
    ) -> tuple[dict, bool]:
        """VM を宣言的に作成/収束し、(spec と状態, 新規作成か) を返す。

        既存と spec が完全一致すれば無変更で現状を返す(冪等 no-op)。相違がある場合は
        planning.plan_change でフィールドごとの反映方式を判定する。稼働中に反映できる
        差分(autostart)はその場で、停止中にしか反映できない差分(memory/vcpus/
        filters)は停止中の domain に限り収束させる(稼働中は ServerRunning)。
        再作成が必要な差分、または管理対象外の同名 domain は破壊せず
        ServerConflict で拒否する。このホストで実現できない機能(macOS での filters
        など)はロックを取る前に PlatformUnsupported で拒否する。
        新規作成時は metadata を起動前に付け、失敗時は teardown で巻き戻して
        all-or-nothing にする。

        name 単位ロックで全体を直列化するため、ロック取得後に _find_domain を再評価する
        (ロック前の判定は信用しない)。同名 overlay volume の delete→再作成
        (resources.create_overlay_volume)も provision 経由でこのロック内に入るため
        直列化される。

        secrets(startup_script テンプレートに渡す秘密情報)は provision() にのみ
        渡し、_write_spec()(=libvirt metadata)には渡さない。

        新規作成成功後は DNS レコードをベストエフォートで登録する。opt-in であり、
        失敗しても create は成功する。docs/dns-registration.md 参照。

        Returns:
            (result, created) のタプル。result は spec と status をキーに持つ dict。
            created は新規作成なら True、既存一致の冪等 no-op・収束なら False。

        Raises:
            ServerConflict: 不変フィールドの差分、または管理対象外の同名 domain の場合。
            ServerRunning: 停止中にしか反映できない差分があり、対象 VM が起動中の場合。
            PlatformUnsupported: このホストで実現できない機能が指定された場合。
        """
        name = spec["name"]
        check_platform(spec, get_profile())
        with self._locked(name):
            existing = _find_domain(self.conn, name)
            if existing is None:
                _LOGGER.info("%s: 新規作成を開始", name)
                try:
                    dom = provision(self.conn, spec, secrets=secrets)
                    _write_spec(dom, spec)
                    dom.create()
                except Exception:
                    _LOGGER.error("%s: 新規作成に失敗、巻き戻す", name)
                    teardown(self.conn, {"name": name})
                    raise
                _LOGGER.info("%s: 新規作成が完了", name)
                # DNS 登録は VM 作成成功が確定した後(try/except の外)で行う。
                # register は例外を送出しない契約(ベストエフォート)だが、
                # 仮に失敗しても teardown 巻き戻しを誘発しない位置に置く。
                dns_registration.register(spec)
                return self.get(name), True

            if not _is_managed(existing):
                raise ServerConflict(name)

            # metadata の spec はフィールド追加前の版で書かれている可能性がある
            # (例: nameservers 追加前に作った VM)。Pydantic を通して欠落フィールドに
            # デフォルトを補完し、同じ YAML の再 create が差分扱いにならないようにする。
            old_spec = ServerSpec(**_read_spec(existing)).model_dump()
            change = plan_change(old_spec, spec, running=bool(existing.isActive()))
            if change.action is Action.NOOP:
                _LOGGER.info("%s: 既存と一致、変更なし", name)
                return self.get(name), False
            if change.action is Action.CONFLICT:
                raise ServerConflict(
                    f"{name} (再作成が必要なフィールド: {sorted(change.recreate_keys)})"
                )
            if change.action is Action.BLOCKED_RUNNING:
                raise ServerRunning(
                    f"{name} (停止中にしか反映できないフィールド: "
                    f"{sorted(change.offline_keys)})"
                )

            _LOGGER.info("%s: 差分を収束 fields=%s", name, sorted(change.diff_keys))
            dom = self._converge(existing, old_spec, spec, set(change.diff_keys))
            # _write_spec が失敗しても domain 実体側はロールバックしない。_converge の
            # 各操作(resize/filterref 設定/nwfilter 定義・削除/autostart)は全遷移
            # パターンで冪等なため、同じ spec で create() を再実行すれば自己修復する。
            _write_spec(dom, spec)
            return self.get(name), False

    def _converge(self, dom, old_spec: dict, new_spec: dict, diff_keys: set) -> object:
        """可変フィールドの差分を domain に適用する。

        反映方式は planning.FIELD_APPLY_MODES が決める。autostart は稼働中でも
        setAutostart で反映する。memory/vcpus/filters は停止中の domain にだけ適用する
        (稼働中なら呼び出し前に ServerRunning で拒否済み)。

        dom.XMLDesc(INACTIVE) を最小差分編集して defineXML する。build_domain_xml に
        よるテンプレート再構築ではなく既存定義への差分編集にすることで、MAC アドレス・
        UUID の意図しない再生成を避ける。nwfilter は使用中(domain の filterref から
        参照されている間)は undefine できないため(teardown() 参照)、フィルタ解除時は
        defineXML で filterref を外した後に undefine する。フィルタ新設時は逆に
        nwfilterDefineXML で先に定義してから defineXML で filterref を付ける
        (provision() と同じ順序)。undefine 前には teardown() と同じく存在確認する
        (_write_spec 失敗後に create() が再実行された場合、前回既に undefine 済みの
        filter に対して呼ばれる可能性があるため)。

        Returns:
            defineXML 後の domain(filters/memory/vcpus のいずれの差分も無ければ
            引数の dom をそのまま返す)。
        """
        if "autostart" in diff_keys:
            dom.setAutostart(1 if new_spec.get("autostart", True) else 0)
        if not diff_keys & {"memory", "vcpus", "filters"}:
            return dom

        xml = dom.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)

        if diff_keys & {"memory", "vcpus"}:
            xml = resize_domain_xml(xml, new_spec["memory"] * 1024, new_spec["vcpus"])

        filter_name = None
        should_undefine = False
        if "filters" in diff_keys:
            filter_name = _filter_name(new_spec)
            has_filter = new_spec.get("filters") is not None
            should_undefine = old_spec.get("filters") is not None and not has_filter
            if has_filter:
                self.conn.nwfilterDefineXML(build_nwfilter_xml(new_spec))
            xml = set_domain_filterref_xml(xml, filter_name if has_filter else None)

        dom = self.conn.defineXML(xml)

        if should_undefine and filter_name in {
            f.name() for f in self.conn.listAllNWFilters()
        }:
            self.conn.nwfilterLookupByName(filter_name).undefine()
            _LOGGER.debug("nwfilter %s を削除", filter_name)

        return dom

    def get(self, name: str) -> dict:
        """指定した VM の spec と状態を返す。

        Returns:
            spec と status をキーに持つ dict。

        Raises:
            ServerNotFound: 指定した name が存在しない、または管理対象外の場合。
        """
        dom = _lookup(self.conn, name)
        spec = _read_spec(dom)
        return {"spec": spec, "status": _status_of(dom, spec)}

    def list(self) -> list[str]:
        """管理対象の VM 名の一覧を返す。

        Returns:
            minivps 名前空間の metadata を持つ domain 名のリスト。
        """
        return [dom.name() for dom in self.conn.listAllDomains() if _is_managed(dom)]

    def is_managed(self, dom) -> bool:
        """指定した domain が管理対象かを判定する。

        `getAllDomainStats()` のようにすでに domain オブジェクトを持っている
        呼び出し元が、`list()` と同じ判定基準で1件ずつ絞り込むために使う。
        """
        return _is_managed(dom)

    def status(self, name: str) -> dict:
        """指定した VM の現在の状態を返す。

        Returns:
            state と ip をキーに持つ dict。

        Raises:
            ServerNotFound: 指定した name が存在しない、または管理対象外の場合。
        """
        dom = _lookup(self.conn, name)
        return _status_of(dom, _read_spec(dom))

    def delete(self, name: str) -> None:
        """管理対象の VM を削除する。

        未管理(または不在)の name は削除せず ServerNotFound で拒否する。
        削除成功後は DNS レコードをベストエフォートで削除する。opt-in であり、
        失敗しても delete は成功する。docs/dns-registration.md 参照。

        Raises:
            ServerNotFound: 指定した name が存在しない、または管理対象外の場合。
        """
        # create と同じ name ロックで直列化し、作成途中の VM への delete 競合を防ぐ
        with self._locked(name):
            dom = _lookup(self.conn, name)
            _LOGGER.info("%s: 削除を開始", name)
            # DNS レコードの削除に使う IP は teardown で metadata ごと消える前に
            # 読んでおく。unregister は teardown 成功後にのみ呼ぶ。teardown が
            # 失敗した=VM が残っているのに名前だけ消える事故を防ぐため。
            spec = _read_spec(dom)
            teardown(self.conn, {"name": name})
            dns_registration.unregister(spec)
            _LOGGER.info("%s: 削除が完了", name)

    def start(self, name: str) -> dict:
        """管理対象の VM を起動する。

        既に起動中なら何もせず現状を返す(冪等)。create()/reinstall() と同じく、
        dom.create() の前に spec が参照する network を確実に起動する。ホスト再起動後
        などで network だけ非アクティブなまま domain が残るケースに備えるため。

        Returns:
            spec と status をキーに持つ dict。

        Raises:
            ServerNotFound: 指定した name が存在しない、または管理対象外の場合。
        """
        with self._locked(name):
            dom = _lookup(self.conn, name)
            if not dom.isActive():
                ensure_network_active(self.conn, _read_spec(dom))
                dom.create()
                _LOGGER.info("%s: 起動した", name)
            else:
                _LOGGER.info("%s: 既に起動中、変更なし", name)
            return self.get(name)

    def stop(self, name: str, force: bool = False) -> dict:
        """管理対象の VM を停止する。

        既に停止中なら何もせず現状を返す(冪等)。force=False(既定)は
        dom.shutdown() でゲスト OS へ ACPI 経由の正常シャットダウンを要求するのみで、
        実際に shutoff になるまで待たない(呼び出し側が status をポーリングして
        確認する想定)。force=True は dom.destroy() で即座に電源を落とす
        (応答しないゲストを落とす手段)。

        Returns:
            spec と status をキーに持つ dict。

        Raises:
            ServerNotFound: 指定した name が存在しない、または管理対象外の場合。
        """
        with self._locked(name):
            dom = _lookup(self.conn, name)
            if dom.isActive():
                dom.destroy() if force else dom.shutdown()
                _LOGGER.info("%s: 停止を要求 force=%s", name, force)
            else:
                _LOGGER.info("%s: 既に停止中、変更なし", name)
            return self.get(name)

    def restart(self, name: str, force: bool = False) -> dict:
        """管理対象の VM を再起動する。

        reinstall と異なり disk・spec・IP は変更しない。force=False(既定)は
        dom.reboot() でゲスト OS へ ACPI 経由の正常再起動を要求するのみで、
        停止中の VM には ServerNotRunning を送出し fail-loud に拒否する。電源が
        入っていない機器を ACPI 経由で再起動できないのと同じ。force=True は
        起動中なら destroy() してから create() する強制再起動(停止中の VM は
        create() のみで起動する)。start()と同じく create() の前に network を
        確実に起動する。

        Returns:
            spec と status をキーに持つ dict。

        Raises:
            ServerNotFound: 指定した name が存在しない、または管理対象外の場合。
            ServerNotRunning: force=False で対象 VM が停止中の場合。
        """
        with self._locked(name):
            dom = _lookup(self.conn, name)
            if force:
                if dom.isActive():
                    dom.destroy()
                ensure_network_active(self.conn, _read_spec(dom))
                dom.create()
            else:
                if not dom.isActive():
                    raise ServerNotRunning(name)
                dom.reboot()
            _LOGGER.info("%s: 再起動を要求 force=%s", name, force)
            return self.get(name)

    def exec(
        self,
        name: str,
        argv: list[str],
        stdin: bytes | str | None = None,
        timeout: float = 60,
    ) -> dict:
        """稼働中の VM の中で、qemu-guest-agent 経由でコマンドを実行する。

        ゲスト内 root 権限でのコマンド実行と同等で、SSH 鍵もネットワークも要らない
        (docs/guest-agent.md 参照)。終了まで待って結果を返し、timeout を超えたら
        timed_out=True を返す(プロセスはゲストで走り続ける。
        guest_agent.exec_command 参照)。

        ライフサイクルの書き込みではないため name ロックは取らない。取ると、長い
        コマンドの実行中に同じ VM への stop/pause が待たされ、暴走したコマンドを
        止める手段まで塞いでしまうため。実行中に VM が止まれば、次のポーリングで
        ServerNotRunning になる。

        ログには name・argv[0]・終了コードだけを出す。引数・stdin・出力は
        secrets を含みうるため出さない。

        Args:
            name: VM 名。
            argv: 実行するコマンドと引数(シェルを介さない)。
            stdin: 標準入力へ渡すデータ。str は UTF-8 で符号化する。
            timeout: 終了を待つ秒数。

        Returns:
            pid・exit_code・signal・stdout・stderr・truncated・timed_out を持つ dict。
            stdout/stderr は UTF-8 として復号した文字列(不正なバイトは置換文字)。

        Raises:
            ServerNotFound: 指定した name が存在しない、または管理対象外の場合。
            ServerNotRunning: 対象 VM が停止中・一時停止中の場合。
            GuestAgentUnavailable: guest agent を使えない場合。
            GuestExecError: argv・stdin が不正、またはゲストでコマンドを
                開始できない場合。
        """
        dom = _lookup(self.conn, name)
        _require_running(dom, name)
        if isinstance(stdin, str):
            stdin = stdin.encode()
        result = guest_agent.exec_command(dom, argv, stdin=stdin, timeout=timeout)
        if result.timed_out:
            _LOGGER.warning(
                "%s: exec がタイムアウト command=%s pid=%d", name, argv[0], result.pid
            )
        else:
            _LOGGER.info(
                "%s: exec が終了 command=%s exit_code=%s signal=%s",
                name,
                argv[0],
                result.exit_code,
                result.signal,
            )
        return {
            "pid": result.pid,
            "exit_code": result.exit_code,
            "signal": result.signal,
            "stdout": result.stdout.decode(errors="replace"),
            "stderr": result.stderr.decode(errors="replace"),
            "truncated": result.truncated,
            "timed_out": result.timed_out,
        }

    def pause(self, name: str) -> dict:
        """稼働中の VM を一時停止(vCPU を凍結)する。

        暴走したエージェントをその場で止め、メモリ・ディスクの状態を保ったまま調べる
        ための操作。既に一時停止中なら何もせず現状を返す(冪等)。メモリは解放されない。

        Returns:
            spec と status をキーに持つ dict。

        Raises:
            ServerNotFound: 指定した name が存在しない、または管理対象外の場合。
            ServerNotRunning: 対象 VM が停止中の場合。
        """
        with self._locked(name):
            dom = _lookup(self.conn, name)
            state = dom.state()[0]
            if state == libvirt.VIR_DOMAIN_PAUSED:
                _LOGGER.info("%s: 既に一時停止中、変更なし", name)
            elif dom.isActive():
                dom.suspend()
                _LOGGER.info("%s: 一時停止した", name)
            else:
                raise ServerNotRunning(name)
            return self.get(name)

    def resume(self, name: str) -> dict:
        """一時停止中の VM を再開する。

        既に稼働中(一時停止していない)なら何もせず現状を返す(冪等)。

        Returns:
            spec と status をキーに持つ dict。

        Raises:
            ServerNotFound: 指定した name が存在しない、または管理対象外の場合。
            ServerNotRunning: 対象 VM が停止中の場合。
        """
        with self._locked(name):
            dom = _lookup(self.conn, name)
            state = dom.state()[0]
            if state == libvirt.VIR_DOMAIN_PAUSED:
                dom.resume()
                _LOGGER.info("%s: 再開した", name)
            elif dom.isActive():
                _LOGGER.info("%s: 一時停止していない、変更なし", name)
            else:
                raise ServerNotRunning(name)
            return self.get(name)

    def ssh_endpoint(self, name: str) -> dict:
        """VM へ SSH 接続するための接続先を返す。

        user-mode ネットワーク(macOS)では domain XML のポート転送先
        (127.0.0.1:転送ポート)、libvirt ネットワークでは status と同じ方法で
        解決した IP の 22 番を返す。鍵は cloud-init が authorized_keys に入れた
        本ツール専用鍵(spec.ssh_identity_path)の秘密鍵。読み取りのみなので
        ロックは取らない。

        Returns:
            host・port・user・identity_file を持つ dict。

        Raises:
            ServerNotFound: 指定した name が存在しない、または管理対象外の場合。
            ServerNotRunning: 対象 VM が停止中・一時停止中の場合。
            GuestAgentUnavailable: 稼働中だが IP を解決できない場合(DHCP リースも
                guest agent の報告も無い。起動直後に多い)。
        """
        dom = _lookup(self.conn, name)
        _require_running(dom, name)
        spec = _read_spec(dom)
        port = ssh_forward_port(dom.XMLDesc(0))
        if port is not None:
            host = "127.0.0.1"
        else:
            host = _status_of(dom, spec)["ip"]
            port = 22
            if host is None:
                raise GuestAgentUnavailable(
                    f"{name}: IP アドレスを解決できません(DHCP リースも guest agent の"
                    "報告もありません。起動直後なら待って再試行してください)"
                )
        return {
            "host": host,
            "port": port,
            "user": ServerSpec(**spec).user,
            "identity_file": str(ssh_identity_path()),
        }

    def reinstall(self, name: str, secrets: dict[str, str] | None = None) -> dict:
        """管理対象の VM の disk を base から作り直し、同じ spec で再起動する。

        domain 定義(MAC アドレス含む)は変更しないため IP は維持される。失敗時も
        対象 VM は削除せず、例外をそのまま呼び出し側に伝播させる。

        spec["startup_script"] の秘密情報は metadata に永続化されないため、
        テンプレートを再度効かせたい場合は呼び出しのたびに secrets を
        渡し直す必要がある。

        起動成功後は DNS レコードをベストエフォートで再登録する。冪等な
        delete→add の組なので無害で、DNS 有効化前に作った VM のレコードを
        後追い補充する復旧手段を兼ねる。docs/dns-registration.md 参照。

        Returns:
            spec と status をキーに持つ dict。

        Raises:
            ServerNotFound: 指定した name が存在しない、または管理対象外の場合。
        """
        with self._locked(name):
            dom = _lookup(self.conn, name)
            spec = _read_spec(dom)
            _LOGGER.info("%s: 再インストールを開始", name)

            # overlay 再作成(破壊的)より前に seed を作り直す
            build_seed_iso(self.conn, spec, read_pubkey(), secrets=secrets)

            if dom.isActive():
                dom.destroy()
            create_overlay_volume(self.conn, spec)

            ensure_network_active(self.conn, spec)
            dom.create()

            # spec(name/IP)は不変なので既存レコードと同値の再登録になるが、
            # register は delete→add の冪等な組であり無害。DNS 有効化前に
            # 作った VM のレコードを後追い補充する復旧手段としても機能する。
            dns_registration.register(spec)
            _LOGGER.info("%s: 再インストールが完了", name)
            return self.get(name)
