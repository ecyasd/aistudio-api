"""Bound inference work without occupying management request capacity."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import HTTPException

from aistudio_api.infrastructure.utils.response_diagnostics import note_timing


class InferenceLimiter:
    def __init__(self, concurrency: int = 2, max_waiting: int = 4, wait_seconds: float = 10):
        self._slots = asyncio.Semaphore(concurrency)
        self.max_waiting = max_waiting
        self.wait_seconds = wait_seconds
        self.waiting = 0
        self.active = 0

    @staticmethod
    def _busy() -> HTTPException:
        return HTTPException(
            503, detail={"message": "Inference capacity is busy. Please retry shortly.", "type": "service_busy"},
            headers={"Retry-After": "2"},
        )

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        queued = self._slots.locked()
        if queued:
            if self.waiting >= self.max_waiting:
                raise self._busy()
            self.waiting += 1
        try:
            try:
                async with asyncio.timeout(self.wait_seconds):
                    await self._slots.acquire()
            except TimeoutError as exc:
                raise self._busy() from exc
        finally:
            if queued:
                self.waiting -= 1
        self.active += 1
        try:
            note_timing("admitted")
            yield
        finally:
            self.active -= 1
            self._slots.release()


inference_limiter = InferenceLimiter()
