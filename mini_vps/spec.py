"""VM スペックの定義(Pydantic)・YAML 読み込み・SSH 公開鍵の取得。

検証の真実を ServerSpec 1 箇所に集約し、YAML(CLI) と JSON(API) の
両入口を同じモデルへ収束させる。
"""

import ipaddress
import pathlib
from importlib.resources import files
from typing import Annotated, Literal

import yaml
from pydantic import (
    BaseModel,
    Field,
    StringConstraints,
    field_serializer,
    model_validator,
)

from .config import EGRESS_MAX_RULES
from .startup_scripts import STARTUP_SCRIPT_NAMES

# name/network/hostname 用。libvirt domain XML(str.format())やファイルパスへ
# そのまま埋め込まれるため、XML メタ文字・パス区切り・シェルメタ文字を一切許さない
# (RFC1123 ホスト名ラベル相当: 英数字始まり、英数字/ハイフン/アンダースコア、63文字以内)
_NAME_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$"

# user 用。startup_scripts.py の cloud-init runcmd(シェルコマンド文字列)へ未クォートで
# 展開されるため、Debian/Ubuntu の adduser が許容する POSIX ユーザー名の慣例に合わせる。
_USERNAME_PATTERN = r"^[a-z_][a-z0-9_-]{0,31}$"

# networks の要素用。_NAME_PATTERN と同じ文字種制約を list の各要素に適用する。
_NetworkName = Annotated[str, StringConstraints(pattern=_NAME_PATTERN)]

# depends_on の要素用。VM の name と同じ文字種制約を list の各要素に適用する。
_ServerName = Annotated[str, StringConstraints(pattern=_NAME_PATTERN)]

# NetworkAttachment.search の要素用。netplan の nameservers.search へ yaml.safe_dump
# 経由で埋め込むドメイン名。DNS の search ドメインは慣例上アンダースコアを含まない
# ため _NAME_PATTERN は流用せず、RFC 1123 のホスト名ラベル(英数字始まり・ハイフン可・
# 63文字以内)をドットで連結した形のみ許す(例: "minivps.internal")。
_SEARCH_DOMAIN_PATTERN = (
    r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,62})?(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,62})?)*$"
)
_SearchDomain = Annotated[
    str, StringConstraints(pattern=_SEARCH_DOMAIN_PATTERN, max_length=253)
]


class FilterRule(BaseModel):
    """inbound 許可ルール1件(単一ポート・単一プロトコル)。"""

    port: int = Field(ge=1, le=65535)
    protocol: Literal["tcp", "udp"]


class EgressRule(BaseModel):
    """egress(VM から外向きの通信)ルール1件。

    egress はリストの記述順に評価し、どれにも当たらなければ drop する。cidr は宛先の
    IPv4 ネットワークで、ホストビットが立った値(192.168.122.1/24 など)は意図が
    曖昧なため拒否する(単一ホストは /32 か接頭辞長の省略で書く)。port は宛先ポートで、
    protocol が tcp / udp のときだけ指定できる。
    """

    action: Literal["accept", "drop"] = "accept"
    cidr: ipaddress.IPv4Network
    protocol: Literal["tcp", "udp", "all"] = "all"
    port: int | None = Field(default=None, ge=1, le=65535)

    @field_serializer("cidr")
    def _serialize_cidr(self, value: ipaddress.IPv4Network) -> str:
        """metadata永続化(yaml.safe_dump)向けに文字列化する。"""
        return str(value)

    @model_validator(mode="after")
    def _validate_port_protocol(self) -> EgressRule:
        """Port は tcp / udp のときだけ許す(all にはポートの概念が無いため)。"""
        if self.port is not None and self.protocol == "all":
            raise ValueError(
                "egress の port は protocol が tcp / udp のときだけ指定できます"
            )
        return self


class StaticRoute(BaseModel):
    """ゲストに注入するスタティックルート1件(宛先ネットワークと次ホップ)。"""

    destination: ipaddress.IPv4Network
    via: ipaddress.IPv4Address

    @field_serializer("destination", "via")
    def _serialize_ip(self, value: object) -> str:
        """metadata永続化(yaml.safe_dump)向けに文字列化する。"""
        return str(value)


class NetworkAttachment(BaseModel):
    """静的IPで結線するNIC1件(ネットワーク名・アドレス・任意のゲートウェイ・DNS設定)。

    gateway は address のサブネット内にあることを検証する(同一NIC・同一セグメント内で
    あるべき値のため)。StaticRoute.viaとは異なり、運用者の決め打ちに委ねる対象ではない。

    nameservers にはサブネット内検証を掛けない。search は netplan の nameservers.search
    に渡す検索ドメインのリスト。いずれも空なら network-config にnameservers キー自体を
    出力しない。
    """

    name: _NetworkName
    address: ipaddress.IPv4Interface
    gateway: ipaddress.IPv4Address | None = None
    nameservers: list[ipaddress.IPv4Address] = Field(default_factory=list)
    search: list[_SearchDomain] = Field(default_factory=list)

    @field_serializer("address")
    def _serialize_address(self, value: ipaddress.IPv4Interface) -> str:
        """metadata永続化(yaml.safe_dump)向けに文字列化する。"""
        return str(value)

    @field_serializer("gateway")
    def _serialize_gateway(self, value: ipaddress.IPv4Address | None) -> str | None:
        """metadata永続化(yaml.safe_dump)向けに文字列化する。"""
        return str(value) if value is not None else None

    @field_serializer("nameservers")
    def _serialize_nameservers(self, value: list[ipaddress.IPv4Address]) -> list[str]:
        """metadata永続化(yaml.safe_dump)向けに文字列化する。"""
        return [str(v) for v in value]

    @model_validator(mode="after")
    def _validate_gateway_in_subnet(self) -> NetworkAttachment:
        """サブネット外を指す gateway の設定ミスを拒否する。"""
        if self.gateway is not None and self.gateway not in self.address.network:
            raise ValueError(
                f"gateway({self.gateway})がaddress({self.address})の"
                f"サブネット({self.address.network})外です"
            )
        return self


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
    networks: list[_NetworkName | NetworkAttachment] = Field(
        default_factory=lambda: ["default"], min_length=1
    )
    # None: フィルタ無し(全許可)。[]: 意図的な全 inbound 拒否。
    filters: list[FilterRule] | None = None
    # None: 外向き全許可(従来どおり)。リスト: 記述順に評価し、最後に既定 drop。
    # []: 意図的な全 outbound 拒否(DHCP と、inbound で受けた接続への戻りは除く)。
    # 件数の上限は nwfilter の priority の幅から決まる(config.EGRESS_MAX_RULES)。
    egress: list[EgressRule] | None = Field(default=None, max_length=EGRESS_MAX_RULES)
    # ゲストに注入するスタティックルート。空リストなら追加ルート無し。
    static_routes: list[StaticRoute] = Field(default_factory=list)
    # 初回起動時に適用する cloud-init テンプレート名。非秘匿のため metadata への
    # 永続化を許容する(秘密情報は別途 secrets 引数で渡し、ここには含めない)。
    startup_script: str | None = None
    # ホスト(libvirt)の起動時に VM も起動するか。稼働中でも反映できる。
    autostart: bool = True
    # 所属スタック名(stack.py)。apply --prune が「このスタックの VM か」を判定する
    # ラベルで、domain には影響しない。スタックファイル経由なら自動で補完される。
    stack: str | None = Field(default=None, pattern=_NAME_PATTERN)
    # 先に作成・起動しておく VM の name(同じスタック内、または既存の管理 VM)。
    # plan/apply の適用順を決めるだけで、domain には影響しない。
    depends_on: list[_ServerName] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_depends_on_unique(self) -> ServerSpecInput:
        """depends_on の重複を拒否する(設定ミスの可能性が高いため)。"""
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError(f"depends_on に重複があります: {self.depends_on!r}")
        return self

    @model_validator(mode="after")
    def _validate_networks_unique(self) -> ServerSpecInput:
        """同一ネットワークへの重複所属をネットワーク名で検証する(設定ミスの可能性が高いため拒否する)。"""
        names = [n if isinstance(n, str) else n.name for n in self.networks]
        if len(names) != len(set(names)):
            raise ValueError(f"networks に重複があります: {names!r}")
        return self

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

    hostname 未指定時は name で補完する。
    """

    name: str = Field(pattern=_NAME_PATTERN)

    @model_validator(mode="after")
    def _default_hostname(self) -> ServerSpec:
        """未指定なら name から hostname を補完する。"""
        if self.hostname is None:
            self.hostname = self.name
        return self

    @model_validator(mode="after")
    def _validate_not_depends_on_self(self) -> ServerSpec:
        """自分自身への depends_on を拒否する(自明な循環のため)。"""
        if self.name in self.depends_on:
            raise ValueError(f"depends_on に自分自身({self.name})は指定できません")
        return self


def load_sample_spec() -> str:
    """パッケージに同梱した vm-spec.yaml のテキストを返す。"""
    return files("mini_vps").joinpath("vm-spec.yaml").read_text()


SAMPLE_SPEC = load_sample_spec()


def ssh_identity_path() -> pathlib.Path:
    """本ツール専用の SSH 秘密鍵のパス(~/.ssh/minivps_ed25519)を返す。

    公開鍵はこのパスに .pub を付けたもの(read_pubkey 参照)。
    """
    return pathlib.Path.home() / ".ssh" / "minivps_ed25519"


def read_pubkey() -> str:
    """SSH 公開鍵を ~/.ssh/minivps_ed25519.pub から読み込んで返す。

    ユーザーの個人鍵(id_ed25519 等)とは別に、本ツール専用の鍵を使う。
    """
    pubkey_path = ssh_identity_path().with_name("minivps_ed25519.pub")
    with pubkey_path.open("r") as f:
        pubkey = f.read().strip()
    return pubkey


def load_spec(text) -> dict:
    """YAML テキストを解析し、検証済み VM スペックの dict を返す。

    必須キー検証とデフォルト補完は ServerSpec(Pydantic)に委譲する。
    """
    return ServerSpec(**yaml.safe_load(text)).model_dump()
