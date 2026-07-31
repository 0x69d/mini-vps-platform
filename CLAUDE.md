# CLAUDE.md

このファイルは、このリポジトリで作業する Claude Code 向けの開発ガイドです。
プロジェクトの詳しい背景・セットアップ手順は `README.md`(日本語)を参照してください。
ここでは重複を避け、コードを触るうえで必要な運用情報に絞ります。

## プロジェクト概要

QEMU/KVM + libvirt を使った単一ホスト向けの小さな VM 制御プレーン。
spec(YAML/JSON) → parse → XML → libvirt define/start という一方向の流れで VM を宣言的に管理する。
状態は自前 DB を持たず、libvirt domain の `<metadata>` に spec を埋め込んで往復させる。

## 主要コマンド

パッケージ管理は uv(`uv.lock` 追跡)。Python は 3.14 以上が必須(`.python-version`)。

| 目的 | コマンド |
| --- | --- |
| 依存を同期 | `uv sync` |
| Lint | `uv run ruff check` |
| Format | `uv run ruff format`(CI は `--check`) |
| テスト | `uv run pytest` |
| CLI | `uv run mini-vps --help`(`uv run python -m mini_vps` も同一) |
| Web API | `uv run uvicorn mini_vps.api:app` (`/docs` に OpenAPI) |
| Prometheus エクスポーター | `uv run python -m mini_vps.exporter` (既定 `127.0.0.1:9177/metrics`) |

## アーキテクチャ

上位から下位へ、各層は下位層の薄いラッパー。

- `spec.py` — 検証の真実源。Pydantic モデル `FilterRule` / `ServerSpecInput`(name 無し) /
  `ServerSpec`(name 付き, `hostname` 未指定なら `name` で補完)。`load_spec`(YAML)・`read_pubkey`。
  YAML(CLI) と JSON(API) の両入口をこの 1 モデルに収束させる設計を壊さないこと。
- `startup_scripts.py` — 名前付き cloud-init テンプレート。テンプレート名から
  `write_files`/`runcmd` フラグメントを組み立てる。秘密情報(secrets)はここでのみ user-data へ
  展開され、spec/metadata には一切載せない。例外 `StartupScriptError`。
- `manager.py` — `ServerManager`。`name` を主キーに操作する管理層。書き込み系操作
  (create/delete/start/stop/restart/reinstall)を `name` 単位ロックで直列化して TOCTOU を防ぐ。
  read(get/list/status)はロックを取らない。
  `create()` がロック内で `self.get()` を呼ぶため、read にロックを足すと非再帰 Lock で自己デッドロックする。
  spec は libvirt `<metadata>` に埋め込み、自前 DB を持たない。
  例外 `ServerNotFound`/`ServerConflict`/`ServerNotRunning`。
- `lifecycle.py` — `provision` / `teardown` / `wait_for_ip` / `ensure_network_active`。
- `dns_registration.py` — nsupdate subprocess による A/PTR の自動登録。opt-in・ベストエフォートで、
  manager の create/delete/reinstall から呼ばれ例外を伝播させない。`docs/dns-registration.md` 参照。
- `resources.py` — pool / overlay volume / seed ISO / domain XML / nwfilter XML の生成。
  純粋関数(`build_domain_xml`・`build_nwfilter_xml`・`_filter_name`)と、libvirt/subprocess を伴う関数が同居。
- `config.py` — 定数(`LIBVIRT_URI` 含む)と XML/cloud-init テンプレート。
- `logging_config.py` — 入口層が共有するログ設定。`configure(level)` / `resolve_level(level)`。
- 入口 — CLI: `cli.py`(manager の例外を終了コードへ正規化。`__main__.py` は `cli.run` への
  shim)、Web API: `api.py`(manager の例外を HTTP ステータスへ正規化)、
  Prometheus エクスポーター: `exporter.py`(`ServerManager` を読み取り専用で再利用し、
  `conn.getAllDomainStats()` の一括統計を `prometheus_client` の Custom Collector として公開)。
  CLI と Web API はどちらも `ServerManager` の薄いラッパーという対称な関係にある。

## ログ

層で役割を分ける。ライブラリ層(`manager`/`lifecycle`/`resources`/`dns_registration`)は
`logging.getLogger(__name__)` を持つだけで出力先もレベルも決めない。決めるのは入口層で、
`logging_config.configure()` を1度だけ呼ぶ。ライブラリ層で `basicConfig` を呼ぶと、
import した全アプリのログ設定を上書きするため禁止。

`configure()` はルートではなく `mini_vps` ロガーだけを設定し、そこに stderr ハンドラを付ける。
uvicorn がハンドラを付けるのは `uvicorn` 系ロガーだけでルートには付けないため、伝播に任せると
INFO 以下が出力先を持たず消える。3入口とも同じ `configure()` を呼ぶ。`propagate` は True のまま
残す(ルートに既定でハンドラが無いので出力は増えず、`caplog` がレコードを拾える)。
CLI は `-v`(INFO)/`-vv`(DEBUG)、レベルの既定は環境変数 `MINIVPS_LOG_LEVEL`、
未設定なら WARNING。

出力先の分離を壊さないこと。CLI の stdout はコマンド結果専用で、ログは stderr へ出す。

> [!WARNING]
> spec 本文・user-data 本文・secrets はログに出さない。出してよいのは name・network 名など
> libvirt metadata に既に載っている値だけ。`startup_scripts.py` が守っている
> 「secrets を spec/metadata に載せない」という不変条件は、ログから容易に破れる。

## コーディング規約

- docstring は必須・日本語・google 規約。ruff `D` を有効化し、`D105`/`D107`/`D415` のみ ignore する。
  `D415` を ignore するのは日本語句点「。」を許すため。line-length は 88。
- コミットは Conventional Commits を日本語で書く(例: `feat: nwfilter で inbound フィルタを実装する`、
  `fix: create() の TOCTOU を name 単位ロックで直列化`、`docs: ...`)。
- 入力検証は増やさず `spec.py` の `ServerSpec` に集約する。

## テスト方針

外部依存ゼロの純粋関数は素の値でテストする(`spec.py` の検証ロジック・`startup_scripts.py` の
テンプレート組み立て・`resources.py` の
`build_domain_xml`/`build_nwfilter_xml`/`_filter_name`・`exporter.py` の `_parse_domain_stats`)。
libvirt 接続・subprocess に依存する関数は `unittest.mock`(`MagicMock`/`monkeypatch`)で
外部呼び出しを差し替えてユニットテスト化する(`manager.py`・`lifecycle.py`・`resources.py` の残り・
`exporter.py` の `DomainCollector.collect`)。`api.py` は `fastapi.testclient.TestClient` +
`dependency_overrides` で HTTP 層を検証する。`cli.py` は `main(argv, manager_factory=...)` の
`manager_factory` に `ServerManager` の Mock を返すコンテキストマネージャを注入して検証する
(`dependency_overrides` の CLI 版)。実 libvirtd・`cloud-localds` バイナリを要する結合的な
動作確認は、別途手動または統合実行で行う。

ログは `caplog` フィクスチャで検証する。`mini_vps` ロガーの handlers と level は
`tests/conftest.py` の autouse フィクスチャ `_restore_minivps_logger` が全テストで
退避・復元する(CLI テストは Typer の callback 経由で実物の `configure()` を通るため、
logging のテストに限らず汚染が起きる)。
secrets がログに漏れないことは DEBUG レベルで検証する
(`test_manager.py::test_create_does_not_log_secrets`)。

## 外部依存・前提(統合実行時のみ)

`libvirtd`(`qemu:///system`)、base image ストレージプール `images`、`cloud-localds` バイナリ、
ebtables/iptables/arptables(nwfilter 用)、`~/.ssh/minivps_ed25519.pub`(cloud-init 用)、
OVMF/edk2-ovmf(UEFI firmware 自動選択用)。
これらのホスト事前設定は `ansible/playbook.yml` で自動化している(`README.md` 参照)。
`libvirt-python` は sdist ビルドに libvirt 開発ヘッダ(`libvirt-dev` 等)を要する。
