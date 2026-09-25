# macOS で使う

mini-vps は macOS(Apple Silicon / Intel)を標準でサポートする。Linux と同じ CLI・Web API・spec で
VM を扱えるが、ネットワークまわりの一部機能は Linux ホスト専用になる([Linux との機能差](#linux-との機能差))。

## 仕組み

プラットフォームの違いは `mini_vps/platform_profile.py` の `HostProfile` にまとめてある。macOS では
次のように動く。

| 項目 | macOS での動き |
|---|---|
| libvirt | Homebrew の libvirt。接続先はユーザーごとの `qemu:///session`(root 不要) |
| 仮想化 | Hypervisor.framework(domain type `hvf`)。CPU は `host-passthrough` |
| アーキテクチャ | Apple Silicon は aarch64(machine `virt`)、Intel は x86_64(machine `q35`) |
| ファームウェア | UEFI(`<os firmware='efi'>`)。Homebrew の qemu に同梱された edk2 を libvirt が自動選択する |
| ネットワーク | QEMU の user-mode ネットワーク。ゲストの 22 番をホストの `127.0.0.1:<port>` へ転送する |
| seed ISO | 純 Python(pycdlib)で生成するため `cloud-localds` は不要 |
| データ | `~/Library/Application Support/mini-vps/` の `vps-pool`・`seeds`・`images`・`locks` |

ネットワークは libvirt のネットワーク(NAT ブリッジ)も nwfilter も使わない。QEMU の `-netdev user` を
domain XML の `qemu:commandline` で直接渡し、SSH だけをループバックへ転送する。ゲストからは外へ
出られるが、ホストや LAN からゲストへは転送した SSH ポート以外では届かない。

## 前提

- macOS 15(Sequoia)以降を推奨。Apple Silicon の macOS 14 以前では、Homebrew の qemu が HVF 無しで
  ビルドされているため、ハードウェア仮想化を使えない(`MINIVPS_ACCEL=tcg` で低速に動かすことはできる)。
- [Homebrew](https://brew.sh)
- Xcode Command Line Tools(`xcode-select --install`)。`libvirt-python` を sdist からビルドするのに使う。
- [uv](https://docs.astral.sh/uv/)。Python 3.14 は uv が用意する。
- ディスクの空き数 GB(base image 約 600 MB + VM ごとの overlay)

ターミナルは Rosetta 2 ではなくネイティブ(arm64)で動かすこと。Rosetta 上では `uname -m` や Python の
`platform.machine()` が `x86_64` を返し、アーキテクチャを取り違える。

## セットアップ

```sh
git clone <this repository> && cd mini-vps-platform
scripts/macos-setup.sh
uv sync
```

`scripts/macos-setup.sh` は Linux の `ansible/playbook.yml` に当たる冪等なスクリプトで、何度実行してもよい。
行うことは次のとおり。

1. Homebrew・Xcode Command Line Tools・Hypervisor.framework(`sysctl kern.hv_support`)を確認する
2. `brew install libvirt qemu pkgconf`
3. `brew services start libvirt`(ログイン時に session デーモンを起動し、autostart の VM を戻す)
4. `virsh -c qemu:///session version` で疎通を確認する
5. データディレクトリを作り、base image 用の `images` プールを define / start / autostart する
6. アーキテクチャに合う Ubuntu 24.04 の cloud image(Apple Silicon は arm64 版)を
   `ubuntu-24.04.img` として取得する(SHA256 を検証し、読み取り専用 `0444` にする)
7. `~/.ssh/minivps_ed25519` を生成する(既にあれば使う)
8. UEFI firmware の記述子を確認し、`brew upgrade qemu` で壊れないパスに固定する
   ([後述](#brew-upgrade-qemu-のあと既存の-vm-が起動しない))
9. `virsh domcapabilities` で `hvf` と UEFI が libvirt から使えることを確かめる

最後に警告の一覧と、次に打つコマンドが表示される。

`sudo` は付けない。`sudo brew services start libvirt` とすると root の `qemu:///system` デーモンが
立ち上がり、mini-vps が使う `qemu:///session` とは別物になる。

## 最初の VM

[`examples/macos-quickstart.yaml`](../examples/macos-quickstart.yaml) を使う。

```yaml
name: quickstart
memory: 2048
vcpus: 2
base_image: ubuntu-24.04.img
disk: 10
networks: [default]
```

```sh
uv run mini-vps create examples/macos-quickstart.yaml
uv run mini-vps status quickstart
```

初回起動では cloud-init がユーザー作成と qemu-guest-agent の導入(apt)を行うため、SSH で入れるまで
1〜2 分かかる。

### SSH で入る

```sh
uv run mini-vps ssh quickstart
```

`mini-vps ssh` は転送先のポート(`127.0.0.1:<port>`)を解決し、`~/.ssh/minivps_ed25519` で接続する。
ポートを自分で調べて繋ぐ場合は次のとおり。

```sh
virsh -c qemu:///session dumpxml quickstart | grep -o 'hostfwd=tcp:127.0.0.1:[0-9]*'
ssh -i ~/.ssh/minivps_ed25519 -p 2201 ubuntu@127.0.0.1
```

ポートは VM の作成時に `2201-2999`(`MINIVPS_SSH_PORT_RANGE`)から空いているものを割り当て、
VM を削除するまで変わらない。

### シリアルコンソール

SSH が繋がらないときは、シリアルコンソールで起動ログを見られる(抜けるときは `Ctrl+]`)。

```sh
virsh -c qemu:///session console quickstart
```

### 片付け

```sh
uv run mini-vps delete quickstart
```

## ホストの再起動とログアウト

`qemu:///session` の VM はユーザーの libvirtd の子プロセスとして動く。そのため Linux の
`qemu:///system` とは寿命が異なる。

- libvirtd はログインセッションの LaunchAgent として動く。ログアウト中の VM の稼働は保証しない
  (常時動かしたい VM は、ログインしたままのマシンか Linux ホストに置く)。
- ログインすると `brew services` が libvirtd を起動し、`autostart: true`(既定)の VM が起動する。
- Linux の `libvirt-guests` に当たる仕組みが無く、macOS の終了時に VM は正常終了を待たずに止まる。
  書き込み中のデータを守りたい場合は、終了前に `uv run mini-vps stop <name>` で止めておく。

## Linux との機能差

| 機能 | Linux | macOS | 補足 |
|---|:-:|:-:|---|
| VM の作成・起動・停止・削除・再インストール | ○ | ○ | |
| `autostart` | ○ | ○ | macOS はログイン時に起動する |
| `mini-vps ssh` | ○ | ○ | macOS は `127.0.0.1` への転送ポートに繋ぐ |
| qemu-guest-agent 経由の操作 | ○ | ○ | |
| Prometheus エクスポーター | ○ | ○ | |
| 複数 NIC・ネットワークセグメント | ○ | - | `networks` は `[default]` だけ指定できる |
| 静的 IP(`NetworkAttachment`) | ○ | - | 同上 |
| inbound フィルタ(`filters`、nwfilter) | ○ | - | 指定すると `PlatformUnsupported` で拒否する |
| egress 許可リスト(`egress`、nwfilter) | ○ | - | 同上 |
| DNS 自動登録(`MINIVPS_DNS_*`) | ○ | - | ゲストの IP が user-mode の内部アドレスで、外から引けても届かない |
| Ansible によるホスト設定 | ○ | - | macOS は `scripts/macos-setup.sh` |
| ホスト停止時の正常終了(`libvirt-guests`) | ○ | - | [上記](#ホストの再起動とログアウト) |

macOS で使えない機能を spec に書くと、`create` は黙って無視せず `PlatformUnsupported` で失敗する。
例えば `filters` を無視して VM を作ると、利用者は遮断されているつもりで全ポートを開けたまま使うことになるため。

## 環境変数

既定値はホストから検出する。変える必要があるときだけ設定する。

| 変数 | macOS の既定 | 意味 |
|---|---|---|
| `MINIVPS_DATA_DIR` | `~/Library/Application Support/mini-vps` | 下の4ディレクトリの親 |
| `MINIVPS_POOL_PATH` | `$MINIVPS_DATA_DIR/vps-pool` | overlay volume のプール |
| `MINIVPS_SEED_DIR` | `$MINIVPS_DATA_DIR/seeds` | seed ISO のプール |
| `MINIVPS_IMAGES_DIR` | `$MINIVPS_DATA_DIR/images` | base image の `images` プール(`macos-setup.sh` が使う) |
| `MINIVPS_LOCK_DIR` | `$MINIVPS_DATA_DIR/locks` | name 単位のプロセス間ロック |
| `MINIVPS_SSH_PORT_RANGE` | `2201-2999` | SSH 転送に使うホストポートの範囲(`LOW-HIGH`) |
| `MINIVPS_ACCEL` | `hvf` | `hvf` か `tcg`(ソフトウェアエミュレーション。低速) |
| `MINIVPS_LIBVIRT_URI` | `qemu:///session` | libvirt の接続先 |
| `MINIVPS_NETWORK_MODE` | `user` | ネットワーク方式。macOS では変えない |
| `MINIVPS_DISK_CACHE` | (libvirt の既定) | ディスクの `cache` 属性。空文字で libvirt の既定 |
| `MINIVPS_DISK_IO` | (libvirt の既定) | ディスクの `io` 属性。`native` は Linux 専用 |
| `MINIVPS_LOG_LEVEL` | `WARNING` | ログレベル(CLI は `-v` / `-vv` でも変えられる) |
| `MINIVPS_EXPORTER_ADDR` | `127.0.0.1` | Prometheus エクスポーターの待ち受けアドレス |
| `MINIVPS_EXPORTER_PORT` | `9177` | Prometheus エクスポーターの待ち受けポート |

`MINIVPS_DATA_DIR` などを変えた場合は、`scripts/macos-setup.sh` を実行するシェルにも同じ値を設定する
(`images` プールの場所がずれるため)。

## よくあるトラブル

### UEFI firmware が見つからない

`create` が `Unable to find any firmware to satisfy 'efi'` などで失敗する場合、libvirt が qemu 同梱の
firmware 記述子を見つけられていない。

```sh
ls "$(brew --prefix qemu)/share/qemu/firmware/"   # 60-edk2-aarch64.json などがあるか
ls ~/.config/qemu/firmware/                       # macos-setup.sh が置いた固定版
virsh -c qemu:///session domcapabilities --virttype hvf --arch aarch64 --machine virt | grep -A3 firmware
```

記述子が無ければ `brew reinstall qemu` のあと `scripts/macos-setup.sh` を再実行する。
Intel Mac では `--arch x86_64 --machine q35` に読み替える。

### `brew upgrade qemu` のあと既存の VM が起動しない

libvirt は VM の定義時に選んだ firmware のパスを domain 定義に書き込む。qemu 同梱の記述子は
`/opt/homebrew/Cellar/qemu/<version>/...` というバージョン付きのパスを指すため、qemu を更新して古い
Cellar が消えると、その VM は `loader` が見つからず起動できなくなる。

`scripts/macos-setup.sh` は同名の記述子をバージョンに依存しない `$(brew --prefix qemu)/share/qemu/...`
へ書き換えて `~/.config/qemu/firmware/` に置く(libvirt はこのディレクトリを最優先で読む)。
スクリプトを実行する前に作った VM は、`virsh -c qemu:///session edit <name>` で `<loader>` と
`<nvram template=...>` のパスを `$(brew --prefix qemu)/share/qemu/` に直すか、`delete` して `create`
し直す(`reinstall` は domain 定義を作り直さないので直らない)。

### hvf が使えない

`create` や `start` が、virt type `hvf` をサポートしていないという libvirt のエラーで失敗する場合:

- Apple Silicon で macOS 14 以前: Homebrew の qemu が HVF 無しでビルドされている。macOS 15 以降に更新する。
- 仮想マシンの中の macOS: 入れ子の Hypervisor.framework は使えない(`sysctl kern.hv_support` が `0`)。
- どうしても動かしたい場合は `MINIVPS_ACCEL=tcg` でソフトウェアエミュレーションにできる(かなり遅い)。

### SSH の転送ポートが埋まっている

VM の作成は、範囲内で他の VM に割り当て済みでなく、今 bind できるポートを選ぶ。範囲を使い切ったときや、
作成後に別のプロセスが同じポートを使い始めたときは VM の起動に失敗する。
別の範囲を指定するか、衝突しているプロセスを止める。

```sh
lsof -nP -iTCP:2201 -sTCP:LISTEN     # 誰が使っているか
export MINIVPS_SSH_PORT_RANGE=3201-3299
```

既存 VM のポートは domain 定義に書かれていて変わらない(`reinstall` でも同じ)。変えるには `delete` して `create` し直す。

### `uv sync` で libvirt-python のビルドに失敗する

- `xcrun: error: invalid active developer path`: `xcode-select --install` を実行する。
- `Package libvirt was not found in the pkg-config search path`: Homebrew の pkgconf を使っているか
  (`which pkg-config` が `$(brew --prefix)/bin/pkg-config`)を確認し、だめなら
  `export PKG_CONFIG_PATH="$(brew --prefix)/lib/pkgconfig"` してから `uv sync` する。

### ログの場所

- VM(QEMU)のログ: `~/.cache/libvirt/qemu/log/<name>.log`
- libvirt デーモンの状態: `brew services info libvirt`
- mini-vps 自身のログ: `uv run mini-vps -vv ...`(stderr に出る)
