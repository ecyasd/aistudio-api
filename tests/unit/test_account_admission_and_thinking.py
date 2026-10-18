"""Offline regressions for account admission, failover state and thinking inputs."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from aistudio_api.api.schemas import GeminiGenerateContentRequest
from aistudio_api.api.state import runtime_state
from aistudio_api.application import account_orchestrator as orchestrator
from aistudio_api.application.account_rotator import (
    AccountRotator,
    get_pacific_date_key,
)
from aistudio_api.application.api_service_gemini import handle_gemini_generate_content
from aistudio_api.application.chat_service import normalize_gemini_request
from aistudio_api.config import settings

MODEL = "gemini-admission-test"


@pytest.fixture
def account_env(monkeypatch):
    monkeypatch.setattr(settings, "persist_rotator", False)
    account = SimpleNamespace(id="synthetic-a", name="A")
    accounts = [account]
    store = SimpleNamespace(
        list_accounts=lambda: accounts,
        get_account=lambda account_id: next(
            (item for item in accounts if item.id == account_id), None
        ),
    )
    rotator = AccountRotator(store)
    session = SimpleNamespace(
        is_ready=True,
        _auth_file="synthetic-a",
        active_context=SimpleNamespace(context_id=1),
        transition_to_account=AsyncMock(),
        request_scope=MagicMock(side_effect=AssertionError("Unexpected account lease")),
    )
    service = SimpleNamespace(
        list_accounts=lambda: accounts,
        get_active_account=lambda: account,
        get_account_auth_path=lambda account_id: account_id,
    )

    async def activate(account_id, _session):
        nonlocal account
        account = store.get_account(account_id)
        session._auth_file = account_id
        return account

    service.activate_account = AsyncMock(side_effect=activate)
    client = SimpleNamespace(
        session=session,
        clear_templates=MagicMock(),
        generate_content=AsyncMock(side_effect=AssertionError("Unexpected model call")),
        stream_generate_content=MagicMock(side_effect=AssertionError("Unexpected model call")),
    )
    monkeypatch.setattr(runtime_state, "client", client)
    monkeypatch.setattr(runtime_state, "account_service", service)
    monkeypatch.setattr(runtime_state, "rotator", rotator)
    monkeypatch.setattr(orchestrator, "_last_switch_time", 0.0)
    req = GeminiGenerateContentRequest.model_validate(
        {"contents": [{"parts": [{"text": "synthetic offline request"}]}]}
    )
    return SimpleNamespace(
        account=account, accounts=accounts, store=store, rotator=rotator,
        session=session, service=service, client=client, req=req,
        stats=rotator.get_account_stats(account.id),
    )


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("state,status", [("cooldown", 429), ("expired", 503)])
async def test_unavailable_accounts_rejected_before_browser_or_inference(
    account_env, stream, state, status
):
    env = account_env
    if state == "expired":
        env.stats.session_expired = True
    else:
        env.stats.model_cooldowns[MODEL] = time.time() + 3600
        env.stats.model_rate_limited_dates[MODEL] = get_pacific_date_key()
    before = env.stats.to_dict()

    with pytest.raises(HTTPException) as caught:
        await handle_gemini_generate_content(MODEL, env.req, env.client, stream=stream)

    assert caught.value.status_code == status
    if state == "expired":
        assert caught.value.detail["type"] == "upstream_auth_expired"
        assert "keep the current API key" in caught.value.detail["message"]
    assert env.stats.to_dict() == before
    env.client.generate_content.assert_not_awaited()
    env.client.stream_generate_content.assert_not_called()
    env.session.request_scope.assert_not_called()
    env.session.transition_to_account.assert_not_awaited()
    env.service.activate_account.assert_not_awaited()


@pytest.mark.parametrize("attempt", [0, 2])
async def test_retry_admission_also_rejects_newly_quarantined_account(account_env, attempt):
    account_env.stats.record_session_expired(MODEL)
    with pytest.raises(HTTPException) as caught:
        await orchestrator.ensure_active_account(attempt, MODEL)
    assert caught.value.status_code == 503
    account_env.session.transition_to_account.assert_not_awaited()


async def test_automatic_failover_preserves_target_usage_and_drip_history(account_env):
    env = account_env
    target = SimpleNamespace(id="synthetic-b", name="B")
    env.accounts.append(target)
    env.rotator.add_account(target.id)
    target_stats = env.rotator.get_account_stats(target.id)
    target_stats.model_requests[MODEL] = 19
    target_stats.model_requests_dates[MODEL] = get_pacific_date_key()
    target_stats.model_rate_limited[MODEL] = 4
    target_stats.model_drip_mode[MODEL] = True
    # The previous cooldown has elapsed; its backoff history must survive.
    target_stats.model_cooldowns[MODEL] = time.time() - 1
    target_stats.model_rate_limited_dates[MODEL] = get_pacific_date_key()
    before = target_stats.to_dict()

    assert await orchestrator.try_switch_account(MODEL, env.account.id)

    assert target_stats.to_dict() == before
    env.service.activate_account.assert_awaited_once_with(target.id, env.session)
    target_stats.record_rate_limited(MODEL)
    assert target_stats.model_rate_limited[MODEL] == 5
    assert target_stats.model_cooldowns[MODEL] - time.time() > 590


async def test_model_quarantine_during_browser_preparation_fails_closed(account_env):
    env = account_env
    env.session.is_ready = False

    async def quarantine(_auth_file):
        env.stats.record_rate_limited(MODEL)

    env.session.transition_to_account.side_effect = quarantine
    with pytest.raises(HTTPException) as caught:
        await orchestrator.ensure_active_account(0, MODEL)
    assert caught.value.status_code == 429
    assert not env.stats.is_available(MODEL)


async def test_event_loop_stats_and_persistence_keep_all_concurrent_results(
    account_env, tmp_path, monkeypatch
):
    env = account_env
    state_path = tmp_path / "synthetic-rotator.json"
    monkeypatch.setattr(settings, "persist_rotator", True)
    monkeypatch.setattr(
        "aistudio_api.application.account_rotator.resolve_rotator_state_file",
        lambda: state_path,
    )

    async def request_completed():
        await asyncio.sleep(0)
        env.rotator.record_success(env.account.id, MODEL)

    await asyncio.gather(*(request_completed() for _ in range(100)))
    pending_save = env.rotator._save_task
    env.rotator.save_state()
    if pending_save is not None:
        pending_save.cancel()
        await asyncio.gather(pending_save, return_exceptions=True)
    stored = json.loads(state_path.read_text(encoding="utf-8"))[env.account.id]
    assert stored["requests"] == stored["success"] == 100
    assert stored["model_requests"][MODEL] == 100
    assert env.rotator.get_account_stats(env.account.id).to_dict() == stored


@pytest.mark.parametrize(
    "thinking",
    [
        {"thinkingBudget": 0, "includeThoughts": False},
        {"thinkingBudget": 1024, "includeThoughts": True},
        {"includeThoughts": False},
        {"includeThoughts": True},
        {"thinking_budget": 0},
        {"include_thoughts": False},
        {"thinkingLevel": "LOW", "thinkingBudget": 1024},
    ],
)
def test_unverified_thinking_fields_are_explicitly_rejected(thinking):
    req = GeminiGenerateContentRequest.model_validate(
        {
            "contents": [{"parts": [{"text": "synthetic request"}]}],
            "generationConfig": {"thinkingConfig": thinking},
        }
    )
    with pytest.raises(ValueError, match="does not support"):
        normalize_gemini_request(req, MODEL)


@pytest.mark.parametrize("level,wire_level", [("MINIMAL", 4), ("LOW", 1), ("MEDIUM", 2), ("HIGH", 3)])
def test_verified_thinking_levels_keep_distinct_wire_values(level, wire_level):
    req = GeminiGenerateContentRequest.model_validate(
        {
            "contents": [{"parts": [{"text": "synthetic request"}]}],
            "generationConfig": {"thinkingConfig": {"thinkingLevel": level}},
        }
    )
    normalized = normalize_gemini_request(req, MODEL)
    assert normalized.generation_config_overrides["thinking_config"] == [
        1, None, None, wire_level
    ]


@pytest.mark.parametrize("level", ["UNKNOWN", True, None, 0, 99])
def test_invalid_thinking_levels_raise_validation_errors(level):
    req = GeminiGenerateContentRequest.model_validate(
        {
            "contents": [{"parts": [{"text": "synthetic request"}]}],
            "generationConfig": {"thinkingConfig": {"thinkingLevel": level}},
        }
    )
    with pytest.raises(ValueError, match="requires a valid thinkingLevel"):
        normalize_gemini_request(req, MODEL)
