"""An abandoned candidate must not survive a restored warm browser session."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest

from aistudio_api.infrastructure.browser import cdp_client as module


@pytest.mark.parametrize("phase", ["connect", "init_domains", "spare_cleanup"])
async def test_level_cancellation_disposes_only_unpublished_candidate(monkeypatch, phase):
    client = module.CDPClient(port=1)
    client.wait_until_ready = AsyncMock()
    entered = asyncio.Event()
    closed, disposed = [], []
    old = SimpleNamespace(browser_context_id="old-context")
    client._owned_pages["old-target"] = old
    client._browser_contexts.add("old-context")

    async def browser_send(method, params=None):
        if method == "Target.createBrowserContext":
            return {"browserContextId": "candidate-context"}
        if method == "Target.createTarget":
            return {"targetId": "candidate-target"}
        assert method == "Target.disposeBrowserContext"
        await anyio.sleep(0)
        disposed.append(params["browserContextId"])
        return {}

    client._browser_cdp = SimpleNamespace(send=browser_send, _closed=False)
    conn = MagicMock(spec=module.CDPConnection)

    async def wait_at(name):
        if phase == name:
            entered.set()
            await asyncio.Event().wait()

    async def connect():
        await wait_at("connect")

    conn.connect = AsyncMock(side_effect=connect)

    async def close():
        await anyio.sleep(0)
        closed.append(True)

    conn.close = AsyncMock(side_effect=close)
    monkeypatch.setattr(module, "CDPConnection", lambda _url: conn)

    async def initialize(_self, **_kwargs):
        await wait_at("init_domains")

    monkeypatch.setattr(module.CDPPage, "init_domains", initialize)
    client.get_targets = AsyncMock(return_value=[
        {"id": "candidate-target", "type": "page", "webSocketDebuggerUrl": "ws://synthetic.invalid/candidate"},
        {"id": "old-target", "type": "page"},
        {"id": "spare-target", "type": "page"},
    ])

    async def close_spare(target):
        assert target == "spare-target"
        await wait_at("spare_cleanup")

    client.close_target = AsyncMock(side_effect=close_spare)
    async with anyio.create_task_group() as group:
        group.start_soon(client.create_new_page)
        await asyncio.wait_for(entered.wait(), 1)
        group.cancel_scope.cancel()
    assert closed == [True]
    assert disposed == ["candidate-context"]
    assert client._browser_contexts == {"old-context"}
    assert client._owned_pages == {"old-target": old}


@pytest.mark.parametrize("cancellation", ["task", "level"])
async def test_cancel_after_remote_allocation_waits_for_its_id_and_disposes_it(cancellation):
    client = module.CDPClient(port=1)
    client.wait_until_ready = AsyncMock()
    client._browser_contexts.add("old-context")
    created, reply = asyncio.Event(), asyncio.Event()
    remote = {"old-context"}
    commands = []

    async def send(method, params=None):
        commands.append(method)
        if method == "Target.createBrowserContext":
            remote.add("candidate-context")
            created.set()
            await reply.wait()
            return {"browserContextId": "candidate-context"}
        assert method == "Target.disposeBrowserContext"
        await anyio.sleep(0)
        remote.remove(params["browserContextId"])
        return {}

    client._browser_cdp = SimpleNamespace(send=send, _closed=False)
    if cancellation == "task":
        operation = asyncio.create_task(client.create_new_page())
        try:
            await asyncio.wait_for(created.wait(), 1)
            operation.cancel()
            await asyncio.sleep(0)
            operation.cancel()
            reply.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(operation, 1)
        finally:
            reply.set()
            await asyncio.gather(operation, return_exceptions=True)
    else:
        async with anyio.create_task_group() as group:
            group.start_soon(client.create_new_page)
            await asyncio.wait_for(created.wait(), 1)
            group.cancel_scope.cancel()
            reply.set()
    assert remote == client._browser_contexts == {"old-context"}
    assert commands == ["Target.createBrowserContext", "Target.disposeBrowserContext"]
    assert not client._browser_cdp._closed
    assert not any(task.get_name() == "cdp-context-create" and not task.done() for task in asyncio.all_tasks())
