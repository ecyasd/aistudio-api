"""A full unsigned terminal snapshot must not replay already delivered prose."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from aistudio_api.api.gemini import format_sse_event
from aistudio_api.infrastructure.gateway.capture import CapturedRequest
from aistudio_api.infrastructure.gateway.streaming import StreamingGateway
from aistudio_api.infrastructure.gateway.wire_parser import parse_text_output
from aistudio_api.infrastructure.utils import error_records, response_diagnostics

BODY = "合成正文的一段，包含换行和标点。\n" * 40


def chunk(text=None, *, finish=None, signature=None):
    parts = []
    if text is not None:
        part = [None] * 15
        part[1], part[14] = text, signature
        parts.append(part)
    return [[[[parts, "model"], finish]], None, None]


async def public_parts(chunks, fragment_size=13):
    raw = json.dumps([chunks], ensure_ascii=False).encode()

    async def events(**_kwargs):
        yield "status", 200
        for offset in range(0, len(raw), fragment_size):
            yield "chunk", raw[offset:offset + fragment_size]

    gateway = StreamingGateway(SimpleNamespace(send_streaming_request=events))
    parts, metadata = [], []
    async for kind, payload in gateway.stream_chat(
        captured=CapturedRequest(url="https://synthetic.invalid", headers={}, body="[]"),
        model="gemini-synthetic",
    ):
        frame = format_sse_event(kind, payload)
        if frame:
            parts.extend(json.loads(frame[6:])["candidates"][0]["content"]["parts"])
        else:
            metadata.append((kind, payload))
    return parts, metadata


@pytest.mark.parametrize("finish", [1, 2])
@pytest.mark.parametrize("fragment_size", [1, 4093])
async def test_terminal_full_body_replay_is_delivered_once(finish, fragment_size):
    chunks = [chunk(BODY[:200]), chunk(BODY[200:]), chunk(BODY, finish=finish)]
    parts, metadata = await public_parts(chunks, fragment_size)
    assert "".join(part.get("text", "") for part in parts) == BODY
    assert ("finish_reason", finish) in metadata
    assert metadata[-1] == ("done", None)
    output = parse_text_output(json.dumps([chunks]))
    assert output.text == BODY
    assert "".join(part.text or "" for part in output.candidates[0].parts) == BODY


@pytest.mark.parametrize("texts", [
    ["哈", "哈"],
    [BODY[:200], BODY[200:], "新的结尾"],
    [BODY, BODY],
])
async def test_ordinary_repeated_deltas_and_new_terminal_text_are_preserved(texts):
    # Repetition without a terminal snapshot is ordinary generated content.
    chunks = [*(chunk(text) for text in texts), chunk(finish=1)]
    parts, _ = await public_parts(chunks)
    assert "".join(part.get("text", "") for part in parts) == "".join(texts)
    assert parse_text_output(json.dumps([chunks])).text == "".join(texts)


async def test_signed_terminal_part_is_never_rewritten():
    chunks = [chunk(BODY), chunk(BODY, finish=1, signature="synthetic-signature")]
    parts, _ = await public_parts(chunks)
    assert parts[-1] == {"text": BODY, "thoughtSignature": "synthetic-signature"}
    assert parse_text_output(json.dumps([chunks])).text == BODY * 2


@pytest.mark.parametrize("chunks", [
    [chunk(BODY), chunk(BODY, finish=1)],
    [chunk("哈"), chunk("哈"), chunk("哈哈", finish=1)],
    [chunk(BODY[:200]), chunk(BODY[200:]), chunk(BODY + "新结尾", finish=1)],
    [chunk(BODY[:200]), chunk(BODY[200:]), chunk(BODY, finish=3)],
    [chunk(BODY[:200], signature="synthetic-prior-signature"), chunk(BODY[200:]), chunk(BODY, finish=1)],
])
async def test_ambiguous_or_signed_repetitions_are_preserved(chunks):
    expected = parse_text_output(json.dumps([chunks])).text
    parts, _ = await public_parts(chunks)
    assert "".join(part.get("text", "") for part in parts) == expected
    assert expected == "".join(c[0][0][0][0][0][1] for c in chunks)


async def test_replay_diagnostics_are_bounded_metadata_only(monkeypatch):
    store = response_diagnostics.ResponseDiagnosticStore()
    monkeypatch.setattr(response_diagnostics, "get_response_diagnostic_store", lambda: store)
    token = error_records.bind_request_context()
    try:
        response_diagnostics.initialize_response_diagnostics(stream=True)
        response_diagnostics.configure_generation(model="gemini-synthetic", auto_continue=False)
        response_diagnostics.begin_upstream_call()
        chunks = [chunk(BODY[:200]), chunk(BODY[200:]), chunk(BODY, finish=1)]
        await public_parts(chunks)
        response_diagnostics.finish_response_diagnostics("success", 200)
        row = store.list_records()["items"][0]
        assert row["body_parts"] == 3
        assert row["body_chars"] == 2 * len(BODY)  # Received, before suppression.
        assert row["terminal_body_chars"] == row["suppressed_body_chars"] == len(BODY)
        assert row["terminal_body_replays"] == 1
        assert row["finish_reasons"] == [1]
        assert "terminal_body_replay_suppressed" in row["flags"]
        assert BODY[:20] not in json.dumps(row, ensure_ascii=False)
        assert not any("digest" in key or "hash" in key for key in row)
    finally:
        error_records.reset_request_context(token)
