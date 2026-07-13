"""VM 管理層。name を主キーとして操作する。

libvirt domain の <metadata> 要素に spec を埋め込むことで、自前 DB を持たずに
get / list を成立させる。各メソッドは lifecycle の実行部品の薄いラッパー。
"""

import threading
import xml.etree.ElementTree as ET

import libvirt
import yaml

from .config import METADATA_KEY, METADATA_NS
from .lifecycle import (
    ensure_filters_enforceable,
    ensure_network_active,
    get_domain_ipv4,
    provision,
    teardown,
)
from .resources import (
    _filter_name,
    build_nwfilter_xml,
    build_seed_iso,
    create_overlay_volume,
    resize_domain_xml,
    set_domain_filterref_xml,
)
from .spec import read_pubkey

# create() が停止中の既存 VM に対して収束(defineXML の最小差分編集)を許す
# フィールド。それ以外のフィールドの差分は ServerConflict で拒否する。
# network はインターフェース XML の書き換えだけなら技術的には可能だが、
# 実運用への影響が大きいためスコープ外とし、明示的に別操作として扱う。
# 新しい可変フィールドを追加する場合はここに追記する。
_MUTABLE_FIELDS = frozenset({"memory", "vcpus", "filters"})

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


def register_quiet_error_handler() -> None:
    """既定の libvirt エラーハンドラを抑制する。

    VIR_ERR_NO_DOMAIN 等を正常系として Python 側で捕捉していても、libvirt は
    既定で全エラーを無条件に C 層から stderr へ出力する。`libvirt.open()` より前に
    一度呼び出すことでその出力を抑制する(Python 側の例外処理自体は変更しない)。
    """
    libvirt.registerErrorHandler(lambda ctx, err: None, None)


def _write_spec(dom, spec: dict) -> None:
    """VM スペックを YAML 化し、dom の <metadata> に書き込む。

    ElementTree でテキストノードを組むことで、spec 値の & < > が自動エスケープされる。
    flags は AFFECT_CONFIG のみ。起動前に書くため、起動時の live が CONFIG を引き継ぐ。
    """
    el = ET.Element("spec")
    el.text = yaml.safe_dump(spec)
    dom.setMetadata(
        libvirt.VIR_DOMAIN_METADATA_ELEMENT,
        ET.tostring(el, encoding="unicode"),
        METADATA_KEY,
        METADATA_NS,
        libvirt.VIR_DOMAIN_AFFECT_CONFIG,
    )


def _read_spec(dom) -> dict:
    """VM スペックを dom の <metadata> から読み戻す(未保有なら libvirtError)。"""
    raw = dom.metadata(libvirt.VIR_DOMAIN_METADATA_ELEMENT, METADATA_NS, 0)
    return yaml.safe_load(ET.fromstring(raw).text)


class ServerNotFound(Exception):
    """指定した name の管理対象 domain が存在しない、または管理対象外であることを表す。

    呼び出し側はこの 1 つの例外を捕捉すれば、libvirt のエラーコードを意識せずに
    「minivps が知らない name」を扱える(例: ルーターで 404 に変換する)。
    """


class ServerConflict(Exception):
    """create() の対象 name が既存実体と相違する(または管理対象外)ことを表す。

    収束ロジックを持たないため相違は fail-loud に拒否する。ServerNotFound と対称で、
    Web API では PUT /servers/{name} の 409 Conflict に対応づける。
    """


class ServerNotRunning(Exception):
    """restart(force=False) の対象 VM が停止中であることを表す。

    ACPI 経由の正常再起動は稼働中のゲスト OS にしか要求できない。libvirt の
    生の例外を伝播させる代わりにこの例外で fail-loud に拒否することで、
    ServerNotFound/ServerConflict と同様に CLI の終了コード・Web API の
    HTTP ステータスへ正規化できるようにする(起動も含めた強制再起動は
    force=True で行う)。
    """


class ServerRunning(Exception):
    """create() が可変フィールド差分を収束させる対象 VM が起動中であることを表す。

    稼働中の memory/vcpus/filters 変更はホットプラグ対応(スコープ外)が必要なため、
    ServerNotRunning と対称的に fail-loud に拒否する(先に stop してから
    再度 create/PUT する運用を促す)。
    """


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


def _status_of(dom) -> dict:
    """VM の状態と IP のスナップショットを返す(IP は待たない)。"""
    state = dom.state()[0]
    # 起動中のときだけ IP を引く。それ以外は IP 未確定とみなす
    ip = get_domain_ipv4(dom) if state == libvirt.VIR_DOMAIN_RUNNING else None
    return {"state": STATE_NAMES.get(state, "unknown"), "ip": ip}


class ServerManager:
    """VM の作成・取得・一覧・削除を行う管理層。

    書き込み系操作(create/delete/start/stop/restart/reinstall)は name 単位ロックで
    直列化し、同名への並行収束(check-then-act)の TOCTOU を防ぐ。
    別 name 同士は並行のまま。get / list / status はロックを取らない
    (libvirt 接続が個々の呼び出し単位でスレッドセーフなため)。create() はロック内で
    self.get() を呼ぶので、読み取り側にロックを足すと非再帰 Lock で自己デッドロック
    する点に注意。

    Attributes:
        conn: libvirt 接続オブジェクト。
    """

    def __init__(self, conn):
        self.conn = conn
        # name -> Lock(生成の直列化は _lock_for を参照)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, name: str) -> threading.Lock:
        """指定 name 専用の Lock を返す(無ければ生成する)。

        dict.setdefault は CPython では実質アトミックだが、意図を明示するため
        _locks_guard で囲んで新規 name の Lock 生成を確実に直列化する。
        """
        with self._locks_guard:
            return self._locks.setdefault(name, threading.Lock())

    def create(
        self, spec: dict, secrets: dict[str, str] | None = None
    ) -> tuple[dict, bool]:
        """VM を宣言的に作成/収束し、(spec と状態, 新規作成か) を返す。

        既存と spec が完全一致すれば無変更で現状を返す(冪等 no-op)。相違がある場合、
        差分が _MUTABLE_FIELDS(memory/vcpus/filters)に収まっていれば停止中の domain
        に限り収束させる(稼働中は ServerRunning で拒否)。それ以外のフィールドの
        差分、または管理対象外の同名 domain は破壊せず ServerConflict で拒否する。
        新規作成時は metadata を起動前に付け、失敗時は teardown で巻き戻して
        all-or-nothing にする。

        name 単位ロックで全体を直列化するため、ロック取得後に _find_domain を再評価する
        (ロック前の判定は信用しない)。同名 overlay volume の delete→再作成
        (resources.create_overlay_volume)も provision 経由でこのロック内に入るため
        直列化される。

        secrets(startup_script テンプレートに渡す秘密情報)は provision() にのみ
        渡し、_write_spec()(=libvirt metadata)には渡さない。

        Returns:
            (result, created) のタプル。result は spec と status をキーに持つ dict。
            created は新規作成なら True、既存一致の冪等 no-op・収束なら False。

        Raises:
            ServerConflict: 不変フィールドの差分、または管理対象外の同名 domain の場合。
            ServerRunning: 可変フィールドの差分があり、対象 VM が起動中の場合。
            FiltersUnsupported: filters 指定があり network が OVS 接続の場合。
        """
        name = spec["name"]
        with self._lock_for(name):
            existing = _find_domain(self.conn, name)
            if existing is None:
                try:
                    dom = provision(self.conn, spec, secrets=secrets)
                    _write_spec(dom, spec)
                    dom.create()
                except Exception:
                    teardown(self.conn, {"name": name})
                    raise
                return self.get(name), True

            if not _is_managed(existing):
                raise ServerConflict(name)

            # 新規経路は provision 内で検証済み。既存経路は冪等 no-op を含めて
            # ここで検証し、filters が素通しの VM の存在を fail-loud に可視化する。
            ensure_filters_enforceable(self.conn, spec)

            old_spec = _read_spec(existing)
            if old_spec == spec:
                return self.get(name), False

            diff_keys = {k for k, v in spec.items() if old_spec.get(k) != v}
            if diff_keys - _MUTABLE_FIELDS:
                raise ServerConflict(name)
            if existing.isActive():
                raise ServerRunning(name)

            dom = self._converge(existing, old_spec, spec, diff_keys)
            # _write_spec が失敗しても domain 実体側はロールバックしない。_converge の
            # 各操作(resize/filterref 設定/nwfilter 定義・削除)は全遷移パターンで冪等
            # なため、同じ spec で create() を再実行すれば自己修復する。
            _write_spec(dom, spec)
            return self.get(name), False

    def _converge(self, dom, old_spec: dict, new_spec: dict, diff_keys: set) -> object:
        """可変フィールド(memory/vcpus/filters)の差分を、停止中の domain に適用する。

        dom.XMLDesc(INACTIVE) を最小差分編集して defineXML する(build_domain_xml に
        よるテンプレート再構築ではなく既存定義への差分編集にすることで、MAC アドレス・
        UUID の意図しない再生成を避ける)。nwfilter は使用中(domain の filterref から
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

        return dom

    def get(self, name: str) -> dict:
        """指定した VM の spec と状態を返す。

        Returns:
            spec と status をキーに持つ dict。

        Raises:
            ServerNotFound: 指定した name が存在しない、または管理対象外の場合。
        """
        dom = _lookup(self.conn, name)
        return {"spec": _read_spec(dom), "status": _status_of(dom)}

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
        return _status_of(_lookup(self.conn, name))

    def delete(self, name: str) -> None:
        """管理対象の VM を削除する。

        未管理(または不在)の name は削除せず ServerNotFound で拒否する。

        Raises:
            ServerNotFound: 指定した name が存在しない、または管理対象外の場合。
        """
        # create と同じ name ロックで直列化し、作成途中の VM への delete 競合を防ぐ
        with self._lock_for(name):
            _lookup(self.conn, name)
            teardown(self.conn, {"name": name})

    def start(self, name: str) -> dict:
        """管理対象の VM を起動する。

        既に起動中なら何もせず現状を返す(冪等)。create()/reinstall() と同じく、
        dom.create() の前に spec が参照する network を確実に起動する(ホスト再起動後
        などで network だけ非アクティブなまま domain が残るケースに備える)。

        Returns:
            spec と status をキーに持つ dict。

        Raises:
            ServerNotFound: 指定した name が存在しない、または管理対象外の場合。
            FiltersUnsupported: filters 指定があり network が OVS 接続の場合。
        """
        with self._lock_for(name):
            dom = _lookup(self.conn, name)
            if not dom.isActive():
                spec = _read_spec(dom)
                ensure_filters_enforceable(self.conn, spec)
                ensure_network_active(self.conn, spec)
                dom.create()
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
        with self._lock_for(name):
            dom = _lookup(self.conn, name)
            if dom.isActive():
                dom.destroy() if force else dom.shutdown()
            return self.get(name)

    def restart(self, name: str, force: bool = False) -> dict:
        """管理対象の VM を再起動する。

        reinstall と異なり disk・spec・IP は変更しない。force=False(既定)は
        dom.reboot() でゲスト OS へ ACPI 経由の正常再起動を要求するのみで、
        停止中の VM には ServerNotRunning を送出し fail-loud に拒否する(電源が
        入っていない機器を ACPI 経由で再起動できないのと同じ)。force=True は
        起動中なら destroy() してから create() する強制再起動(停止中の VM は
        create() のみで起動する)。start()と同じく create() の前に network を
        確実に起動する。

        Returns:
            spec と status をキーに持つ dict。

        Raises:
            ServerNotFound: 指定した name が存在しない、または管理対象外の場合。
            ServerNotRunning: force=False で対象 VM が停止中の場合。
            FiltersUnsupported: force=True で filters 指定があり network が
                OVS 接続の場合(destroy 前に検証し、起動し直せない電源断を防ぐ)。
        """
        with self._lock_for(name):
            dom = _lookup(self.conn, name)
            if force:
                spec = _read_spec(dom)
                ensure_filters_enforceable(self.conn, spec)
                if dom.isActive():
                    dom.destroy()
                ensure_network_active(self.conn, spec)
                dom.create()
            else:
                if not dom.isActive():
                    raise ServerNotRunning(name)
                dom.reboot()
            return self.get(name)

    def reinstall(self, name: str, secrets: dict[str, str] | None = None) -> dict:
        """管理対象の VM の disk を base から作り直し、同じ spec で再起動する。

        domain 定義(MAC アドレス含む)は変更しないため IP は維持される。失敗時も
        対象 VM は削除せず、例外をそのまま呼び出し側に伝播させる。

        spec["startup_script"] の秘密情報は metadata に永続化されないため、
        テンプレートを再度効かせたい場合は呼び出しのたびに secrets を
        渡し直す必要がある。

        Returns:
            spec と status をキーに持つ dict。

        Raises:
            ServerNotFound: 指定した name が存在しない、または管理対象外の場合。
            FiltersUnsupported: filters 指定があり network が OVS 接続の場合
                (seed 再作成・overlay 破棄より前に検証する)。
        """
        with self._lock_for(name):
            dom = _lookup(self.conn, name)
            spec = _read_spec(dom)
            ensure_filters_enforceable(self.conn, spec)

            # overlay 再作成(破壊的)より前に seed を作り直す
            build_seed_iso(self.conn, spec, read_pubkey(), secrets=secrets)

            if dom.isActive():
                dom.destroy()
            create_overlay_volume(self.conn, spec)

            ensure_network_active(self.conn, spec)
            dom.create()

            return self.get(name)
