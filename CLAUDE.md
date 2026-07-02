# CLAUDE.md

このファイルは、このリポジトリで作業する Claude Code 向けの開発ガイドです。
プロジェクトの詳しい背景・セットアップ手順は `README.md`(日本語)を参照してください。
ここでは重複を避け、コードを触るうえで必要な運用情報に絞ります。

## プロジェクト概要

QEMU/KVM + libvirt を使った**単一ホスト向けの小さな VM 制御プレーン**。
spec(YAML/JSON) → parse → XML → libvirt define/start という一方向の流れで VM を宣言的に管理する。
状態は自前 DB を持たず、libvirt domain の `<metadata>` に spec を埋め込んで往復させる。

## 主要コマンド

パッケージ管理は **uv**(`uv.lock` 追跡)。Python は **3.14 以上**が必須(`.python-version`)。

| 目的 | コマンド |
| --- | --- |
| 依存を同期 | `uv sync` |
| Lint | `uv run ruff check` |
| Format | `uv run ruff format`(CI は `--check`) |
| テスト | `uv run pytest` |
| デモ CLI | `uv run python -m mini_vps` |
| Web API | `uv run uvicorn mini_vps.api:app` (`/docs` に OpenAPI) |

## アーキテクチャ

上位から下位へ、各層は下位層の薄いラッパー。

- **`spec.py`** — 検証の真実源。Pydantic モデル `FilterRule` / `ServerSpecInput`(name 無し) /
  `ServerSpec`(name 付き, `hostname` 未指定なら `name` で補完)。`load_spec`(YAML)・`read_pubkey`。
  YAML(CLI) と JSON(API) の両入口をこの 1 モデルに収束させる設計を壊さないこと。
- **`manager.py`** — `ServerManager`。`name` を主キーに操作する管理層。`name` 単位ロックで
  create/delete/reinstall を直列化して TOCTOU を防ぐ。read(get/list/status)はロックを取らない
  (`create()` がロック内で `self.get()` を呼ぶため、read にロックを足すと非再帰 Lock で自己デッドロックする)。
  spec は libvirt `<metadata>` に埋め込み、自前 DB を持たない。例外 `ServerNotFound`/`ServerConflict`。
- **`lifecycle.py`** — `provision` / `teardown` / `wait_for_ip` / `ensure_network_active`。
- **`resources.py`** — pool / overlay volume / seed ISO / domain XML / nwfilter XML の生成。
  純粋関数(`build_domain_xml`・`build_nwfilter_xml`・`_filter_name`)と、libvirt/subprocess を伴う関数が同居。
- **`config.py`** — 定数と XML/cloud-init テンプレート。
- **入口** — CLI: `__main__.py`、Web API: `api.py`(manager の例外を HTTP ステータスへ正規化)。

## コーディング規約

- **docstring は必須・日本語・google 規約**。ruff `D` を有効化(`D105`/`D107`/`D415` のみ ignore、
  `D415` は日本語句点「。」を許すため)。line-length は 88。
- **コミットは Conventional Commits を日本語で**書く(例: `feat: nwfilter で inbound フィルタを実装する`、
  `fix: create() の TOCTOU を name 単位ロックで直列化`、`docs: ...`)。
- 入力検証は増やさず `spec.py` の `ServerSpec` に集約する。

## テスト方針

ユニットテストは**外部依存ゼロの純粋関数のみ**を対象にする(`tests/`)。
実 libvirt 接続・`cloud-localds` サブプロセス・SSH 公開鍵を要する関数(`ensure_pool`・`build_seed_iso`・
`ServerManager`・`api`)は統合実行が必要なためユニットテストの対象外。

## 外部依存・前提(統合実行時のみ)

`libvirtd`(`qemu:///system`)、base image ストレージプール `images`、`cloud-localds` バイナリ、
ebtables/iptables/arptables(nwfilter 用)、`~/.ssh/id_ed25519.pub`(cloud-init 用)。
`libvirt-python` は sdist ビルドに libvirt 開発ヘッダ(`libvirt-dev` 等)を要する。
