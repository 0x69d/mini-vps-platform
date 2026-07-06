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
- パケットフィルタ: `filters` で宣言した inbound ポートのみ許可する。
- 監視: Prometheus + Grafana によってメトリクスを可視化する。

### 含まないもの

- 複数物理ホストへのスケジューリング。
- マルチテナンシー、課金、認証などの大規模運用機構。
- パケットフィルタの IPv6・egress・動的なルール更新 API（inbound・IPv4・作成時適用のみ）。
- アラート通知(Alertmanager 等)。

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
| `network` | str（`name` と同じ文字種制約）。Ansible で事前定義済みのネットワーク名(`default`・`seg1`〜`seg3`)を指定する。未定義名を指定すると作成時に libvirt エラーになる | 任意 | `default` |
| `filters` | list[FilterRule] \| null | 任意 | 未指定(null)なら全 inbound 許可。`[]` を明示すると全 inbound 拒否 |
| `startup_script` | str \| null | 任意 | 未指定(null)。指定する場合は既知のテンプレート名のみ許可([docs/startup-scripts.md](docs/startup-scripts.md) 参照) |

`FilterRule`: `{port: int(1-65535), protocol: "tcp" \| "udp"}` の inbound 許可ルール1件。

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
定義される libvirt 標準ネットワークで、spec で `network` 未指定時の受け皿(汎用)。
`seg1`〜`seg3` は本プロジェクトが vars で管理する、分離を明示的に意図した配置先。
遮断の機構は共通で、`default` も各セグメントから見れば相互遮断されたネットワークの
1つとして振る舞う。

VM の所属セグメントは spec の `network` で指定する。

```yaml
name: web-1
memory: 1024
vcpus: 2
base_image: ubuntu-26.04.img
disk: 10
network: seg1
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

## スタートアップスクリプト

名前付き cloud-init テンプレートで VM の初期セットアップを自動化する機能。
`startup_script` に既知のテンプレート名を指定すると、VM 初回起動時の cloud-init
(`write_files`/`runcmd`)としてテンプレートの内容が適用される。対応テンプレート・
secrets の渡し方・トラブルシューティングは [docs/startup-scripts.md](docs/startup-scripts.md)
を参照。

## 必要環境

- Linux（KVM 対応 CPU、`/dev/kvm` 利用可）
- QEMU/KVM, libvirt デーモン
- [uv](https://docs.astral.sh/uv/)
- ビルド依存（libvirt-python は PyPI で sdist のみ提供のため、`uv add` 時にソースビルドが走る）: libvirt の開発ヘッダ + Python 開発ヘッダ（`Python.h`）+ pkg-config + C コンパイラ

## セットアップ

### 1. ホスト側の事前設定(Ansible)

パッケージ導入(apt/dnf)・libvirtd の起動と自動起動・実行ユーザーの `libvirt`
グループ追加・default ネットワーク・セグメント NAT ネットワーク(`seg1`〜`seg3`)・
`images` ストレージプール・base image・seed ISO 置き場(`/var/lib/libvirt/seeds`)・
SSH 鍵まで、Ansible playbook で一括セットアップする。

```bash
uv sync --group ops
uv run ansible-playbook -i ansible/inventory.ini ansible/playbook.yml --ask-become-pass
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
| `create <file>` | spec YAML から VM を宣言的・冪等に作成する |
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
| `PUT` | `/servers/{name}` | 宣言的・冪等な作成/収束(新規 201・冪等 200・spec 相違 409) |
| `POST` | `/servers/{name}/start` | VM を起動する(起動中なら冪等に no-op、不在/管理外 404) |
| `POST` | `/servers/{name}/stop` | VM を停止する(停止中なら冪等に no-op、不在/管理外 404) |
| `POST` | `/servers/{name}/restart` | disk を保持したまま VM を再起動する(不在/管理外 404) |
| `DELETE` | `/servers/{name}` | 削除(成功 204・不在/管理外 404) |
| `POST` | `/servers/{name}/reinstall` | disk を base から作り直して再起動(不在/管理外 404) |

`PUT`/`POST .../reinstall` の JSON body には、`startup_script` テンプレートに渡す
秘密情報として `secrets` フィールドを追加で渡せる。詳細は
[docs/startup-scripts.md](docs/startup-scripts.md) を参照。

`stop`/`restart` の既定はゲスト OS への ACPI 経由の正常なシャットダウン/再起動の
要求のみで、実際に状態が変わるまで待たない(`GET /servers/{name}/status` でポーリング
して確認する)。JSON body に `{"force": true}` を渡すと即座に強制する。停止中の VM に
`restart`(force 無し)を実行すると 409(`ServerNotRunning`)で拒否する。

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
