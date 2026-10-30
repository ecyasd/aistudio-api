"""Browser recovery regressions using synthetic pages and no upstream access."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aistudio_api.config import settings
from aistudio_api.infrastructure.browser.cdp_client import CDPClient, CDPPage
from aistudio_api.infrastructure.gateway.session import BrowserSession
from aistudio_api.infrastructure.gateway.session_auth import (
    load_cookies_to_page,
    save_browser_cookies,
)
from aistudio_api.infrastructure.gateway.session_state import (
    SessionContext,
    SessionState,
)
from aistudio_api.infrastructure.utils import error_records, response_diagnostics


def context(target_id: str = "synthetic", auth_file: str | None = None):
    page = MagicMock(spec=CDPPage)
    page.target_id = target_id
    page.is_closed.return_value = False
    return SessionContext(page, "synthetic-snapshot", "0", auth_file, {"body": "[]"})


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setattr(settings, "auth_file", None)
    monkeypatch.setattr(settings, "browser_idle_timeout", 5)
    return BrowserSession(19222)


@contextmanager
def request_diagnostics(monkeypatch):
    store = response_diagnostics.ResponseDiagnosticStore()
    monkeypatch.setattr(response_diagnostics, "get_response_diagnostic_store", lambda: store)
    token = error_records.bind_request_context()
    response_diagnostics.initialize_response_diagnostics(stream=True)
    response_diagnostics.configure_generation(model="gemini-synthetic", auto_continue=False)
    try:
        yield store
    finally:
        response_diagnostics.finish_response_diagnostics("success", 200)
        error_records.reset_request_context(token)


async def test_consecutive_requests_reuse_ready_context_without_transition(session, monkeypatch):
    current = context()
    session.set_ready_context(current)
    session.transition_to_account = AsyncMock(side_effect=AssertionError("must reuse"))
    with request_diagnostics(monkeypatch) as store:
        assert await session.ensure_ready() is current
        async with session.request_scope() as acquired:
            assert acquired is current
        assert await session.ensure_ready() is current
    session.transition_to_account.assert_not_awaited()
    assert current.in_flight == 0
    item = store.list_records()["items"][0]
    assert item["session_preparation_reason"] == "warm_reuse"
    assert item["browser_idle_timeout_s"] == 5


async def test_idle_recycle_waits_for_threshold_and_preserves_reason_after_preparation(session, monkeypatch):
    from aistudio_api.infrastructure.gateway import session as module

    clock = [104.0]
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    session._last_activity_time = 100.0
    session._proc = MagicMock()
    assert await session.check_idle_timeout() is False
    clock[0] = 106.0
    assert await session.check_idle_timeout() is True
    assert session._last_stop_reason == "idle"

    page = context().page
    client = SimpleNamespace(create_new_page=AsyncMock(return_value=page))

    async def launch():
        session._proc = MagicMock()
        session._cdp_client = client

    monkeypatch.setattr(session, "_ensure_browser_cdp", launch)
    monkeypatch.setattr(session, "persist_context_cookies", AsyncMock())
    monkeypatch.setattr(module, "navigate_to_aistudio", AsyncMock())
    monkeypatch.setattr(module, "bootstrap_botguard_and_template", AsyncMock(return_value=("synthetic-key", {"body": "[]"})))
    with request_diagnostics(monkeypatch) as store:
        prepared = await session.transition_to_account(None)
        assert await session.ensure_ready() is prepared
    item = store.list_records()["items"][0]
    assert item["session_preparation_reason"] == "idle_recycle"
    assert item["browser_idle_timeout_s"] == 5


@pytest.mark.asyncio
async def test_idle_monitor_rechecks_request_after_waiting_for_transition(session):
    session._proc = MagicMock()
    session._last_activity_time = 0
    session._close_internal = AsyncMock()
    await session._transition_lock.acquire()
    monitor = asyncio.create_task(session.check_idle_timeout())
    await asyncio.sleep(0)
    session._in_flight = 1
    session._last_activity_time = time.time()
    session._transition_lock.release()
    assert await monitor is False
    session._close_internal.assert_not_awaited()


@pytest.mark.asyncio
async def test_idle_monitor_rechecks_fresh_activity_without_an_active_request(session):
    session._proc = MagicMock()
    session._last_activity_time = 0
    session._close_internal = AsyncMock()
    await session._transition_lock.acquire()
    monitor = asyncio.create_task(session.check_idle_timeout())
    await asyncio.sleep(0)
    session._last_activity_time = time.time()
    session._transition_lock.release()
    assert await monitor is False
    session._close_internal.assert_not_awaited()


@pytest.mark.asyncio
async def test_idle_cleanup_blocks_new_leases_before_awaiting_cookie_save(session):
    old = context("old", "synthetic-old-auth")
    new = context("new")
    session.set_ready_context(old)
    session._proc = MagicMock()
    session._last_activity_time = 0
    saving = asyncio.Event()
    finish_save = asyncio.Event()

    async def save(_context):
        saving.set()
        await finish_save.wait()

    async def transition(*_args, **_kwargs):
        async with session._transition_lock:
            session.set_ready_context(new)
            return new

    session.persist_context_cookies = AsyncMock(side_effect=save)
    session.transition_to_account = transition
    idle = asyncio.create_task(session.check_idle_timeout())
    await saving.wait()

    async def acquire():
        async with session.request_scope() as acquired:
            return acquired

    request = asyncio.create_task(acquire())
    await asyncio.sleep(0)
    assert not request.done()
    assert old.in_flight == 0
    finish_save.set()
    assert await idle is True
    assert await request is new


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError("CDP startup timeout"), asyncio.CancelledError()])
async def test_failed_cdp_startup_disposes_its_process_and_client(session, error):
    proc = MagicMock()
    proc.is_alive.return_value = True
    client = MagicMock(spec=CDPClient)
    client.wait_until_ready = AsyncMock(side_effect=error)
    client.close = AsyncMock()
    with (
        patch("aistudio_api.infrastructure.gateway.session.launch_chromium_process", return_value=proc),
        patch("aistudio_api.infrastructure.gateway.session.CDPClient", return_value=client),
        pytest.raises(type(error)),
    ):
        await session.transition_to_account(None)
    proc.terminate.assert_called_once()
    client.close.assert_awaited_once()
    assert session._proc is None
    assert session._cdp_client is None
    assert session.get_status()["status"] == "failed"


@pytest.mark.asyncio
async def test_next_request_retries_a_failed_cold_start_with_a_fresh_process(session):
    proc1, proc2 = MagicMock(), MagicMock()
    proc1.is_alive.return_value = proc2.is_alive.return_value = True
    client1, client2 = MagicMock(spec=CDPClient), MagicMock(spec=CDPClient)
    client1.wait_until_ready = AsyncMock(side_effect=TimeoutError("CDP not ready"))
    client1.close = AsyncMock()
    client2.wait_until_ready = AsyncMock()
    client2.create_new_page = AsyncMock(return_value=context().page)
    with (
        patch("aistudio_api.infrastructure.gateway.session.launch_chromium_process", side_effect=[proc1, proc2]) as launch,
        patch("aistudio_api.infrastructure.gateway.session.CDPClient", side_effect=[client1, client2]),
        patch("aistudio_api.infrastructure.gateway.session.navigate_to_aistudio", AsyncMock()),
        patch("aistudio_api.infrastructure.gateway.session.bootstrap_botguard_and_template", AsyncMock(return_value=("snapshot", {"body": "[]"}))),
    ):
        with pytest.raises(TimeoutError):
            await session.ensure_ready()
        recovered = await session.ensure_ready()
        assert launch.call_count == 2
    assert recovered is session.active_context
    assert session.is_ready
    assert session._proc is proc2
    proc1.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_failed_bootstrap_releases_cold_browser_for_the_next_request(session):
    proc = MagicMock()
    proc.is_alive.return_value = True
    client = MagicMock(spec=CDPClient)
    client.wait_until_ready = AsyncMock()
    client.create_new_page = AsyncMock(return_value=context().page)
    client.close = AsyncMock()
    with (
        patch("aistudio_api.infrastructure.gateway.session.launch_chromium_process", return_value=proc),
        patch("aistudio_api.infrastructure.gateway.session.CDPClient", return_value=client),
        patch("aistudio_api.infrastructure.gateway.session.navigate_to_aistudio", AsyncMock()),
        patch("aistudio_api.infrastructure.gateway.session.bootstrap_botguard_and_template", AsyncMock(side_effect=RuntimeError("synthetic handshake failure"))),
        pytest.raises(RuntimeError, match="synthetic handshake failure"),
    ):
        await session.ensure_ready()
    proc.terminate.assert_called_once()
    assert session.get_status()["status"] == "failed"
    assert session._proc is None


@pytest.mark.asyncio
@pytest.mark.parametrize("process_dead", [False, True])
async def test_closed_page_or_dead_process_is_not_ready_and_can_recover(session, process_dead):
    old = context("old")
    session.set_ready_context(old)
    session._proc = MagicMock()
    session._proc.is_alive.return_value = not process_dead
    old.page.is_closed.return_value = not process_dead
    assert not session.is_ready
    assert session.state == SessionState.FAILED
    new = context("new")

    async def transition(*_args, **_kwargs):
        session._proc.is_alive.return_value = True
        session.set_ready_context(new)
        return new

    session.transition_to_account = AsyncMock(side_effect=transition)
    assert await session.ensure_ready() is new
    session.transition_to_account.assert_awaited_once_with(None, force_refresh=False)


@pytest.mark.asyncio
async def test_cold_capture_waits_for_a_real_bootstrap_template(session):
    prepared = context()
    prepared.base_template = {"body": '["models/from-actual-bootstrap",null]'}

    async def transition(*_args, **_kwargs):
        session.set_ready_context(prepared)
        return prepared

    session.transition_to_account = AsyncMock(side_effect=transition)
    result = await session.capture_template("models/target")
    assert result["body"] == '["models/target",null]'
    session.transition_to_account.assert_awaited_once()


@pytest.mark.asyncio
async def test_persist_and_close_use_each_contexts_own_account_path(session):
    old, new = context("old", "synthetic-account-a"), context("new", "synthetic-account-b")
    session._retiring_contexts.append(old)
    session.set_ready_context(new)
    session._auth_file = "unrelated-active-registry-path"
    session._cdp_client = MagicMock(spec=CDPClient)
    session._cdp_client.close = AsyncMock()
    with patch("aistudio_api.infrastructure.gateway.session.save_browser_cookies", AsyncMock()) as save:
        await session.close()
    assert save.await_count == 2
    assert save.await_args_list[0].kwargs["auth_file"] == "synthetic-account-a"
    assert save.await_args_list[1].kwargs["auth_file"] == "synthetic-account-b"


@pytest.mark.asyncio
async def test_import_waiting_for_browser_does_not_replace_a_newer_owner_import(session):
    page = context().page
    version_captured = []

    def version(_path):
        version_captured.append(True)
        return "synthetic-old-version"

    @asynccontextmanager
    async def temporary_page():
        assert version_captured
        yield page

    session._temporary_auth_page = temporary_page
    with (
        patch("aistudio_api.infrastructure.gateway.session.get_auth_file_version", side_effect=version),
        patch("aistudio_api.infrastructure.gateway.session.bind_auth_file", return_value=False) as bind,
        patch("aistudio_api.infrastructure.gateway.session.save_browser_cookies", AsyncMock()) as save,
    ):
        assert await session.import_cookies("SAPISID=synthetic", "synthetic-account") == 1
    bind.assert_called_once_with(page, "synthetic-account", expected_version="synthetic-old-version")
    page.set_cookies.assert_not_awaited()
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_account_refresh_drains_old_checkpoint_before_binding_new_page(session, tmp_path):
    auth_path = tmp_path / "synthetic-auth.json"

    def jar(generation):
        return [
            {"name": "SID", "value": f"synthetic-sid-{generation}", "domain": ".google.com"},
            {"name": "SAPISID", "value": f"synthetic-sapisid-{generation}", "domain": ".google.com"},
        ]

    auth_path.write_text(json.dumps({"cookies": jar(1), "origins": []}))
    old, new = context("old", str(auth_path)), context("new", str(auth_path))
    old.page.url = new.page.url = "https://aistudio.google.com/prompts/new_chat"
    old.page.get_cookies = AsyncMock(return_value=jar(2))
    new.page.get_cookies = AsyncMock(return_value=jar(3))
    assert await load_cookies_to_page(old.page, str(auth_path))
    session._auth_file = str(auth_path)
    session.set_ready_context(old)
    session._proc = MagicMock()
    session._proc.is_alive.return_value = True
    session._cdp_client = MagicMock(spec=CDPClient)
    session._cdp_client.create_new_page = AsyncMock(return_value=new.page)
    session._cdp_client.close_target = AsyncMock()
    old.incr_in_flight()

    with (
        patch("aistudio_api.infrastructure.gateway.session_auth.verify_account_identity", AsyncMock(return_value=True)),
        patch("aistudio_api.infrastructure.gateway.session.navigate_to_aistudio", AsyncMock()),
        patch("aistudio_api.infrastructure.gateway.session.bootstrap_botguard_and_template", AsyncMock(return_value=("new-snapshot", {"body": "[]"}))),
    ):
        refresh = asyncio.create_task(session.transition_to_account(str(auth_path), force_refresh=True))
        await asyncio.sleep(0)
        assert session.get_status()["status"] == "starting"
        session._cdp_client.create_new_page.assert_not_awaited()
        old.page.close.assert_not_awaited()
        await session.persist_context_cookies(old)
        old.decr_in_flight()
        assert (await refresh).page is new.page
        assert new.page.set_cookies.await_args.args[0] == jar(2)
        assert await save_browser_cookies(new.page, str(auth_path))
    assert json.loads(auth_path.read_text())["cookies"] == jar(3)
    old.page.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_same_account_refresh_never_waits_for_its_own_request_lease(session):
    active = context("active", "synthetic-account")
    session.set_ready_context(active)
    session._auth_file = active.auth_file
    async with session.request_scope():
        with pytest.raises(RuntimeError, match="owning its active request lease"):
            await asyncio.wait_for(session.transition_to_account(active.auth_file, force_refresh=True), timeout=1)
        assert session.is_ready


@pytest.mark.asyncio
async def test_closed_old_page_is_still_disposed_during_account_switch(session):
    old, new = context("crashed", "synthetic-old"), context("new", "synthetic-new")
    old.page.is_closed.return_value = True
    session.set_ready_context(old)
    session._proc = MagicMock()
    session._proc.is_alive.return_value = True
    session._cdp_client = MagicMock(spec=CDPClient)
    session._cdp_client.create_new_page = AsyncMock(return_value=new.page)
    session._cdp_client.close_target = AsyncMock()
    with (
        patch("aistudio_api.infrastructure.gateway.session.load_cookies_to_page", AsyncMock(return_value=True)),
        patch("aistudio_api.infrastructure.gateway.session.navigate_to_aistudio", AsyncMock()),
        patch("aistudio_api.infrastructure.gateway.session.bootstrap_botguard_and_template", AsyncMock(return_value=("snapshot", {"body": "[]"}))),
    ):
        await session.transition_to_account(new.auth_file)
        await asyncio.gather(*session._background_tasks)
    session._cdp_client.close_target.assert_awaited_once_with("crashed")
    assert not session._retiring_contexts


def test_status_distinguishes_lazy_idle_starting_ready_failed_and_closed(session):
    assert session.get_status()["status"] == "not_started"
    session._last_stop_reason = "idle"
    assert session.get_status()["status"] == "idle"
    session._gate.set_preparing()
    assert session.get_status()["status"] == "starting"
    session._proc = MagicMock()
    session._proc.is_alive.return_value = True
    session.set_ready_context(context())
    assert session.get_status()["status"] == "ready"
    session._proc.is_alive.return_value = False
    assert session.get_status()["status"] == "failed"
    session._gate.reset()
    session._proc = None
    session._last_stop_reason = "closed"
    assert session.get_status() == {
        "status": "closed", "process_running": False,
        "in_flight": 0, "idle_timeout_seconds": 5,
    }


def test_temporary_auth_browser_is_starting_instead_of_not_started(session):
    session._proc = MagicMock()
    session._proc.is_alive.return_value = True
    assert session.get_status()["status"] == "starting"
