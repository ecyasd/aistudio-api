"""Independent deadline isolation and event-listener shutdown edge cases."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import textwrap
from unittest.mock import AsyncMock, MagicMock

import pytest

from aistudio_api.infrastructure.browser import cdp_client as module
from aistudio_api.infrastructure.browser.cdp_client import CDPConnection


@pytest.mark.parametrize("check_liveness", [False, True])
async def test_one_dispatch_timeout_preserves_another_pending_command(
    monkeypatch, check_liveness
):
    connection = CDPConnection("ws://synthetic.invalid")
    connection.ws = AsyncMock()
    if check_liveness:
        connection.set_liveness_checker(lambda: True)
    sent, blocked = asyncio.Event(), asyncio.Event()
    contexts = []

    def timeout_factory(_deadline):
        context = asyncio.Timeout(None)
        contexts.append(context)
        return context

    async def send(payload):
        if json.loads(payload)["id"] == 1:
            sent.set()
            return
        blocked.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(module.asyncio, "timeout_at", timeout_factory)
    connection.ws.send.side_effect = send
    first = asyncio.create_task(connection.send("Runtime.evaluate"))
    await sent.wait()
    second = asyncio.create_task(connection.send("Runtime.evaluate"))
    await blocked.wait()
    try:
        contexts[-1].reschedule(asyncio.get_running_loop().time() - 1)
        with pytest.raises(TimeoutError):
            await second
        assert not connection._closed
        assert set(connection._futures) == {1}
        connection._futures[1].set_result({"synthetic": "surviving reply"})
        assert await first == {"synthetic": "surviving reply"}
        assert not connection._futures
    finally:
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)


async def test_async_listener_can_close_its_own_connection_without_self_join():
    connection = CDPConnection("ws://synthetic.invalid")
    connection.ws = AsyncMock()
    messages = asyncio.Queue()
    connection.ws.recv.side_effect = messages.get
    finished = asyncio.Event()

    async def listener(_params):
        await connection.close()
        finished.set()

    connection.on("Synthetic.close", listener)
    connection._recv_task = asyncio.create_task(connection._recv_loop())
    await messages.put(json.dumps({"method": "Synthetic.close", "params": {}}))
    await asyncio.wait_for(finished.wait(), timeout=10)
    await asyncio.sleep(0)
    assert connection.ws is None
    assert connection._recv_task.done()
    assert not connection._background_tasks


async def test_async_listener_failure_is_retrieved_without_logging_arguments(
    monkeypatch,
):
    connection = CDPConnection("ws://synthetic.invalid")
    connection.ws = AsyncMock()
    messages = asyncio.Queue()
    connection.ws.recv.side_effect = messages.get
    entered = asyncio.Event()
    logged = MagicMock()
    monkeypatch.setattr(module.log, "debug", logged)

    async def listener(_params):
        entered.set()
        raise RuntimeError("synthetic-private-event-arguments")

    connection.on("Synthetic.error", listener)
    connection._recv_task = asyncio.create_task(connection._recv_loop())
    try:
        await messages.put(json.dumps({"method": "Synthetic.error", "params": {}}))
        await entered.wait()
        while connection._background_tasks:
            await asyncio.sleep(0)
        logged.assert_called_with("CDP asynchronous event handler failed")
        assert "synthetic-private-event-arguments" not in str(logged.call_args_list)
    finally:
        await connection.close()


async def test_cancelled_close_can_be_retried_to_finish_websocket_cleanup():
    connection = CDPConnection("ws://synthetic.invalid")
    websocket = AsyncMock()
    connection.ws = websocket
    entered = asyncio.Event()
    calls = 0

    async def close_websocket():
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await asyncio.Event().wait()

    websocket.close.side_effect = close_websocket
    first = asyncio.create_task(connection.close())
    await entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert connection.ws is websocket
    await connection.close()
    assert connection.ws is None
    assert websocket.close.await_count == 2


def test_close_is_reentrant_from_two_cancelled_listener_cleanup_blocks():
    # A cancellation cycle can recursively cancel Tasks and poison pytest's own
    # event loop. Run this regression in an isolated, offline child process.
    script = textwrap.dedent("""
        import asyncio
        import os
        from unittest.mock import AsyncMock
        from aistudio_api.infrastructure.browser.cdp_client import CDPConnection

        async def main():
            connection = CDPConnection("ws://synthetic.invalid")
            connection.ws = AsyncMock()
            entered = [asyncio.Event(), asyncio.Event()]

            async def listener(index):
                entered[index].set()
                try:
                    await asyncio.Event().wait()
                finally:
                    await connection.close()

            tasks = [asyncio.create_task(listener(i)) for i in range(2)]
            connection._background_tasks.update(tasks)
            await asyncio.gather(*(event.wait() for event in entered))
            await connection.close()
            assert all(task.done() for task in tasks)
            assert not connection._background_tasks
            assert connection.ws is None

        try:
            asyncio.run(main())
        except BaseException as error:
            print(type(error).__name__, flush=True)
            os._exit(2)
        os._exit(0)
    """)
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("CDP close deadlocked when cancelled listeners also closed it")
    assert completed.returncode == 0, completed.stdout + completed.stderr[-2000:]
