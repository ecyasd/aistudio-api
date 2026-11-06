"""Account administration must affect new requests without changing in-flight ones."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from aistudio_api.api.routes_accounts import (
    ToggleAccountRequest,
    batch_toggle_group,
    toggle_account,
)
from aistudio_api.api.state import runtime_state
from aistudio_api.application.account_orchestrator import (
    ensure_active_account,
    record_rotator_event,
)
from aistudio_api.application.account_rotator import AccountRotator
from aistudio_api.application.account_service import AccountService
from aistudio_api.config import settings
from aistudio_api.infrastructure.account.account_store import AccountStore
from aistudio_api.infrastructure.gateway.client import AIStudioClient


@pytest.fixture
async def account_pool(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "persist_rotator", False)
    store = AccountStore(accounts_dir=tmp_path / "accounts")
    first = store.save_account(
        name="First fixture",
        email="first@example.com",
        storage_state={"cookies": [{"name": "SID", "value": "fixture-first"}]},
        group="primary",
    )
    second = store.save_account(
        name="Second fixture",
        email="second@example.com",
        storage_state={"cookies": [{"name": "SID", "value": "fixture-second"}]},
        group="backup",
    )
    service = AccountService(store)
    rotator = AccountRotator(store)
    client = AIStudioClient()
    monkeypatch.setattr(runtime_state, "account_service", service)
    monkeypatch.setattr(runtime_state, "rotator", rotator)
    monkeypatch.setattr(runtime_state, "client", client)
    await service.activate_account(first.id, client.session)
    try:
        yield service, client, first, second
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("by_group", [False, True])
@pytest.mark.parametrize("model", ["gemini-fixture", "gemini-fixture-build"])
async def test_disable_active_account_selects_enabled_credentials_for_next_request(
    account_pool, by_group, model
):
    service, client, first, second = account_pool
    original_context = await client.session.ensure_ready()
    if by_group:
        await batch_toggle_group("primary", False, service)
    else:
        await toggle_account(first.id, ToggleAccountRequest(enabled=False), service)

    assert service.get_active_account().id == second.id
    await ensure_active_account(0, model=model)
    selected_context = await client.session.ensure_ready()

    assert selected_context.account_id == second.id
    assert selected_context.cookies == {"SID": "fixture-second"}
    assert selected_context is not original_context
    assert original_context.account_id == first.id
    assert original_context.cookies == {"SID": "fixture-first"}


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gemini-fixture", "gemini-fixture-build"])
async def test_all_disabled_accounts_reject_new_requests_and_clear_active_session(
    account_pool, model
):
    service, client, first, second = account_pool
    original_context = await client.session.ensure_ready()
    service.toggle_account_enabled(first.id, False)
    service.toggle_account_enabled(second.id, False)

    with pytest.raises(HTTPException) as caught:
        await ensure_active_account(0, model=model)

    assert caught.value.status_code == 503
    assert not client.session.is_ready
    assert original_context.cookies == {"SID": "fixture-first"}


@pytest.mark.asyncio
async def test_account_switch_keeps_concurrent_pg_context_and_statistics(
    account_pool, monkeypatch
):
    service, client, first, second = account_pool
    entered, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(
        client.session, "generate_snapshot", AsyncMock(return_value="fixture-proof")
    )
    pg_model = "gemini-fixture"
    build_model = "gemini-fixture-build"

    async def pending_pg():
        await ensure_active_account(0, model=pg_model)
        prepared = await client.prepare_request(prompt="fixture", model=pg_model)
        entered.set()
        await release.wait()
        assert prepared.ctx.account_id == first.id
        assert prepared.ctx.cookies == {"SID": "fixture-first"}
        record_rotator_event("success", model=pg_model)

    pending = asyncio.create_task(pending_pg())
    try:
        await entered.wait()
        await toggle_account(first.id, ToggleAccountRequest(enabled=False), service)
        await ensure_active_account(0, model=build_model)
        context = await client.session.ensure_ready()
        assert context.account_id == second.id
        record_rotator_event("rate_limited", model=build_model)
    finally:
        release.set()
        await pending

    rotator = runtime_state.rotator
    assert rotator is not None
    first_stats = rotator.get_account_stats(first.id)
    second_stats = rotator.get_account_stats(second.id)
    assert first_stats is not None
    assert second_stats is not None
    assert first_stats.get_model_daily(pg_model).success == 1
    assert first_stats.get_model_daily(build_model).rate_limited == 0
    assert second_stats.get_model_daily(build_model).rate_limited == 1
    assert second_stats.get_model_daily(pg_model).success == 0


@pytest.mark.asyncio
async def test_reimport_active_account_uses_new_credentials_without_mutating_in_flight(
    account_pool,
):
    service, client, first, _second = account_pool
    original_context = await client.session.ensure_ready()
    service.save_account_from_cookies(
        name=first.name,
        email=first.email,
        account_id=first.id,
        storage_state={"cookies": [{"name": "SID", "value": "fixture-refreshed"}]},
    )
    # The import route explicitly refreshes the current account after saving.
    await service.activate_account(first.id, client.session)
    await ensure_active_account(0, model="gemini-fixture-build")
    selected_context = await client.session.ensure_ready()

    assert selected_context.cookies == {"SID": "fixture-refreshed"}
    assert selected_context is not original_context
    assert original_context.cookies == {"SID": "fixture-first"}


@pytest.mark.asyncio
async def test_detected_tier_applies_to_next_request_without_changing_in_flight_tier(
    account_pool,
):
    service, client, first, _second = account_pool
    original_context = await client.session.ensure_ready()
    service.update_account_tier(first.id, "Pro")

    await ensure_active_account(0, model="gemini-fixture-build")
    selected_context = await client.session.ensure_ready()

    assert selected_context.account_id == first.id
    assert selected_context.tier == "Pro"
    assert original_context.tier == "Free"
