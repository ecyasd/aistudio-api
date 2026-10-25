"""AnyIO level cancellation must allow session rollback to finish in its task."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest

from aistudio_api.infrastructure.browser.cdp_client import CDPClient, CDPPage
from aistudio_api.infrastructure.gateway import session as session_module
from aistudio_api.infrastructure.gateway.session_state import (
    SessionContext,
    SessionState,
)


@pytest.fixture
def transition_env(monkeypatch):
    monkeypatch.setattr(session_module, "derive_profile_dir", lambda _auth: None)
    monkeypatch.setattr(session_module, "get_auth_generation", lambda _auth: None)
    monkeypatch.setattr(session_module, "get_auth_user_from_meta", lambda _auth: "1")
    monkeypatch.setattr(session_module, "load_cookies_to_page", AsyncMock(return_value=True))
    monkeypatch.setattr(session_module, "navigate_to_aistudio", AsyncMock())
    session = session_module.BrowserSession(port=1)
    session._auth_file = "synthetic-old-auth"
    session._auth_user = "0"
    page = MagicMock(spec=CDPPage)
    page.target_id = "synthetic-candidate"
    page.is_closed.return_value = False
    client = MagicMock(spec=CDPClient)
    client.create_new_page = AsyncMock(return_value=page)
    client.wait_until_ready = AsyncMock()
    closed = []

    async def close_target(target):
        await anyio.sleep(0)
        closed.append(target)

    async def close_client():
        await anyio.sleep(0)
        closed.append("client")

    client.close_target = AsyncMock(side_effect=close_target)
    client.close = AsyncMock(side_effect=close_client)
    proc = MagicMock()
    proc.is_alive.return_value = True
    monkeypatch.setattr(session_module, "launch_chromium_process", lambda **_kwargs: proc)
    monkeypatch.setattr(session_module, "CDPClient", lambda **_kwargs: client)
    started = asyncio.Event()

    async def block(*_args, **_kwargs):
        started.set()
        await anyio.sleep_forever()

    monkeypatch.setattr(session_module, "bootstrap_botguard_and_template", block)
    return SimpleNamespace(
        session=session, client=client, proc=proc, page=page, closed=closed,
        started=started, block=block,
    )


async def _cancel_during(operation, started):
    cancelled = []

    async def run():
        try:
            await operation()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        pytest.fail("session cancellation must propagate after cleanup")

    async with anyio.create_task_group() as group:
        group.start_soon(run)
        await started.wait()
        group.cancel_scope.cancel()
    assert cancelled == [True]


async def test_level_cancelled_transition_closes_candidate_and_restores_old_context(transition_env):
    env = transition_env
    session = env.session
    old_page = MagicMock(spec=CDPPage)
    old_page.is_closed.return_value = False
    old = SessionContext(
        page=old_page, snap_key="synthetic", auth_user="0",
        auth_file="synthetic-old-auth", base_template={},
    )
    session._proc = env.proc
    session._cdp_client = env.client
    session.set_ready_context(old)

    await _cancel_during(
        lambda: session.transition_to_account("synthetic-new-auth"), env.started
    )

    assert env.closed == ["synthetic-candidate"]
    assert session.state == SessionState.READY
    assert session.active_context is old
    assert session.is_ready
    assert session._auth_file == "synthetic-old-auth"
    assert session._auth_user == "0"
    assert not session._transition_lock.locked()
    old_page.close.assert_not_called()
    env.proc.terminate.assert_not_called()


@pytest.mark.parametrize("stage", ["cdp_startup", "bootstrap"])
async def test_level_cancelled_cold_transition_closes_client_and_process(transition_env, stage):
    env = transition_env
    session = env.session
    if stage == "cdp_startup":
        env.client.wait_until_ready.side_effect = env.block

    await _cancel_during(
        lambda: session.transition_to_account("synthetic-new-auth"), env.started
    )

    if stage == "bootstrap":
        assert env.closed == ["synthetic-candidate", "client"]
    else:
        assert env.closed == ["client"]
    assert session.state == SessionState.FAILED
    assert session.active_context is None
    assert session._proc is None
    assert session._cdp_client is None
    assert session.get_status()["status"] == "failed"
    assert not session._transition_lock.locked()
    env.proc.terminate.assert_called_once()


async def test_level_cancelled_browser_startup_finishes_owned_cleanup(transition_env):
    env = transition_env
    env.client.wait_until_ready.side_effect = env.block

    await _cancel_during(env.session._ensure_browser_cdp, env.started)

    assert env.closed == ["client"]
    assert env.session._proc is None
    assert env.session._cdp_client is None
    assert env.session.get_status()["status"] == "failed"
    env.proc.terminate.assert_called_once()
