# mini-vps-platform

QEMU/KVM + libvirt + Python で構築する、VPS サービスの最小版。

宣言的な YAML 入力を受け取り、ローカルマシン上に仮想サーバーをプロビジョニングする。
クラウドでいう「コントロールプレーン」の中核——宣言的入力からリソース確保までの翻訳——を自作する。

## 目的・前提

- ユーザーが宣言した「欲しいサーバー」を、libvirt の domain として実体化する。
- CLI(YAML)と Web API(JSON)の2つの入口から同じ操作を提供する。
- 単一ホスト上でローカル完結させる。

## スコープ

### 含むもの（最小構成）

- `Server` リソース: YAML / JSON 定義から libvirt domain を生成・起動・停止・削除する。
- NAT ネットワーク: libvirt の仮想ブリッジ経由でゲストを外向き通信させる。
- セグメント分離: 複数の独立 NAT ネットワークで VM を隔離する。
- パケットフィルタ: `filters` で宣言した inbound ポートのみ許可する
  (作成後に `vm-spec.yaml` を編集して再度 `create`/`PUT` することで変更できる)。
- 静的IP割当: `networks` の要素に `NetworkAttachment`(`name`/`address`/`gateway`)を
  指定すると、cloud-init の `network-config` 経由で固定IPを割り当てる
  ([静的IP割当](#静的ip割当)参照)。
- 監視: Prometheus + Grafana によってメトリクスを可視化する。

### 含まないもの

- 複数物理ホストへのスケジューリング。
- マルチテナンシー、課金、認証などの大規模運用機構。
- パケットフィルタの IPv6・egress・稼働中 VM へのライブ反映(ルール変更は停止中の VM に
  限り inbound・IPv4 のみ対応、反映は次回起動時から)。
- アラート通知(Alertmanager 等)。
- MAC アドレスのユーザー明示指定(`(name, index)` から内部で決定的に自動生成する。
  [静的IP割当](#静的ip割当)参照)。
- `status`/`get` の IP アドレス表示は、複数 NIC の VM でも最初に見つかった1件のみ
  (全 NIC の IP 一覧表示は非対応。静的アドレスを持つ NIC があれば起動状態に関わらず
  それを優先し、無ければ起動中の DHCP リースを表示する)。
- `networks`・`static_routes` は `create()` の可変フィールドではない
  (`startup_script` と同様、変更するには対象 VM の削除・再作成が必要)。
- 静的アドレスと Ansible 側 DHCP レンジ(`.2`〜`.254`、セグメント全域)の衝突回避
  (dnsmasq の ICMP 到達確認である程度は緩和されるが、起動順序次第で衝突しうる。
  レンジを狭める調整は本プロジェクトのスコープ外)。

## アーキテクチャ

```
spec.yaml / spec.json  →  parse  →  内部データ構造  →  XML 生成  →  libvirt define / start
```

- **入力**: YAML / JSON。domain XML は手書きせず、変換層で生成する。
- **ネットワーク**: NAT。ゲストはセグメントごとに独立した仮想ブリッジに接続し、ホストの NAT 経由で外に出る。
- **実体**: 各 VM は libvirt domain に対応する。

## 最小 YAML スキーマ

```yaml
name: web-1
memory: 1024                  # MB
vcpus: 2
base_image: ubuntu-26.04.img
disk: 10                      # GB
```

| キー | 型 | 必須/任意 | デフォルト |
|---|---|---|---|
| `name` | str（英数字・`-`・`_`、先頭は英数字、63文字以内） | CLI（YAML）では必須。API（JSON）では URL パスから与える | — |
| `memory` | int (MB, 正の整数) | 必須 | — |
| `vcpus` | int (正の整数) | 必須 | — |
| `base_image` | str | 必須 | — |
| `disk` | int (GB, 正の整数) | 必須 | — |
| `hostname` | str（`name` と同じ文字種制約） | 任意 | 未指定なら `name` で補完 |
| `user` | str（小文字・数字・`-`・`_`、先頭は小文字かアンダースコア、32文字以内） | 任意 | `ubuntu` |
| `networks` | list[str \| NetworkAttachment]（各要素は文字列(DHCP)か `NetworkAttachment`(静的IP)、1件以上、ネットワーク名の重複不可)。Ansible で事前定義済みのネットワーク名(`default`・`seg1`〜`seg3`)を指定する。未定義名を指定すると作成時に libvirt エラーになる。複数指定すると VM に複数 NIC が付く | 任意 | `["default"]` |
| `filters` | list[FilterRule] \| null | 任意 | 未指定(null)なら全 inbound 許可。`[]` を明示すると全 inbound 拒否 |
| `static_routes` | list[StaticRoute] | 任意 | 未指定なら追加ルート無し([スタティックルート](#スタティックルート)参照) |
| `startup_script` | str \| null | 任意 | 未指定(null)。指定する場合は既知のテンプレート名のみ許可([docs/startup-scripts.md](docs/startup-scripts.md) 参照) |

`FilterRule`: `{port: int(1-65535), protocol: "tcp" \| "udp"}` の inbound 許可ルール1件。

`StaticRoute`: `{destination: str(CIDR), via: str(IPv4アドレス)}` のスタティックルート1件。

`NetworkAttachment`: `{name: str, address: str(CIDR、ホストアドレス), gateway: str(IPv4アドレス) \| null}`
の静的IP割当1件([静的IP割当](#静的ip割当)参照)。`gateway` は任意で、省略時はそのNICに
デフォルトルートを追加しない。

> **警告**: `filters` を1件でも宣言すると、明示したポート以外の inbound は SSH(22番)を含めて
> すべて拒否される。SSH アクセスを維持したい場合は `{port: 22, protocol: "tcp"}` を
> 自分で `filters` に含める必要がある(暗黙の許可は無い)。

## ネットワークセグメント

VM を互いに隔離するための独立 NAT ネットワーク。Ansible playbook が以下を事前定義する
(定義は `ansible/vars/network_segments.yml`)。セグメントのサブネットは
「192.168.(200+セグメント番号).0/24」の規則で、名前から即座に読み取れる。

| name | bridge | サブネット | DHCP レンジ |
|---|---|---|---|
| `default` | virbr0 | 192.168.122.0/24 | .2〜.254 |
| `seg1` | virbr-seg1 | 192.168.201.0/24 | .2〜.254 |
| `seg2` | virbr-seg2 | 192.168.202.0/24 | .2〜.254 |
| `seg3` | virbr-seg3 | 192.168.203.0/24 | .2〜.254 |

`default` とセグメントの違いは管理元と役割のみ。`default` はディストリ同梱 XML から
定義される libvirt 標準ネットワークで、spec で `networks` 未指定時の受け皿(汎用)。
`seg1`〜`seg3` は本プロジェクトが vars で管理する、分離を明示的に意図した配置先。
遮断の機構は共通で、`default` も各セグメントから見れば相互遮断されたネットワークの
1つとして振る舞う。

VM の所属セグメントは spec の `networks` で指定する。

```yaml
name: web-1
memory: 1024
vcpus: 2
base_image: ubuntu-26.04.img
disk: 10
networks: [seg1]
```

`networks` に複数のネットワーク名を指定すると、VM に NIC が複数付き、それぞれの
セグメントに同時所属できる。

```yaml
name: router-1
memory: 1024
vcpus: 2
base_image: ubuntu-26.04.img
disk: 10
networks: [seg1, seg2]
```

**ポリシー**: 同一セグメント内の VM は自由に通信できる。セグメント間は相互遮断され、
各セグメントから外向き(インターネット方向)の通信は NAT 経由で許可される。

この遮断に追加のファイアウォール設定は不要である。libvirt は NAT ネットワークの起動時に
ネットワーク単位の FORWARD ルール(iptables backend では `LIBVIRT_FWI`/`LIBVIRT_FWO`
チェーン)を自動投入し、別ブリッジ宛の新規パケットは宛先ネットワーク側の REJECT に当たる
ため、独立 NAT ネットワークに分けた時点でセグメント間通信は遮断される。

> **注意**: ホスト側で FORWARD チェーンの `LIBVIRT_*` より前に広範な ACCEPT ルールを
> 手動追加すると、この遮断は崩れる。

セグメントを追加する場合は `ansible/vars/network_segments.yml` に1エントリ追記して
playbook を再実行する(サブネットとブリッジ名は既存と重複させないこと)。

## 静的IP割当

`networks` の要素にネットワーク名の文字列ではなく `NetworkAttachment` オブジェクトを
指定すると、そのNICに固定IPを割り当てる。DHCPの文字列要素と混在できる。

```yaml
name: router-1
memory: 1024
vcpus: 2
base_image: ubuntu-26.04.img
disk: 10
networks:
  - default
  - name: seg1
    address: 192.168.201.10/24
  - name: seg2
    address: 192.168.202.10/24
    gateway: 192.168.202.1
```

**仕組み**: `create()` は VM名とNICインデックスから決定的にMACアドレスを生成し
(`52:54:00` プレフィックス)、domain XML の各 `<interface>` に埋め込む。静的IPを持つ
NICが1つでもあれば、cloud-init の `network-config`(v2形式)を生成し
`cloud-localds -N` で seed ISO に組み込む。`network-config` を渡すとそれが唯一の
設定源になるため、DHCPの文字列要素も含めて全NICをMACマッチで列挙する(記載の無い
NICは cloud-init から一切設定されなくなるため)。静的IPを1つも持たないVMでは
`network-config` 自体を生成せず、cloud-localds の呼び出しも変わらない。

`gateway` を指定すると、そのNICに `routes: [{to: default, via: gateway}]` として
デフォルトルートを追加する。省略するとそのNICにはルートを追加しない
(`static_routes` の `via` と同様、運用者が必要な時だけ決め打ちで指定する設計)。
`address`/`gateway` 間のサブネット整合性は検証しない。

**`status`/`get` のIP表示**: 静的アドレスを持つNICが1つでもあれば、VMの起動状態に
関わらずそれを(宣言値として)優先表示する。cloud-initが実際に適用したかは確認しない。
静的アドレスが無ければ従来通り起動中のみDHCPリースを表示する。いずれの場合も複数NIC
中の最初の1件のみ。

**既知の制約**: Ansible が定義するDHCPレンジ(`.2`〜`.254`、セグメント全域)は静的
アドレスの割当範囲と重複しうる。dnsmasqのICMP到達確認である程度は緩和されるが、
起動順序次第では衝突する可能性がある。レンジを狭める調整は本プロジェクトのスコープ外。

## スタートアップスクリプト

名前付き cloud-init テンプレートで VM の初期セットアップを自動化する機能。
`startup_script` に既知のテンプレート名を指定すると、VM 初回起動時の cloud-init
(`write_files`/`runcmd`)としてテンプレートの内容が適用される。対応テンプレート・
secrets の渡し方・トラブルシューティングは [docs/startup-scripts.md](docs/startup-scripts.md)
を参照。

## スタティックルート

ゲストに追加のスタティックルートを注入する機能。`static_routes` に宛先ネットワーク
(`destination`)と次ホップ(`via`)の組を指定すると、VM 初回起動時に systemd の
oneshot ユニット(`minivps-static-routes.service`)として登録される。

```yaml
name: web-1
memory: 1024
vcpus: 2
base_image: ubuntu-26.04.img
disk: 10
networks: [seg1]
static_routes:
  - destination: 192.168.202.0/24
    via: 192.168.201.1
```

`via` はセグメント内の到達可能な IP を運用者が決め打ちで指定する値であり、`static_routes`
自体はそれを検証しない。次ホップ側(例: ルータVM)のIPを安定させたい場合は
[静的IP割当](#静的ip割当)を参照。

**永続化の仕組み**: cloud-init の `runcmd` は初回起動時にしか実行されないため、単純な
`ip route add` では VM 再起動後にルートが消える。そのため `ip route replace` を
`ExecStart` に持つ systemd oneshot ユニットを書き込み、`systemctl enable --now` で
有効化する。`enable` により次回以降の起動でも自動的に再適用され、これが再起動をまたぐ
永続化の実体になる。

**トラブルシューティング**: `ExecStart` の各行は先頭に `-` を付けており、1つの経路が
`via` 未到達で失敗しても他の経路の適用を妨げない。この `-` はエラーを握りつぶすため、
失敗はユニット全体のステータスには現れない。適用結果を確認するには、ゲスト内で以下を
実行する。

```bash
journalctl -u minivps-static-routes.service
ip route show
```

## 必要環境

- Linux（KVM 対応 CPU、`/dev/kvm` 利用可）
- QEMU/KVM, libvirt デーモン
- [uv](https://docs.astral.sh/uv/)
- ビルド依存（libvirt-python は PyPI で sdist のみ提供のため、`uv add` 時にソースビルドが走る）: libvirt の開発ヘッダ + Python 開発ヘッダ（`Python.h`）+ pkg-config + C コンパイラ

### 動作確認済みホスト OS

- Ubuntu 26.04 LTS
- Fedora Linux 44

## セットアップ

### 1. ホスト側の事前設定(Ansible)

パッケージ導入(apt/dnf)・libvirtd の起動と自動起動・実行ユーザーの `libvirt`
グループ追加・default ネットワーク・セグメント NAT ネットワーク(`seg1`〜`seg3`)・
`images` ストレージプール・base image・seed ISO 置き場(`/var/lib/libvirt/seeds`)・
SSH 鍵まで、Ansible playbook で一括セットアップする。

```bash
uv sync --only-group ops
uv run --only-group ops ansible-playbook -i ansible/inventory.ini ansible/playbook.yml --ask-become-pass
```

> **警告**: `sudo ansible-playbook ...` のように実行コマンド自体を sudo しないこと。
> その場合 playbook 内で実行ユーザーが root と誤認識され、seed ISO 置き場や SSH 鍵の
> 所有者が root になり、後続の VM 作成が壊れる。root 権限が必要な個々のタスクは
> playbook 内の `become: true` で昇格するため、パスワードレス sudo でなければ
> `--ask-become-pass` を付ければ十分。

> **注記**: `sudo` の既定実装が Rust 版(`sudo-rs`)のホストでは、`-p`/`--prompt` の
> 扱いの違いにより Ansible の become パスワードプロンプト検出が失敗し、
> `Timed out waiting for become success or become password prompt` で
> playbook が止まる場合がある([ansible#85837](https://github.com/ansible/ansible/issues/85837)、
> [sudo-rs#1461](https://github.com/trifectatechfoundation/sudo-rs/issues/1461)、
> 修正は将来の ansible-core リリースに追従予定)。発生した場合は GNU 版 sudo に
> 切り替えると回避できる(`sudo update-alternatives --auto sudo` で元に戻せる)。
>
> ```bash
> sudo update-alternatives --set sudo /usr/bin/sudo.ws
> ```

対応可能なゲスト OS と base image の入手・登録手順は
[docs/guest-os.md](docs/guest-os.md) を参照。

`libvirt` グループの反映にはシェルの再ログイン(または `newgrp libvirt`)が必要。

### 2. Python 依存の同期（uv）

依存は `pyproject.toml` / `uv.lock` で管理している。

```bash
uv sync
```

`libvirt-python` のバージョンは、実行環境の libvirt と同じかそれ以下に揃える（新しいバインディングを古い `.so` に当てると実行時にシンボル不足になる）。手元のバージョンは `virsh --version` で確認する。

### 3. CLI(YAML)

宣言的 YAML を渡して VM を操作する。`uv run mini-vps` または `uv run python -m mini_vps` のどちらでも同じ CLI が起動する。

```bash
uv run mini-vps create mini_vps/vm-spec.yaml
uv run mini-vps list
uv run mini-vps get web-1
uv run mini-vps status web-1
uv run mini-vps start web-1
uv run mini-vps stop web-1
uv run mini-vps restart web-1
uv run mini-vps reinstall web-1
uv run mini-vps delete web-1
```

| サブコマンド | 説明 |
|---|---|
| `create <file>` | spec YAML から VM を宣言的に作成・収束する |
| `get <name>` | spec と状態を表示する(不在なら終了コード 2) |
| `list` | 管理対象の VM 名を1行ずつ表示する |
| `status <name>` | 状態(state・ip)を表示する(不在なら終了コード 2) |
| `start <name>` | VM を起動する(起動中なら冪等に no-op、不在なら終了コード 2) |
| `stop <name> [--force]` | VM を停止する(停止中なら冪等に no-op、不在なら終了コード 2) |
| `restart <name> [--force]` | disk を保持したまま VM を再起動する(不在なら終了コード 2) |
| `delete <name>` | VM を削除する(不在/管理外なら終了コード 2) |
| `reinstall <name>` | disk を base から作り直して再起動する(不在なら終了コード 2) |

`create`/`reinstall` はどちらも `--startup-param KEY=VALUE`(複数回指定可)を
受け付ける。`startup_script` テンプレートに渡す秘密情報の指定方法は
[docs/startup-scripts.md](docs/startup-scripts.md) を参照。

`stop`/`restart` の既定はゲスト OS への ACPI 経由の正常なシャットダウン/再起動の
要求のみで、実際に状態が変わるまで待たない。`--force` 指定時は即座に強制する。
停止中の VM に `restart`(force 無し)を実行すると終了コード 4(`ServerNotRunning`)
で拒否する。

`create` を既存 VM に対して再実行すると、`memory`/`vcpus`/`filters` の差分のみ
収束させる(それ以外のフィールドの差分は spec 相違として終了コード 3
(`ServerConflict`)で拒否する)。収束はドメイン停止中の VM のみ許可し、
稼働中に実行すると終了コード 5(`ServerRunning`)で拒否する(先に `stop` してから
再実行する)。

### 4. Web API(JSON)

他サービス向けの入口。宣言的 YAML は CLI 向け、API は JSON で分離する。

```bash
uv run uvicorn mini_vps.api:app
```

OpenAPI ドキュメントは <http://127.0.0.1:8000/docs> で確認できる。

| メソッド | パス | 説明 |
|---|---|---|
| `GET` | `/servers` | 管理対象の VM 名一覧 |
| `GET` | `/servers/{name}` | spec と状態(不在なら 404) |
| `GET` | `/servers/{name}/status` | 状態 state・ip(不在なら 404) |
| `PUT` | `/servers/{name}` | 宣言的な作成/収束(新規 201・完全一致の no-op/収束 200・spec 相違 409) |
| `POST` | `/servers/{name}/start` | VM を起動する(起動中なら冪等に no-op、不在/管理外 404) |
| `POST` | `/servers/{name}/stop` | VM を停止する(停止中なら冪等に no-op、不在/管理外 404) |
| `POST` | `/servers/{name}/restart` | disk を保持したまま VM を再起動する(不在/管理外 404) |
| `DELETE` | `/servers/{name}` | 削除(成功 204・不在/管理外 404) |
| `POST` | `/servers/{name}/reinstall` | disk を base から作り直して再起動(不在/管理外 404) |

`PUT`/`POST .../reinstall` の JSON body には、`startup_script` テンプレートに渡す
秘密情報として `secrets` フィールドを追加で渡せる。詳細は
[docs/startup-scripts.md](docs/startup-scripts.md) を参照。

`stop`/`restart` の既定動作は CLI の `stop`/`restart`(上記参照)と同じ。強制は
JSON body に `{"force": true}` を渡し、状態変化は `GET /servers/{name}/status`
でポーリングして確認する。停止中の VM への `restart`(force 無し)は CLI 同様
拒否され、API では 409(`ServerNotRunning`)で返る。

収束の挙動は CLI の `create`(上記参照)と同じ。API では `ServerConflict`・
`ServerRunning` のどちらも 409 で返る。

### 5. Prometheus エクスポーター

管理対象 VM の CPU・メモリ・ネットワーク・ディスク I/O・起動状態を Prometheus 形式で公開する。
Web API とは別の独立プロセスとして動く。

```bash
uv run python -m mini_vps.exporter
```

既定では `127.0.0.1:9177/metrics` で待ち受ける(同一ホスト上で動く Prometheus サーバーからの
スクレイプを想定。単一ホスト上でローカル完結させるという本プロジェクトの前提に合わせている)。
`MINIVPS_EXPORTER_PORT`・`MINIVPS_EXPORTER_ADDR` 環境変数でポート・待受アドレスを変更できる。
認証機構は無いため、待受アドレスを変更して外部公開する場合はファイアウォール等で
アクセス元を制限すること。

メトリクスの可視化(Prometheus + Grafana)は `### 6. Prometheus + Grafana(docker-compose)` を参照。

### 6. Prometheus + Grafana(docker-compose)

`### 5.` の exporter が公開するメトリクスを Prometheus でスクレイプし、Grafana で
可視化する。Docker(docker compose v2 プラグイン込み)が導入済みであること、
`### 5.` の exporter が `127.0.0.1:9177` で起動済みであることが前提。

```bash
uv run python -m mini_vps.exporter &   # 別ターミナルで起動していれば不要
cp .env.example .env                   # GF_SECURITY_ADMIN_PASSWORD を書き換えること
docker compose up -d
```

- Prometheus UI: <http://127.0.0.1:9090>
- Grafana: <http://127.0.0.1:3000>(ログイン情報は `.env` の
  `GF_SECURITY_ADMIN_USER`/`GF_SECURITY_ADMIN_PASSWORD`)。ログイン後、
  「mini-vps-platform」フォルダの「mini-vps-platform Overview」ダッシュボードで
  VM ごとの CPU・メモリ・ネットワーク・ディスク I/O・起動状態を確認できる。

Prometheus・Grafana とも `network_mode: host` で動作し、`127.0.0.1` にのみ bind する
(exporter と同じく単一ホスト内で完結させ、外部には公開しない)。

停止する場合は `docker compose down`(データは named volume に残る)。データも含めて
完全に削除する場合は `docker compose down -v` を使う。
