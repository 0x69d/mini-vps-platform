# DNSレコード自動登録(opt-in・ベストエフォート)

`create()` / `delete()` / `reinstall()` の成功後に、VM の A レコードと PTR レコードを
内部DNS([minivps-dns-appliance](https://github.com/0x69d/minivps-dns-appliance) の dns-1)へ
`nsupdate` で自動登録・削除する機能。実装は `mini_vps/dns_registration.py`。

## 有効化手順

以下の3つの環境変数が**すべて**設定されているときのみ有効になる。1つでも欠ければ
機能全体が完全に無効(既存挙動と同一)になり、nsupdate は一切呼ばれない。

| 環境変数 | 内容 | 例 |
|---|---|---|
| `MINIVPS_DNS_SERVER` | dns-1 の IP(nsupdate の送信先) | `192.168.122.30` |
| `MINIVPS_DNS_ZONE` | 正引きゾーン名 | `minivps.internal` |
| `MINIVPS_DNS_TSIG_KEY_FILE` | TSIG 鍵ファイルのパス | `~/.config/minivps/dns-tsig.key` |

```bash
export MINIVPS_DNS_SERVER=192.168.122.30
export MINIVPS_DNS_ZONE=minivps.internal
export MINIVPS_DNS_TSIG_KEY_FILE=~/.config/minivps/dns-tsig.key
uv run mini-vps create specs/web-1.yaml   # 直後から dig で A/PTR が引ける
```

TSIG 鍵の生成・配置・ホストへの持ち出しは minivps-dns-appliance の README
「TSIG鍵の初期化」を参照。**鍵はパス参照のみ**で、mini-vps-platform は鍵ファイルを
開かない。鍵の中身は spec・libvirt metadata・ログ・例外メッセージのいずれにも
現れない(secrets 分離原則の適用)。ホスト側には `nsupdate`(`bind9-dnsutils`
パッケージ)が必要。

## 動作

- `create()` 成功後(新規作成時のみ): VM の**最初の静的NIC**の IP で
  `<name>.<zone>` の A と対応する PTR を登録する。この規約は `mini-vps status` が
  表示する管理IP(`_static_ipv4()`)と一致させている。静的NICが1つも無い VM は
  スキップし、その旨をログに残す(DHCP 割当 VM のリゾルバ切替と同様、DHCP VM は
  DNS 統合の対象外)。
- `delete()` 成功後: A と PTR を削除する。
- `reinstall()` 成功後: 再登録する(spec は不変なので同値の上書きになるが、
  DNS 有効化前に作った VM のレコードを後追い補充する復旧手段を兼ねる)。
- 登録は `update delete` → `update add` の組で書くため**冪等**(再実行・reinstall で
  重複しない)。A(正引きゾーン)と PTR(in-addr.arpa)は別ゾーンのため、
  RFC 2136(動的更新は1メッセージ=1ゾーン)に従い `send` を2回に分ける。
- タイムアウト: `nsupdate -t 5`(リクエスト単位)+ subprocess の 15 秒
  (ハードリミット)の二段構え。dns-1 が無応答でも create/delete が長時間
  ブロックしない。

## 設計判断

### 依存方向の逆転を opt-in + ベストエフォートで無害化する

本機能は、制御プレーン(mini-vps-platform)が自身の上で動くVM(dns-1)に依存する
という**レイヤの逆転**を含む。通常この向きの依存は避けるべきだが、以下の2点で
無害化している:

1. **opt-in**: 3環境変数が揃わない限り完全に不活性で、コードパスとして存在しない
   のと同等になる。dns-1 を使わない構成には一切影響しない。
2. **ベストエフォート**: `register()` / `unregister()` は例外を一切送出しない契約
   とし、nsupdate の失敗(タイムアウト・到達不能・SERVFAIL・コマンド不在)は
   警告ログのみで VM 操作自体は成功として完了する。

### ベストエフォートを選んだ理由

DNS 登録の失敗で create/delete を失敗させると、**dns-1 自身が壊れたとき dns-1 を
再作成する操作も失敗する**という循環依存のデッドロックに陥る。制御プレーンの
可用性は名前解決の即時整合よりも優先する。登録は冪等な delete→add の組なので、
取りこぼしたレコードは後から `mini-vps reinstall <name>` で補充できる
(自己修復可能)。

### nsupdate subprocess を選んだ理由

dnspython 等のライブラリ依存を増やさず、`cloud-localds` を subprocess で呼ぶ
`resources.build_seed_iso()` と同じ構図に揃えた。TSIG 署名や RFC 2136 の
プロトコル実装は BIND 純正ツールに委譲され、自前実装を持たない。
`ServerManager` からは `register` / `unregister` の2関数だけを呼び、依存方向は
manager → dns_registration の一方向に保つ。

### ログに logging を使う理由

本リポジトリの CLI 入口層は `print()` を使うが、manager 層は CLI / API /
exporter の3入口から共有されるライブラリ層のため、`dns_registration.py` に限り
`logging` を使う。logging 未設定の CLI でも標準の last-resort ハンドラが
WARNING 以上を stderr へ出すため体験は print と同等で、API(uvicorn)配下では
ログ基盤に統合・抑制できる。

## 制限

- A 成功・PTR 失敗のような**部分失敗**がありうる(send を2分割するため)。
  警告ログで検知し、`reinstall` で再登録して回復する。
- dns-1 が逆引きゾーンを持たないサブネットの IP は PTR 側のみ失敗し警告になる
  (A は登録される)。
- 収束(`create()` の可変フィールド差分適用)では再登録しない(networks は
  不変フィールドで IP が変わる余地が無いため)。
- `update delete <name> A` は同名の**すべての** A レコードを消す。同名に手動で
  複数 A を持たせる運用とは併用できない。
