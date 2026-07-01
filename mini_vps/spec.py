"""VM スペックの定義(Pydantic)・YAML 読み込み・SSH 公開鍵の取得。

検証の真実を ServerSpec 1 箇所に集約し、YAML(CLI) と JSON(API) の
両入口を同じモデルへ収束させる。
"""

import pathlib
from importlib.resources import files
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class FilterRule(BaseModel):
    """inbound 許可ルール1件(単一ポート・単一プロトコル)。"""

    port: int = Field(ge=1, le=65535)
    protocol: Literal["tcp", "udp"]


class ServerSpecInput(BaseModel):
    """name を含まない VM スペック入力。

    API の PUT body(name は URL パスから与える) と、name 以外の共通フィールド
    定義を兼ねる。
    """

    memory: int
    vcpus: int
    base_image: str
    disk: int
    hostname: str | None = None
    user: str = "ubuntu"
    network: str = "default"
    # None: フィルタ無し(全許可)。[]: 意図的な全 inbound 拒否。
    filters: list[FilterRule] | None = None


class ServerSpec(ServerSpecInput):
    """name を含む完全な VM スペック。

    hostname 未指定時は name で補完する(従来の load_spec の挙動を踏襲)。
    """

    name: str

    @model_validator(mode="after")
    def _default_hostname(self) -> ServerSpec:
        """未指定なら name から hostname を補完する。"""
        if self.hostname is None:
            self.hostname = self.name
        return self


def load_sample_spec() -> str:
    """パッケージに同梱した vm-spec.yaml のテキストを返す。"""
    return files("mini_vps").joinpath("vm-spec.yaml").read_text()


SAMPLE_SPEC = load_sample_spec()


def read_pubkey() -> str:
    """SSH 公開鍵を ~/.ssh/id_ed25519.pub から読み込んで返す。

    Returns:
        公開鍵の文字列(末尾の改行を除去済み)。

    Raises:
        FileNotFoundError: 公開鍵ファイルが存在しない場合。
    """
    pubkey_path = pathlib.Path.home() / ".ssh" / "id_ed25519.pub"
    with pubkey_path.open("r") as f:
        pubkey = f.read().strip()
    return pubkey


def load_spec(text) -> dict:
    """YAML テキストを解析し、検証済み VM スペックの dict を返す。

    必須キー検証とデフォルト補完は ServerSpec(Pydantic)に委譲する。

    Args:
        text: vm-spec.yaml 形式の YAML 文字列。

    Returns:
        ServerSpec で正規化した dict。

    Raises:
        pydantic.ValidationError: 必須キー欠落や型不一致の場合。
    """
    return ServerSpec(**yaml.safe_load(text)).model_dump()
