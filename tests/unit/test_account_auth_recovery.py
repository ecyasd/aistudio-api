"""Account recovery keeps client keys, history, and model quotas intact."""

from __future__ import annotations

import copy
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from aistudio_api.api.routes_accounts import (
    ImportBundleRequest,
    ImportCookiesRequest,
    ProbeAndImportRequest,
    activate_account,
    import_bundle,
    import_cookies,
    probe_and_import,
)
from aistudio_api.api.state import RuntimeState
from aistudio_api.application.account_rotator import (
    AccountRotator,
    get_pacific_date_key,
)
from aistudio_api.application.account_service import AccountService
from aistudio_api.config import settings
from aistudio_api.infrastructure.account.account_store import AccountStore
from aistudio_api.infrastructure.gateway.model_defaults import (
    get_configured_api_keys,
    invalidate_config_cache,
)


@pytest.fixture
def recovery_state(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "persist_stats", False)
    monkeypatch.setattr(settings, "persist_rotator", False)
    config_path = tmp_path / "config.yaml"
    config_bytes = b"api_keys:\n  - dummy-client-key-preserved-during-recovery\n"
    config_path.write_bytes(config_bytes)
    monkeypatch.setenv("AISTUDIO_CONFIG_FILE", str(config_path))
    invalidate_config_cache()
    keys_before = get_configured_api_keys()

    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    # Never search for or migrate any legacy credentials outside this fixture.
    (accounts_dir / "registry.json").write_text(
        '{"accounts":{},"active_account_id":null}', encoding="utf-8"
    )
    store = AccountStore(accounts_dir)
    account = store.save_account(
        name="Existing test account",
        email="test-account@example.com",
        storage_state={"cookies": [], "origins": []},
        account_id="acc_recovery",
        auth_user="0",
    )
    service = AccountService(store)
    rotator = AccountRotator(store)
    rotator.record_session_expired(account.id)
    stats = rotator.get_account_stats(account.id)
    assert stats is not None
    stats.requests = 12
    stats.success = 8
    stats.errors = 4
    stats.auth_errors = 3
    stats.rate_limited = 2
    stats.last_rate_limited = time.time() - 60
    stats.model_requests = {"gemini-limited": 7}
    stats.model_requests_dates = {"gemini-limited": get_pacific_date_key()}
    stats.model_cooldowns = {"gemini-limited": time.time() + 3600}
    stats.model_rate_limited = {"gemini-limited": 2}
    stats.model_rate_limited_dates = {"gemini-limited": get_pacific_date_key()}
    stats.model_drip_mode = {"gemini-limited": True}
    before = copy.deepcopy(stats.to_dict())
    runtime = RuntimeState(account_service=service, rotator=rotator)
    fixture = SimpleNamespace(
        store=store,
        service=service,
        rotator=rotator,
        account=account,
        stats=stats,
        before=before,
        runtime=runtime,
    )
    yield fixture
    assert config_path.read_bytes() == config_bytes
    invalidate_config_cache()
    assert get_configured_api_keys() == keys_before
    invalidate_config_cache()


def assert_auth_recovered(state):
    expected = copy.deepcopy(state.before)
    expected["session_expired"] = False
    expected["auth_cooldown"] = 0.0
    assert state.stats.to_dict() == expected
    assert state.rotator.is_account_available(state.account.id)
    assert not state.rotator.is_account_available(
        state.account.id, model="gemini-limited"
    )


def test_clear_auth_lockout_preserves_history_and_model_quota(recovery_state):
    state = recovery_state
    state.rotator.clear_auth_lockout(state.account.id)
    assert_auth_recovered(state)


def test_recovered_auth_state_survives_reload(recovery_state, tmp_path, monkeypatch):
    state = recovery_state
    monkeypatch.setattr(settings, "persist_rotator", True)
    monkeypatch.setenv("AISTUDIO_ROTATOR_STATE_FILE", str(tmp_path / "rotation.json"))
    state.rotator.clear_auth_lockout(state.account.id)

    restored = AccountRotator(state.store).get_account_stats(state.account.id)
    assert restored is not None
    assert restored.to_dict() == state.stats.to_dict()
    assert_auth_recovered(state)


@pytest.mark.asyncio
async def test_successful_activation_recovers_only_auth_lockout(recovery_state):
    state = recovery_state
    browser = SimpleNamespace(transition_to_account=AsyncMock())
    state.runtime.client = SimpleNamespace(_session=browser)

    result = await activate_account(
        state.account.id,
        account_service=state.service,
        runtime_state=state.runtime,
    )

    assert result.id == state.account.id
    browser.transition_to_account.assert_awaited_once()
    assert_auth_recovered(state)


@pytest.mark.asyncio
async def test_failed_activation_keeps_auth_lockout(recovery_state):
    state = recovery_state
    browser = SimpleNamespace(
        transition_to_account=AsyncMock(
            side_effect=RuntimeError("Test browser failure")
        )
    )
    state.runtime.client = SimpleNamespace(_session=browser)

    with pytest.raises(RuntimeError, match="Test browser failure"):
        await activate_account(
            state.account.id,
            account_service=state.service,
            runtime_state=state.runtime,
        )

    assert state.stats.to_dict() == state.before
    assert not state.rotator.is_account_available(state.account.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("import_mode", ["single", "bundle"])
async def test_reimport_recovers_existing_account_without_rotating_key(
    recovery_state, import_mode
):
    state = recovery_state
    cookies = "SID=dummy-test-sid; SAPISID=dummy-test-sapisid"
    if import_mode == "single":
        result = await import_cookies(
            ImportCookiesRequest(
                cookies=cookies,
                account_id=state.account.id,
                name=state.account.name,
                email=state.account.email,
                auth_user=state.account.auth_user,
            ),
            account_service=state.service,
            runtime_state=state.runtime,
        )
        assert result.account_id == state.account.id
    else:
        result = await import_bundle(
            ImportBundleRequest(
                content=json.dumps(
                    {
                        "accounts": [
                            {
                                "name": state.account.name,
                                "email": state.account.email,
                                "cookies": cookies,
                                "auth_user": state.account.auth_user,
                            }
                        ]
                    }
                )
            ),
            account_service=state.service,
            runtime_state=state.runtime,
        )
        assert [a.id for a in result.accounts] == [state.account.id]

    assert [a.id for a in state.store.list_accounts()] == [state.account.id]
    assert_auth_recovered(state)


@pytest.mark.asyncio
async def test_invalid_cookie_import_does_not_clear_auth_lockout(recovery_state):
    state = recovery_state
    with pytest.raises(HTTPException) as exc:
        await import_cookies(
            ImportCookiesRequest(cookies="", account_id=state.account.id),
            account_service=state.service,
            runtime_state=state.runtime,
        )
    assert exc.value.status_code == 400
    assert state.stats.to_dict() == state.before


@pytest.mark.asyncio
async def test_probe_import_registers_new_accounts_without_unlocking_old(
    recovery_state, monkeypatch
):
    state = recovery_state
    probe = AsyncMock(return_value=[{"auth_user": "0"}, {"auth_user": "1"}])
    monkeypatch.setattr(
        "aistudio_api.infrastructure.account.cookie_parser.probe_google_accounts_infinite",
        probe,
    )
    result = await probe_and_import(
        ProbeAndImportRequest(cookies="SID=dummy-test-sid; SAPISID=dummy-test-sapisid"),
        account_service=state.service,
        runtime_state=state.runtime,
    )

    assert result.imported_count == 2
    assert {a.auth_user for a in result.accounts} == {"0", "1"}
    for account in result.accounts:
        assert account.id != state.account.id
        assert state.rotator.is_account_available(account.id)
    assert state.stats.to_dict() == state.before
