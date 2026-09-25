"""ホストのプラットフォーム差を1か所に閉じ込めるプロファイル。

Linux(KVM / KVM 無しの TCG)と macOS(HVF)で異なるのは、libvirt の接続先・
domain type・アーキテクチャ・ネットワーク方式・パス・nwfilter の有無である。
上位層は OS を直接判定せず、`get_profile()` が返す `HostProfile` の属性だけを見る。

既定値はホストから検出し、`MINIVPS_*` 環境変数で個別に上書きできる。
"""

import dataclasses
import functools
import os
import platform
from pathlib import Path

# ネットワーク方式。libvirt の NAT ネットワーク(Linux)か、QEMU の user-mode
# ネットワーク(macOS。libvirt のネットワークドライバが無いため)か。
NETWORK_LIBVIRT = "libvirt"
NETWORK_USER = "user"

_LINUX_DATA_DIR = "/var/lib/libvirt"
_LINUX_LOCK_DIR = "/run/minivps/locks"
_DARWIN_DATA_DIR = "~/Library/Application Support/mini-vps"

# user-mode ネットワークで SSH をホストへ転送するポートの既定範囲(両端を含む)。
_DEFAULT_SSH_PORT_RANGE = (2201, 2999)


@dataclasses.dataclass(frozen=True)
class HostProfile:
    """ホストごとに変わる設定の束。

    Attributes:
        os: "linux" または "darwin"。
        arch: libvirt の arch 名("x86_64" / "aarch64")。
        accel: "kvm" / "hvf" / "tcg"。
        libvirt_uri: libvirt の接続先。
        domain_type: domain XML の `<domain type=...>`。
        machine: machine type("q35" / "virt")。
        cpu_mode: `<cpu mode=...>`。None なら `<cpu>` を出さない。
        network_mode: NETWORK_LIBVIRT または NETWORK_USER。
        supports_nwfilter: nwfilter(inbound / egress フィルタ)を使えるか。
        disk_cache: ディスクの cache 属性。None なら libvirt の既定。
        disk_io: ディスクの io 属性。None なら libvirt の既定。
        pool_path: overlay volume 用プールのディレクトリ。
        seed_dir: seed ISO 用プールのディレクトリ。
        images_dir: base image 用プール(`images`)のディレクトリ。
        lock_dir: name 単位のプロセス間ロックファイルを置くディレクトリ。
        ssh_port_range: user-mode ネットワークで SSH を転送するホストポートの範囲。
    """

    os: str
    arch: str
    accel: str
    libvirt_uri: str
    domain_type: str
    machine: str
    cpu_mode: str | None
    network_mode: str
    supports_nwfilter: bool
    disk_cache: str | None
    disk_io: str | None
    pool_path: str
    seed_dir: str
    images_dir: str
    lock_dir: str
    ssh_port_range: tuple[int, int]


def _normalize_arch(machine: str) -> str:
    """platform.machine() の値を libvirt の arch 名へ正規化する。"""
    return {"arm64": "aarch64", "amd64": "x86_64"}.get(machine.lower(), machine.lower())


def _parse_port_range(text: str) -> tuple[int, int]:
    """LOW-HIGH 形式(例: 2201-2999)の文字列をポート範囲のタプルへ変換する。"""
    low, sep, high = text.partition("-")
    if not sep:
        raise ValueError(f"MINIVPS_SSH_PORT_RANGE must be LOW-HIGH: {text!r}")
    port_range = (int(low), int(high))
    if not 1 <= port_range[0] <= port_range[1] <= 65535:
        raise ValueError(f"invalid MINIVPS_SSH_PORT_RANGE: {text!r}")
    return port_range


def _env_or_none(env: dict, key: str, default: str | None) -> str | None:
    """環境変数を読み、空文字は None(= libvirt の既定)として扱う。"""
    value = env.get(key, default)
    return value or None


def detect(
    system: str | None = None,
    machine: str | None = None,
    env: dict | None = None,
    kvm_available: bool | None = None,
) -> HostProfile:
    """ホストを検出して HostProfile を組み立てる。

    引数はテスト用の差し替え口で、既定では実ホストの値を使う。

    Args:
        system: platform.system() の値("Linux" / "Darwin")。
        machine: platform.machine() の値。
        env: 環境変数の dict。
        kvm_available: /dev/kvm が使えるか。

    Returns:
        検出結果に環境変数の上書きを適用した HostProfile。
    """
    system = (system or platform.system()).lower()
    arch = _normalize_arch(machine or platform.machine())
    env = dict(os.environ if env is None else env)

    if system == "darwin":
        home = env.get("HOME") or str(Path.home())
        data_dir = env.get("MINIVPS_DATA_DIR", _DARWIN_DATA_DIR).replace("~", home, 1)
        accel = env.get("MINIVPS_ACCEL", "hvf")
        defaults = {
            "libvirt_uri": "qemu:///session",
            "network_mode": NETWORK_USER,
            "supports_nwfilter": False,
            # io='native' は Linux の AIO 専用。macOS では libvirt の既定に任せる。
            "disk_cache": None,
            "disk_io": None,
            "pool_path": f"{data_dir}/vps-pool",
            "seed_dir": f"{data_dir}/seeds",
            "images_dir": f"{data_dir}/images",
            "lock_dir": f"{data_dir}/locks",
        }
    else:
        data_dir = env.get("MINIVPS_DATA_DIR", _LINUX_DATA_DIR)
        if kvm_available is None:
            kvm_available = os.access("/dev/kvm", os.R_OK | os.W_OK)
        accel = env.get("MINIVPS_ACCEL", "kvm" if kvm_available else "tcg")
        defaults = {
            "libvirt_uri": "qemu:///system",
            "network_mode": NETWORK_LIBVIRT,
            "supports_nwfilter": True,
            # ホストのページキャッシュとの二重キャッシュを避け、メモリをゲストに残す。
            "disk_cache": "none",
            "disk_io": "native",
            "pool_path": f"{data_dir}/vps-pool",
            "seed_dir": f"{data_dir}/seeds",
            "images_dir": f"{data_dir}/images",
            "lock_dir": _LINUX_LOCK_DIR,
        }

    domain_type = {"kvm": "kvm", "hvf": "hvf", "tcg": "qemu"}[accel]
    # host-model は KVM 専用。HVF はホスト CPU をそのまま渡し、TCG はエミュレーション
    # できる最大の CPU を使う(最近のディストリが要求する x86-64-v2 以上を満たすため)。
    cpu_mode = {"kvm": "host-model", "hvf": "host-passthrough", "tcg": "maximum"}[accel]
    machine_type = "virt" if arch == "aarch64" else "q35"

    return HostProfile(
        os=system,
        arch=arch,
        accel=accel,
        libvirt_uri=env.get("MINIVPS_LIBVIRT_URI", defaults["libvirt_uri"]),
        domain_type=domain_type,
        machine=machine_type,
        cpu_mode=cpu_mode,
        network_mode=env.get("MINIVPS_NETWORK_MODE", defaults["network_mode"]),
        supports_nwfilter=defaults["supports_nwfilter"],
        disk_cache=_env_or_none(env, "MINIVPS_DISK_CACHE", defaults["disk_cache"]),
        disk_io=_env_or_none(env, "MINIVPS_DISK_IO", defaults["disk_io"]),
        pool_path=env.get("MINIVPS_POOL_PATH", defaults["pool_path"]),
        seed_dir=env.get("MINIVPS_SEED_DIR", defaults["seed_dir"]),
        images_dir=env.get("MINIVPS_IMAGES_DIR", defaults["images_dir"]),
        lock_dir=env.get("MINIVPS_LOCK_DIR", defaults["lock_dir"]),
        ssh_port_range=_parse_port_range(env["MINIVPS_SSH_PORT_RANGE"])
        if "MINIVPS_SSH_PORT_RANGE" in env
        else _DEFAULT_SSH_PORT_RANGE,
    )


_override: HostProfile | None = None


@functools.cache
def _detected() -> HostProfile:
    """実ホストを1度だけ検出してキャッシュする。"""
    return detect()


def get_profile() -> HostProfile:
    """プロセス内で共有する HostProfile を返す。

    set_profile() で差し替えられていればそれを、無ければ実ホストの検出結果を返す。
    """
    return _override if _override is not None else _detected()


def set_profile(profile: HostProfile | None) -> None:
    """プロセス内の HostProfile を差し替える(None で実ホストの検出結果に戻す)。

    テストで特定のプラットフォームを再現するための口。検出結果のキャッシュも捨てるため、
    環境変数を変えたあとに None を渡せば再検出される。
    """
    global _override
    _override = profile
    _detected.cache_clear()
