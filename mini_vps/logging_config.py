"""3つの入口層が共有するログ設定。

ライブラリ層(manager / lifecycle / resources / dns_registration)は
`logging.getLogger(__name__)` でロガーを取るだけで、出力先もレベルも決めない。
どこへどれだけ出すかを決めるのは入口層(cli / api / exporter)の責務であり、
その設定をこのモジュールに集約する。

`logging.basicConfig()` は使わない。basicConfig はルートロガーを設定するため、
import した全アプリの設定を巻き込む。代わりに "mini_vps" ロガーだけを設定し、
ルートには触れない。

uvicorn がハンドラを付けるのは "uvicorn" 系ロガーだけでルートには付けない。
そのため伝播に任せるとルートにハンドラが無く、WARNING 未満が
lastResort にも拾われず消える。3つの入口すべてで自前のハンドラを付ける。
ルートを汚さないので uvicorn 側のログと二重になることもない。

propagate は既定の True のまま残す。ルートへ伝播させても既定ではハンドラが
無いため出力は増えず、pytest の caplog がルート経由でレコードを拾える。

ログに秘密情報を載せない。spec 本文・user-data 本文・secrets は出力せず、
name や network 名など libvirt metadata に載っている値だけを出す。
"""

import logging
import os
import sys

_ROOT_LOGGER_NAME = "mini_vps"
_LEVEL_ENV_VAR = "MINIVPS_LOG_LEVEL"
_DEFAULT_LEVEL = logging.WARNING
_FORMAT = "%(levelname)-5s %(name)s %(message)s"

# configure() が付けたハンドラを識別するための目印。再呼び出しでハンドラが
# 増殖しないよう、この属性を持つハンドラの有無で判定する。pytest や他の
# ライブラリが付けたハンドラを自分のものと誤認しないため、handlers の
# 空判定ではなくこの目印を使う。
_HANDLER_MARK = "_minivps_handler"


def resolve_level(level: str | int | None = None) -> int:
    """適用するログレベルを決める。

    優先順位は引数、環境変数 MINIVPS_LOG_LEVEL、既定の WARNING。
    環境変数には "DEBUG" のような名前と "10" のような数値の両方を許す。
    解釈できない値は既定値に落とす。

    Args:
        level: 明示指定するレベル。レベル名または数値。None なら環境変数を見る。

    Returns:
        logging のレベル数値。
    """
    if level is None:
        level = os.environ.get(_LEVEL_ENV_VAR)
    if level is None:
        return _DEFAULT_LEVEL
    if isinstance(level, int):
        return level
    if level.isdigit():
        return int(level)
    resolved = logging.getLevelNamesMapping().get(level.upper())
    return _DEFAULT_LEVEL if resolved is None else resolved


def configure(level: str | int | None = None) -> None:
    """mini_vps ロガーのレベルと出力先を設定する。

    入口層のプロセス起動時に一度だけ呼ぶ。何度呼んでもハンドラは1つを超えない。

    Args:
        level: 適用するレベル。None なら環境変数 MINIVPS_LOG_LEVEL、
            それも無ければ WARNING。
    """
    logger = logging.getLogger(_ROOT_LOGGER_NAME)
    logger.setLevel(resolve_level(level))

    if any(getattr(h, _HANDLER_MARK, False) for h in logger.handlers):
        return

    # データは stdout、ログは stderr。CLI の stdout はコマンド結果専用に保つ。
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT))
    setattr(handler, _HANDLER_MARK, True)
    logger.addHandler(handler)
