"""Session rollback retains candidate ownership across repeated task cancellation."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from aistudio_api.api.nonstream_disconnect import NonStreamingDisconnectMiddleware
from aistudio_api.application import generation
from aistudio_api.infrastructure.browser.cdp_client import (
    CDPClient,
    CDPConnection,
    CDPPage,
)
from aistudio_api.infrastructure.gateway import session as session_module
from aistudio_api.infrastructure.gateway.session_state import (
    SessionContext,
    SessionState,
)


class SyntheticSocket:
    def __init__(self, owner, disposing=None, allow_dispose=None):
        self.owner = owner
        self.disposing = disposing
        self.allow_dispose = allow_dispose
        self.delivered = []

    async def send(self, raw):
        message = json.loads(raw)
        if message["method"] == "Target.disposeBrowserContext":
            self.disposing.set()
            # Model CDP socket backpressure before this side effect is sent.
            await self.allow_dispose.wait()
        self.delivered.append(message["method"])
        self.owner._futures[message["id"]].set_result({})

    async def close(self):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("repeated_cancel", [False, True])
@pytest.mark.parametrize("entry_point", [
    "direct", "nonstream-disconnect", "continuation-deadline-then-disconnect",
])
async def test_preparation_rollback_restores_old_gate_after_cancellation(
    monkeypatch, repeated_cancel, entry_point,
):
    monkeypatch.setattr(session_module, "derive_profile_dir", lambda _path: None)
    monkeypatch.setattr(session_module, "get_auth_generation", lambda _path: None)
    monkeypatch.setattr(session_module, "get_auth_user_from_meta", lambda _path: "0")
    monkeypatch.setattr(session_module, "load_cookies_to_page", AsyncMock(return_value=True))
    monkeypatch.setattr(session_module, "navigate_to_aistudio", AsyncMock())

    bootstrap_started, disposing, allow_dispose = (
        asyncio.Event(), asyncio.Event(), asyncio.Event()
    )

    async def bootstrap(*_args, **_kwargs):
        bootstrap_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(session_module, "bootstrap_botguard_and_template", bootstrap)
    session = session_module.BrowserSession(port=1)
    session._auth_file = "synthetic-old-auth"
    session._auth_user = "0"
    session._proc = MagicMock()
    session._proc.is_alive.return_value = True

    old_connection = CDPConnection("ws://synthetic.invalid/old")
    old_connection.ws = SyntheticSocket(old_connection)
    old_page = CDPPage(old_connection, "synthetic-old-page", "synthetic-old-context")
    old = SessionContext(old_page, "synthetic-key", "0", "synthetic-old-auth", {})
    session.set_ready_context(old)

    browser_connection = CDPConnection("ws://synthetic.invalid/browser")
    browser_socket = SyntheticSocket(browser_connection, disposing, allow_dispose)
    browser_connection.ws = browser_socket
    candidate_connection = CDPConnection("ws://synthetic.invalid/candidate")
    candidate_connection.ws = SyntheticSocket(candidate_connection)
    candidate = CDPPage(
        candidate_connection, "synthetic-candidate-page", "synthetic-candidate-context",
        browser_connection,
    )
    client = CDPClient(port=1)
    client._browser_cdp = browser_connection
    client._owned_pages[candidate.target_id] = candidate
    client._browser_contexts.add(candidate.browser_context_id)
    client.create_new_page = AsyncMock(return_value=candidate)
    session._cdp_client = client

    incoming = asyncio.Queue()
    incoming.put_nowait({"type": "http.request", "body": b"{}", "more_body": False})

    async def prepare(_number, *, model):
        await session.transition_to_account("synthetic-new-auth")

    monkeypatch.setattr(generation, "ensure_active_account", prepare)
    monkeypatch.setattr(generation.settings, "timeout_stream", 0.02)

    async def application(_scope, receive, _send):
        await receive()
        if entry_point == "continuation-deadline-then-disconnect":
            run = generation.GenerationRun(
                SimpleNamespace(session=session), SimpleNamespace(model="synthetic"),
                auto_continue=True,
            )
            await run.generate()
        else:
            await session.transition_to_account("synthetic-new-auth")

    if entry_point == "direct":
        operation = session.transition_to_account("synthetic-new-auth")
    else:
        operation = NonStreamingDisconnectMiddleware(application)(
            {"type": "http", "method": "POST", "path": "/v1beta/models/synthetic:generateContent"},
            incoming.get, AsyncMock(),
        )
    task = asyncio.create_task(operation)
    await bootstrap_started.wait()
    if entry_point == "direct":
        task.cancel()
    elif entry_point == "nonstream-disconnect":
        incoming.put_nowait({"type": "http.disconnect"})
    await disposing.wait()
    if repeated_cancel:
        if entry_point == "continuation-deadline-then-disconnect":
            # The real GenerationRun timer supplied the first cancellation;
            # the real middleware supplies the second after client disconnect.
            incoming.put_nowait({"type": "http.disconnect"})
        else:
            task.cancel()
        completed, _ = await asyncio.wait({task}, timeout=0.02)
        exited_before_disposal = bool(completed)
    else:
        exited_before_disposal = False
    allow_dispose.set()
    if entry_point == "continuation-deadline-then-disconnect":
        if repeated_cancel:
            await task
        else:
            with pytest.raises(generation.GenerationDeadlineExceeded):
                await task
    elif entry_point == "direct" or repeated_cancel:
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task

    observed = {
        "repeat": repeated_cancel,
        "entry_point": entry_point,
        "gate": str(session.state),
        "old_context_retained": session.active_context is old,
        "old_page_closed": old_page.is_closed(),
        "ready": session.is_ready,
        "candidate_owned": candidate.target_id in client._owned_pages,
        "candidate_context_owned": candidate.browser_context_id in client._browser_contexts,
        "dispose_delivered": "Target.disposeBrowserContext" in browser_socket.delivered,
        "transition_lock_held": session._transition_lock.locked(),
    }
    try:
        if repeated_cancel:
            with suppress(TimeoutError):
                await session.ensure_ready(timeout_s=0.02)
            observed["gate_after_followup_ready"] = str(session.state)
        assert not exited_before_disposal, observed
        # Correct rollback must dispose only the candidate and reopen the old gate.
        assert session.state is SessionState.READY
        assert session.is_ready
        assert session.active_context is old
        assert candidate.target_id not in client._owned_pages
        assert candidate.browser_context_id not in client._browser_contexts
        assert not old_page.is_closed()
    finally:
        # Release the entirely synthetic resources even when the contract fails.
        allow_dispose.set()
        await client.close()
        await old_page.close()

