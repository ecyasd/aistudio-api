"""Unit tests for XHRStreamTransport and CDP native stream binding."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from aistudio_api.domain.errors import BrowserTransportError
from aistudio_api.infrastructure.browser.cdp_client import CDPConnection, CDPPage
from aistudio_api.infrastructure.browser.scripts import (
    HOOKED_CLEANUP_JS,
    HOOKED_REQUEST_JS,
    STREAM_ACK_JS,
    STREAM_CLEANUP_JS,
    STREAM_RESEND_JS,
    STREAM_STATE_JS,
    STREAMING_INIT_JS,
)
from aistudio_api.infrastructure.gateway.transport import XHRStreamTransport


def make_synthetic_page():
    """No network, cookies, or account state: run transport against a CDP double."""
    page = MagicMock(spec=CDPPage)
    page.has_stream_binding = False
    page.is_closed.return_value = False
    page.get_cookies = AsyncMock(return_value=[])
    page.add_binding = AsyncMock()
    listeners = []
    page.on_binding.side_effect = lambda _name, callback: listeners.append(callback)
    return page, listeners


@pytest.mark.parametrize("stage", ["probe", "binding", "resend"])
@pytest.mark.parametrize("after_body", [False, True])
async def test_native_stream_recovery_failures_terminate_without_replay(
    monkeypatch, stage, after_body
):
    from fastapi import HTTPException

    from aistudio_api.api import gemini
    from aistudio_api.infrastructure.gateway import transport as module

    monkeypatch.setattr(module, "STREAM_PROBE_INTERVAL_S", 0.005)
    page, listeners = make_synthetic_page()
    dispatched = []
    error = RuntimeError("CDP connection closed: synthetic-private-detail")

    async def add_binding(name):
        if name == module.STREAM_RECOVERY_BINDING and stage == "binding":
            raise error

    async def evaluate(script, args=None, **_kwargs):
        if script == STREAMING_INIT_JS:
            dispatched.append(args["rid"])
            if after_body:
                for event in (
                    {"type": "status", "status": 200, "seq": 1},
                    {"type": "chunk", "text": "published", "seq": 2},
                ):
                    listeners[0](json.dumps({"rid": args["rid"], **event}))
            return {"started": True}
        if script == STREAM_STATE_JS:
            if stage == "probe":
                raise error
            return {"phase": "awaiting_ack", "seq": 3 if after_body else 1}
        if script == STREAM_RESEND_JS:
            raise error
        return None

    page.add_binding.side_effect = add_binding
    page.evaluate = AsyncMock(side_effect=evaluate)
    transport = XHRStreamTransport()
    stream = transport.send_streaming_request(
        page, url="https://synthetic.invalid", headers={}, body="[]", timeout_ms=500
    )
    if after_body:
        assert await anext(stream) == ("status", 200)
        assert await anext(stream) == ("chunk", b"published")
    with pytest.raises(BrowserTransportError) as caught:
        await anext(stream)
    client = MagicMock()
    switch = AsyncMock(return_value=True)
    monkeypatch.setattr(gemini, "try_switch_account", switch)
    with pytest.raises(HTTPException) as terminal:
        await gemini._handle_attempt_exception(
            caught.value, attempt=0, model_path="gemini-test", normalized_model="gemini-test",
            client=client, has_yielded_data=after_body,
        )
    assert terminal.value.status_code == 503
    switch.assert_not_awaited()
    assert len(dispatched) == 1
    assert transport.in_flight_count == 0 and transport._buffered_bytes == 0
    assert not transport._cleanup_tasks and not transport._received_seq


@pytest.mark.parametrize("failure_stage", ["status_ack", "done_ack", "page_closed"])
async def test_native_stream_delivery_failure_terminates_without_replay(monkeypatch, failure_stage):
    from fastapi import HTTPException

    from aistudio_api.api import gemini

    page, listeners = make_synthetic_page()
    dispatched = []

    async def evaluate(script, args=None, **_kwargs):
        if script == STREAMING_INIT_JS:
            dispatched.append(args["rid"])
            listeners[0](json.dumps({
                "rid": args["rid"], "type": "status", "status": 200, "seq": 1,
            }))
            return {"started": True}
        if script == STREAM_ACK_JS:
            if failure_stage == "status_ack" or args["seq"] == 2:
                raise RuntimeError("CDP connection closed: synthetic-private-detail")
            if failure_stage == "done_ack":
                listeners[0](json.dumps({
                    "rid": args["rid"], "type": "done", "seq": 2,
                }))
        return None

    page.evaluate = AsyncMock(side_effect=evaluate)
    transport = XHRStreamTransport()
    stream = transport.send_streaming_request(
        page, url="https://synthetic.invalid", headers={}, body="[]", timeout_ms=500,
    )
    assert await anext(stream) == ("status", 200)
    if failure_stage == "page_closed":
        page.is_closed.return_value = True
    with pytest.raises(Exception) as caught:
        await anext(stream)
    assert len(dispatched) == 1
    assert transport.in_flight_count == 0 and transport._buffered_bytes == 0
    assert not transport._cleanup_tasks and not transport._received_seq
    assert any(call.args[0] == STREAM_CLEANUP_JS for call in page.evaluate.await_args_list)

    switch = AsyncMock(return_value=True)
    monkeypatch.setattr(gemini, "try_switch_account", switch)
    # A response can complete without emitting a client-visible Part. Neither
    # its ACK failure nor a closed page permits replaying that generation.
    with pytest.raises(HTTPException) as terminal:
        await gemini._handle_attempt_exception(
            caught.value, attempt=0, model_path="gemini-test", normalized_model="gemini-test",
            client=MagicMock(), has_yielded_data=False,
        )
    assert terminal.value.status_code == 503
    switch.assert_not_awaited()
    assert isinstance(caught.value, BrowserTransportError)


async def test_native_nonstream_browser_timeout_keeps_504_and_never_switches(monkeypatch):
    from fastapi import HTTPException

    from aistudio_api.api import gemini

    page, _ = make_synthetic_page()
    page.evaluate = AsyncMock(return_value={"status": 0, "body": "timeout"})
    transport = XHRStreamTransport()
    with pytest.raises(TimeoutError) as caught:
        await transport.send_hooked_request(
            page, url="https://synthetic.invalid", headers={}, body="[]", timeout_ms=500
        )
    switch = AsyncMock(return_value=True)
    monkeypatch.setattr(gemini, "try_switch_account", switch)
    with pytest.raises(HTTPException) as terminal:
        await gemini._handle_attempt_exception(
            caught.value, attempt=0, model_path="gemini-test", normalized_model="gemini-test",
            client=MagicMock(),
        )
    assert terminal.value.status_code == 504
    switch.assert_not_awaited()
    assert sum(call.args[0] == HOOKED_REQUEST_JS for call in page.evaluate.await_args_list) == 1
    assert any(call.args[0] == HOOKED_CLEANUP_JS for call in page.evaluate.await_args_list)
    assert not transport._cleanup_tasks


async def test_stream_binding_registration_failure_never_dispatches_fetch():
    page, _ = make_synthetic_page()
    page.add_binding.side_effect = RuntimeError("CDP binding registration failed")
    page.evaluate = AsyncMock()
    transport = XHRStreamTransport()
    with pytest.raises(RuntimeError, match="CDP binding registration failed"):
        await anext(transport.send_streaming_request(
            page, url="https://synthetic.invalid", headers={}, body="[]", timeout_ms=50,
        ))
    page.evaluate.assert_not_awaited()
    assert transport.in_flight_count == 0


@pytest.mark.parametrize("repaired", [True, False])
async def test_missing_global_binding_repairs_only_after_explicit_no_dispatch_receipt(repaired):
    page, listeners = make_synthetic_page()
    inits, acks = [], []

    async def evaluate(script, args=None, **_kwargs):
        if script == STREAMING_INIT_JS:
            inits.append(args["rid"])
            if len(inits) == 1 or not repaired:
                return {"started": False, "reason": "binding_unavailable"}
            listeners[0](json.dumps({"rid": args["rid"], "type": "status", "status": 200, "seq": 1}))
            return {"started": True}
        if script == STREAM_ACK_JS:
            acks.append(args["seq"])
            if args["seq"] == 1:
                listeners[0](json.dumps({"rid": args["rid"], "type": "done", "seq": 2}))
        return None

    page.evaluate = AsyncMock(side_effect=evaluate)
    transport = XHRStreamTransport()
    stream = transport.send_streaming_request(page, url="https://synthetic.invalid", headers={}, body="[]", timeout_ms=1000)
    if repaired:
        assert [event async for event in stream] == [("status", 200)]
        assert acks == [1, 2], "completion must be acknowledged before browser cleanup"
    else:
        with pytest.raises(BrowserTransportError):
            await anext(stream)
    assert len(inits) == 2 and inits[0] == inits[1]
    assert transport.in_flight_count == 0 and transport._buffered_bytes == 0
    assert [call.args[0] for call in page.add_binding.await_args_list] == ["__aistudio_stream_push__", "__aistudio_stream_resume__"]


async def test_no_response_headers_timeout_keeps_its_type_and_cleans_browser():
    page, _ = make_synthetic_page()
    page.evaluate = AsyncMock()
    transport = XHRStreamTransport()
    with pytest.raises(TimeoutError, match="no response status"):
        await anext(transport.send_streaming_request(
            page, url="https://synthetic.invalid", headers={}, body="[]", timeout_ms=30,
        ))
    assert page.evaluate.call_args.args[0] == STREAM_CLEANUP_JS
    assert transport.in_flight_count == 0 and transport._buffered_bytes == 0


@pytest.mark.parametrize("after_body", [False, True])
async def test_silent_push_recovery_keeps_one_fetch_and_deduplicates_late_delivery(
    monkeypatch, after_body
):
    from aistudio_api.infrastructure.gateway import transport as module

    monkeypatch.setattr(module, "STREAM_PROBE_INTERVAL_S", 0.015)
    page, listeners = make_synthetic_page()
    captured = {}
    last_seq = 3 if after_body else 1

    def push(event, *, duplicate=False):
        payload = json.dumps({"rid": captured["rid"], **event})
        listeners[1](payload)
        if duplicate:
            listeners[0](payload)

    async def evaluate(script, args=None, **_kwargs):
        if script == STREAMING_INIT_JS:
            captured.update(args)
            if after_body:
                push({"type": "status", "status": 200, "seq": 1})
                push({"type": "chunk", "text": "first", "seq": 2})
        elif script == STREAM_STATE_JS:
            return {"phase": "awaiting_ack", "seq": last_seq, "bindingReady": True}
        elif script == STREAM_RESEND_JS:
            push({"type": "chunk", "text": "second", "seq": 3} if after_body else
                 {"type": "status", "status": 200, "seq": 1}, duplicate=True)
            return True
        elif script == STREAM_ACK_JS:
            if after_body and args["seq"] == 3:
                push({"type": "done"})
            elif not after_body:
                push({"type": "chunk", "text": "second", "seq": 2} if args["seq"] == 1 else
                     {"type": "done"})
        return None

    page.evaluate = AsyncMock(side_effect=evaluate)
    transport = XHRStreamTransport()
    events = [event async for event in transport.send_streaming_request(
        page, url="https://synthetic.invalid", headers={}, body="x" * 300_000, timeout_ms=1500,
    )]
    assert events == ([("status", 200), ("chunk", b"first"), ("chunk", b"second")]
                      if after_body else [("status", 200), ("chunk", b"second")])
    assert sum(call.args[0] == STREAMING_INIT_JS for call in page.evaluate.call_args_list) == 1
    assert [call.args for call in page.add_binding.call_args_list] == [
        ("__aistudio_stream_push__",), (module.STREAM_RECOVERY_BINDING,),
    ]
    assert not transport._received_seq and not transport._cleanup_tasks
    assert transport.in_flight_count == 0 and transport._buffered_bytes == 0


@pytest.mark.parametrize("phase", ["fetch_pending", "read_pending"])
async def test_slow_real_upstream_wait_does_not_restart_or_shorten_request(monkeypatch, phase):
    from aistudio_api.infrastructure.gateway import transport as module

    monkeypatch.setattr(module, "STREAM_PROBE_INTERVAL_S", 0.015)
    page, listeners = make_synthetic_page()
    captured = {}
    probed = asyncio.Event()

    async def evaluate(script, args=None, **_kwargs):
        if script == STREAMING_INIT_JS:
            captured.update(args)
        elif script == STREAM_STATE_JS:
            probed.set()
            return {"phase": phase, "seq": 0, "bindingReady": True}
        return None

    async def upstream():
        await probed.wait()
        await asyncio.sleep(0.04)
        for event in ({"type": "status", "status": 200}, {"type": "chunk", "text": "late"}, {"type": "done"}):
            listeners[0](json.dumps({"rid": captured["rid"], **event}))

    page.evaluate = AsyncMock(side_effect=evaluate)
    task = asyncio.create_task(upstream())
    transport = XHRStreamTransport()
    try:
        assert [event async for event in transport.send_streaming_request(
            page, url="https://synthetic.invalid", headers={}, body="[]", timeout_ms=250,
        )] == [("status", 200), ("chunk", b"late")]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    page.add_binding.assert_awaited_once_with("__aistudio_stream_push__")
    assert not transport._received_seq and transport.in_flight_count == 0


async def test_stuck_stream_probe_can_be_cancelled_without_retaining_request(monkeypatch):
    from aistudio_api.infrastructure.gateway import transport as module

    monkeypatch.setattr(module, "STREAM_PROBE_INTERVAL_S", 0.01)
    page, _ = make_synthetic_page()
    entered = asyncio.Event()

    async def evaluate(script, *_args, **_kwargs):
        if script == STREAM_STATE_JS:
            entered.set()
            await asyncio.Event().wait()

    page.evaluate = AsyncMock(side_effect=evaluate)
    transport = XHRStreamTransport()
    stream = transport.send_streaming_request(
        page, url="https://synthetic.invalid", headers={}, body="[]", timeout_ms=1500,
    )
    task = asyncio.create_task(anext(stream))
    await asyncio.wait_for(entered.wait(), 0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await stream.aclose()
    assert not transport._cleanup_tasks and not transport._received_seq
    assert transport.in_flight_count == 0 and transport._buffered_bytes == 0


async def test_long_prefill_can_later_recover_a_lost_first_status(monkeypatch):
    from aistudio_api.infrastructure.gateway import transport as module

    monkeypatch.setattr(module, "STREAM_PROBE_INTERVAL_S", 0.01)
    page, listeners = make_synthetic_page()
    captured = {}
    probes = 0

    def push(event):
        listeners[1](json.dumps({"rid": captured["rid"], **event}))

    async def evaluate(script, args=None, **_kwargs):
        nonlocal probes
        if script == STREAMING_INIT_JS:
            captured.update(args)
        elif script == STREAM_STATE_JS:
            probes += 1
            return {"phase": "fetch_pending", "seq": 0} if probes == 1 else {
                "phase": "awaiting_ack", "seq": 1, "bindingReady": True,
            }
        elif script == STREAM_RESEND_JS:
            push({"type": "status", "status": 200, "seq": 1})
            return True
        elif script == STREAM_ACK_JS:
            push({"type": "done"})
        return None

    page.evaluate = AsyncMock(side_effect=evaluate)
    stream = XHRStreamTransport().send_streaming_request(
        page, url="https://synthetic.invalid", headers={}, body="x" * 300_000, timeout_ms=1500,
    )
    try:
        assert await asyncio.wait_for(anext(stream), 0.3) == ("status", 200)
        assert [event async for event in stream] == []
    finally:
        await stream.aclose()
    assert probes == 2


async def test_stream_probe_obeys_original_request_deadline_and_preserves_timeout(monkeypatch):
    from aistudio_api.infrastructure.gateway import transport as module

    monkeypatch.setattr(module, "STREAM_PROBE_INTERVAL_S", 0.01)
    page, _ = make_synthetic_page()

    async def evaluate(script, *_args, **_kwargs):
        if script == STREAM_STATE_JS:
            await asyncio.Event().wait()

    page.evaluate = AsyncMock(side_effect=evaluate)
    transport = XHRStreamTransport()
    with pytest.raises(TimeoutError, match="before completion"):
        await asyncio.wait_for(anext(transport.send_streaming_request(
            page, url="https://synthetic.invalid", headers={}, body="[]", timeout_ms=60,
        )), 0.3)
    assert not transport._received_seq and not transport._cleanup_tasks
    assert transport.in_flight_count == 0


@pytest.mark.parametrize("after_body", [False, True])
async def test_lost_browser_push_fails_promptly_instead_of_silence_until_deadline(
    monkeypatch, after_body
):
    from aistudio_api.infrastructure.gateway import transport as module

    monkeypatch.setattr(module, "STREAM_PROBE_INTERVAL_S", 0.02, raising=False)
    page, listeners = make_synthetic_page()
    browser_aborted = asyncio.Event()

    async def evaluate(script, args=None, **_kwargs):
        if script == STREAMING_INIT_JS and after_body:
            for event in ({"type": "status", "status": 200, "seq": 1},
                          {"type": "chunk", "text": "already delivered", "seq": 2}):
                listeners[0](json.dumps({"rid": args["rid"], **event}))
        elif script == STREAM_STATE_JS:
            return {"phase": "awaiting_ack", "seq": 3 if after_body else 1}
        elif script == STREAM_CLEANUP_JS:
            browser_aborted.set()
        return None

    page.evaluate = AsyncMock(side_effect=evaluate)
    transport = XHRStreamTransport()
    stream = transport.send_streaming_request(
        page, url="https://synthetic.invalid", headers={}, body="x" * 300_000, timeout_ms=1500,
    )
    try:
        if after_body:
            assert await anext(stream) == ("status", 200)
            assert await anext(stream) == ("chunk", b"already delivered")
        with pytest.raises(BrowserTransportError, match="CDP stream event delivery stalled"):
            await asyncio.wait_for(anext(stream), 0.3)
    finally:
        await stream.aclose()
    assert browser_aborted.is_set()
    assert transport.in_flight_count == 0 and transport._buffered_bytes == 0


@pytest.mark.asyncio
async def test_nonstream_cancellation_explicitly_aborts_browser_fetch():
    page, _ = make_synthetic_page()
    started = asyncio.Event()
    captured = {}

    async def evaluate(script, args=None, **_kwargs):
        if script == HOOKED_REQUEST_JS:
            captured.update(args)
            started.set()
            await asyncio.Event().wait()

    page.evaluate = AsyncMock(side_effect=evaluate)
    task = asyncio.create_task(
        XHRStreamTransport().send_hooked_request(
            page,
            url="https://synthetic.invalid",
            headers={},
            body="[]",
            timeout_ms=1000,
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert any(
        call.args == (HOOKED_CLEANUP_JS, captured["rid"])
        for call in page.evaluate.call_args_list
    )


@pytest.mark.asyncio
async def test_nonstream_read_failure_is_not_a_successful_empty_body():
    page, _ = make_synthetic_page()
    page.evaluate = AsyncMock(
        return_value={"status": 0, "body": "response read failed"}
    )
    with pytest.raises(BrowserTransportError, match="Browser response delivery failed"):
        await XHRStreamTransport().send_hooked_request(
            page,
            url="https://synthetic.invalid",
            headers={},
            body="[]",
            timeout_ms=1000,
        )
    assert page.evaluate.call_args.args[0] == HOOKED_CLEANUP_JS


@pytest.mark.asyncio
async def test_stream_overflow_aborts_while_consumer_paused_and_cannot_turn_into_done(
    monkeypatch,
):
    from aistudio_api.infrastructure.gateway import transport as module

    monkeypatch.setattr(module, "MAX_STREAM_BUFFER_BYTES", 8)
    page, listeners = make_synthetic_page()
    captured = {}

    async def evaluate(script, args=None, **_kwargs):
        if script == STREAMING_INIT_JS:
            captured.update(args)
            listeners[0](
                json.dumps({"rid": args["rid"], "type": "status", "status": 200})
            )

    page.evaluate = AsyncMock(side_effect=evaluate)
    transport = XHRStreamTransport()
    stream = transport.send_streaming_request(
        page, url="https://synthetic.invalid", headers={}, body="[]", timeout_ms=1000
    )
    assert await anext(stream) == ("status", 200)
    rid = captured["rid"]
    for _ in range(2048):
        listeners[0](json.dumps({"rid": rid, "type": "chunk", "text": "界界"}))
    listeners[0](json.dumps({"rid": rid, "type": "done"}))
    assert transport._queues[rid].qsize() == 1
    assert transport._buffered_bytes == 0
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert any(
        call.args == (STREAM_CLEANUP_JS, rid) for call in page.evaluate.call_args_list
    )
    with pytest.raises(BrowserTransportError, match="Browser stream delivery failed"):
        await anext(stream)
    assert transport.in_flight_count == 0
    assert transport._buffered_bytes == 0


@pytest.mark.asyncio
async def test_stream_ack_waits_for_actual_consumer_and_keeps_order():
    page, listeners = make_synthetic_page()
    captured = {}
    acks = []

    async def evaluate(script, args=None, **_kwargs):
        if script == STREAMING_INIT_JS:
            captured.update(args)
            listeners[0](
                json.dumps(
                    {"rid": args["rid"], "type": "status", "status": 200, "seq": 1}
                )
            )
        elif script == STREAM_ACK_JS:
            acks.append(args["seq"])
            event = (
                {"rid": captured["rid"], "type": "chunk", "text": "one", "seq": 2}
                if args["seq"] == 1
                else {"rid": captured["rid"], "type": "done"}
            )
            listeners[0](json.dumps(event))

    page.evaluate = AsyncMock(side_effect=evaluate)
    transport = XHRStreamTransport()
    stream = transport.send_streaming_request(
        page, url="https://synthetic.invalid", headers={}, body="[]", timeout_ms=1000
    )
    assert await anext(stream) == ("status", 200)
    assert acks == []
    assert await anext(stream) == ("chunk", b"one")
    assert acks == [1]
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert acks == [1, 2]
    assert transport._buffered_bytes == 0


@pytest.mark.asyncio
async def test_ack_timeout_covers_stalled_cdp_socket_write_and_cleans_up(monkeypatch):
    from aistudio_api.infrastructure.gateway import transport as module

    monkeypatch.setattr(module, "CLEANUP_TIMEOUT_S", 0.02)
    page, listeners = make_synthetic_page()
    connection = CDPConnection("ws://synthetic.invalid")
    connection.ws = AsyncMock()
    writing = asyncio.Event()

    async def stalled_write(_message):
        writing.set()
        await asyncio.Event().wait()

    connection.ws.send.side_effect = stalled_write

    async def evaluate(script, args=None, **kwargs):
        if script == STREAMING_INIT_JS:
            listeners[0](
                json.dumps(
                    {
                        "rid": args["rid"],
                        "type": "status",
                        "status": 200,
                        "seq": 1,
                    }
                )
            )
        elif script == STREAM_ACK_JS:
            # Exercise CDPConnection.send before its internal response timer:
            # a socket write under backpressure can wait indefinitely here.
            return await connection.send(
                "Runtime.evaluate",
                {"expression": "synthetic ack"},
                timeout_s=kwargs["timeout_s"],
            )
        return None

    page.evaluate = AsyncMock(side_effect=evaluate)
    transport = XHRStreamTransport()
    stream = transport.send_streaming_request(
        page, url="https://synthetic.invalid", headers={}, body="[]", timeout_ms=1000
    )
    assert await anext(stream) == ("status", 200)
    advance = asyncio.create_task(anext(stream))
    try:
        await writing.wait()
        done, _ = await asyncio.wait({advance}, timeout=0.5)
        assert advance in done, (
            "ACK must time out while the socket write is still stalled"
        )
        with pytest.raises(TimeoutError):
            await advance
        assert connection._futures == {}
        assert transport.in_flight_count == 0
        assert any(
            call.args[0] == STREAM_CLEANUP_JS for call in page.evaluate.call_args_list
        )
    finally:
        if not advance.done():
            advance.cancel()
            with pytest.raises(asyncio.CancelledError):
                await advance
        await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["event_count", "single_chunk", "total_bytes"])
async def test_stream_bounds_fail_explicitly_and_preserve_other_streams(
    monkeypatch, limit
):
    from aistudio_api.infrastructure.gateway import transport as module

    monkeypatch.setattr(module, "MAX_STREAM_EVENTS", 3)
    monkeypatch.setattr(module, "MAX_CHUNK_BYTES", 8)
    monkeypatch.setattr(module, "MAX_TOTAL_BUFFER_BYTES", 12)
    page, listeners = make_synthetic_page()
    initialized = []

    async def evaluate(script, args=None, **_kwargs):
        if script == STREAMING_INIT_JS:
            initialized.append(args["rid"])
            listeners[0](
                json.dumps({"rid": args["rid"], "type": "status", "status": 200})
            )

    page.evaluate = AsyncMock(side_effect=evaluate)
    transport = XHRStreamTransport()
    streams = [
        transport.send_streaming_request(
            page,
            url="https://synthetic.invalid",
            headers={},
            body="[]",
            timeout_ms=1000,
        )
        for _ in range(2)
    ]
    for stream in streams:
        assert await anext(stream) == ("status", 200)
    first, second = initialized
    listeners[0](json.dumps({"rid": first, "type": "chunk", "text": "okay"}))
    if limit == "event_count":
        for _ in range(4):
            listeners[0](json.dumps({"rid": second, "type": "status", "status": 200}))
    else:
        text = "x" * (13 if limit == "single_chunk" else 9)
        listeners[0](json.dumps({"rid": second, "type": "chunk", "text": text}))
    with pytest.raises(BrowserTransportError, match="Browser stream delivery failed"):
        await anext(streams[1])
    assert transport.in_flight_count == 1
    assert transport._buffered_bytes == 4
    assert await anext(streams[0]) == ("chunk", b"okay")
    await streams[0].aclose()
    assert transport.in_flight_count == 0
    assert transport._buffered_bytes == 0


@pytest.mark.asyncio
async def test_browser_abort_is_failure_even_after_http_200():
    page, listeners = make_synthetic_page()

    async def evaluate(script, args=None, **_kwargs):
        if script == STREAMING_INIT_JS:
            for event in ({"type": "status", "status": 200}, {"type": "aborted"}):
                listeners[0](json.dumps({"rid": args["rid"], **event}))

    page.evaluate = AsyncMock(side_effect=evaluate)
    with pytest.raises(BrowserTransportError, match="Browser stream delivery failed"):
        async for _ in XHRStreamTransport().send_streaming_request(
            page,
            url="https://synthetic.invalid",
            headers={},
            body="[]",
            timeout_ms=1000,
        ):
            pass


def test_streaming_scripts_defined_and_non_empty():
    """Verify streaming initialization and cleanup scripts are properly loaded."""
    assert len(STREAMING_INIT_JS.strip()) > 0
    assert len(STREAM_CLEANUP_JS.strip()) > 0
    assert "__AISTUDIO__" in STREAMING_INIT_JS
    assert "__AISTUDIO__" in STREAM_CLEANUP_JS


@pytest.mark.asyncio
async def test_transport_native_cdp_binding_stream_push():
    """Verify realtime stream event dispatch via CDP Runtime.bindingCalled."""
    transport = XHRStreamTransport()
    page = MagicMock(spec=CDPPage)
    binding_listener = None

    def mock_on_binding(name: str, callback):
        nonlocal binding_listener
        if name == "__aistudio_stream_push__":
            binding_listener = callback

    page.on_binding = MagicMock(side_effect=mock_on_binding)
    page.add_binding = AsyncMock()

    captured_rid = None

    async def fake_evaluate(expr, args=None, *a, **kw):
        nonlocal captured_rid
        if isinstance(args, dict) and "rid" in args:
            captured_rid = args["rid"]
        return

    page.evaluate = AsyncMock(side_effect=fake_evaluate)

    async def push_events():
        while captured_rid is None or binding_listener is None:
            await asyncio.sleep(0.01)

        binding_listener(
            json.dumps({"rid": captured_rid, "type": "status", "status": 200})
        )
        binding_listener(
            json.dumps({"rid": captured_rid, "type": "chunk", "text": "Hello "})
        )
        binding_listener(
            json.dumps({"rid": captured_rid, "type": "chunk", "text": "CDP Binding!"})
        )
        binding_listener(json.dumps({"rid": captured_rid, "type": "done"}))

    push_task = asyncio.create_task(push_events())

    collected = []
    async for tag, payload in transport.send_streaming_request(
        page,
        url="http://test.com",
        headers={},
        body="{}",
        timeout_ms=3000,
    ):
        collected.append((tag, payload))

    await push_task

    assert ("status", 200) in collected
    assert ("chunk", b"Hello ") in collected
    assert ("chunk", b"CDP Binding!") in collected


@pytest.mark.asyncio
async def test_transport_streaming_timeout_raises():
    """Verify stream timeout before completion explicitly raises TimeoutError."""
    transport = XHRStreamTransport()
    page = MagicMock(spec=CDPPage)
    binding_listener = None

    def mock_on_binding(name: str, callback):
        nonlocal binding_listener
        if name == "__aistudio_stream_push__":
            binding_listener = callback

    page.on_binding = MagicMock(side_effect=mock_on_binding)
    page.add_binding = AsyncMock()

    captured_rid = None

    async def fake_evaluate(expr, args=None, *a, **kw):
        nonlocal captured_rid
        if isinstance(args, dict) and "rid" in args:
            captured_rid = args["rid"]
        return

    page.evaluate = AsyncMock(side_effect=fake_evaluate)

    async def push_status_only():
        while captured_rid is None or binding_listener is None:
            await asyncio.sleep(0.01)
        binding_listener(
            json.dumps({"rid": captured_rid, "type": "status", "status": 200})
        )

    push_task = asyncio.create_task(push_status_only())
    with pytest.raises(
        TimeoutError, match="streaming response timed out before completion"
    ):
        async for _ in transport.send_streaming_request(
            page,
            url="http://test.com",
            headers={},
            body="{}",
            timeout_ms=100,
        ):
            pass
    await push_task


@pytest.mark.asyncio
async def test_transport_abort_stream_by_rid():
    """Verify abort_stream targets specific active stream by rid without disturbing other streams."""
    transport = XHRStreamTransport()
    page = MagicMock(spec=CDPPage)
    page.on_binding = MagicMock()
    page.add_binding = AsyncMock()

    captured_rids: list[str] = []

    async def fake_evaluate(expr, args=None, *a, **kw):
        if isinstance(args, dict) and "rid" in args:
            captured_rids.append(args["rid"])
        return

    page.evaluate = AsyncMock(side_effect=fake_evaluate)

    assert transport.in_flight_count == 0

    async def run_stream():
        async for _ in transport.send_streaming_request(
            page,
            url="http://test.com",
            headers={},
            body="{}",
            timeout_ms=5000,
        ):
            pass

    task1 = asyncio.create_task(run_stream())
    task2 = asyncio.create_task(run_stream())
    await asyncio.sleep(0.05)
    assert transport.in_flight_count == 2
    assert len(captured_rids) == 2

    # Abort unknown rid returns False
    assert transport.abort_stream("non_existent_rid") is False
    assert transport.in_flight_count == 2

    # Abort stream 1 specifically
    rid1 = captured_rids[0]
    assert transport.abort_stream(rid1, "client_disconnected") is True

    with pytest.raises(
        BrowserTransportError, match="Browser stream delivery failed"
    ):
        await task1

    assert transport.in_flight_count == 1

    # Close transport interrupts remaining stream 2
    transport.close("transport_closed")
    with pytest.raises(
        BrowserTransportError, match="Browser stream delivery failed"
    ):
        await task2

    assert transport.in_flight_count == 0
