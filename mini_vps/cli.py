"""CLI 層(YAML)。

人間向けの入口。JSON の Web API は `api.py` に分離する。`manager.py` の例外を
HTTP ステータスではなく終了コードへ正規化する点のみが api.py との違いで、
それ以外はどちらも `ServerManager` の薄いラッパーである。
"""

import contextlib
import functools
import json
import sys
from typing import Annotated

import libvirt
import typer
import yaml
from pydantic import ValidationError

from .config import LIBVIRT_URI
from .manager import (
    ServerConflict,
    ServerManager,
    ServerNotFound,
    register_quiet_error_handler,
)
from .spec import load_spec
from .startup_scripts import StartupScriptError

# add_completion=False: 運用ツールにシェル補完は不要なため。
app = typer.Typer(
    add_completion=False,
    help="QEMU/KVM + libvirt 製 VM 制御プレーンの CLI",
)

# create/reinstall で共有する --startup-param オプションの型。
_StartupParamOption = Annotated[
    list[str],
    typer.Option(
        "--startup-param",
        metavar="KEY=VALUE",
        help="startup_script に渡す秘密パラメータ(複数回指定可)",
    ),
]


@contextlib.contextmanager
def _open_manager():
    """既定の manager_factory。libvirt 接続を開閉しつつ ServerManager を貸し出す。

    CLI は1回の呼び出しごとに短命プロセスとして起動するため、API の
    `lifespan`(プロセス起動時に1度だけ open)とは異なり、呼び出しのたびに
    open/close する。

    Yields:
        ServerManager。
    """
    register_quiet_error_handler()
    conn = libvirt.open(LIBVIRT_URI)
    try:
        yield ServerManager(conn)
    finally:
        conn.close()


def _parse_startup_params(pairs: list[str]) -> dict[str, str]:
    """--startup-param の KEY=VALUE 文字列のリストを dict に変換する。

    値側に "=" を含みうる(base64 トークン等)ため、str.split ではなく
    先頭の1つだけ分割する str.partition を使う。

    Args:
        pairs: "KEY=VALUE" 形式の文字列のリスト。

    Returns:
        変換した secrets の dict。

    Raises:
        StartupScriptError: "KEY=VALUE" 形式でない要素がある場合。
    """
    secrets: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise StartupScriptError(
                f"invalid --startup-param (expected KEY=VALUE): {pair!r}"
            )
        secrets[key] = value
    return secrets


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


def _run_command(func):
    """コマンド関数を包み、manager 接続の開閉と例外の終了コード正規化を行う。

    api.py の @app.exception_handler(...) と対称に、例外を
    「HTTP ステータス」ではなく「終了コード」へ変換する。引数不足など
    純粋な Typer の使用法エラーはここでは扱わず、Typer の既定動作
    (終了コード2, Usage 表示)に委ねる。

    Args:
        func: ctx: typer.Context を第一引数に取るコマンド関数。

    Returns:
        例外捕捉と _print_result による出力整形を行うラップ済み関数。
    """

    @functools.wraps(func)
    def wrapper(ctx: typer.Context, *args, **kwargs):
        factory = ctx.obj
        try:
            with factory() as mgr:
                ctx.obj = mgr
                result = func(ctx, *args, **kwargs)
        except ServerNotFound as e:
            print(f"error: server not found: {e}", file=sys.stderr)
            raise typer.Exit(code=2) from None
        except ServerConflict as e:
            print(f"error: server conflict: {e}", file=sys.stderr)
            raise typer.Exit(code=3) from None
        except (ValidationError, yaml.YAMLError, OSError, StartupScriptError) as e:
            print(f"error: {e}", file=sys.stderr)
            raise typer.Exit(code=1) from None
        _print_result(result)

    return wrapper


def _command(name: str, *, help: str):
    """`_run_command` を必ず適用したうえで `app.command` に登録する。

    Args:
        name: サブコマンド名。
        help: `--help` に表示する説明文。

    Returns:
        コマンド関数に適用するデコレータ。
    """

    def decorator(func):
        return app.command(name, help=help)(_run_command(func))

    return decorator


@_command("create", help="VM スペックの YAML から VM を宣言的に作成する")
def _cmd_create(
    ctx: typer.Context,
    spec_file: Annotated[str, typer.Argument(help="VM スペックの YAML ファイルパス")],
    startup_param: _StartupParamOption = [],
) -> dict:
    """VM スペックの YAML ファイルから VM を宣言的・冪等に作成する。

    Args:
        ctx: ServerManager を保持する Typer コンテキスト(ctx.obj)。
        spec_file: VM スペックの YAML ファイルパス。
        startup_param: --startup-param の KEY=VALUE 文字列のリスト。

    Returns:
        spec と status をキーに持つ dict。
    """
    with open(spec_file, encoding="utf-8") as f:
        spec = load_spec(f.read())
    secrets = _parse_startup_params(startup_param)
    result, _created = ctx.obj.create(spec, secrets=secrets or None)
    return result


@_command("get", help="VM の spec と状態を取得する")
def _cmd_get(ctx: typer.Context, name: str) -> dict:
    """指定 VM の spec と状態を返す。"""
    return ctx.obj.get(name)


@_command("list", help="管理対象の VM 名一覧を表示する")
def _cmd_list(ctx: typer.Context) -> list[str]:
    """管理対象の VM 名一覧を返す。"""
    return ctx.obj.list()


@_command("status", help="VM の状態(state, ip)を取得する")
def _cmd_status(ctx: typer.Context, name: str) -> dict:
    """指定 VM の状態(state, ip)を返す。"""
    return ctx.obj.status(name)


@_command("delete", help="管理対象の VM を削除する")
def _cmd_delete(ctx: typer.Context, name: str) -> str:
    """管理対象の VM を削除する。

    Returns:
        表示用の完了メッセージ。
    """
    ctx.obj.delete(name)
    return f"deleted: {name}"


@_command("reinstall", help="VM の disk を base から作り直して再起動する")
def _cmd_reinstall(
    ctx: typer.Context,
    name: str,
    startup_param: _StartupParamOption = [],
) -> dict:
    """指定 VM の disk を作り直し、同じ spec で再起動する。

    spec["startup_script"] の秘密情報は metadata に永続化されないため、
    テンプレートを再度効かせたい場合は --startup-param を渡し直す必要がある。
    """
    secrets = _parse_startup_params(startup_param)
    return ctx.obj.reinstall(name, secrets=secrets or None)


def main(argv: list[str] | None = None, manager_factory=None) -> int:
    """CLI のエントリポイント本体。

    manager.py の例外を、api.py の exception_handler(HTTP ステータス)と対称に
    終了コードへ正規化する(実処理は各コマンド関数を包む _run_command が行う)。
    Typer は既定(standalone_mode=True)で動作し、内部で sys.exit() する。
    その SystemExit を捕捉して int の終了コードとして返す。

    Args:
        argv: コマンドライン引数。None なら sys.argv から取得する。
        manager_factory: ServerManager を yield するコンテキストマネージャを
            返す呼び出し可能オブジェクト。テストで `ServerManager` を差し替える
            ためのフック(既定は libvirt 接続を開閉する `_open_manager`)。

    Returns:
        プロセス終了コード(成功 0、ServerNotFound 2、ServerConflict 3、
        spec ファイル関連のエラー 1、Typer の使用法エラー 2)。
    """
    factory = manager_factory or _open_manager
    try:
        app(args=argv, obj=factory)
    except SystemExit as e:
        return e.code
    return 0


def run() -> None:
    """コンソールスクリプト(`mini-vps`)のエントリポイント。"""
    sys.exit(main())
