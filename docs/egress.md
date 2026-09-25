# egress 制御

VM から外へ出る通信(egress)を spec の `egress` で宣言的に絞る機能。AI エージェントを
住まわせる VM で一番怖いのは持ち出し(外向き通信)なので、「どこへなら出してよいか」を
VM ごとに決め、それ以外を遮断する。inbound の `filters` と同じ VM 専用の nwfilter に
組み込まれ、稼働中の VM にも止めずに反映できる。

## モデル

```yaml
egress:
  - {cidr: 192.168.122.1/32, protocol: udp, port: 53}   # accept は省略可
  - {action: drop, cidr: 10.0.0.0/8}
  - {cidr: 0.0.0.0/0}
```

| 値 | 意味 |
|---|---|
| 未指定(`null`) | 外向きは全許可(従来どおり)。egress 追加前に作った VM もこの扱い |
| リスト | **記述順に評価**し、最初に一致したルールで accept / drop が決まる。どれにも一致しなければ drop |
| `[]` | 全 drop(DHCP と、inbound で受けた接続への戻りパケットは除く) |

ルール1件(`EgressRule`)のフィールド:

| フィールド | 型 | 既定 | 説明 |
|---|---|---|---|
| `action` | `accept` \| `drop` | `accept` | 一致したときの動作 |
| `cidr` | IPv4 ネットワーク | 必須 | 宛先。`192.168.122.1` のように接頭辞長を省くと `/32`。ホストビットが立った値(`192.168.122.1/24`)は意図が曖昧なため拒否する |
| `protocol` | `tcp` \| `udp` \| `all` | `all` | `all` は ICMP などを含む IPv4 の全プロトコル |
| `port` | 1〜65535 \| `null` | `null` | 宛先ポート。`protocol` が `tcp` / `udp` のときだけ指定できる |

ルール数の上限は 499 件(nwfilter の priority の幅から決まる。後述)。検証は
`mini_vps/spec.py` の `EgressRule` / `ServerSpecInput.egress` にある。

`filters`(inbound)とは独立に指定できる。

| `filters` | `egress` | nwfilter | inbound | outbound |
|---|---|---|---|---|
| `null` | `null` | 作らない | 全許可 | 全許可 |
| リスト | `null` | 作る | 宣言ポートだけ | 全許可 |
| `null` | リスト | 作る | 全許可 | egress のとおり |
| リスト | リスト | 作る | 宣言ポートだけ | egress のとおり |

## 評価順

libvirt の nwfilter はルールを記述順ではなく **priority の昇順** で評価する(同じ
priority 同士の順序は保証されない)。そこで egress ルールには記述順に 501, 502, … と
単調増加する priority を振り、次の順に並べる。生成は `resources.build_nwfilter_xml`。

| priority | 方向 | ルール | 理由 |
|---|---|---|---|
| (チェーン) | 両方 | `allow-arp` / `allow-dhcp`(libvirt 同梱) | ARP と DHCP の初回取得 |
| 500 | out | `ESTABLISHED,RELATED` を accept | inbound で受けた接続(SSH など)への戻りを egress の drop に落とさない |
| 500 | out | UDP 68 → 67 を accept | DHCP のリース更新。`allow-dhcp` は 0.0.0.0 → 255.255.255.255 の初回取得しか許さず、クライアント IP からのユニキャストの更新が既定 drop に落ちるため |
| 500 | in | `filters` の各ポート(`filters: null` なら全 accept) | inbound |
| 501〜999 | out | egress ルール(記述順)。`state='NEW'` で新しい接続の最初のパケットだけを判定 | 利用者の宣言 |
| 1000 | out | IPv6 の送信を drop(ebtables 層) | egress ルールは IPv4 しか表せない。IPv6 を通すと link-local 経由でホストや同じブリッジ上の VM に抜けられる |
| 1000 | out | 残りを drop | 既定 drop |
| 1000 | in | 残りを drop(`filters` がリストのときだけ) | inbound の既定 drop |

egress ルールに `state='NEW'` を付けるのは、libvirt が state 属性の無い iptables 層の
ルールを逆方向(VM 宛て)にも展開するためでもある。state を明示すると逆方向のルールが
作られず、egress の drop が inbound に影響しない。

作られた filter は `virsh nwfilter-dumpxml minivps-<name>` で確認できる。

## よくある構成

### LAN とホストへは出さず、インターネットには出す

エージェント用の VM の基本形。全体は [`examples/agent-home.yaml`](../examples/agent-home.yaml)。

```yaml
egress:
  - {cidr: 192.168.122.1/32, protocol: udp, port: 53}
  - {cidr: 192.168.122.1/32, protocol: tcp, port: 53}
  - {action: drop, cidr: 10.0.0.0/8}
  - {action: drop, cidr: 172.16.0.0/12}
  - {action: drop, cidr: 192.168.0.0/16}
  - {action: drop, cidr: 100.64.0.0/10}
  - {action: drop, cidr: 169.254.0.0/16}
  - {cidr: 0.0.0.0/0}
```

DNS の accept を drop より前に書くのが要点。後ろに書くと `192.168.0.0/16` の drop に
先に当たる。

### 決まった宛先だけに出す(許可リスト)

```yaml
egress:
  - {cidr: 192.168.122.1/32, protocol: udp, port: 53}
  - {cidr: 203.0.113.10/32, protocol: tcp, port: 443}   # 例: 社外の API ゲートウェイ
```

最後に `0.0.0.0/0` を書かなければ、それ以外はすべて既定 drop に落ちる。

### 外向きを完全に止める

```yaml
egress: []
```

DHCP と inbound で受けた接続への戻りだけが通る。SSH で入って作業することはできるが、
VM からは名前解決もできない。

## 注意

### DNS を許可し忘れると名前解決できない

libvirt の NAT ネットワークでは、ゲストのリゾルバはネットワークのゲートウェイ
(default なら `192.168.122.1`)で動く dnsmasq になる。これはホスト自身への通信なので、
プライベートアドレスを drop すると DNS も止まる。症状は「IP 直打ちなら繋がるのに
ホスト名では繋がらない」。DNS(UDP 53、必要なら TCP 53)の accept を drop より前に書く。
静的 IP の NIC で `nameservers` を指定している場合は、その宛先を許可する。

### cloud-init の packages にも外向き通信が要る

minivps は初回起動時に cloud-init の `packages` で `qemu-guest-agent` を導入する
(exec やゲストからの IP 取得に使う)。これには DNS と、パッケージミラー
(Ubuntu なら `archive.ubuntu.com` などへの HTTP/HTTPS)への外向き通信が要る。
許可リスト型の egress で作成すると導入に失敗し、guest agent を使う機能が動かない。
その場合は、最初はミラーへの通信を許して作成し、導入後に egress を絞る
(稼働中に反映できる。次節)。`startup_script` のテンプレートが外部から何かを
取得する場合も同じ。

### 確立済みの接続は egress を絞っても切れない

戻り通信のために `ESTABLISHED,RELATED` を egress ルールより先に accept しているため、
egress を絞る前に確立していた接続は、絞った後も conntrack のエントリが残る間は通り続ける。
今まさに持ち出している接続を止めたい場合は、VM を一時停止(pause)または停止するか、
ホストで `conntrack -D -s <VM の IP>` を実行して conntrack のエントリを消す。

### ホスト自身の公開 IP

VM からホストの公開 IP(またはプライベートアドレス以外のホストのアドレス)への通信は、
ホスト宛て(INPUT)として `0.0.0.0/0` の accept に一致する。ホスト上のサービスを
守りたい場合は、そのアドレスの drop を `0.0.0.0/0` より前に書く。

### IPv6

egress を指定すると IPv6 の送信はすべて drop する(ルールで IPv6 を許可する手段は無い)。
libvirt の default ネットワークは IPv6 を配らないため、通常の構成では影響しない。

### action は drop のみ(reject は無い)

遮断した接続はタイムアウトまで待つ(ICMP で即座に拒否を返さない)。エージェントから
見ると「応答が無い」になる。

## 稼働中の反映

`filters` / `egress` の変更は、VM を止めずに `create`(`PUT /servers/{name}`)で反映できる。
反映の方式は `planning.field_apply_mode` が決める。

| 変更 | 反映方式 | 実際の操作 |
|---|---|---|
| ルールの中身だけ(filter の有無は変わらない) | 稼働中に反映 | 同名の nwfilter を `nwfilterDefineXML` で再定義する。libvirt はその filter を参照する稼働中の VM のルールも更新する |
| filter の有無が変わる(例: 両方 `null` の VM に `egress` を足す、`filters` だけの VM から `filters` を外す) | 稼働中に反映(`FILTER_ATTACH_APPLY_MODE`) | domain 定義(次回起動時)の filterref を差分編集して `defineXML` し、稼働中の interface にも `updateDeviceFlags(AFFECT_LIVE)` で filterref を付け外しする |

`filters` を外しても `egress` が残っていれば filter は要るままなので、前者(再定義だけ)に
なる。

filterref の付け外しは、libvirt の QEMU ドライバが `updateDeviceFlags` で渡された
interface の filterref の変化を検知し、旧ルールを外して新ルールを当てる処理
(`qemuDomainChangeNet` → `qemuDomainChangeNetFilter`。`type='network'` / `bridge` /
`ethernet` の interface が対象)に乗っている。渡す interface XML は稼働中の定義
(`XMLDesc(0)`)から取り、filterref 以外(target dev・alias・PCI address など)は
そのまま往復させる(`resources.live_filterref_updates`。`virsh domif-setlink` と同じ手法)。
filterref 以外が変わっていると libvirt は稼働中の変更を拒否するため。

実環境でこの付け外しに問題が出た場合は、`planning.FILTER_ATTACH_APPLY_MODE` を
`ApplyMode.OFFLINE` にすると、稼働中の付け外しは `ServerRunning` で拒否され、
停止中の VM への差分編集だけが使われる(ルールの中身の変更は稼働中のまま反映できる)。

稼働中に収束させたときは、spec の metadata を live と config の両方に書く。libvirt の
metadata は稼働中なら live 側が読まれるため、config だけに書くと `get` が古い spec を
返し、次の `create` が同じ差分を何度も収束させてしまう。

## ドメイン名単位の許可を見送った理由

「`api.example.com` だけ許可」のようなドメイン名での指定はしない。nwfilter(iptables /
ebtables)は IP アドレスでしか判定できず、ドメイン名を扱うにはホスト側に DNS の応答を
見て許可する IP を動的に足す仕組み(透過プロキシや DNS と連動する ipset)が要る。
CDN の背後のサービスは IP が頻繁に変わり、同じ IP を多数のドメインが共有するため、
IP に展開しても「そのドメインだけ」にはならない。仕組みの大きさに比べて境界として
信用できる度合いが低いため、A5 は IP / CIDR 単位に留める。ドメイン名で絞りたい場合は、
egress をプロキシ(ホストや別 VM の HTTP プロキシ)の IP だけに許可し、プロキシ側で
ドメインを絞る構成を推奨する。

## macOS

macOS は QEMU の user-mode ネットワークで動き、nwfilter が無い。egress を実現できない
ため、`egress` を指定すると `PlatformUnsupported` で拒否する(黙って無視しない。
守られているつもりで VM を使わせないため)。判定は `planning.check_platform`。
