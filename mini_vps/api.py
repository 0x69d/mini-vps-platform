"""FastAPI による Web API 層(JSON)。

プログラム向けの入口。宣言的 YAML は CLI 向けの入口として別系統に分離する。
manager の例外は exception_handler で HTTP ステータスへ正規化する。
"""

import logging
from contextlib import asynccontextmanager

import libvirt
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import errors
from .doctor import has_error
from .logging_config import configure as configure_logging
from .manager import ServerManager, register_quiet_error_handler
from .platform_profile import get_profile
from .spec import ServerSpec, ServerSpecInput
from .stack import (
    DEFAULT_WAIT_TIMEOUT,
    StackDefinition,
    apply_stack,
    plan_stack,
    resolve_stack,
)
from .startup_scripts import StartupScriptError

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """起動時に一度だけ libvirt 接続を開き、全リクエストで共有する。

    libvirt 接続は内部ロックでスレッドセーフなため、スレッドプールで動く
    複数ハンドラから単一接続を共有してよい。ただしそれは個々の API 呼び出しの保証で
    あり、複数呼び出しにまたがる create/delete の収束のアトミック性は ServerManager の
    name 単位ロックの責務である。

    ログは CLI / exporter と同じく自前のハンドラを付けて設定する。uvicorn が
    ハンドラを付けるのは "uvicorn" 系ロガーだけでルートには付けないため、
    伝播に任せると INFO 以下が出力先を持たず消える。レベルは環境変数
    MINIVPS_LOG_LEVEL で制御する。
    """
    configure_logging()
    register_quiet_error_handler()
    conn = libvirt.open(get_profile().libvirt_uri)
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


class StackPlanRequest(StackDefinition):
    """POST /stacks/plan の body(スタックファイルと同じ構造 + prune)。"""

    prune: bool = False


class StackApplyRequest(StackPlanRequest):
    """POST /stacks/apply の body。

    secrets は server の name → startup_script に渡す秘密情報。apply_stack() が
    ServerManager.create() にだけ渡し、spec/metadata・ログ・応答には載せない。
    """

    wait: bool = False
    wait_timeout: float = Field(default=DEFAULT_WAIT_TIMEOUT, gt=0, le=3600)
    secrets: dict[str, dict[str, str]] = Field(default_factory=dict)


class ExecRequest(BaseModel):
    """POST /servers/{name}/exec の body。

    argv はシェルを介さずにそのまま実行される(パイプ等が要るなら
    ["sh", "-c", "..."])。stdin は UTF-8 で符号化してゲストの標準入力へ渡す。
    """

    argv: list[str] = Field(min_length=1)
    stdin: str | None = None
    timeout: float = Field(default=60, gt=0, le=3600)


def get_manager(request: Request) -> ServerManager:
    """共有 ServerManager を返す依存。"""
    return request.app.state.manager


@app.exception_handler(errors.MiniVpsError)
async def _minivps_error_handler(
    request: Request, exc: errors.MiniVpsError
) -> JSONResponse:
    """管理層の例外を errors.ERROR_TABLE の HTTP ステータスに変換する。"""
    mapping = errors.lookup(exc)
    if mapping is None:
        raise exc
    return JSONResponse(
        status_code=mapping.http_status,
        content={"detail": f"{mapping.label}: {exc}"},
    )


@app.exception_handler(StartupScriptError)
async def _startup_script_error_handler(
    request: Request, exc: StartupScriptError
) -> JSONResponse:
    """StartupScriptError を 422 に変換する(pydantic 検証エラーと同じ意味論)。"""
    return JSONResponse(
        status_code=422, content={"detail": f"startup script error: {exc}"}
    )


@app.exception_handler(libvirt.libvirtError)
async def _libvirt_error_handler(
    request: Request, exc: libvirt.libvirtError
) -> JSONResponse:
    """ホスト側の libvirtError を 503 に変換する(CLI の終了コード 7 と対応する)。

    他のハンドラと違いクライアント起因ではなくホスト側の障害のため、
    レスポンスだけでは運用者の手元に残らない。ここでログにも残す。
    ただし libvirt のメッセージは base_image パスなど spec の値を含みうるため、
    ログにはパスだけを書き、本文はレスポンスの detail にのみ載せる。
    """
    logger.warning("libvirt error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=503, content={"detail": f"libvirt error: {exc}"})


@app.get("/servers")
def list_servers(mgr: ServerManager = Depends(get_manager)) -> dict:
    """管理対象の VM 名一覧を返す。"""
    return {"servers": mgr.list()}


@app.get("/images")
def list_images(mgr: ServerManager = Depends(get_manager)) -> dict:
    """Base image の一覧(サイズ・フォーマット・参照している管理 VM)を返す。"""
    return {"images": mgr.images()}


@app.get("/doctor")
def get_doctor(mgr: ServerManager = Depends(get_manager)) -> dict:
    """ホストの前提と孤児リソースを検査する。

    検査自体が成功すれば error を含んでいても 200 を返し、ok で全体の可否を示す
    (ok は error が1件も無ければ true)。
    """
    checks = mgr.doctor()
    return {"ok": not has_error(checks), "checks": checks}


class GcRequest(BaseModel):
    """POST /gc の任意 body。"""

    apply: bool = False


@app.post("/gc")
def post_gc(
    body: GcRequest | None = None, mgr: ServerManager = Depends(get_manager)
) -> dict:
    """孤児リソースを回収する。

    既定(body 無し、または apply=false)は dry-run で、消す予定を返すだけ。
    apply=true のときは孤児ごとに name 単位ロックを取って再判定してから消す。
    """
    return mgr.gc(apply=body.apply if body else False)


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


@app.post("/stacks/plan")
def plan_stack_endpoint(
    body: StackPlanRequest, mgr: ServerManager = Depends(get_manager)
) -> dict:
    """スタックと既存 VM の差分(変更計画)を返す。何も変更しない。

    スタックとして不正(name の重複・depends_on の循環や未知の参照など)なら 422。
    """
    return plan_stack(mgr, resolve_stack(body), prune=body.prune).to_dict()


@app.post("/stacks/apply")
def apply_stack_endpoint(
    body: StackApplyRequest, mgr: ServerManager = Depends(get_manager)
) -> dict:
    """スタックの VM を依存順に一括で作成・収束する。

    conflict / blocked_running を含む計画は何も変更せずに 422 で拒否する。途中で
    失敗した場合も 422 で、detail に適用済みと未適用の VM を示す。wait=true なら
    依存先の起動を待つため、応答まで数分かかりうる。
    """
    return apply_stack(
        mgr,
        resolve_stack(body),
        prune=body.prune,
        wait=body.wait,
        secrets=body.secrets,
        wait_timeout=body.wait_timeout,
    )


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


@app.post("/servers/{name}/exec")
def exec_in_server(
    name: str, body: ExecRequest, mgr: ServerManager = Depends(get_manager)
) -> dict:
    """稼働中の VM 内で guest agent 経由でコマンドを実行する(ServerManager.exec)。

    終了まで待って exit_code・stdout・stderr などを返す。timeout を超えた場合も
    200 で timed_out=true を返す(プロセスはゲストで走り続ける)。停止中・一時停止中
    は 409(ServerNotRunning)、guest agent を使えなければ 409
    (GuestAgentUnavailable)、ゲストでコマンドを開始できなければ 422。
    """
    return mgr.exec(name, body.argv, stdin=body.stdin, timeout=body.timeout)


@app.post("/servers/{name}/pause")
def pause_server(name: str, mgr: ServerManager = Depends(get_manager)) -> dict:
    """稼働中の VM を一時停止する(一時停止中なら冪等に no-op、停止中なら 409)。"""
    return mgr.pause(name)


@app.post("/servers/{name}/resume")
def resume_server(name: str, mgr: ServerManager = Depends(get_manager)) -> dict:
    """一時停止中の VM を再開する(稼働中なら冪等に no-op、停止中なら 409)。"""
    return mgr.resume(name)


@app.get("/servers/{name}/ssh")
def get_ssh_endpoint(name: str, mgr: ServerManager = Depends(get_manager)) -> dict:
    """VM への SSH の接続先(host・port・user・identity_file)を返す。

    identity_file は API サーバを動かすユーザーのホームにある秘密鍵のパスで、
    鍵の中身は返さない。
    """
    return mgr.ssh_endpoint(name)


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
