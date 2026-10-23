"""Synthetic account imports, stale leases, and empty registry boundaries."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from aistudio_api.api import routes_accounts
from aistudio_api.api.dependencies import get_account_service, get_runtime_state
from aistudio_api.api.state import RuntimeState, runtime_state
from aistudio_api.application import account_orchestrator
from aistudio_api.application.account_orchestrator import (
    ensure_active_account,
    record_rotator_event,
    try_switch_account,
)
from aistudio_api.application.account_rotator import AccountRotator
from aistudio_api.application.account_service import AccountService
from aistudio_api.application.api_service_gemini import handle_attempt_exception
from aistudio_api.domain.errors import AuthError, SessionExpiredError
from aistudio_api.infrastructure.account.account_store import (
    AccountStore,
    _safe_account_dir,
)
from aistudio_api.infrastructure.browser.cdp_client import CDPClient, CDPPage
from aistudio_api.infrastructure.gateway.session import BrowserSession
from aistudio_api.infrastructure.gateway.session_auth import (
    load_cookies_to_page,
    save_browser_cookies,
)
from aistudio_api.infrastructure.gateway.session_state import SessionContext


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    monkeypatch.setattr("aistudio_api.infrastructure.account.account_store._resolve_legacy_auth_file", lambda: None)
    store = AccountStore(tmp_path / "accounts")
    account = store.save_account(
        name="Synthetic", email="synthetic@example.invalid", account_id="synthetic",
        storage_state={"cookies": [{"name": "SID", "value": "synthetic"}]},
    )
    service = AccountService(store)
    rotator = AccountRotator(store)
    return SimpleNamespace(store=store, service=service, account=account, rotator=rotator)


def ready_session(accounts):
    session = BrowserSession(port=1)
    page = MagicMock(spec=CDPPage)
    page.target_id = "synthetic-old"
    page.is_closed.return_value = False
    context = SessionContext(
        page=page, snap_key="synthetic", auth_user="0",
        auth_file=str(accounts.store.get_auth_path(accounts.account.id)), base_template={},
    )
    session._auth_file = context.auth_file
    session.set_ready_context(context)
    session._proc = MagicMock()
    session._proc.is_alive.return_value = True
    session._cdp_client = MagicMock(spec=CDPClient)
    return session, context


def reimport(accounts):
    return accounts.store.save_account(
        name="Synthetic", email=accounts.account.email, account_id=accounts.account.id,
        storage_state={"cookies": [{"name": "SID", "value": "synthetic-new"}]},
    )


def test_dot_and_resolved_root_are_never_account_directories(tmp_path):
    for account_id in (".", "..", "../outside", "a/..", ""):
        with pytest.raises(ValueError):
            _safe_account_dir(tmp_path, account_id)


@pytest.mark.asyncio
async def test_dot_import_and_delete_rejected_without_removing_sibling(accounts):
    app = FastAPI()
    app.include_router(routes_accounts.router, prefix="/api")
    app.dependency_overrides[get_account_service] = lambda: accounts.service
    app.dependency_overrides[get_runtime_state] = lambda: RuntimeState()
    original = accounts.store.get_auth_path(accounts.account.id).read_bytes()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://synthetic") as client:
        imported = await client.post("/api/accounts/import-cookies", json={"cookies": "SID=synthetic", "account_id": "."})
        deleted = await client.delete("/api/accounts/%2E")
    assert imported.status_code == 422
    assert deleted.status_code == 422
    assert accounts.store.get_auth_path(accounts.account.id).read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [False, True])
async def test_deleting_last_account_retires_lease_without_preparing_anonymous_page(accounts, group):
    session, old = ready_session(accounts)
    state = RuntimeState(client=SimpleNamespace(_session=session), rotator=accounts.rotator)
    old.incr_in_flight()
    if group:
        result = await routes_accounts.delete_cookie_group(
            f"cookie_{accounts.account.created_at[:16]}", account_service=accounts.service, runtime_state=state,
        )
        assert result == {"deleted": 1}
    else:
        assert await routes_accounts.delete_account(
            accounts.account.id, account_service=accounts.service, runtime_state=state,
        ) == {"ok": True}
    assert session.active_context is None
    assert session._auth_file is None
    assert not session.is_ready
    with pytest.raises(RuntimeError, match="No account"):
        async with session.request_scope():
            pytest.fail("A deleted account cannot serve a new request")
    old.page.close.assert_not_awaited()
    session._cdp_client.create_new_page.assert_not_awaited()
    old.decr_in_flight()
    await asyncio.gather(*session._background_tasks)
    old.page.close.assert_awaited_once()
    assert accounts.store.list_accounts() == []


@pytest.mark.asyncio
async def test_reimport_requires_new_context_before_next_account_admission(accounts, monkeypatch):
    session, old = ready_session(accounts)
    # Capture the generation actually loaded by this synthetic READY page.
    old.auth_generation = accounts.account.auth_generation
    replacement = reimport(accounts)
    assert replacement.auth_generation != accounts.account.auth_generation
    session.transition_to_account = AsyncMock(return_value=old)
    monkeypatch.setattr(runtime_state, "account_service", accounts.service)
    monkeypatch.setattr(runtime_state, "rotator", accounts.rotator)
    monkeypatch.setattr(runtime_state, "client", SimpleNamespace(session=session))
    await ensure_active_account(0, model="gemini-test")
    session.transition_to_account.assert_awaited_once_with(old.auth_file)


@pytest.mark.asyncio
@pytest.mark.parametrize("has_yielded_data", [False, True])
async def test_old_session_expiration_does_not_quarantine_new_import(accounts, monkeypatch, has_yielded_data):
    old_generation = accounts.account.auth_generation
    reimport(accounts)
    monkeypatch.setattr(runtime_state, "account_service", accounts.service)
    monkeypatch.setattr(runtime_state, "rotator", accounts.rotator)
    client = SimpleNamespace(clear_templates=MagicMock())
    kwargs = {
        "attempt": 0, "model_path": "gemini-test", "normalized_model": "gemini-test", "client": client,
        "account_id": accounts.account.id, "request_context_id": 1,
        "request_auth_generation": old_generation, "has_yielded_data": has_yielded_data,
    }
    if has_yielded_data:
        with pytest.raises(HTTPException) as exc:
            await handle_attempt_exception(SessionExpiredError("synthetic expired"), **kwargs)
        assert exc.value.status_code == 503
    else:
        assert await handle_attempt_exception(SessionExpiredError("synthetic expired"), **kwargs)
    assert accounts.rotator.is_account_available(accounts.account.id, model="gemini-test")


@pytest.mark.asyncio
async def test_real_transition_tags_generation_and_rechecks_before_new_lease(accounts):
    session, old = ready_session(accounts)
    old.auth_generation = accounts.account.auth_generation
    reimport(accounts)
    new_page = MagicMock(spec=CDPPage)
    new_page.target_id = "synthetic-new"
    new_page.is_closed.return_value = False
    session._cdp_client.create_new_page.return_value = new_page
    with (
        patch("aistudio_api.infrastructure.gateway.session.load_cookies_to_page", AsyncMock(return_value=True)),
        patch("aistudio_api.infrastructure.gateway.session.navigate_to_aistudio", AsyncMock()),
        patch("aistudio_api.infrastructure.gateway.session.bootstrap_botguard_and_template", AsyncMock(return_value=("new-snap", {}))),
        patch.object(session, "persist_context_cookies", AsyncMock()),
    ):
        async with session.request_scope() as current:
            assert current is not old
            assert current.auth_generation == accounts.service.get_account(accounts.account.id).auth_generation
    old.page.close.assert_awaited_once()
    assert json.loads(accounts.store._registry_path.read_text())["accounts"][accounts.account.id]["auth_generation"] == current.auth_generation


@pytest.mark.asyncio
async def test_reimport_while_auth_failure_waits_for_switch_lock_stays_available(accounts, monkeypatch):
    session, old = ready_session(accounts)
    old.auth_generation = accounts.account.auth_generation
    monkeypatch.setattr(runtime_state, "account_service", accounts.service)
    monkeypatch.setattr(runtime_state, "rotator", accounts.rotator)
    monkeypatch.setattr(runtime_state, "client", SimpleNamespace(session=session))
    lock = account_orchestrator._get_switch_lock()
    async with lock:
        failure = asyncio.create_task(try_switch_account(
            model="gemini-test", failed_account_id=accounts.account.id,
            failed_context_id=old.context_id, failed_auth_generation=old.auth_generation,
            is_auth_error=True,
        ))
        await asyncio.sleep(0)
        reimport(accounts)
    assert await failure
    assert accounts.rotator.is_account_available(accounts.account.id, model="gemini-test")
    session._cdp_client.create_new_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_checkpoint_cannot_overwrite_byte_identical_reimport(accounts):
    auth = accounts.store.get_auth_path(accounts.account.id)
    page = MagicMock(spec=CDPPage)
    page.is_closed.return_value = False
    assert await load_cookies_to_page(page, str(auth))
    original = json.loads(auth.read_bytes())
    accounts.store.save_account(
        name=accounts.account.name, email=accounts.account.email,
        account_id=accounts.account.id, storage_state=original,
    )
    with patch("aistudio_api.infrastructure.gateway.session_auth.verify_account_identity", AsyncMock(return_value=True)):
        assert not await save_browser_cookies(
            page, str(auth), cookies=[{"name": "SID", "value": "synthetic-old-rotation"}],
        )
    assert json.loads(auth.read_bytes()) == original


def test_old_success_does_not_increment_new_import_statistics(accounts, monkeypatch):
    monkeypatch.setattr(runtime_state, "account_service", accounts.service)
    monkeypatch.setattr(runtime_state, "rotator", accounts.rotator)
    old_generation = accounts.account.auth_generation
    reimport(accounts)
    record_rotator_event(
        "success", model="gemini-test", account_id=accounts.account.id,
        auth_generation=old_generation,
    )
    assert accounts.rotator.get_account_stats(accounts.account.id).requests == 0


@pytest.mark.asyncio
async def test_legacy_generation_failure_cannot_quarantine_first_reimport(accounts, monkeypatch):
    monkeypatch.setattr(runtime_state, "account_service", accounts.service)
    monkeypatch.setattr(runtime_state, "rotator", accounts.rotator)
    assert await handle_attempt_exception(
        AuthError("synthetic legacy failure"), attempt=0,
        model_path="gemini-test", normalized_model="gemini-test",
        client=SimpleNamespace(clear_templates=MagicMock()),
        account_id=accounts.account.id, request_context_id=1, request_auth_generation=None,
    )
    assert accounts.rotator.is_account_available(accounts.account.id, model="gemini-test")
