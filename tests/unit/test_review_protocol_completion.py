"""Stream and non-stream replay must agree on completed generation trailers."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from aistudio_api.domain.errors import IncompleteResponseError
from aistudio_api.infrastructure.gateway.capture import CapturedRequest
from aistudio_api.infrastructure.gateway.streaming import StreamingGateway
from aistudio_api.infrastructure.gateway.wire_parser import parse_text_output


def chunk(*, text=None, finish=None, usage=None):
    parts = [[None, text]] if text is not None else []
    return [[[[parts, "model"], finish]], None, usage]


async def replay_stream(raw):
    async def events(**_kwargs):
        yield "status", 200
        yield "chunk", raw.encode()

    gateway = StreamingGateway(SimpleNamespace(send_streaming_request=events))
    return [event async for event in gateway.stream_chat(
        captured=CapturedRequest(
            url="https://synthetic.invalid/generate", headers={}, body="[]",
        ),
        model="models/synthetic",
    )]


@pytest.mark.parametrize("finish_reason", [1, 2, 3])
async def test_unspecified_tail_marker_does_not_erase_completed_generation(finish_reason):
    raw = json.dumps([[
        chunk(text="synthetic answer", finish=finish_reason),
        chunk(finish=0, usage=[3, 4, 7]),
    ]])
    events = dict(await replay_stream(raw))
    assert events["finish_reason"] == finish_reason
    assert events["usage"]["total_tokens"] == 7
    assert "done" in events

    output = parse_text_output(raw)
    assert output.text == "synthetic answer"
    assert output.candidates[0].finish_reason == events["finish_reason"]
    assert output.usage == events["usage"]


async def test_unspecified_markers_without_a_completion_still_fail_in_both_modes():
    raw = json.dumps([[
        chunk(text="synthetic partial answer", finish=0),
        chunk(finish=0, usage=[3, 4, 7]),
    ]])
    with pytest.raises(IncompleteResponseError):
        parse_text_output(raw)
    with pytest.raises(IncompleteResponseError):
        await replay_stream(raw)
