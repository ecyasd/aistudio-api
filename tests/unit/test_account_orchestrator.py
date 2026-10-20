"""Unit tests for account_orchestrator module."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from aistudio_api.api.state import runtime_state
from aistudio_api.application import account_rotator as rotator_module
from aistudio_api.application.account_orchestrator import (
    AccountPreparationError,
    account_id_for_auth_file,
    ensure_active_account,
    record_rotator_event,
    try_switch_account,
)
from aistudio_api.application.account_rotator import AccountRotator, AccountStats
from aistudio_api.application.account_service import AccountService
from aistudio_api.config import settings
from aistudio_api.domain.errors import AuthError, SessionExpiredError
from aistudio_api.infrastructure.account.account_store import AccountMeta, AccountStore
from aistudio_api.infrastructure.browser.cdp_client import CDPPage
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.gateway.session import BrowserSession
from aistudio_api.infrastructure.gateway.session_state import SessionContext


@pytest.mark.asyncio
async def test_try_switch_account_returns_false_when_uninitialized():
    # Save original state
    orig_rotator = runtime_state.rotator
    orig_service = runtime_state.account_service
    orig_client = runtime_state.client

    try:
        runtime_state.rotator = None
        assert await try_switch_account() is False
    finally:
        runtime_state.rotator = orig_rotator
        runtime_state.account_service = orig_service
        runtime_state.client = orig_client


@pytest.mark.asyncio
async def test_try_switch_account_double_check_reuse():
    mock_rotator = MagicMock(spec=AccountRotator)
    mock_service = MagicMock(spec=AccountService)
    mock_client = MagicMock(spec=AIStudioClient)
    mock_client.session = MagicMock()

    # Active account is now "acc_2"
    active_acc = AccountMeta(
        id="acc_2",
        name="Account 2",
        email="acc2@gmail.com",
        created_at="2026-01-01",
    )
    mock_service.get_active_account = MagicMock(return_value=active_acc)

    # acc_2 is available for target model
    mock_rotator.is_account_available = MagicMock(return_value=True)

    orig_rotator = runtime_state.rotator
    orig_service = runtime_state.account_service
    orig_client = runtime_state.client

    try:
        runtime_state.rotator = mock_rotator
        runtime_state.account_service = mock_service
        runtime_state.client = mock_client

        # Call with failed_account_id = "acc_1", current is already "acc_2" and available
        reused = await try_switch_account(
            model="gemini-2.5-flash", failed_account_id="acc_1"
        )
        assert reused is True
    finally:
        runtime_state.rotator = orig_rotator
        runtime_state.account_service = orig_service
        runtime_state.client = orig_client


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", [0, 1])
async def test_ensure_active_account_early_switch(monkeypatch, attempt):
    from aistudio_api.application import account_orchestrator

    mock_rotator = MagicMock(spec=AccountRotator)
    mock_service = MagicMock(spec=AccountService)

    active_acc = AccountMeta(
        id="acc_1",
        name="Account 1",
        email="acc1@gmail.com",
        created_at="2026-01-01",
    )
    mock_service.get_active_account = MagicMock(return_value=active_acc)
    mock_service.list_accounts.return_value = [active_acc]

    # acc_1 is NOT available for this model
    mock_rotator.is_account_available = MagicMock(return_value=False)
    mock_rotator.get_account_stats.return_value = None
    switch = AsyncMock(return_value=False)
    monkeypatch.setattr(account_orchestrator, "try_switch_account", switch)

    orig_rotator = runtime_state.rotator
    orig_service = runtime_state.account_service

    try:
        runtime_state.rotator = mock_rotator
        runtime_state.account_service = mock_service

        # Every attempt must fail closed if selection cannot bind a usable account.
        with pytest.raises(HTTPException) as caught:
            await ensure_active_account(attempt=attempt, model="gemini-2.5-flash")
        assert caught.value.status_code == 503
        switch.assert_awaited_once_with(
            model="gemini-2.5-flash", failed_account_id="acc_1"
        )
        mock_rotator.is_account_available.assert_called_with(
            "acc_1", model="gemini-2.5-flash"
        )
    finally:
        runtime_state.rotator = orig_rotator
        runtime_state.account_service = orig_service


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_auth", [None, "other-account/auth.json"])
async def test_first_request_binds_registry_account_once(
    tmp_path, monkeypatch, previous_auth
):
    """Concurrent first requests bind the current registry, not startup settings."""
    monkeypatch.setattr(settings, "auth_file", previous_auth)
    session = BrowserSession(port=9222)
    target_auth = str(tmp_path / "imported_after_startup" / "auth.json")
    account = AccountMeta(
        id="acc_imported", name="Imported", email=None, created_at="2026-10-18"
    )
    service = MagicMock(spec=AccountService)
    service.get_active_account.return_value = account
    service.list_accounts.return_value = [account]
    service.get_account_auth_path.return_value = target_auth
    rotator = MagicMock(spec=AccountRotator)
    rotator.is_account_available.return_value = True
    client = MagicMock(spec=AIStudioClient)
    client.session = session

    async def prepare(auth_file):
        await asyncio.sleep(0)
        session._auth_file = auth_file
        page = MagicMock(spec=CDPPage)
        page.is_closed.return_value = False
        session.set_ready_context(
            SessionContext(
                page=page,
                snap_key="synthetic",
                auth_user="0",
                auth_file=auth_file,
                base_template={},
            )
        )

    transition = AsyncMock(side_effect=prepare)
    monkeypatch.setattr(session, "transition_to_account", transition)
    monkeypatch.setattr(runtime_state, "client", client)
    monkeypatch.setattr(runtime_state, "account_service", service)
    monkeypatch.setattr(runtime_state, "rotator", rotator)

    await asyncio.gather(
        *(ensure_active_account(0, model="gemini-test") for _ in range(4))
    )

    transition.assert_awaited_once_with(target_auth)
    assert session._auth_file == target_auth
    service.activate_account.assert_not_awaited()
    service.set_active_account.assert_not_called()
    rotator.clear_auth_lockout.assert_not_called()
    rotator.clear_cooldown.assert_not_called()


@pytest.mark.asyncio
async def test_first_request_rejects_missing_active_account_auth(monkeypatch):
    service = MagicMock(spec=AccountService)
    service.get_active_account.return_value = AccountMeta(
        id="missing", name="Missing", email=None, created_at="2026-10-18"
    )
    service.get_account_auth_path.return_value = None
    service.list_accounts.return_value = [service.get_active_account.return_value]
    rotator = MagicMock(spec=AccountRotator)
    rotator.is_account_available.return_value = True
    client = MagicMock(spec=AIStudioClient)
    client.session = MagicMock(spec=BrowserSession)
    monkeypatch.setattr(runtime_state, "client", client)
    monkeypatch.setattr(runtime_state, "account_service", service)
    monkeypatch.setattr(runtime_state, "rotator", rotator)

    with pytest.raises(RuntimeError, match="credentials are unavailable"):
        await ensure_active_account(0)

    client.session.transition_to_account.assert_not_awaited()


@pytest.mark.asyncio
async def test_cold_start_failure_keeps_attempted_account_after_registry_changes(
    tmp_path, monkeypatch
):
    service = MagicMock(spec=AccountService)
    old = AccountMeta(id="old", name="Old", email=None, created_at="2026-10-18")
    new = AccountMeta(id="new", name="New", email=None, created_at="2026-10-18")
    service.get_active_account.return_value = old
    service.list_accounts.return_value = [old, new]
    service.get_account_auth_path.return_value = tmp_path / "old" / "auth.json"
    session = MagicMock(spec=BrowserSession)
    session._auth_file = None
    session.is_ready = False
    original_error = SessionExpiredError("synthetic expired cookie")

    async def fail_after_registry_changes(*args, **kwargs):
        service.get_active_account.return_value = new
        raise original_error

    session.transition_to_account = AsyncMock(side_effect=fail_after_registry_changes)
    client = MagicMock(spec=AIStudioClient)
    client.session = session
    rotator = MagicMock(spec=AccountRotator)
    rotator.is_account_available.return_value = True
    monkeypatch.setattr(runtime_state, "client", client)
    monkeypatch.setattr(runtime_state, "account_service", service)
    monkeypatch.setattr(runtime_state, "rotator", rotator)

    with pytest.raises(AccountPreparationError) as caught:
        await ensure_active_account(0, model="gemini-test")

    assert caught.value.account_id == "old"
    assert caught.value.original_error is original_error
    assert caught.value.__cause__ is original_error
    record_rotator_event(
        "session_expired", model="gemini-test", account_id=caught.value.account_id
    )
    rotator.record_session_expired.assert_called_once_with("old", model="gemini-test")
    assert service.get_active_account().id == "new"


@pytest.mark.asyncio
@pytest.mark.parametrize("target_id", ["target", "old"])
@pytest.mark.parametrize("already_wrapped", [False, True])
async def test_switch_preparation_failure_preserves_actual_target(
    monkeypatch, target_id, already_wrapped
):
    from aistudio_api.application import account_orchestrator

    old = AccountMeta(id="old", name="Old", email=None, created_at="2026-10-18")
    target = AccountMeta(
        id=target_id, name="Target", email=None, created_at="2026-10-18"
    )
    service = MagicMock(spec=AccountService)
    service.get_active_account.return_value = old
    original_error = SessionExpiredError("synthetic expired cookie")
    failure = (
        AccountPreparationError("inner_target", original_error)
        if already_wrapped
        else original_error
    )
    service.activate_account = AsyncMock(side_effect=failure)
    client = MagicMock(spec=AIStudioClient)
    client.session = MagicMock(spec=BrowserSession)
    rotator = MagicMock(spec=AccountRotator)
    rotator.get_next_account = AsyncMock(return_value=target)
    monkeypatch.setattr(runtime_state, "client", client)
    monkeypatch.setattr(runtime_state, "account_service", service)
    monkeypatch.setattr(runtime_state, "rotator", rotator)
    monkeypatch.setattr(account_orchestrator, "_last_switch_time", 0.0)

    with pytest.raises(AccountPreparationError) as caught:
        await try_switch_account(
            model="gemini-test", failed_account_id="old", is_auth_error=True
        )

    assert caught.value.account_id == ("inner_target" if already_wrapped else target_id)
    assert caught.value.original_error is original_error
    if already_wrapped:
        assert caught.value is failure
    rotator.clear_cooldown.assert_not_called()


def test_record_rotator_event():
    mock_rotator = MagicMock(spec=AccountRotator)
    mock_service = MagicMock(spec=AccountService)
    active_acc = AccountMeta(
        id="acc_1",
        name="Account 1",
        email="acc1@gmail.com",
        created_at="2026-01-01",
    )
    mock_service.get_active_account = MagicMock(return_value=active_acc)

    orig_rotator = runtime_state.rotator
    orig_service = runtime_state.account_service

    try:
        runtime_state.rotator = mock_rotator
        runtime_state.account_service = mock_service

        record_rotator_event("success", model="gemini-2.5-flash", account_id="acc_1")
        mock_rotator.record_success.assert_called_with(
            "acc_1", model="gemini-2.5-flash"
        )

        record_rotator_event(
            "rate_limited", model="gemini-2.5-flash", account_id="acc_1"
        )
        mock_rotator.record_rate_limited.assert_called_with(
            "acc_1", model="gemini-2.5-flash"
        )

        record_rotator_event("auth_error", model="gemini-2.5-flash", account_id="acc_1")
        mock_rotator.record_auth_error.assert_called_with(
            "acc_1", model="gemini-2.5-flash"
        )

        record_rotator_event(
            "session_expired", model="gemini-2.5-flash", account_id="acc_1"
        )
        mock_rotator.record_session_expired.assert_called_with(
            "acc_1", model="gemini-2.5-flash"
        )

        record_rotator_event("error", model="gemini-2.5-flash", account_id="acc_1")
        mock_rotator.record_error.assert_called_with("acc_1", model="gemini-2.5-flash")
    finally:
        runtime_state.rotator = orig_rotator
        runtime_state.account_service = orig_service


def test_rotator_events_keep_leased_identity_after_concurrent_switch(monkeypatch):
    rotator = MagicMock(spec=AccountRotator)
    service = MagicMock(spec=AccountService)
    service.get_active_account.return_value = AccountMeta(
        id="new_active", name="New", email=None, created_at="2026-10-18"
    )
    monkeypatch.setattr(runtime_state, "rotator", rotator)
    monkeypatch.setattr(runtime_state, "account_service", service)

    record_rotator_event("auth_error", model="gemini-test", account_id="old_lease")
    record_rotator_event("success", model="gemini-test", account_id=None)

    rotator.record_auth_error.assert_called_once_with("old_lease", model="gemini-test")
    rotator.record_success.assert_not_called()
    service.get_active_account.assert_not_called()


def test_account_id_for_auth_file_never_falls_back_to_registry(tmp_path, monkeypatch):
    service = MagicMock(spec=AccountService)
    account = AccountMeta(
        id="leased", name="Leased", email=None, created_at="2026-10-18"
    )
    auth_path = tmp_path / "leased" / "auth.json"
    service.list_accounts.return_value = [account]
    service.get_account_auth_path.return_value = auth_path
    monkeypatch.setattr(runtime_state, "account_service", service)

    assert account_id_for_auth_file(str(auth_path)) == "leased"
    assert account_id_for_auth_file(str(tmp_path / "unknown.json")) is None
    assert account_id_for_auth_file(None) is None
    service.get_active_account.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_recorded", [False, True])
async def test_concurrent_switch_records_failed_lease_once(
    monkeypatch, failure_recorded
):
    from aistudio_api.application import account_orchestrator

    rotator = MagicMock(spec=AccountRotator)
    rotator.is_account_available.return_value = True
    service = MagicMock(spec=AccountService)
    service.get_active_account.return_value = AccountMeta(
        id="healthy", name="Healthy", email=None, created_at="2026-10-18"
    )
    client = MagicMock(spec=AIStudioClient)
    client.session = MagicMock(spec=BrowserSession)
    monkeypatch.setattr(runtime_state, "rotator", rotator)
    monkeypatch.setattr(runtime_state, "account_service", service)
    monkeypatch.setattr(runtime_state, "client", client)
    monkeypatch.setattr(account_orchestrator, "_last_switch_time", float("inf"))

    assert await try_switch_account(
        model="gemini-test",
        failed_account_id="expired_lease",
        is_session_expired=True,
        failure_recorded=failure_recorded,
    )

    if failure_recorded:
        rotator.record_session_expired.assert_not_called()
    else:
        rotator.record_session_expired.assert_called_once_with(
            "expired_lease", model="gemini-test"
        )
    service.activate_account.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("pre_recorded", [False, True])
async def test_old_403_context_recovers_once_through_handler_or_pre_recorded_path(
    monkeypatch, pre_recorded
):
    from aistudio_api.application import account_orchestrator
    from aistudio_api.application.api_service_gemini import handle_attempt_exception

    account = AccountMeta(
        id="same_account", name="Same", email=None, created_at="2026-10-18"
    )
    stats = AccountStats(account_id=account.id)
    stats.model_cooldowns = {"unrelated-model": float("inf")}
    rotator = MagicMock(spec=AccountRotator)
    rotator.is_account_available.side_effect = lambda _id, model=None: (
        stats.is_available(model)
    )
    rotator.record_auth_error.side_effect = lambda _id, model=None: (
        stats.record_auth_error(model)
    )
    def clear_auth_lockout(_id):
        stats.session_expired = False
        stats.auth_cooldown = 0.0

    rotator.clear_auth_lockout.side_effect = clear_auth_lockout
    rotator.get_next_account = AsyncMock(return_value=account)
    service = MagicMock(spec=AccountService)
    service.get_active_account.return_value = account
    client = MagicMock(spec=AIStudioClient)
    session = MagicMock(spec=BrowserSession)
    session.active_context = SimpleNamespace(context_id=1)
    client.session = session

    async def refresh_account(*args, **kwargs):
        await asyncio.sleep(0.01)
        session.active_context = SimpleNamespace(context_id=2)
        return account

    service.activate_account = AsyncMock(side_effect=refresh_account)
    monkeypatch.setattr(runtime_state, "rotator", rotator)
    monkeypatch.setattr(runtime_state, "account_service", service)
    monkeypatch.setattr(runtime_state, "client", client)
    monkeypatch.setattr(account_orchestrator, "_last_switch_time", 0.0)

    async def old_context_failure():
        if pre_recorded:
            record_rotator_event(
                "auth_error", model="gemini-test", account_id=account.id
            )
            return await try_switch_account(
                model="gemini-test",
                failed_account_id=account.id,
                failed_context_id=1,
                is_auth_error=True,
                failure_recorded=True,
            )
        return await handle_attempt_exception(
            AuthError("synthetic old-context 403"),
            attempt=0,
            model_path="gemini-test",
            normalized_model="gemini-test",
            client=client,
            account_id=account.id,
            request_context_id=1,
        )

    assert all(await asyncio.gather(*(old_context_failure() for _ in range(3))))
    # A slow response from the retired context may arrive after recovery ended.
    assert await old_context_failure()

    service.activate_account.assert_awaited_once()
    assert stats.auth_cooldown == 0.0
    assert stats.is_available("gemini-test")
    assert stats.model_cooldowns == {"unrelated-model": float("inf")}
    assert rotator.record_auth_error.call_count == (4 if pre_recorded else 1)


@pytest.mark.asyncio
async def test_try_switch_account_single_account_recovery():
    """单账号发生 403 鉴权错误时，自动触发当前会话与 BotGuard 强制重建。"""
    mock_rotator = MagicMock(spec=AccountRotator)
    mock_service = MagicMock(spec=AccountService)
    mock_client = MagicMock(spec=AIStudioClient)
    mock_client.session = MagicMock()
    mock_client.clear_templates = MagicMock()

    active_acc = AccountMeta(
        id="acc_1",
        name="Single Account",
        email="single@gmail.com",
        created_at="2026-01-01",
    )
    mock_service.get_active_account = MagicMock(return_value=active_acc)
    mock_service.activate_account = MagicMock(return_value=active_acc)
    # 模拟 rotator 只有唯一账号 acc_1
    from unittest.mock import AsyncMock

    mock_service.activate_account = AsyncMock(return_value=active_acc)
    mock_rotator.get_next_account = AsyncMock(return_value=active_acc)

    orig_rotator = runtime_state.rotator
    orig_service = runtime_state.account_service
    orig_client = runtime_state.client

    try:
        runtime_state.rotator = mock_rotator
        runtime_state.account_service = mock_service
        runtime_state.client = mock_client

        recovered = await try_switch_account(
            model="gemini-3.8-flash",
            failed_account_id="acc_1",
            is_auth_error=True,
        )
        assert recovered is True
        mock_client.clear_templates.assert_called_once()
        mock_service.activate_account.assert_called_once_with(
            "acc_1",
            mock_client.session,
        )
    finally:
        runtime_state.rotator = orig_rotator
        runtime_state.account_service = orig_service
        runtime_state.client = orig_client


@pytest.mark.asyncio
async def test_try_switch_account_session_expired_strictly_forbids_in_place_retry():
    """登录态失效时严禁在原账号原地重试，无备用账号时直接返回 False。"""
    from unittest.mock import AsyncMock

    mock_rotator = MagicMock(spec=AccountRotator)
    mock_service = MagicMock(spec=AccountService)
    mock_client = MagicMock(spec=AIStudioClient)
    mock_client.session = MagicMock()

    active_acc = AccountMeta(
        id="acc_1",
        name="Single Account",
        email="single@gmail.com",
        created_at="2026-01-01",
    )
    mock_service.get_active_account = MagicMock(return_value=active_acc)
    mock_service.activate_account = AsyncMock()
    # 当 is_session_expired=True 且无其他备用账号时，rotator 返回 None
    mock_rotator.get_next_account = AsyncMock(return_value=None)

    orig_rotator = runtime_state.rotator
    orig_service = runtime_state.account_service
    orig_client = runtime_state.client

    try:
        runtime_state.rotator = mock_rotator
        runtime_state.account_service = mock_service
        runtime_state.client = mock_client

        result = await try_switch_account(
            model="gemini-3.8-flash",
            failed_account_id="acc_1",
            is_auth_error=True,
            is_session_expired=True,
        )
        # 严禁原地重试，必须返回 False
        assert result is False
        mock_rotator.record_session_expired.assert_called_once_with(
            "acc_1", model="gemini-3.8-flash"
        )
        mock_service.activate_account.assert_not_called()
    finally:
        runtime_state.rotator = orig_rotator
        runtime_state.account_service = orig_service
        runtime_state.client = orig_client


@pytest.mark.asyncio
async def test_try_switch_account_session_expired_switches_to_healthy_account():
    """同 Cookie 或备用账号中某一个失效时，仅该账号失效并平滑切换到同 Cookie 的另一个正常子账号。"""
    from unittest.mock import AsyncMock

    mock_rotator = MagicMock(spec=AccountRotator)
    mock_service = MagicMock(spec=AccountService)
    mock_client = MagicMock(spec=AIStudioClient)
    mock_client.session = MagicMock()

    acc1 = AccountMeta(
        id="acc_1", name="Cookie A u/0", email="user@gmail.com", created_at="2026-01-01"
    )
    acc2 = AccountMeta(
        id="acc_2", name="Cookie A u/1", email="user@gmail.com", created_at="2026-01-01"
    )

    mock_service.get_active_account = MagicMock(return_value=acc1)
    mock_service.activate_account = AsyncMock(return_value=acc2)
    # 成功挑中同 Cookie 的备用子账号 acc_2
    mock_rotator.get_next_account = AsyncMock(return_value=acc2)

    orig_rotator = runtime_state.rotator
    orig_service = runtime_state.account_service
    orig_client = runtime_state.client

    try:
        runtime_state.rotator = mock_rotator
        runtime_state.account_service = mock_service
        runtime_state.client = mock_client

        result = await try_switch_account(
            model="gemini-3.8-flash",
            failed_account_id="acc_1",
            is_auth_error=True,
            is_session_expired=True,
        )
        assert result is True
        # 仅禁用失效的 acc_1
        mock_rotator.record_session_expired.assert_called_once_with(
            "acc_1", model="gemini-3.8-flash"
        )
        # 成功激活 acc_2
        mock_service.activate_account.assert_called_once_with(
            "acc_2",
            mock_client.session,
        )
    finally:
        runtime_state.rotator = orig_rotator
        runtime_state.account_service = orig_service
        runtime_state.client = orig_client


def test_account_stats_session_expired_permanent_isolation():
    """账号标记为 session_expired 后处于永久禁用状态，美西跨天不自动重置，ignore_auth_cooldown 也不可用。"""
    stats = AccountStats(account_id="acc_test")
    assert stats.is_available("gemini-3.8-flash")

    stats.record_session_expired(model="gemini-3.8-flash")
    assert stats.session_expired is True
    assert not stats.is_available("gemini-3.8-flash")
    # 即使 ignore_auth_cooldown 也绝对不可用
    assert not stats.is_available("gemini-3.8-flash", ignore_auth_cooldown=True)

    # 模拟跨天
    stats._check_date_reset("gemini-3.8-flash", "2099-01-01")
    assert not stats.is_available("gemini-3.8-flash")
    assert stats.session_expired is True

    # 仅手动清除冷却/重新导入时恢复
    stats.clear_cooldown()
    assert stats.session_expired is False
    assert stats.is_available("gemini-3.8-flash")


def test_account_stats_short_cooldown_sync(monkeypatch):
    """Verify short 60s cooldown is tracked accurately for monitoring."""
    # LA noon leaves room for the full daytime 300-second backoff.
    instant = datetime(2026, 10, 20, 19, tzinfo=UTC)
    clock = MagicMock(wraps=datetime)
    clock.now.side_effect = lambda tz=None: instant.astimezone(tz)
    monkeypatch.setattr(rotator_module, "ZoneInfo", lambda _: timezone(timedelta(hours=-7)))
    monkeypatch.setattr(rotator_module, "datetime", clock)
    monkeypatch.setattr(rotator_module.time, "time", instant.timestamp)
    stats = AccountStats(account_id="acc_test")
    stats.record_rate_limited(model="gemini-2.5-flash")

    assert not stats.is_available("gemini-2.5-flash")
    rem = stats.get_cooldown_remaining("gemini-2.5-flash")
    assert 0 < rem <= 60.0

    stats.record_rate_limited(model="gemini-2.5-flash")
    assert not stats.is_available("gemini-2.5-flash")
    rem2 = stats.get_cooldown_remaining("gemini-2.5-flash")
    assert 0 < rem2 <= 60.0

    stats.record_rate_limited(model="gemini-2.5-flash")
    assert not stats.is_available("gemini-2.5-flash")
    rem3 = stats.get_cooldown_remaining("gemini-2.5-flash")
    assert rem3 > 60.0


def test_safe_account_dir_path_traversal_rejection(tmp_path):
    """Verify _safe_account_dir rejects path traversal and malicious account IDs."""
    from aistudio_api.infrastructure.account.account_store import _safe_account_dir

    base = tmp_path / "accounts"
    base.mkdir()

    # Valid account IDs
    valid_dir = _safe_account_dir(base, "user_123")
    assert valid_dir == (base / "user_123").resolve()

    valid_email = _safe_account_dir(base, "test.user@gmail.com")
    assert valid_email == (base / "test.user@gmail.com").resolve()

    # Invalid / traversal IDs
    for bad_id in [
        "../etc/passwd",
        "..",
        "user/subdir",
        "user\\subdir",
        "foo/../bar",
        "",
    ]:
        with pytest.raises(ValueError):
            _safe_account_dir(base, bad_id)


def test_cleanup_ghost_accounts():
    """测试幽灵账号清理：已被删除但残留在 rotator_state 中的失效/429 账号被自动检测并彻底剔除。"""
    store = MagicMock(spec=AccountStore)
    real_acc = AccountMeta(
        id="real_acc", name="Real", email="real@gmail.com", created_at="2026-01-01"
    )
    store.list_accounts.return_value = [real_acc]

    rotator = AccountRotator(account_store=store)
    # 使用公共 API 模拟持久化恢复或历史遗留的失效/429 幽灵账号
    rotator.add_account("ghost_acc_1")
    rotator.record_rate_limited("ghost_acc_1")
    rotator.add_account("ghost_acc_2")
    rotator.record_session_expired("ghost_acc_2")

    cleaned = rotator.cleanup_ghost_accounts()
    assert set(cleaned) == {"ghost_acc_1", "ghost_acc_2"}
    assert "ghost_acc_1" not in rotator.get_all_stats()
    assert "ghost_acc_2" not in rotator.get_all_stats()
    assert "real_acc" in rotator.get_all_stats()

    # 测试单账号删除同步
    rotator.remove_account("real_acc")
    assert "real_acc" not in rotator._stats
