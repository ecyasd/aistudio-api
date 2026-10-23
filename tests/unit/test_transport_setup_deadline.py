"""A stalled CDP setup cannot outlive the request timeout or retain a queue."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from aistudio_api.infrastructure.browser.cdp_client import CDPPage
from aistudio_api.infrastructure.browser.scripts import (
    STREAM_CLEANUP_JS,
    STREAMING_INIT_JS,
)
from aistudio_api.infrastructure.gateway.transport import XHRStreamTransport


@pytest.mark.parametrize("stage", ["cookies", "binding", "initialization", "nonstream_cookies"])
async def test_stalled_setup_obeys_timeout_and_releases_owned_resources(stage):
    page = MagicMock(spec=CDPPage)
    page.has_stream_binding = False
    page.is_closed.return_value = False
    page.get_cookies = AsyncMock(return_value=[])
    page.add_binding = AsyncMock()
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def stalled(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def evaluate(script, *_args, **_kwargs):
        if script == STREAMING_INIT_JS and stage == "initialization":
            await stalled()

    page.evaluate = AsyncMock(side_effect=evaluate)
    if stage in ("cookies", "nonstream_cookies"):
        page.get_cookies.side_effect = stalled
    elif stage == "binding":
        page.add_binding.side_effect = stalled
    transport = XHRStreamTransport()
    arguments = {"url": "https://synthetic.invalid", "headers": {}, "body": "[]", "timeout_ms": 20}
    stream = None
    if stage == "nonstream_cookies":
        task = asyncio.create_task(transport.send_hooked_request(page, **arguments))
    else:
        stream = transport.send_streaming_request(page, **arguments)
        task = asyncio.create_task(anext(stream))
    try:
        await asyncio.wait_for(started.wait(), 1)
        done, _ = await asyncio.wait({task}, timeout=0.5)
        assert task in done, "request timeout must include stalled browser setup"
        with pytest.raises(TimeoutError):
            await task
        assert cancelled.is_set()
        assert transport.in_flight_count == 0
        assert transport._buffered_bytes == 0
        if stage == "initialization":
            assert any(call.args[0] == STREAM_CLEANUP_JS for call in page.evaluate.call_args_list)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if stream is not None:
            await stream.aclose()
