"""Partial CDP page creation keeps its ownership until local cleanup finishes."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest

from aistudio_api.infrastructure.browser import cdp_client as cdp_module


@pytest.fixture
def creation_env(monkeypatch):
    env = SimpleNamespace(
        stage="domains", failure=None, started=asyncio.Event(), closed=[], disposed=[],
        connections=[], cleanup_owners=[], operation_owner=None, close_error=None,
    )

    async def fail_at(stage):
        if stage != env.stage:
            return
        env.started.set()
        if env.failure is not None:
            raise env.failure
        await anyio.sleep_forever()

    async def browser_send(method, params=None, **_kwargs):
        if method == "Target.createBrowserContext":
            return {"browserContextId": "synthetic-new-context"}
        if method == "Target.createTarget":
            return {"targetId": "synthetic-new-page"}
        if method == "Target.disposeBrowserContext":
            env.cleanup_owners.append(asyncio.current_task())
            await anyio.sleep(0)
            env.disposed.append(params["browserContextId"])
            return {}
        if method == "Page.close":
            return {}
        raise AssertionError("unexpected synthetic CDP method")

    class Connection:
        def __init__(self, url):
            self.url = url
            self._closed = False
            self._close_in_progress = False
            env.connections.append(self)

        async def connect(self):
            await fail_at("browser_connect" if self.url.endswith("browser") else "page_connect")

        async def send(self, method, params=None, **kwargs):
            return await browser_send(method, params, **kwargs)

        async def close(self):
            env.cleanup_owners.append(asyncio.current_task())
            await anyio.sleep(0)
            self._closed = True
            env.closed.append(self.url)
            if env.close_error is not None:
                raise env.close_error

    async def init_domains(_page, **_kwargs):
        await fail_at("domains")

    monkeypatch.setattr(cdp_module, "CDPConnection", Connection)
    monkeypatch.setattr(cdp_module.CDPPage, "init_domains", init_domains)
    client = cdp_module.CDPClient(port=1)
    client.wait_until_ready = AsyncMock()
    client.get_version = AsyncMock(return_value={"webSocketDebuggerUrl": "ws://synthetic.invalid/browser"})
    client.get_targets = AsyncMock(return_value=[
        {"id": "synthetic-new-page", "type": "page", "webSocketDebuggerUrl": "ws://synthetic.invalid/page"},
        {"id": "synthetic-old-page", "type": "page"},
        {"id": "synthetic-spare", "type": "page"},
    ])
    env.client = client
    env.browser = SimpleNamespace(_closed=False, send=browser_send)
    old = MagicMock(spec=cdp_module.CDPPage)
    old.target_id = "synthetic-old-page"
    old.is_closed.return_value = False
    env.old = old
    original_close_target = client.close_target

    async def close_target(target):
        if target == "synthetic-spare":
            await fail_at("spare")
        else:
            await original_close_target(target)

    monkeypatch.setattr(client, "close_target", close_target)

    def configure(stage, old):
        env.stage = stage
        if stage != "browser_connect" and old:
            client._browser_cdp = env.browser
        if old:
            client._owned_pages[env.old.target_id] = env.old
            client._browser_contexts.add("synthetic-old-context")
        else:
            # In a cold client, the spare old target is not a real workload.
            client.get_targets.return_value = [
                item for item in client.get_targets.return_value if item["id"] != "synthetic-old-page"
            ]

    env.configure = configure
    return env


SCENARIOS = [
    ("browser_connect", False),
    ("page_connect", True),
    ("domains", True),
    ("domains", False),
    ("spare", True),
]


def assert_cleaned(env, *, stage, old):
    expected_socket = "browser" if stage == "browser_connect" else "page"
    assert any(url.endswith(expected_socket) for url in env.closed)
    expected_contexts = {"synthetic-old-context"} if old else set()
    assert env.client._browser_contexts == expected_contexts
    assert set(env.client._owned_pages) == ({env.old.target_id} if old else set())
    if stage != "browser_connect":
        assert env.disposed == ["synthetic-new-context"]
    assert env.cleanup_owners and all(owner is env.operation_owner for owner in env.cleanup_owners)
    env.old.close.assert_not_called()


@pytest.mark.parametrize(("stage", "old"), SCENARIOS)
async def test_level_cancelled_page_creation_finishes_owned_cleanup(creation_env, stage, old):
    env = creation_env
    env.configure(stage, old)
    cancelled = []

    async def create():
        env.operation_owner = asyncio.current_task()
        try:
            await env.client.create_new_page()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        pytest.fail("page creation cancellation must propagate")

    async with anyio.create_task_group() as group:
        group.start_soon(create)
        await env.started.wait()
        group.cancel_scope.cancel()

    assert cancelled == [True]
    assert_cleaned(env, stage=stage, old=old)


@pytest.mark.parametrize(("stage", "old"), SCENARIOS)
async def test_page_creation_error_preserves_failure_and_old_page(creation_env, stage, old):
    env = creation_env
    env.configure(stage, old)
    original = RuntimeError("synthetic page creation failure")
    env.failure = original
    env.operation_owner = asyncio.current_task()

    with pytest.raises(RuntimeError) as caught:
        await env.client.create_new_page()

    assert caught.value is original
    assert_cleaned(env, stage=stage, old=old)


async def test_browser_connect_cleanup_error_does_not_replace_original_failure(creation_env):
    env = creation_env
    env.configure("browser_connect", False)
    original = RuntimeError("synthetic connect failure")
    env.failure = original
    env.close_error = ValueError("synthetic close failure")
    env.operation_owner = asyncio.current_task()

    with pytest.raises(RuntimeError) as caught:
        await env.client.create_new_page()

    assert caught.value is original
    assert_cleaned(env, stage="browser_connect", old=False)
