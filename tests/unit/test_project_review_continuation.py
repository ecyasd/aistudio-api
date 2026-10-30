"""Offline regressions found by the whole-project protocol review."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from aistudio_api.domain.errors import RequestError
from aistudio_api.infrastructure.gateway.capture import CapturedRequest
from aistudio_api.infrastructure.gateway.streaming import StreamingGateway
from aistudio_api.infrastructure.gateway.wire_parser import (
    parse_response_part,
    parse_text_output,
)


def _chunk(text="ordinary text", *, lead=None, finish=1):
    return [[[[[[lead, text]], "model"], finish]], None, [1, 1, 2]]


async def _stream(raw, received, closed):
    async def transport(**unused):
        try:
            yield "status", 200
            yield "chunk", raw.encode("utf-8")
        finally:
            closed.append(True)

    gateway = StreamingGateway(SimpleNamespace(send_streaming_request=transport))
    async for event in gateway.stream_chat(
        captured=CapturedRequest(
            url="https://synthetic.invalid/generate", headers={}, body="[]"
        ),
        model="models/synthetic-review",
    ):
        received.append(event)


@pytest.mark.parametrize("text", ["user", "model", "system", "assistant"])
@pytest.mark.parametrize("lead", [None, False, True])
def test_compact_text_equal_to_a_role_name_is_not_dropped(text, lead):
    part = parse_response_part([lead, text])
    assert part.text == text
    assert part.thought is bool(lead)
    output = parse_text_output(json.dumps([[_chunk(text, lead=lead)]]))
    assert output.candidates[0].parts[0].text == text
    assert (output.thinking if lead else output.text) == text


@pytest.mark.parametrize("text", ["user", "model", "system", "assistant"])
async def test_stream_preserves_compact_role_word_answers(text):
    received, closed = [], []
    await _stream(json.dumps([[_chunk(text)]]), received, closed)
    parts = [part for kind, value in received if kind == "parts" for part in value]
    assert [part.text for part in parts] == [text]
    assert received[-3][0:2] == ("finish_reason", 1)
    assert closed == [True]


@pytest.mark.parametrize("malformed", [[42], [[42]], [[["not-a-candidate"]]]])
@pytest.mark.parametrize("position", ["before", "after"])
async def test_malformed_stream_chunk_cannot_be_hidden_by_a_valid_finish(
    malformed, position
):
    chunks = [_chunk(), malformed] if position == "after" else [malformed, _chunk()]
    raw = json.dumps([chunks])
    with pytest.raises(RequestError) as nonstream:
        parse_text_output(raw)
    assert nonstream.value.status == 502
    received, closed = [], []
    with pytest.raises(RequestError) as stream:
        await _stream(raw, received, closed)
    assert stream.value.status == 502
    assert not {kind for kind, _value in received}.intersection(
        {"finish_reason", "usage", "done"}
    )
    assert closed == [True]


async def test_valid_finish_only_usage_and_response_id_trailers_remain_supported():
    content = _chunk(finish=None)
    finish = [[[None, 1, 0]], None, None]
    usage_and_id = [[], None, [10, 3, 13], None, None, None, None, "synthetic-id"]
    raw = json.dumps([[content, finish, usage_and_id]])
    nonstream = parse_text_output(raw)
    received, closed = [], []
    await _stream(raw, received, closed)
    assert nonstream.text == "ordinary text"
    assert ("finish_reason", 1) in received
    assert ("response_id", "synthetic-id") in received
    assert received[-2][1] == nonstream.usage
    assert received[-1] == ("done", None)
    assert closed == [True]


@pytest.mark.parametrize(("index", "payload"), [
    (2, 42), (2, ["image/png"]), (2, ["image/png", "a"]),
    (2, [None, "YWJj"]), (2, ["image/png", 123]),
    (3, 42), (4, True), (10, 42), (11, 42), (11, True),
])
async def test_malformed_nontext_part_cannot_finish_successfully(index, payload):
    part = [None] * 15
    part[index] = payload
    raw = json.dumps([[[[[[part], "model"], 1]], None, [1, 1, 2]]])
    with pytest.raises(RequestError) as error:
        parse_text_output(raw)
    assert error.value.status == 502
    received, closed = [], []
    with pytest.raises(RequestError) as error:
        await _stream(raw, received, closed)
    assert error.value.status == 502
    assert not any(name in ("done", "finish_reason", "usage") for name, _ in received)
    assert closed == [True]


def test_multiframe_aggregation_preserves_order_empty_parts_and_last_metadata():
    chunks = [_chunk(f"段落{i}", lead=i % 2 == 1, finish=None) for i in range(500)]
    # Explicit empty text carrying a signature must survive aggregation.
    signed = [None] * 15
    signed[1], signed[14] = "", "synthetic-signature"
    chunks.append([[[[[signed], "model"], 1]], None, [10, 20, 30], None, None, None, None, "last-id"])
    output = parse_text_output(json.dumps([chunks], ensure_ascii=False))
    assert output.text == "".join(f"段落{i}" for i in range(0, 500, 2))
    assert output.thinking == "".join(f"段落{i}" for i in range(1, 500, 2))
    assert output.candidates[0].parts[-1].text == ""
    assert output.candidates[0].parts[-1].thought_signature == "synthetic-signature"
    assert len(output.candidates[0].parts) == 501
    assert output.response_id == "last-id"
