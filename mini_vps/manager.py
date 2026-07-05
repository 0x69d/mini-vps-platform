"""VM 管理層。name を主キーとして操作する。

libvirt domain の <metadata> 要素に spec を埋め込むことで、自前 DB を持たずに
get / list を成立させる。各メソッドは lifecycle の実行部品の薄いラッパー。
"""

import threading
import xml.etree.ElementTree as ET

import libvirt
import yaml

from .config import METADATA_KEY, METADATA_NS
from .lifecycle import _lease_ipv4, ensure_network_active, provision, teardown
from .resources import build_seed_iso, create_overlay_volume
from .spec import read_pubkey

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

    Args:
        dom: 書き込み対象の libvirt.virDomain。
        spec: 書き込む VM スペックの dict。
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
    """VM スペックを dom の <metadata> から読み戻す。

    Args:
        dom: 読み取り対象の libvirt.virDomain。

    Returns:
        復元した VM スペックの dict。

    Raises:
        libvirt.libvirtError: metadata が存在しない場合。
    """
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
    将来の Web API では PUT /servers/{name} の 409 Conflict に対応づける想定。
    """


class ServerNotRunning(Exception):
    """restart(force=False) の対象 VM が停止中であることを表す。

    ACPI 経由の正常再起動は稼働中のゲスト OS にしか要求できない。libvirt の
    生の例外を伝播させる代わりにこの例外で fail-loud に拒否することで、
    ServerNotFound/ServerConflict と同様に CLI の終了コード・Web API の
    HTTP ステータスへ正規化できるようにする(起動も含めた強制再起動は
    force=True で行う)。
    """


def _lookup(conn, name: str):
    """指定した name の管理対象 domain を返す。

    存在しない、または minivps の管理対象外(metadata 未保有)の場合は
    ServerNotFound に正規化する。これにより get / status / 将来のルーターが
    libvirt のエラーコードを直接ハンドルせずに済む。

    Args:
        conn: libvirt 接続オブジェクト。
        name: VM 名。

    Returns:
        管理対象の libvirt.virDomain。

    Raises:
        ServerNotFound: domain が存在しない、または管理対象外の場合。
        libvirt.libvirtError: 上記以外の libvirt エラー。
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

    Args:
        conn: libvirt 接続オブジェクト。
        name: VM 名。

    Returns:
        domain が存在すれば libvirt.virDomain、無ければ None。

    Raises:
        libvirt.libvirtError: VIR_ERR_NO_DOMAIN 以外の libvirt エラー。
    """
    try:
        return conn.lookupByName(name)
    except libvirt.libvirtError as e:
        if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
            return None
        raise


def _is_managed(dom) -> bool:
    """指定した domain が minivps の管理対象(spec metadata を保有)か判定する。

    Args:
        dom: 判定対象の libvirt.virDomain。

    Returns:
        管理対象なら True。

    Raises:
        libvirt.libvirtError: VIR_ERR_NO_DOMAIN_METADATA 以外の libvirt エラー。
    """
    try:
        dom.metadata(libvirt.VIR_DOMAIN_METADATA_ELEMENT, METADATA_NS, 0)
    except libvirt.libvirtError as e:
        if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN_METADATA:
            return False
        raise
    return True


def _spec_matches(dom, spec: dict) -> bool:
    """保存済み spec と入力 spec が一致するか判定する。

    管理対象外(metadata 無し)は比較対象が無いため不一致扱い(=拒否)とする。

    Args:
        dom: 比較対象の libvirt.virDomain。
        spec: 入力 VM スペックの dict。

    Returns:
        spec が一致すれば True。

    Raises:
        libvirt.libvirtError: VIR_ERR_NO_DOMAIN_METADATA 以外の libvirt エラー。
    """
    try:
        return _read_spec(dom) == spec
    except libvirt.libvirtError as e:
        if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN_METADATA:
            return False
        raise


def _status_of(dom) -> dict:
    """VM の状態と IP のスナップショットを返す(IP は待たない)。"""
    state = dom.state()[0]
    # 起動中のときだけリースを引く。それ以外は IP 未確定とみなす
    ip = _lease_ipv4(dom) if state == libvirt.VIR_DOMAIN_RUNNING else None
    return {"state": STATE_NAMES.get(state, "unknown"), "ip": ip}


class ServerManager:
    """VM の作成・取得・一覧・削除を行う管理層。

    create / delete は name 単位ロックで直列化し、同名への並行収束(check-then-act)の
    TOCTOU を防ぐ。別 name 同士は並行のまま。get / list / status はロックを取らない
    (libvirt 接続が個々の呼び出し単位でスレッドセーフなため)。create() はロック内で
    self.get() を呼ぶので、読み取り側にロックを足すと非再帰 Lock で自己デッドロック
    する点に注意。

    Attributes:
        conn: libvirt 接続オブジェクト。
    """

    def __init__(self, conn):
        self.conn = conn
        # name -> Lock。新規 name の Lock 生成自体を _locks_guard で直列化する
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, name: str) -> threading.Lock:
        """指定 name 専用の Lock を返す(無ければ生成する)。

        dict.setdefault は CPython では実質アトミックだが、意図を明示するため
        _locks_guard で囲んで新規 name の Lock 生成を確実に直列化する。

        Args:
            name: VM 名。

        Returns:
            その name 専用の threading.Lock。
        """
        with self._locks_guard:
            return self._locks.setdefault(name, threading.Lock())

    def create(
        self, spec: dict, secrets: dict[str, str] | None = None
    ) -> tuple[dict, bool]:
        """VM を宣言的・冪等に作成し、(spec と状態, 新規作成か) を返す。

        既存と spec が一致すれば無変更で現状を返し、相違 or 管理対象外なら破壊せず
        ServerConflict で拒否する。新規は metadata を起動前に付け、失敗時は teardown で
        巻き戻して all-or-nothing にする。

        name 単位ロックで全体を直列化するため、ロック取得後に _find_domain を再評価する
        (ロック前の判定は信用しない)。同名 overlay volume の delete→再作成
        (resources.create_overlay_volume)も provision 経由でこのロック内に入るため
        直列化される。

        Args:
            spec: VM スペックの dict。
            secrets: spec["startup_script"] テンプレートに渡す秘密情報の dict。
                provision() にのみ渡し、_write_spec() には一切渡さない
                (libvirt の metadata に永続化させないため)。

        Returns:
            (result, created) のタプル。result は spec と status をキーに持つ dict。
            created は新規作成なら True、既存一致の冪等 no-op なら False。

        Raises:
            ServerConflict: 既存と spec が相違、または管理対象外の場合。
        """
        name = spec["name"]
        with self._lock_for(name):
            existing = _find_domain(self.conn, name)
            if existing is not None:
                # 一致なら冪等 no-op、相違 or 管理外なら破壊せず拒否
                if _spec_matches(existing, spec):
                    return self.get(name), False
                raise ServerConflict(name)

            # 新規は起動前に metadata を付け、途中失敗は teardown で巻き戻す
            try:
                dom = provision(self.conn, spec, secrets=secrets)
                _write_spec(dom, spec)
                dom.create()
            except Exception:
                teardown(self.conn, {"name": name})
                raise
            return self.get(name), True

    def get(self, name: str) -> dict:
        """指定した VM の spec と状態を返す。

        Args:
            name: VM 名。

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

        Args:
            dom: 判定対象の libvirt.virDomain。

        Returns:
            管理対象なら True。
        """
        return _is_managed(dom)

    def status(self, name: str) -> dict:
        """指定した VM の現在の状態を返す。

        Args:
            name: VM 名。

        Returns:
            state と ip をキーに持つ dict。

        Raises:
            ServerNotFound: 指定した name が存在しない、または管理対象外の場合。
        """
        return _status_of(_lookup(self.conn, name))

    def delete(self, name: str) -> None:
        """管理対象の VM を削除する。

        未管理(または不在)の name は削除せず ServerNotFound で拒否する。

        Args:
            name: VM 名。

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

        Args:
            name: VM 名。

        Returns:
            spec と status をキーに持つ dict。

        Raises:
            ServerNotFound: 指定した name が存在しない、または管理対象外の場合。
        """
        with self._lock_for(name):
            dom = _lookup(self.conn, name)
            if not dom.isActive():
                ensure_network_active(self.conn, _read_spec(dom))
                dom.create()
            return self.get(name)

    def stop(self, name: str, force: bool = False) -> dict:
        """管理対象の VM を停止する。

        既に停止中なら何もせず現状を返す(冪等)。force=False(既定)は
        dom.shutdown() でゲスト OS へ ACPI 経由の正常シャットダウンを要求するのみで、
        実際に shutoff になるまで待たない(呼び出し側が status をポーリングして
        確認する想定)。force=True は dom.destroy() で即座に電源を落とす
        (応答しないゲストを落とす手段)。

        Args:
            name: VM 名。
            force: True なら即座に強制停止する。

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

        Args:
            name: VM 名。
            force: True なら電源断→起動による強制再起動を行う。

        Returns:
            spec と status をキーに持つ dict。

        Raises:
            ServerNotFound: 指定した name が存在しない、または管理対象外の場合。
            ServerNotRunning: force=False で対象 VM が停止中の場合。
        """
        with self._lock_for(name):
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
            return self.get(name)

    def reinstall(self, name: str, secrets: dict[str, str] | None = None) -> dict:
        """管理対象の VM の disk を base から作り直し、同じ spec で再起動する。

        domain 定義(MAC アドレス含む)は変更しないため IP は維持される。失敗時も
        対象 VM は削除せず、例外をそのまま呼び出し側に伝播させる。

        spec["startup_script"] の秘密情報は metadata に永続化されないため、
        テンプレートを再度効かせたい場合は呼び出しのたびに secrets を
        渡し直す必要がある。

        Args:
            name: VM 名。
            secrets: spec["startup_script"] テンプレートに渡す秘密情報の dict。

        Returns:
            spec と status をキーに持つ dict。

        Raises:
            ServerNotFound: 指定した name が存在しない、または管理対象外の場合。
        """
        with self._lock_for(name):
            dom = _lookup(self.conn, name)
            spec = _read_spec(dom)

            # overlay 再作成(破壊的)より前に seed を作り直す
            build_seed_iso(spec, read_pubkey(), secrets=secrets)

            if dom.isActive():
                dom.destroy()
            create_overlay_volume(self.conn, spec)

            ensure_network_active(self.conn, spec)
            dom.create()

            return self.get(name)
