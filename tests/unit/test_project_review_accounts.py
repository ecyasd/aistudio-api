"""Offline regressions for out-of-order account completion and cooldowns."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from aistudio_api.application import account_rotator as module
from aistudio_api.application.account_rotator import AccountRotator
from aistudio_api.infrastructure.account.account_store import AccountMeta, AccountStore

MODEL = "models/gemini-offline"
OTHER_MODEL = "models/gemini-other-offline"
MODEL_KEY = "gemini-offline"


@pytest.fixture
def rotator_env(monkeypatch):
    clock = [1_700_000_000.0]
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    monkeypatch.setattr(module.settings, "persist_rotator", False)
    accounts = [AccountMeta(
        id=account_id, name=account_id, email=None, created_at="offline",
    ) for account_id in ("account-a", "account-b")]
    store = MagicMock(spec=AccountStore)
    store.list_accounts.return_value = accounts
    rotator = AccountRotator(store)
    return SimpleNamespace(clock=clock, rotator=rotator)


def _failure(rotator, kind):
    if kind == "quota":
        rotator.record_rate_limited("account-a", MODEL)
    else:
        rotator.record_auth_error("account-a", MODEL, cooldown_seconds=300)


@pytest.mark.parametrize("kind", ["quota", "authentication"])
@pytest.mark.parametrize("completion_model", [MODEL, "models/抗截断/gemini-offline"])
async def test_late_inflight_success_preserves_newer_account_cooldown(
    rotator_env, kind, completion_model,
):
    env = rotator_env
    started = asyncio.Event()
    finish = asyncio.Event()

    async def already_inflight_request():
        started.set()
        await finish.wait()
        env.rotator.record_success("account-a", completion_model)

    task = asyncio.create_task(already_inflight_request())
    await started.wait()
    _failure(env.rotator, kind)
    env.clock[0] += 1
    finish.set()
    await task
    stats = env.rotator.get_account_stats("account-a")
    assert stats.requests == 2
    assert stats.success == 1
    assert stats.model_requests == {MODEL_KEY: 2}
    assert not env.rotator.is_account_available("account-a", MODEL)
    if kind == "quota":
        assert stats.rate_limited == 1
        assert stats.model_cooldowns[MODEL_KEY] > env.clock[0]
    else:
        assert stats.auth_errors == 1
        assert stats.auth_cooldown > env.clock[0]
    selected = await env.rotator.get_next_account(
        model=MODEL, current_account_id="account-a",
    )
    assert selected.id == "account-b"


async def test_late_success_on_another_model_keeps_quota_isolation(rotator_env):
    env = rotator_env
    env.rotator.record_rate_limited("account-a", MODEL)
    before = env.rotator.get_account_stats("account-a").model_cooldowns.copy()
    env.clock[0] += 1
    env.rotator.record_success("account-a", OTHER_MODEL)
    stats = env.rotator.get_account_stats("account-a")
    assert stats.model_cooldowns == before
    assert not stats.is_available(MODEL)
    assert stats.is_available(OTHER_MODEL)
    selected = await env.rotator.get_next_account(
        model=OTHER_MODEL, current_account_id="account-a",
    )
    assert selected.id == "account-a"


@pytest.mark.parametrize("kind", ["quota", "authentication"])
async def test_success_after_cooldown_expiry_cleans_markers_and_keeps_sticky_account(
    rotator_env, kind,
):
    env = rotator_env
    _failure(env.rotator, kind)
    stats = env.rotator.get_account_stats("account-a")
    deadline = stats.model_cooldowns[MODEL_KEY] if kind == "quota" else stats.auth_cooldown
    env.clock[0] = deadline + 1
    env.rotator.record_success("account-a", MODEL)
    assert stats.auth_errors == 0
    assert stats.auth_cooldown == 0
    assert MODEL_KEY not in stats.model_cooldowns
    assert MODEL_KEY not in stats.model_rate_limited_dates
    assert stats.is_available(MODEL)
    selected = await env.rotator.get_next_account(
        model=MODEL, current_account_id="account-a",
    )
    assert selected.id == "account-a"


def test_late_success_cannot_clear_expired_google_session_quarantine(rotator_env):
    env = rotator_env
    env.rotator.record_session_expired("account-a", MODEL)
    env.clock[0] += 1
    env.rotator.record_success("account-a", MODEL)
    stats = env.rotator.get_account_stats("account-a")
    assert stats.session_expired
    assert stats.auth_errors == 1
    assert stats.auth_cooldown > env.clock[0]
    assert not stats.is_available(MODEL)


@pytest.mark.parametrize("kind", ["quota", "authentication"])
def test_explicit_owner_recovery_still_allows_normal_success(rotator_env, kind):
    env = rotator_env
    _failure(env.rotator, kind)
    env.rotator.clear_cooldown("account-a", MODEL)
    env.rotator.record_success("account-a", MODEL)
    stats = env.rotator.get_account_stats("account-a")
    assert stats.success == 1
    assert stats.auth_errors == 0
    assert stats.is_available(MODEL)


def test_one_invalid_runtime_counter_cannot_drop_other_models_on_next_save(
    tmp_path, monkeypatch,
):
    from aistudio_api.api import state as state_module

    path = tmp_path / "synthetic-stats.json"
    path.write_text(json.dumps({
        "models/first": {"requests": 4, "success": 4},
        "models/corrupted-counter": {"requests": "invalid-offline-value", "success": 3},
        "models/last": {"requests": 7, "success": 6, "total_tokens": 91},
    }), encoding="utf-8")
    monkeypatch.setattr(state_module, "resolve_stats_file", lambda: path)
    monkeypatch.setattr(state_module.settings, "persist_stats", True)
    state = state_module.RuntimeState()
    assert state.model_stats["models/last"].requests == 7
    assert state.model_stats["models/last"].total_tokens == 91
    assert state.model_stats["models/corrupted-counter"].success == 3
    assert state.model_stats["models/corrupted-counter"].requests == 0
    state.save_stats()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["models/first"]["requests"] == 4
    assert saved["models/last"]["requests"] == 7
    assert saved["models/last"]["total_tokens"] == 91


def test_invalid_runtime_counter_is_not_printed_in_exception_logs(
    tmp_path, monkeypatch, caplog,
):
    from aistudio_api.api import state as state_module

    sentinel = "synthetic-private-counter-value"
    path = tmp_path / "synthetic-stats.json"
    path.write_text(json.dumps({"models/offline": {"requests": sentinel}}), encoding="utf-8")
    monkeypatch.setattr(state_module, "resolve_stats_file", lambda: path)
    monkeypatch.setattr(state_module.settings, "persist_stats", True)
    state_module.RuntimeState()
    assert sentinel not in caplog.text


def test_runtime_save_failure_preserves_disk_and_does_not_log_raw_exception(
    tmp_path, monkeypatch, caplog,
):
    from aistudio_api.api import state as state_module

    sentinel = "synthetic-private-write-failure"
    path = tmp_path / "synthetic-stats.json"
    original = '{"models/offline":{"requests":7}}'
    path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(state_module, "resolve_stats_file", lambda: path)
    monkeypatch.setattr(state_module.settings, "persist_stats", True)
    state = state_module.RuntimeState()

    def fail_write(*args):
        raise ValueError(sentinel)

    monkeypatch.setattr(state_module, "atomic_write_json", fail_write)
    state.save_stats()
    assert path.read_text(encoding="utf-8") == original
    assert sentinel not in caplog.text
    assert "ValueError" in caplog.text


@pytest.mark.parametrize(
    ("before_midnight", "utc_offset_hours", "old_date", "new_date"),
    [
        ("2026-10-20T06:59:26+00:00", -7, "2026-10-19", "2026-10-20"),
        ("2026-12-20T07:59:26+00:00", -8, "2026-12-19", "2026-12-20"),
    ],
    ids=["daylight-saving-time", "standard-time"],
)
def test_model_backoff_near_pacific_midnight_resets_on_the_new_local_date(
    monkeypatch, before_midnight, utc_offset_hours, old_date, new_date,
):
    # This reproduces the CI boundary explicitly rather than weakening daytime
    # backoff assertions: the stored deadline has a 60-second minimum, while
    # the old local-day quota bucket is retired when midnight arrives earlier.
    clock = [datetime.fromisoformat(before_midnight).timestamp()]
    fake_datetime = MagicMock(wraps=datetime)
    fake_datetime.now.side_effect = lambda tz=None: datetime.fromtimestamp(clock[0], tz)
    # Windows need not have the optional IANA tzdata package installed.
    monkeypatch.setattr(module, "ZoneInfo", lambda _: timezone(timedelta(hours=utc_offset_hours)))
    monkeypatch.setattr(module, "datetime", fake_datetime)
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    stats = module.AccountStats(account_id="offline-midnight-account")
    assert module.get_pacific_date_key() == old_date
    assert module.get_seconds_until_pacific_midnight() == 34
    for _ in range(3):
        stats.record_rate_limited(MODEL)
    assert stats.model_rate_limited[MODEL_KEY] == 3
    assert stats.model_drip_mode[MODEL_KEY]
    assert stats.get_cooldown_remaining(MODEL) == 60

    clock[0] += 33
    assert module.get_pacific_date_key() == old_date
    assert not stats.is_available(MODEL)
    assert stats.get_cooldown_remaining(MODEL) == 27

    clock[0] += 2
    assert module.get_pacific_date_key() == new_date
    assert stats.is_available(MODEL)
    assert stats.model_requests[MODEL_KEY] == 0
    assert MODEL_KEY not in stats.model_cooldowns
    assert MODEL_KEY not in stats.model_rate_limited
    assert MODEL_KEY not in stats.model_drip_mode
    assert MODEL_KEY not in stats.model_rate_limited_dates
    stats.record_rate_limited(MODEL)
    assert stats.model_requests[MODEL_KEY] == 1
    assert stats.model_rate_limited[MODEL_KEY] == 1
    assert stats.get_cooldown_remaining(MODEL) == 60
