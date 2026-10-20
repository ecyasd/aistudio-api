"""Offline regressions for command deadlines and CDP connection cleanup."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from aistudio_api.infrastructure.browser.cdp_client import CDPConnection


@pytest.mark.parametrize("check_liveness", [False, True])
async def test_command_deadline_includes_blocked_websocket_send(check_liveness):
    connection = CDPConnection("ws://offline.invalid")
    connection.ws = AsyncMock()
    if check_liveness:
        connection.set_liveness_checker(lambda: True)
    entered = asyncio.Event()

    async def blocked_send(_payload):
        entered.set()
        await asyncio.Event().wait()

    connection.ws.send.side_effect = blocked_send
    task = asyncio.create_task(connection.send("Runtime.evaluate", timeout_s=0.02))
    await entered.wait()
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=0.3)
        assert task.done(), "The command's own timeout must finish it, not the test watchdog"
        assert connection._futures == {}
        assert not connection._closed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("check_liveness", [False, True])
async def test_send_and_response_share_one_deadline(check_liveness):
    connection = CDPConnection("ws://offline.invalid")
    connection.ws = AsyncMock()
    if check_liveness:
        connection.set_liveness_checker(lambda: True)
    response_tasks = []

    async def send(payload):
        request_id = json.loads(payload)["id"]
        await asyncio.sleep(0.03)

        async def response():
            await asyncio.sleep(0.03)
            future = connection._futures.get(request_id)
            if future is not None and not future.done():
                future.set_result({"value": "too late"})

        response_tasks.append(asyncio.create_task(response()))

    connection.ws.send.side_effect = send
    try:
        with pytest.raises(TimeoutError):
            await connection.send("Runtime.evaluate", timeout_s=0.05)
        assert connection._futures == {}
    finally:
        for task in response_tasks:
            task.cancel()
        await asyncio.gather(*response_tasks, return_exceptions=True)


async def test_dispatch_failure_immediately_releases_other_pending_commands():
    connection = CDPConnection("ws://offline.invalid")
    connection.ws = AsyncMock()
    first_sent = asyncio.Event()

    async def send(payload):
        if json.loads(payload)["id"] == 1:
            first_sent.set()
            return
        raise OSError("synthetic private browser error")

    connection.ws.send.side_effect = send
    first = asyncio.create_task(connection.send("Runtime.evaluate", timeout_s=30))
    await first_sent.wait()
    try:
        with pytest.raises(RuntimeError) as failure:
            await connection.send("Runtime.evaluate")
        assert "synthetic private browser error" not in str(failure.value)
        with pytest.raises(RuntimeError, match="CDP connection closed"):
            await asyncio.wait_for(first, timeout=0.2)
        assert connection._futures == {}
        assert connection._closed
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)


async def test_close_releases_pending_command_without_waiting_for_receive_loop():
    connection = CDPConnection("ws://offline.invalid")
    connection.ws = AsyncMock()
    sent = asyncio.Event()
    connection.ws.send.side_effect = lambda _payload: sent.set()
    task = asyncio.create_task(connection.send("Runtime.evaluate", timeout_s=30))
    await sent.wait()
    try:
        await connection.close()
        with pytest.raises(RuntimeError, match="CDP connection closed"):
            await asyncio.wait_for(task, timeout=0.2)
        assert connection._futures == {}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_close_cancels_and_joins_async_event_handlers():
    connection = CDPConnection("ws://offline.invalid")
    connection.ws = AsyncMock()
    messages = asyncio.Queue()
    connection.ws.recv.side_effect = messages.get
    entered = asyncio.Event()
    finalized = asyncio.Event()

    async def listener(_params):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalized.set()

    connection.on("Synthetic.event", listener)
    connection._recv_task = asyncio.create_task(connection._recv_loop())
    await messages.put(json.dumps({"method": "Synthetic.event", "params": {}}))
    await entered.wait()
    tasks = list(connection._background_tasks)
    try:
        await connection.close()
        assert finalized.is_set()
        assert all(task.done() for task in tasks)
        assert not connection._background_tasks
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
