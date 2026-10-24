"""Python upstream idle deadlines exclude time suspended at consumer yields."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from aistudio_api.infrastructure.browser.cdp_client import CDPPage
from aistudio_api.infrastructure.browser.scripts import (
    STREAM_ACK_JS,
    STREAM_CLEANUP_JS,
    STREAMING_INIT_JS,
)
from aistudio_api.infrastructure.gateway.transport import XHRStreamTransport


def synthetic_page(*, return_body: bool):
    page = MagicMock(spec=CDPPage)
    page.has_stream_binding = False
    page.is_closed.return_value = False
    page.get_cookies = AsyncMock(return_value=[])
    page.add_binding = AsyncMock()
    listener = None
    rid = None
    acknowledgements = []

    def on_binding(_name, callback):
        nonlocal listener
        listener = callback

    def push(event):
        assert listener is not None and rid is not None
        listener(json.dumps({"rid": rid, **event}))

    async def evaluate(script, args=None, **_kwargs):
        nonlocal rid
        if script == STREAMING_INIT_JS:
            rid = args["rid"]
            push({"type": "status", "status": 200, "seq": 1})
        elif script == STREAM_ACK_JS:
            acknowledgements.append(args["seq"])
            if return_body:
                push(
                    {"type": "chunk", "text": "response", "seq": 2}
                    if args["seq"] == 1
                    else {"type": "done"}
                )

    page.on_binding.side_effect = on_binding
    page.evaluate = AsyncMock(side_effect=evaluate)
    return page, acknowledgements


async def test_consumer_pauses_do_not_accumulate_in_python_upstream_deadline():
    # This isolates Python's idle clock. The production browser separately
    # enforces its consumer ACK deadline, covered by browser_transport.mjs.
    page, acknowledgements = synthetic_page(return_body=True)
    transport = XHRStreamTransport()
    stream = transport.send_streaming_request(
        page, url="https://synthetic.invalid", headers={}, body="[]", timeout_ms=25
    )
    try:
        assert await anext(stream) == ("status", 200)
        await asyncio.sleep(0.06)
        assert await anext(stream) == ("chunk", b"response")
        await asyncio.sleep(0.06)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        assert acknowledgements == [1, 2]
        assert transport.in_flight_count == 0
        assert transport._buffered_bytes == 0
        assert page.evaluate.call_args.args[0] == STREAM_CLEANUP_JS
    finally:
        await stream.aclose()


async def test_upstream_silence_after_consumer_resumes_still_times_out():
    page, acknowledgements = synthetic_page(return_body=False)
    transport = XHRStreamTransport()
    stream = transport.send_streaming_request(
        page, url="https://synthetic.invalid", headers={}, body="[]", timeout_ms=25
    )
    try:
        assert await anext(stream) == ("status", 200)
        await asyncio.sleep(0.06)
        with pytest.raises(TimeoutError, match="timed out before completion"):
            await anext(stream)
        assert acknowledgements == [1]
        assert transport.in_flight_count == 0
        assert transport._buffered_bytes == 0
        assert page.evaluate.call_args.args[0] == STREAM_CLEANUP_JS
    finally:
        await stream.aclose()
