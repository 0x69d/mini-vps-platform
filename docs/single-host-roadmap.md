# 単一ホスト仮想化基盤としてのロードマップ(提案)

「VPS の最小版」から「1台のホストを長く安全に回し続けられる仮想化基盤」へ進むための
リファクタリング・機能追加の提案。コード調査(`mini_vps/` 全体、`ansible/`、`monitoring/`)に
基づく。ここに書くのは提案で、実装済みの機能ではない。

## 方向性

単一ホストに絞るなら、クラウドのまねをする必要はない。スケジューラ・分散状態ストア・
マルチテナンシーは作らない。代わりに次の4つに投資する。

1. 耐久性: ホストの再起動・障害・誤操作のあとでも VM とデータが残る。
2. 容量の正直さ: ホスト1台の CPU・メモリ・ディスクを超えて約束しない。
3. 宣言的な構成の一括管理: router/dns/web/db のようなアプライアンス群を1つの単位として
   `plan` / `apply` する(「VM 版 docker compose」)。
4. 運用の手触り: console・snapshot・doctor といった、ホスト1台の管理者が毎日使う操作。

「自前 DB を持たず libvirt `<metadata>` を真実源にする」という現行の設計原則は、
単一ホストではむしろ強みになる。以下の提案はすべてこの原則を保ったまま実現できる。

## 現状の課題(調査結果)

優先度は「事故が起きたときの被害の大きさ」で付けている。

| # | 課題 | 根拠 | 影響 |
|---|---|---|---|
| A1 | domain の autostart を設定していない | `setAutostart` はストレージプール(`resources.ensure_pool`)にしか無い | ホスト再起動後に全 VM が停止したまま残る。router-1/dns-1 が落ちたままになり、全セグメントが道連れになる |
| A2 | ホスト停止時にゲストを正常終了させる設定が無い | `ansible/` に `libvirt-guests` の設定が無い | ホストの shutdown で VM が強制停止され、DB 系 VM のデータが壊れうる |
| A3 | name 単位ロックがプロセス内に閉じている | `ServerManager._locks` は `threading.Lock` | CLI と API は別プロセスなので、同じ name への `create` と `delete` が並行すると TOCTOU が再発する |
| A4 | base image の保護が無い | overlay は `backingStore` で base を参照するが、使用中かどうかを誰も数えていない | 使用中の base image を削除・上書きすると、それを参照する全 VM のディスクが壊れる |
| A5 | 容量のアドミッション制御が無い | `create()` はホストの空きメモリや vCPU 数を見ない | メモリの過剰割当で OOM killer が qemu を落とす。ディスクの過剰割当(thin provisioning)でプールが埋まり、全 VM が I/O エラーで止まる |
| A6 | `disk >= base image の仮想サイズ` を検証していない | `docs/spec.md` で利用者の責任としている | 起動しない VM が「作成成功」として返る |
| B1 | 管理外リソースの残骸を検出できない | `teardown` は途中で失敗しうるが、孤児の volume・seed・nwfilter を見つける手段が無い | 長期運用でプールに残骸がたまる |
| B2 | 稼働中の VM には何も変更できない | `ServerRunning` で一律に拒否している | filters 変更のたびに停止が要る(後述のとおり nwfilter は稼働中でも更新できる) |
| B3 | バックアップ・スナップショットが無い | — | reinstall/delete の誤操作から戻れない |
| C1 | domain XML の生成経路が2系統ある | 新規作成は `str.format` テンプレート(`config.DOMAIN_XML_TEMPLATE`)、収束は ElementTree での差分編集(`resize_domain_xml` / `set_domain_filterref_xml`) | 収束できるフィールドを増やすたびに、差分編集用の関数を1つずつ足すことになる |
| C2 | 層の境界で spec が dict に落ちる | `ServerSpec(...).model_dump()` の後、下位層は `spec["networks"]` のように dict で読む。`_network_name` などは `str` か `dict` かを実行時に判定している | 型の保証が `spec.py` から先に届かない |

## 提案

### フェーズ1: 耐久性(最優先・小さく出せる)

#### 1-1. domain の autostart を spec で持つ

`ServerSpecInput` に `autostart: bool = True` を足し、`provision` で `dom.setAutostart()` を
呼ぶ。`_MUTABLE_FIELDS` にも入れる(`setAutostart` は稼働中でも効く)。既定を True に
するのは、単一ホストで VM を宣言したなら「ホストが起きたら VM も起きる」のが自然なため。
既存 VM は metadata に `autostart` が無いので、`create()` が既に使っている
`ServerSpec(**_read_spec(...))` の欠落フィールド補完で True として読まれる。
autostart の実体を揃えるには、既存 VM へ同じ spec で `create` を再実行する(収束する)。

関連して、`ensure_network_active` がやっている「network を必要になったときに起動する」
処理はホストの起動直後には走らない。network 側の autostart が Ansible
(`segment_network.yml` / `network.yml`)で有効になっているかを確認し、`doctor`(3-2)の
検査項目に加える。

#### 1-2. `libvirt-guests` を Ansible で構成する

`ON_SHUTDOWN=shutdown`、`SHUTDOWN_TIMEOUT=120`、`ON_BOOT=ignore` を設定する。
`ON_BOOT` を `ignore` にするのは、起動するかどうかを 1-1 の domain autostart に一本化する
ため。`start` にすると、ホスト停止時に動いていた VM が autostart=false でも復帰して
しまい、spec の宣言と挙動がずれる。

#### 1-3. ロックをプロセス間ロックにする

`_lock_for` を、`/run/minivps/locks/<name>.lock` に対する `fcntl.flock` とプロセス内
`threading.Lock` の二段構えにする。`flock` はファイル記述子単位で効くので、同じプロセスの
スレッド同士はプロセス内 Lock で、別プロセス同士は flock で排他する。CLI・API・
(将来の)デーモンが同じホストで動いても、同じ name への書き込みは直列化される。
非再帰という性質は今と同じなので、`create()` から `self.get()` を呼ぶ現状の構造は
そのまま使える。

`/run/minivps` は Ansible で tmpfiles.d を使って作り、`libvirt` グループが書けるように
する。

#### 1-4. 作成前の容量チェック(アドミッション制御)

`manager.create()` のロック内、`provision` の前に `_admit(spec)` を置く。

- メモリ: `conn.getInfo()` のホストメモリから、管理対象 VM の `memory` の合計と
  予約分(既定はホストの 10% か 2GiB の大きいほう)を引き、足りなければ拒否する。
  オーバーコミット率は設定で変えられるようにし、既定は 1.0(オーバーコミットしない)。
- vCPU: 1 VM の `vcpus` がホストの論理 CPU 数を超えていたら拒否する。合計の
  オーバーコミットは許すが、比率はメトリクスで見えるようにする(4-2)。
- ディスク: `vps-pool` の `info()` から空き容量を取り、`disk` の合計が超えるなら
  警告または拒否する(thin provisioning なので、既定は警告)。
- 仮想サイズ: `base_image` の `info()[1]`(capacity)が `disk` GiB を超えていたら
  拒否する(A6)。

拒否は新しい例外 `InsufficientCapacity` で表し、API では 409 または 507 に、CLI では
新しい終了コードに対応づける。「入力の検証は `spec.py` に集約する」という規約とは
ぶつからない。これはホストの状態に依存する判定で、入力の検証ではないため。

#### 1-5. base image を保護する

- 使用中チェック: 管理対象 VM の spec にある `base_image` を集計すれば参照数が出る。
  `mini-vps image list` で「イメージ名・仮想サイズ・参照している VM」を表示する。
- 読み取り専用化: Ansible でダウンロードするファイルのモードを `0644` から `0444` に
  変え、誤って上書きしにくくする。
- 版の固定: `guest_images.yml` の `current` / `latest` の URL はダウンロードのたびに
  中身が変わる。イメージ名に日付かバージョンを入れる運用(`ubuntu-26.04-20260901.img`)
  にし、更新は「新しい名前で追加して、VM ごとに reinstall で移す」に統一する。

### フェーズ2: 構成の一括管理(プロダクトの軸になる機能)

アプライアンス群(router/dns/web/db)は、個々の VM ではなく1つの構成として扱いたい。
単一ホストで一番価値が出るのはここだと考える。

#### 2-1. `mini-vps apply -f <dir|file>` と `plan`

- 複数の spec(1ファイルに複数ドキュメント、またはディレクトリ)を読み込み、
  「作成・収束・変更なし・競合・削除候補」に分けて表示する(`plan`)。
- `apply` は `plan` の結果を順に `ServerManager.create()` に流すだけの薄い層にする。
  既存の `create()` が冪等で、差分の分類(`diff_keys` / `_MUTABLE_FIELDS`)も持っているので、
  必要なのは分類ロジックを `create()` から純粋関数として切り出すことだけ
  (`plan_change(old_spec, new_spec) -> Change`)。純粋関数にすれば、ほかの純粋関数と
  同じ方針で素の値だけでテストできる。
- 起動順: `depends_on: [dns-1]` を spec に足し、トポロジカル順に作成する。DNS 登録
  (`dns_registration.register`)は dns-1 が先に上がっていれば初回から成功する。
- 削除: `--prune` を付けたときだけ、構成ファイルに無い管理対象 VM を消す。
  どの構成に属するかは metadata に `stack` ラベルを書いておき、それで判定する
  (DB を持たない原則を保てる)。

API にも `POST /stacks/{name}/plan` と `PUT /stacks/{name}` を足せば、CLI と API の
対称性も保てる。

#### 2-2. ネットワークを宣言の対象に含める(任意)

今はセグメントを Ansible(`network_segments.yml`)だけで定義しているので、構成を
`apply` しても network が無ければ失敗する。選択肢は2つ。

- (推奨)Ansible に残し、`plan` で「参照しているが未定義の network」を検出して止めるだけにする。
  ホストの初期設定と VM の宣言を分けるという現在の責務分担を保てる。
- `Network` リソースを API に足し、`networkDefineXML` で作る。柔軟になるが、iptables との
  相互作用(`docs/spec.md` の注意書き)まで責任範囲に入る。

### フェーズ3: 運用の手触り

#### 3-1. 稼働中の VM に反映できる変更を広げる(B2)

libvirt が稼働中の反映に対応しているものから順に広げる。

| フィールド | 稼働中の反映方法 | 難しさ |
|---|---|---|
| `filters`(ルールの内容だけ変える) | 同じ名前で `nwfilterDefineXML` を再実行すると、libvirt が稼働中のインターフェースにも反映する | 小。filterref を付け外ししない変更だけなら domain XML に触れる必要が無い |
| `filters`(フィルタの有無を切り替える) | `updateDeviceFlags` で interface を `AFFECT_LIVE\|AFFECT_CONFIG` 更新 | 中 |
| `autostart` | `setAutostart` | 小 |
| `memory`(縮小) | balloon の `setMemoryFlags(AFFECT_LIVE)` | 中。上限を増やすには `maxMemory` の事前確保が要る |
| `vcpus`(増加) | `<vcpu current=...>` で上限を先に確保し、`setVcpusFlags` | 中 |
| `disk`(拡張のみ) | `vol.resize` と `dom.blockResize`。ゲスト側は cloud-init の growpart が次回起動時に広げる | 中 |

`ServerRunning` で一律に拒否するのをやめ、フィールドごとに「稼働中に反映できる・停止中
なら反映できる・再作成が要る」の3段階を宣言した表にする(`_MUTABLE_FIELDS` を置き換える)。

#### 3-2. `mini-vps doctor` / `gc`(B1)

DB を持たない設計なので、整合性の確認は「libvirt の実体を見て突き合わせる」ことになる。

- `vps-pool` / `vps-seeds` の volume と `minivps-*` の nwfilter のうち、対応する管理対象
  domain が無いものを孤児として列挙する。`gc` で削除する(既定は dry-run)。
- network の autostart が無効・base image が欠けている・参照先の network が未定義、など
  ホスト側の前提を検査する。
- 読み取り専用なので、exporter から `minivps_orphan_resources` として出せば Grafana でも見られる。

#### 3-3. スナップショットとバックアップ(B3)

- スナップショット: overlay qcow2 の internal snapshot は UEFI(pflash)の VM では
  使えないので、external snapshot(`snapshotCreateXML` の `DISK_ONLY`)を使う。
  `snapshot create|list|revert|delete` を CLI/API に足す。revert は停止中に限る。
- バックアップ: libvirt の `backupBegin`(push モード)で、稼働中の VM から一貫した
  qcow2 を取り出す。保存先はホストのローカルディレクトリにし、世代管理は数だけ持つ。
- reinstall と delete の直前に自動でスナップショットを取るオプション(`--snapshot`)を
  付ければ、誤操作から戻れるようになる。

#### 3-4. console

serial console は既に定義してある(`<serial type='pty'>`)ので、`mini-vps console <name>` は
`virsh console` への薄いラッパーで済む。cloud-init が失敗して SSH で入れない VM の調査に要る。

### フェーズ4: 単一ホスト向けの性能と観測

#### 4-1. ディスク I/O の既定値

`<driver name='qemu' type='qcow2' discard='unmap'/>` に `cache='none' io='native'` を
足す。ホストのページキャッシュとの二重キャッシュを避け、ホストのメモリをゲストのために
残せる。単一ホストでは、ホストのメモリの余裕がそのまま VM の数の上限になる。
加えて、ゲストの `fstrim.timer` が有効か(`discard='unmap'` を活かすのに必要)を
`docs/guest-os.md` に書き足す。

#### 4-2. ホスト側のメトリクス

exporter は今、VM ごとのメトリクスしか出していない。容量計画に要るホスト全体の値を足す。

- `minivps_host_memory_bytes` / `minivps_allocated_memory_bytes`(割当率)
- `minivps_host_cpus` / `minivps_allocated_vcpus`(オーバーコミット率)
- `minivps_pool_capacity_bytes` / `minivps_pool_allocation_bytes`(`vps-pool`・`images`)

これが 1-4 のアドミッション制御の根拠を、そのまま可視化する役目も持つ。
アラート通知はスコープ外のままでよいが、プールの残り容量だけは Grafana の閾値表示を付けておく。

#### 4-3. libvirt イベントの購読(任意)

`domainEventRegisterAny` でライフサイクルイベント(crash、ゲストからの shutdown)を受けて
INFO ログに残す。crash した VM に `<on_crash>restart</on_crash>` を付けるかどうかは spec の
選択肢として出す。

### フェーズ5: API の信頼境界

今は「localhost の TCP で待ち受けること」が信頼境界になっている。単一ホストでは、同じ
ホストの別ユーザーからも到達できてしまう。libvirt 自身がそうしているように、
UNIX ドメインソケット(`uvicorn --uds /run/minivps/api.sock`)で待ち受け、ファイルの
パーミッションで `libvirt` グループに限定するのが、単一ホストで一番安くて筋のよい認可になる。
TCP の待ち受けは開発用に残す。systemd の socket activation にすれば、`ansible/` から
常駐サービスとしても配れる。

## リファクタリング

機能追加の前に(または合わせて)進めたいもの。どれも振る舞いは変えない。

### R1. domain XML の生成を ElementTree に一本化する(C1)

`build_domain_xml` をテンプレートの `str.format` から ElementTree で組み立てる形に変え、
「spec から domain の要素を作る関数」と「既存の XML に spec を当てる関数」を同じ部品で
書けるようにする。`resize_domain_xml` / `set_domain_filterref_xml` は、この部品の
「既存の要素を書き換える」使い方に置き換えられる。3-1 で稼働中に反映できるフィールドを
増やすとき、フィールドごとに XML 編集関数を足さずに済む。

副次的な効果として、テンプレートに値を文字列で埋め込むことによるエスケープ漏れの心配が無くなる
(今は `spec.py` の文字種制約に頼っている)。

### R2. 下位層に型付きの spec を渡す(C2)

`manager` 以下が受け取る spec を `dict` から `ServerSpec` に変える。`_network_name` /
`_has_static_network` の `isinstance(net, str)` 判定は、`networks` の要素を読み込み時に
`NetworkAttachment`(`address=None` なら DHCP)へそろえておけば不要になる。
metadata への書き込みは今のまま `model_dump()` の YAML にすれば、保存形式の互換性も保てる。
テストの多くは dict を組み立てて渡しているので、移行はモジュール単位で段階的に進める。

### R3. 差分の分類を純粋関数に切り出す

`create()` の中にある `diff_keys` の計算と `_MUTABLE_FIELDS` による判定を
`plan_change(old, new) -> Change` として `manager.py` の外(例: `planning.py`)に出す。
2-1 の `plan` と 3-1 のフィールドごとの反映表の土台になる。

### R4. 設定値を環境変数で差し替えられるようにする

`config.py` のパス(`POOL_PATH` / `SEED_DIR`)と `LIBVIRT_URI` は定数で決め打ちに
なっている。DNS 登録と同じ `MINIVPS_*` 環境変数で上書きできるようにしておくと、
1-4 の予約値やオーバーコミット率も同じ仕組みに載せられる。

## 着手順の提案

1. フェーズ1 の 1-1〜1-3(autostart、libvirt-guests、プロセス間ロック)。どれも小さく、
   単一ホストで長く運用するなら避けて通れない。
2. 1-4・1-5(容量と base image の保護)と、4-2(ホスト側のメトリクス)。
3. R3 → 2-1(`plan` / `apply`)。プロダクトの軸にする機能。
4. R1 → 3-1(稼働中に反映できる変更)。
5. 3-2〜3-4、R2、フェーズ5。

## スコープ外のまま据え置くもの

README の「含まないもの」は、単一ホストという方向性とも矛盾しないので維持する。

- 複数ホストへのスケジューリング・ライブマイグレーション
- マルチテナンシー・課金
- アラート通知(メトリクスは出すが、通知は Alertmanager など外部に任せる)
