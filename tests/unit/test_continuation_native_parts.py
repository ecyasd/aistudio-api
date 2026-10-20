"""Native signed Parts retain their original order and response-turn ownership."""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from aistudio_api.application import continuation
from aistudio_api.application.chat_service import NormalizedGeminiRequest
from aistudio_api.domain.models import Candidate, ContentPart, ModelOutput
from aistudio_api.infrastructure.gateway.capture import CapturedRequest
from aistudio_api.infrastructure.gateway.model_defaults import ModelDefaults
from aistudio_api.infrastructure.gateway.streaming import StreamingGateway
from aistudio_api.infrastructure.gateway.wire_parser import parse_text_output
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart


def request(**overrides):
    values = {
        "model": "models/plain",
        "contents": [AistudioContent("user", [AistudioPart(text="question")])],
        "system_instruction": None,
        "tools": [[None, [["available_tool"]]]],
        "tool_config": [None, [2]],
        "safety_settings": None,
        "capture_prompt": "question",
        "capture_images": None,
    }
    values.update(overrides)
    return NormalizedGeminiRequest(**values)


def native(parts, finish=2):
    return ModelOutput(
        candidates=[
            Candidate(
                text="".join(part.text or "" for part in parts if not part.thought),
                thinking="".join(part.text or "" for part in parts if part.thought),
                thought_signature=next(
                    (
                        p.thought_signature
                        for p in reversed(parts)
                        if p.thought_signature
                    ),
                    "",
                ),
                parts=parts,
                finish_reason=finish,
            )
        ]
    )


class Client:
    def __init__(self, rounds):
        self.rounds = rounds
        self.calls = []
        self.closed = []

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return self.rounds[len(self.calls) - 1]

    async def stream_generate_content(self, **kwargs):
        index = len(self.calls)
        self.calls.append(kwargs)
        try:
            result = self.rounds[index]
            if isinstance(result, list):
                for event in result:
                    yield event
                return
            candidate = result.candidates[0]
            for part in candidate.parts:
                yield "parts", [part]
            yield "finish_reason", candidate.finish_reason
            yield "usage", result.usage
            yield "done", None
        finally:
            self.closed.append(index)


@pytest.fixture(autouse=True)
def defaults(monkeypatch):
    monkeypatch.setattr(
        continuation, "resolve_model_defaults", lambda _model: ModelDefaults()
    )


async def run(client, normalized, streaming):
    if streaming:
        return [
            event
            async for event in continuation.stream_generate_content(
                client, normalized, auto_continue=True
            )
        ]
    return await continuation.generate_content(client, normalized, auto_continue=True)


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("mode", [None, 1, 2, 3])
async def test_signed_thinking_rounds_keep_exact_parts_and_separate_model_turns(
    streaming, mode
):
    first = [
        ContentPart(
            text="thinking one", thought=True, thought_signature="thought-sig-1"
        ),
        ContentPart(text="first ", thought_signature="visible-sig-1"),
        ContentPart(text="", thought_signature="empty-sig-1"),
    ]
    second = [
        ContentPart(
            text="thinking two", thought=True, thought_signature="thought-sig-2"
        ),
        ContentPart(text=" second", thought_signature="visible-sig-2"),
    ]
    last = [ContentPart(text="done", thought_signature="visible-sig-3")]
    rounds = [native(first), native(second), native(last, 1)]
    original_rounds = deepcopy(rounds)
    normalized = request(tool_config=None if mode is None else [None, [mode]])
    original_request = deepcopy(normalized)
    client = Client(rounds)
    result = await run(client, normalized, streaming)
    assert len(client.calls) == 3
    assert normalized == original_request
    assert rounds == original_rounds
    for call in client.calls:
        assert call["tools"] == original_request.tools
        assert call["tool_config"] == original_request.tool_config
    assert [c.role for c in client.calls[2]["contents"]] == [
        "user",
        "model",
        "user",
        "model",
        "user",
    ]
    for call_index, content_index, expected_parts in (
        (1, 1, first),
        (2, 1, first),
        (2, 3, second),
    ):
        actual = client.calls[call_index]["contents"][content_index].parts
        assert [(p.text, p.thought, p.thought_signature) for p in actual] == [
            (p.text, p.thought, p.thought_signature or None) for p in expected_parts
        ]
    assert client.calls[1]["contents"] is not client.calls[2]["contents"]
    if streaming:
        assert [
            p for kind, payload in result if kind == "parts" for p in payload
        ] == first + second + last
        assert [event for event in result if event[0] == "finish_reason"] == [
            ("finish_reason", 1)
        ]
        assert client.closed == [0, 1, 2]
    else:
        assert result.candidates[0].parts == first + second + last
        assert result.text == "first  seconddone"
        assert result.thinking == "thinking onethinking two"
        assert result.candidates[0].thought_signature == "visible-sig-3"


@pytest.mark.parametrize("streaming", [False, True])
async def test_signed_input_and_system_parts_are_copied_without_changing_ownership(
    streaming,
):
    normalized = request(
        contents=[
            AistudioContent(
                "model",
                [
                    AistudioPart(
                        text="prior thought",
                        thought=True,
                        thought_signature="prior-thought",
                    )
                ],
            ),
            AistudioContent(
                "user", [AistudioPart(text="next", thought_signature="prior-user")]
            ),
        ],
        system_instruction=AistudioContent(
            "system", [AistudioPart(text="rule", thought_signature="system-sig")]
        ),
    )
    original = deepcopy(normalized)
    client = Client(
        [
            native([ContentPart(text="A", thought_signature="output-sig")]),
            native([ContentPart(text="B")], 1),
        ]
    )
    await run(client, normalized, streaming)
    assert len(client.calls) == 2
    assert client.calls[1]["contents"][:2] == original.contents
    assert client.calls[1]["contents"][0] is not normalized.contents[0]
    assert client.calls[1]["system_instruction_content"] == original.system_instruction
    assert normalized == original


@pytest.mark.parametrize("streaming", [False, True])
async def test_thought_only_token_exhaustion_does_not_spend_another_round(streaming):
    parts = [
        ContentPart(
            text="only reasoning", thought=True, thought_signature="thought-only"
        )
    ]
    client = Client([native(parts)])
    result = await run(client, request(), streaming)
    assert len(client.calls) == 1
    if streaming:
        assert result[-3:] == [("finish_reason", 2), ("usage", None), ("done", None)]
    else:
        assert result.candidates[0].parts == parts
        assert result.candidates[0].finish_reason == 2


@pytest.mark.parametrize(
    "candidate",
    [
        Candidate(
            text="A",
            parts=[ContentPart(text="A")],
            finish_reason=2,
            continuation_unsafe=True,
        ),
        Candidate(text="A", thinking="orphan thought", finish_reason=2),
        Candidate(text="A", thought_signature="orphan signature", finish_reason=2),
        Candidate(
            text="A",
            parts=[ContentPart(text="B", thought_signature="bound")],
            finish_reason=2,
        ),
        Candidate(
            text="A",
            thinking="different",
            parts=[ContentPart(text="thought", thought=True), ContentPart(text="A")],
            finish_reason=2,
        ),
        Candidate(
            text="A",
            thought_signature="orphan",
            parts=[ContentPart(text="A", thought_signature="bound")],
            finish_reason=2,
        ),
    ],
)
async def test_ambiguous_summary_or_signature_ownership_is_not_guessed(candidate):
    upstream = ModelOutput(candidates=[candidate])
    client = Client([upstream])
    assert await run(client, request(), False) is upstream
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "mixed",
    [
        [
            ("body", "legacy"),
            ("parts", [ContentPart(text="native", thought_signature="sig")]),
        ],
        [
            ("parts", [ContentPart(text="native", thought_signature="sig")]),
            ("body", "legacy"),
        ],
        [("body", "legacy"), ("thought_signature", "unbound")],
        [
            ("parts", [ContentPart(text="native", thought_signature="sig")]),
            ("thought_signature", "unbound"),
        ],
    ],
)
async def test_stream_mixed_formats_and_unbound_signature_events_stop_continuation(
    mixed,
):
    round_events = [*mixed, ("finish_reason", 2), ("usage", None), ("done", None)]
    client = Client([round_events])
    result = await run(client, request(), True)
    assert result == round_events
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "auto_continue, excluded", [(False, False), (True, False), (True, True)]
)
async def test_internal_unsafe_marker_is_filtered_in_every_mode(
    auto_continue, excluded
):
    round_events = [
        ("parts", [ContentPart(text="genuine")]),
        ("continuation_unsafe", True),
        ("finish_reason", 2),
        ("usage", None),
        ("done", None),
    ]
    normalized = request(
        capture_images=[("image/png", "synthetic")] if excluded else None
    )
    client = Client([round_events])
    result = [
        event
        async for event in continuation.stream_generate_content(
            client, normalized, auto_continue=auto_continue
        )
    ]
    assert result == [
        event for event in round_events if event[0] != "continuation_unsafe"
    ]
    assert len(client.calls) == 1


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("overflow", ["thinking", "signature", "empty_parts"])
async def test_thinking_signatures_and_empty_parts_all_consume_retention_budget(
    monkeypatch, streaming, overflow
):
    monkeypatch.setattr(continuation, "MAX_CONTINUATION_BYTES", 1024)
    if overflow == "thinking":
        parts = [ContentPart(text="x" * 1024, thought=True), ContentPart(text="A")]
    elif overflow == "signature":
        parts = [ContentPart(text="A", thought_signature="x" * 1024)]
    else:
        parts = [*[ContentPart(text="") for _ in range(12)], ContentPart(text="A")]
    upstream = native(parts)
    client = Client([upstream])
    result = await run(client, request(), streaming)
    assert len(client.calls) == 1
    if streaming:
        assert [
            p for kind, payload in result if kind == "parts" for p in payload
        ] == parts
        assert result[-3][1] == 2
    else:
        assert result is upstream


@pytest.mark.parametrize("streaming", [False, True])
async def test_signed_parts_retention_budget_is_shared_across_rounds(
    monkeypatch, streaming
):
    monkeypatch.setattr(continuation, "MAX_CONTINUATION_BYTES", 400)
    first = [ContentPart(text="A", thought_signature="x" * 100)]
    second = [ContentPart(text="B", thought_signature="y" * 100)]
    client = Client([native(first), native(second)])
    result = await run(client, request(), streaming)
    assert len(client.calls) == 2
    if streaming:
        assert [
            p for kind, payload in result if kind == "parts" for p in payload
        ] == first + second
        assert result[-3][1] == 2
    else:
        assert result.candidates[0].parts == first + second
        assert result.candidates[0].finish_reason == 2


@pytest.mark.parametrize("streaming", [False, True])
async def test_history_budget_includes_appended_signed_turn_and_user_prompt(
    monkeypatch, streaming
):
    monkeypatch.setattr(continuation, "MAX_HISTORY_BYTES", 600)
    upstream = native([ContentPart(text="A", thought_signature="x" * 100)])
    client = Client([upstream])
    await run(client, request(), streaming)
    assert len(client.calls) == 1


@pytest.mark.parametrize("streaming", [False, True])
async def test_later_safety_finish_preserves_signed_parts_and_does_not_continue(
    streaming,
):
    first = [ContentPart(text="A", thought_signature="sig-A")]
    second = [ContentPart(text="B", thought_signature="sig-B")]
    client = Client([native(first), native(second, 3)])
    result = await run(client, request(), streaming)
    assert len(client.calls) == 2
    if streaming:
        assert result[-3] == ("finish_reason", 3)
    else:
        assert result.candidates[0].parts == first + second
        assert result.candidates[0].finish_reason == 3


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("finish_content", ["none", "empty_model", "none_zero_index"])
@pytest.mark.parametrize("empty_thought", [False, True])
async def test_native_finish_only_and_usage_trailers_allow_normal_continuation(
    streaming, finish_content, empty_thought
):
    def response(text, finish):
        part = [None] * 15
        part[1], part[14] = text, f"sig-{text}"
        parts = [part]
        if empty_thought:
            thought_part = [None] * 13
            thought_part[12] = 1
            parts.insert(0, thought_part)
        body = [[[[parts, "model"], None]], None, None]
        finish_candidate = [None, finish]
        if finish_content == "empty_model":
            finish_candidate[0] = [[], "model"]
        elif finish_content == "none_zero_index":
            finish_candidate.append(0)
        finish_chunk = [[finish_candidate], None, None]
        usage_trailer = [
            [],
            None,
            [10, 2, 12],
            None,
            None,
            None,
            None,
            f"response-{text}",
        ]
        return json.dumps([[body, finish_chunk, usage_trailer]])

    class RawClient(Client):
        async def generate_content(self, **kwargs):
            self.calls.append(kwargs)
            return parse_text_output(self.rounds[len(self.calls) - 1])

        async def stream_generate_content(self, **kwargs):
            index = len(self.calls)
            self.calls.append(kwargs)

            async def transport(**unused):
                try:
                    yield "status", 200
                    yield "chunk", self.rounds[index].encode()
                finally:
                    self.closed.append(index)

            gateway = StreamingGateway(
                SimpleNamespace(send_streaming_request=transport)
            )
            async for event in gateway.stream_chat(
                captured=CapturedRequest(
                    url="https://synthetic.invalid/generate", headers={}, body="[]"
                ),
                model=kwargs["model"],
            ):
                yield event

    client = RawClient([response("A", 2), response("B", 1)])
    result = await run(client, request(), streaming)
    assert len(client.calls) == 2
    replayed = client.calls[1]["contents"][-2].parts
    expected = [("A", False, "sig-A")]
    if empty_thought:
        expected.insert(0, (None, True, None))
    assert [
        (part.text, part.thought, part.thought_signature) for part in replayed
    ] == expected
    if empty_thought:
        replayed_wire = client.calls[1]["contents"][-2].to_wire()
        assert replayed_wire[0][0][1] is None
        assert replayed_wire[0][0][12] == 1
        actual_parts = (
            [part for kind, payload in result if kind == "parts" for part in payload]
            if streaming
            else result.candidates[0].parts
        )
        assert actual_parts[0] == ContentPart(text=None, thought=True)
    if streaming:
        assert result[-3][0:2] == ("finish_reason", 1)
        assert result[-2][1]["prompt_tokens"] == 20
        assert not any(kind == "continuation_unsafe" for kind, _payload in result)
        assert client.closed == [0, 1]
    else:
        assert result.text == "AB"
        assert result.candidates[0].continuation_unsafe is False
        assert result.usage["prompt_tokens"] == 20
