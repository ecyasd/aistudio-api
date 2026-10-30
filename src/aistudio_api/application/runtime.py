"""Application runtime state and persistent model counters."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from aistudio_api.config import resolve_stats_file, settings
from aistudio_api.infrastructure.utils.common import atomic_write_json

if TYPE_CHECKING:
    from aistudio_api.application.account_rotator import AccountRotator
    from aistudio_api.application.account_service import AccountService
    from aistudio_api.infrastructure.gateway.client import AIStudioClient

from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("state")


def _stats_counter(value: object) -> int:
    """Keep one damaged persisted counter from dropping unrelated statistics."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return 0
    try:
        return max(0, int(value))
    except (ValueError, OverflowError):
        return 0


@dataclass
class ModelStatsItem:
    requests: int = 0
    success: int = 0
    rate_limited: int = 0
    errors: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    last_used: str | None = None


@dataclass
class RuntimeState:
    client: AIStudioClient | None = None
    browser_port: int = 9222
    account_service: AccountService | None = None
    rotator: AccountRotator | None = None
    model_stats: dict[str, ModelStatsItem] = field(
        default_factory=lambda: defaultdict(ModelStatsItem)
    )
    _last_saved_time: float = field(default=0.0, repr=False)
    _save_task: asyncio.Task[None] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.load_stats()

    def load_stats(self) -> None:
        """从持久化文件加载历史调用统计。"""
        if not settings.persist_stats:
            return
        stats_path = resolve_stats_file()
        if not stats_path.is_file():
            return
        try:
            raw = stats_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                for model, item in data.items():
                    if isinstance(item, dict):
                        self.model_stats[model] = ModelStatsItem(
                            requests=_stats_counter(item.get("requests", 0)),
                            success=_stats_counter(item.get("success", 0)),
                            rate_limited=_stats_counter(item.get("rate_limited", 0)),
                            errors=_stats_counter(item.get("errors", 0)),
                            prompt_tokens=_stats_counter(item.get("prompt_tokens", 0)),
                            completion_tokens=_stats_counter(item.get("completion_tokens", 0)),
                            total_tokens=_stats_counter(item.get("total_tokens", 0)),
                            last_used=str(item["last_used"])
                            if item.get("last_used")
                            else None,
                        )
        except Exception as e:
            logger.warning("读取模型统计失败: type=%s", type(e).__name__)

    def save_stats(self) -> None:
        """将模型调用与 Token 统计持久化到文件。"""
        if not settings.persist_stats:
            return
        stats_path = resolve_stats_file()
        try:
            payload = {model: asdict(item) for model, item in self.model_stats.items()}
            atomic_write_json(stats_path, payload)
            import time

            self._last_saved_time = time.time()
        except Exception as e:
            logger.warning("持久化模型统计失败: type=%s", type(e).__name__)

    def _schedule_debounced_save(self, delay: float = 2.0) -> None:
        if not settings.persist_stats:
            return
        import asyncio
        import time

        now = time.time()
        if now - self._last_saved_time >= 5.0:
            self.save_stats()
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.save_stats()
            return

        task = getattr(self, "_save_task", None)
        if task is not None and not task.done():
            return

        async def _delayed():
            await asyncio.sleep(delay)
            self.save_stats()

        self._save_task = loop.create_task(_delayed())

    async def flush_stats(self) -> None:
        """Retire the pending timer, then persist the final complete snapshot."""
        task, self._save_task = self._save_task, None
        try:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        finally:
            self.save_stats()

    def record(
        self,
        model: str,
        result: str,
        usage: dict[str, object] | None = None,
    ) -> None:
        stats = self.model_stats[model]
        stats.requests += 1
        tz = timezone(timedelta(hours=8))
        stats.last_used = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")

        if result == "success":
            stats.success += 1
        elif result == "rate_limited":
            stats.rate_limited += 1
        elif result == "errors":
            stats.errors += 1

        if usage:
            pt = usage.get("prompt_tokens", 0)
            ct = usage.get("completion_tokens", 0)
            tt = usage.get("total_tokens", 0)
            stats.prompt_tokens += pt if isinstance(pt, int) else 0
            stats.completion_tokens += ct if isinstance(ct, int) else 0
            stats.total_tokens += tt if isinstance(tt, int) else 0
        if result in ("rate_limited", "errors"):
            self.save_stats()
        else:
            self._schedule_debounced_save()


runtime_state = RuntimeState()
