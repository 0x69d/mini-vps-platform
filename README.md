# mini-vps-platform

QEMU/KVM + libvirt + Python で構築する、VPS サービスの最小版。

宣言的な YAML 入力を受け取り、ローカルマシン上に仮想サーバーをプロビジョニングする。
クラウドでいう「コントロールプレーン」の中核——宣言的入力からリソース確保までの翻訳——を自作することを目的とする。

## 目的

- YAML で宣言した「欲しいサーバー」を、libvirt の domain として実体化する。
- 最終的に Web API として操作できる形まで持っていく。
- 単一ホスト上でローカル完結させる。実機 VPS との接続は前提としない。
- 並行制御は単一プロセス前提とし、同名への並行 create/delete は `ServerManager` の name 単位ロックで直列化する（複数 worker / プロセス間ロックは対象外）。

## スコープ

### 含むもの（最小構成）

- `Server` リソース: YAML 定義から libvirt domain を生成・起動・停止・削除する。
- YAML → domain XML への変換層（Python）。
- NAT ネットワーク: libvirt の仮想ブリッジ経由でゲストを外向き通信させる。
- パケットフィルタ: `filters` で宣言した inbound ポートのみ許可する(libvirt nwfilter)。
- 監視: Prometheus + Grafana によるメトリクス可視化(docker-compose、単一ホスト内完結、外部非公開)。

### 含まないもの

- 複数物理ホストへのスケジューリング。
- マルチテナンシー、課金、認証などの大規模運用機構。
- パケットフィルタの IPv6・egress・動的なルール更新 API（inbound・IPv4・作成時適用のみ）。
- アラート通知(Alertmanager 等)。可視化までが範囲。

## アーキテクチャ

```
spec.yaml  →  parse  →  内部データ構造  →  XML 生成  →  libvirt define / start
```

- **入力**: 人間に優しい YAML。domain XML は手書きせず、変換層で生成する。
- **ネットワーク**: NAT。ゲストは仮想ブリッジに接続し、ホストの NAT 経由で外に出る。外部からゲストへの直接到達は想定しない。
- **実体**: 各 `Server` は libvirt domain に対応する。

## 最小 YAML スキーマ

```yaml
name: web-1
memory: 1024                  # MB
vcpus: 2
base_image: ubuntu-24.04.img
disk: 10                      # GB
```

| キー | 型 | 必須/任意 | デフォルト |
|---|---|---|---|
| `name` | str | 必須（CLI/YAML）。API は URL パスから与える | — |
| `memory` | int (MB) | 必須 | — |
| `vcpus` | int | 必須 | — |
| `base_image` | str | 必須 | — |
| `disk` | int (GB) | 必須 | — |
| `hostname` | str | 任意 | 未指定なら `name` で補完 |
| `user` | str | 任意 | `ubuntu` |
| `network` | str | 任意 | `default` |
| `filters` | list[FilterRule] \| null | 任意 | 未指定(null)なら全 inbound 許可。`[]` を明示すると全 inbound 拒否 |
| `startup_script` | str \| null | 任意 | 未指定(null)。指定する場合は既知のテンプレート名のみ許可([docs/startup-scripts.md](docs/startup-scripts.md) 参照) |

`FilterRule`: `{port: int(1-65535), protocol: "tcp" \| "udp"}` の inbound 許可ルール1件。

> **警告**: `filters` を1件でも宣言すると、明示したポート以外の inbound は SSH(22番)を含めて
> すべて拒否される。SSH アクセスを維持したい場合は `{port: 22, protocol: "tcp"}` を
> 自分で `filters` に含める必要がある(暗黙の許可は無い)。

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

ゲスト VM の CPU はホストに合わせて選択する(`<cpu mode='host-model'/>`)。未設定だと
libvirt の既定 CPU モデル(`qemu64`)にフォールバックし、AVX 等の拡張命令が
ゲストに公開されず、それに依存するソフトウェアがクラッシュすることがある。

## セットアップ

### 1. ホスト側の事前設定(Ansible)

パッケージ導入(apt/dnf)・libvirtd の起動と自動起動・実行ユーザーの `libvirt`
グループ追加・default ネットワーク・`images` ストレージプール・base image・
seed ISO 置き場(`/var/lib/libvirt/seeds`)・SSH 鍵まで、Ansible playbook で
一括セットアップする。

```bash
uv sync --group ops
uv run ansible-playbook -i ansible/inventory.ini ansible/playbook.yml --ask-become-pass
```

> **警告**: `sudo ansible-playbook ...` のように実行コマンド自体を sudo しないこと。
> その場合 playbook 内で実行ユーザーが root と誤認識され、seed ISO 置き場や SSH 鍵の
> 所有者が root になり、後続の VM 作成が壊れる。root 権限が必要な個々のタスクは
> playbook 内の `become: true` で昇格するため、パスワードレス sudo でなければ
> `--ask-become-pass` を付ければ十分。

対応可能なゲスト OS と base image の入手・登録手順は
[docs/guest-os.md](docs/guest-os.md) を参照。

`libvirt` グループの反映にはシェルの再ログイン(または `newgrp libvirt`)が必要。

パケットフィルタ(nwfilter)はホスト側で ebtables/iptables/arptables ルールを操作するが、
その処理は root 権限で動く libvirtd デーモンが代行する。呼び出し側ユーザー自身が root で
ある必要はなく、`libvirt` グループ所属で足りる。

### 2. Python 依存（uv）

```bash
uv add pyyaml "libvirt-python==12.0.0" fastapi "uvicorn[standard]" typer
```

`libvirt-python` のバージョンは、実行環境の libvirt と同じかそれ以下に揃える（新しいバインディングを古い `.so` に当てると実行時にシンボル不足になる）。手元のバージョンは `virsh --version` で確認する。

### 3. CLI(YAML)

人間向けの入口。宣言的 YAML を渡して VM を操作する。`uv run mini-vps` または
`uv run python -m mini_vps` のどちらでも同じ CLI が起動する。

```bash
uv run mini-vps create mini_vps/vm-spec.yaml
uv run mini-vps list
uv run mini-vps get web-1
uv run mini-vps status web-1
uv run mini-vps reinstall web-1
uv run mini-vps delete web-1
```

| サブコマンド | 説明 |
|---|---|
| `create <file>` | spec YAML から VM を宣言的・冪等に作成する |
| `get <name>` | spec と状態を表示する(不在なら終了コード 2) |
| `list` | 管理対象の VM 名を1行ずつ表示する |
| `status <name>` | 状態(state・ip)を表示する(不在なら終了コード 2) |
| `delete <name>` | VM を削除する(不在/管理外なら終了コード 2) |
| `reinstall <name>` | disk を base から作り直して再起動する(不在なら終了コード 2) |

`create`/`reinstall` はどちらも `--startup-param KEY=VALUE`(複数回指定可)を
受け付ける。`startup_script` テンプレートに渡す秘密情報の指定方法は
[docs/startup-scripts.md](docs/startup-scripts.md) を参照。

`create` で spec が既存と相違、または管理外の同名 domain がある場合は終了コード 3
(`ServerConflict`)で拒否する。CLI は Web API と同じ `ServerManager` を呼ぶ薄い
フロントエンドで、どちらの入口を使っても操作結果は変わらない。

### 4. Web API(JSON)

機械(フロント・他サービス)向けの入口。宣言的 YAML は CLI 向け、API は JSON で分離する。

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
| `DELETE` | `/servers/{name}` | 削除(成功 204・不在/管理外 404) |
| `POST` | `/servers/{name}/reinstall` | disk を base から作り直して再起動(不在/管理外 404) |

`PUT` は VM を即時に定義・起動して返すが、ブートや DHCP は待たない。IP の確定は
`GET /servers/{name}/status` を `ip` が出るまでポーリングして観測する。`filters` を
指定した場合、nwfilter ルールは domain 定義に組み込む形で IP 確定を待たずに適用される。

`DELETE` は VM 本体に加え、その VM 専用の nwfilter ルールも同時に削除する(孤児ルールは残さない)。

`reinstall` は overlay volume のみを作り直すため spec・metadata・IP アドレス
(MAC アドレス)・nwfilter ルールは変わらない。別 base image への入れ替えは対象外。

`PUT`/`POST .../reinstall` の JSON body には、`startup_script` テンプレートに渡す
秘密情報として `secrets` フィールドを追加で渡せる。詳細は
[docs/startup-scripts.md](docs/startup-scripts.md) を参照。

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
(exporter と同じく単一ホスト内で完結させ、外部には公開しない)。動作確認は主に
ネイティブ Linux ホストを想定しており、WSL2 上では可能な範囲での確認に留まる。

停止する場合は `docker compose down`(データは named volume に残る)。データも含めて
完全に削除する場合は `docker compose down -v` を使う。

## ステータス

初期開発中。
