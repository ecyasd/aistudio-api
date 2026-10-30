"""Synthetic wire replay for protocol completion, Part fidelity and tail usage."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from aistudio_api.api import gemini as api
from aistudio_api.api.responses import to_gemini_parts, to_gemini_usage_metadata
from aistudio_api.api.schemas import GeminiGenerateContentRequest
from aistudio_api.application import (
    generation as generation_execution,
    generation_recovery as recovery,
)
from aistudio_api.domain.errors import RequestError
from aistudio_api.infrastructure.gateway.capture import CapturedRequest
from aistudio_api.infrastructure.gateway.stream_parser import (
    IncrementalJSONStreamParser,
)
from aistudio_api.infrastructure.gateway.streaming import StreamingGateway
from aistudio_api.infrastructure.gateway.wire_parser import parse_text_output


def part(*, text=None, signature=None, function=None, image=None, thought=False):
    value = [None] * 15
    value[1] = text
    value[2] = image
    value[3] = [function, {}] if function else None
    value[10] = thought
    value[14] = signature
    return value


def chunk(*parts, finish=None, usage=None, response_id=None):
    return [
        [[[list(parts), "model"], finish]],
        None,
        usage,
        None,
        None,
        None,
        None,
        response_id,
    ]


def capture():
    return CapturedRequest(
        url="https://synthetic.invalid/generate", headers={}, body="[]"
    )


def gateway_for(pieces, *, status=200, closed=None):
    async def source(**kwargs):
        try:
            yield "status", status
            for piece in pieces:
                yield "chunk", piece
        finally:
            if closed is not None:
                closed.append(True)

    return StreamingGateway(SimpleNamespace(send_streaming_request=source))


@pytest.fixture
def client_env(monkeypatch):
    @asynccontextmanager
    async def lease():
        yield SimpleNamespace(auth_file="synthetic-account", context_id=42)

    account = SimpleNamespace(id="synthetic-account")
    monkeypatch.setattr(generation_execution, "ensure_active_account", AsyncMock())
    monkeypatch.setattr(recovery, "record_rotator_event", MagicMock())
    monkeypatch.setattr(generation_execution, "record_rotator_event", recovery.record_rotator_event)
    monkeypatch.setattr(recovery.runtime_state, "record", MagicMock())
    monkeypatch.setattr(
        recovery.runtime_state,
        "rotator",
        SimpleNamespace(get_account_stats=lambda _: None),
    )
    monkeypatch.setattr(
        recovery.runtime_state,
        "account_service",
        SimpleNamespace(
            list_accounts=lambda: [account],
            get_active_account=lambda: account,
            get_account_auth_path=lambda _: "synthetic-account",
        ),
    )
    client = SimpleNamespace(
        session=SimpleNamespace(request_scope=lease),
        clear_templates=MagicMock(),
        generate_content=AsyncMock(),
    )
    req = GeminiGenerateContentRequest.model_validate(
        {
            "contents": [
                {"role": "user", "parts": [{"text": "offline synthetic prompt"}]}
            ],
        }
    )
    return client, req


async def sse_payloads(client, req):
    response = await api.handle_gemini_generate_content(
        "gemini-test", req, client, stream=True
    )
    return [
        json.loads(frame[6:])
        async for frame in response.body_iterator
        if frame.startswith("data: ")
    ]


@pytest.mark.parametrize(
    "raw",
    [
        "not valid JSON",
        "",
        "[[",
        "[[[]]",
        "[[[null,]]]",
        "[[[]]]junk",
        "[[[]],]",
        "[[[],]]",
        "[[[]][]]",
        "[[[]]] [[[]]]",
    ],
)
def test_framing_rejects_malformed_and_truncated_json(raw):
    parser = IncrementalJSONStreamParser()
    with pytest.raises(RequestError) as caught:
        list(parser.feed(raw))
        list(parser.finish())
    assert caught.value.status == 502


def test_framing_handles_every_character_boundary_and_multiple_batches():
    chunks = [chunk(part(text='a [brace] and "quote"')), chunk(finish=1)]
    raw = ")]}'\n" + json.dumps([[chunks[0]], [chunks[1]]])
    parser = IncrementalJSONStreamParser()
    result = [item for ch in raw for item in parser.feed(ch)]
    result.extend(parser.finish())
    assert result == chunks
    assert parser.buffer == ""
    output = parse_text_output(raw)
    assert output.text == 'a [brace] and "quote"'
    assert output.candidates[0].finish_reason == 1


def test_nonstream_accepts_single_chunk_and_rejects_malformed_sibling():
    done = chunk(part(text="complete"), finish=1)
    assert parse_text_output(json.dumps(done)).text == "complete"
    with pytest.raises(RequestError):
        parse_text_output(json.dumps([[done, 123]]))


def test_framing_active_chunk_and_depth_are_bounded(monkeypatch):
    from aistudio_api.infrastructure.gateway import stream_parser

    monkeypatch.setattr(stream_parser, "MAX_PENDING_JSON_CHARS", 100)
    parser = IncrementalJSONStreamParser()
    with pytest.raises(RequestError, match="buffer limit"):
        list(parser.feed('[[["' + "a" * 100))
    parser = IncrementalJSONStreamParser()
    monkeypatch.setattr(stream_parser, "MAX_PENDING_JSON_CHARS", 1000)
    monkeypatch.setattr(stream_parser, "MAX_JSON_NESTING", 4)
    with pytest.raises(RequestError):
        list(parser.feed("[" * 8))


@pytest.mark.parametrize(
    "raw",
    [
        "not valid JSON",
        "",
        "[[",
        json.dumps([[chunk(part(text="partial"))]]),
        json.dumps([[chunk(part(text="partial"), finish=0)]]),
        json.dumps([[chunk(part(text="partial"), finish=True)]]),
    ],
)
async def test_http_200_without_protocol_completion_cannot_succeed(raw):
    with pytest.raises(RequestError) as caught:
        parse_text_output(raw)
    assert caught.value.status == 502
    events = []
    with pytest.raises(RequestError) as caught:
        async for event in gateway_for([raw.encode()]).stream_chat(
            captured=capture(), model="fixture"
        ):
            events.append(event)
    assert caught.value.status == 502
    assert not any(kind in {"done", "finish_reason"} for kind, _ in events)


async def test_completion_marker_does_not_hide_truncated_envelope_or_transport_failure():
    raw = json.dumps([[chunk(part(text="answer"), finish=1)]])
    events = []
    with pytest.raises(RequestError):
        async for event in gateway_for([raw[:-1]]).stream_chat(
            captured=capture(), model="fixture"
        ):
            events.append(event)
    assert any(kind == "parts" for kind, _ in events)
    assert not any(kind == "done" for kind, _ in events)

    async def broken(**kwargs):
        yield "status", 200
        yield "chunk", raw
        raise RequestError(504, "Synthetic stream read failed")

    events = []
    with pytest.raises(RequestError) as caught:
        async for event in StreamingGateway(
            SimpleNamespace(send_streaming_request=broken)
        ).stream_chat(captured=capture(), model="fixture"):
            events.append(event)
    assert caught.value.status == 504
    assert not any(kind == "done" for kind, _ in events)


async def test_finish_is_followed_by_tail_usage_and_latest_response_id():
    usage = [100, 20, 150, None, None, None, None, None, None, 30]
    done = chunk(part(text="answer"), finish=1, response_id="original")
    trailer = [[], None, usage, None, None, None, None, "tail-id"]
    raw = json.dumps([[done, trailer]])
    closed = []
    events = [
        event
        async for event in gateway_for(
            [raw[: len(raw) // 2], raw[len(raw) // 2 :]], closed=closed
        ).stream_chat(captured=capture(), model="fixture")
    ]
    assert closed == [True]
    metadata = dict(events)
    assert metadata["response_id"] == "tail-id"
    assert metadata["finish_reason"] == 1
    assert metadata["usage"]["total_tokens"] == 150
    output = parse_text_output(raw)
    assert output.response_id == "tail-id"
    assert to_gemini_usage_metadata(output.usage).model_dump() == {
        "promptTokenCount": 100,
        "candidatesTokenCount": 20,
        "thoughtsTokenCount": 30,
        "totalTokenCount": 150,
    }


async def test_blocked_generation_with_finish_and_no_text_is_valid():
    raw = json.dumps([[chunk(finish=3)]])
    assert parse_text_output(raw).candidates[0].finish_reason == 3
    events = [
        event
        async for event in gateway_for([raw]).stream_chat(
            captured=capture(), model="fixture"
        )
    ]
    assert events[-1] == ("done", None)
    assert dict(events)["finish_reason"] == 3


@pytest.mark.parametrize("stream", [False, True])
async def test_parts_keep_original_order_and_own_signatures(client_env, stream):
    client, req = client_env
    original = [
        part(function="tool_A", signature="signature-A"),
        part(function="tool_B", signature="signature-B"),
        part(text="thinking", thought=True, signature="thinking-signature"),
        part(text="answer", signature="text-signature"),
        part(image=["image/png", "cG5n"], signature="image-signature"),
        part(text="", signature="terminal-empty-signature"),
    ]
    raw = json.dumps(
        [
            [
                chunk(item, finish=1 if index == len(original) - 1 else None)
                for index, item in enumerate(original)
            ]
        ]
    )
    output = parse_text_output(raw)
    expected = [
        {
            "functionCall": {"name": "tool_A", "args": {}},
            "thoughtSignature": "signature-A",
        },
        {
            "functionCall": {"name": "tool_B", "args": {}},
            "thoughtSignature": "signature-B",
        },
        {"text": "thinking", "thought": True, "thoughtSignature": "thinking-signature"},
        {"text": "answer", "thoughtSignature": "text-signature"},
        {
            "inlineData": {"mimeType": "image/png", "data": "cG5n"},
            "thoughtSignature": "image-signature",
        },
        {"text": "", "thoughtSignature": "terminal-empty-signature"},
    ]
    assert [
        p.model_dump(exclude_none=True)
        for p in to_gemini_parts("", source_parts=output.candidates[0].parts)
    ] == expected
    if stream:
        gateway = gateway_for([raw.encode()])
        client.stream_generate_content = lambda **kwargs: gateway.stream_chat(
            captured=capture(), model="fixture"
        )
        payloads = await sse_payloads(client, req)
        actual = [
            p
            for payload in payloads
            for c in payload.get("candidates", [])
            for p in c["content"]["parts"]
            if p.get("thoughtSignature")
        ]
        assert payloads[-1]["candidates"][0]["finishReason"] == "STOP"
    else:
        client.generate_content.return_value = output
        response = await api.handle_gemini_generate_content(
            "gemini-test", req, client, stream=False
        )
        actual = [
            p.model_dump(exclude_none=True)
            for p in response.candidates[0].content.parts
        ]
    assert actual == expected
    assert recovery.runtime_state.record.call_args.args[1] == "success"


async def test_partial_output_then_truncation_is_in_band_error_without_success(
    client_env,
):
    client, req = client_env
    raw = json.dumps([[chunk(part(text="partial answer"))]])[:-1]
    gateway = gateway_for([raw])
    client.stream_generate_content = lambda **kwargs: gateway.stream_chat(
        captured=capture(), model="fixture"
    )
    payloads = await sse_payloads(client, req)
    assert (
        payloads[0]["candidates"][0]["content"]["parts"][0]["text"] == "partial answer"
    )
    assert payloads[-1]["error"]["code"] == 502
    assert not any(
        c.get("finishReason") == "STOP"
        for p in payloads
        for c in p.get("candidates", [])
    )
    assert all(
        call.args[1] != "success" for call in recovery.runtime_state.record.call_args_list
    )


async def test_non_200_html_error_keeps_original_status():
    with pytest.raises(RequestError) as caught:
        _ = [
            event
            async for event in gateway_for(
                ["<html>unavailable</html>"], status=503
            ).stream_chat(captured=capture(), model="fixture")
        ]
    assert caught.value.status == 503


async def test_utf8_split_across_transport_frames_is_preserved():
    raw = json.dumps(
        [[chunk(part(text="你好"), finish=1)]], ensure_ascii=False
    ).encode()
    events = [
        event
        async for event in gateway_for([bytes([byte]) for byte in raw]).stream_chat(
            captured=capture(), model="fixture"
        )
    ]
    assert dict(events)["parts"][0].text == "你好"


def test_gemini_usage_fallback_separates_thinking_and_total():
    assert to_gemini_usage_metadata(
        {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "completion_tokens_details": {"reasoning_tokens": 30},
        }
    ).model_dump() == {
        "promptTokenCount": 100,
        "candidatesTokenCount": 20,
        "thoughtsTokenCount": 30,
        "totalTokenCount": 150,
    }
