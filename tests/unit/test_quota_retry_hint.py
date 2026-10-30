"""Quota responses describe actual current cooldowns, not an assumed reset day."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from aistudio_api.api import gemini as service
from aistudio_api.api.app import http_exception_handler
from aistudio_api.application import (
    account_orchestrator as orchestration,
    account_rotator as rotation,
    generation as generation_execution,
)
from aistudio_api.application.account_rotator import AccountStats
from aistudio_api.domain.errors import UsageLimitExceeded


def setup_accounts(monkeypatch, stats):
    monkeypatch.setattr(service.runtime_state, "account_service", SimpleNamespace(
        list_accounts=lambda: [SimpleNamespace(id=key) for key in stats],
    ))
    monkeypatch.setattr(service.runtime_state, "rotator", SimpleNamespace(
        get_account_stats=stats.get,
    ))


async def test_terminal_temporary_429_never_claims_midnight_reset(monkeypatch):
    monkeypatch.setattr(rotation.time, "time", lambda: 10000.0)
    stats = AccountStats(account_id="synthetic")
    setup_accounts(monkeypatch, {stats.account_id: stats})

    def record(event, *, model, **_kwargs):
        assert event == "rate_limited"
        stats.record_rate_limited(model)

    monkeypatch.setattr(service, "record_rotator_event", record)
    monkeypatch.setattr(generation_execution, "record_rotator_event", service.record_rotator_event)
    monkeypatch.setattr(service.runtime_state, "record", lambda *_args, **_kwargs: None)
    with pytest.raises(HTTPException) as caught:
        await service.handle_attempt_exception(
            UsageLimitExceeded("synthetic temporary limit"),
            attempt=service.MAX_RETRIES - 1, model_path="gemini-synthetic",
            normalized_model="gemini-synthetic", client=SimpleNamespace(),
            account_id=stats.account_id,
        )
    error = caught.value
    assert error.status_code == 429
    assert error.headers == {"Retry-After": "60"}
    payload = json.loads(service.format_sse_error(error)[6:])
    assert payload["error"]["code"] == 429
    assert "temporarily" in payload["error"]["message"]
    assert "00:00" not in payload["error"]["message"]
    assert "daily" not in payload["error"]["message"]
    monkeypatch.setattr(rotation.time, "time", lambda: 10061.0)
    orchestration.validate_account_availability("gemini-synthetic")


async def test_retry_after_uses_earliest_eligible_model_cooldown_and_preserves_stats(monkeypatch):
    monkeypatch.setattr(rotation.time, "time", lambda: 10000.0)
    stats = {
        "first": AccountStats(account_id="first", model_cooldowns={"gemini-synthetic": 10037.2}),
        "later": AccountStats(account_id="later", model_cooldowns={"gemini-synthetic": 10080}),
        "expired": AccountStats(account_id="expired", session_expired=True),
    }
    before = {key: item.to_dict() for key, item in stats.items()}
    setup_accounts(monkeypatch, stats)
    with pytest.raises(HTTPException) as caught:
        orchestration.validate_account_availability("gemini-synthetic")
    assert caught.value.headers == {"Retry-After": "38"}
    response = await http_exception_handler(Request({"type": "http", "path": "/synthetic"}), caught.value)
    assert response.status_code == 429 and response.headers["retry-after"] == "38"
    assert {key: item.to_dict() for key, item in stats.items()} == before


def test_legacy_daily_limit_hint_ignores_shorter_auth_or_model_timers(monkeypatch):
    monkeypatch.setattr(rotation.time, "time", lambda: 10000.0)
    monkeypatch.setattr(orchestration, "get_seconds_until_pacific_midnight", lambda: 36000.0)
    stats = AccountStats(
        account_id="synthetic", rate_limited_date_la=rotation.get_pacific_date_key(10000),
        auth_cooldown=10001, model_cooldowns={"gemini-synthetic": 10020},
    )
    setup_accounts(monkeypatch, {stats.account_id: stats})
    assert orchestration.quota_unavailable_error("gemini-synthetic").headers == {"Retry-After": "36000"}
