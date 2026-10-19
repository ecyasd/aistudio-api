"""Offline incomplete-response recovery without replaying published Parts."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from aistudio_api.api.schemas import GeminiGenerateContentRequest
from aistudio_api.application import api_service_gemini as api
from aistudio_api.domain.errors import IncompleteResponseError, RequestError
from aistudio_api.infrastructure.gateway.capture import CapturedRequest
from aistudio_api.infrastructure.gateway.stream_parser import (
    IncrementalJSONStreamParser,
)
from aistudio_api.infrastructure.gateway.streaming import StreamingGateway
from aistudio_api.infrastructure.gateway.wire_parser import parse_text_output


def _part(*, text=None, thought=False, function=None, image=None, signature=None):
    value = [None] * 15
    value[1] = text
    value[2] = image
    value[3] = [function, {}] if function else None
    value[10] = thought
    value[14] = signature
    return value


def _response(*parts, finish=None):
    chunk = [[[list(parts), "model"], finish]]
    return json.dumps([[[chunk, None, [3, 2, 5]]]])


@pytest.fixture
def recovery_env(monkeypatch):
    context = SimpleNamespace(auth_file="offline/recovery-account", context_id=42)
    leases = []
    closed = []
    calls = []
    account = SimpleNamespace(id="recovery-account")

    @asynccontextmanager
    async def request_scope():
        assert not leases
        leases.append(context)
        try:
            yield context
        finally:
            leases.remove(context)
            closed.append(context.context_id)

    client = SimpleNamespace(
        session=SimpleNamespace(request_scope=request_scope),
        clear_templates=MagicMock(),
        generate_content=AsyncMock(),
        stream_generate_content=MagicMock(),
    )
    monkeypatch.setattr(api, "ensure_active_account", AsyncMock())
    monkeypatch.setattr(api, "try_switch_account", AsyncMock(return_value=False))
    monkeypatch.setattr(api, "record_rotator_event", MagicMock())
    monkeypatch.setattr(api, "record_failure", MagicMock())
    monkeypatch.setattr(api, "has_final_failure", lambda: False)
    monkeypatch.setattr(api.runtime_state, "record", MagicMock())
    monkeypatch.setattr(
        api.runtime_state, "account_service",
        SimpleNamespace(
            list_accounts=lambda: [account],
            get_active_account=lambda: account,
            get_account_auth_path=lambda _: context.auth_file,
        ),
    )
    monkeypatch.setattr(
        api.runtime_state, "rotator",
        SimpleNamespace(get_account_stats=lambda _: None),
    )
    request = GeminiGenerateContentRequest.model_validate(
        {"contents": [{"role": "user", "parts": [{"text": "offline test"}]}]}
    )
    return SimpleNamespace(
        client=client, request=request, context=context, leases=leases,
        closed=closed, calls=calls,
    )


def _install_responses(env, responses):
    upstream_closed = []

    async def nonstream(**kwargs):
        env.calls.append(env.leases[0].context_id)
        response = responses[min(len(env.calls) - 1, len(responses) - 1)]
        if isinstance(response, BaseException):
            raise response
        return parse_text_output(response)

    async def stream(**kwargs):
        env.calls.append(env.leases[0].context_id)
        attempt = len(env.calls) - 1
        response = responses[min(attempt, len(responses) - 1)]

        async def events(**kwargs):
            try:
                yield "status", 200
                if isinstance(response, BaseException):
                    raise response
                yield "chunk", response.encode()
            finally:
                upstream_closed.append(attempt)

        gateway = StreamingGateway(SimpleNamespace(send_streaming_request=events))
        captured = CapturedRequest(
            url="https://synthetic.invalid/generate", headers={}, body="[]"
        )
        async for event in gateway.stream_chat(captured=captured, model="fixture"):
            yield event

    env.client.generate_content.side_effect = nonstream
    env.client.stream_generate_content.side_effect = stream
    return upstream_closed


async def _request(env, *, stream):
    response = await api.handle_gemini_generate_content(
        "gemini-test", env.request, env.client, stream=stream
    )
    if not stream:
        return response
    return [
        json.loads(frame[6:])
        async for frame in response.body_iterator
        if frame.startswith("data: ")
    ]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("failure", ["truncated", "missing_marker"])
async def test_unpublished_incomplete_response_retries_once_on_same_account(
    recovery_env, stream, failure,
):
    env = recovery_env
    incomplete = "[[" if failure == "truncated" else (
        _response() if stream else _response(_part(text="unpublished partial"))
    )
    upstream_closed = _install_responses(
        env, [incomplete, _response(_part(text="complete"), finish=1)]
    )
    response = await _request(env, stream=stream)
    if stream:
        assert response[0]["candidates"][0]["content"]["parts"][0]["text"] == "complete"
        assert response[-1]["candidates"][0]["finishReason"] == "STOP"
        assert upstream_closed == [0, 1]
        assert not any("error" in frame for frame in response)
    else:
        assert response.candidates[0].content.parts[0].text == "complete"
        assert response.candidates[0].finishReason == "STOP"
    assert env.calls == [42, 42]
    assert env.closed == [42, 42]
    assert not env.leases
    env.client.clear_templates.assert_called_once_with()
    api.try_switch_account.assert_not_awaited()
    api.record_rotator_event.assert_called_once_with(
        "success", model="models/gemini-test", account_id="recovery-account"
    )
    assert api.runtime_state.record.call_args.args[1] == "success"


@pytest.mark.parametrize("stream", [False, True])
async def test_repeated_incomplete_response_stops_after_one_retry(recovery_env, stream):
    env = recovery_env
    _install_responses(env, ["[["])
    if stream:
        payloads = await _request(env, stream=True)
        assert payloads == [{"error": {
            "code": 502,
            "message": "HTTP 502: Upstream response ended before completion",
            "status": "INTERNAL",
        }}]
    else:
        with pytest.raises(HTTPException) as caught:
            await _request(env, stream=False)
        assert caught.value.status_code == 502
    assert env.calls == [42, 42]
    assert env.closed == [42, 42]
    assert not env.leases
    api.try_switch_account.assert_not_awaited()
    api.record_rotator_event.assert_not_called()
    api.runtime_state.record.assert_not_called()


@pytest.mark.parametrize(
    "published_part",
    [
        _part(text="partial"),
        _part(text="thinking", thought=True),
        _part(function="offline_tool"),
        _part(image=["image/png", "cG5n"]),
        _part(text="", signature="offline-signature"),
    ],
    ids=["text", "thinking", "tool", "image", "signature"],
)
@pytest.mark.parametrize("failure", ["missing_marker", "truncated_envelope"])
async def test_incomplete_response_after_visible_part_is_never_replayed(
    recovery_env, published_part, failure,
):
    env = recovery_env
    incomplete = (
        _response(published_part) if failure == "missing_marker"
        else _response(published_part, finish=1)[:-1]
    )
    upstream_closed = _install_responses(env, [incomplete])
    payloads = await _request(env, stream=True)
    assert len(payloads) == 2
    assert payloads[0]["candidates"][0]["content"]["parts"]
    assert payloads[-1]["error"]["code"] == 502
    assert not any(
        "finishReason" in candidate
        for frame in payloads for candidate in frame.get("candidates", [])
    )
    assert env.calls == [42]
    assert env.closed == [42]
    assert upstream_closed == [0]
    assert not env.leases
    env.client.clear_templates.assert_not_called()
    api.try_switch_account.assert_not_awaited()
    api.record_rotator_event.assert_not_called()
    api.runtime_state.record.assert_not_called()


@pytest.mark.parametrize("stream", [False, True])
async def test_malformed_response_does_not_retry(recovery_env, stream):
    env = recovery_env
    _install_responses(env, ["[[[null,]]]"])
    if stream:
        payloads = await _request(env, stream=True)
        assert payloads[-1]["error"]["code"] == 502
    else:
        with pytest.raises(HTTPException) as caught:
            await _request(env, stream=False)
        assert caught.value.status_code == 502
    assert env.calls == [42]
    assert env.closed == [42]
    assert not env.leases
    env.client.clear_templates.assert_not_called()
    api.try_switch_account.assert_not_awaited()
    api.runtime_state.record.assert_not_called()


async def test_buffer_limit_is_not_an_incomplete_response(recovery_env, monkeypatch):
    from aistudio_api.infrastructure.gateway import stream_parser

    monkeypatch.setattr(stream_parser, "MAX_PENDING_JSON_CHARS", 20)
    env = recovery_env
    _install_responses(env, [_response(_part(text="bounded"), finish=1)])
    payloads = await _request(env, stream=True)
    assert payloads[-1]["error"]["code"] == 502
    assert env.calls == [42]
    assert not env.leases
    env.client.clear_templates.assert_not_called()
    api.try_switch_account.assert_not_awaited()


@pytest.mark.parametrize("stream", [False, True])
async def test_cancelled_response_releases_lease_without_retry(recovery_env, stream):
    env = recovery_env
    _install_responses(env, [asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await _request(env, stream=stream)
    assert env.calls == [42]
    assert env.closed == [42]
    assert not env.leases
    env.client.clear_templates.assert_not_called()
    api.try_switch_account.assert_not_awaited()
    api.runtime_state.record.assert_not_called()


@pytest.mark.parametrize("finish", [2, 3, 4])
async def test_completed_nonstop_response_does_not_trigger_recovery(recovery_env, finish):
    env = recovery_env
    _install_responses(env, [_response(finish=finish)])
    payloads = await _request(env, stream=True)
    assert payloads[-1]["candidates"][0]["finishReason"] == {
        2: "MAX_TOKENS", 3: "SAFETY", 4: "RECITATION",
    }[finish]
    assert env.calls == [42]
    env.client.clear_templates.assert_not_called()
    api.try_switch_account.assert_not_awaited()


def test_parser_only_classifies_unfinished_eof_as_incomplete():
    parser = IncrementalJSONStreamParser()
    list(parser.feed("[["))
    with pytest.raises(IncompleteResponseError):
        list(parser.finish())
    parser = IncrementalJSONStreamParser()
    with pytest.raises(RequestError) as caught:
        list(parser.feed("[[[null,]]]"))
    assert type(caught.value) is RequestError


def test_parser_malformed_unclosed_chunk_keeps_ordinary_error():
    parser = IncrementalJSONStreamParser()
    list(parser.feed("[[[garbage"))
    with pytest.raises(RequestError) as caught:
        list(parser.finish())
    assert type(caught.value) is RequestError


@pytest.mark.parametrize(
    "raw", ["[[", _response(_part(text="unfinished"))[:-4]],
)
def test_nonstream_recognizes_unfinished_json_prefix(raw):
    with pytest.raises(IncompleteResponseError) as caught:
        parse_text_output(raw)
    assert caught.value.status == 502


@pytest.mark.parametrize("raw", ["invalid json", "[[[null,]]]", "[[[]]]junk"])
def test_nonstream_malformed_json_keeps_ordinary_error(raw):
    with pytest.raises(RequestError) as caught:
        parse_text_output(raw)
    assert type(caught.value) is RequestError


@pytest.mark.parametrize(
    "prefix",
    ["n", "nu", "nul", "t", "tr", "tru", "f", "fa", "fal", "fals",
     "-", "1.", "-0.", "1e", "-0e+", "1.25E-",
     r'"\u', r'"\u1', r'"\u12', r'"\u123', r'"\u1234'],
)
@pytest.mark.parametrize("stream", [False, True])
def test_valid_unfinished_scalar_tail_is_incomplete(prefix, stream):
    raw = "[[[" + prefix
    with pytest.raises(IncompleteResponseError):
        if stream:
            parser = IncrementalJSONStreamParser()
            list(parser.feed(raw))
            list(parser.finish())
        else:
            parse_text_output(raw)


@pytest.mark.parametrize(
    "tail",
    ["nullx", "trux", "1ee", "01", "01e", "1.e", "1e ", "nul ",
     "1 2e", "true tru", ",1e", '"\\q', '"\\uZZ',
     r'"\u12Z', r'"\u123Z', r'"\u12"', r'"\u12 ', r'"\u12\n'],
)
@pytest.mark.parametrize("stream", [False, True])
def test_malformed_scalar_neighbors_are_not_incomplete(tail, stream):
    raw = "[[[" + tail
    with pytest.raises(RequestError) as caught:
        if stream:
            parser = IncrementalJSONStreamParser()
            list(parser.feed(raw))
            list(parser.finish())
        else:
            parse_text_output(raw)
    assert type(caught.value) is RequestError


@pytest.mark.parametrize("prefix", ["nul", "tru", "1e", "1e+", r'"\u12'])
@pytest.mark.parametrize("stream", [False, True])
async def test_valid_unfinished_scalar_tail_recovers_before_output(
    recovery_env, prefix, stream,
):
    env = recovery_env
    _install_responses(
        env, ["[[[" + prefix, _response(_part(text="complete"), finish=1)]
    )
    response = await _request(env, stream=stream)
    if stream:
        assert response[-1]["candidates"][0]["finishReason"] == "STOP"
    else:
        assert response.candidates[0].finishReason == "STOP"
    assert env.calls == [42, 42]
    assert env.closed == [42, 42]
    assert not env.leases
    api.try_switch_account.assert_not_awaited()
