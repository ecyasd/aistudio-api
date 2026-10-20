"""Synthetic wire payloads must not silently become replayable text history."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from aistudio_api.api.schemas import GeminiGenerateContentRequest
from aistudio_api.application import api_service_gemini as api
from aistudio_api.infrastructure.gateway.capture import CapturedRequest
from aistudio_api.infrastructure.gateway.streaming import StreamingGateway
from aistudio_api.infrastructure.gateway.wire_parser import (
    parse_response_chunk,
    parse_text_output,
)


def _part(text=None, *, field=None, value=None):
    part = [None] * 15
    part[1] = text
    if field is not None:
        if field >= len(part):
            part.extend([None] * (field + 1 - len(part)))
        part[field] = value
    return part


def _chunk(kind="plain", *, candidate_index=None):
    parts = [_part("visible text")]
    candidate = [[parts, "model"], 2, candidate_index]
    if kind == "code":
        parts.append(_part(field=8, value="synthetic_code()"))
    elif kind == "code_result":
        parts.append(_part(field=9, value=[1, "synthetic result"]))
    elif kind == "file":
        parts.append(_part(field=5, value=["offline-file-id"]))
    elif kind == "invalid_image":
        parts.append(_part(field=2, value=["image/png", "not-base64!?"]))
    elif kind == "unknown_part":
        parts.append(_part(field=18, value={"synthetic": True}))
    elif kind == "grounding":
        candidate.extend([None, None, {"syntheticGrounding": True}])
    elif kind == "citation":
        candidate.extend([None, None, None, {"syntheticCitation": True}])
    elif kind == "nonzero_index":
        candidate[2] = 1
    return [[candidate], None, [3, 2, 5]]


UNSAFE_KINDS = [
    "code", "code_result", "file", "invalid_image", "unknown_part", "grounding", "citation",
    "nonzero_index",
]


@pytest.mark.parametrize("kind", UNSAFE_KINDS)
def test_unreplayable_wire_payload_marks_candidate_and_merged_output_unsafe(kind):
    raw_chunk = _chunk(kind)
    candidate = parse_response_chunk(raw_chunk)
    assert candidate.text == "visible text"
    assert candidate.continuation_unsafe is True
    # A later plain chunk must not erase a prior safety decision while merging.
    output = parse_text_output(json.dumps([[raw_chunk, _chunk()]]))
    assert output.candidates[0].continuation_unsafe is True


@pytest.mark.parametrize(
    ("index", "unsafe"),
    [(None, False), (0, False), (1, True), (-1, True), ("0", True),
     (0.0, True), (False, True), (True, True), ({}, True), ([], True), ("", True)],
)
def test_only_default_or_strict_zero_candidate_index_is_replayable(index, unsafe):
    assert parse_response_chunk(_chunk(candidate_index=index)).continuation_unsafe is unsafe


@pytest.fixture
def wire_env(monkeypatch):
    context = SimpleNamespace(auth_file="offline/wire-account", context_id=19)
    account = SimpleNamespace(id="wire-account")
    leases = []
    released = []
    calls = []
    transport_closed = []

    @asynccontextmanager
    async def request_scope():
        assert not leases
        leases.append(context)
        try:
            yield context
        finally:
            leases.remove(context)
            released.append(context.context_id)

    monkeypatch.setattr(api, "ensure_active_account", AsyncMock())
    monkeypatch.setattr(api, "try_switch_account", AsyncMock(return_value=False))
    monkeypatch.setattr(api, "record_rotator_event", MagicMock())
    monkeypatch.setattr(api.runtime_state, "record", MagicMock())
    monkeypatch.setattr(api.runtime_state, "account_service", SimpleNamespace(
        list_accounts=lambda: [account], get_active_account=lambda: account,
        get_account_auth_path=lambda _: context.auth_file,
    ))
    monkeypatch.setattr(api.runtime_state, "rotator", SimpleNamespace(
        get_account_stats=lambda _: None,
    ))
    return SimpleNamespace(
        context=context, leases=leases, released=released, calls=calls,
        transport_closed=transport_closed, request_scope=request_scope,
    )


def _source_for(env, raw):
    async def transport(**kwargs):
        try:
            yield "status", 200
            yield "chunk", raw.encode()
        finally:
            env.transport_closed.append(True)

    gateway = StreamingGateway(SimpleNamespace(send_streaming_request=transport))
    return gateway.stream_chat(
        captured=CapturedRequest(
            url="https://synthetic.invalid/generate", headers={}, body="[]",
        ),
        model="models/gemini-offline",
    )


@pytest.mark.parametrize("kind", UNSAFE_KINDS)
async def test_gateway_emits_internal_marker_before_visible_parts(wire_env, kind):
    events = [event async for event in _source_for(wire_env, json.dumps([[_chunk(kind)]]))]
    names = [name for name, _ in events]
    assert names.count("continuation_unsafe") == 1
    assert names.index("continuation_unsafe") < names.index("parts")
    assert events[-1] == ("done", None)
    assert wire_env.transport_closed == [True]


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("kind", UNSAFE_KINDS)
async def test_internal_wire_safety_marker_never_leaks_or_starts_continuation(
    wire_env, enabled, stream, kind,
):
    env = wire_env
    raw = json.dumps([[_chunk(kind)]])

    async def generate(**kwargs):
        assert env.leases == [env.context]
        env.calls.append(kwargs)
        return parse_text_output(raw)

    def generate_stream(**kwargs):
        assert env.leases == [env.context]
        env.calls.append(kwargs)
        return _source_for(env, raw)

    client = SimpleNamespace(
        generate_content=generate, stream_generate_content=generate_stream,
        clear_templates=MagicMock(),
        session=SimpleNamespace(request_scope=env.request_scope),
    )
    request = GeminiGenerateContentRequest.model_validate({
        "contents": [{"parts": [{"text": "offline prompt"}]}],
        "tools": [], "autoContinue": enabled,
    })
    response = await api.handle_gemini_generate_content(
        "models/gemini-offline", request, client, stream=stream,
    )
    if stream:
        frames = [frame async for frame in response.body_iterator]
        serialized = "".join(frames)
        payloads = [json.loads(frame[6:]) for frame in frames if frame.startswith("data: ")]
        assert env.transport_closed == [True]
    else:
        payload = response.model_dump(mode="json", exclude_none=True)
        serialized = json.dumps(payload)
        payloads = [payload]
    assert "continuation_unsafe" not in serialized
    assert all("error" not in payload for payload in payloads)
    assert payloads[-1]["candidates"][0]["finishReason"] == "MAX_TOKENS"
    assert len(env.calls) == 1
    assert env.released == [19]
    assert not env.leases
    api.try_switch_account.assert_not_awaited()
    api.runtime_state.record.assert_called_once()
