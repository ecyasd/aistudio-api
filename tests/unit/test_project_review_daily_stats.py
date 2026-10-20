"""Daily account usage stays accurate when the first new-day attempt fails."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from aistudio_api.application import account_rotator as module
from aistudio_api.infrastructure.account.account_store import AccountMeta, AccountStore

MODEL = "models/抗截断/gemini-offline"
MODEL_KEY = "gemini-offline"
OTHER_MODEL = "gemini-other-offline"


@pytest.fixture(params=[
    ("2026-10-20T06:59:59+00:00", -7, "2026-10-19", "2026-10-20"),
    ("2026-12-20T07:59:59+00:00", -8, "2026-12-19", "2026-12-20"),
], ids=["summer", "winter"])
def daily_clock(request, monkeypatch):
    before, offset_hours, old_date, new_date = request.param
    clock = [datetime.fromisoformat(before).timestamp()]
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    # Exercise both offsets without requiring the optional Windows tzdata.
    monkeypatch.setattr(
        module, "ZoneInfo", lambda _: timezone(timedelta(hours=offset_hours)),
    )
    monkeypatch.setattr(module.settings, "persist_rotator", False)
    return clock, old_date, new_date


@pytest.mark.parametrize("event", ["record_error", "record_auth_error", "record_session_expired"])
def test_failure_after_midnight_does_not_count_yesterday_or_lose_todays_attempt(
    daily_clock, event,
):
    clock, old_date, new_date = daily_clock
    stats = module.AccountStats(account_id="offline-account")
    stats.record_success(MODEL)
    stats.record_success(MODEL)
    assert stats.model_requests_dates[MODEL_KEY] == old_date

    clock[0] += 2
    getattr(stats, event)(MODEL)
    assert stats.model_requests[MODEL_KEY] == 1
    assert stats.model_requests_dates[MODEL_KEY] == new_date
    stats.record_success(MODEL)
    assert stats.model_requests[MODEL_KEY] == 2
    assert stats.requests == 4


@pytest.mark.parametrize("event", ["record_error", "record_auth_error", "record_session_expired"])
def test_first_failure_for_new_model_has_a_date_and_resets_next_day(daily_clock, event):
    clock, old_date, new_date = daily_clock
    stats = module.AccountStats(account_id="offline-account")
    getattr(stats, event)(MODEL)
    assert stats.model_requests_dates[MODEL_KEY] == old_date
    clock[0] += 2
    getattr(stats, event)(MODEL)
    assert stats.model_requests[MODEL_KEY] == 1
    assert stats.model_requests_dates[MODEL_KEY] == new_date
    assert stats.requests == 2


def test_account_statistics_refresh_inactive_models_at_midnight(daily_clock):
    clock, old_date, new_date = daily_clock
    store = MagicMock(spec=AccountStore)
    store.list_accounts.return_value = [AccountMeta(
        id="offline-account", name="Offline", email=None, created_at="offline",
    )]
    rotator = module.AccountRotator(store)
    rotator.record_success("offline-account", MODEL)
    rotator.record_success("offline-account", OTHER_MODEL)
    stats = rotator.get_account_stats("offline-account")
    assert stats is not None
    stats.model_rate_limited_dates[OTHER_MODEL] = old_date
    stats.model_cooldowns[OTHER_MODEL] = clock[0] + 60
    stats.model_drip_mode[OTHER_MODEL] = True
    stats.model_rate_limited[OTHER_MODEL] = 3
    stats.session_expired = True

    clock[0] += 2
    snapshot = rotator.get_all_stats()["offline-account"]
    assert snapshot["model_requests"] == {MODEL_KEY: 0, OTHER_MODEL: 0}
    assert stats.model_requests_dates == {MODEL_KEY: new_date, OTHER_MODEL: new_date}
    assert snapshot["model_rate_limited"] == {}
    assert snapshot["model_drip_mode"] == {}
    assert snapshot["session_expired"]
    assert snapshot["requests"] == 2

    rotator.record_error("offline-account", MODEL)
    assert rotator.get_all_stats()["offline-account"]["model_requests"] == {
        MODEL_KEY: 1, OTHER_MODEL: 0,
    }
