"""管理層の例外と、入口層での正規化先(HTTP ステータス・終了コード)の対応表。

CLI(終了コード)・Web API(HTTP ステータス)・MCP(エラーメッセージ)は、どれも
この `ERROR_TABLE` を引いて例外を正規化する。新しい例外を足すときはクラスを定義し、
表に1行足せば3入口すべてに反映される。

終了コード 1 は入力の誤り、2 は Typer の使用法エラー(Typer が予約)、3 以降は
VM やホストの状態に起因する拒否に割り当てる。
"""

import dataclasses


class MiniVpsError(Exception):
    """mini-vps の管理層が送出する例外の基底クラス。"""


class ServerNotFound(MiniVpsError):
    """指定した name の管理対象 domain が存在しない、または管理対象外であることを表す。

    呼び出し側はこの 1 つの例外を捕捉すれば、libvirt のエラーコードを意識せずに
    「minivps が知らない name」を扱える(例: ルーターで 404 に変換する)。
    """


class ServerConflict(MiniVpsError):
    """create() の対象 name が既存実体と相違する(または管理対象外)ことを表す。

    再作成が必要なフィールドの差分は fail-loud に拒否する。ServerNotFound と対称で、
    Web API では PUT /servers/{name} の 409 Conflict に対応づける。
    """


class ServerNotRunning(MiniVpsError):
    """稼働中の VM にしかできない操作を、停止中の VM に要求したことを表す。

    例: ACPI 経由の正常再起動、guest agent 経由のコマンド実行、一時停止。
    """


class ServerRunning(MiniVpsError):
    """停止中にしか反映できない変更を、稼働中の VM に要求したことを表す。

    先に stop してから再度 create/PUT する運用を促す。
    """


class PlatformUnsupported(MiniVpsError):
    """spec の機能がこのホストのプラットフォームでは使えないことを表す。

    例: macOS(user-mode ネットワーク)での nwfilter・複数 NIC・静的 IP。
    黙って無視すると、利用者は守られていると誤解したまま VM を使うことになるため、
    作成前に拒否する。
    """


class SnapshotNotFound(MiniVpsError):
    """指定した VM に、指定した名前のスナップショットが存在しないことを表す。

    VM 自体が無い場合は ServerNotFound を使い、こちらは VM が存在する前提で
    スナップショットだけが見つからない場合に限る。
    """


@dataclasses.dataclass(frozen=True)
class ErrorMapping:
    """例外1種類分の正規化先。

    Attributes:
        http_status: Web API が返す HTTP ステータス。
        exit_code: CLI の終了コード。
        label: エラーメッセージの接頭辞(例: "server not found")。
    """

    http_status: int
    exit_code: int
    label: str


# 並び順は捕捉の優先順位を兼ねる(サブクラスを先に置くこと)。
ERROR_TABLE: dict[type[Exception], ErrorMapping] = {
    ServerNotFound: ErrorMapping(404, 3, "server not found"),
    ServerConflict: ErrorMapping(409, 4, "server conflict"),
    ServerNotRunning: ErrorMapping(409, 5, "server not running"),
    ServerRunning: ErrorMapping(409, 6, "server running"),
    # 7 は libvirtError(ホスト側の障害 / 503)。libvirt は入口層でだけ扱う。
    PlatformUnsupported: ErrorMapping(422, 8, "platform unsupported"),
    SnapshotNotFound: ErrorMapping(404, 10, "snapshot not found"),
}


def lookup(exc: BaseException) -> ErrorMapping | None:
    """例外に対応する ErrorMapping を返す(表に無ければ None)。"""
    for exc_type, mapping in ERROR_TABLE.items():
        if isinstance(exc, exc_type):
            return mapping
    return None
