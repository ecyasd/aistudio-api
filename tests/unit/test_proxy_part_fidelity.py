"""Offline regressions for malformed frames and native Part round trips."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from aistudio_api.api import gemini as api
from aistudio_api.api.responses import to_gemini_parts
from aistudio_api.api.schemas import GeminiGenerateContentRequest
from aistudio_api.application import generation as generation_execution
from aistudio_api.application.chat_service import normalize_gemini_request
from aistudio_api.domain.errors import RequestError
from aistudio_api.infrastructure.gateway.capture import CapturedRequest
from aistudio_api.infrastructure.gateway.streaming import StreamingGateway
from aistudio_api.infrastructure.gateway.wire_parser import (
    parse_response_part,
    parse_text_output,
)
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart


def _chunk(parts=None, *, finish=None, content=None):
    if content is None:
        content = [parts or [], "model"]
    return [[[content, finish]], None, None]


def _part(slot, payload, *, thought=False, signature=None):
    part = [None] * 15
    part[slot] = payload
    part[10] = True if thought else None
    part[14] = signature
    return part


async def _stream(raw, closed):
    async def transport(**unused):
        try:
            yield "status", 200
            yield "chunk", raw.encode()
        finally:
            closed.append(True)

    gateway = StreamingGateway(SimpleNamespace(send_streaming_request=transport))
    async for event in gateway.stream_chat(
        captured=CapturedRequest(
            url="https://synthetic.invalid/generate", headers={}, body="[]",
        ),
        model="gemini-synthetic",
    ):
        yield event


@pytest.mark.parametrize("bad_content", [
    ["not an array of Parts", "model"],
    [[{"text": "undecodable"}], "model"],
    [[[[[None, "nested"]], "model"]], "model"],
    [[[None, 123]], "model"],
    123,
])
@pytest.mark.parametrize("prior_finish", [None, 1])
async def test_nonempty_malformed_content_cannot_be_completed(bad_content, prior_finish):
    raw = json.dumps([[
        _chunk([[None, "visible prefix"]], finish=prior_finish),
        _chunk(content=bad_content, finish=1),
    ]])
    with pytest.raises(RequestError) as nonstream:
        parse_text_output(raw)
    assert nonstream.value.status == 502
    received, closed = [], []
    with pytest.raises(RequestError) as stream:
        async for event in _stream(raw, closed):
            received.append(event)
    assert stream.value.status == 502
    assert [part.text for kind, parts in received if kind == "parts" for part in parts] == [
        "visible prefix",
    ]
    assert not {kind for kind, _ in received}.intersection({"done", "finish_reason"})
    assert closed == [True]


@pytest.mark.parametrize("slot", [8, 9])
@pytest.mark.parametrize("payload", [{}, [1, 123], [True, "payload"], 123])
@pytest.mark.parametrize("prior_finish", [None, 1])
async def test_invalid_known_code_payload_cannot_disappear_behind_completion(
    slot, payload, prior_finish,
):
    raw = json.dumps([[
        _chunk([[None, "visible prefix"]], finish=prior_finish),
        _chunk([_part(slot, payload)], finish=1),
    ]])
    with pytest.raises(RequestError) as nonstream:
        parse_text_output(raw)
    assert nonstream.value.status == 502
    closed, received = [], []
    with pytest.raises(RequestError) as stream:
        async for event in _stream(raw, closed):
            received.append(event)
    assert stream.value.status == 502
    assert closed == [True]
    assert not {kind for kind, _ in received}.intersection({"done", "finish_reason"})


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("code_only", [False, True])
async def test_public_response_keeps_code_parts_in_order_with_metadata(
    monkeypatch, stream, legacy, code_only,
):
    parts = [
        _part(8, "print(40 + 2)" if legacy else [1, "print(40 + 2)"],
              thought=True, signature="synthetic-code-signature"),
        _part(9, "42\n" if legacy else [1, "42\n"],
              signature="synthetic-result-signature"),
    ]
    if not code_only:
        parts = [[None, "before"], *parts, [None, "after"]]
    raw = json.dumps([[_chunk(parts, finish=2)]])
    closed, calls, released = [], [], []

    @asynccontextmanager
    async def request_scope():
        try:
            yield SimpleNamespace(
                auth_file="synthetic-auth", context_id=71, auth_generation=None,
            )
        finally:
            released.append(True)

    async def generate(**kwargs):
        calls.append(kwargs)
        return parse_text_output(raw)

    async def generate_stream(**kwargs):
        calls.append(kwargs)
        async for event in _stream(raw, closed):
            yield event

    client = SimpleNamespace(
        session=SimpleNamespace(request_scope=request_scope),
        generate_content=generate, stream_generate_content=generate_stream,
        clear_templates=MagicMock(),
    )
    monkeypatch.setattr(generation_execution, "ensure_active_account", AsyncMock())
    monkeypatch.setattr(api, "validate_account_availability", MagicMock())
    monkeypatch.setattr(generation_execution, "account_id_for_auth_file", lambda _: "synthetic-account")
    monkeypatch.setattr(api, "record_rotator_event", MagicMock())
    monkeypatch.setattr(generation_execution, "record_rotator_event", api.record_rotator_event)
    monkeypatch.setattr(api, "record_failure", MagicMock())
    monkeypatch.setattr(api.runtime_state, "record", MagicMock())
    request = GeminiGenerateContentRequest.model_validate({
        "contents": [{"parts": [{"text": "offline prompt"}]}], "tools": [],
        "autoContinue": True,
    })
    response = await api.handle_gemini_generate_content(
        "gemini-synthetic", request, client, stream=stream,
    )
    if stream:
        frames = [json.loads(frame[6:]) async for frame in response.body_iterator
                  if frame.startswith("data: ")]
        assert not any("error" in frame for frame in frames)
        public = [part for frame in frames for candidate in frame.get("candidates", [])
                  for part in candidate["content"]["parts"]]
        assert frames[-1]["candidates"][0]["finishReason"] == "MAX_TOKENS"
        assert closed == [True]
    else:
        public = [part.model_dump(exclude_none=True)
                  for part in response.candidates[0].content.parts]
        assert response.candidates[0].finishReason == "MAX_TOKENS"
    expected = [
        {"executableCode": {"language": "LANGUAGE_UNSPECIFIED" if legacy else "PYTHON",
                            "code": "print(40 + 2)"},
         "thought": True, "thoughtSignature": "synthetic-code-signature"},
        {"codeExecutionResult": {"outcome": "OUTCOME_UNSPECIFIED" if legacy else "OUTCOME_OK",
                                 "output": "42\n"},
         "thoughtSignature": "synthetic-result-signature"},
    ]
    if not code_only:
        expected = [{"text": "before"}, *expected, {"text": "after"}]
    assert public == expected
    assert len(calls) == 1
    assert released == [True]


@pytest.mark.parametrize("payload", [
    {"inlineData": {"mimeType": "image/png", "data": "c3ludGhldGlj"}},
    {"functionCall": {"name": "synthetic_tool", "args": {}, "id": "synthetic-call"}},
    {"functionResponse": {"name": "synthetic_tool", "response": {}, "id": "synthetic-call"}},
    {"executableCode": {"language": "PYTHON", "code": "print(1)"}},
    {"codeExecutionResult": {"outcome": "OUTCOME_OK", "output": "1"}},
])
def test_nontext_history_keeps_thought_and_signature(payload):
    request = GeminiGenerateContentRequest.model_validate({
        "contents": [{"role": "model", "parts": [
            {**payload, "thought": True, "thoughtSignature": "synthetic-signature"},
        ]}], "tools": [],
    })
    normalized = normalize_gemini_request(request, "gemini-synthetic")
    part = normalized.contents[0].parts[0]
    assert part.thought is True
    decoded = parse_response_part(part.to_wire())
    assert decoded.thought is True
    assert decoded.thought_signature == "synthetic-signature"


def test_reasoning_image_public_response_can_be_replayed_without_ownership_loss():
    raw = json.dumps([[_chunk([
        _part(2, ["image/png", "c3ludGhldGlj"], thought=True,
              signature="synthetic-signature"),
    ], finish=1)]])
    output = parse_text_output(raw)
    public = [part.model_dump(exclude_none=True) for part in to_gemini_parts(
        output.text, source_parts=output.candidates[0].parts,
    )]
    request = GeminiGenerateContentRequest.model_validate({
        "contents": [{"role": "model", "parts": public}], "tools": [],
    })
    normalized = normalize_gemini_request(request, "gemini-synthetic")
    replayed = parse_response_part(normalized.contents[0].parts[0].to_wire())
    assert replayed.thought is True
    assert replayed.inline_data == ("image/png", "c3ludGhldGlj")
    assert replayed.thought_signature == "synthetic-signature"


@pytest.mark.parametrize("slot,key,enum_key,text_key", [
    (8, "executableCode", "language", "code"),
    (9, "codeExecutionResult", "outcome", "output"),
])
@pytest.mark.parametrize("enum", [0, 41])
def test_code_enum_survives_response_to_request_roundtrip(slot, key, enum_key, text_key, enum):
    original = _part(slot, [enum, "synthetic payload"], thought=True,
                     signature="synthetic-signature")
    parsed = parse_text_output(json.dumps([[_chunk([original], finish=1)]]))
    public = [part.model_dump(exclude_none=True) for part in to_gemini_parts(
        "", source_parts=parsed.candidates[0].parts,
    )]
    expected_enum = (
        "LANGUAGE_UNSPECIFIED" if slot == 8 else "OUTCOME_UNSPECIFIED"
    ) if enum == 0 else enum
    assert public[0][key] == {enum_key: expected_enum, text_key: "synthetic payload"}
    request = GeminiGenerateContentRequest.model_validate({
        "contents": [{"role": "model", "parts": public}], "tools": [],
    })
    replayed = normalize_gemini_request(request, "gemini-synthetic").contents[0].parts[0].to_wire()
    assert replayed[slot] == original[slot]
    assert replayed[12] == 1
    assert replayed[14] == original[14]


@pytest.mark.parametrize("payload", [
    {"executable_code": ("UNKNOWN_LANGUAGE", "synthetic")},
    {"code_execution_result": ("UNKNOWN_OUTCOME", "synthetic")},
])
def test_unknown_code_symbol_is_rejected_instead_of_inventing_success(payload):
    with pytest.raises(ValueError, match="Unsupported"):
        AistudioPart(**payload).to_wire()


@pytest.mark.parametrize("key,enum_key,text_key", [
    ("executableCode", "language", "code"),
    ("codeExecutionResult", "outcome", "output"),
])
@pytest.mark.parametrize("enum", ["UNKNOWN_SYMBOL", True, 1.5])
def test_invalid_code_enum_is_rejected_at_request_validation(key, enum_key, text_key, enum):
    with pytest.raises(ValidationError):
        GeminiGenerateContentRequest.model_validate({
            "contents": [{"parts": [{
                key: {enum_key: enum, text_key: "synthetic payload"},
            }]}], "tools": [],
        })


def test_direct_nontext_domain_part_and_system_text_keep_thought_metadata():
    part = AistudioPart(inline_data=("image/png", "c3ludGhldGlj"), thought=True)
    assert parse_response_part(part.to_wire()).thought is True
    request = GeminiGenerateContentRequest.model_validate({
        "contents": [{"parts": [{"text": "offline"}]}], "tools": [],
        "systemInstruction": {"parts": [
            {"text": "synthetic thought", "thought": True,
             "thoughtSignature": "synthetic-signature"},
        ]},
    })
    system = normalize_gemini_request(request, "gemini-synthetic").system_instruction
    assert system is not None
    replayed = parse_response_part(system.parts[0].to_wire())
    assert replayed.thought is True
    assert replayed.thought_signature == "synthetic-signature"
