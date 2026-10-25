"""Timeout wrappers must not turn internal deadline cancellation into client aborts."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from aistudio_api.application.account_orchestrator import AccountPreparationError
from aistudio_api.application.inference_limits import InferenceLimiter
from aistudio_api.infrastructure.utils.error_records import classify_failure


@pytest.mark.parametrize("wrapper", ["none", "http", "account"])
async def test_async_deadline_preserves_timeout_classification(wrapper):
    try:
        try:
            async with asyncio.timeout(0.001):
                await asyncio.Event().wait()
        except TimeoutError as original:
            if wrapper == "http":
                raise HTTPException(504, "synthetic timeout") from original
            if wrapper == "account":
                raise AccountPreparationError("synthetic-account", original) from original
            raise
    except Exception as error:
        assert classify_failure(error)[:2] == (504, "timeout")


async def test_capacity_wait_timeout_is_not_a_client_disconnect():
    limiter = InferenceLimiter(concurrency=1, max_waiting=1, wait_seconds=0.001)
    async with limiter.slot():
        with pytest.raises(HTTPException) as rejected:
            async with limiter.slot():
                pytest.fail("the synthetic waiter must time out")
        assert rejected.value.status_code == 503
        assert classify_failure(rejected.value)[:2] == (503, "timeout")
        assert limiter.active == 1 and limiter.waiting == 0
    assert limiter.active == 0


def test_explicit_client_cancellation_stays_cancelled():
    assert classify_failure(asyncio.CancelledError())[:2] == (499, "cancelled")
