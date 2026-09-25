# ゲストの操作と監督(exec・ssh・console・pause/resume)

VM の外(人間やホスト側のエージェント)から、VM の中を操作・監督するための機能。
ロードマップの A1〜A3 にあたる。実装は `mini_vps/guest_agent.py`(qemu-guest-agent との
通信)と `ServerManager` の `exec` / `pause` / `resume` / `ssh_endpoint`。

| 操作 | CLI | Web API | 必要なもの |
|---|---|---|---|
| コマンド実行 | `mini-vps exec NAME -- CMD...` | `POST /servers/{name}/exec` | ゲストの qemu-guest-agent |
| SSH 接続 | `mini-vps ssh NAME` | `GET /servers/{name}/ssh`(接続先を返す) | IP(DHCP リースか guest agent)と専用鍵 |
| シリアルコンソール | `mini-vps console NAME` | なし | `virsh` |
| 一時停止・再開 | `mini-vps pause NAME` / `resume NAME` | `POST /servers/{name}/pause`・`/resume` | なし |

## exec: guest agent 経由のコマンド実行

```bash
mini-vps exec web-1 -- uname -a            # 結果を JSON で表示
mini-vps exec --raw web-1 -- ls -la /etc   # 出力をそのまま流し、ゲストの終了コードで終わる
echo 'hello' | mini-vps exec --raw --stdin web-1 -- tee /tmp/greeting
mini-vps exec --timeout 600 web-1 -- sh -c 'apt-get update && apt-get -y upgrade'
```

```bash
curl -X POST localhost:8000/servers/web-1/exec \
  -H 'content-type: application/json' \
  -d '{"argv": ["sh", "-c", "df -h /"], "timeout": 30}'
```

戻り値は `pid`・`exit_code`・`signal`・`stdout`・`stderr`・`truncated`・`timed_out`。

- **SSH もネットワークも使わない。** virtio-serial の channel `org.qemu.guest_agent.0` を
  通じてゲスト内の qemu-guest-agent に `guest-exec` を送り、`guest-exec-status` を
  ポーリングして終了を待つ。ネットワークが壊れたゲストや、egress を絞ったゲストにも届く。
- **シェルを介さない。** `argv` はそのまま実行される(`argv[0]` は PATH から探される)。
  パイプ・リダイレクト・`&&` が要るなら `["sh", "-c", "..."]` を渡す。CLI では `-` で
  始まる引数を含むコマンドを `--` の後に置く(`--` より前の `--raw` などは mini-vps の
  オプションとして解釈される)。
- **stdin** は最大 1 MiB。CLI の `--stdin` はこのプロセスの標準入力を読んで渡し、
  API の `stdin` は文字列を UTF-8 で渡す。指定しなければゲスト側の標準入力は空になる。
- **出力** は stdout・stderr それぞれ 1 MiB で切り詰め、`truncated: true` で知らせる。
  UTF-8 として復号し、不正なバイトは置換文字になる(バイナリを取り出す用途には向かない。
  `base64` を通すか、ファイルに書いて別の手段で取り出す)。
- **timeout**(既定 60 秒、API は最大 3600 秒)を超えると、例外ではなく
  `timed_out: true`・`exit_code: null` を返す。qemu-guest-agent にはプロセスを止める
  コマンドが無いため、**プロセスはゲストで走り続ける**。止めるには返った `pid` を使って
  `mini-vps exec NAME -- kill PID` する。`--raw` ではこのとき終了コード 124 になる
  (coreutils の `timeout` と同じ)。シグナルで終わったコマンドは 128+シグナル番号。
- exec は name ロックを取らない。長いコマンドの実行中でも、同じ VM への `stop` や
  `pause` がすぐ効くようにするため。

### 信頼境界

exec は **ゲスト内の root 権限でのコマンド実行と同等** である。qemu-guest-agent は
root で動き、ユーザー・パスワード・SSH 鍵の確認を一切しない。

- ホストで mini-vps の CLI を実行できる人、または Web API に到達できる人は、全 VM の
  root を持つ。Web API を他人が到達できるアドレスで待ち受けないこと(現状の API には
  認証が無い。UNIX ドメインソケットと権限モデルはロードマップのフェーズ4)。
- 逆向きの境界は保たれる。ゲストの中から guest agent を通じてホストや他の VM を
  操作する経路は無い(channel はホストからゲストへの要求にしか使わない)。
- ログには name・`argv[0]`・終了コードだけを出す。引数・stdin・出力は secrets を
  含みうるため、どのレベルのログにも出さない。API のレスポンスと CLI の出力には当然含まれる。
- libvirt はこの経路(`virDomainQemuAgentCommand`)を使った domain に
  `custom-ga-command` の taint を付け、domain のログに警告を1行残す。動作には影響しない。

### 使えないときのエラー

exec が失敗したときは、原因ごとに次のように返す(CLI の終了コード / HTTP ステータス)。

| 状況 | エラー | CLI / HTTP |
|---|---|---|
| VM が停止中・一時停止中 | `server not running` | 5 / 409 |
| domain XML に guest agent の channel が無い(古い VM) | `guest agent unavailable` | 9 / 409 |
| qemu-guest-agent が未導入・未起動・応答しない | `guest agent unavailable` | 9 / 409 |
| ゲストの qemu-guest-agent 設定で `guest-exec` が無効化されている | `guest agent unavailable` | 9 / 409 |
| 実行ファイルがゲストに無い・argv が空・stdin が大きすぎる | `guest exec failed` | 1 / 422 |

ホストからは「agent が未導入」と「未起動」を区別できない(どちらも channel の向こうに
誰もいない)。起動直後は cloud-init が qemu-guest-agent をパッケージで導入して起動する
までこの状態になるので、数十秒〜数分待って再試行する。

## guest agent が使えないケース

- **channel を付ける前に作った VM。** domain XML の guest agent channel は、
  基盤のリファクタリング(`build_domain_xml` の ElementTree 化)以降に作った VM に
  だけ付いている。`reinstall` は disk と seed ISO を作り直すだけで domain XML は
  変えないため、**reinstall では channel は増えない**。`delete` して同じ spec で
  `create` し直す(disk は作り直しになる)。どうしても disk を残したいなら、停止中に
  `virsh edit` で `<channel type='unix'><target type='virtio' name='org.qemu.guest_agent.0'/></channel>`
  を `<devices>` に足す手もあるが、mini-vps の管理外の変更になる。
- **qemu-guest-agent が入っていないゲスト。** cloud-init の user-data が
  `packages: [qemu-guest-agent]` で導入し、`runcmd` で起動する。パッケージの導入には
  ゲストからパッケージミラーへの到達が要る(egress を絞った VM では許可が必要)。
  古い VM は seed にこの指定が無いが、channel も無いので上の再作成で両方そろう。
- **ゲスト側で guest-exec が無効化されている。** ディストリビューションによっては
  qemu-guest-agent の既定設定(RHEL 系の `/etc/sysconfig/qemu-ga` など)で
  `guest-exec` を許可リストから外している。この場合はゲスト側の設定を変える必要がある。
  また SELinux が enforcing のゲストでは、exec したコマンドが qemu-ga の SELinux
  ドメインの制約を受けることがある。
- **VM が停止中・一時停止中。** libvirt は稼働中の domain にしか agent コマンドを
  送らない。

`status` / `get` の `ip` は、DHCP リースが無ければ guest agent が報告するアドレスで
補う(user-mode ネットワークの VM や、リースを持たない VM 向け)。agent が使えなければ
これまで通り `null` で、エラーにはしない。ゲスト内に docker0 などのブリッジがあっても、
VM の NIC(MAC アドレスで判定)のアドレスを優先する。

## ssh: SSH 接続

```bash
mini-vps ssh web-1                          # そのまま接続(このプロセスが ssh に置き換わる)
mini-vps ssh web-1 -- uptime                # リモートでコマンドを実行
mini-vps ssh web-1 -- -L 8080:localhost:80  # ssh への追加オプション
mini-vps ssh --print web-1                  # 実行する ssh コマンドを表示するだけ
```

接続先は `ServerManager.ssh_endpoint` が解決する(API は `GET /servers/{name}/ssh`)。

- libvirt ネットワーク(Linux): `status` と同じ方法で解決した IP の 22 番
  (静的アドレス → DHCP リース → guest agent の順)。
- user-mode ネットワーク(macOS): domain XML のポート転送先 `127.0.0.1:<ポート>`。
- ユーザーは spec の `user`、鍵は cloud-init が `authorized_keys` に入れた本ツール専用鍵
  `~/.ssh/minivps_ed25519`(公開鍵は `~/.ssh/minivps_ed25519.pub`)。
- `-o StrictHostKeyChecking=accept-new` で初回の host key を自動で受け入れ、
  `-o IdentitiesOnly=yes` で ssh-agent の他の鍵を試さない。
- VM を作り直すと host key が変わり、同じ IP・ポートに対して ssh が接続を拒否する。
  `ssh-keygen -R 192.168.122.10`(user-mode なら `ssh-keygen -R '[127.0.0.1]:2201'`)で
  古い鍵を消す。
- 停止中・一時停止中は `server not running`、稼働中でも IP が分からない(起動直後で
  DHCP リースも guest agent の報告も無い)ときは `guest agent unavailable` になる。

## console: シリアルコンソール

```bash
mini-vps console web-1    # 抜けるときは Ctrl+]
```

`virsh -c <libvirt URI> console NAME` に置き換わる。ネットワークも guest agent も
使わないため、起動が止まった・ネットワーク設定を壊したなど、他の手段が効かないときの
最後の入口になる。ゲストの `ttyS0` にログインプロンプトが出るが、cloud-init で作る
ユーザーはパスワードがロックされているため、ログインするにはゲスト側で事前に
パスワードを設定しておく必要がある(ブートログやカーネルメッセージの確認には不要)。

## pause / resume: 一時停止

```bash
mini-vps pause agent-1    # vCPU を凍結する
mini-vps exec agent-1 ... # 一時停止中は使えない(server not running)
mini-vps resume agent-1
```

`pause` は libvirt の `virDomainSuspend` で vCPU を止める。ゲストは自分が止められた
ことを知らず、プロセスもネットワーク接続もその瞬間の状態で凍る。

- **用途: 暴走したエージェントの凍結。** 大量の API 呼び出し・想定外の削除・
  データの持ち出しなど、VM の中のエージェントが危ない動きをしたときに、状態を
  壊さずに即座に止める。`stop --force`(電源断)と違い、メモリ上の状態が残るので、
  止めたまま調べてから `resume` するか、`stop --force` / `delete` するかを決められる。
- メモリは解放されない。ゲストが止まるので、新しいディスク I/O やネットワーク送信も起きない。
- 冪等: 一時停止中の `pause`、稼働中の `resume` は何もせず現状を返す。停止中の VM には
  どちらも `server not running`。
- 一時停止中は exec・ssh・guest agent からの IP 取得ができない。`status` の `state` は
  `paused` になる。
- 一時停止中の VM に `stop`(ACPI)を送っても、ゲストが止まっているので処理されない。
  止めるなら `resume` してから `stop` するか、`stop --force` を使う。

## 実装メモ

- libvirt のエラーの出方は libvirt 10.0 のソース(`qemuDomainAgentAvailable` /
  `qemuAgentCheckError`)で確認した。停止中・一時停止中は `VIR_ERR_OPERATION_INVALID`、
  channel が無ければ `VIR_ERR_ARGUMENT_UNSUPPORTED`、agent が未接続・応答しなければ
  `VIR_ERR_AGENT_UNRESPONSIVE`、agent がコマンドにエラーを返せば `VIR_ERR_INTERNAL_ERROR`。
- qemu-guest-agent は `guest-exec` の出力を最大 16 MiB まで溜め、`exited: true` を
  返した応答でだけ出力を渡してプロセスの記録を捨てる。timeout で見放したプロセスの
  記録は、誰も `guest-exec-status` を呼ばないため agent の中に残る。
- libvirt の RPC は1つの文字列を 4 MiB までしか運べない。stdin と出力の上限
  (各 1 MiB)はこれに収まるよう決めてある。ただし上限で切り詰めるのはホスト側に
  届いてからなので、ゲストで stdout と stderr の合計がおよそ 3 MiB を超えると、
  libvirt が応答を運べずにエラー(`libvirt error`)になり、出力は失われる。
  大きな出力はゲスト内のファイルに書き出す。
- `out-truncated` の意味は QEMU の版で揺れる(8.2 までは切り詰め時にキーだけが付き値は
  false)。値が true か、出力が 16 MiB に達しているかで判定している。
