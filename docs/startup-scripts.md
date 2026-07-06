# スタートアップスクリプト

名前付き cloud-init テンプレートで VM の初期セットアップを自動化する機能。
`startup_script` に既知のテンプレート名を指定すると、VM 初回起動時の cloud-init
(`write_files`/`runcmd`)としてテンプレートの内容が適用される。

## 対応テンプレート

| テンプレート名 | 内容 |
|---|---|
| `opencode-sakura-ai-engine` | [OpenCode](https://opencode.ai)(ターミナル向け AI コーディングエージェント)をインストールし、[さくらのAI Engine](https://ai.sakura.ad.jp/sakura-ai/ai-engine/) をカスタム OpenAI 互換プロバイダとして `~/.config/opencode/opencode.json` に登録する |

`opencode-sakura-ai-engine` は secrets に `AI_ENGINE_TOKEN`(さくらのAI Engineの
アクセストークン)を必須とする。実機検証では 2GB メモリの VM で OpenCode のエージェント
機能(ファイル書き込み等のツール呼び出し)を含めて問題なく動作した。

## 秘密情報(secrets)の渡し方

さくらのAI Engineのトークンのような秘密情報は、VM の spec そのもの(`ServerSpec`)には
含めない。このリポジトリは自前 DB を持たず、spec を丸ごと libvirt domain の
`<metadata>` に永続化して `get`/`list`/`status` で平文のまま読み戻す設計のため、
spec に混ぜると秘密情報がそのまま漏洩する。secrets は VM 作成/reinstall のたびに
spec とは別経路で渡す。

CLI(`--startup-param KEY=VALUE` は複数回指定可。値側の `=` は先頭の1つだけで
分割するため保持される):

```bash
uv run mini-vps create mini_vps/vm-spec.yaml \
  --startup-param AI_ENGINE_TOKEN=<トークン>
```

Web API: `PUT /servers/{name}` の JSON body に `secrets` フィールドを足す。

```json
{
  "memory": 1024,
  "vcpus": 2,
  "base_image": "ubuntu-24.04.img",
  "disk": 10,
  "startup_script": "opencode-sakura-ai-engine",
  "secrets": { "AI_ENGINE_TOKEN": "<トークン>" }
}
```

`POST /servers/{name}/reinstall` も同様に body へ `secrets` を渡せる。

## reinstall では secrets を毎回渡し直す

secrets は libvirt の metadata に一切保存されない。そのため `reinstall` で
`startup_script` を再度実行する場合は、そのたびに `--startup-param`/`secrets`
を渡し直す必要がある。

## スコープ外(既知の制約)

- 生成した seed ISO(`{name}-seed.iso`)には秘密情報が平文で書き込まれ、ホスト上に
  残る。既存の SSH 公開鍵と同じ性質の制約であり、本プロジェクトはシークレット
  管理機構を持たない(スコープ外)。
- `--startup-param` の値はシェル履歴・`ps` の引数一覧に残りうる。

## モデルについて

`opencode-sakura-ai-engine` の既定モデルは `gpt-oss-120b`(安定版)。コーディング/
エージェント用途向けの `preview/Kimi-K2.6` も選択肢として登録されるが、プレビュー
扱いのため今後仕様が変更される可能性がある。さくらのAI Engineのモデルラインナップは
変更されることがある。最新のモデル一覧は `GET https://api.ai.sakura.ad.jp/v1/models`
で確認すること。

## トラブルシューティング

実機検証済みの確認手順:

```bash
ssh ubuntu@<VM の IP>
opencode --version
echo $AI_ENGINE_TOKEN     # ai-engine-token.env が読み込まれていれば表示される
cd <作業ディレクトリ>
opencode run "1+1は?"     # sakura-ai-engine/gpt-oss-120b で応答が返るか確認
```
