"""统一日志入口。

所有模块禁止使用裸 print，一律通过 get_logger(name) 获取 logger。
"""
from __future__ import annotations

import logging
import sys

_DEFAULT_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_configured = False

# 等级配色：DEBUG 绿 / INFO 白 / WARNING 黄 / ERROR 红
_LEVEL_COLORS = {
    logging.DEBUG: "\033[32m",     # 绿
    logging.INFO: "\033[37m",      # 白
    logging.WARNING: "\033[33m",   # 黄
    logging.ERROR: "\033[31m",     # 红
    logging.CRITICAL: "\033[35m",  # 品红（比 ERROR 更醒目）
}
_RESET = "\033[0m"


class ColorFormatter(logging.Formatter):
    """按日志等级着色的 Formatter。"""

    def format(self, record: logging.LogRecord) -> str:
        color = _LEVEL_COLORS.get(record.levelno, _RESET)
        message = super().format(record)
        return f"{color}{message}{_RESET}"


def setup_logging(level: int = logging.INFO) -> None:
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(ColorFormatter(_DEFAULT_FORMAT))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(level)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)
