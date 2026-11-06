"""Tests for runtime log formatting and level selection."""

from __future__ import annotations

import logging

from aistudio_api.infrastructure.utils.logger import (
    UnifiedFormatter,
    get_log_level,
    get_logger,
    set_log_level,
    setup_logging,
)


def test_unified_formatter_plain_and_colored():
    plain_formatter = UnifiedFormatter(use_color=False)
    color_formatter = UnifiedFormatter(use_color=True)

    record = logging.LogRecord(
        name="aistudio.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="这是一条中文测试日志",
        args=(),
        exc_info=None,
    )

    plain_output = plain_formatter.format(record)
    assert "[INFO]" in plain_output
    assert "[aistudio.test]" in plain_output
    assert "这是一条中文测试日志" in plain_output

    color_output = color_formatter.format(record)
    assert "\033[32m" in color_output  # Green color code for INFO
    assert "这是一条中文测试日志" in color_output


def test_logger_namespace_hierarchy():
    logger1 = get_logger("server")
    assert logger1.name == "aistudio.server"

    logger2 = get_logger("aistudio.gateway")
    assert logger2.name == "aistudio.gateway"

    logger3 = get_logger("aistudio")
    assert logger3.name == "aistudio"


def test_dynamic_log_level_switching():
    setup_logging("INFO")
    assert get_log_level() == "INFO"
    assert logging.getLogger("aistudio").level == logging.INFO

    set_log_level("DEBUG")
    assert get_log_level() == "DEBUG"
    assert logging.getLogger("aistudio").level == logging.DEBUG
    # Third-party protocol traces can expose response cookies and raw errors.
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
    assert not logging.getLogger("httpcore.http11").isEnabledFor(logging.DEBUG)

    set_log_level("WARNING")
    assert get_log_level() == "WARNING"
    assert logging.getLogger("aistudio").level == logging.WARNING

    set_log_level(logging.ERROR)
    assert get_log_level() == "ERROR"
    assert logging.getLogger("aistudio").level == logging.ERROR

    # 恢复默认
    set_log_level("INFO")
