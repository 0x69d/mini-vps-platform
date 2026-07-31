import logging

import libvirt
import pytest


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


def make_libvirt_error(code):
    """指定したエラーコードを持つ libvirt.libvirtError を作る。

    MagicMock は BaseException ではなく raise できないため、実インスタンスを
    作って get_error_code だけ差し替える。
    """
    err = libvirt.libvirtError("mock error")
    err.get_error_code = lambda: code
    return err
