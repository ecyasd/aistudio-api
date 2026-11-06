"""Unified runtime logging. Request observations live in telemetry.py."""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_UNIFIED_LOG_FORMAT = "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
_UNIFIED_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# ANSI Color codes for TTY output
_COLORS = {
    "DEBUG": "\033[36m",  # Cyan
    "INFO": "\033[32m",  # Green
    "WARNING": "\033[33m",  # Yellow
    "ERROR": "\033[31m",  # Red
    "CRITICAL": "\033[1;31m",  # Bold Red
}
_RESET = "\033[0m"


class UnifiedFormatter(logging.Formatter):
    """Unified formatter supporting optional ANSI colors on TTY streams."""

    def __init__(self, use_color: bool | None = None) -> None:
        super().__init__(fmt=_UNIFIED_LOG_FORMAT, datefmt=_UNIFIED_DATE_FORMAT)
        if use_color is None:
            use_color = hasattr(sys.stderr, "isatty") and sys.stderr.isatty()
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        orig_levelname = record.levelname
        if self.use_color and record.levelname in _COLORS:
            color = _COLORS[record.levelname]
            record.levelname = f"{color}{record.levelname}{_RESET}"
        else:
            record.levelname = record.levelname

        try:
            return super().format(record)
        finally:
            record.levelname = orig_levelname


_root_handler: logging.Handler | None = None
_current_level: str = "INFO"
_file_handler: RotatingFileHandler | None = None


def get_log_level() -> str:
    """获取当前全局配置的日志级别名称 (如 'INFO', 'DEBUG')。"""
    return _current_level


def set_log_level(level: str | int) -> None:
    """动态设置全局与 aistudio 系列日志器的日志级别。"""
    global _current_level
    if isinstance(level, int):
        level_name = logging.getLevelName(level)
    else:
        level_name = str(level).strip().upper()

    numeric_level = getattr(logging, level_name, logging.INFO)
    _current_level = level_name

    root = logging.getLogger()
    root.setLevel(numeric_level)

    aistudio_logger = logging.getLogger("aistudio")
    aistudio_logger.setLevel(numeric_level)

    # 同步更新已挂载的 handler 级别
    if _root_handler:
        _root_handler.setLevel(numeric_level)

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(logger_name).setLevel(numeric_level)

    # 非 DEBUG 模式下静默外部 HTTP 客户端的冗余网络日志
    httpx_level = logging.DEBUG if level_name == "DEBUG" else logging.WARNING
    for logger_name in ("httpx", "httpcore"):
        logging.getLogger(logger_name).setLevel(httpx_level)


def setup_logging(level: str | int = "INFO") -> None:
    """初始化全局日志系统并统一所有子系统的格式与输出。"""
    global _root_handler, _file_handler
    formatter = UnifiedFormatter()

    if _root_handler is None:
        _root_handler = logging.StreamHandler(sys.stderr)
        _root_handler.setFormatter(formatter)
        root = logging.getLogger()
        root.handlers.clear()
        root.addHandler(_root_handler)
    else:
        _root_handler.setFormatter(formatter)

    directory = os.getenv("AISTUDIO_LOG_DIR")
    if directory and _file_handler is None:
        Path(directory).mkdir(parents=True, exist_ok=True)
        _file_handler = RotatingFileHandler(
            Path(directory) / "runtime.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        _file_handler.setFormatter(UnifiedFormatter(use_color=False))
        logging.getLogger().addHandler(_file_handler)

    # 将 uvicorn 与 httpx 的日志器也接入统一格式
    for logger_name in (
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "httpx",
        "httpcore",
    ):
        lgr = logging.getLogger(logger_name)
        lgr.handlers.clear()
        if _root_handler not in lgr.handlers:
            lgr.addHandler(_root_handler)
        lgr.propagate = False

    set_log_level(level)


def get_logger(name: str = "aistudio") -> logging.Logger:
    """获取遵循统一命名空间的日志器实例。"""
    if not name.startswith("aistudio.") and name != "aistudio":
        full_name = f"aistudio.{name}"
    else:
        full_name = name
    return logging.getLogger(full_name)
