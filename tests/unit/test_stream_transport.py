"""Unit tests for XHRStreamTransport and CDP native stream binding."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from aistudio_api.infrastructure.browser.cdp_client import CDPConnection, CDPPage
from aistudio_api.infrastructure.browser.scripts import (
    HOOKED_CLEANUP_JS,
    HOOKED_REQUEST_JS,
    STREAM_ACK_JS,
    STREAM_CLEANUP_JS,
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
    with pytest.raises(RuntimeError, match="response read failed"):
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
    with pytest.raises(RuntimeError, match="stream buffer limit exceeded"):
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
    with pytest.raises(RuntimeError, match="stream buffer limit exceeded"):
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
    with pytest.raises(RuntimeError, match="cancelled"):
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
        RuntimeError, match="streaming request failed: client_disconnected"
    ):
        await task1

    assert transport.in_flight_count == 1

    # Close transport interrupts remaining stream 2
    transport.close("transport_closed")
    with pytest.raises(
        RuntimeError, match="streaming request failed: transport_closed"
    ):
        await task2

    assert transport.in_flight_count == 0
