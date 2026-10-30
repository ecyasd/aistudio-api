"""Synthetic native responses retain signed Part boundaries in later requests."""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from aistudio_api.application import continuation
from aistudio_api.application.chat_service import NormalizedGeminiRequest
from aistudio_api.infrastructure.gateway import wire_codec
from aistudio_api.infrastructure.gateway.capture import RequestCaptureService
from aistudio_api.infrastructure.gateway.model_defaults import ModelDefaults
from aistudio_api.infrastructure.gateway.streaming import StreamingGateway
from aistudio_api.infrastructure.gateway.wire_parser import parse_text_output
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart


def _response_part(text, signature, *, thought_slot=None):
    part = [None] * 15
    part[1] = text
    part[14] = signature
    if thought_slot is not None:
        part[thought_slot] = 1 if thought_slot == 12 else True
    return part


def _request_part(text, signature, *, thought=False):
    # Assert the actual native JSON slots, without a decode/encode roundtrip:
    # the request decoder intentionally handles only a template subset.
    part = [None] * 15
    part[1] = text
    part[12] = 1 if thought else None
    part[14] = signature
    while part and part[-1] is None:
        part.pop()
    return part


def _wire_response(parts, finish, response_id):
    # Several response chunks belong to ONE model turn. The last two Parts
    # distinguish a signature-only Part from an explicitly empty text Part.
    chunks = []
    for index, part in enumerate(parts):
        chunks.append(
            [
                [[[[part], "model"], finish if index == len(parts) - 1 else None]],
                None,
                [10, 3, 15, None, None, None, None, None, None, 2],
                None,
                None,
                None,
                None,
                response_id,
            ]
        )
    return json.dumps([chunks], ensure_ascii=False)


class _SnapshotSession:
    request_context = SimpleNamespace(context_id=73)

    def __init__(self):
        self.snapshot_inputs = []

    async def capture_template(self, model):
        return {
            "url": "https://synthetic.invalid/generate",
            "headers": {"Content-Type": "application/json+protobuf"},
            "body": json.dumps([model, [], None, [], "old-synthetic-ticket"]),
        }

    async def generate_snapshot(self, contents):
        self.snapshot_inputs.append(deepcopy(contents))
        return f"synthetic-ticket-{len(self.snapshot_inputs)}"


class _NativeWireClient:
    def __init__(self, responses):
        self.responses = responses
        self.session = _SnapshotSession()
        self.capture = RequestCaptureService(self.session)
        self.bodies = []
        self.closed = []

    async def _capture(self, kwargs):
        captured = await self.capture.capture(
            prompt=kwargs["capture_prompt"],
            model=kwargs["model"],
            images=kwargs["capture_images"],
            contents=kwargs["contents"],
            system_instruction_content=kwargs["system_instruction_content"],
            tools=kwargs["tools"],
            tool_config=kwargs["tool_config"],
            safety_settings=kwargs["safety_settings"],
            temperature=kwargs["temperature"],
            top_p=kwargs["top_p"],
            top_k=kwargs["top_k"],
            max_tokens=kwargs["max_tokens"],
            generation_config_overrides=kwargs["generation_config_overrides"],
            sanitize_plain_text=kwargs["sanitize_plain_text"],
        )
        self.bodies.append(json.loads(captured.body))
        return captured, len(self.bodies) - 1

    async def generate_content(self, **kwargs):
        _, index = await self._capture(kwargs)
        return parse_text_output(self.responses[index])

    async def stream_generate_content(self, **kwargs):
        captured, index = await self._capture(kwargs)
        raw = self.responses[index].encode("utf-8")

        async def transport(**unused):
            try:
                yield "status", 200
                # Split inside UTF-8 and JSON strings to exercise the actual
                # gateway framing rather than inventing idealized Part events.
                for start in range(0, len(raw), 5):
                    yield "chunk", raw[start : start + 5]
            finally:
                self.closed.append(index)

        gateway = StreamingGateway(SimpleNamespace(send_streaming_request=transport))
        async for event in gateway.stream_chat(
            captured=captured, model=kwargs["model"]
        ):
            yield event


@pytest.fixture
def normalized_native_request(monkeypatch):
    monkeypatch.setattr(
        continuation, "resolve_model_defaults", lambda _: ModelDefaults()
    )
    monkeypatch.setattr(wire_codec, "resolve_model_defaults", lambda _: ModelDefaults())
    return NormalizedGeminiRequest(
        model="models/synthetic-native",
        contents=[
            AistudioContent("user", [AistudioPart(text="earlier question")]),
            AistudioContent(
                "model",
                [
                    AistudioPart(
                        text="earlier thought",
                        thought=True,
                        thought_signature="synthetic-history-thought",
                    ),
                    AistudioPart(
                        text="earlier answer",
                        thought_signature="synthetic-history-text",
                    ),
                ],
            ),
            AistudioContent("user", [AistudioPart(text="continue discussion")]),
        ],
        system_instruction=AistudioContent("user", [AistudioPart(text="be precise")]),
        tools=[[[]]],
        tool_config=[None, [1]],
        safety_settings=[[None, None, 7, 2]],
        capture_prompt="continue discussion",
        capture_images=None,
        max_tokens=11,
        generation_config_overrides={
            "thinking_config": [1, None, None, 3],
            "response_mime_type": "text/plain",
        },
    )


@pytest.mark.parametrize("stream", [False, True])
async def test_native_response_parts_keep_own_signatures_and_distinct_rounds(
    normalized_native_request,
    stream,
):
    normalized = normalized_native_request
    original = deepcopy(normalized)
    first = [
        _response_part("思考-1", "synthetic-thought-1", thought_slot=10),
        _response_part("A ", "synthetic-text-1"),
        _response_part(None, "synthetic-only-1"),
        _response_part("", "synthetic-empty-1"),
    ]
    second = [
        _response_part("thought-2", "synthetic-thought-2", thought_slot=0),
        _response_part(" B", "synthetic-text-2"),
        _response_part(None, "synthetic-only-2"),
        _response_part("", "synthetic-empty-2"),
    ]
    final = [_response_part("done", "synthetic-final", thought_slot=None)]
    client = _NativeWireClient(
        [
            _wire_response(first, 2, "synthetic-response-1"),
            _wire_response(second, 2, "synthetic-response-2"),
            _wire_response(final, 1, "synthetic-response-3"),
        ]
    )
    if stream:
        emitted = [
            event
            async for event in continuation.stream_generate_content(
                client, normalized, auto_continue=True
            )
        ]
        actual_parts = [
            part for kind, value in emitted if kind == "parts" for part in value
        ]
        assert [kind for kind, _ in emitted].count("finish_reason") == 1
        assert dict(emitted)["finish_reason"] == 1
        assert client.closed == [0, 1, 2]
    else:
        result = await continuation.generate_content(
            client, normalized, auto_continue=True
        )
        assert result.text == "A  Bdone"
        assert result.thinking == "思考-1thought-2"
        assert result.candidates[0].finish_reason == 1
        actual_parts = result.candidates[0].parts

    assert normalized == original
    assert len(client.bodies) == 3
    assert [
        (part.text, part.thought, part.thought_signature) for part in actual_parts
    ] == [
        ("思考-1", True, "synthetic-thought-1"),
        ("A ", False, "synthetic-text-1"),
        (None, False, "synthetic-only-1"),
        ("", False, "synthetic-empty-1"),
        ("thought-2", True, "synthetic-thought-2"),
        (" B", False, "synthetic-text-2"),
        (None, False, "synthetic-only-2"),
        ("", False, "synthetic-empty-2"),
        ("done", False, "synthetic-final"),
    ]
    first_request = client.bodies[0][1]
    continued_once = client.bodies[1][1]
    continued_twice = client.bodies[2][1]
    assert len(first_request) == 3
    assert len(continued_once) == 5
    assert len(continued_twice) == 7
    assert continued_once[:3] == first_request
    assert continued_twice[:5] == continued_once
    assert continued_once[3] == [
        [
            _request_part("思考-1", "synthetic-thought-1", thought=True),
            _request_part("A ", "synthetic-text-1"),
            _request_part(None, "synthetic-only-1"),
            _request_part("", "synthetic-empty-1"),
        ],
        "model",
    ]
    assert continued_twice[5] == [
        [
            _request_part("thought-2", "synthetic-thought-2", thought=True),
            _request_part(" B", "synthetic-text-2"),
            _request_part(None, "synthetic-only-2"),
            _request_part("", "synthetic-empty-2"),
        ],
        "model",
    ]
    assert (
        continued_once[4]
        == continued_twice[6]
        == [[[None, continuation.CONTINUE_PROMPT]], "user"]
    )
    for index, body in enumerate(client.bodies):
        assert body[0] == normalized.model
        assert body[3][3] == 11
        assert body[3][16] == [1, None, None, 3]
        assert body[4] == f"synthetic-ticket-{index + 1}"
        assert body[5] == normalized.system_instruction.to_wire()
        assert body[6] == normalized.tools
        assert [
            content.to_wire() for content in client.session.snapshot_inputs[index]
        ] == body[1]


async def test_stream_retains_snapshot_before_consumer_mutates_emitted_part(
    normalized_native_request,
):
    first = [
        _response_part("thought", "synthetic-thought", thought_slot=12),
        _response_part("answer", "synthetic-answer"),
    ]
    client = _NativeWireClient(
        [
            _wire_response(first, 2, "synthetic-first"),
            _wire_response(
                [_response_part("done", "synthetic-done")], 1, "synthetic-next"
            ),
        ]
    )
    async for kind, value in continuation.stream_generate_content(
        client, normalized_native_request, auto_continue=True
    ):
        if kind == "parts" and len(client.bodies) == 1:
            value[0].text = "synthetic-consumer-change"
            value[0].thought_signature = "synthetic-consumer-signature"
            value[0].thought = not value[0].thought
    assert len(client.bodies) == 2
    assert client.bodies[1][1][-2] == [
        [
            _request_part("thought", "synthetic-thought", thought=True),
            _request_part("answer", "synthetic-answer"),
        ],
        "model",
    ]


@pytest.mark.parametrize(
    ("slot", "flag", "safe"),
    [
        (0, True, True),
        (0, False, True),
        (10, True, True),
        (10, False, True),
        (12, 1, True),
        (12, 0, True),
        (0, 1, False),
        (0, 0, False),
        (10, 1, False),
        (10, 0, False),
    ],
)
def test_continuation_accepts_only_thought_encodings_the_response_parser_preserves(
    slot,
    flag,
    safe,
):
    part = _response_part("synthetic text", "synthetic signature")
    part[slot] = flag
    candidate = parse_text_output(_wire_response([part], 2, "synthetic-id")).candidates[
        0
    ]
    assert candidate.continuation_unsafe is not safe
    if safe:
        assert candidate.parts[0].thought is bool(flag)


@pytest.mark.parametrize(
    "change",
    [
        "user_role",
        "assistant_role",
        "missing_role",
        "extra_content_payload",
        "multiple_candidates",
        "nonzero_candidate_index",
        "boolean_candidate_index",
        "citation_metadata",
        "grounding_metadata",
    ],
)
def test_unrepresented_content_or_candidate_metadata_stops_continuation(change):
    native = json.loads(
        _wire_response(
            [_response_part("synthetic text", "synthetic signature")], 2, "id"
        )
    )
    candidates = native[0][0][0]
    candidate = candidates[0]
    content = candidate[0]
    if change == "user_role":
        content[1] = "user"
    elif change == "assistant_role":
        content[1] = "assistant"
    elif change == "missing_role":
        content.pop()
    elif change == "extra_content_payload":
        content.append({"synthetic_unrepresented_metadata": True})
    elif change == "multiple_candidates":
        candidates.append(deepcopy(candidate))
    elif change == "nonzero_candidate_index":
        candidate.append(1)
    elif change == "boolean_candidate_index":
        candidate.append(False)
    elif change == "citation_metadata":
        candidate.extend([None, None, None, {"synthetic_citation": True}])
    elif change == "grounding_metadata":
        candidate.extend([None] * 6 + [{"synthetic_grounding": True}])
    result = parse_text_output(json.dumps(native)).candidates[0]
    assert result.continuation_unsafe
    assert result.text == "synthetic text"
    assert result.parts[0].thought_signature == "synthetic signature"


def test_explicit_zero_candidate_index_does_not_block_signed_text():
    native = json.loads(
        _wire_response(
            [_response_part("synthetic text", "synthetic signature")], 2, "id"
        )
    )
    native[0][0][0][0].append(0)
    result = parse_text_output(json.dumps(native)).candidates[0]
    assert not result.continuation_unsafe


@pytest.mark.parametrize("slot", [2, 3, 4, 5, 8, 9, 11, 13])
def test_signed_nontext_or_unknown_part_payload_is_never_replayed_as_empty_text(slot):
    part = _response_part(None, "synthetic signature")
    part[slot] = ["synthetic payload"]
    if slot == 2:
        part[slot] = ["image/png", "c3ludGhldGlj"]
    candidate = parse_text_output(_wire_response([part], 2, "synthetic-id")).candidates[
        0
    ]
    assert candidate.continuation_unsafe
