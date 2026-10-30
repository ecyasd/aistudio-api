"""A stopped ordinary stream must not retain its slot for optional DOM work."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from aistudio_api.api.schemas import GeminiGenerateContentRequest
from aistudio_api.application import api_service_gemini as service
from aistudio_api.application.inference_limits import InferenceLimiter
from aistudio_api.infrastructure.browser.cdp_client import CDPPage
from aistudio_api.infrastructure.browser.scripts import (
    DOM_GC_CLEANUP_JS,
    STREAM_CLEANUP_JS,
    STREAMING_INIT_JS,
)
from aistudio_api.infrastructure.gateway.capture import CapturedRequest
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.gateway.session import BrowserSession
from aistudio_api.infrastructure.gateway.session_state import SessionContext


async def test_real_stream_chain_cancel_skips_fifth_dom_cleanup_and_admits_reroll(monkeypatch):
    entered, new_entered, aborted, disconnected = (asyncio.Event() for _ in range(4))
    page = MagicMock(spec=CDPPage)
    page.has_stream_binding = False
    page.is_closed.return_value = False
    page.get_cookies = AsyncMock(return_value=[])
    page.add_binding = AsyncMock()
    dom_calls = []

    async def evaluate(script, *_args, **_kwargs):
        if script == STREAMING_INIT_JS:
            (new_entered if entered.is_set() else entered).set()
        elif script == STREAM_CLEANUP_JS:
            aborted.set()
        elif script == DOM_GC_CLEANUP_JS:
            dom_calls.append(True)
            await asyncio.Event().wait()

    page.evaluate = AsyncMock(side_effect=evaluate)
    client = AIStudioClient(port=1)
    session = client.session
    context = SessionContext(page, "synthetic", "0", None, {})
    session.set_ready_context(context)
    session._stream_cleanup_count = 4
    client.capture_request = AsyncMock(return_value=CapturedRequest(
        url="https://synthetic.invalid/generate", headers={}, body="[]",
    ))
    limiter = InferenceLimiter()
    monkeypatch.setattr(service, "inference_limiter", limiter)
    monkeypatch.setattr(service, "ensure_active_account", AsyncMock())
    monkeypatch.setattr(service, "record_rotator_event", MagicMock())
    monkeypatch.setattr(service.runtime_state, "record", MagicMock())
    req = GeminiGenerateContentRequest.model_validate({"contents": [{"parts": [{"text": "synthetic"}]}]})

    async def receive():
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(_message):
        pass

    async with limiter.slot():
        old = service._build_gemini_streaming_response(client=client, req=req, model_path="gemini-synthetic")
        old_task = asyncio.create_task(old({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send))
        new_task = None
        try:
            await asyncio.wait_for(entered.wait(), 1)
            disconnected.set()
            new = service._build_gemini_streaming_response(client=client, req=req, model_path="gemini-synthetic")
            new_task = asyncio.create_task(anext(new.body_iterator))
            await asyncio.wait_for(old_task, 1)
            await asyncio.wait_for(new_entered.wait(), 1)
            assert aborted.is_set() and not dom_calls
            assert limiter.active == 2 and limiter.waiting == 0
            assert session._in_flight == context.in_flight == 1
            assert session._transport.in_flight_count == 1
        finally:
            old_task.cancel()
            if new_task is not None:
                new_task.cancel()
            await asyncio.gather(old_task, *([new_task] if new_task else []), return_exceptions=True)
            await client.close()
    assert limiter.active == limiter.waiting == 0
    assert session._in_flight == context.in_flight == 0
    assert session._transport.in_flight_count == 0


async def test_successful_stream_optional_maintenance_has_small_independent_budget():
    session = BrowserSession(port=1)
    session._in_flight = 1
    session._stream_cleanup_count = 4
    page = MagicMock(spec=CDPPage)
    page.is_closed.return_value = False

    async def stuck(*_args, **_kwargs):
        await asyncio.Event().wait()

    page.evaluate = AsyncMock(side_effect=stuck)
    await asyncio.wait_for(session.cleanup_stream_page(page), 1)
    page.evaluate.assert_awaited_once_with(DOM_GC_CLEANUP_JS, timeout_s=0.25)
