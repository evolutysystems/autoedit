# ロガー生成モジュール
# 設計書 8. ログ出力方針 に対応 (Python標準 logging のみ使用)
import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler

# 既定ログフォーマット
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# ハンドラの多重登録を防ぐためのフラグ保持辞書
_configured_loggers = {}


# ファイル + コンソール出力のロガーを取得する
def get_logger(name, log_dir="logs", level="INFO"):
    # 既に構成済みのロガーであればそのまま返す
    if name in _configured_loggers:
        return _configured_loggers[name]

    logger = logging.getLogger(name)
    logger.setLevel(_resolve_level(level))
    logger.propagate = False

    formatter = logging.Formatter(_LOG_FORMAT)

    # コンソール出力ハンドラ
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # ファイル出力ハンドラ (日付別ファイル)
    try:
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, f"autoedit_{datetime.now():%Y%m%d}.log")
        file_handler = RotatingFileHandler(
            log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError:
        # ログディレクトリ作成失敗時はコンソールのみで継続
        logger.warning("ログディレクトリ作成失敗。コンソールのみで継続します: %s", log_dir)

    _configured_loggers[name] = logger
    return logger


# 文字列レベルを logging レベル値に変換する
def _resolve_level(level):
    if isinstance(level, int):
        return level
    return getattr(logging, str(level).upper(), logging.INFO)
