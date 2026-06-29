"""FastAPI による Web API 層(JSON)。

プログラム向けの入口。宣言的 YAML は CLI 向けの入口として別系統に分離する。
manager の例外は exception_handler で HTTP ステータスへ正規化する。
"""

from contextlib import asynccontextmanager

import libvirt
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse

from .manager import ServerConflict, ServerManager, ServerNotFound
from .spec import ServerSpec, ServerSpecInput


@asynccontextmanager
async def lifespan(app: FastAPI):
    """起動時に一度だけ libvirt 接続を開き、全リクエストで共有する。

    libvirt 接続は内部ロックでスレッドセーフなため、スレッドプールで動く
    複数ハンドラから単一接続を共有してよい。
    """
    conn = libvirt.open("qemu:///system")
    app.state.manager = ServerManager(conn)
    try:
        yield
    finally:
        conn.close()


app = FastAPI(title="mini-vps-platform", lifespan=lifespan)


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
    body: ServerSpecInput,
    response: Response,
    mgr: ServerManager = Depends(get_manager),
) -> dict:
    """VM を宣言的・冪等に作成/収束する。

    新規作成なら 201、既存 spec と一致する no-op なら 200。spec 相違や管理外の
    同名 domain は 409(ServerConflict)。

    Args:
        name: URL パスから与える VM 名。
        body: name を除く spec。
        response: 201/200 を出し分けるための Response。
        mgr: 共有 ServerManager。

    Returns:
        spec と status をキーに持つ dict。
    """
    # 新規か既存かを事前判定して 201/200 を出し分ける(相違時は create が 409 を投げる)
    try:
        mgr.get(name)
        created = False
    except ServerNotFound:
        created = True

    spec = ServerSpec(name=name, **body.model_dump()).model_dump()
    result = mgr.create(spec)
    response.status_code = 201 if created else 200
    return result


@app.delete("/servers/{name}", status_code=204)
def delete_server(name: str, mgr: ServerManager = Depends(get_manager)) -> None:
    """管理対象の VM を削除する(不在/管理外なら 404)。"""
    mgr.delete(name)
