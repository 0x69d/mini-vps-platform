"""FastAPI による Web API 層(JSON)。

プログラム向けの入口。宣言的 YAML は CLI 向けの入口として別系統に分離する。
manager の例外は exception_handler で HTTP ステータスへ正規化する。
"""

from contextlib import asynccontextmanager

import libvirt
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import LIBVIRT_URI
from .lifecycle import FiltersUnsupported
from .manager import (
    ServerConflict,
    ServerManager,
    ServerNotFound,
    ServerNotRunning,
    ServerRunning,
    register_quiet_error_handler,
)
from .spec import ServerSpec, ServerSpecInput
from .startup_scripts import StartupScriptError


@asynccontextmanager
async def lifespan(app: FastAPI):
    """起動時に一度だけ libvirt 接続を開き、全リクエストで共有する。

    libvirt 接続は内部ロックでスレッドセーフなため、スレッドプールで動く
    複数ハンドラから単一接続を共有してよい。ただしそれは個々の API 呼び出しの保証で
    あり、複数呼び出しにまたがる create/delete の収束のアトミック性は ServerManager の
    name 単位ロックの責務である。
    """
    register_quiet_error_handler()
    conn = libvirt.open(LIBVIRT_URI)
    app.state.manager = ServerManager(conn)
    try:
        yield
    finally:
        conn.close()


app = FastAPI(title="mini-vps-platform", lifespan=lifespan)


class ServerSpecInputWithSecrets(ServerSpecInput):
    """PUT /servers/{name} の入力。

    ServerSpecInput に secrets を足しただけの API 境界専用モデル。secrets は
    ハンドラ内で分離し、ServerSpec/libvirt の metadata には一切渡さない。
    """

    secrets: dict[str, str] = Field(default_factory=dict)


class ReinstallRequest(BaseModel):
    """POST /servers/{name}/reinstall の任意 body。"""

    secrets: dict[str, str] = Field(default_factory=dict)


class PowerActionRequest(BaseModel):
    """POST /servers/{name}/stop, /restart の任意 body。"""

    force: bool = False


def get_manager(request: Request) -> ServerManager:
    """共有 ServerManager を返す依存。"""
    return request.app.state.manager


@app.exception_handler(ServerNotFound)
async def _not_found_handler(request: Request, exc: ServerNotFound) -> JSONResponse:
    """ServerNotFound を 404 に変換する。"""
    return JSONResponse(status_code=404, content={"detail": f"server not found: {exc}"})


@app.exception_handler(ServerConflict)
async def _conflict_handler(request: Request, exc: ServerConflict) -> JSONResponse:
    """ServerConflict を 409 に変換する。"""
    return JSONResponse(status_code=409, content={"detail": f"server conflict: {exc}"})


@app.exception_handler(ServerNotRunning)
async def _not_running_handler(request: Request, exc: ServerNotRunning) -> JSONResponse:
    """ServerNotRunning を 409 に変換する。"""
    return JSONResponse(
        status_code=409, content={"detail": f"server not running: {exc}"}
    )


@app.exception_handler(ServerRunning)
async def _running_handler(request: Request, exc: ServerRunning) -> JSONResponse:
    """ServerRunning を 409 に変換する。"""
    return JSONResponse(status_code=409, content={"detail": f"server running: {exc}"})


@app.exception_handler(StartupScriptError)
async def _startup_script_error_handler(
    request: Request, exc: StartupScriptError
) -> JSONResponse:
    """StartupScriptError を 422 に変換する(pydantic 検証エラーと同じ意味論)。"""
    return JSONResponse(
        status_code=422, content={"detail": f"startup script error: {exc}"}
    )


@app.exception_handler(FiltersUnsupported)
async def _filters_unsupported_handler(
    request: Request, exc: FiltersUnsupported
) -> JSONResponse:
    """FiltersUnsupported を 422 に変換する(spec とホスト構成の非互換)。

    409 だと「対象 VM の状態を変えれば通る」と誤誘導するため、
    StartupScriptError と同じ 422 に寄せる。
    """
    return JSONResponse(
        status_code=422, content={"detail": f"filters unsupported: {exc}"}
    )


@app.get("/servers")
def list_servers(mgr: ServerManager = Depends(get_manager)) -> dict:
    """管理対象の VM 名一覧を返す。"""
    return {"servers": mgr.list()}


@app.get("/servers/{name}")
def get_server(name: str, mgr: ServerManager = Depends(get_manager)) -> dict:
    """指定 VM の spec と状態を返す(不在なら 404)。"""
    return mgr.get(name)


@app.get("/servers/{name}/status")
def get_status(name: str, mgr: ServerManager = Depends(get_manager)) -> dict:
    """指定 VM の状態(state, ip)を返す(不在なら 404)。"""
    return mgr.status(name)


@app.put("/servers/{name}")
def put_server(
    name: str,
    body: ServerSpecInputWithSecrets,
    response: Response,
    mgr: ServerManager = Depends(get_manager),
) -> dict:
    """VM を宣言的に作成/収束する。

    新規作成なら 201。既存 spec と完全一致する no-op、または memory/vcpus/filters
    のみの差分を収束させた場合は 200(収束は対象 VM がドメイン停止中の場合のみ、
    稼働中なら 409/ServerRunning)。それ以外のフィールドの差分、または管理外の
    同名 domain は 409(ServerConflict)。body は name を除く spec と secrets。
    """
    # 201/200 の判定は create が name ロック内で原子的に行う(created を返す)。
    # ハンドラ側で事前 get すると並行 2 本が共に created=True になり破綻するため避ける。
    payload = body.model_dump()
    secrets = payload.pop("secrets")
    spec = ServerSpec(name=name, **payload).model_dump()
    result, created = mgr.create(spec, secrets=secrets or None)
    response.status_code = 201 if created else 200
    return result


@app.post("/servers/{name}/start")
def start_server(name: str, mgr: ServerManager = Depends(get_manager)) -> dict:
    """管理対象の VM を起動する(起動中なら冪等に no-op、不在/管理外なら 404)。"""
    return mgr.start(name)


@app.post("/servers/{name}/stop")
def stop_server(
    name: str,
    body: PowerActionRequest | None = None,
    mgr: ServerManager = Depends(get_manager),
) -> dict:
    """管理対象の VM を停止する(停止中なら冪等に no-op、不在/管理外なら 404)。

    既定はゲスト OS への ACPI 経由の正常シャットダウンで、実際に shutoff になる
    まで待たない。body.force=true 指定時は即座に強制停止する。
    """
    return mgr.stop(name, force=body.force if body else False)


@app.post("/servers/{name}/restart")
def restart_server(
    name: str,
    body: PowerActionRequest | None = None,
    mgr: ServerManager = Depends(get_manager),
) -> dict:
    """管理対象の VM を再起動する(disk・spec・IP は変更しない、不在/管理外なら 404)。

    既定はゲスト OS への ACPI 経由の正常再起動。body.force=true 指定時は
    電源断→起動による強制再起動を行う。
    """
    return mgr.restart(name, force=body.force if body else False)


@app.delete("/servers/{name}", status_code=204)
def delete_server(name: str, mgr: ServerManager = Depends(get_manager)) -> None:
    """管理対象の VM を削除する(不在/管理外なら 404)。"""
    mgr.delete(name)


@app.post("/servers/{name}/reinstall")
def reinstall_server(
    name: str,
    body: ReinstallRequest | None = None,
    mgr: ServerManager = Depends(get_manager),
) -> dict:
    """管理対象の VM の disk を初期化し、同じ spec で再起動する(不在なら 404)。

    spec["startup_script"] の秘密情報は metadata に永続化されないため、
    テンプレートを再度効かせたい場合は body.secrets を渡し直す必要がある。
    """
    secrets = body.secrets if body else None
    return mgr.reinstall(name, secrets=secrets or None)
