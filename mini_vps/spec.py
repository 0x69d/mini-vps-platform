"""VM スペック(宣言的 YAML)の読み込みと、SSH 公開鍵の取得。"""

import pathlib
from importlib.resources import files

import yaml


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
    """YAML テキストを解析して VM スペックの dict を返す。

    Args:
        text: vm-spec.yaml 形式の YAML 文字列。

    Returns:
        必須キーと省略可能キーのデフォルト値を補完した dict。

    Raises:
        ValueError: 必須キーが欠けている場合。
    """
    spec = yaml.safe_load(text)
    required_keys = ["name", "memory", "vcpus", "base_image", "disk"]
    for key in required_keys:
        if key not in spec:
            raise ValueError(f"Missing required key: {key}")

    spec["user"] = spec.get("user", "ubuntu")
    spec["hostname"] = spec.get("hostname", spec["name"])
    return spec
