import logging

from mini_vps.logging_config import _HANDLER_MARK, configure, resolve_level

# mini_vps ロガーの退避・復元は conftest.py の _restore_minivps_logger が行う。
_LOGGER_NAME = "mini_vps"


def _own_handlers(logger):
    return [h for h in logger.handlers if getattr(h, _HANDLER_MARK, False)]


# --- resolve_level ---


def test_resolve_level_defaults_to_warning(monkeypatch):
    monkeypatch.delenv("MINIVPS_LOG_LEVEL", raising=False)
    assert resolve_level() == logging.WARNING


def test_resolve_level_reads_env_var(monkeypatch):
    monkeypatch.setenv("MINIVPS_LOG_LEVEL", "debug")
    assert resolve_level() == logging.DEBUG


def test_resolve_level_accepts_numeric_env_var(monkeypatch):
    monkeypatch.setenv("MINIVPS_LOG_LEVEL", "10")
    assert resolve_level() == logging.DEBUG


def test_resolve_level_argument_wins_over_env(monkeypatch):
    monkeypatch.setenv("MINIVPS_LOG_LEVEL", "DEBUG")
    assert resolve_level("ERROR") == logging.ERROR


def test_resolve_level_falls_back_on_unknown_name(monkeypatch):
    monkeypatch.delenv("MINIVPS_LOG_LEVEL", raising=False)
    assert resolve_level("SHOUT") == logging.WARNING


# --- configure ---


def test_configure_sets_level_and_attaches_handler(monkeypatch):
    monkeypatch.delenv("MINIVPS_LOG_LEVEL", raising=False)
    logger = logging.getLogger(_LOGGER_NAME)
    logger.handlers[:] = []

    configure("INFO")

    assert logger.level == logging.INFO
    assert len(_own_handlers(logger)) == 1


def test_configure_is_idempotent(monkeypatch):
    monkeypatch.delenv("MINIVPS_LOG_LEVEL", raising=False)
    logger = logging.getLogger(_LOGGER_NAME)
    logger.handlers[:] = []

    configure("INFO")
    configure("DEBUG")

    # ハンドラは増えず、レベルだけが更新される。
    assert len(_own_handlers(logger)) == 1
    assert logger.level == logging.DEBUG


def test_configure_ignores_foreign_handlers(monkeypatch):
    """他ライブラリや pytest が付けたハンドラを自分のものと誤認しない。"""
    monkeypatch.delenv("MINIVPS_LOG_LEVEL", raising=False)
    logger = logging.getLogger(_LOGGER_NAME)
    logger.handlers[:] = [logging.NullHandler()]

    configure("INFO")

    assert len(_own_handlers(logger)) == 1
    assert len(logger.handlers) == 2


def test_configure_keeps_propagation_enabled(monkeypatch):
    """伝播は切らない。ルートに既定でハンドラが無いので出力は増えない。"""
    monkeypatch.delenv("MINIVPS_LOG_LEVEL", raising=False)
    configure("INFO")

    assert logging.getLogger(_LOGGER_NAME).propagate is True


def test_configure_emits_under_uvicorn_log_config(monkeypatch, capsys):
    """uvicorn のログ設定下でも INFO が消えず、かつ二重出力にならない。

    uvicorn がハンドラを付けるのは "uvicorn" 系ロガーだけでルートには付けない。
    伝播だけに頼るとルートに出力先が無く、WARNING 未満は lastResort にも
    拾われずに消える。
    """
    import logging.config

    from uvicorn.config import LOGGING_CONFIG

    monkeypatch.delenv("MINIVPS_LOG_LEVEL", raising=False)
    logger = logging.getLogger(_LOGGER_NAME)
    logger.handlers[:] = []

    # pytest 自身がルートにハンドラを付けているため、実プロセスの初期状態を
    # 再現するにはいったん外す必要がある。
    root = logging.getLogger()
    saved_root_handlers = list(root.handlers)
    root.handlers[:] = []
    try:
        logging.config.dictConfig(LOGGING_CONFIG)
        # uvicorn の設定を適用してもルートにハンドラは増えない。
        assert root.handlers == []

        configure("INFO")
        logging.getLogger("mini_vps.manager").info("疎通確認")
    finally:
        root.handlers[:] = saved_root_handlers
        # dictConfig は uvicorn 系ロガーをプロセス全体に設定するため、
        # 後続のテストへ影響が残らないよう明示的に外す。
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            logging.getLogger(name).handlers[:] = []

    assert capsys.readouterr().err.count("疎通確認") == 1


def test_configure_handler_writes_to_stderr(monkeypatch):
    """ログは stderr へ。stdout は CLI のコマンド結果専用に空けておく。"""
    import sys

    monkeypatch.delenv("MINIVPS_LOG_LEVEL", raising=False)
    logger = logging.getLogger(_LOGGER_NAME)
    logger.handlers[:] = []

    configure("INFO")

    assert _own_handlers(logger)[0].stream is sys.stderr
