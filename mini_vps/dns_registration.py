"""nsupdate による DNS レコードの自動登録・削除(opt-in・ベストエフォート)。

3つの環境変数(MINIVPS_DNS_SERVER / MINIVPS_DNS_ZONE /
MINIVPS_DNS_TSIG_KEY_FILE)がすべて設定されているときのみ有効になり、
1つでも欠ければ機能全体が完全に無効(既存挙動と同一)になる。DNS 操作は
BIND 付属の nsupdate CLI を subprocess で呼ぶ薄い層とし、dnspython 等の
追加依存は入れない(cloud-localds を subprocess で呼ぶ
resources.build_seed_iso() と同型)。

公開関数(register / unregister)は例外を一切送出しない(ベストエフォート
契約)。DNS の失敗で VM の create/delete を失敗させると、dns-1 自身の
再作成すら不可能になる循環依存が生じるため、失敗は警告ログにとどめる
(設計判断の詳細は docs/dns-registration.md)。

TSIG 鍵は環境変数で受けたファイルパスを nsupdate の -k にそのまま渡すだけで、
本モジュールはファイルを開かない。鍵の中身は spec・libvirt metadata・ログ・
例外メッセージのいずれにも現れない。

警告の出力には CLI 入口層の print() ではなく logging を使う。manager 層は
CLI / API / exporter の3入口から共有されるライブラリ層であり、logging 未設定の
CLI でも標準の last-resort ハンドラが WARNING 以上を stderr に出すため体験は
print と同等、API(uvicorn)配下ではログ基盤に統合できる。
"""

import ipaddress
import logging
import os
import subprocess

_LOGGER = logging.getLogger(__name__)

_DNS_SERVER_ENV_VAR = "MINIVPS_DNS_SERVER"
_DNS_ZONE_ENV_VAR = "MINIVPS_DNS_ZONE"
_DNS_TSIG_KEY_FILE_ENV_VAR = "MINIVPS_DNS_TSIG_KEY_FILE"

# nsupdate -t: 1リクエストの上限秒。既定の300秒は dns-1 停止時に
# create/delete を長時間ブロックするため数秒に絞る。
_NSUPDATE_TIMEOUT_SEC = 5
# subprocess.run のハードリミット。A と PTR の2トランザクション分 + 余裕。
_SUBPROCESS_TIMEOUT_SEC = 15
_RECORD_TTL = 300


def _config() -> tuple[str, str, str] | None:
    """環境変数から DNS 登録の設定を読む。

    呼び出しのたびに読む(import 時に固定しない)ことで、テストの
    monkeypatch.setenv や API 常駐プロセスでの設定変更に追随する。

    Returns:
        3変数すべてが設定されていれば (server, zone, key_file)。zone は
        末尾ドットを除去して正規化する。1つでも欠けていれば None
        (機能全体が無効)。
    """
    server = os.environ.get(_DNS_SERVER_ENV_VAR)
    zone = os.environ.get(_DNS_ZONE_ENV_VAR)
    key_file = os.environ.get(_DNS_TSIG_KEY_FILE_ENV_VAR)
    if not server or not zone or not key_file:
        return None
    return server, zone.rstrip("."), key_file


def _first_static_ipv4(spec: dict) -> str | None:
    """spec["networks"] の先頭から最初の静的NICの IPv4 アドレスを返す。

    manager._static_ipv4() と同じ規約(status/get が表示する管理IPと
    登録される A レコードを一致させる)。依存方向を manager →
    dns_registration の一方向に保つため import はせず、同じ走査を持つ。
    """
    for net in spec.get("networks", []):
        if isinstance(net, dict) and net.get("address"):
            return net["address"].split("/", 1)[0]
    return None


def _names(zone: str, name: str, ip: str) -> tuple[str, str]:
    """登録に使う FQDN と PTR レコード名(いずれも末尾ドット付き)を返す。"""
    fqdn = f"{name}.{zone}."
    ptr = f"{ipaddress.IPv4Address(ip).reverse_pointer}."
    return fqdn, ptr


def _run_nsupdate(script: str, key_file: str, name: str, action: str) -> None:
    """TSIG 鍵付きで nsupdate を実行し、失敗は警告ログに落とす(例外は送出しない)。

    Args:
        script: nsupdate の stdin に渡すコマンド列。
        key_file: TSIG 鍵ファイルのパス(-k に渡すのみで中身は読まない)。
        name: ログ表示用の VM 名。
        action: ログ表示用の操作名("register" / "unregister")。
    """
    cmd = ["nsupdate", "-k", key_file, "-t", str(_NSUPDATE_TIMEOUT_SEC)]
    try:
        subprocess.run(
            cmd,
            input=script,
            text=True,
            capture_output=True,
            check=True,
            timeout=_SUBPROCESS_TIMEOUT_SEC,
        )
    except subprocess.CalledProcessError as exc:
        # nsupdate の stderr に鍵素材は出ない(鍵はパス参照のみ)。
        _LOGGER.warning(
            "dns %s failed for %s: nsupdate exited %d: %s",
            action,
            name,
            exc.returncode,
            (exc.stderr or "").strip(),
        )
    except subprocess.TimeoutExpired:
        _LOGGER.warning(
            "dns %s failed for %s: nsupdate timed out after %ds",
            action,
            name,
            _SUBPROCESS_TIMEOUT_SEC,
        )
    except OSError as exc:
        # nsupdate 未導入(FileNotFoundError)等。
        _LOGGER.warning("dns %s failed for %s: %s", action, name, exc)


def register(spec: dict) -> None:
    """VM の A レコードと PTR レコードを登録する(opt-in・ベストエフォート)。

    最初の静的NICの IP で <name>.<zone> の A と対応する PTR を登録する。
    既存レコードを update delete してから add する冪等な組で書くため、
    reinstall や再実行でも重複しない。A と PTR は別ゾーンのため send を
    2回に分ける(RFC 2136 の動的更新は1メッセージ=1ゾーン)。

    静的NICが1つも無い VM はスキップし、その旨をログに残す。失敗しても
    例外は送出しない。

    Args:
        spec: ServerSpec の model_dump 形式の dict。
    """
    config = _config()
    if config is None:
        return
    server, zone, key_file = config
    name = spec["name"]
    ip = _first_static_ipv4(spec)
    if ip is None:
        _LOGGER.info("dns register skipped for %s: no static NIC", name)
        return
    fqdn, ptr = _names(zone, name, ip)
    script = (
        f"server {server}\n"
        f"update delete {fqdn} A\n"
        f"update add {fqdn} {_RECORD_TTL} A {ip}\n"
        f"send\n"
        f"update delete {ptr} PTR\n"
        f"update add {ptr} {_RECORD_TTL} PTR {fqdn}\n"
        f"send\n"
    )
    _run_nsupdate(script, key_file, name, "register")


def unregister(spec: dict) -> None:
    """VM の A レコードと PTR レコードを削除する(opt-in・ベストエフォート)。

    register と同じ規約(最初の静的NIC)で対象レコードを特定して削除する。
    静的NICが無い VM は登録もされていないためスキップする。失敗しても
    例外は送出しない。

    Args:
        spec: ServerSpec の model_dump 形式の dict。
    """
    config = _config()
    if config is None:
        return
    server, zone, key_file = config
    name = spec["name"]
    ip = _first_static_ipv4(spec)
    if ip is None:
        _LOGGER.info("dns unregister skipped for %s: no static NIC", name)
        return
    fqdn, ptr = _names(zone, name, ip)
    script = (
        f"server {server}\n"
        f"update delete {fqdn} A\n"
        f"send\n"
        f"update delete {ptr} PTR\n"
        f"send\n"
    )
    _run_nsupdate(script, key_file, name, "unregister")
