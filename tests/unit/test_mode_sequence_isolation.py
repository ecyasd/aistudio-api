"""Signed history and continuation flags remain local to each API request."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from contextvars import ContextVar
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from aistudio_api.api import gemini as api
from aistudio_api.api.schemas import GeminiGenerateContentRequest
from aistudio_api.application import (
    chat_service,
    continuation,
    generation as generation_execution,
    generation_recovery as recovery,
)
from aistudio_api.infrastructure.gateway.model_defaults import ModelDefaults
from aistudio_api.infrastructure.gateway.wire_parser import parse_text_output

BASE_MODEL = "models/gemini-offline"
ALIAS_MODEL = "models/抗截断/gemini-offline"


def _request():
    return GeminiGenerateContentRequest.model_validate({
        "contents": [
            {"role": "model", "parts": [
                {"text": "earlier", "thoughtSignature": "synthetic-visible"},
                {"thought": True, "thoughtSignature": "synthetic-thought-only"},
                {"text": "", "thoughtSignature": "synthetic-explicit-empty"},
            ]},
            {"role": "user", "parts": [{"text": "offline prompt"}]},
        ],
        "tools": [],
        "autoContinue": False,
    })


@pytest.mark.parametrize("thought", [False, True])
@pytest.mark.parametrize("system", [False, True])
def test_signature_only_input_part_keeps_ownership_and_absent_text(thought, system):
    part = {"thought": thought, "thoughtSignature": "synthetic-signature-only"}
    payload = {"contents": [{"role": "user", "parts": [{"text": "offline"}]}]}
    if system:
        payload["systemInstruction"] = {"parts": [part]}
    else:
        payload["contents"].insert(0, {"role": "model", "parts": [part]})
    request = GeminiGenerateContentRequest.model_validate(payload)
    original = request.model_dump()
    normalized = chat_service.normalize_gemini_request(request, BASE_MODEL)
    content = normalized.system_instruction if system else normalized.contents[0]
    assert content is not None
    assert len(content.parts) == 1
    actual = content.parts[0]
    assert actual.text is None
    assert actual.inline_data is None
    assert actual.thought is thought
    assert actual.thought_signature == "synthetic-signature-only"
    wire = actual.to_wire()
    assert wire[1] is None
    assert wire[12] == (1 if thought else None)
    assert wire[14] == "synthetic-signature-only"
    assert request.model_dump() == original


def _native_response(round_index):
    parts = []
    for text, thought, signature in (
        (f"thought-{round_index}", True, f"synthetic-thought-{round_index}"),
        (f"answer-{round_index}", False, f"synthetic-answer-{round_index}"),
        (None, True, f"synthetic-tail-{round_index}"),
    ):
        part = [None] * 15
        part[1], part[12], part[14] = text, 1 if thought else None, signature
        parts.append(part)
    chunk = [[[[parts, "model"], 2 if round_index == 0 else 1]], None, [2, 1, 3]]
    return parse_text_output(json.dumps([[chunk]]))


@pytest.fixture
def sequence_env(monkeypatch):
    scope: ContextVar[int] = ContextVar("synthetic-mode-scope", default=-1)
    calls = {}
    closed = []
    next_id = 0
    parallel = False
    first_rounds = 0
    both_started = asyncio.Event()

    @asynccontextmanager
    async def request_scope():
        nonlocal next_id
        identity = next_id
        next_id += 1
        token = scope.set(identity)
        try:
            yield SimpleNamespace(auth_file="synthetic-auth", context_id=72)
        finally:
            scope.reset(token)
            closed.append(identity)

    async def generate(**kwargs):
        nonlocal first_rounds
        identity = scope.get()
        assert identity >= 0
        request_calls = calls.setdefault(identity, [])
        index = len(request_calls)
        request_calls.append(deepcopy(kwargs))
        if parallel and index == 0:
            first_rounds += 1
            if first_rounds == 2:
                both_started.set()
            await both_started.wait()
        return _native_response(index)

    async def generate_stream(**kwargs):
        output = await generate(**kwargs)
        for part in output.candidates[0].parts:
            yield "parts", [part]
        yield "finish_reason", output.candidates[0].finish_reason
        yield "usage", output.usage
        yield "done", None

    client = SimpleNamespace(
        session=SimpleNamespace(request_scope=request_scope),
        generate_content=generate,
        stream_generate_content=generate_stream,
    )
    monkeypatch.setattr(generation_execution, "ensure_active_account", AsyncMock())
    monkeypatch.setattr(api, "validate_account_availability", MagicMock())
    monkeypatch.setattr(generation_execution, "account_id_for_auth_file", lambda _: "synthetic-account")
    monkeypatch.setattr(recovery, "record_rotator_event", MagicMock())
    monkeypatch.setattr(generation_execution, "record_rotator_event", recovery.record_rotator_event)
    monkeypatch.setattr(recovery.runtime_state, "record", MagicMock())
    monkeypatch.setattr(chat_service, "resolve_model_defaults", lambda _: ModelDefaults())
    monkeypatch.setattr(continuation, "resolve_model_defaults", lambda _: ModelDefaults())

    def concurrent():
        nonlocal parallel
        parallel = True

    return SimpleNamespace(client=client, calls=calls, closed=closed, concurrent=concurrent)


async def _consume(env, request, model, stream):
    response = await api.handle_gemini_generate_content(
        model, request, env.client, stream=stream,
    )
    if stream:
        frames = [
            json.loads(frame[6:]) async for frame in response.body_iterator
            if frame.startswith("data: ")
        ]
    else:
        frames = [response.model_dump(exclude_none=True)]
    assert all("error" not in frame for frame in frames)
    parts = [
        part for frame in frames for candidate in frame["candidates"]
        for part in candidate["content"]["parts"]
    ]
    return parts, frames[-1]["candidates"][0]["finishReason"]


@pytest.mark.parametrize("alias_stream", [False, True])
@pytest.mark.parametrize("plain_stream", [False, True])
@pytest.mark.parametrize("parallel", [False, True])
async def test_alias_then_plain_or_concurrent_reuse_does_not_leak_mode_or_history(
    sequence_env, alias_stream, plain_stream, parallel,
):
    env = sequence_env
    request = _request()
    before = request.model_dump()
    if parallel:
        env.concurrent()
        alias, plain = await asyncio.wait_for(asyncio.gather(
            _consume(env, request, ALIAS_MODEL, alias_stream),
            _consume(env, request, BASE_MODEL, plain_stream),
        ), timeout=5)
    else:
        alias = await _consume(env, request, ALIAS_MODEL, alias_stream)
        plain = await _consume(env, request, BASE_MODEL, plain_stream)
    assert alias[1] == "STOP"
    assert plain[1] == "MAX_TOKENS"
    assert [part["text"] for part in alias[0] if part.get("text") and not part.get("thought")] == [
        "answer-0", "answer-1",
    ]
    assert [part["text"] for part in plain[0] if part.get("text") and not part.get("thought")] == [
        "answer-0",
    ]
    assert sorted(len(calls) for calls in env.calls.values()) == [1, 2]
    for calls in env.calls.values():
        assert all(call["model"] == BASE_MODEL for call in calls)
        first_history = calls[0]["contents"]
        assert len(first_history) == 2
        assert len(first_history[0].parts) == 3
        signed_only = first_history[0].parts[1]
        assert signed_only.text is None
        assert signed_only.thought is True
        assert signed_only.thought_signature == "synthetic-thought-only"
        if len(calls) == 2:
            assert len(calls[1]["contents"]) == 4
            assert calls[1]["contents"][:2] == first_history
            assert calls[1]["contents"][2].parts[-1].thought_signature == "synthetic-tail-0"
    assert len(env.closed) == 2
    assert request.model_dump() == before
    assert request.autoContinue is False
