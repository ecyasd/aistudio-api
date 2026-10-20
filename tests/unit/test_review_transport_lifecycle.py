"""Offline regressions for disposing failed or interrupted CDP page owners."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from aistudio_api.infrastructure.browser.cdp_client import (
    CDPClient,
    CDPConnection,
    CDPPage,
)


def owned_page():
    client = CDPClient(port=1)
    connection = CDPConnection("ws://synthetic.invalid/page")
    connection.ws = AsyncMock()
    page = CDPPage(connection, "synthetic-target", "synthetic-context")
    client._owned_pages[page.target_id] = page
    client._browser_contexts.add("synthetic-context")
    client._browser_cdp = MagicMock(spec=CDPConnection)
    return client, page, connection


async def test_failed_owned_page_releases_socket_and_event_handlers():
    client, page, connection = owned_page()
    websocket = connection.ws
    started, finalized = asyncio.Event(), asyncio.Event()
    unsubscribed = MagicMock()
    page._track_listener(unsubscribed)

    async def handler():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalized.set()

    task = asyncio.create_task(handler())
    connection._background_tasks.add(task)
    await asyncio.wait_for(started.wait(), timeout=1)
    connection.fail_pending_futures(RuntimeError("synthetic target crash"))
    try:
        await asyncio.wait_for(client.close_target(page.target_id), timeout=1)
        assert connection.ws is None, "Failed targets still own a socket until closed"
        websocket.close.assert_awaited_once()
        assert finalized.is_set()
        assert task.done()
        assert not connection._background_tasks
        unsubscribed.assert_called_once()
        assert not client._owned_pages
        assert not client._browser_contexts
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await connection.close()


async def test_cancelled_page_close_keeps_owner_for_context_disposal_retry():
    client, page, connection = owned_page()
    sent = asyncio.Event()
    connection.ws.send.side_effect = lambda _payload: sent.set()
    closing = asyncio.create_task(client.close_target(page.target_id))
    await asyncio.wait_for(sent.wait(), timeout=1)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing

    try:
        assert page.target_id in client._owned_pages
        assert "synthetic-context" in client._browser_contexts
        assert connection.ws is None, "Cancelling Page.close must release its CDP socket"
        assert not connection._futures
        await client.close_target(page.target_id)
        assert not client._owned_pages
        assert not client._browser_contexts
        client._browser_cdp.send.assert_awaited_once_with(
            "Target.disposeBrowserContext", {"browserContextId": "synthetic-context"}
        )
    finally:
        await connection.close()


async def test_failed_context_disposal_can_retry_the_owned_target():
    client, page, connection = owned_page()
    connection.fail_pending_futures(RuntimeError("synthetic target crash"))
    client._browser_cdp.send.side_effect = [RuntimeError("synthetic dispose failure"), {}]
    try:
        with pytest.raises(RuntimeError, match="synthetic dispose failure"):
            await client.close_target(page.target_id)
        assert page.target_id in client._owned_pages
        assert "synthetic-context" in client._browser_contexts
        await client.close_target(page.target_id)
        assert not client._owned_pages
        assert not client._browser_contexts
        assert connection.ws is None
    finally:
        await connection.close()


async def test_concurrent_target_close_cannot_drop_the_socket_cleanup_owner():
    client, page, connection = owned_page()
    websocket = connection.ws
    connection.fail_pending_futures(RuntimeError("synthetic target crash"))
    entered = asyncio.Event()
    attempts = 0

    async def close_websocket():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            entered.set()
            await asyncio.Event().wait()

    websocket.close.side_effect = close_websocket
    first = asyncio.create_task(client.close_target(page.target_id))
    await asyncio.wait_for(entered.wait(), timeout=1)
    try:
        # A concurrent/reentrant call may return while the first closer owns the
        # join. It must not claim that the still-open socket was disposed.
        await asyncio.wait_for(client.close_target(page.target_id), timeout=1)
        assert client._owned_pages.get(page.target_id) is page
        assert "synthetic-context" in client._browser_contexts
        client._browser_cdp.send.assert_not_awaited()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert connection.ws is websocket
        assert client._owned_pages.get(page.target_id) is page

        await client.close_target(page.target_id)
        assert connection.ws is None
        assert not client._owned_pages
        assert not client._browser_contexts
        assert attempts == 2
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await connection.close()


async def test_socket_cleanup_failure_retains_owner_and_allows_retry():
    client, page, connection = owned_page()
    websocket = connection.ws
    connection.fail_pending_futures(RuntimeError("synthetic target crash"))
    websocket.close.side_effect = [RuntimeError("synthetic socket close failure"), None]
    try:
        with pytest.raises(RuntimeError, match="synthetic socket close failure"):
            await client.close_target(page.target_id)
        assert connection.ws is websocket
        assert client._owned_pages.get(page.target_id) is page
        assert "synthetic-context" in client._browser_contexts
        client._browser_cdp.send.assert_not_awaited()

        await client.close_target(page.target_id)
        assert connection.ws is None
        assert not client._owned_pages
        assert not client._browser_contexts
        assert websocket.close.await_count == 2
    finally:
        await connection.close()


@pytest.mark.parametrize("close_from_listener", ["target", "connection"])
async def test_cancelled_listener_can_reenter_close_without_disposing_early(
    close_from_listener,
):
    client, page, connection = owned_page()
    connection.fail_pending_futures(RuntimeError("synthetic target crash"))
    entered, finalized = asyncio.Event(), asyncio.Event()
    disposed_with_live_socket = []

    async def dispose_context(_method, _params):
        disposed_with_live_socket.append(connection.ws is not None)
        return {}

    client._browser_cdp.send.side_effect = dispose_context

    async def handler():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            if close_from_listener == "target":
                await client.close_target(page.target_id)
            else:
                await connection.close()
            finalized.set()

    task = asyncio.create_task(handler())
    connection._background_tasks.add(task)
    await asyncio.wait_for(entered.wait(), timeout=1)
    try:
        await asyncio.wait_for(client.close_target(page.target_id), timeout=1)
        assert finalized.is_set()
        assert task.done()
        assert task.cancelled()
        assert disposed_with_live_socket == [False]
        assert connection.ws is None
        assert not client._owned_pages
        assert not client._browser_contexts
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await connection.close()
