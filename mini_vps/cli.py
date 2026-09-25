"""CLI 層(YAML)。

人間向けの入口。JSON の Web API は `api.py` に分離する。`manager.py` の例外を
HTTP ステータスではなく終了コードへ正規化する点のみが api.py との違いで、
それ以外はどちらも `ServerManager` の薄いラッパーである。
"""

import contextlib
import functools
import json
import os
import shlex
import sys
from typing import Annotated

import libvirt
import typer
import yaml
from pydantic import ValidationError

from . import errors
from .doctor import has_error
from .guest_agent import STDIN_LIMIT_BYTES
from .logging_config import configure as configure_logging
from .manager import ServerManager, register_quiet_error_handler
from .platform_profile import get_profile
from .spec import load_spec
from .stack import DEFAULT_WAIT_TIMEOUT, apply_stack, load_stack, plan_stack
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

# stop/restart で共有する --force オプションの型。
_ForceOption = Annotated[
    bool,
    typer.Option("--force", help="ACPI を待たず即座に強制する"),
]


@app.callback()
def _main_callback(
    verbose: Annotated[
        int,
        typer.Option(
            "--verbose",
            "-v",
            count=True,
            help="ログを詳しくする(-v で INFO、-vv で DEBUG)。"
            "サブコマンドより前に置くこと",
        ),
    ] = 0,
) -> None:
    """全コマンド共通の前処理。ログ設定を適用する。

    グループオプションのため `mini-vps -v list` の位置でのみ受け付ける。
    `mini-vps list -v` は click がサブコマンドのオプションとして解釈しエラーになる。
    """
    level = {0: None, 1: "INFO"}.get(verbose, "DEBUG")
    configure_logging(level)


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
    conn = libvirt.open(get_profile().libvirt_uri)
    try:
        yield ServerManager(conn)
    finally:
        conn.close()


def _parse_startup_params(pairs: list[str]) -> dict[str, str]:
    """--startup-param の KEY=VALUE 文字列のリストを dict に変換する。

    値側に "=" を含みうる(base64 トークン等)ため、str.split ではなく
    先頭の1つだけ分割する str.partition を使う。形式不正は StartupScriptError。
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
    """ハンドラの戻り値を種類に応じた形式(str/1行ずつ/JSON)で標準出力へ書く。"""
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
    (終了コード 2, Usage 表示)に委ねる。1 は入力(spec ファイル・
    --startup-param)の誤り。管理層の例外の終了コードは errors.ERROR_TABLE が決める。
    """

    @functools.wraps(func)
    def wrapper(ctx: typer.Context, *args, **kwargs):
        factory = ctx.obj
        try:
            with factory() as mgr:
                ctx.obj = mgr
                result = func(ctx, *args, **kwargs)
        except errors.MiniVpsError as e:
            mapping = errors.lookup(e)
            if mapping is None:
                raise
            print(f"error: {mapping.label}: {e}", file=sys.stderr)
            raise typer.Exit(code=mapping.exit_code) from None
        except libvirt.libvirtError as e:
            # register_quiet_error_handler() が libvirt 自身の stderr を抑止するため、
            # ここで出さないと libvirtd 停止時に traceback だけが残る。
            print(f"error: libvirt: {e}", file=sys.stderr)
            raise typer.Exit(code=7) from None
        except (ValidationError, yaml.YAMLError, OSError, StartupScriptError) as e:
            print(f"error: {e}", file=sys.stderr)
            raise typer.Exit(code=1) from None
        _print_result(result)

    return wrapper


def _command(name: str, *, help: str):
    """`_run_command` を必ず適用したうえで `app.command` に登録するデコレータ。"""

    def decorator(func):
        return app.command(name, help=help)(_run_command(func))

    return decorator


@_command(
    "create",
    help="VM スペックの YAML から VM を宣言的に作成・収束する",
)
def _cmd_create(
    ctx: typer.Context,
    spec_file: Annotated[str, typer.Argument(help="VM スペックの YAML ファイルパス")],
    startup_param: _StartupParamOption = [],
) -> dict:
    """VM スペックの YAML ファイルから VM を宣言的に作成/収束する。

    既存 VM に対して再実行した場合、memory/vcpus/filters の差分のみドメイン停止中に
    限り収束させる(起動中なら ServerRunning)。それ以外のフィールドの差分は
    ServerConflict で拒否する(ServerManager.create 参照)。
    """
    with open(spec_file, encoding="utf-8") as f:
        spec = load_spec(f.read())
    secrets = _parse_startup_params(startup_param)
    result, _created = ctx.obj.create(spec, secrets=secrets or None)
    return result


# plan/apply で共有するオプションの型。
_StackFileArgument = Annotated[str, typer.Argument(help="スタックファイル(YAML)のパス")]
_PruneOption = Annotated[
    bool,
    typer.Option(
        "--prune",
        help="同じ stack ラベルを持つがファイルに無い VM を削除する",
    ),
]


def _parse_server_startup_params(pairs: list[str]) -> dict[str, dict[str, str]]:
    """--startup-param SERVER:KEY=VALUE(apply 用)を server ごとの dict に変換する。

    server の name は ":" を含みえない(spec の name 制約)ため、先頭の ":" で分ける。
    KEY=VALUE 側は _parse_startup_params と同じ規則。形式不正は StartupScriptError。
    """
    secrets: dict[str, dict[str, str]] = {}
    for pair in pairs:
        server, sep, rest = pair.partition(":")
        if not sep or not server:
            raise StartupScriptError(
                "invalid --startup-param (expected SERVER:KEY=VALUE): "
                f"{pair.partition('=')[0]!r}"
            )
        secrets.setdefault(server, {}).update(_parse_startup_params([rest]))
    return secrets


def _read_stack(stack_file: str):
    """スタックファイルを読み込み、検証済みの Stack を返す。"""
    with open(stack_file, encoding="utf-8") as f:
        return load_stack(f.read())


@_command("plan", help="スタックファイルと既存 VM の差分(変更計画)を表示する")
def _cmd_plan(
    ctx: typer.Context,
    stack_file: _StackFileArgument,
    prune: _PruneOption = False,
) -> dict:
    """スタックの変更計画を返す。何も変更しない(stack.plan_stack 参照)。"""
    return plan_stack(ctx.obj, _read_stack(stack_file), prune=prune).to_dict()


@_command("apply", help="スタックファイルの VM を依存順に一括で作成・収束する")
def _cmd_apply(
    ctx: typer.Context,
    stack_file: _StackFileArgument,
    prune: _PruneOption = False,
    wait: Annotated[
        bool,
        typer.Option("--wait", help="依存先の VM が起動し IP を得るまで待ってから進む"),
    ] = False,
    wait_timeout: Annotated[
        float,
        typer.Option("--wait-timeout", min=1, help="依存先1台あたりの待ち時間(秒)"),
    ] = DEFAULT_WAIT_TIMEOUT,
    startup_param: Annotated[
        list[str],
        typer.Option(
            "--startup-param",
            metavar="SERVER:KEY=VALUE",
            help="server の startup_script に渡す秘密パラメータ(複数回指定可)",
        ),
    ] = [],
) -> dict:
    """スタックを適用する。

    conflict / blocked_running を含む計画は何も変更せずに拒否する。途中で失敗したら
    適用済みと未適用の VM を報告して止める(stack.apply_stack 参照)。
    """
    stack = _read_stack(stack_file)
    secrets = _parse_server_startup_params(startup_param)
    return apply_stack(
        ctx.obj,
        stack,
        prune=prune,
        wait=wait,
        secrets=secrets,
        wait_timeout=wait_timeout,
    )


@_command("get", help="VM の spec と状態を取得する")
def _cmd_get(ctx: typer.Context, name: str) -> dict:
    """指定 VM の spec と状態を返す。"""
    return ctx.obj.get(name)


@_command("list", help="管理対象の VM 名一覧を表示する")
def _cmd_list(ctx: typer.Context) -> list[str]:
    """管理対象の VM 名一覧を返す。"""
    return ctx.obj.list()


def _human_bytes(n: int) -> str:
    """バイト数を 1024 進の短い表記(例: 3.5GiB)にする。"""
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024:
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}TiB"


def _format_images(images: list[dict]) -> list[str]:
    """Image list の結果を列を揃えた表の行にする(image が無ければ空リスト)。"""
    if not images:
        return []
    rows = [("NAME", "VIRTUAL", "ACTUAL", "FORMAT", "USED_BY")] + [
        (
            i["name"],
            _human_bytes(i["virtual_bytes"]),
            _human_bytes(i["actual_bytes"]),
            i["format"] or "-",
            ",".join(i["used_by"]) or "-",
        )
        for i in images
    ]
    widths = [max(len(row[col]) for row in rows) for col in range(4)]
    return [
        "  ".join(cell.ljust(w) for cell, w in zip(row[:4], widths)) + "  " + row[4]
        for row in rows
    ]


def _format_gc(result: dict) -> list[str]:
    """Gc の結果を1リソース1行の文字列にする。"""
    if not result["orphans"]:
        return ["no orphans"]
    if not result["applied"]:
        return [
            f"would remove: {o['pool'] or 'nwfilter'}/{o['name']}"
            for o in result["orphans"]
        ]
    lines = [
        f"removed: {r['pool'] or 'nwfilter'}/{r['name']}" for r in result["removed"]
    ]
    lines += [
        f"skipped: {r['pool'] or 'nwfilter'}/{r['name']} ({r['reason']})"
        for r in result["skipped"]
    ]
    return lines


image_app = typer.Typer(help="base image を扱う", no_args_is_help=True)
app.add_typer(image_app, name="image")


@image_app.command("list", help="base image と参照している VM を一覧する")
@_run_command
def _cmd_image_list(ctx: typer.Context) -> list[str]:
    """Base image の名前・仮想サイズ・実サイズ・フォーマット・参照 VM を表で返す。"""
    return _format_images(ctx.obj.images())


@_command(
    "doctor",
    help="ホストの前提と孤児リソースを検査する(error があれば終了コード 1)",
)
def _cmd_doctor(ctx: typer.Context) -> None:
    """検査結果を1行ずつ出し、error が1件でもあれば終了コード 1 で終える。

    結果の出力と終了コードを両立させるため、_run_command の出力に任せず自分で
    stdout に書いてから typer.Exit を送出する。
    """
    results = ctx.obj.doctor()
    for r in results:
        print(f"{r['level']:<5}  {r['check']}: {r['detail']}")
    raise typer.Exit(code=1 if has_error(results) else 0)


@_command("gc", help="孤児リソース(volume・seed・nwfilter)を回収する(既定は dry-run)")
def _cmd_gc(
    ctx: typer.Context,
    apply: Annotated[
        bool, typer.Option("--apply", help="dry-run をやめて実際に削除する")
    ] = False,
) -> list[str]:
    """孤児リソースを一覧する。--apply のときは name 単位ロックを取って削除する。"""
    return _format_gc(ctx.obj.gc(apply=apply))


@_command("status", help="VM の状態(state, ip)を取得する")
def _cmd_status(ctx: typer.Context, name: str) -> dict:
    """指定 VM の状態(state, ip)を返す。"""
    return ctx.obj.status(name)


@_command("start", help="VM を起動する")
def _cmd_start(ctx: typer.Context, name: str) -> dict:
    """指定 VM を起動する(起動中なら冪等に no-op)。"""
    return ctx.obj.start(name)


@_command("stop", help="VM を停止する")
def _cmd_stop(ctx: typer.Context, name: str, force: _ForceOption = False) -> dict:
    """指定 VM を停止する(停止中なら冪等に no-op。挙動は ServerManager.stop 参照)。"""
    return ctx.obj.stop(name, force=force)


@_command("restart", help="VM を再起動する(disk は保持する)")
def _cmd_restart(ctx: typer.Context, name: str, force: _ForceOption = False) -> dict:
    """指定 VM を再起動する(disk・spec・IP は保持。ServerManager.restart 参照)。"""
    return ctx.obj.restart(name, force=force)


# exec の --raw でタイムアウトしたときの終了コード(coreutils の timeout と同じ)。
_EXEC_TIMEOUT_EXIT_CODE = 124


def _exec_raw_exit_code(result: dict) -> int:
    """`exec --raw` の終了コードを、ゲスト側の終了状態から決める(シェルの慣習)。"""
    if result["timed_out"]:
        return _EXEC_TIMEOUT_EXIT_CODE
    if result["exit_code"] is not None:
        return result["exit_code"]
    if result["signal"] is not None:
        return 128 + result["signal"]
    return 1


def _read_stdin_bytes() -> bytes:
    """標準入力を上限+1バイトまで読む(上限超過の判定は guest_agent に任せる)。"""
    return sys.stdin.buffer.read(STDIN_LIMIT_BYTES + 1)


@_command(
    "exec",
    help="guest agent 経由で VM 内のコマンドを実行する(例: exec web-1 -- ls -la /)",
)
def _cmd_exec(
    ctx: typer.Context,
    name: str,
    command: Annotated[
        list[str],
        typer.Argument(help="実行するコマンドと引数(- で始まる引数は -- の後に置く)"),
    ],
    raw: Annotated[
        bool,
        typer.Option(
            "--raw",
            help="stdout/stderr をそのまま流し、ゲスト側の終了コードで終了する",
        ),
    ] = False,
    stdin: Annotated[
        bool,
        typer.Option("--stdin", help="このプロセスの標準入力をゲストのコマンドへ渡す"),
    ] = False,
    timeout: Annotated[
        float,
        typer.Option("--timeout", min=0.001, help="終了を待つ秒数"),
    ] = 60,
) -> dict | None:
    """VM 内でコマンドを実行する(ServerManager.exec 参照)。

    既定は他コマンドと同じく結果を JSON で出す。--raw はシェルやエージェントから
    使うためのモードで、ゲストの stdout/stderr をそのまま書き出し、終了コードを
    引き継ぐ(シグナル終了は 128+番号、タイムアウトは 124)。
    """
    data = _read_stdin_bytes() if stdin else None
    result = ctx.obj.exec(name, command, stdin=data, timeout=timeout)
    if not raw:
        return result
    sys.stdout.write(result["stdout"])
    sys.stdout.flush()
    sys.stderr.write(result["stderr"])
    if result["truncated"]:
        print("warning: output truncated", file=sys.stderr)
    if result["timed_out"]:
        print(
            f"error: timed out; still running in guest (pid {result['pid']})",
            file=sys.stderr,
        )
    sys.stderr.flush()
    raise typer.Exit(code=_exec_raw_exit_code(result))


def _ssh_argv(endpoint: dict, extra: list[str]) -> list[str]:
    """ssh_endpoint の結果から ssh コマンドの argv を組み立てる。

    IdentitiesOnly は、ssh-agent に鍵が多いと専用鍵を試す前に
    "Too many authentication failures" で切られるのを避けるため。
    """
    return [
        "ssh",
        "-i",
        endpoint["identity_file"],
        "-p",
        str(endpoint["port"]),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        f"{endpoint['user']}@{endpoint['host']}",
        *extra,
    ]


@_command("ssh", help="VM へ SSH 接続する(-- の後は ssh への追加引数)")
def _cmd_ssh(
    ctx: typer.Context,
    name: str,
    extra: Annotated[
        list[str] | None,
        typer.Argument(help="ssh に渡す追加の引数(リモートで実行するコマンドなど)"),
    ] = None,
    print_only: Annotated[
        bool,
        typer.Option("--print", help="接続せず、実行する ssh コマンドを表示する"),
    ] = False,
) -> str | None:
    """VM へ SSH 接続する。このプロセスを ssh に置き換える(os.execvp)。"""
    argv = _ssh_argv(ctx.obj.ssh_endpoint(name), extra or [])
    if print_only:
        return shlex.join(argv)
    os.execvp(argv[0], argv)
    return None


@_command("console", help="VM のシリアルコンソールに接続する(抜けるのは Ctrl+])")
def _cmd_console(ctx: typer.Context, name: str) -> None:
    """`virsh console` で VM のシリアルコンソールに接続する(os.execvp)。

    存在しない・管理対象外の name は virsh に渡す前に ServerNotFound で拒否する。
    """
    ctx.obj.status(name)
    argv = ["virsh", "-c", get_profile().libvirt_uri, "console", name]
    os.execvp(argv[0], argv)


@_command("pause", help="VM を一時停止する(vCPU を凍結。メモリは保持)")
def _cmd_pause(ctx: typer.Context, name: str) -> dict:
    """指定 VM を一時停止する(一時停止中なら冪等に no-op)。"""
    return ctx.obj.pause(name)


@_command("resume", help="一時停止中の VM を再開する")
def _cmd_resume(ctx: typer.Context, name: str) -> dict:
    """指定 VM を再開する(稼働中なら冪等に no-op)。"""
    return ctx.obj.resume(name)


@_command("delete", help="管理対象の VM を削除する")
def _cmd_delete(ctx: typer.Context, name: str) -> str:
    """管理対象の VM を削除する。"""
    ctx.obj.delete(name)
    return f"deleted: {name}"


@_command("reinstall", help="VM の disk を base から作り直して再起動する")
def _cmd_reinstall(
    ctx: typer.Context,
    name: str,
    startup_param: _StartupParamOption = [],
) -> dict:
    """指定 VM の disk を作り直し、同じ spec で再起動する。

    secrets は永続化されないため --startup-param は毎回渡し直す
    (ServerManager.reinstall 参照)。
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
        プロセス終了コード。

        - 0: 成功
        - 1: spec ファイル関連のエラー(ValidationError / YAML / OSError /
          StartupScriptError)
        - 2: Typer の使用法エラー専用。Click の `UsageError.exit_code` が
          この値を予約しているため、ドメイン例外には割り当てない
        - 3: ServerNotFound
        - 4: ServerConflict
        - 5: ServerNotRunning
        - 6: ServerRunning(create が可変フィールド差分を起動中の VM に
          適用しようとした場合を含む)
        - 7: libvirtError(libvirtd 停止・接続不可など)
        - 8: PlatformUnsupported
        - 9: GuestAgentUnavailable
        - 11: InsufficientCapacity(容量チェックで作成・拡張を拒否)
        - 12: StackError(スタックの検証・計画・適用の失敗)

        `exec --raw` はこれらに加えて、ゲスト側のコマンドの終了コードで終了する。
        `doctor` は検査で error が1件でもあれば 1 を返す。
    """
    factory = manager_factory or _open_manager
    try:
        app(args=argv, obj=factory)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1
    return 0


def run() -> None:
    """コンソールスクリプト(`mini-vps`)のエントリポイント。"""
    sys.exit(main())
