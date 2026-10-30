"""Keep thought Parts intact across the wire parser and public SSE boundary."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from aistudio_api.api.gemini import format_sse_event
from aistudio_api.api.responses import to_gemini_parts
from aistudio_api.infrastructure.gateway.capture import CapturedRequest
from aistudio_api.infrastructure.gateway.streaming import StreamingGateway
from aistudio_api.infrastructure.gateway.wire_parser import parse_text_output


def _part(text=None, *, flag=10, signature=None):
    part = [None] * 15
    part[1] = text
    if flag is not None:
        part[flag] = 1 if flag == 12 else True
    part[14] = signature
    return part


def _chunk(parts, finish=None):
    return [[[[parts, "model"], finish]], None, None]


async def _public_stream(raw, fragment_size):
    closed = []

    async def transport(**unused):
        try:
            yield "status", 200
            encoded = raw.encode("utf-8")
            for offset in range(0, len(encoded), fragment_size):
                yield "chunk", encoded[offset:offset + fragment_size]
        finally:
            closed.append(True)

    gateway = StreamingGateway(SimpleNamespace(send_streaming_request=transport))
    public, metadata = [], []
    async for kind, payload in gateway.stream_chat(
        captured=CapturedRequest(
            url="https://synthetic.invalid/generate", headers={}, body="[]",
        ),
        model="gemini-synthetic",
    ):
        frame = format_sse_event(kind, payload)
        if frame is None:
            metadata.append((kind, payload))
            continue
        public.extend(json.loads(frame[6:])["candidates"][0]["content"]["parts"])
    assert closed == [True]
    assert ("finish_reason", 1) in metadata
    assert metadata[-1] == ("done", None)
    return public


@pytest.mark.parametrize("flag", [0, 10, 12])
async def test_thought_only_response_survives_every_byte_boundary(flag):
    text = '合成思考 🧪 "quoted" \\ escaped\nnext line'
    raw = ")]}'\n" + json.dumps([[
        _chunk([_part(text, flag=flag, signature="synthetic-thought-signature")]),
        _chunk([], finish=1),
    ]], ensure_ascii=False)
    output = parse_text_output(raw)
    assert output.text == ""
    assert output.thinking == text
    assert await _public_stream(raw, 1) == [{
        "text": text, "thought": True,
        "thoughtSignature": "synthetic-thought-signature",
    }]


async def test_mixed_thought_body_and_empty_signed_parts_keep_order():
    parts = [
        _part("first thought", signature="synthetic-first-signature"),
        _part("", signature="synthetic-empty-thought-signature"),
        _part("body", flag=None),
        _part(signature="synthetic-standalone-thought-signature"),
        _part("late thought", flag=12),
        _part("", flag=None, signature="synthetic-empty-body-signature"),
    ]
    raw = json.dumps([[_chunk(parts, finish=1)]])
    output = parse_text_output(raw)
    expected = [part.model_dump(exclude_none=True) for part in to_gemini_parts(
        output.text, source_parts=output.candidates[0].parts,
    )]
    assert expected == [
        {"text": "first thought", "thought": True,
         "thoughtSignature": "synthetic-first-signature"},
        {"text": "", "thought": True,
         "thoughtSignature": "synthetic-empty-thought-signature"},
        {"text": "body"},
        {"thought": True, "thoughtSignature": "synthetic-standalone-thought-signature"},
        {"text": "late thought", "thought": True},
        {"text": "", "thoughtSignature": "synthetic-empty-body-signature"},
    ]
    assert await _public_stream(raw, 7) == expected


async def test_long_thought_survives_fragmentation_before_body():
    text = '合成思考 🧪 "quoted" \\ escaped\n' * 16_384
    thought_parts = [_part(text[:len(text) // 2]), _part(text[len(text) // 2:], flag=12)]
    raw = json.dumps([[
        _chunk(thought_parts),
        _chunk([_part("final answer", flag=None)], finish=1),
    ]], ensure_ascii=False)
    output = parse_text_output(raw)
    public = await _public_stream(raw, 4093)
    assert output.thinking == text
    assert output.text == "final answer"
    assert "".join(part.get("text", "") for part in public if part.get("thought")) == text
    assert [part for part in public if not part.get("thought")] == [{"text": "final answer"}]
