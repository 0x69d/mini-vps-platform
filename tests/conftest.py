import dataclasses
import logging

import libvirt
import pytest

from mini_vps.platform_profile import detect, set_profile


@pytest.fixture(autouse=True)
def _restore_minivps_logger():
    """configure() が触る mini_vps ロガーをテストごとに元へ戻す。

    CLI テストは Typer の callback 経由で実物の configure() を通るため、
    logging_config のテストに限らず全テストで復元が要る。復元しないと、
    capsys が差し替えた stderr を掴んだままのハンドラが後続テストへ残る。
    """
    logger = logging.getLogger("mini_vps")
    saved_handlers = list(logger.handlers)
    saved_level = logger.level
    yield
    logger.handlers[:] = saved_handlers
    logger.setLevel(saved_level)


@pytest.fixture(autouse=True)
def _isolate_dns_registration_env(monkeypatch):
    """DNS 自動登録の環境変数を全テストから隔離する。

    開発環境で MINIVPS_DNS_* が設定されていても、テストが実際の nsupdate を
    呼ばないようにする(test_dns_registration.py は必要な変数を自分で
    monkeypatch.setenv で設定する)。
    """
    for var in (
        "MINIVPS_DNS_SERVER",
        "MINIVPS_DNS_ZONE",
        "MINIVPS_DNS_TSIG_KEY_FILE",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _skip_capacity_check(monkeypatch):
    """ServerManager.create() の容量チェックを全テストで素通しにする。

    create() のテストの多くは素の MagicMock を libvirt 接続に使うため、実物の
    admission.check_capacity を通すと getInfo() などの戻り値が数値にならず判定が
    意味を持たない。容量チェックとの結合を検証するテストは、
    `monkeypatch.setattr("mini_vps.manager.check_capacity", ...)` で差し戻す。
    """
    monkeypatch.setattr("mini_vps.manager.check_capacity", lambda *a, **k: None)


def make_libvirt_error(code):
    """指定したエラーコードを持つ libvirt.libvirtError を作る。

    MagicMock は BaseException ではなく raise できないため、実インスタンスを
    作って get_error_code だけ差し替える。
    """
    err = libvirt.libvirtError("mock error")
    err.get_error_code = lambda: code
    return err


def linux_kvm_profile(**overrides):
    """テスト既定の HostProfile(Linux・x86_64・KVM)を返す。"""
    profile = detect(system="Linux", machine="x86_64", env={}, kvm_available=True)
    return dataclasses.replace(profile, **overrides)


def macos_profile(**overrides):
    """macOS(Apple Silicon・HVF)の HostProfile を返す。"""
    profile = detect(
        system="Darwin", machine="arm64", env={"MINIVPS_DATA_DIR": "/Users/u/mv"}
    )
    return dataclasses.replace(profile, **overrides)


@pytest.fixture(autouse=True)
def _pin_host_profile(tmp_path):
    """全テストの HostProfile を Linux/KVM に固定する。

    テストを実行するホスト(macOS の CI ランナーを含む)によって domain XML や
    ネットワーク方式が変わらないようにする。ロックファイルは tmp_path に置く。
    プラットフォーム固有の振る舞いを検証するテストは set_profile() で上書きする。
    """
    set_profile(linux_kvm_profile(lock_dir=str(tmp_path / "locks")))
    yield
    set_profile(None)
