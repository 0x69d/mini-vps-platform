# ロードマップ: AI エージェントの住処になる単一ホスト仮想化基盤

mini-vps-platform を「VPS の最小版」から、**AI エージェントを自分のマシン上で安全に長時間動かすための
仮想化基盤**へ進める。Linux(KVM)と macOS(Apple Silicon / Intel の HVF)を標準でサポートする。

## 方向性

ローカルでも VPS 的な計算資源の需要は伸びる。エージェントに root・自由なネットワーク・長時間の
自律作業を任せるなら、コンテナより VM の隔離が欲しくなるからだ。

このとき mini-vps は **エージェントを動かすためのもの(住処)** を主軸にする。

| | 住処(主軸) | サンドボックス(後段) |
|---|---|---|
| エージェントの位置 | VM の中で働く | VM の外から道具として呼ぶ |
| VM の寿命 | 数時間〜数日 | 数秒〜数分 |
| 勝負どころ | 隔離・egress 制御・人間の監督・巻き戻し | 起動速度・fork |
| ローカルである理由 | 常時稼働のコスト、データを外に出さない、手元のリポジトリや社内網に近い | 弱い |

「両方」は長期的に自然に合流する(住処の中の親エージェントが試行用の子 VM を作る)。ただし
ゲストからホストの制御 API を呼ばせると隔離に穴が開くため、委譲の権限モデルを先に作る必要がある。
そのため順序は **住処を固める → 権限モデル → 子 VM(サンドボックス)** とする。

設計原則は維持する。

- 自前 DB を持たない。真実源は libvirt の domain(`<metadata>` の spec と domain XML そのもの)。
- 検証は `spec.py` に集約し、入口(CLI / Web API / exporter / MCP)は `ServerManager` の薄いラッパー。
- secrets は spec・metadata・ログに載せない。

## 対応プラットフォーム

プラットフォームの違いは `platform_profile.py` の `HostProfile` 1か所に閉じ込め、上位層は
プロファイルの属性だけを見る。

| | Linux(KVM) | Linux(KVM 無し) | macOS |
|---|---|---|---|
| libvirt URI | `qemu:///system` | `qemu:///system` | `qemu:///session`(Homebrew の libvirt) |
| domain type | `kvm` | `qemu`(TCG・低速。検証用) | `hvf` |
| アーキテクチャ | ホストに合わせる(x86_64 / aarch64) | 同左 | Apple Silicon は aarch64(`virt`)、Intel は x86_64(`q35`) |
| ネットワーク | libvirt の NAT ネットワーク・セグメント・静的 IP | 同左 | QEMU user-mode(SSH はホストの 127.0.0.1 へポート転送) |
| inbound / egress フィルタ | nwfilter | nwfilter | 非対応(指定すると `PlatformUnsupported` で拒否。黙って無視しない) |
| ホストの事前設定 | `ansible/playbook.yml` | 同左 | `scripts/macos-setup.sh` |

seed ISO は `cloud-localds`(Linux の cloud-image-utils)をやめ、純 Python の `pycdlib` で生成する。
macOS に同等のコマンドが無いためで、これで ISO 生成に外部バイナリが要らなくなる。

## 機能

### フェーズ1: 基盤(リファクタリング)

| ID | 内容 |
|---|---|
| F1 | `HostProfile`: OS・アクセラレータ(kvm / hvf / tcg)・URI・パス・ネットワーク方式・フィルタ対応を検出し、`MINIVPS_*` 環境変数で上書きできる |
| F2 | seed ISO を `pycdlib` で生成する(外部バイナリ不要) |
| F3 | domain XML の生成を ElementTree に一本化し、プロファイルに応じて組み立てる。qemu-guest-agent の channel を常に付ける。ディスクは Linux で `cache='none' io='native'` |
| F4 | `planning.py`: フィールドごとの反映方式(稼働中に反映 / 停止中に反映 / 再作成が必要)の表と、差分を分類する純粋関数 `plan_change` |
| F5 | name 単位ロックをプロセス間ロック(`fcntl.flock`)にする。CLI・API・MCP が別プロセスでも同じ name への書き込みを直列化する |
| F6 | 例外と終了コード・HTTP ステータスの対応を `errors.py` の1つの表に集約する |

### フェーズ2: ホストの耐久性と安全

| ID | 内容 |
|---|---|
| D1 | spec の `autostart`(既定 true)。ホスト再起動後に VM が戻る。稼働中でも反映できる |
| D2 | Ansible で `libvirt-guests` を設定し、ホスト停止時にゲストを正常終了させる |
| D3 | 作成前の容量チェック(メモリ・vCPU・プール空き・base image の仮想サイズ)。足りなければ `InsufficientCapacity` |
| D4 | `image list`(base image と参照している VM)。Ansible で base image を読み取り専用にする |
| D5 | `doctor` / `gc`: 孤児リソース(volume・seed・nwfilter)とホストの前提を検査・回収する |
| D6 | exporter にホスト全体のメトリクス(割当率・オーバーコミット率・プール使用量) |

### フェーズ3: エージェントの住処

| ID | 内容 |
|---|---|
| A1 | `exec`: qemu-guest-agent 経由でゲスト内のコマンドを実行し、終了コード・stdout・stderr を返す(SSH 不要) |
| A2 | `ssh` / `console`: 接続先の解決(macOS はポート転送先)とシリアルコンソール |
| A3 | `pause` / `resume`: 暴走したエージェントをその場で凍結する |
| A4 | スナップショット(作成・一覧・巻き戻し・削除)。危険な操作の前のチェックポイント |
| A5 | egress 許可リスト(`egress`)。エージェントの持ち出しを止める境界 |
| A6 | 稼働中の反映: `filters` / `egress` のルール変更と `autostart` を停止なしで反映する |
| A7 | `plan` / `apply`: 複数 spec(スタック)を差分表示・一括適用する。`depends_on` で起動順を決める |
| A8 | MCP サーバ: エージェント(Claude Code など)から VM を操作する4つ目の入口 |

### フェーズ4: 両方へ(このブランチでは設計のみ)

| ID | 内容 |
|---|---|
| P1 | Web API を UNIX ドメインソケットで待ち受け、ファイル権限で利用者を限定する |
| P2 | 委譲トークン: 「自分の子 VM だけ・割当量の範囲内だけ」を操作できるスコープ付きの資格情報 |
| P3 | 子 VM: 住処の中のエージェントが P2 の範囲でサンドボックスを作る |

フェーズ4を実装しないのは、権限モデルの形がフェーズ3を実際に使ってみないと決まらないため。
資格情報の設計を推測で固めると、後から緩めるのが難しい。

## 見送るもの

- 複数ホストへのスケジューリング・ライブマイグレーション・マルチテナンシー・課金
- ドメイン名単位の egress 許可(ホスト側に DNS を見る透過プロキシが要る。A5 は IP/CIDR 単位)
- 稼働中の memory / vCPU 変更(`maxMemory` / 最大 vCPU の事前確保という別設計が要る)
- 下位層への型付き spec の受け渡し(旧ロードマップの R2)。効果に比べて既存テスト全体の書き換えが大きい
- アラート通知(メトリクスは出す。通知は Alertmanager など外部に任せる)

## 開発の進め方

リード(Claude)が基盤(フェーズ1)を直列で作り、その上でフェーズ2〜3を独立したワークツリーの
エージェントに並列で割り振る。各エージェントの成果はリードがレビューして統合ブランチ
(`claude/single-host-virtualization-strategy-jhin4v`)にマージする。

| 担当 | 範囲 |
|---|---|
| リード | フェーズ1、統合、実 libvirt での E2E 確認、MCP サーバ(A8) |
| エージェント A | A1〜A3(guest agent・ssh・console・pause/resume) |
| エージェント B | A4(スナップショット) |
| エージェント C | D3〜D6(容量・イメージ・doctor/gc・ホストメトリクス) |
| エージェント D | A5・A6(egress・稼働中の反映) |
| エージェント E | A7(plan/apply) |
| エージェント F | macOS のセットアップ・CI・Ansible(D2 を含む)・ドキュメント |
