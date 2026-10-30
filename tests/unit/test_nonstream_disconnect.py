"""Nonstream clients must release inference resources when their transport closes."""
from __future__ import annotations

import asyncio
import importlib.util
import json
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from aistudio_api.api.dependencies import get_client, require_api_key
from aistudio_api.api.error_middleware import ErrorRecordsMiddleware
from aistudio_api.api.nonstream_disconnect import NonStreamingDisconnectMiddleware
from aistudio_api.application import api_service_gemini as service
from aistudio_api.application.inference_limits import InferenceLimiter
from aistudio_api.domain.errors import RequestError
from aistudio_api.domain.models import Candidate, ModelOutput
from aistudio_api.infrastructure.utils import error_records, response_diagnostics


class Connection:
    def __init__(self, *, spec="2.3", path=None):
        self.disconnected = asyncio.Event()
        self.listening = asyncio.Event()
        self.request_sent = False
        self.receiving = False
        self.sent = []
        self.spec = spec
        self.path = path or "/v1beta/models/gemini-test:generateContent"

    async def receive(self):
        assert not self.receiving, "request body and disconnect must have one reader"
        self.receiving = True
        try:
            if not self.request_sent:
                self.request_sent = True
                return {
                    "type": "http.request",
                    "body": json.dumps({"contents": [{"role": "user", "parts": [{"text": "synthetic"}]}]}).encode(),
                    "more_body": False,
                }
            self.listening.set()
            await self.disconnected.wait()
            return {"type": "http.disconnect"}
        finally:
            self.receiving = False

    async def send(self, message):
        # Uvicorn ignores sends once a connection is gone.
        if not self.disconnected.is_set():
            self.sent.append(message)

    def scope(self):
        return {
            "type": "http", "asgi": {"version": "3.0", "spec_version": self.spec},
            "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": self.path, "raw_path": self.path.encode(),
            "query_string": b"", "root_path": "",
            "headers": [(b"content-type", b"application/json"), (b"x-api-key", b"synthetic-key")],
            "server": ("synthetic.invalid", 80), "client": ("127.0.0.1", 10000),
        }

    def start(self, app):
        return asyncio.create_task(app(self.scope(), self.receive, self.send))

    @property
    def status(self):
        return next(item["status"] for item in self.sent if item["type"] == "http.response.start")


@pytest.fixture
def generation(monkeypatch):
    from aistudio_api.api.app import app

    limiter = InferenceLimiter(concurrency=2, max_waiting=4, wait_seconds=1)
    monkeypatch.setattr(service, "inference_limiter", limiter)
    monkeypatch.setattr(service, "ensure_active_account", AsyncMock())
    monkeypatch.setattr(service, "account_id_for_auth_file", lambda _path: "synthetic-account")
    monkeypatch.setattr(service, "record_rotator_event", MagicMock())
    monkeypatch.setattr(service.runtime_state, "record", MagicMock())
    diagnostics = response_diagnostics.ResponseDiagnosticStore()
    errors = error_records.ErrorRecordStore()
    monkeypatch.setattr(response_diagnostics, "_store", diagnostics)
    monkeypatch.setattr(error_records, "_store", errors)
    env = SimpleNamespace(
        app=app, limiter=limiter, leases=[], entered=asyncio.Event(), closed=[],
        mode="waiting", diagnostics=diagnostics, errors=errors, contexts=[],
    )
    lease_context = ContextVar("synthetic_nonstream_lease", default=None)

    @asynccontextmanager
    async def request_scope():
        context = SimpleNamespace(auth_file="synthetic-account", context_id=len(env.leases) + 1)
        env.leases.append(context)
        token = lease_context.set(context)
        try:
            yield context
        finally:
            # BrowserSession.request_scope releases its lease synchronously.
            lease_context.reset(token)
            env.leases.remove(context)

    async def upstream(_client, _normalized, **_kwargs):
        assert lease_context.get() in env.leases
        env.contexts.append(error_records.get_request_context())
        env.entered.set()
        try:
            if env.mode == "waiting":
                await asyncio.Event().wait()
            if env.mode == "error":
                raise RequestError(504, "synthetic upstream timeout")
            return ModelOutput(candidates=[Candidate(text="completed", finish_reason=1)])
        finally:
            env.closed.append(True)

    client = SimpleNamespace(session=SimpleNamespace(request_scope=request_scope))
    monkeypatch.setattr(service.continuation, "generate_content", upstream)
    monkeypatch.setitem(app.dependency_overrides, get_client, lambda: client)
    monkeypatch.setitem(app.dependency_overrides, require_api_key, lambda: None)
    return env


def secured(app):
    spec = importlib.util.spec_from_file_location(
        "synthetic_disconnect_guard", Path(__file__).resolve().parents[2] / "deploy/public_access_guard.py"
    )
    assert spec is not None and spec.loader is not None
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    return ErrorRecordsMiddleware(guard.PublicAccessGuard(
        app, settings_provider=lambda: SimpleNamespace(web_password="synthetic-admin"),
        api_keys_provider=lambda: {"synthetic-key"},
    ))


def live_monitors():
    return [task for task in asyncio.all_tasks()
            if not task.done() and task.get_name().startswith("nonstream-")]


@pytest.mark.parametrize("secure", [False, True])
@pytest.mark.parametrize("spec", ["2.0", "2.3", "2.4"])
async def test_disconnected_requests_release_both_slots_and_next_request_succeeds(generation, spec, secure):
    env = generation
    app = secured(env.app) if secure else env.app
    connections = [Connection(spec=spec), Connection(spec=spec)]
    tasks = [connection.start(app) for connection in connections]
    try:
        async with asyncio.timeout(1):
            while len(env.leases) != 2:
                await asyncio.sleep(0)
        for connection in connections:
            connection.disconnected.set()
        done, _ = await asyncio.wait(tasks, timeout=0.4)
        assert len(done) == 2, "disconnected nonstream requests still hold inference slots"
        assert all(task.result() is None for task in tasks)
        assert env.limiter.active == 0 and not env.leases
        assert len(env.closed) == 2
        assert all(not connection.sent for connection in connections)
        items = env.diagnostics.list_records()["items"]
        assert len(items) == 2 and all(item["outcome"] == "cancelled" for item in items)
        assert {item["request_id"] for item in items} == {
            context.request_id for context in env.contexts
        }
        errors = env.errors.list_records()["items"]
        assert len(errors) == 2
        assert all(item["status"] == 499 and item["category"] == "cancelled" for item in errors)
        env.mode = "success"
        third = Connection(spec=spec)
        await asyncio.wait_for(third.start(app), 1)
        assert third.status == 200
        assert env.limiter.active == 0 and not env.leases
        assert not live_monitors()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_disconnect_while_preparing_account_releases_slot(generation):
    env = generation
    closed = asyncio.Event()

    async def prepare(*_args, **_kwargs):
        env.entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    service.ensure_active_account.side_effect = prepare
    connection = Connection()
    task = connection.start(env.app)
    try:
        await asyncio.wait_for(env.entered.wait(), 1)
        connection.disconnected.set()
        await asyncio.wait_for(task, 1)
        assert closed.is_set() and env.limiter.active == 0
        assert not env.leases and not live_monitors()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_disconnect_while_queued_removes_only_its_waiter(generation):
    env = generation
    first, second, queued = Connection(), Connection(), Connection()
    tasks = [first.start(env.app), second.start(env.app)]
    try:
        async with asyncio.timeout(1):
            while env.limiter.active != 2:
                await asyncio.sleep(0)
        tasks.append(queued.start(env.app))
        async with asyncio.timeout(1):
            while env.limiter.waiting != 1:
                await asyncio.sleep(0)
        queued.disconnected.set()
        await asyncio.wait_for(tasks[-1], 1)
        assert env.limiter.waiting == 0 and env.limiter.active == 2
        first.disconnected.set()
        second.disconnected.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 1)
        assert env.limiter.active == 0 and not env.leases and not live_monitors()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize(("mode", "status"), [("success", 200), ("error", 504)])
async def test_normal_and_error_completion_stop_disconnect_listener(generation, mode, status):
    env = generation
    env.mode = mode
    connection = Connection()
    await asyncio.wait_for(connection.start(env.app), 1)
    assert connection.status == status
    assert not connection.receiving and not live_monitors()
    assert env.limiter.active == 0 and not env.leases
    summary = env.diagnostics.list_records()["items"][0]
    assert summary["outcome"] == ("success" if mode == "success" else "failed")


async def test_parent_cancellation_propagates_after_resource_cleanup(generation):
    env = generation
    connection = Connection()
    task = connection.start(env.app)
    await asyncio.wait_for(env.entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert env.limiter.active == 0 and not env.leases
    assert not live_monitors() and not connection.receiving


async def test_fragmented_body_and_response_disconnect_listener_have_one_raw_reader():
    messages = [
        {"type": "http.request", "body": b"first", "more_body": True},
        {"type": "http.request", "body": b"second", "more_body": True},
        {"type": "http.request", "body": b"last", "more_body": False},
    ]
    received = []
    raw_waiting = asyncio.Event()
    raw_receiving = False
    raw_calls = 0
    listener = None
    sent = []

    async def receive():
        nonlocal raw_receiving, raw_calls
        assert not raw_receiving, "concurrent raw receive during response sending"
        raw_receiving = True
        raw_calls += 1
        try:
            if messages:
                return messages.pop(0)
            raw_waiting.set()
            await asyncio.Event().wait()
        finally:
            raw_receiving = False

    async def app(_scope, receive_body, send):
        nonlocal listener
        for _ in range(3):
            received.append(await receive_body())
        listener = asyncio.create_task(receive_body())
        try:
            await raw_waiting.wait()
            await asyncio.sleep(0)
            assert not listener.done()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"complete"})
        finally:
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)

    async def send(message):
        sent.append(message)

    await asyncio.wait_for(NonStreamingDisconnectMiddleware(app)(Connection().scope(), receive, send), 1)
    assert [message["body"] for message in received] == [b"first", b"second", b"last"]
    assert raw_calls == 4 and not raw_receiving
    assert sent[-1]["body"] == b"complete" and not live_monitors()


async def test_receive_error_cancels_and_awaits_inference_cleanup():
    first = True
    started = asyncio.Event()
    closed = asyncio.Event()

    async def receive():
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": b"", "more_body": False}
        await started.wait()
        raise RuntimeError("synthetic receive failure")

    async def app(_scope, receive_body, _send):
        await receive_body()
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.01)
            closed.set()

    with pytest.raises(RuntimeError, match="synthetic receive failure"):
        await NonStreamingDisconnectMiddleware(app)(Connection().scope(), receive, AsyncMock())
    assert closed.is_set() and not live_monitors()


async def test_completed_response_wins_simultaneous_monitor_failure():
    first = True
    sent = []

    async def receive():
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": b"", "more_body": False}
        raise RuntimeError("synthetic late receive failure")

    async def app(_scope, receive_body, send):
        await receive_body()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"completed"})

    async def send(message):
        sent.append(message)

    await NonStreamingDisconnectMiddleware(app)(Connection().scope(), receive, send)
    assert sent[-1]["body"] == b"completed" and not live_monitors()


@pytest.mark.parametrize("tail_error", [False, True])
async def test_finished_response_preserves_application_tail_when_server_reports_disconnect(tail_error):
    first = True
    response_sent = asyncio.Event()
    tail_completed = asyncio.Event()

    async def receive():
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": b"", "more_body": False}
        # Uvicorn's receive also returns disconnect once the response completes.
        await response_sent.wait()
        return {"type": "http.disconnect"}

    async def app(_scope, receive_body, send):
        await receive_body()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"completed"})
        await asyncio.sleep(0.01)
        tail_completed.set()
        if tail_error:
            raise RuntimeError("synthetic application finalization failure")

    async def send(message):
        if message["type"] == "http.response.body":
            response_sent.set()

    operation = NonStreamingDisconnectMiddleware(app)(Connection().scope(), receive, send)
    if tail_error:
        with pytest.raises(RuntimeError, match="application finalization failure"):
            await operation
    else:
        await operation
    assert tail_completed.is_set() and not live_monitors()


@pytest.mark.parametrize("path", ["/health", "/v1beta/models/gemini-test:streamGenerateContent"])
async def test_other_routes_keep_original_receive_and_task(path):
    owner = asyncio.current_task()
    receive = AsyncMock()

    async def app(_scope, original_receive, _send):
        assert asyncio.current_task() is owner and original_receive is receive

    await NonStreamingDisconnectMiddleware(app)(Connection(path=path).scope(), receive, AsyncMock())
    receive.assert_not_awaited()
    assert not live_monitors()


async def test_uvicorn_accepts_disconnected_app_without_logging_asgi_500(generation):
    import h11
    from uvicorn.protocols.http.h11_impl import RequestResponseCycle

    env = generation
    connection = Connection()
    logger = MagicMock()
    transport = MagicMock()
    cycle = RequestResponseCycle(
        scope=connection.scope(), conn=h11.Connection(h11.SERVER), transport=transport,
        flow=MagicMock(), logger=logger, access_logger=MagicMock(), access_log=False,
        default_headers=[], message_event=asyncio.Event(), on_response=MagicMock(),
    )
    cycle.body = bytearray(json.dumps({
        "contents": [{"role": "user", "parts": [{"text": "synthetic"}]}]}
    ).encode())
    cycle.more_body = False
    cycle.message_event.set()
    task = asyncio.create_task(cycle.run_asgi(secured(env.app)))
    try:
        await asyncio.wait_for(env.entered.wait(), 1)
        cycle.disconnected = True
        cycle.message_event.set()
        await asyncio.wait_for(task, 1)
        assert env.limiter.active == 0 and not env.leases and not live_monitors()
        logger.error.assert_not_called()
        transport.write.assert_not_called()
        assert env.errors.list_records()["items"][0]["status"] == 499
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_disconnect_during_real_session_transition_restores_ready_context(generation, monkeypatch):
    from aistudio_api.infrastructure.browser.cdp_client import CDPClient, CDPPage
    from aistudio_api.infrastructure.gateway import session as session_module
    from aistudio_api.infrastructure.gateway.session_state import (
        SessionContext,
        SessionState,
    )

    env = generation
    monkeypatch.setattr(session_module, "derive_profile_dir", lambda _auth: None)
    monkeypatch.setattr(session_module, "get_auth_generation", lambda _auth: None)
    monkeypatch.setattr(session_module, "get_auth_user_from_meta", lambda _auth: "1")
    monkeypatch.setattr(session_module, "load_cookies_to_page", AsyncMock(return_value=True))
    monkeypatch.setattr(session_module, "navigate_to_aistudio", AsyncMock())
    session = session_module.BrowserSession(port=1)
    session._auth_file = "synthetic-old-auth"
    session._auth_user = "0"
    old_page = MagicMock(spec=CDPPage)
    old_page.is_closed.return_value = False
    new_page = MagicMock(spec=CDPPage)
    new_page.target_id = "synthetic-new-page"
    new_page.is_closed.return_value = False
    cdp = MagicMock(spec=CDPClient)
    cdp.create_new_page = AsyncMock(return_value=new_page)
    closed = []

    async def close_target(target):
        await asyncio.sleep(0.01)
        closed.append(target)

    cdp.close_target = AsyncMock(side_effect=close_target)
    session._cdp_client = cdp
    session._proc = SimpleNamespace(is_alive=lambda: True)
    old = SessionContext(
        page=old_page, snap_key="synthetic", auth_user="0",
        auth_file="synthetic-old-auth", base_template={},
    )
    session.set_ready_context(old)

    async def bootstrap(*_args, **_kwargs):
        env.entered.set()
        await asyncio.Event().wait()

    async def prepare(*_args, **_kwargs):
        await session.transition_to_account("synthetic-new-auth")

    monkeypatch.setattr(session_module, "bootstrap_botguard_and_template", bootstrap)
    service.ensure_active_account.side_effect = prepare
    connection = Connection()
    task = connection.start(secured(env.app))
    try:
        await asyncio.wait_for(env.entered.wait(), 1)
        connection.disconnected.set()
        await asyncio.wait_for(task, 1)
        assert env.limiter.active == 0 and not live_monitors()
        assert closed == ["synthetic-new-page"]
        assert session.state == SessionState.READY and session.active_context is old
        assert session.is_ready and not session._transition_lock.locked()
        old_page.close.assert_not_called()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
