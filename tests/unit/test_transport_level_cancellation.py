"""Browser aborts finish before level cancellation releases request ownership."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest

from aistudio_api.infrastructure.browser.cdp_client import CDPPage
from aistudio_api.infrastructure.browser.scripts import (
    HOOKED_CLEANUP_JS,
    HOOKED_REQUEST_JS,
    STREAM_CLEANUP_JS,
    STREAMING_INIT_JS,
)
from aistudio_api.infrastructure.gateway.transport import XHRStreamTransport


@pytest.mark.parametrize("streaming", [False, True])
async def test_level_cancellation_awaits_the_bounded_browser_abort(streaming):
    page = MagicMock(spec=CDPPage)
    page.has_stream_binding = False
    page.is_closed.return_value = False
    page.get_cookies = AsyncMock(return_value=[])
    page.add_binding = AsyncMock()
    started = asyncio.Event()
    aborted = asyncio.Event()
    transport = XHRStreamTransport()
    request_script = STREAMING_INIT_JS if streaming else HOOKED_REQUEST_JS
    cleanup_script = STREAM_CLEANUP_JS if streaming else HOOKED_CLEANUP_JS

    async def evaluate(script, *_args, **_kwargs):
        if script == request_script:
            started.set()
            if not streaming:
                await asyncio.Event().wait()
        elif script == cleanup_script:
            # CDP abort is asynchronous too. Returning from cancellation before
            # this await finishes leaves the browser request running briefly.
            await asyncio.sleep(0.01)
            aborted.set()

    page.evaluate = AsyncMock(side_effect=evaluate)

    async def request():
        arguments = {
            "url": "https://synthetic.invalid", "headers": {}, "body": "[]",
            "timeout_ms": 1000,
        }
        if streaming:
            stream = transport.send_streaming_request(page, **arguments)
            try:
                await anext(stream)
            finally:
                await stream.aclose()
        else:
            await transport.send_hooked_request(page, **arguments)

    async with anyio.create_task_group() as group:
        group.start_soon(request)
        await asyncio.wait_for(started.wait(), 1)
        group.cancel_scope.cancel()

    try:
        assert aborted.is_set(), "request cancellation must await its browser abort"
        assert not transport._cleanup_tasks
        assert transport.in_flight_count == 0
        assert transport._buffered_bytes == 0
    finally:
        # Keep the pre-fix reproduction isolated, even when the assertion shows
        # that its cancelled parent failed to wait for the bounded cleanup task.
        with anyio.CancelScope(shield=True):
            await asyncio.gather(*tuple(transport._cleanup_tasks), return_exceptions=True)
