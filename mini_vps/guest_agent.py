"""qemu-guest-agent 経由のゲスト操作。libvirt_qemu への依存をここに閉じ込める。

ゲスト内の qemu-guest-agent(qga)とは、domain XML の virtio-serial channel
`org.qemu.guest_agent.0` を通じて JSON で話す。libvirt はこれを
`libvirt_qemu.qemuAgentCommand(dom, json_text, timeout, 0)` として素通しし、
応答の JSON 文字列(`{"return": ...}`)をそのまま返す。

JSON の組み立て・応答の解釈・base64 デコード・切り詰めは外部依存の無い純粋関数に
分け、libvirt を呼ぶ関数(`ping` / `exec_command` / `agent_ipv4`)はその組み合わせに
留める。

libvirt 側のエラーの出方(libvirt 10.0 の qemuDomainAgentAvailable /
qemuAgentCheckError で確認):

- domain が停止中・一時停止中: VIR_ERR_OPERATION_INVALID("domain is not running")
- domain XML に channel が無い: VIR_ERR_ARGUMENT_UNSUPPORTED
  ("QEMU guest agent is not configured")
- channel はあるが agent が未接続・応答しない: VIR_ERR_AGENT_UNRESPONSIVE
- agent がコマンドにエラーを返した: VIR_ERR_INTERNAL_ERROR
  ("unable to execute QEMU agent command 'guest-exec': <agent のエラー文>")

なお `qemuAgentCommand`(任意コマンドの素通し)を使うと、libvirt は domain に
`custom-ga-command` の taint を付け、domain のログに1行警告を残す。動作には影響しない。
"""

import base64
import dataclasses
import ipaddress
import json
import time

import libvirt
import libvirt_qemu

from .errors import GuestAgentUnavailable, GuestExecError, ServerNotRunning

# 1回の agent 呼び出し(guest-exec / guest-exec-status など)の応答待ち秒数。
# guest-exec はプロセスを起動するだけで即座に返るため、コマンドの実行時間とは無関係。
AGENT_CALL_TIMEOUT_SECONDS = 10

# exec の stdout / stderr それぞれの上限。libvirt の RPC は1つの文字列を 4 MiB
# (VIR_NET_MESSAGE_STRING_MAX)までしか運べず、guest-exec-status の応答には
# stdout と stderr が base64(4/3 倍)で両方載る。
OUTPUT_LIMIT_BYTES = 1024 * 1024

# exec に渡せる stdin の上限。guest-exec の要求にも base64 で載るため、同じ RPC の
# 上限から余裕を取って決める。
STDIN_LIMIT_BYTES = 1024 * 1024

# qga 自身が guest-exec の出力を捕捉する上限(qga/commands.c の GUEST_EXEC_MAX_OUTPUT)。
QGA_OUTPUT_MAX_BYTES = 16 * 1024 * 1024

# guest-exec-status のポーリング間隔。短いコマンドをすぐ返すため小さく始め、倍々で
# 上限まで伸ばす。
_POLL_INITIAL_SECONDS = 0.05
_POLL_MAX_SECONDS = 1.0


@dataclasses.dataclass(frozen=True)
class ExecResult:
    """guest-exec 1回分の結果。

    Attributes:
        pid: ゲスト内のプロセス ID。
        exit_code: 終了コード。シグナルで終了した場合とタイムアウト時は None。
        signal: プロセスを終了させたシグナル番号。無ければ None。
        stdout: 標準出力(上限で切り詰め済み)。
        stderr: 標準エラー出力(上限で切り詰め済み)。
        truncated: stdout / stderr のどちらかが切り詰められたか。
        timed_out: timeout までにプロセスが終わらなかったか。True のとき
            プロセスはゲストで走り続けており、出力は取得できていない。
    """

    pid: int
    exit_code: int | None
    signal: int | None
    stdout: bytes
    stderr: bytes
    truncated: bool
    timed_out: bool


# --- 純粋関数 ---


def build_request(execute: str, arguments: dict | None = None) -> str:
    """Guest agent(qga)に送る JSON リクエストを組み立てる。

    Args:
        execute: qga のコマンド名(例: "guest-exec")。
        arguments: コマンドの引数。None なら arguments キーを付けない。

    Returns:
        qemuAgentCommand に渡す JSON 文字列。
    """
    request: dict = {"execute": execute}
    if arguments is not None:
        request["arguments"] = arguments
    return json.dumps(request)


def parse_reply(text: str):
    """Guest agent の応答 JSON から "return" の値を取り出す。

    libvirt は "error" を含む応答を例外に変換済みなので、ここに来るのは成功応答のみ。

    Raises:
        ValueError: "return" を含まない応答の場合。
    """
    reply = json.loads(text)
    if not isinstance(reply, dict) or "return" not in reply:
        raise ValueError("guest agent の応答に return がありません")
    return reply["return"]


def build_exec_arguments(argv: list[str], stdin: bytes | None = None) -> dict:
    """guest-exec の引数を組み立てる。

    qga は path を PATH から探すため、argv[0] は "ls" のようなコマンド名でもよい。
    stdin を渡すと qga が子プロセスの標準入力へ書き込んでから閉じる。渡さなければ
    子プロセスの標準入力は qga の既定(/dev/null)になる。

    Args:
        argv: 実行するコマンドと引数。
        stdin: 標準入力へ渡すバイト列。None なら渡さない。

    Returns:
        guest-exec の arguments。

    Raises:
        GuestExecError: argv が空、または stdin が STDIN_LIMIT_BYTES を超える場合。
    """
    if not argv:
        raise GuestExecError("argv が空です")
    if stdin is not None and len(stdin) > STDIN_LIMIT_BYTES:
        raise GuestExecError(
            f"stdin が上限 {STDIN_LIMIT_BYTES} バイトを超えています({len(stdin)})"
        )
    arguments = {"path": argv[0], "arg": list(argv[1:]), "capture-output": True}
    if stdin is not None:
        arguments["input-data"] = base64.b64encode(stdin).decode("ascii")
    return arguments


def decode_output(
    data: str | None, agent_truncated: bool | None, limit: int = OUTPUT_LIMIT_BYTES
) -> tuple[bytes, bool]:
    """guest-exec-status の out-data / err-data を base64 デコードし上限で切り詰める。

    qga の out-truncated / err-truncated は版によって意味が揺れる(QEMU 8.2 までは
    切り詰め時にキーだけが付き値は false、以降は出力があれば常にキーが付き値が
    真偽を表す)。そのため値が true か、デコード後の長さが qga の捕捉上限に
    達しているかのどちらかで qga 側の切り詰めと判定する。

    Args:
        data: base64 文字列。出力が無ければ None。
        agent_truncated: qga の *-truncated の値。無ければ None。
        limit: 返すバイト数の上限。

    Returns:
        (出力のバイト列, 切り詰められたか) のタプル。
    """
    raw = base64.b64decode(data) if data else b""
    truncated = agent_truncated is True or len(raw) >= QGA_OUTPUT_MAX_BYTES
    if len(raw) > limit:
        raw = raw[:limit]
        truncated = True
    return raw, truncated


def parse_exec_status(
    status: dict, pid: int, limit: int = OUTPUT_LIMIT_BYTES
) -> ExecResult | None:
    """guest-exec-status の応答を解釈する。

    qga は exited が true になった応答でだけ終了コードと出力を返し、同時にその pid
    の記録を捨てる。したがって exited の応答は1度しか受け取れない。

    Args:
        status: guest-exec-status の "return" の値。
        pid: 問い合わせた pid。
        limit: stdout / stderr それぞれの上限バイト数。

    Returns:
        終了していれば ExecResult、まだ実行中なら None。
    """
    if not status.get("exited"):
        return None
    stdout, out_truncated = decode_output(
        status.get("out-data"), status.get("out-truncated"), limit
    )
    stderr, err_truncated = decode_output(
        status.get("err-data"), status.get("err-truncated"), limit
    )
    return ExecResult(
        pid=pid,
        exit_code=status.get("exitcode"),
        signal=status.get("signal"),
        stdout=stdout,
        stderr=stderr,
        truncated=out_truncated or err_truncated,
        timed_out=False,
    )


def pick_ipv4(ifaces: dict, macs: set[str] | None = None) -> str | None:
    """NIC のアドレス一覧から、ループバック・リンクローカル以外の IPv4 を選ぶ。

    macs を渡すと、その MAC アドレスを持つ NIC のアドレスを優先する。ゲスト内に
    docker0 などのブリッジがあっても、VM の NIC のアドレスを返すため。見つからなければ
    最初に見つかったアドレスを返す。

    Args:
        ifaces: `dom.interfaceAddresses()` の戻り値
            ({名前: {"hwaddr": ..., "addrs": [{"type", "addr", "prefix"}]}})。
        macs: 優先する MAC アドレスの集合(小文字)。

    Returns:
        IPv4 アドレス。無ければ None。
    """
    candidates = []
    for iface in (ifaces or {}).values():
        hwaddr = (iface.get("hwaddr") or "").lower()
        for addr in iface.get("addrs") or []:
            if addr.get("type") != libvirt.VIR_IP_ADDR_TYPE_IPV4:
                continue
            ip = ipaddress.IPv4Address(addr["addr"])
            if ip.is_loopback or ip.is_link_local:
                continue
            candidates.append((hwaddr, str(ip)))
    if macs:
        for hwaddr, ip in candidates:
            if hwaddr in macs:
                return ip
    return candidates[0][1] if candidates else None


def translate_error(err: libvirt.libvirtError, name: str) -> Exception | None:
    """Guest agent 呼び出しの libvirtError を、原因を区別した管理層の例外へ変換する。

    ホストから見ると「agent が未導入」と「未起動」は区別できない(どちらも channel の
    向こうに誰もいない)。起動直後は cloud-init が qemu-guest-agent を導入・起動する
    までこの状態になるため、メッセージで待てば解消しうることを伝える。

    Args:
        err: qemuAgentCommand / interfaceAddresses が送出した例外。
        name: VM 名(メッセージ用)。

    Returns:
        変換後の例外。guest agent の可用性と無関係なエラーなら None。
    """
    code = err.get_error_code()
    if code == libvirt.VIR_ERR_OPERATION_INVALID:
        return ServerNotRunning(f"{name} (起動していないか一時停止中)")
    if code == libvirt.VIR_ERR_ARGUMENT_UNSUPPORTED:
        return GuestAgentUnavailable(
            f"{name}: domain XML に guest agent の channel がありません"
            "(channel を付ける前に作った VM)。reinstall では domain XML が"
            "変わらないため、delete して create し直してください"
        )
    if code in (libvirt.VIR_ERR_AGENT_UNRESPONSIVE, libvirt.VIR_ERR_AGENT_UNSYNCED):
        return GuestAgentUnavailable(
            f"{name}: qemu-guest-agent が応答しません(未導入・未起動。起動直後なら"
            "cloud-init が導入を終えるまで待って再試行してください)"
        )
    if code == libvirt.VIR_ERR_INTERNAL_ERROR and "has been disabled" in str(err):
        return GuestAgentUnavailable(
            f"{name}: ゲストの qemu-guest-agent の設定で"
            "このコマンドが無効化されています"
        )
    return None


# --- libvirt を呼ぶ関数 ---


def _agent_command(
    dom, execute: str, arguments: dict | None = None, timeout: int | None = None
):
    """Guest agent にコマンドを1回送り、"return" の値を返す。

    Raises:
        ServerNotRunning: domain が停止中・一時停止中の場合。
        GuestAgentUnavailable: guest agent を使えない場合。
        libvirt.libvirtError: それ以外の libvirt エラー(agent がコマンドに
            エラーを返した場合を含む)。
    """
    request = build_request(execute, arguments)
    if timeout is None:
        timeout = AGENT_CALL_TIMEOUT_SECONDS
    try:
        text = libvirt_qemu.qemuAgentCommand(dom, request, timeout, 0)
    except libvirt.libvirtError as e:
        translated = translate_error(e, dom.name())
        if translated is not None:
            raise translated from e
        raise
    return parse_reply(text)


def ping(dom) -> None:
    """guest-ping で guest agent が応答するか確かめる。

    Raises:
        ServerNotRunning: domain が停止中・一時停止中の場合。
        GuestAgentUnavailable: guest agent を使えない場合(理由はメッセージ)。
    """
    _agent_command(dom, "guest-ping")


def exec_command(
    dom,
    argv: list[str],
    stdin: bytes | None = None,
    timeout: float = 60,
    *,
    clock=time.monotonic,
    sleep=time.sleep,
) -> ExecResult:
    """ゲスト内でコマンドを実行し、終了まで待って結果を返す。

    guest-exec でプロセスを起動し、guest-exec-status を間隔を伸ばしながら
    ポーリングして exited になるまで待つ。コマンドは qga の権限(通常 root)で、
    シェルを介さずに argv のまま実行される(パイプやリダイレクトが要るなら
    `["sh", "-c", "..."]` を渡す)。

    timeout を超えた場合は例外にせず、timed_out=True・exit_code=None の結果を返す。
    qga にはプロセスを止める手段が無いため、プロセスはゲストで走り続け、その出力は
    取得できない。例外にしないのは、pid を返して呼び出し側が後始末
    (`exec VM -- kill PID` など)できるようにするため。

    Args:
        dom: libvirt domain。
        argv: 実行するコマンドと引数。
        stdin: 標準入力へ渡すバイト列(上限 STDIN_LIMIT_BYTES)。
        timeout: 終了を待つ秒数。
        clock: 経過時間の計測に使う関数(テスト用の差し替え口)。
        sleep: ポーリング間の待機に使う関数(テスト用の差し替え口)。

    Returns:
        ExecResult。stdout / stderr は各 OUTPUT_LIMIT_BYTES で切り詰める。

    Raises:
        GuestExecError: argv・stdin・timeout が不正、またはゲストでコマンドを
            開始できない(実行ファイルが無いなど)場合。
        ServerNotRunning: domain が停止中・一時停止中の場合。
        GuestAgentUnavailable: guest agent を使えない場合。
    """
    if timeout <= 0:
        raise GuestExecError(f"timeout は正の秒数で指定してください({timeout})")
    arguments = build_exec_arguments(argv, stdin)
    try:
        started = _agent_command(dom, "guest-exec", arguments)
    except libvirt.libvirtError as e:
        # 残る INTERNAL_ERROR は qga が guest-exec にエラーを返したもの
        # (多くは実行ファイルが見つからない)。メッセージは qga の説明文を含む。
        if e.get_error_code() == libvirt.VIR_ERR_INTERNAL_ERROR:
            raise GuestExecError(f"{dom.name()}: {e}") from e
        raise
    pid = started["pid"]

    deadline = clock() + timeout
    interval = _POLL_INITIAL_SECONDS
    while True:
        status = _agent_command(dom, "guest-exec-status", {"pid": pid})
        result = parse_exec_status(status, pid)
        if result is not None:
            return result
        remaining = deadline - clock()
        if remaining <= 0:
            return ExecResult(
                pid=pid,
                exit_code=None,
                signal=None,
                stdout=b"",
                stderr=b"",
                truncated=False,
                timed_out=True,
            )
        sleep(min(interval, remaining))
        interval = min(interval * 2, _POLL_MAX_SECONDS)


def agent_ipv4(dom, macs: set[str] | None = None) -> str | None:
    """Guest agent が報告するアドレスから、ループバック以外の IPv4 を1つ返す。

    DHCP リースを持たない VM(静的 IP を cloud-init 以外で設定した場合や、
    user-mode ネットワーク)でも IP を得るために使う。

    Args:
        dom: libvirt domain。
        macs: 優先する NIC の MAC アドレス(小文字)。pick_ipv4 参照。

    Returns:
        IPv4 アドレス。agent がアドレスを報告しなければ None。

    Raises:
        ServerNotRunning: domain が停止中・一時停止中の場合。
        GuestAgentUnavailable: guest agent を使えない場合。
        libvirt.libvirtError: それ以外の libvirt エラー。
    """
    try:
        ifaces = dom.interfaceAddresses(
            libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_AGENT
        )
    except libvirt.libvirtError as e:
        translated = translate_error(e, dom.name())
        if translated is not None:
            raise translated from e
        raise
    return pick_ipv4(ifaces, macs)
