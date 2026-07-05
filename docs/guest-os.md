# ゲスト OS 対応方針

ゲストとして起動できる OS には `mini_vps/config.py` の domain XML テンプレートに
起因する暗黙の制約がある。ここでその制約と、対応可能な OS・base image の入手/登録
手順を明文化する。

## 暗黙の契約(前提条件)

- **アーキテクチャ**: x86_64 + KVM(`<type arch='x86_64' machine='q35'>hvm</type>`)。
- **ディスク/NIC**: virtio(`bus='virtio'`)前提。virtio ドライバを内蔵した cloud
  image であること。
- **ブート**: UEFI(`<os firmware='efi'>` による libvirt の firmware 自動選択、
  `<loader secure='no'/>` で secure-boot 非対応 firmware を選択)。`teardown()` は
  per-VM の nvram ファイルも `VIR_DOMAIN_UNDEFINE_NVRAM` フラグで併せて削除する。
- **cloud-init**: NoCloud データソース。`cloud-localds` が `cidata` ラベルの ISO を
  生成し、ゲスト側の cloud-init がそれを読む前提(image 自体に NoCloud 対応の
  cloud-init が同梱されている必要がある)。
- **ユーザー**: `spec.user`(既定 `ubuntu`)で cloud-init が新規ユーザーを作成する。
  **base image の既定ユーザーとは無関係**。`_build_user_data()` は `users` に
  `spec["user"]` のみを積み `default: true` は含めないため、base image に組み込み
  のユーザーがあってもそれは作成されず、常に `spec.user` で SSH ログインする。
- **bash / PAM**: user-data は `shell: /bin/bash` を指定し、cloud-init 既定の
  パスワード「!」ロックに依存する。bash を持たない、または sshd が PAM 無し
  ビルドのゲスト(Alpine 等。「!」ロックを公開鍵認証でも拒否する)は
  このままでは SSH ログインできないため対象外。
- **実機検証済み**: UEFI + q35 構成で下表の全 OS について、起動
  (`/sys/firmware/efi` 存在 = UEFI ブート)・DHCP リース・SSH ログイン・
  cloud-init 完了(`cloud-init status: done`)まで確認済み。
- **CPU モデル**: `<cpu mode='host-model'/>` でホスト CPU をゲストへ公開する。
  Rocky Linux 10 等の RHEL 10 系は x86-64-v3(AVX2 世代)を要求するため、
  ホスト CPU が v3 未満の環境では起動できない。

## 対応 OS 一覧

`ansible/vars/guest_images.yml` と対応する(実行可能なドキュメント)。
既定ダウンロード(`fetch: true`)は既定 base image の Ubuntu 26.04 LTS のみに
絞っており、他の OS は必要になったとき `fetch: true` に変えるか手動で配置する。

| OS | base_image ファイル名 | 検証状態 | 自動取得 |
|---|---|---|---|
| Ubuntu 26.04 LTS (Resolute) - server cloudimg | `ubuntu-26.04.img` | 検証済み | ○ |
| Ubuntu 24.04 LTS (Noble Numbat) - server cloudimg | `ubuntu-24.04.img` | 検証済み | - |
| Debian 13 (trixie) - genericcloud | `debian-13.qcow2` | 検証済み | - |
| Fedora Cloud Base 44 (Generic variant) | `fedora-44.qcow2` | 検証済み | - |
| Rocky Linux 10 GenericCloud (Base variant) | `rocky-10.qcow2` | 検証済み | - |
| AlmaLinux 10 GenericCloud (latest) | `almalinux-10.qcow2` | 検証済み | - |
| openSUSE Leap 16.0 (Minimal-VM Cloud variant) | `opensuse-leap-16.0.qcow2` | 検証済み | - |

spec の `disk` は base image の仮想サイズ以上を指定する必要がある(overlay は
base より小さくできない)。Rocky Linux 10・AlmaLinux 10 の qcow2 は仮想サイズが
10 GiB なので `disk` は **10 以上**を指定する(他は 3〜5 GiB なので既定的な
10 で足りる)。

OS ごとの補足:

- **Rocky 10 / AlmaLinux 10**(RHEL 10 系): x86-64-v3(AVX2 世代)未満の
  ホスト CPU では起動できない(「暗黙の契約」参照)。

Fedora Cloud Base 43 と Rocky Linux 9(9.8)も UEFI + q35 で検証済みだが、
それぞれ Fedora 44・Rocky 10 で置き換えたため一覧からは外した(必要なら
手動追加手順で再登録すれば使える)。

### 過去に動作しなかった構成

- **Rocky Linux 9 GenericCloud × legacy BIOS**(UEFI + q35 移行前):
  起動後 DHCP リースが得られず、シリアルコンソールにも出力が無いまま CPU 時間
  だけ増加し続けた(9分待機しても IP 未確定)。UEFI + q35 移行後に再検証した
  ところ、同一イメージ系列(Rocky 9.8)で起動・IP リース・SSH ログインまで
  問題なく動作した。RHEL 9 系以降のイメージは UEFI ブートを前提とするのが安全
  (RHEL 10 系は upstream が legacy BIOS を廃止しており UEFI 必須)。

## base image の登録手順

### Ansible 経由(推奨)

`fetch: true` のエントリは `ansible-playbook ansible/playbook.yml` 実行時に自動で
ダウンロードされ、`images` プール配下に配置される。

### 手動での追加

1. `ansible/vars/guest_images.yml` に新しい OS のエントリを追加する(まず
   `fetch: false`・`verified: false` で)。
2. qcow2/raw イメージを `/var/lib/libvirt/images/` に配置し、
   `virsh pool-refresh images` を実行する。
3. `mini_vps/vm-spec.yaml` の `base_image` に指定して `mini-vps create` を試し、
   起動・IP リース・SSH ログインまで確認できたら `verified: true`・`fetch: true`
   に更新する。

## スコープ外(既知の制約)

- IPv6・非 x86_64 アーキテクチャは非対応。
- base image の中身(cloud-init 対応・virtio ドライバ有無)をアプリ側は一切検証
  しない。非対応イメージを指定した場合、`create` はエラーにならず、ブート失敗や
  IP 未確定として観測される(`status` の `ip` がいつまでも確定しない、等)。
