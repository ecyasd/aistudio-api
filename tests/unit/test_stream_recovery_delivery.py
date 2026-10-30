"""Lost native pushes through the real transport, parser, lease and HTTP SSE."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from aistudio_api.api import gemini
from aistudio_api.api.schemas import GeminiGenerateContentRequest
from aistudio_api.application import generation, generation_recovery as recovery
from aistudio_api.application.inference_limits import InferenceLimiter
from aistudio_api.infrastructure.browser.cdp_client import CDPPage
from aistudio_api.infrastructure.browser.scripts import (
    STREAM_ACK_JS,
    STREAM_CLEANUP_JS,
    STREAM_RESEND_JS,
    STREAM_STATE_JS,
    STREAMING_INIT_JS,
)
from aistudio_api.infrastructure.gateway import transport
from aistudio_api.infrastructure.gateway.capture import CapturedRequest
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.gateway.session_state import SessionContext


@pytest.mark.parametrize("lost_event", ["chunk", "done"])
async def test_late_original_push_preserves_recovery_for_next_loss(monkeypatch, lost_event):
    page = MagicMock(spec=CDPPage)
    page.has_stream_binding = False
    page.is_closed.return_value = False
    page.get_cookies = AsyncMock(return_value=[])
    listeners, dispatches, cleanups, resends, acknowledgements = [], [], [], [], []
    page.on_binding.side_effect = lambda _name, callback: listeners.append(callback)
    part = [None] * 15
    part[1], part[14] = "synthetic reply", "synthetic-signature"
    wire = json.dumps([[[[[[[part], "model"], 1]], None, None]]])
    events = [
        {"type": "status", "status": 200, "seq": 1},
        {"type": "chunk", "text": wire, "seq": 2},
        {"type": "done", "seq": 3},
    ]
    pending = 0

    def push(*, duplicate=False):
        payload = json.dumps({"rid": dispatches[0], **events[pending]})
        listeners[0](payload)
        if duplicate:
            listeners[1](payload)

    async def add_binding(name):
        if name == transport.STREAM_RECOVERY_BINDING and not resends and pending == 0:
            # Original delivery wins the race with installing the spare channel.
            push()

    async def evaluate(script, args=None, **_kwargs):
        nonlocal pending
        if script == STREAMING_INIT_JS:
            dispatches.append(args["rid"])
            return {"started": True}
        if script == STREAM_STATE_JS:
            return {"phase": "awaiting_ack", "seq": events[pending]["seq"], "bindingReady": True}
        if script == STREAM_RESEND_JS:
            resends.append(args["seq"])
            push(duplicate=True)
            return True
        if script == STREAM_ACK_JS:
            acknowledgements.append(args["seq"])
            pending += 1
            if pending < len(events) and events[pending]["type"] != lost_event:
                push()
        elif script == STREAM_CLEANUP_JS:
            cleanups.append(args)
        return None

    page.add_binding = AsyncMock(side_effect=add_binding)
    page.evaluate = AsyncMock(side_effect=evaluate)
    client = AIStudioClient(port=1)
    context = SessionContext(page, "synthetic", "0", None, {})
    client.session.set_ready_context(context)
    client.session.transition_to_account = AsyncMock(side_effect=AssertionError("Unexpected browser startup"))
    client.session.persist_context_cookies = AsyncMock()
    client.capture_request = AsyncMock(return_value=CapturedRequest(
        url="https://synthetic.invalid/generate", headers={}, body="[]",
    ))
    limiter = InferenceLimiter(concurrency=1, max_waiting=0)
    record = MagicMock()
    switch = AsyncMock(return_value=True)
    monkeypatch.setattr(transport, "STREAM_PROBE_INTERVAL_S", 0.005)
    monkeypatch.setattr(generation, "inference_limiter", limiter)
    monkeypatch.setattr(generation, "ensure_active_account", AsyncMock())
    monkeypatch.setattr(generation, "record_rotator_event", record)
    monkeypatch.setattr(recovery, "try_switch_account", switch)
    monkeypatch.setattr(recovery.runtime_state, "record", MagicMock())
    request = GeminiGenerateContentRequest.model_validate({
        "contents": [{"parts": [{"text": "synthetic prompt"}]}],
    })
    response = gemini._build_gemini_streaming_response(
        client=client, req=request, model_path="gemini-synthetic",
    )
    messages = []

    async def send(message):
        messages.append(message)

    async def receive():
        await asyncio.Event().wait()

    try:
        await asyncio.wait_for(response(
            {"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send,
        ), 2)
        frames = b"".join(message.get("body", b"") for message in messages).decode()
        assert '"error"' not in frames
        assert frames.count("synthetic reply") == frames.count("synthetic-signature") == 1
        assert '"finishReason": "STOP"' in frames
        assert len(dispatches) == 1 and cleanups == dispatches
        assert resends == [2 if lost_event == "chunk" else 3]
        assert acknowledgements == [1, 2, 3]
        switch.assert_not_awaited()
        record.assert_called_once()
        assert limiter.active == limiter.waiting == 0
        assert client.session._in_flight == context.in_flight == 0
        assert client.session._transport.in_flight_count == 0
        assert client.session._transport._buffered_bytes == 0
        assert not client.session._transport._cleanup_tasks
    finally:
        await client.close()
