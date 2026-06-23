"""VM 管理層。name を主キーとして操作する。

libvirt domain の <metadata> 要素に spec を埋め込むことで、自前 DB を持たずに
get / list を成立させる。各メソッドは lifecycle の実行部品の薄いラッパー。
"""

import xml.etree.ElementTree as ET

import libvirt
import yaml

from .config import METADATA_KEY, METADATA_NS
from .lifecycle import _lease_ipv4, provision, teardown

_STATE_NAMES = {
    libvirt.VIR_DOMAIN_NOSTATE: "nostate",
    libvirt.VIR_DOMAIN_RUNNING: "running",
    libvirt.VIR_DOMAIN_BLOCKED: "blocked",
    libvirt.VIR_DOMAIN_PAUSED: "paused",
    libvirt.VIR_DOMAIN_SHUTDOWN: "shutdown",
    libvirt.VIR_DOMAIN_SHUTOFF: "shutoff",
    libvirt.VIR_DOMAIN_CRASHED: "crashed",
    libvirt.VIR_DOMAIN_PMSUSPENDED: "pmsuspended",
}


def _write_spec(dom, spec: dict) -> None:
    """VM スペックを YAML 化し、dom の <metadata> に書き込む。

    ElementTree でテキストノードを組むことで、spec 値の & < > が自動エスケープされる。
    flags に LIVE|CONFIG を明示するのは、CURRENT(0) では起動中 dom に書いても
    再起動で spec が消えるため。

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
        libvirt.VIR_DOMAIN_AFFECT_LIVE | libvirt.VIR_DOMAIN_AFFECT_CONFIG,
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


def _status_of(dom) -> dict:
    """VM の状態と IP のスナップショットを返す(IP は待たない)。"""
    state = dom.state()[0]
    # 起動中のときだけリースを引く。それ以外は IP 未確定とみなす
    ip = _lease_ipv4(dom) if state == libvirt.VIR_DOMAIN_RUNNING else None
    return {"state": _STATE_NAMES.get(state, "unknown"), "ip": ip}


class ServerManager:
    """VM の作成・取得・一覧・削除を行う管理層。

    Attributes:
        conn: libvirt 接続オブジェクト。
    """

    def __init__(self, conn):
        self.conn = conn

    def create(self, spec: dict) -> dict:
        """VM を作成し、spec を metadata に書き込んでから状態を返す。

        Args:
            spec: VM スペックの dict。

        Returns:
            spec と status をキーに持つ dict。
        """
        dom = provision(self.conn, spec)
        _write_spec(dom, spec)
        return self.get(spec["name"])

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
        out = []
        for dom in self.conn.listAllDomains():
            try:
                dom.metadata(libvirt.VIR_DOMAIN_METADATA_ELEMENT, METADATA_NS, 0)
            except libvirt.libvirtError as e:
                # metadata 未保有 = 管理対象外。それ以外のエラーは握りつぶさない
                if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN_METADATA:
                    continue
                raise
            out.append(dom.name())
        return out

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
        """VM を後始末する。

        Args:
            name: VM 名。
        """
        teardown(self.conn, {"name": name})
