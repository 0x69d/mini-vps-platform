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
- **実機検証済み**: Ubuntu 24.04 LTS(`ubuntu-24.04.img`)、Fedora Cloud Base 43
  (`fedora-43.qcow2`)。ただし legacy BIOS 時代の検証であり、UEFI + q35 移行後は
  **再検証が必要**(いずれも hybrid GPT の genericcloud 系イメージのため UEFI
  起動自体は問題ない見込みだが未確認)。

## 対応 OS 一覧

`ansible/vars/guest_images.yml` と対応する(実行可能なドキュメント)。

| OS | base_image ファイル名 | 検証状態 | 自動取得 |
|---|---|---|---|
| Ubuntu 24.04 LTS (Noble Numbat) - server cloudimg | `ubuntu-24.04.img` | 検証済み | ○ |
| Fedora Cloud Base 43 (Generic variant) | `fedora-43.qcow2` | 検証済み | ○ |

### 動作未確認だったもの

- **Rocky Linux 9 GenericCloud**(`Rocky-9-GenericCloud.latest.x86_64.qcow2`):
  起動後 DHCP リースが得られず、シリアルコンソールにも出力が無いまま CPU 時間
  だけ増加し続けた(9分待機しても IP 未確定)。legacy BIOS boot・NoCloud
  cloud-init との相性で何らかの問題がある可能性があるが、原因未特定。
  `guest_images.yml` には追加していない。UEFI + q35 移行後に再度試す価値はある。

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
