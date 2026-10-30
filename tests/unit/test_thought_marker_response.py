"""Contentless thought markers must not start a public reasoning phase."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from aistudio_api.api import gemini as api
from aistudio_api.api.responses import to_gemini_parts
from aistudio_api.api.schemas import GeminiGenerateContentRequest
from aistudio_api.application import (
    generation as generation_execution,
    generation_recovery as recovery,
)
from aistudio_api.domain.models import ContentPart, GeneratedImage
from aistudio_api.infrastructure.gateway.capture import CapturedRequest
from aistudio_api.infrastructure.gateway.streaming import StreamingGateway
from aistudio_api.infrastructure.gateway.wire_parser import parse_text_output


def test_public_parts_filter_only_markers_without_payload_or_signature():
    retained = [
        ContentPart(text="thinking", thought=True),
        ContentPart(text="", thought=True),
        ContentPart(thought=True, thought_signature="synthetic-signature"),
        ContentPart(image=GeneratedImage("image/png", b"png", 3), thought=True),
        ContentPart(function_call={"name": "call", "args": {}}, thought=True),
        ContentPart(function_response={"name": "call", "args": {}}, thought=True),
    ]
    result = to_gemini_parts(
        "", source_parts=[ContentPart(thought=True), *retained]
    )
    assert len(result) == len(retained)
    assert [part.model_dump(exclude_none=True) for part in result[:3]] == [
        {"text": "thinking", "thought": True},
        {"text": "", "thought": True},
        {"thought": True, "thoughtSignature": "synthetic-signature"},
    ]
    assert result[3].inlineData is not None
    assert result[4].functionCall is not None
    assert result[5].functionResponse is not None


def test_contentless_thought_event_does_not_emit_empty_sse_candidate():
    assert api.format_sse_event("parts", [ContentPart(thought=True)]) is None


def _wire_part(text=None, *, thought=False):
    part = [None] * 13
    part[1] = text
    part[12] = 1 if thought else None
    return part


def _wire_chunk(part, *, finish=None):
    return [[[[[part], "model"], finish]], None, None]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("initial_marker_failure", [False, True])
async def test_plain_model_thought_trailer_does_not_reopen_reasoning_or_continue(
    monkeypatch, stream, initial_marker_failure
):
    raw = json.dumps([[
        _wire_chunk(_wire_part("thinking", thought=True)),
        _wire_chunk(_wire_part("answer")),
        _wire_chunk(_wire_part(thought=True), finish=2),
    ]])
    internal = parse_text_output(raw)
    # Retain the native marker internally for signed-history reconstruction.
    assert internal.candidates[0].parts[-1] == ContentPart(thought=True)
    responses = [raw]
    if initial_marker_failure:
        # A marker alone is not published data and must not block the existing
        # single retry when the response ends without a completion marker.
        responses.insert(0, json.dumps([[_wire_chunk(_wire_part(thought=True))]]))
    upstream_calls = []
    closed = []

    @asynccontextmanager
    async def request_scope():
        yield SimpleNamespace(auth_file="synthetic-auth", context_id=72)

    async def generate(**kwargs):
        upstream_calls.append(kwargs)
        return parse_text_output(responses[len(upstream_calls) - 1])

    async def transport(**kwargs):
        try:
            yield "status", 200
            yield "chunk", responses[len(upstream_calls) - 1].encode()
        finally:
            closed.append(True)

    gateway = StreamingGateway(SimpleNamespace(send_streaming_request=transport))

    async def generate_stream(**kwargs):
        upstream_calls.append(kwargs)
        async for event in gateway.stream_chat(
            captured=CapturedRequest(
                url="https://synthetic.invalid/generate", headers={}, body="[]"
            ),
            model=kwargs["model"],
        ):
            yield event

    client = SimpleNamespace(
        session=SimpleNamespace(request_scope=request_scope),
        generate_content=generate,
        stream_generate_content=generate_stream,
        clear_templates=MagicMock(),
    )
    monkeypatch.setattr(generation_execution, "ensure_active_account", AsyncMock())
    monkeypatch.setattr(api, "validate_account_availability", MagicMock())
    monkeypatch.setattr(generation_execution, "account_id_for_auth_file", lambda _: "synthetic-account")
    monkeypatch.setattr(recovery, "record_rotator_event", MagicMock())
    monkeypatch.setattr(generation_execution, "record_rotator_event", recovery.record_rotator_event)
    monkeypatch.setattr(api, "record_failure", MagicMock())
    monkeypatch.setattr(recovery, "record_failure", api.record_failure)
    monkeypatch.setattr(recovery.runtime_state, "record", MagicMock())
    request = GeminiGenerateContentRequest.model_validate({
        "contents": [{"role": "user", "parts": [{"text": "offline prompt"}]}],
        "tools": [],
    })
    response = await api.handle_gemini_generate_content(
        "gemini-offline", request, client, stream=stream
    )
    if stream:
        frames = [
            json.loads(frame[6:])
            async for frame in response.body_iterator
            if frame.startswith("data: ")
        ]
        assert not any("error" in frame for frame in frames)
        assert len(frames) == 3
        parts = [
            part for frame in frames for candidate in frame.get("candidates", [])
            for part in candidate["content"]["parts"]
        ]
        assert frames[-1]["candidates"][0]["finishReason"] == "MAX_TOKENS"
        assert closed == [True] * len(responses)
    else:
        parts = [
            part.model_dump(exclude_none=True)
            for part in response.candidates[0].content.parts
        ]
        assert response.candidates[0].finishReason == "MAX_TOKENS"
    assert parts == [
        {"text": "thinking", "thought": True},
        {"text": "answer"},
    ]
    assert len(upstream_calls) == len(responses)
    assert client.clear_templates.call_count == int(initial_marker_failure)
    assert request.autoContinue is False
