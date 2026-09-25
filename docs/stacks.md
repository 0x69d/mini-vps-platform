# スタック(plan / apply)

エージェントの住処は1台で完結しないことが多い(内部 DNS・ルータ・エージェント VM 複数)。
複数の VM spec を1つの構成(スタック)としてファイルにまとめ、差分の表示(`plan`)と
一括適用(`apply`)を行う。実装は `mini_vps/stack.py`。

自前 DB は持たない。「どの VM がどのスタックに属するか」は各 VM の spec の `stack`
フィールドとして libvirt metadata にだけ載せる。

## スタックファイル

```yaml
stack: agents
servers:
  - name: dns-1
    memory: 1024
    vcpus: 1
    base_image: ubuntu-24.04.img
    disk: 10
  - name: agent-1
    memory: 4096
    vcpus: 2
    base_image: ubuntu-24.04.img
    disk: 30
    depends_on: [dns-1]
```

完全な例は [examples/agent-stack.yaml](../examples/agent-stack.yaml)(内部 DNS `dns-1` と、
それを名前解決に使うエージェント用 VM `agent-1`・`agent-2`)。

- `stack`: スタック名(`name` と同じ文字種制約)。
- `servers`: 1件以上の VM spec。各要素は単体の `create` に渡す spec と同じ形で、同じ検証を
  受ける([spec.md](spec.md))。

読み込み時に次を検証する。違反は `StackError`(CLI の終了コード 12、HTTP 422)。

- 各 server の `stack` は省略するとファイルの `stack` で補完される。別の値を書いていたら拒否する。
- `name` の重複を拒否する。
- `depends_on` の循環を拒否する(例: `a -> b -> a` のように経路を示す)。
- `depends_on` の参照先がスタックにも既存の管理 VM にも無ければ拒否する(`plan` / `apply` の
  開始時に既存 VM と突き合わせて検証する)。

spec の形の誤り(必須キーの欠落など)は単体の `create` と同じく検証エラー
(CLI の終了コード 1、HTTP 422)になる。

## plan

```bash
uv run mini-vps plan examples/agent-stack.yaml [--prune]
```

何も変更しない。各 server について既存 VM の spec・状態と比べ、次の action を出す。
判定は単体の `create` と同じ `planning.plan_change` を使う。

| action | 意味 |
|---|---|
| `create` | VM が無いので作る |
| `noop` | 既存 VM と一致している |
| `converge` | 差分があり、その場で反映できる(停止中の memory/vcpus/filters、稼働中でも反映できる autostart/stack/depends_on) |
| `conflict` | 再作成が必要な差分がある(`recreate_fields`)。または既存 VM が別のスタックに属している(`reason`) |
| `blocked_running` | 停止中にしか反映できない差分(`offline_fields`)があるのに稼働中 |
| `delete` | `--prune` 指定時、同じ `stack` ラベルを持つがファイルに無い管理 VM |

出力の順序は依存される側が先のトポロジカル順(依存関係が決めない順序はファイルの順)で、
`delete` はその後に逆順(依存する側が先)で並ぶ。

```json
{
  "stack": "agents",
  "changes": [
    {"name": "dns-1", "action": "noop"},
    {"name": "agent-1", "action": "converge", "fields": ["memory"], "offline_fields": ["memory"]},
    {"name": "agent-2", "action": "create"},
    {"name": "old-agent", "action": "delete"}
  ]
}
```

`stack` ラベルの無い既存 VM(スタック機能より前に作った VM など)をスタックに書くと、
`stack` フィールドの `converge` になり、稼働中でも metadata の書き換えだけで取り込める。
別のスタックに属する VM を書いた場合は、2つのスタックがラベルを奪い合い互いの `--prune` で
消し合うのを防ぐため `conflict` にする。移したい場合は先に単体の `create` / `PUT` で
`stack` を書き換える。

## apply

```bash
uv run mini-vps apply examples/agent-stack.yaml [--prune] [--wait] [--wait-timeout 300] \
  [--startup-param SERVER:KEY=VALUE ...]
```

1. plan を立て、`conflict` / `blocked_running` が1つでもあれば **何も変更せずに** 拒否する。
   メッセージにはどの VM のどのフィールドかを示す
   (例: `dns-1 (conflict: disk); agent-1 (blocked_running: vcpus)`)。
2. トポロジカル順に各 server へ `ServerManager.create` を呼ぶ(`noop` の VM にも呼ぶ。
   plan から apply までの間に変わっていても、`create` が name ロック内で判定し直す)。
3. `--prune` の `delete` は最後に逆順で `ServerManager.delete` を呼ぶ。`delete` 対象を、残る VM
   (スタック内外を問わない)が `depends_on` で参照していれば plan の段階で拒否する。

`--wait` を付けると、server を作る前にその `depends_on` の VM が `status` で `running` かつ IP を
持つまで待つ(1台あたり `--wait-timeout` 秒、既定 300 秒)。依存される VM の作成直後ではなく、
最初にそれに依存する VM を作る直前に待つため、互いに依存しない VM の起動は並行に進む。
依存先が停止している(`shutoff`)場合は待たずに失敗する。

- 静的 IP の VM は `status` が宣言値の IP を返すため、`running` になった時点で条件を満たす。
  ゲスト内のサービス(DNS サーバなど)が応答するかまでは確かめない。
- user-mode ネットワーク(macOS)ではゲストの IP を libvirt から得られないため、
  `running` だけを条件にする。

途中で失敗したら(待ちのタイムアウトを含む)そこで止め、適用済みと未適用の VM を
`StackError` で報告する。適用済みの VM は巻き戻さない。原因を直して同じファイルを再度
`apply` すれば、適用済みの VM は `noop` になり続きから収束する。

```text
error: stack error: agent-1 の適用に失敗しました (server conflict: agent-1)。適用済み: ['dns-1']、未適用: ['agent-1', 'agent-2']
```

既知の制約: 同名の **管理対象外** の domain があると plan は `create` と表示し、apply の
その VM で `server conflict` になって止まる(管理対象外の domain は `ServerManager.get` から
見えないため)。

### secrets

`startup_script` に渡す秘密情報は server ごとに渡す。CLI では `--startup-param SERVER:KEY=VALUE`
(複数回指定可、値に `=` を含んでよい)。スタックに無い server 宛ての secrets は、打ち間違いを
黙って捨てないよう拒否する。secrets は `ServerManager.create` にだけ渡し、spec・metadata・
ログ・応答には載せない。新規作成時にしか使われない点は単体の `create` と同じ。

## Web API

| メソッド | パス | body |
|---|---|---|
| POST | `/stacks/plan` | スタックファイルと同じ構造の JSON + `prune`(既定 false) |
| POST | `/stacks/apply` | 上に加えて `wait`(既定 false)・`wait_timeout`(秒、既定 300、0 より大きく 3600 以下)・`secrets`(server の name → `{KEY: VALUE}`) |

```json
{
  "stack": "agents",
  "servers": [
    {"name": "dns-1", "memory": 1024, "vcpus": 1, "base_image": "ubuntu-24.04.img", "disk": 10},
    {"name": "agent-1", "memory": 4096, "vcpus": 2, "base_image": "ubuntu-24.04.img", "disk": 30,
     "depends_on": ["dns-1"], "startup_script": "opencode-sakura-ai-engine"}
  ],
  "prune": false,
  "wait": true,
  "secrets": {"agent-1": {"AI_ENGINE_TOKEN": "..."}}
}
```

応答は CLI の出力と同じ JSON(apply は各 VM の `status` も含む)。`StackError` は 422 で、
`detail` は `stack error: ...`。`wait: true` の apply は応答まで数分かかりうる。
