"""CLI 層(YAML)。

人間向けの入口。JSON の Web API は `api.py` に分離する。`manager.py` の例外を
HTTP ステータスではなく終了コードへ正規化する点のみが api.py との違いで、
それ以外はどちらも `ServerManager` の薄いラッパーである。
"""

import argparse
import contextlib
import json
import sys

import libvirt
import yaml
from pydantic import ValidationError

from .config import LIBVIRT_URI
from .manager import ServerConflict, ServerManager, ServerNotFound
from .spec import load_spec


@contextlib.contextmanager
def _open_manager():
    """既定の manager_factory。libvirt 接続を開閉しつつ ServerManager を貸し出す。

    CLI は1回の呼び出しごとに短命プロセスとして起動するため、API の
    `lifespan`(プロセス起動時に1度だけ open)とは異なり、呼び出しのたびに
    open/close する。

    Yields:
        ServerManager。
    """
    conn = libvirt.open(LIBVIRT_URI)
    try:
        yield ServerManager(conn)
    finally:
        conn.close()


def _cmd_create(args: argparse.Namespace, mgr: ServerManager) -> dict:
    """VM スペックの YAML ファイルから VM を宣言的・冪等に作成する。

    Args:
        args: spec_file(YAML ファイルパス)を持つ引数。
        mgr: 対象の ServerManager。

    Returns:
        spec と status をキーに持つ dict。
    """
    with open(args.spec_file, encoding="utf-8") as f:
        spec = load_spec(f.read())
    result, _created = mgr.create(spec)
    return result


def _cmd_get(args: argparse.Namespace, mgr: ServerManager) -> dict:
    """指定 VM の spec と状態を返す。"""
    return mgr.get(args.name)


def _cmd_list(_args: argparse.Namespace, mgr: ServerManager) -> list[str]:
    """管理対象の VM 名一覧を返す。"""
    return mgr.list()


def _cmd_status(args: argparse.Namespace, mgr: ServerManager) -> dict:
    """指定 VM の状態(state, ip)を返す。"""
    return mgr.status(args.name)


def _cmd_delete(args: argparse.Namespace, mgr: ServerManager) -> str:
    """管理対象の VM を削除する。

    Returns:
        表示用の完了メッセージ。
    """
    mgr.delete(args.name)
    return f"deleted: {args.name}"


def _cmd_reinstall(args: argparse.Namespace, mgr: ServerManager) -> dict:
    """指定 VM の disk を作り直し、同じ spec で再起動する。"""
    return mgr.reinstall(args.name)


def _build_parser() -> argparse.ArgumentParser:
    """サブコマンドを定義した ArgumentParser を返す。

    サブコマンドは ServerManager の public メソッドと1対1に対応させ、
    api.py のエンドポイント構成と対称にする。
    """
    parser = argparse.ArgumentParser(
        prog="mini-vps", description="QEMU/KVM + libvirt 製 VM 制御プレーンの CLI"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser(
        "create", help="VM スペックの YAML から VM を宣言的に作成する"
    )
    create.add_argument("spec_file", help="VM スペックの YAML ファイルパス")
    create.set_defaults(func=_cmd_create)

    get = subparsers.add_parser("get", help="VM の spec と状態を取得する")
    get.add_argument("name")
    get.set_defaults(func=_cmd_get)

    list_ = subparsers.add_parser("list", help="管理対象の VM 名一覧を表示する")
    list_.set_defaults(func=_cmd_list)

    status = subparsers.add_parser("status", help="VM の状態(state, ip)を取得する")
    status.add_argument("name")
    status.set_defaults(func=_cmd_status)

    delete = subparsers.add_parser("delete", help="管理対象の VM を削除する")
    delete.add_argument("name")
    delete.set_defaults(func=_cmd_delete)

    reinstall = subparsers.add_parser(
        "reinstall", help="VM の disk を base から作り直して再起動する"
    )
    reinstall.add_argument("name")
    reinstall.set_defaults(func=_cmd_reinstall)

    return parser


def _print_result(result) -> None:
    """ハンドラの戻り値を種類に応じた形式で標準出力へ書く。

    Args:
        result: str ならそのまま、list ならVM名を1行ずつ、それ以外(dict)は
            JSON として出力する。
    """
    if isinstance(result, str):
        print(result)
    elif isinstance(result, list):
        for line in result:
            print(line)
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))


def main(argv: list[str] | None = None, manager_factory=None) -> int:
    """CLI のエントリポイント本体。

    manager.py の例外を、api.py の exception_handler(HTTP ステータス)と対称に
    終了コードへ正規化する。

    Args:
        argv: コマンドライン引数。None なら sys.argv から取得する。
        manager_factory: ServerManager を yield するコンテキストマネージャを
            返す呼び出し可能オブジェクト。テストで `ServerManager` を差し替える
            ためのフック(既定は libvirt 接続を開閉する `_open_manager`)。

    Returns:
        プロセス終了コード(成功 0、ServerNotFound 2、ServerConflict 3、
        spec ファイル関連のエラー 1)。
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    factory = manager_factory or _open_manager

    with factory() as mgr:
        try:
            result = args.func(args, mgr)
        except ServerNotFound as e:
            print(f"error: server not found: {e}", file=sys.stderr)
            return 2
        except ServerConflict as e:
            print(f"error: server conflict: {e}", file=sys.stderr)
            return 3
        except (ValidationError, yaml.YAMLError, OSError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    _print_result(result)
    return 0


def run() -> None:
    """コンソールスクリプト(`mini-vps`)のエントリポイント。"""
    sys.exit(main())
