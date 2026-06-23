"""VM スペック(宣言的 YAML)の読み込みと、SSH 公開鍵の取得。"""

import pathlib
from importlib.resources import files

import yaml


def load_sample_spec() -> str:
    return files("mini_vps").joinpath("vm-spec.yaml").read_text()


SAMPLE_SPEC = load_sample_spec()


def read_pubkey() -> str:
    """
    SSH 公開鍵を ~/.ssh/id_ed25519.pub から読み込んで返す。存在しない場合はエラーにする。
    """
    pubkey_path = pathlib.Path.home() / ".ssh" / "id_ed25519.pub"
    with pubkey_path.open("r") as f:
        pubkey = f.read().strip()
    return pubkey


def load_spec(text) -> dict:
    """
    YAML文字列を dict にして返す。必須キーが無ければ分かるエラーにする。
    """
    spec = yaml.safe_load(text)
    required_keys = ["name", "memory", "vcpus", "base_image", "disk"]
    for key in required_keys:
        if key not in spec:
            raise ValueError(f"Missing required key: {key}")

    spec["user"] = spec.get("user", "ubuntu")
    spec["hostname"] = spec.get("hostname", spec["name"])
    return spec
