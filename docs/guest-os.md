# ゲスト OS 対応方針

ゲストとして起動できる OS には `mini_vps/resources.py` の `build_domain_xml` が
組み立てる domain XML に起因する暗黙の制約がある。ここでその制約と、対応可能な OS・base image の入手/登録
手順を明文化する。

## 暗黙の契約(前提条件)

- アーキテクチャ: ホストと同じ(`HostProfile.arch`)。x86_64 は machine `q35`、
  aarch64(Apple Silicon の macOS・arm64 の Linux)は machine `virt`。ゲストの
  cloud image もホストと同じアーキテクチャのものを使う([arm64](#arm64apple-silicon)参照)。
- CPU モデル: KVM では `<cpu mode='host-model'/>`、macOS の HVF では
  `<cpu mode='host-passthrough'/>` でホスト CPU に近い CPU モデルをゲストへ公開する。
  RHEL 10 系(Rocky 10・AlmaLinux 10 等)は x86-64-v3(AVX2 世代)を要求するため、
  x86_64 ホストの CPU が v3 未満だと起動できない。
- ディスク/NIC: virtio前提。virtio ドライバを内蔵した cloud image であること。
- ブート: UEFI。`<os firmware='efi'>` による libvirt の firmware 自動選択、
  `<loader secure='no'/>` で secure-boot 非対応 firmware を選択。`teardown()` は
  VM の nvram ファイルも `VIR_DOMAIN_UNDEFINE_NVRAM` フラグで併せて削除する。
- cloud-init: NoCloud データソース。mini-vps が pycdlib で `cidata` ラベルの ISO を
  生成し、ゲスト側の cloud-init がそれを読む前提。image 自体に NoCloud 対応の
  cloud-init が同梱されている必要がある。x86_64 では SATA の CD-ROM、aarch64 では
  (`virt` machine に SATA が無いため)読み取り専用の virtio ディスクとして渡す。
  cloud-init はデバイス種別ではなくボリュームラベルで seed を探すので、どちらでも読まれる。
- qemu-guest-agent: user-data の `packages` で導入するため、初回起動時にゲストから
  パッケージリポジトリへ届く必要がある(Ubuntu の cloud image には入っていない)。
- ユーザー: `spec.user`(既定 `ubuntu`)で cloud-init が新規ユーザーを作成する。
  base image の既定ユーザーとは無関係。`_build_user_data()` は `users` に
  `spec["user"]` のみを積み `default: true` は含めないため、base image に組み込み
  のユーザーがあってもそれは作成されない。
- bash / PAM: user-data は `shell: /bin/bash` を指定し、cloud-init 既定の
  パスワード「!」ロックに依存する。bash が無い、または sshd が PAM 無しビルド
  (「!」ロックを公開鍵認証でも拒否する)のゲストは対象外。

## 対応 OS 一覧

`ansible/vars/guest_images.yml` と対応する。
下表の全 OS について、実機で起動・DHCP リース・SSH ログイン・cloud-init 完了
を確認済み。既定では base image を Ubuntu 26.04 LTS のみに絞っており、
他の OS は必要になったとき `fetch: true` に変えるか手動で配置する。

`static_routes` を指定した VM については、上記に加えて再起動後もスタティックルートが
残っているかを下表の全 OS について確認する(`stop`→`start` または `restart` 経由で
`ip route show`)。`network-online.target` の実効性はイメージによって異なり、最小構成
イメージでは DHCP リース完了前に reached 判定される場合がある。再起動直後に
`ip route show` で経路が消えていないか、`journalctl -u minivps-static-routes.service` で
`ExecStart` が実際に成功しているかを併せて確認する
([spec.md の「スタティックルート」](spec.md#スタティックルート)参照)。

| OS | base_image ファイル名 | 自動取得 |
|---|---|---|
| Ubuntu 26.04 LTS (Resolute, server cloudimg) | `ubuntu-26.04.img` | ○ |
| Ubuntu 24.04 LTS (Noble Numbat, server cloudimg) | `ubuntu-24.04.img` | - |
| Debian 13 (trixie, genericcloud) | `debian-13.qcow2` | - |
| Fedora Cloud Base 44 (Generic variant) | `fedora-44.qcow2` | - |
| Rocky Linux 10 GenericCloud (Base variant) | `rocky-10.qcow2` | - |
| AlmaLinux 10 GenericCloud (latest) | `almalinux-10.qcow2` | - |
| openSUSE Leap 16.0 (Minimal-VM Cloud variant) | `opensuse-leap-16.0.qcow2` | - |

spec の `disk` は base image の仮想サイズ以上を指定する必要がある。
Rocky Linux 10・AlmaLinux 10 の qcow2 は仮想サイズが10 GiB なので
`disk` は 10 以上を指定する(他は 3〜5 GiB なので既定的な 10 で足りる)。

## base image の登録手順

### Ansible 経由(推奨)

`fetch: true` のエントリは `ansible-playbook ansible/playbook.yml` 実行時に自動で
ダウンロードされ、`images` プール配下に配置される。

### 手動での追加

1. `ansible/vars/guest_images.yml` に新しい OS のエントリを追加する。
  まずは `fetch: false`・`verified: false` で。
2. qcow2/raw イメージを `/var/lib/libvirt/images/` に配置し、
   `virsh pool-refresh images` を実行する。
3. `mini_vps/vm-spec.yaml` の `base_image` に指定して `mini-vps create` を試し、
   起動・IP リース・SSH ログインまで確認できたら `verified: true`・`fetch: true`
   に更新する。

## arm64(Apple Silicon)

Apple Silicon の macOS(と arm64 の Linux ホスト)では、ゲストも arm64(aarch64)の
cloud image を使う。amd64 のイメージを指定すると、`create` は成功してもファームウェアが
ブートローダを見つけられず起動しない。

| OS | arm64 の cloud image |
|---|---|
| Ubuntu 24.04 LTS | `https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-arm64.img` |
| Ubuntu 26.04 LTS | `https://cloud-images.ubuntu.com/resolute/current/resolute-server-cloudimg-arm64.img` |
| Debian 13 | `https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-arm64.qcow2` |

注意点:

- macOS では `scripts/macos-setup.sh` が Ubuntu 24.04 の arm64 版を `ubuntu-24.04.img` として
  `~/Library/Application Support/mini-vps/images/` に置く。spec の `base_image` はファイル名だけを
  書くので、同じ spec が Linux(amd64 版)と macOS(arm64 版)の両方で動く。
- 実機での起動確認は Ubuntu 24.04 の arm64 版のみ(上の対応 OS 一覧の「確認済み」は x86_64 での結果)。
  他の OS の arm64 版は、UEFI(AAVMF / edk2-aarch64)で起動できる `virt` machine 向けの
  generic cloud image であれば動く見込みだが未確認。
- ブートは UEFI 必須。arm64 の cloud image は BIOS 起動を持たない(mini-vps は常に UEFI で起動する)。
- 手動で追加する場合は、macOS では base image を `~/Library/Application Support/mini-vps/images/`
  に置いて `virsh -c qemu:///session pool-refresh images` を実行する(Linux の手順の
  `/var/lib/libvirt/images/` と `virsh pool-refresh images` に当たる)。上書き事故を防ぐため
  `chmod 0444` しておく。

## スコープ外

- IPv6 は非対応。
- ホストと異なるアーキテクチャのゲスト(Apple Silicon 上の x86_64 ゲストなど)は非対応。
  TCG なら技術的には動くが、`HostProfile` はホストのアーキテクチャしか選ばない。
- cloud-init 対応・virtio ドライバ有無などの base image の検証をアプリ側は一切
  行わない。非対応イメージを指定した場合、`create` はエラーにならず、ブート失敗や
  IP 未確定として観測される(`status` の `ip` がいつまでも確定しない、等)。
