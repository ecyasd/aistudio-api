"""Synthetic response-shape probes never retain prompts, text or signatures."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import Depends, FastAPI

from aistudio_api.api import routes_errors
from aistudio_api.api.dependencies import require_web_auth
from aistudio_api.api.error_middleware import ErrorRecordsMiddleware
from aistudio_api.api.schemas import GeminiGenerateContentRequest
from aistudio_api.application import api_service_gemini as api, continuation
from aistudio_api.application.chat_service import normalize_gemini_request
from aistudio_api.config import settings
from aistudio_api.domain.errors import RequestError
from aistudio_api.domain.models import Candidate, ContentPart
from aistudio_api.infrastructure.gateway.capture import CapturedRequest
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.gateway.streaming import StreamingGateway
from aistudio_api.infrastructure.gateway.wire_parser import parse_text_output
from aistudio_api.infrastructure.utils import (
    error_records as errors,
    response_diagnostics as diagnostics,
)


@pytest.fixture
def diagnostic_store(monkeypatch):
    store = diagnostics.ResponseDiagnosticStore()
    monkeypatch.setattr(diagnostics, "get_response_diagnostic_store", lambda: store)
    monkeypatch.setattr(diagnostics, "logger", MagicMock())
    monkeypatch.setattr(errors, "_store", errors.ErrorRecordStore())
    return store


@contextmanager
def trace(*, stream=False, auto_continue=False, model="gemini-4-argon"):
    token = errors.bind_request_context()
    try:
        diagnostics.initialize_response_diagnostics(stream=stream)
        diagnostics.configure_generation(model=model, auto_continue=auto_continue)
        yield errors.get_request_context()
    finally:
        errors.reset_request_context(token)


def part(text=None, *, thought=False, signature=None):
    raw = [None] * 15
    raw[1], raw[12], raw[14] = text, 1 if thought else None, signature
    return raw


def chunk(*parts, finish=None):
    return [[[ [list(parts), "model"], finish ]], None, None]


def wire(*chunks):
    return json.dumps([list(chunks)])


def client_for(responses):
    pending = iter(responses)

    @asynccontextmanager
    async def request_scope():
        yield SimpleNamespace(auth_file="synthetic-auth", context_id=42)

    async def replay(captured, **kwargs):
        return 200, next(pending).encode()

    async def transport(**kwargs):
        yield "status", 200
        yield "chunk", next(pending).encode()

    client = object.__new__(AIStudioClient)
    client._session = SimpleNamespace(request_scope=request_scope)
    client._capture_service = SimpleNamespace(clear_templates=MagicMock())
    client._replay_service = SimpleNamespace(replay=replay)
    client._streaming_gateway = StreamingGateway(SimpleNamespace(send_streaming_request=transport))
    client._dump_raw_exchange = lambda **kwargs: None
    client.capture_request = AsyncMock(return_value=CapturedRequest(
        url="https://synthetic.invalid/generate", headers={}, body="[]",
    ))
    return client


def summary(index, stamp):
    return {"request_id": f"req_{index:032x}", "time": stamp.isoformat(),
            "outcome": "success", "model": "gemini-4-argon"}


def test_store_is_independent_bounded_expires_and_copies_arrays():
    now = [datetime(2026, 10, 22, tzinfo=UTC)]
    store = diagnostics.ResponseDiagnosticStore(clock=lambda: now[0])
    assert store.list_records() == {"items": [], "retention": {"minutes": 10, "max_records": 100}}
    for index in range(105):
        store.record({**summary(index, now[0]), "finish_reasons": [1]})
    page = store.list_records(limit=100)
    assert len(page["items"]) == 100
    assert page["items"][0]["request_id"] == f"req_{104:032x}"
    page["items"][0]["finish_reasons"].append(2)
    assert store.list_records()["items"][0]["finish_reasons"] == [1]
    assert diagnostics.ResponseDiagnosticStore().list_records()["items"] == []
    now[0] += timedelta(minutes=10, milliseconds=1)
    assert store.list_records()["items"] == []


def test_store_reconstructs_only_strict_safe_fields():
    store = diagnostics.ResponseDiagnosticStore()
    secret = "SYNTHETIC_SECRET_NOT_TO_RETAIN"
    store.record({**summary(1, datetime.now(UTC)), "model": "gemini-4-argon/" + secret,
                  "prompt": secret, "signature": secret, "headers": {secret: secret},
                  "outcome": [], "error_kind": [], "retries": True,
                  "finish_reasons": [True, -1, 1, 256, secret],
                  "phase_sequence": [secret, "body"], "flags": [secret, "marker_in_body"]})
    item = store.list_records()["items"][0]
    assert secret not in json.dumps(item)
    assert item["model"] is None and item["error_kind"] is None
    assert item["outcome"] == "failed" and item["retries"] == 0
    assert item["finish_reasons"] == [1]
    assert item["phase_sequence"] == ["body"]
    assert item["flags"] == ["marker_in_body"]


@pytest.mark.parametrize("stream", [False, True])
async def test_same_parser_counts_stream_and_nonstream_once_without_changing_parts(diagnostic_store, stream):
    secret = "SYNTHETIC_PRIVATE_REASONING"
    signature = "SYNTHETIC_SIGNATURE_NEVER_STORE"
    raw = wire(chunk(part(secret, thought=True)), chunk(part("A[:")),
               chunk(part("3]B")), chunk(part(thought=True, signature=signature)),
               chunk(part("late", thought=True)), chunk(finish=1), chunk(part("after")))
    with trace(stream=stream) as context:
        diagnostics.begin_upstream_call()
        if stream:
            async def transport(**kwargs):
                yield "status", 200
                for character in raw:
                    yield "chunk", character.encode()

            gateway = StreamingGateway(SimpleNamespace(send_streaming_request=transport))
            events = [event async for event in gateway.stream_chat(
                captured=CapturedRequest(url="https://synthetic.invalid", headers={}, body="[]"),
                model="gemini-4-argon",
            )]
            native = [p for kind, payload in events if kind == "parts" for p in payload]
        else:
            output = parse_text_output(raw)
            native = output.candidates[0].parts
            assert output.text == "A[:3]Bafter"
        assert native[3].thought_signature == signature
        diagnostics.finish_response_diagnostics("success", 200)
        diagnostics.finish_response_diagnostics("success", 200)
        # A late child task cannot mutate a finalized request.
        diagnostics.observe_candidate(Candidate(parts=[ContentPart(text="late child")]))
        assert context.response_diagnostics.completed is True
    item = diagnostic_store.list_records()["items"][0]
    assert len(diagnostic_store.list_records()["items"]) == 1
    assert item["body_chars"] == len("A[:3]Bafter")
    assert item["thought_chars"] == len(secret) + len("late")
    assert item["thought_after_body"] == item["empty_thought_parts"] == item["marker_count"] == 1
    assert item["phase_sequence"] == ["thought", "body", "empty_thought", "thought", "finish", "body"]
    assert set(item["flags"]) == {"marker_in_body", "thought_after_body", "empty_thought", "content_after_finish"}
    encoded = json.dumps(item)
    assert secret not in encoded and signature not in encoded and "A[:3]B" not in encoded
    assert diagnostics.logger.warning.call_count == 1
    assert diagnostics.logger.warning.call_args.args[0] == "response_diagnostic %s"


def test_marker_state_crosses_parts_but_not_upstream_rounds_and_sequences_are_bounded(diagnostic_store):
    with trace(auto_continue=True):
        diagnostics.begin_upstream_call()
        for text in ("[", ":", "123", "]", "[:]", "[:x]", "[:1", "2]", "[[:3]"):
            diagnostics.observe_candidate(Candidate(parts=[ContentPart(text=text)]))
        diagnostics.observe_candidate(Candidate(parts=[ContentPart(text="[:")], finish_reason=2))
        diagnostics.note_continuation()
        diagnostics.begin_upstream_call()
        diagnostics.observe_candidate(Candidate(parts=[ContentPart(text="3]", thought=True)]))
        for _ in range(100):
            diagnostics.observe_candidate(Candidate(parts=[ContentPart(text="x"), ContentPart(text="y", thought=True)], finish_reason=2))
        diagnostics.finish_response_diagnostics("success", 200)
    item = diagnostic_store.list_records()["items"][0]
    assert item["marker_count"] == 3
    assert item["upstream_calls"] == 2 and item["retries"] == 0
    assert {"auto_continued", "sequence_limited"} <= set(item["flags"])
    assert len(item["phase_sequence"]) == len(item["finish_reasons"]) == 32


@pytest.mark.parametrize("field,payload", [
    ("executable_code", {"language": "PYTHON", "code": "SYNTHETIC_PRIVATE_CODE"}),
    ("code_execution_result", {"outcome": "OUTCOME_OK", "output": "SYNTHETIC_PRIVATE_OUTPUT"}),
])
@pytest.mark.parametrize("after_finish", [False, True])
def test_code_payload_is_not_an_empty_thought_and_is_never_retained(
    diagnostic_store, field, payload, after_finish,
):
    with trace():
        diagnostics.begin_upstream_call()
        if after_finish:
            diagnostics.observe_candidate(Candidate(finish_reason=1))
        diagnostics.observe_candidate(Candidate(parts=[
            ContentPart(thought=True, **{field: payload}),
        ]))
        diagnostics.finish_response_diagnostics("success", 200)
    item = diagnostic_store.list_records()["items"][0]
    assert item["empty_thought_parts"] == item["thought_chars"] == 0
    assert "empty_thought" not in item["flags"]
    assert ("content_after_finish" in item["flags"]) is after_finish
    assert item["phase_sequence"][-1] == "thought"
    assert "SYNTHETIC_PRIVATE" not in json.dumps(item)


async def test_request_contexts_isolate_concurrent_and_sequential_requests(diagnostic_store):
    gate = asyncio.Event()

    async def request(model, text, wait):
        with trace(model=model):
            diagnostics.begin_upstream_call()
            if wait:
                await gate.wait()
            else:
                gate.set()
            diagnostics.observe_candidate(Candidate(parts=[ContentPart(text=text)], finish_reason=1))
            diagnostics.finish_response_diagnostics("success", 200)

    await asyncio.gather(request("gemini-a", "[:3]", True), request("gemini-b", "plain", False))
    await request("gemini-c", "last", False)
    items = {item["model"]: item for item in diagnostic_store.list_records()["items"]}
    assert items["gemini-a"]["marker_count"] == 1
    assert items["gemini-b"]["marker_count"] == items["gemini-c"]["marker_count"] == 0
    assert len({item["request_id"] for item in items.values()}) == 3
    assert errors.get_request_context() is None


def test_retry_counters_and_new_round_reset_thought_phase(diagnostic_store):
    with trace():
        diagnostics.begin_upstream_call()
        diagnostics.observe_candidate(Candidate(parts=[ContentPart(text="[:")]))
        diagnostics.note_attempt(1)
        diagnostics.begin_upstream_call()
        diagnostics.observe_candidate(Candidate(parts=[ContentPart(text="3]", thought=True)], finish_reason=1))
        errors.record_failure(RequestError(502, "SYNTHETIC_SECRET"), source="gemini", operation="generate_attempt", retrying=True)
        diagnostics.finish_response_diagnostics("recovered", 200)
    item = diagnostic_store.list_records()["items"][0]
    assert item["upstream_calls"] == 2 and item["retries"] == 1
    assert item["thought_after_body"] == item["marker_count"] == 0
    assert item["outcome"] == "recovered" and item["error_kind"] == "upstream"
    assert set(item["flags"]) == {"multiple_upstream_calls", "retried"}


@pytest.mark.parametrize("stream", [False, True])
async def test_client_counts_only_capture_success_and_continuation_has_explicit_flag(diagnostic_store, stream):
    client = client_for([wire(chunk(part("A"), finish=2)), wire(chunk(part("B"), finish=1))])
    req = GeminiGenerateContentRequest.model_validate({
        "contents": [{"role": "user", "parts": [{"text": "offline synthetic prompt"}]}], "tools": [],
    })
    normalized = normalize_gemini_request(req, "gemini-4-argon")
    with trace(stream=stream, auto_continue=True):
        capture = client.capture_request
        client.capture_request = AsyncMock(return_value=None)
        if stream:
            with pytest.raises(RequestError):
                async for _ in client.stream_generate_content(**continuation._arguments(normalized)):
                    pass
        else:
            with pytest.raises(RequestError):
                await client.generate_content(**continuation._arguments(normalized))
        # A failed capture used one API attempt without starting an upstream
        # replay; the subsequent continuation still needs its explicit flag.
        diagnostics.note_attempt(1)
        client.capture_request = capture
        if stream:
            events = [event async for event in continuation.stream_generate_content(client, normalized, auto_continue=True)]
            assert "".join(p.text or "" for kind, payload in events if kind == "parts" for p in payload) == "AB"
        else:
            output = await continuation.generate_content(client, normalized, auto_continue=True)
            assert output.text == "AB"
        diagnostics.finish_response_diagnostics("success", 200)
    item = diagnostic_store.list_records()["items"][0]
    assert item["upstream_calls"] == 2 and item["retries"] == 1
    assert item["body_chars"] == 2 and item["finish_reasons"] == [2, 1]
    assert "auto_continued" in item["flags"]


@pytest.mark.parametrize("stream", [False, True])
async def test_api_retry_and_continuation_counters_share_one_inbound_context(diagnostic_store, monkeypatch, stream):
    client = client_for([wire(chunk()), wire(chunk(part("A"), finish=2)), wire(chunk(part("B"), finish=1))])
    monkeypatch.setattr(api, "ensure_active_account", AsyncMock())
    monkeypatch.setattr(api, "validate_account_availability", MagicMock())
    monkeypatch.setattr(api, "account_id_for_auth_file", lambda _: "synthetic-account")
    monkeypatch.setattr(api, "record_rotator_event", MagicMock())
    monkeypatch.setattr(api.runtime_state, "record", MagicMock())
    req = GeminiGenerateContentRequest.model_validate({
        "contents": [{"role": "user", "parts": [{"text": "offline synthetic prompt"}]}],
        "tools": [], "autoContinue": True,
    })
    with trace(stream=stream):
        response = await api.handle_gemini_generate_content("gemini-4-argon", req, client, stream=stream)
        if stream:
            frames = [json.loads(frame[6:]) async for frame in response.body_iterator if frame.startswith("data: ")]
            assert not any("error" in frame for frame in frames)
        else:
            assert "".join(part.text or "" for part in response.candidates[0].content.parts) == "AB"
        diagnostics.finish_response_diagnostics("recovered", 200)
    item = diagnostic_store.list_records()["items"][0]
    assert item["upstream_calls"] == 3 and item["retries"] == 1
    assert item["auto_continue"] is True and item["finish_reasons"] == [2, 1]
    assert {"auto_continued", "retried", "multiple_upstream_calls"} <= set(item["flags"])


def test_diagnostic_and_logging_failures_do_not_escape_or_suppress_each_other(diagnostic_store, monkeypatch):
    diagnostics.logger.info.side_effect = RuntimeError("synthetic log failure")
    with trace():
        diagnostics.finish_response_diagnostics("success", 200)
    assert len(diagnostic_store.list_records()["items"]) == 1
    monkeypatch.setattr(diagnostics, "get_response_diagnostic_store", MagicMock(side_effect=RuntimeError("synthetic store failure")))
    with trace():
        diagnostics.finish_response_diagnostics("success", 200)
    assert diagnostics.logger.info.call_count == 2
    diagnostics.begin_upstream_call()
    diagnostics.observe_candidate(Candidate(parts=[ContentPart(text="outside request")]))


@pytest.mark.parametrize("failure", [None, "failed", "cancelled", "disconnect"])
async def test_middleware_records_success_failure_cancel_and_disconnect(diagnostic_store, failure):
    async def endpoint(scope, receive, send):
        diagnostics.configure_generation(model="gemini-4-argon", auto_continue=False)
        diagnostics.begin_upstream_call()
        diagnostics.observe_candidate(Candidate(parts=[ContentPart(text="[:3]")]))
        if failure == "failed":
            raise RuntimeError("SYNTHETIC_PRIVATE_EXCEPTION")
        if failure == "cancelled":
            raise asyncio.CancelledError
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    messages = []

    async def send(message):
        if failure == "disconnect" and message["type"] == "http.response.body":
            raise BrokenPipeError("SYNTHETIC_PRIVATE_NETWORK")
        messages.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    middleware = ErrorRecordsMiddleware(ErrorRecordsMiddleware(endpoint))
    scope = {"type": "http", "path": "/v1beta/models/gemini-4-argon:generateContent", "headers": []}
    if failure:
        with pytest.raises((RuntimeError, asyncio.CancelledError, BrokenPipeError)):
            await middleware(scope, receive, send)
    else:
        await middleware(scope, receive, send)
    items = diagnostic_store.list_records()["items"]
    assert len(items) == 1
    assert items[0]["outcome"] == ({None: "success", "failed": "failed", "cancelled": "cancelled", "disconnect": "cancelled"}[failure])
    assert items[0]["marker_count"] == 1
    if messages:
        assert dict(messages[0]["headers"])[b"x-request-id"].decode() == items[0]["request_id"]
    assert "SYNTHETIC_PRIVATE" not in json.dumps(items)
    assert errors.get_request_context() is None


async def test_errors_diagnostics_are_opt_in_and_require_existing_management_auth(diagnostic_store, monkeypatch):
    with trace():
        diagnostics.finish_response_diagnostics("success", 200)
    monkeypatch.setattr(routes_errors, "get_response_diagnostic_store", lambda: diagnostic_store)
    monkeypatch.setattr(settings, "web_password", "synthetic-management-password")
    unexpected = AsyncMock(side_effect=AssertionError("Diagnostics must not invoke a model"))
    monkeypatch.setattr(AIStudioClient, "generate_content", unexpected)
    app = FastAPI()
    app.include_router(routes_errors.router, dependencies=[Depends(require_web_auth)])
    headers = {"Authorization": "Bearer synthetic-management-password"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://synthetic.invalid") as client:
        assert (await client.get("/errors?include_diagnostics=true")).status_code == 401
        ordinary = await client.get("/errors", headers=headers)
        detailed = await client.get("/errors?include_diagnostics=true", headers=headers)
    assert "diagnostics" not in ordinary.json()
    assert detailed.status_code == 200
    assert len(detailed.json()["diagnostics"]["items"]) == 1
    assert detailed.headers["cache-control"] == "no-store"
    unexpected.assert_not_awaited()


async def test_auth_rejections_do_not_fill_response_window_or_write_summary(diagnostic_store):
    async def denied(scope, receive, send):
        await send({"type": "http.response.start", "status": 401, "headers": []})
        await send({"type": "http.response.body", "body": b"denied"})

    async def receive():
        raise AssertionError("Auth denial must not read request body")

    async def send(message):
        return None

    middleware = ErrorRecordsMiddleware(denied)
    for _ in range(25):
        await middleware({"type": "http", "path": "/v1beta/models/gemini-4-argon:generateContent", "headers": []}, receive, send)
    assert diagnostic_store.list_records()["items"] == []
    diagnostics.logger.info.assert_not_called()
    diagnostics.logger.warning.assert_not_called()
