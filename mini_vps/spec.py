"""VM スペックの定義(Pydantic)・YAML 読み込み・SSH 公開鍵の取得。

検証の真実を ServerSpec 1 箇所に集約し、YAML(CLI) と JSON(API) の
両入口を同じモデルへ収束させる。
"""

import pathlib
from importlib.resources import files
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from .startup_scripts import STARTUP_SCRIPT_NAMES

# name/network/hostname 用。libvirt domain XML(str.format())やファイルパスへ
# そのまま埋め込まれるため、XML メタ文字・パス区切り・シェルメタ文字を一切許さない
# (RFC1123 ホスト名ラベル相当: 英数字始まり、英数字/ハイフン/アンダースコア、63文字以内)
_NAME_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$"

# user 用。startup_scripts.py の cloud-init runcmd(シェルコマンド文字列)へ未クォートで
# 展開されるため、Debian/Ubuntu の adduser が許容する POSIX ユーザー名の慣例に合わせる。
_USERNAME_PATTERN = r"^[a-z_][a-z0-9_-]{0,31}$"


class FilterRule(BaseModel):
    """inbound 許可ルール1件(単一ポート・単一プロトコル)。"""

    port: int = Field(ge=1, le=65535)
    protocol: Literal["tcp", "udp"]


class ServerSpecInput(BaseModel):
    """name を含まない VM スペック入力。

    API の PUT body(name は URL パスから与える) と、name 以外の共通フィールド
    定義を兼ねる。
    """

    memory: int = Field(gt=0)
    vcpus: int = Field(gt=0)
    base_image: str
    disk: int = Field(gt=0)
    hostname: str | None = Field(default=None, pattern=_NAME_PATTERN)
    user: str = Field(default="ubuntu", pattern=_USERNAME_PATTERN)
    network: str = Field(default="default", pattern=_NAME_PATTERN)
    # None: フィルタ無し(全許可)。[]: 意図的な全 inbound 拒否。
    filters: list[FilterRule] | None = None
    # 初回起動時に適用する cloud-init テンプレート名。非秘匿のため metadata への
    # 永続化を許容する(秘密情報は別途 secrets 引数で渡し、ここには含めない)。
    startup_script: str | None = None

    @model_validator(mode="after")
    def _validate_startup_script(self) -> ServerSpecInput:
        """startup_script が既知のテンプレート名であることを検証する。"""
        if (
            self.startup_script is not None
            and self.startup_script not in STARTUP_SCRIPT_NAMES
        ):
            raise ValueError(
                f"unknown startup_script: {self.startup_script!r} "
                f"(known: {sorted(STARTUP_SCRIPT_NAMES)})"
            )
        return self


class ServerSpec(ServerSpecInput):
    """name を含む完全な VM スペック。

    hostname 未指定時は name で補完する(従来の load_spec の挙動を踏襲)。
    """

    name: str = Field(pattern=_NAME_PATTERN)

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
    """SSH 公開鍵を ~/.ssh/minivps_ed25519.pub から読み込んで返す。

    ユーザーの個人鍵(id_ed25519 等)とは別に、本ツール専用の鍵を使う。
    """
    pubkey_path = pathlib.Path.home() / ".ssh" / "minivps_ed25519.pub"
    with pubkey_path.open("r") as f:
        pubkey = f.read().strip()
    return pubkey


def load_spec(text) -> dict:
    """YAML テキストを解析し、検証済み VM スペックの dict を返す。

    必須キー検証とデフォルト補完は ServerSpec(Pydantic)に委譲する。
    """
    return ServerSpec(**yaml.safe_load(text)).model_dump()
