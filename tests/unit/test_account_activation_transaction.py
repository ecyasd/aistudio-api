"""Account selection is committed only after the browser transition succeeds."""

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aistudio_api.application.account_service import AccountService
from aistudio_api.infrastructure.account.account_store import AccountStore


@pytest.fixture
def activation_state(tmp_path):
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    registry_path = accounts_dir / "registry.json"
    registry_path.write_text(
        '{"accounts":{},"active_account_id":null}', encoding="utf-8"
    )
    store = AccountStore(accounts_dir)
    for account_id in ("old", "target"):
        store.save_account(
            name=account_id,
            email=f"{account_id}@example.com",
            account_id=account_id,
            storage_state={"cookies": [], "origins": []},
        )
    store.set_active_account("old")
    state_before = copy.deepcopy(
        [account.to_dict() for account in store.list_accounts()]
    )
    registry_before = registry_path.read_bytes()
    return SimpleNamespace(
        service=AccountService(store),
        store=store,
        registry_path=registry_path,
        registry_before=registry_before,
        state_before=state_before,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", [RuntimeError, asyncio.CancelledError])
async def test_failed_or_cancelled_activation_preserves_registry(
    activation_state, failure_type
):
    state = activation_state

    async def fail_transition(*args, **kwargs):
        assert state.service.get_active_account().id == "old"
        assert state.registry_path.read_bytes() == state.registry_before
        raise failure_type("synthetic browser transition failure")

    session = SimpleNamespace(
        transition_to_account=AsyncMock(side_effect=fail_transition)
    )

    with pytest.raises(failure_type):
        await state.service.activate_account("target", session)

    assert state.service.get_active_account().id == "old"
    assert [
        account.to_dict() for account in state.store.list_accounts()
    ] == state.state_before
    assert state.registry_path.read_bytes() == state.registry_before


@pytest.mark.asyncio
async def test_successful_activation_commits_after_preparation(activation_state):
    state = activation_state

    async def prepare_transition(*args, **kwargs):
        assert state.service.get_active_account().id == "old"
        assert state.registry_path.read_bytes() == state.registry_before

    session = SimpleNamespace(
        transition_to_account=AsyncMock(side_effect=prepare_transition)
    )

    result = await state.service.activate_account("target", session)

    session.transition_to_account.assert_awaited_once_with(
        str(state.store.get_auth_path("target")), force_refresh=True
    )
    assert result.id == "target"
    assert state.service.get_active_account().id == "target"
    assert state.registry_path.read_bytes() != state.registry_before
