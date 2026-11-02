"""Offline independent contract probes; synthetic pages and payloads only."""

import asyncio
import json
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from aistudio_api.domain.errors import RequestError
from aistudio_api.infrastructure.browser.cdp_client import CDPPage
from aistudio_api.infrastructure.browser.scripts import (
    HOOKED_CLEANUP_JS,
    HOOKED_REQUEST_JS,
    STREAM_CLEANUP_JS,
    STREAMING_INIT_JS,
)
from aistudio_api.infrastructure.gateway.capture import CapturedRequest
from aistudio_api.infrastructure.gateway.session import BrowserSession
from aistudio_api.infrastructure.gateway.session_state import SessionContext
from aistudio_api.infrastructure.gateway.streaming import StreamingGateway
from aistudio_api.infrastructure.gateway.wire_parser import (
    WIRE_PART_THOUGHT_SIGNATURE_INDEX,
    parse_text_output,
)

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("streaming", [False, True])
async def test_repeated_cancel_keeps_lease_until_browser_abort(streaming):
    session = BrowserSession(port=1)
    session._auth_file = None
    page = MagicMock(spec=CDPPage)
    page.has_stream_binding = False
    page.is_closed.return_value = False
    page.get_cookies = AsyncMock(return_value=[])
    page.add_binding = AsyncMock()
    context = SessionContext(
        page=page, snap_key="synthetic", auth_user="0", auth_file=None, base_template={}
    )
    session.set_ready_context(context)
    started, cleanup_started, finish_cleanup, aborted = [
        asyncio.Event() for _ in range(4)
    ]
    request_script = STREAMING_INIT_JS if streaming else HOOKED_REQUEST_JS
    cleanup_script = STREAM_CLEANUP_JS if streaming else HOOKED_CLEANUP_JS

    async def evaluate(script, *_args, **_kwargs):
        if script == request_script:
            started.set()
            if streaming:
                return {"started": True}
            await asyncio.Event().wait()
        elif script == cleanup_script:
            cleanup_started.set()
            await finish_cleanup.wait()
            aborted.set()
        return None

    page.evaluate = AsyncMock(side_effect=evaluate)

    async def request():
        arguments = {
            "body": "[]",
            "timeout_ms": 1000,
            "url": "https://synthetic.invalid",
            "headers": {},
        }
        if streaming:
            stream = session.send_streaming_request(**arguments)
            try:
                await anext(stream)
            finally:
                await stream.aclose()
        else:
            await session.send_hooked_request(**arguments)

    task = asyncio.create_task(request())
    await asyncio.wait_for(started.wait(), 1)
    task.cancel("client disconnected")
    await asyncio.wait_for(cleanup_started.wait(), 1)
    assert context.in_flight == 1
    task.cancel("parent shutdown also cancelled request")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    observed = {
        "task_done": task.done(),
        "lease_count": context.in_flight,
        "cleanup_tasks": len(session._transport._cleanup_tasks),
        "aborted": aborted.is_set(),
    }
    try:
        assert not task.done(), observed
        assert context.in_flight == 1, observed
    finally:
        finish_cleanup.set()
        with suppress(asyncio.CancelledError):
            await task
        await asyncio.gather(
            *tuple(session._transport._cleanup_tasks), return_exceptions=True
        )
    assert aborted.is_set()
    assert context.in_flight == 0
    assert not session._transport._cleanup_tasks


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    "signature", [{"invalid": "synthetic-shape"}, ["synthetic"], 1, False]
)
async def test_nonempty_invalid_signature_part_must_not_finish_successfully(
    streaming, signature
):
    part = [None] * 15
    part[WIRE_PART_THOUGHT_SIGNATURE_INDEX] = signature
    # Build the documented outer-batch/chunk/candidate/content structure explicitly.
    chunk = [[[[[[None, "prefix"], part], "model"], 1]], None, None]
    raw = json.dumps([[chunk]])
    if not streaming:
        with pytest.raises(RequestError):
            parse_text_output(raw)
    else:

        async def transport(**_kwargs):
            yield "status", 200
            yield "chunk", raw.encode()

        gateway = StreamingGateway(SimpleNamespace(send_streaming_request=transport))
        with pytest.raises(RequestError):
            async for _ in gateway.stream_chat(
                captured=CapturedRequest("https://synthetic.invalid", {}, "[]"),
                model="synthetic",
            ):
                pass
