# ホストの運用: 容量チェック・image list・doctor / gc・ホストメトリクス

ホスト1台を長く安全に回すための機能をまとめる。

- **約束しすぎない**: 作成前の容量チェック(`mini_vps/admission.py`)
- **残骸を残さない**: 孤児リソースの検出と回収(`mini_vps/doctor.py`)
- **見える**: base image の一覧(`mini_vps/images.py`)とホスト全体のメトリクス(`mini_vps/exporter.py`)

## 容量チェック

`create` / `PUT /servers/{name}` の新規作成は、何も作る前(name 単位ロックの内側、
`provision` の直前)に容量を確かめる。足りなければ `InsufficientCapacity` で拒否し、
libvirt には何も残さない。

| 入口 | 容量不足のとき |
|---|---|
| CLI | 終了コード 11、stderr に `error: insufficient capacity: <name>: <理由>` |
| Web API | 507 Insufficient Storage、`{"detail": "insufficient capacity: ..."}` |

既存 VM への `create` / `PUT` で memory か vcpus を**増やす**収束も、同じ式で確かめる
(`_converge` の直前)。このとき自分自身の現在の割当は数えない(新しい値に置き換えて数える)。
減らすだけの収束と、autostart だけの変更は検査しない。

### 式

| 検査 | 拒否する条件 |
|---|---|
| メモリ | `既存の管理 VM の memory の合計 + 新 VM の memory > floor((ホストメモリ - 予約) × オーバーコミット率)` |
| vCPU | `新 VM の vcpus > ホストの論理 CPU 数` |
| base image | `spec の disk (GiB) < base image の仮想サイズ` |
| overlay 用プール | 拒否しない。`プールの空き < 管理 VM の disk の合計 + 新 VM の disk` のとき WARNING ログを出すだけ |

- メモリは**稼働状態に関わらず全ての管理 VM** を数える。停止中の VM も autostart や
  `start` で同時に起きうるためで、「今空いているか」ではなく「約束の合計が収まるか」を見る。
- 値は spec(libvirt `<metadata>`)の memory / vcpus / disk で数える。virsh で domain を
  直接いじった場合は数えた値とずれる。
- ホストメモリと論理 CPU 数は `conn.getInfo()`(libvirt のノード情報)から取る。
- vCPU は1台がホストの論理 CPU 数を超えることだけを拒否する。合計のオーバーコミットは
  許す(CPU 時間は共有できるため)。
- base image の仮想サイズは `images` プールの volume の `info()` の capacity。disk が
  base image より小さい overlay はゲストのファイルシステムを壊す。base image が無いときは
  ここでは拒否せず、従来どおり provision の失敗(libvirtError)に任せる。
- overlay は thin provisioning(qcow2 は書いた分だけ育つ)のため、プールの空きは作成を
  止めない。警告が出たら `minivps_pool_available_bytes` を見て空きを増やすこと。

### 設定

| 環境変数 | 既定 | 内容 |
|---|---|---|
| `MINIVPS_MEMORY_RESERVE_MIB` | ホストメモリの 10% と 2048 の大きいほう | ホスト OS・libvirtd・QEMU 自身のオーバーヘッド用に残すメモリ(MiB) |
| `MINIVPS_MEMORY_OVERCOMMIT` | `1.0` | メモリのオーバーコミット率。1.5 なら (ホスト - 予約) の 1.5 倍まで約束する |

値は作成のたびに読む(CLI は呼び出しごと、API はプロセスの環境変数)。数値でない値や
範囲外(予約が負、率が 0 以下)は ValueError になる。

例: ホスト 32 GiB(32768 MiB)・既定設定なら、予約は max(3277, 2048) = 3277 MiB、
上限は 29491 MiB。既存 VM が合計 28 GiB なら 2 GiB の VM は拒否される。

## image list

`images` プールの base image と、それを `base_image` に指定している管理 VM を一覧する。
参照がある image を消すと、その VM の overlay の backing file が無くなり起動も
reinstall もできなくなる。

```console
$ mini-vps image list
NAME              VIRTUAL  ACTUAL    FORMAT  USED_BY
ubuntu-24.04.img  3.5GiB   596.3MiB  qcow2   web-1,web-2
```

Web API は `GET /images`:

```json
{"images": [{"name": "ubuntu-24.04.img", "virtual_bytes": 3758096384,
             "actual_bytes": 625262592, "format": "qcow2", "used_by": ["web-1"]}]}
```

`images` プールが無ければ空の一覧を返す(doctor が error として報告する)。

## doctor

ホストの前提と孤児リソースを検査する。読み取りのみで、何も変更しない。
結果は `{level: ok|warn|error, check, detail}` のリスト。

| check | 内容 | level |
|---|---|---|
| `network:<名前>` | 管理 VM が参照する libvirt ネットワークの存在・アクティブ・autostart(libvirt ネットワーク方式のときだけ。macOS の user-mode では検査しない) | 無い: error、非アクティブ / autostart 無効: warn |
| `base_image:<名前>` / `pool:images` | 管理 VM が参照する base image と `images` プールの存在 | 無い: error |
| `lock_dir` | name 単位のプロセス間ロックのディレクトリに書けるか(未作成なら作れるか) | 書けない: warn(プロセス内の直列化だけになる) |
| `accelerator` | アクセラレータ | tcg: warn |
| `autostart:<VM>` | spec の autostart と実際の `dom.autostart()` の食い違い | warn(同じ spec で create / PUT し直すと収束する) |
| `orphan:<プール>/<名前>` | 孤児リソース(下記) | warn |
| `unmanaged:<プール>/<名前>` | minivps の命名に従うが、同名の管理対象外 domain があるもの | warn(gc しない) |

CLI は1行1件で出し、error が1件でもあれば**終了コード 1** で終える(warn だけなら 0)。

```console
$ mini-vps doctor
ok     network:default: アクティブ・autostart 有効
ok     base_image:ubuntu-24.04.img: 存在する(参照: web-1)
ok     lock_dir: /run/minivps/locks: 書き込める
warn   accelerator: tcg(ハードウェア支援なし。KVM/HVF より大幅に遅い)
ok     autostart: spec と domain が一致
warn   orphan:vps-pool/old-1.qcow2: domain old-1 が無い(gc --apply で回収)
```

Web API は `GET /doctor`。検査自体が動けば error を含んでいても 200 を返し、
`ok`(error が無ければ true)で全体の可否を示す: `{"ok": true, "checks": [...]}`。

## gc

孤児リソースを回収する。孤児とは、minivps の命名規則に従うのに**同名の domain が
1つも無い**もの。

| 種類 | 場所 | 名前 |
|---|---|---|
| overlay | `vps-pool` | `{vm}.qcow2` |
| snapshot | `vps-pool` | `{vm}.snap-*.qcow2` |
| seed | `vps-seeds` | `{vm}-seed.iso` |
| nwfilter | nwfilter | `minivps-{vm}`(nwfilter を使えるホストだけ) |

VM 名は `.` を含まないため、volume 名から VM 名を一意に取り出せる。命名に合わない
volume(手で置いたファイルなど)は対象にしない。同名の domain が管理対象外で存在する
場合は、その domain が使っている可能性があるため孤児とみなさない(doctor で
`unmanaged:` として警告する)。

既定は dry-run で、消す予定を出すだけ。`--apply`(API は `{"apply": true}`)で実際に消す。

```console
$ mini-vps gc
would remove: vps-pool/old-1.qcow2
would remove: vps-seeds/old-1-seed.iso
$ mini-vps gc --apply
removed: vps-pool/old-1.qcow2
removed: vps-seeds/old-1-seed.iso
```

Web API は `POST /gc`(body 省略時は dry-run)。戻り値は
`{"applied", "orphans", "removed", "skipped"}` で、skipped には消さなかった理由
(`reason`)が付く。

### 作成途中の VM を壊さないために

`create` は seed ISO と overlay を domain の define **より先に**作る。この間の VM は
一時的に孤児と同じ形に見えるため、dry-run の一覧に出ることがある。`--apply` は孤児ごとに
その VM 名の name 単位ロック(`ServerManager._locked`、プロセス間でも効く)を取り、
**ロックの内側で同名の domain が無いことを再判定してから**消す。create は provision が
終わるまでロックを持つので、gc はそれを待ち、create が define を終えていれば
`skipped`(`domain が作成された`)に回して消さない。create が失敗した場合は create
自身の巻き戻しが先に後始末する。

1件の削除に失敗しても残りは続け、失敗は `skipped` に libvirt のメッセージ付きで残す。

## ホストメトリクス

エクスポーター(`python -m mini_vps.exporter`)は VM ごとのメトリクスに加えて、
ホスト全体の容量と割当を出す。

| メトリクス | ラベル | 内容 |
|---|---|---|
| `minivps_host_memory_bytes` | | ホストの物理メモリ(`conn.getInfo()`) |
| `minivps_host_cpus` | | ホストの論理 CPU 数 |
| `minivps_allocated_memory_bytes` | | 管理 VM の memory の合計(停止中を含む。容量チェックと同じ数え方) |
| `minivps_allocated_vcpus` | | 管理 VM の vcpus の合計(停止中を含む) |
| `minivps_pool_capacity_bytes` | `pool` | ストレージプールの容量 |
| `minivps_pool_allocation_bytes` | `pool` | ストレージプールの使用量 |
| `minivps_pool_available_bytes` | `pool` | ストレージプールの空き |

プールは `vps-pool`(overlay・スナップショット)、`vps-seeds`(seed ISO)、`images`
(base image)のうち存在するものだけを出す。dir 型プールは同じファイルシステムに
置くことが多く、その場合は3つとも同じ capacity / available になる。

libvirt から値を取れなかったスクレイプは、VM のメトリクスと同じく
`minivps_exporter_scrape_success 0` にしてホストのメトリクスも出さない。

よく使う式:

```promql
# メモリ割当率(1 を超えるとオーバーコミット)
minivps_allocated_memory_bytes / minivps_host_memory_bytes
# vCPU オーバーコミット率
minivps_allocated_vcpus / minivps_host_cpus
# プール使用率
minivps_pool_allocation_bytes / minivps_pool_capacity_bytes
```

Grafana のダッシュボード(`monitoring/grafana/dashboards/minivps-overview.json`)の
「ホスト容量」の行に、この3つと、ホストメモリと割当の推移、プールの空きを置いている。
容量チェックの上限(予約とオーバーコミット率を反映した値)は API プロセスの環境変数で
決まるため、エクスポーターは出さない。
