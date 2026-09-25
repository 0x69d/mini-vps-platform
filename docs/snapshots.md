# スナップショット(チェックポイントと巻き戻し)

VM のルートディスクの状態を名前付きで保存し(スナップショット)、あとでその時点へ
巻き戻す。エージェントに危険な操作(パッケージの大規模更新、設定の書き換え、
`rm -rf` を含むスクリプトなど)をさせる前にチェックポイントを取り、失敗したら戻す、
という使い方を想定している。

## 使い方

### CLI

```sh
# チェックポイントを取る(稼働中でも停止中でも取れる)
mini-vps snapshot create web-1 before-upgrade

# guest agent でファイルシステムを凍結してから取る(稼働中のみ)
mini-vps snapshot create web-1 before-upgrade --quiesce

# 一覧(古い順。current が今の書き込み先の直前のスナップショット)
mini-vps snapshot list web-1

# 巻き戻す(稼働中なら強制停止 → 巻き戻し → 起動。より新しいスナップショットは捨てる)
mini-vps snapshot revert web-1 before-upgrade

# 削除する(今のディスクの内容は変わらない。その時点へ戻れなくなるだけ)
mini-vps snapshot delete web-1 before-upgrade
```

`list` の出力例:

```json
{
  "snapshots": [
    {
      "name": "before-upgrade",
      "created_at": "2026-09-25T01:23:45+00:00",
      "parent": null,
      "state": "disk-snapshot",
      "current": true
    }
  ]
}
```

`state` はスナップショットを取ったときの VM の状態で、稼働中・一時停止中に取ったものは
`disk-snapshot`、停止中に取ったものは `shutoff` になる。

### Web API

| メソッド | パス | 内容 |
|---|---|---|
| `POST` | `/servers/{name}/snapshots` | 作成。body は `{"name": "before-upgrade", "quiesce": false}`。201 |
| `GET` | `/servers/{name}/snapshots` | 一覧。`{"snapshots": [...]}` |
| `POST` | `/servers/{name}/snapshots/{snap}/revert` | 巻き戻し。`reverted_to`・`discarded` と `spec`・`status` を返す |
| `DELETE` | `/servers/{name}/snapshots/{snap}` | 削除。204 |

### スナップショット名

VM 名と同じ文字種(英数字で始まり、英数字・`-`・`_`、63 文字以内)で、`.` を含まない。
名前はファイル名 `{vm}.snap-{snap}.qcow2` に埋め込まれるため。

### エラー

| 状況 | 例外 | HTTP | 終了コード |
|---|---|---|---|
| VM が無い | `ServerNotFound` | 404 | 3 |
| スナップショットが無い | `SnapshotNotFound` | 404 | 10 |
| 同名のスナップショットがある・mini-vps の外で作られたスナップショットがある | `ServerConflict` | 409 | 4 |
| `quiesce` を稼働中でない VM に指定した | `ServerNotRunning` | 409 | 5 |
| 削除に要る libvirt の版が無い | `PlatformUnsupported` | 422 | 8 |
| guest agent が応答しない(`quiesce` 指定時)など libvirt のエラー | `libvirtError` | 503 | 7 |

## エージェント運用での使い方

1. 危険な操作の直前に `snapshot create` する。名前は操作の内容がわかるものにする
   (例: `before-apt-upgrade`)。書き込み中のデータを確実に残したいときは `--quiesce` を付ける。
2. 操作をさせる。
3. 結果を確かめる。
   - うまくいった: `snapshot delete` で消す。残すほどディスクを食い、ディスクの層が深くなる。
   - 失敗した: `snapshot revert` で戻す。稼働中の VM はディスクから起動し直す
     (電源を切って入れ直したのと同じ)。
4. 暴走したエージェントを止めてから戻したいときは、先に一時停止してから revert してよい。
   一時停止中の VM は、巻き戻したあとも一時停止のまま起動する(暴走が再開しない)。

revert は、対象より新しいスナップショットを捨てる(レスポンスの `discarded`)。
履歴は常に一直線で、枝分かれさせない。

## 仕組み

UEFI(pflash の NVRAM)の VM では QEMU の internal snapshot が使えないため、ルートディスク
(`vda`)だけの **外部(external)disk-only スナップショット** を使う。seed(x86 は `sda` の
cdrom、aarch64 は `vdb`)は対象外にする。

スナップショット S を取ると、その時点のディスクは凍結され、以後の書き込みは overlay 用
プール(`vps-pool`)の `{vm}.snap-{S}.qcow2` に入る。

```
base image ← web-1.qcow2 ← web-1.snap-a.qcow2 ← web-1.snap-b.qcow2(今の書き込み先)
             └ a の中身 ┘  └──── b の中身 ────┘
```

- 今の書き込み先は `{vm}.snap-{current}.qcow2`(スナップショットが無ければ `{vm}.qcow2`)。
- スナップショット S の中身は `{vm}.snap-{S}.qcow2` の1つ下の層。

| 操作 | 実装 | ファイルの変化(上の図から) |
|---|---|---|
| create c | libvirt の `snapshotCreateXML`(DISK_ONLY・ATOMIC) | `web-1.snap-c.qcow2` を上に足す |
| delete a | libvirt の `snapshotDelete`(block commit) | `web-1.snap-a.qcow2` を `web-1.qcow2` へマージして消す |
| delete b(current) | 同上(active commit) | `web-1.snap-b.qcow2` を `web-1.snap-a.qcow2` へマージして消す |
| revert a | 自前 | `web-1.snap-b.qcow2` を消し、`web-1.snap-a.qcow2` を空で作り直す |

### revert を自前で実装している理由

libvirt にも外部スナップショットへの revert(9.9.0 以降)があるが、使っていない。

1. libvirt 10.0 は、稼働中に取った disk-only スナップショット(状態 `disk-snapshot`)への
   revert を "Invalid target domain state" で拒否する(11.1 で解消)。エージェント運用では
   稼働中に取るのが普通なので、これでは使えない。
2. revert のたびに、seed を含む全ディスクへ `{元のファイル名}.{UNIX 時刻}` という名前の
   overlay を新しく作る。seed ISO が qcow2 の backing になり、ファイル名から持ち主を
   判定できなくなる(teardown や孤児検出が前提にしている命名が崩れる)。
3. domain 定義全体をスナップショット時点へ戻すため、metadata の spec や nwfilter の参照まで
   巻き戻り、spec と実体が食い違う。

自前の revert は、domain のルートディスクの参照先と overlay のファイルだけを変え、
domain 定義のほかの部分(spec・メモリ・vCPU・フィルタ)には触れない。

手順は次のとおり。途中で失敗しても、同じスナップショットへもう一度 revert すれば続きから
収束する(失敗時に VM は停止したままになり、やり直した revert はその停止状態を保つ。
必要なら `start` する)。

1. 稼働中なら強制停止(destroy)する。メモリ状態は巻き戻せないため、正常終了は待たない。
2. ルートディスクを `{vm}.snap-{S}.qcow2` へ向けて定義し直す(`<backingStore>` は外し、
   起動時に libvirt がイメージのヘッダから backing chain を調べ直すようにする)。
3. S より新しいスナップショットのメタデータを、新しい順に METADATA_ONLY で消す。
4. S より新しい overlay と S の overlay を消し、S の overlay を空で作り直す。前回の revert が
   3. の途中で失敗して残った overlay(メタデータが先に消えたもの)もここで回収する。
5. 元が稼働中なら起動し、一時停止中なら一時停止の状態で起動する。

## 既存操作との関係

| 操作 | スナップショットがあるときの挙動 |
|---|---|
| `delete` | スナップショットのメタデータ(`VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA`)と `{vm}.snap-*.qcow2` もすべて消す |
| `reinstall` | スナップショットをすべて捨てる(マージしない)。メタデータを消し、ルートディスクを `{vm}.qcow2` へ戻し、`{vm}.snap-*.qcow2` を消してから overlay を作り直す |
| `create`(収束) | memory / vCPU / filters の収束は domain XML の差分編集なので、ルートディスクの参照先(`{vm}.snap-*.qcow2`)と backing chain はそのまま残る |
| `start` / `stop` / `restart` | 影響しない |

## 制約

- **メモリ状態は含まない。** revert した VM はディスクから起動し直す。実行中のプロセスや
  tmpfs の内容は戻らない。
- **稼働中に取ったスナップショットは crash-consistent。** 電源を突然切ったときと同じ状態で、
  ジャーナリングファイルシステムなら起動時に回復する。アプリケーションの書きかけを残したく
  ないときは `--quiesce` を使う。`--quiesce` はゲストの qemu-guest-agent(cloud-init で
  導入済み)でファイルシステムを凍結してから取る。guest agent が応答しなければ失敗する
  (黙って凍結なしで取ることはしない)。停止中・一時停止中の VM には指定できない。
- **NVRAM(UEFI の変数)は含まない。** ブートエントリを書き換える操作は巻き戻らない。
- **seed(cloud-init の設定)は含まない。**
- **履歴は一直線。** 古いスナップショットへ revert すると、それより新しいスナップショットは
  消える。
- **mini-vps の外(`virsh snapshot-*` など)で作った・巻き戻したスナップショットがあると、
  作成・巻き戻し・削除を `ServerConflict` で拒否する。** 一直線と命名の前提が崩れていると、
  自前の revert と libvirt の delete のどちらも安全に動かないため。`reinstall` か `delete` で
  片付けられる。
- **libvirt の版。** 作成と巻き戻しは古い libvirt でも動く。削除(block commit でのマージ)は
  libvirt 9.0.0 以降が必要で、それより古いと `PlatformUnsupported` で拒否する
  (Ubuntu 22.04 の libvirt 8.0 など)。その場合、スナップショットは `reinstall` か `delete`
  でしか消せない。
- **停止中の VM の削除では、libvirt が QEMU を一時停止状態で一時的に起動する**
  (マージを QEMU の block job で行うため)。VM のネットワークが要るため、mini-vps が先に
  起動する。

## 容量の増え方

- スナップショットを取った直後の overlay はほぼ空で、以後にゲストが書き換えた分だけ
  (qcow2 のクラスタ単位で)大きくなる。書き換えが多い操作(パッケージ更新・ビルド)の前に
  取ったスナップショットほど、その後の層が大きくなる。
- スナップショットを残しているあいだ、同じブロックの古い版と新しい版の両方がディスクに残る。
  VM のディスク使用量は最大で「スナップショットの数 + 1」層ぶんの書き換え量の合計になる。
- 削除は上の層を下の層へマージするため、重複していたブロックの分だけ小さくなる。
  巻き戻しは捨てた層のファイルを丸ごと消すため、その分がすぐに空く。
- ゲストでの TRIM(`fstrim`)が解放するのは今の書き込み先の層だけで、下の層は小さくならない。
- 層が深いほど読み込みが遅くなりうる。使い終わったスナップショットは消す。
