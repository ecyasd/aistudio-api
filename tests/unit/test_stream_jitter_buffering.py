"""Offline regressions for stream jitter buffering, backlog coalescing, and rhythm diagnostics."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from aistudio_api.api import gemini as service
from aistudio_api.api.error_middleware import ErrorRecordsMiddleware
from aistudio_api.api.gemini import (
    SSE_BUFFER_MAX_EVENTS,
    _sse_with_heartbeats,
    _SSEBuffer,
)
from aistudio_api.api.schemas import GeminiGenerateContentRequest
from aistudio_api.api.sse_delivery import SSEDelivery
from aistudio_api.infrastructure.utils import (
    error_records,
    response_diagnostics,
)


@pytest.fixture
def jitter_env(monkeypatch):
    from aistudio_api.application import (
        generation as generation_execution,
        generation_recovery as recovery,
    )
    from aistudio_api.application.account_rotator import AccountStats

    accounts = [SimpleNamespace(id="jitter-account", auth_generation=None)]
    stats = {"jitter-account": AccountStats(account_id="jitter-account")}
    context = SimpleNamespace(
        auth_file="offline/jitter-account", context_id=1, auth_generation=None
    )
    leased = []

    @asynccontextmanager
    async def request_scope():
        leased.append(context)
        try:
            yield context
        finally:
            leased.remove(context)

    client = SimpleNamespace(
        stream_generate_content=MagicMock(side_effect=AssertionError("unexpected model call")),
        generate_content=AsyncMock(side_effect=AssertionError("unexpected model call")),
        clear_templates=MagicMock(),
        session=SimpleNamespace(request_scope=request_scope),
    )
    account_service = SimpleNamespace(
        list_accounts=lambda: accounts,
        get_active_account=lambda: accounts[0] if accounts else None,
        get_account=lambda account_id: next((a for a in accounts if a.id == account_id), None),
        get_account_auth_path=lambda account_id: f"offline/{account_id}",
    )
    rotator = SimpleNamespace(get_account_stats=lambda account_id: stats.get(account_id))
    monkeypatch.setattr(recovery.runtime_state, "account_service", account_service)
    monkeypatch.setattr(recovery.runtime_state, "rotator", rotator)
    monkeypatch.setattr(recovery.runtime_state, "record", MagicMock())
    monkeypatch.setattr(recovery, "record_rotator_event", MagicMock())
    monkeypatch.setattr(generation_execution, "record_rotator_event", recovery.record_rotator_event)
    monkeypatch.setattr(generation_execution, "ensure_active_account", AsyncMock())
    monkeypatch.setattr(recovery, "try_switch_account", AsyncMock(return_value=False))
    monkeypatch.setattr(service, "SSE_HEARTBEAT_INTERVAL_SECONDS", 0.05)
    return SimpleNamespace(
        service=service, accounts=accounts, stats=stats, client=client,
        context=context, leased=leased,
    )


@pytest.mark.asyncio
async def test_sse_buffer_respects_byte_and_event_bounds():
    buf = _SSEBuffer(max_bytes=100, max_events=3, coalesce=False)
    buf.allow_backlog()
    # Put 2 items within limit
    await buf.put("data: chunk1\n\n")  # len 14
    await buf.put("data: chunk2\n\n")  # len 14
    assert buf.buffered_bytes == 28

    # Third item reaches max_events = 3
    await buf.put("data: chunk3\n\n")
    assert buf.buffered_bytes == 42

    # Fourth item must wait because max_events is 3
    put_task = asyncio.create_task(buf.put("data: chunk4\n\n"))
    await asyncio.sleep(0.01)
    assert not put_task.done()

    # Reading an item frees space and lets put proceed
    first = await buf.get()
    assert first == "data: chunk1\n\n"
    await asyncio.wait_for(put_task, timeout=0.1)
    assert buf.buffered_bytes == 42


@pytest.mark.asyncio
async def test_sse_buffer_admits_oversized_item_when_empty_to_prevent_deadlock():
    # max_bytes is 50, but single chunk is 120 bytes
    buf = _SSEBuffer(max_bytes=50, max_events=5, coalesce=False)
    large_chunk = "data: " + ("x" * 100) + "\n\n"
    await buf.put(large_chunk)
    assert buf.buffered_bytes > 50

    # Next chunk must wait because buffer is over capacity
    put_task = asyncio.create_task(buf.put("data: small\n\n"))
    await asyncio.sleep(0.01)
    assert not put_task.done()

    got = await buf.get()
    assert got == large_chunk
    await asyncio.wait_for(put_task, timeout=0.1)
    assert await buf.get() == "data: small\n\n"


@pytest.mark.asyncio
async def test_sse_buffer_first_chunk_never_coalesces_even_if_multiple_queued():
    buf = _SSEBuffer(max_bytes=1000, max_events=10, coalesce=True)
    buf.allow_backlog()
    await buf.put("data: first\n\n")
    await buf.put("data: second\n\n")
    await buf.put("data: third\n\n")

    # First get must yield ONLY the first chunk immediately for TTFT
    first = await buf.get()
    assert first == "data: first\n\n"

    # Subsequent get coalesces the remaining backlog
    second = await buf.get()
    assert second == "data: second\n\ndata: third\n\n"


@pytest.mark.asyncio
async def test_sse_buffer_fast_downstream_yields_uncoalesced():
    buf = _SSEBuffer(max_bytes=1000, max_events=10, coalesce=True)
    await buf.put("data: first\n\n")
    assert await buf.get() == "data: first\n\n"

    # When downstream is fast and queue is empty, next put/get does not coalesce
    await buf.put("data: second\n\n")
    assert await buf.get() == "data: second\n\n"


@pytest.mark.asyncio
async def test_sse_buffer_errors_never_block_and_never_coalesce():
    buf = _SSEBuffer(max_bytes=20, max_events=1, coalesce=True)
    await buf.put("data: partial\n\n")

    # Error chunk or Exception can be put even if buffer reached max_events=1
    exc = RuntimeError("synthetic error")
    await buf.put(exc)

    first = await buf.get()
    assert first == "data: partial\n\n"

    second = await buf.get()
    assert second is exc


@pytest.mark.asyncio
async def test_simulated_network_jitter_coalesces_backlog_in_asgi(jitter_env, monkeypatch):
    diagnostics = response_diagnostics.ResponseDiagnosticStore()
    errors = error_records.ErrorRecordStore()
    monkeypatch.setattr(response_diagnostics, "get_response_diagnostic_store", lambda: diagnostics)
    monkeypatch.setattr(error_records, "get_error_record_store", lambda: errors)

    async def upstream(**_kwargs):
        # Yield first chunk
        yield "body", "token1"
        # Simulate quick bursts from upstream while downstream stalls
        yield "body", "token2"
        yield "body", "token3"
        yield "finish_reason", 1
        yield "done", None

    jitter_env.client.stream_generate_content.side_effect = upstream
    req = GeminiGenerateContentRequest.model_validate(
        {"contents": [{"role": "user", "parts": [{"text": "test jitter"}]}]}
    )
    response = jitter_env.service._build_gemini_streaming_response(
        client=jitter_env.client, req=req, model_path="gemini-test"
    )

    async def endpoint(scope, receive, send):
        await receive()
        response_diagnostics.configure_generation(model="gemini-test", auto_continue=False)
        await response(scope, receive, send)

    app = ErrorRecordsMiddleware(endpoint)

    sent_messages = []
    send_count = 0

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        nonlocal send_count
        if message["type"] == "http.response.body" and message.get("body"):
            send_count += 1
            if send_count == 1:
                # Downstream network stalls briefly on the first body chunk
                await asyncio.sleep(0.04)
        sent_messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"spec_version": "2.4"},
        "method": "POST",
        "path": "/v1beta/models/gemini-test:streamGenerateContent",
        "headers": [],
        "query_string": b"",
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=2.0)

    body_messages = [m for m in sent_messages if m["type"] == "http.response.body" and m.get("body")]
    all_body_text = b"".join(m["body"] for m in body_messages).decode("utf-8")
    assert "token1" in all_body_text
    assert "token2" in all_body_text
    assert "token3" in all_body_text
    assert '"finishReason": "STOP"' in all_body_text

    # Diagnostic records check
    records = diagnostics.list_records()["items"]
    assert len(records) == 1
    diag = records[0]
    assert diag["outcome"] == "success"
    # Coalescing occurred when downstream stalled on chunk 1
    assert diag["coalesced_events"] >= 1
    assert diag["max_queue_bytes"] > 0
    assert diag["send_wait_ms"] > 0
    assert diag["max_send_wait_ms"] > 0


@pytest.mark.asyncio
async def test_rhythm_diagnostics_track_upstream_gap_and_ack_wait(monkeypatch):
    diagnostics = response_diagnostics.ResponseDiagnosticStore()
    monkeypatch.setattr(response_diagnostics, "get_response_diagnostic_store", lambda: diagnostics)

    token = error_records.bind_request_context("req_test_rhythm")
    try:
        response_diagnostics.initialize_response_diagnostics(stream=True)
        response_diagnostics.configure_generation(model="gemini-test", auto_continue=False)

        response_diagnostics.observe_upstream_gap_ms(450)
        response_diagnostics.observe_upstream_gap_ms(120)
        response_diagnostics.observe_queue_bytes(2048)
        response_diagnostics.observe_send_wait_ms(80)
        response_diagnostics.observe_send_wait_ms(150)
        response_diagnostics.observe_ack_wait_ms(30)
        response_diagnostics.observe_ack_wait_ms(60)
        response_diagnostics.note_coalesced_events(2)

        response_diagnostics.finish_response_diagnostics("success", 200)
    finally:
        error_records.reset_request_context(token)

    row = diagnostics.list_records()["items"][0]
    assert row["max_upstream_gap_ms"] == 450
    assert row["max_queue_bytes"] == 2048
    assert row["send_wait_ms"] == 230
    assert row["max_send_wait_ms"] == 150
    assert row["ack_wait_ms"] == 90
    assert row["max_ack_wait_ms"] == 60
    assert row["coalesced_events"] == 2


async def test_first_write_does_not_prefill_the_larger_buffer():
    produced = []

    async def source(emit):
        for i in range(100):
            produced.append(i)
            await emit(f"data: {i}\n\n")

    stream = _sse_with_heartbeats(source, interval=1, max_buffer_events=8, coalesce=True)
    try:
        assert await anext(stream) == "data: 0\n\n"
        # Same scheduling bound as the original single-slot queue: the next
        # event may be queued and one more being prepared, never eight.
        assert len(produced) <= 3
    finally:
        await stream.aclose()


@pytest.mark.parametrize("events,merge", [(1, False), (8, False), (8, True)])
async def test_jitter_keeps_exact_sse_bytes_and_error_boundary(events, merge):
    from aistudio_api.domain.errors import RequestError
    from aistudio_api.domain.models import ContentPart

    frames = [
        service.format_sse_event("body", "首字🙂"),
        service.format_sse_event("parts", [ContentPart(text="思考", thought=True)]),
        service.format_sse_event("parts", [ContentPart(text="", thought_signature="synthetic-signature")]),
        service.format_sse_event("parts", [ContentPart(function_call={"name": "tool", "args": {"x": 1}})]),
        service.format_sse_event("body", '正文\n引号"与反斜杠\\'),
        service.format_sse_error(RequestError(503, "synthetic")),
    ]
    frames = [frame for frame in frames if frame is not None]
    produced = []

    async def source(emit):
        for i, frame in enumerate(frames):
            await emit(frame)
            produced.append(i)

    stream = _sse_with_heartbeats(
        source, interval=1, max_buffer_events=events, coalesce=merge,
    )
    chunks = []
    try:
        chunks.append(await anext(stream))
        chunks.append(await anext(stream))
        # The caller is paused here, as during an ASGI write. Let the source
        # fill its available space without using a network or wall-clock delay.
        for _ in range(10):
            await asyncio.sleep(0)
        assert len(produced) >= (len(frames) if events > 1 else 3)
        chunks.extend([chunk async for chunk in stream])
    finally:
        await stream.aclose()
    assert chunks[0] == frames[0]
    assert "".join(chunks).encode() == "".join(frames).encode()
    assert chunks[-1] == frames[-1]  # Error remains a separate event/write.
    decoded = [json.loads(frame.removeprefix("data: ")) for frame in "".join(chunks).split("\n\n") if frame]
    assert decoded[2]["candidates"][0]["content"]["parts"][0]["thoughtSignature"] == "synthetic-signature"
    assert decoded[-1]["error"]["code"] == 503


async def test_utf8_byte_budget_blocks_before_event_budget():
    frame = "data: 中文🙂\n\n"
    size = len(frame.encode())
    buf = _SSEBuffer(max_bytes=size * 2, max_events=8, coalesce=True)
    buf.allow_backlog()
    await buf.put(frame)
    await buf.put(frame)
    task = asyncio.create_task(buf.put(frame))
    try:
        await asyncio.sleep(0)
        assert not task.done() and buf.buffered_bytes == size * 2
        assert await buf.get() == frame
        await asyncio.wait_for(task, 1)
        assert buf.buffered_bytes == size * 2
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.parametrize("failure", [False, True])
async def test_completion_or_failure_does_not_overtake_queued_data(failure):
    closed = asyncio.Event()

    async def source(emit):
        try:
            for i in range(20):
                await emit(f"data: {i}\n\n")
            if failure:
                raise ValueError("synthetic")
        finally:
            closed.set()

    stream = _sse_with_heartbeats(source, interval=1, max_buffer_events=8, coalesce=True)
    chunks = []
    try:
        while True:
            chunks.append(await anext(stream))
            await asyncio.sleep(0)
    except (StopAsyncIteration, ValueError) as exc:
        assert isinstance(exc, ValueError) == failure
    finally:
        await stream.aclose()
    assert "".join(chunks) == "".join(f"data: {i}\n\n" for i in range(20))
    assert closed.is_set()


async def test_full_production_buffer_cancels_without_leaving_a_producer():
    count = 0
    closed = asyncio.Event()

    async def source(emit):
        nonlocal count
        try:
            for i in range(1000):
                count += 1
                await emit(f"data: {i}\n\n")
        finally:
            closed.set()

    stream = _sse_with_heartbeats(source, interval=1, max_buffer_events=SSE_BUFFER_MAX_EVENTS, coalesce=True)
    await anext(stream)
    await anext(stream)
    for _ in range(10):
        await asyncio.sleep(0)
    # Two returned writes: one first event and at most one coalesced batch.
    assert count <= 1 + SSE_BUFFER_MAX_EVENTS * 2 + 1
    await stream.aclose()
    assert closed.is_set()
    assert not [task for task in asyncio.all_tasks() if task.get_name().startswith("gemini-sse-") and not task.done()]


async def test_native_gap_excludes_consumer_pause_and_ack_time(monkeypatch):
    from aistudio_api.infrastructure.browser.cdp_client import CDPPage
    from aistudio_api.infrastructure.browser.scripts import (
        STREAM_ACK_JS,
        STREAMING_INIT_JS,
    )
    from aistudio_api.infrastructure.gateway.capture import CapturedRequest
    from aistudio_api.infrastructure.gateway.streaming import StreamingGateway
    from aistudio_api.infrastructure.gateway.transport import XHRStreamTransport

    clock = [1000.0]
    page = MagicMock(spec=CDPPage)
    page.has_stream_binding = False
    page.is_closed.return_value = False
    page.get_cookies = AsyncMock(return_value=[])
    page.add_binding = AsyncMock()
    callbacks = {}
    page.on_binding.side_effect = lambda name, callback: callbacks.setdefault(name, callback)
    transport = XHRStreamTransport()
    diagnostics = response_diagnostics.ResponseDiagnosticStore()
    monkeypatch.setattr(response_diagnostics, "get_response_diagnostic_store", lambda: diagnostics)
    monkeypatch.setattr("aistudio_api.infrastructure.gateway.transport.current_monotonic", lambda: clock[0])
    acked = []
    tasks = []

    def chunk(text, finish=None):
        part = [None] * 15
        part[1] = text
        return [[[[[part], "model"], finish]], None, None]

    first = "[[" + json.dumps(chunk("首字")) + ","
    second = json.dumps(chunk("后续", 1)) + "]]"
    pending_events = iter([
        {"type": "chunk", "text": first, "seq": 2},
        {"type": "chunk", "text": second, "seq": 3},
        {"type": "done", "seq": 4},
    ])

    async def push_later(rid):
        await asyncio.sleep(0)
        clock[0] += 0.125
        callbacks["__aistudio_stream_push__"](json.dumps({"rid": rid, **next(pending_events)}))

    async def evaluate(script, args=None, **kwargs):
        if script == STREAMING_INIT_JS:
            callbacks["__aistudio_stream_push__"](json.dumps({"rid": args["rid"], "type": "status", "status": 200, "seq": 1}))
            return {"started": True}
        if script == STREAM_ACK_JS:
            acked.append(args["seq"])
            clock[0] += 0.03125
            if args["seq"] < 4:
                tasks.append(asyncio.create_task(push_later(args["rid"])))
        return None

    page.evaluate = AsyncMock(side_effect=evaluate)

    def send_request(**kwargs):
        return transport.send_streaming_request(page, **kwargs)

    gateway = StreamingGateway(SimpleNamespace(send_streaming_request=send_request))
    token = error_records.bind_request_context()
    try:
        response_diagnostics.initialize_response_diagnostics(stream=True)
        response_diagnostics.configure_generation(model="gemini-test", auto_continue=False)
        parts = []
        async for kind, value in gateway.stream_chat(
            captured=CapturedRequest(url="https://synthetic.invalid", headers={}, body="[]"), model="gemini-test",
        ):
            if kind == "parts":
                parts.extend(value)
                clock[0] += 0.5  # A slow consumer, not upstream silence.
        response_diagnostics.finish_response_diagnostics("success", 200)
    finally:
        await asyncio.gather(*tasks)
        error_records.reset_request_context(token)
    row = diagnostics.list_records()["items"][0]
    assert "".join(part.text for part in parts) == "首字后续"
    assert row["max_upstream_gap_ms"] == 125
    assert row["max_ack_wait_ms"] == 531
    assert row["ack_wait_ms"] == 1124  # Per-event integer milliseconds.
    assert acked == [1, 2, 3, 4]
    assert transport.in_flight_count == 0 and not transport._pushed_at


@pytest.mark.parametrize("disconnect", [False, True])
async def test_real_middleware_counts_each_send_once_and_cleans_up(jitter_env, monkeypatch, disconnect):
    from starlette.middleware.base import BaseHTTPMiddleware

    diagnostics = response_diagnostics.ResponseDiagnosticStore()
    monkeypatch.setattr(response_diagnostics, "get_response_diagnostic_store", lambda: diagnostics)
    clock = [1000.0]
    monkeypatch.setattr("aistudio_api.api.error_middleware.current_monotonic", lambda: clock[0])
    closed = asyncio.Event()

    async def upstream(**kwargs):
        try:
            for i in range(100):
                yield "body", f"SYNTHETIC_PRIVATE_BODY_{i}"
            yield "finish_reason", 1
            yield "done", None
        finally:
            closed.set()

    jitter_env.client.stream_generate_content.side_effect = upstream
    req = GeminiGenerateContentRequest.model_validate({"contents": [{"parts": [{"text": "synthetic"}]}]})
    response = service._build_gemini_streaming_response(client=jitter_env.client, req=req, model_path="gemini-test")

    async def endpoint(scope, receive, send):
        await receive()
        response_diagnostics.configure_generation(model="gemini-test", auto_continue=False)
        await response(scope, receive, send)

    async def passthrough(request, call_next):
        return await call_next(request)

    app = ErrorRecordsMiddleware(BaseHTTPMiddleware(endpoint, dispatch=passthrough))
    requested = False

    async def receive():
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": b"{}", "more_body": False}
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    writes = 0

    async def send(message):
        nonlocal writes
        if message["type"] == "http.response.body" and message.get("body"):
            writes += 1
            clock[0] += 0.125
            if disconnect and writes == 2:
                raise OSError("synthetic peer closed")
            await asyncio.sleep(0)

    scope = {"type": "http", "asgi": {"spec_version": "2.4"}, "method": "POST",
             "path": "/v1beta/models/gemini-test:streamGenerateContent", "headers": [], "query_string": b""}
    if disconnect:
        with pytest.raises(OSError):
            await asyncio.wait_for(app(scope, receive, send), 2)
    else:
        await asyncio.wait_for(app(scope, receive, send), 2)
    row = diagnostics.list_records()["items"][0]
    assert row["send_wait_ms"] == writes * 125
    assert row["max_send_wait_ms"] == 125
    assert row["outcome"] == ("cancelled" if disconnect else "success")
    assert "SYNTHETIC_PRIVATE_BODY" not in json.dumps(row)
    assert closed.is_set() and not jitter_env.leased
    jitter_env.client.stream_generate_content.assert_called_once()


async def test_ready_first_and_later_events_do_not_wait_for_a_batch():
    releases = [asyncio.Event() for _ in range(3)]

    async def source(emit):
        for i, release in enumerate(releases):
            await release.wait()
            await emit(f"data: {i}\n\n")

    stream = _sse_with_heartbeats(source, interval=60, max_buffer_events=8, coalesce=True)
    try:
        for i, release in enumerate(releases):
            release.set()
            # The producer cannot prepare the next event until after this
            # assertion; any batch-fill threshold would hang here.
            assert await asyncio.wait_for(anext(stream), 1) == f"data: {i}\n\n"
    finally:
        await stream.aclose()


async def test_first_outer_asgi_write_keeps_single_slot_work_bound():
    from starlette.middleware.base import BaseHTTPMiddleware

    async def measure(events):
        produced = []
        before_first = []

        async def source(emit):
            for i in range(100):
                produced.append(i)
                await emit(f"data: {i}\n\n")

        async def endpoint(scope, receive, send):
            delivery = SSEDelivery()
            response = service._GeminiStreamingResponse(
                _sse_with_heartbeats(source, interval=60, max_buffer_events=events, coalesce=True, delivery=delivery),
                delivery=delivery,
                media_type="text/event-stream",
            )
            await response(scope, receive, send)

        async def passthrough(request, call_next):
            return await call_next(request)

        app = ErrorRecordsMiddleware(BaseHTTPMiddleware(BaseHTTPMiddleware(endpoint, dispatch=passthrough), dispatch=passthrough))

        async def receive():
            await asyncio.Event().wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body") and not before_first:
                before_first.append(len(produced))

        scope = {"type": "http", "asgi": {"spec_version": "2.4"}, "method": "POST",
                 "path": "/v1beta/models/gemini-test:streamGenerateContent", "headers": [], "query_string": b""}
        await asyncio.wait_for(app(scope, receive, send), 2)
        return before_first[0]

    baseline = await measure(1)
    buffered = await measure(8)
    assert buffered <= baseline
