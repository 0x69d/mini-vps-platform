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

### 含まないもの

- 複数物理ホストへのスケジューリング。
- マルチテナンシー、課金、認証、API 冪等性などの大規模運用機構。

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
memory: 4096   # MB
vcpus: 2
disk: 20       # GB
```

## 必要環境

- Linux（KVM 対応 CPU、`/dev/kvm` 利用可）
- QEMU/KVM, libvirt デーモン
- [uv](https://docs.astral.sh/uv/)
- ビルド依存（libvirt-python は PyPI で sdist のみ提供のため、`uv add` 時にソースビルドが走る）: libvirt の開発ヘッダ + pkg-config + C コンパイラ

## セットアップ

### 1. システムパッケージ

`cloud-localds`（cloud-image-utils / cloud-utils）と libvirt 開発ヘッダ（libvirt-python のビルドに必要）まで含める。

Debian / Ubuntu（apt）:

```bash
sudo apt install -y \
  libvirt-daemon-system libvirt-clients \
  qemu-system-x86 qemu-utils \
  cloud-image-utils \
  libvirt-dev pkg-config build-essential
```

Fedora / RHEL 系（dnf）:

```bash
sudo dnf install -y \
  libvirt libvirt-client \
  qemu-kvm qemu-img \
  cloud-utils \
  libvirt-devel pkgconf-pkg-config gcc
```

### 2. libvirt デーモンの起動と権限

```bash
sudo systemctl enable --now libvirtd
sudo usermod -aG libvirt "$USER"   # 反映には再ログイン
```

### 3. Python 依存（uv）

```bash
uv add pyyaml "libvirt-python==12.0.0" fastapi "uvicorn[standard]"
```

`libvirt-python` のバージョンは、実行環境の libvirt と同じかそれ以下に揃える（新しいバインディングを古い `.so` に当てると実行時にシンボル不足になる）。手元のバージョンは `virsh --version` で確認する。

### 4. 実行(デモ)

```bash
uv run python -m mini_vps
```

### 5. Web API(JSON)

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

`PUT` は VM を即時に定義・起動して返すが、ブートや DHCP は待たない。IP の確定は
`GET /servers/{name}/status` を `ip` が出るまでポーリングして観測する。

## ステータス

初期開発中。
