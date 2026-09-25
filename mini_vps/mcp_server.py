"""MCP サーバ(4つ目の入口)。AI エージェントが VM を操作するためのツール群。

CLI(YAML)・Web API(JSON)・exporter と同じく `ServerManager` の薄いラッパーで、
管理層の例外は `errors.ERROR_TABLE` のラベルを付けたツールエラーに正規化する。
transport は stdio。stdout は MCP のプロトコルが使うため、ログは `logging_config`
の既定どおり stderr へ出す(print を使わないこと)。

破壊的な操作(delete / reinstall など、データが失われるもの)は、環境変数
`MINIVPS_MCP_ALLOW_DESTRUCTIVE=1` のときだけツールとして登録する。登録しなければ
エージェントからはツールの存在自体が見えない。エージェントに VM を渡すときの既定を
「壊せない」側に倒すため。

起動: `uv run mini-vps-mcp`(または `uv run python -m mini_vps.mcp_server`)。
"""

import functools
import logging
import os
from collections.abc import Callable

import anyio
import libvirt
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from . import errors
from .logging_config import configure as configure_logging
from .manager import ServerManager, register_quiet_error_handler
from .platform_profile import get_profile
from .spec import ServerSpec
from .startup_scripts import StartupScriptError

_LOGGER = logging.getLogger(__name__)

_ALLOW_DESTRUCTIVE_ENV_VAR = "MINIVPS_MCP_ALLOW_DESTRUCTIVE"

_INSTRUCTIONS = """\
mini-vps は、このマシン上の VM(QEMU/KVM・macOS では HVF)を宣言的に管理する。
VM は name で指定する。create_server は冪等で、同じ spec を再度渡すと何もしない。
spec のフィールドは get_server の spec と同じ
(name 以外を create_server の spec 引数へ渡す)。
エラーは「server not found: ...」のようにラベル付きで返る。
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
IDEMPOTENT = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
NON_IDEMPOTENT = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)
DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, open_world_hint=False
)


def destructive_allowed(env: dict | None = None) -> bool:
    """破壊的なツールを登録してよいかを環境変数から判定する。"""
    env = os.environ if env is None else env
    return env.get(_ALLOW_DESTRUCTIVE_ENV_VAR, "") in {"1", "true", "yes"}


def _to_tool_error(exc: Exception) -> ToolError:
    """管理層・入力検証・libvirt の例外を、モデルが読めるツールエラーへ変換する。

    CLI の終了コード・API の HTTP ステータスと同じ errors.ERROR_TABLE のラベルを
    使うため、どの入口でも同じ言葉で失敗が伝わる。
    """
    mapping = errors.lookup(exc)
    if mapping is not None:
        return ToolError(f"{mapping.label}: {exc}")
    if isinstance(exc, libvirt.libvirtError):
        return ToolError(f"libvirt error: {exc}")
    if isinstance(exc, (ValidationError, StartupScriptError)):
        return ToolError(f"invalid input: {exc}")
    raise exc


class Tools:
    """ServerManager の各操作を MCP ツールとして公開する。

    ツール関数は async で、ServerManager の同期呼び出しをワーカースレッドで実行する。
    exec のように数十秒かかる操作でも、イベントループ(= stdio の読み書き)を
    止めないため。ServerManager の書き込み系は name 単位ロックで直列化されるので、
    スレッドから並行に呼んでも安全。

    Attributes:
        manager: 操作対象の ServerManager。
    """

    def __init__(self, manager: ServerManager):
        self.manager = manager

    async def call(self, func: Callable, *args, **kwargs):
        """同期関数をワーカースレッドで実行し、例外をツールエラーへ正規化する。"""
        try:
            return await anyio.to_thread.run_sync(
                functools.partial(func, *args, **kwargs)
            )
        except Exception as e:  # noqa: BLE001  (_to_tool_error が想定外は再送出する)
            raise _to_tool_error(e) from None


def build_server(manager: ServerManager, allow_destructive: bool = False) -> MCPServer:
    """ツールを登録した MCPServer を組み立てる。

    Args:
        manager: ツールが操作する ServerManager。
        allow_destructive: 破壊的なツール(delete / reinstall など)を登録するか。

    Returns:
        stdio などで run() できる MCPServer。
    """
    server = MCPServer(name="mini-vps", instructions=_INSTRUCTIONS)
    tools = Tools(manager)

    @server.tool(annotations=READ_ONLY)
    async def list_servers() -> dict:
        """管理対象の VM 名の一覧を返す(Web API の GET /servers と同じ形)。"""
        return {"servers": await tools.call(manager.list)}

    @server.tool(annotations=READ_ONLY)
    async def get_server(name: str) -> dict:
        """VM の spec と状態(state, ip)を返す。"""
        return await tools.call(manager.get, name)

    @server.tool(annotations=READ_ONLY)
    async def server_status(name: str) -> dict:
        """VM の状態(state, ip)だけを返す。"""
        return await tools.call(manager.status, name)

    @server.tool(annotations=IDEMPOTENT)
    async def create_server(
        name: str, spec: dict, secrets: dict[str, str] | None = None
    ) -> dict:
        """VM を宣言的に作成・収束する(冪等)。

        spec は name 以外のフィールド(memory[MiB], vcpus, base_image, disk[GiB],
        networks, filters, egress, autostart, startup_script など)。既存 VM と同じ
        spec なら何もしない。secrets は startup_script に渡す秘密情報で、VM の
        metadata には保存されない。
        """
        full_spec = await tools.call(lambda: ServerSpec(name=name, **spec).model_dump())
        result, created = await tools.call(
            manager.create, full_spec, secrets=secrets or None
        )
        return {**result, "created": created}

    @server.tool(annotations=IDEMPOTENT)
    async def start_server(name: str) -> dict:
        """VM を起動する(起動中なら何もしない)。"""
        return await tools.call(manager.start, name)

    @server.tool(annotations=IDEMPOTENT)
    async def stop_server(name: str, force: bool = False) -> dict:
        """VM を停止する。既定は ACPI の正常シャットダウン、force で即時停止。"""
        return await tools.call(manager.stop, name, force=force)

    @server.tool(annotations=NON_IDEMPOTENT)
    async def restart_server(name: str, force: bool = False) -> dict:
        """VM を再起動する(disk・spec・IP は変えない)。"""
        return await tools.call(manager.restart, name, force=force)

    if allow_destructive:

        @server.tool(annotations=DESTRUCTIVE)
        async def delete_server(name: str) -> dict:
            """VM を削除する。ディスクも消え、元に戻せない。"""
            await tools.call(manager.delete, name)
            return {"deleted": name}

        @server.tool(annotations=DESTRUCTIVE)
        async def reinstall_server(
            name: str, secrets: dict[str, str] | None = None
        ) -> dict:
            """VM のディスクを base image から作り直す。ディスクの中身は失われる。"""
            return await tools.call(manager.reinstall, name, secrets=secrets or None)

    return server


def main() -> None:
    """MCP サーバを stdio で起動する。"""
    configure_logging()
    register_quiet_error_handler()
    conn = libvirt.open(get_profile().libvirt_uri)
    allow = destructive_allowed()
    _LOGGER.info("MCP サーバを起動 destructive=%s", allow)
    try:
        build_server(ServerManager(conn), allow_destructive=allow).run("stdio")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
