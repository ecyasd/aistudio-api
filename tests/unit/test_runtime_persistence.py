"""Unit tests for runtime statistics and account rotator state persistence."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from aistudio_api.application.account_rotator import AccountRotator
from aistudio_api.application.runtime import RuntimeState
from aistudio_api.infrastructure.account.account_store import AccountMeta, AccountStore


def test_model_stats_persistence(tmp_path, monkeypatch):
    stats_file = tmp_path / "data" / "stats.json"
    monkeypatch.setenv("AISTUDIO_STATS_FILE", str(stats_file))
    monkeypatch.setenv("AISTUDIO_PERSIST_STATS", "1")

    state1 = RuntimeState()
    state1.record(
        "gemini-3.7-flash",
        "success",
        {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    )
    state1.record("gemini-3.7-flash", "rate_limited")
    state1.record("gemini-3.1-flash-image-preview", "errors")

    assert stats_file.is_file()
    saved = json.loads(stats_file.read_text(encoding="utf-8"))
    assert saved["gemini-3.7-flash"]["requests"] == 2
    assert saved["gemini-3.7-flash"]["success"] == 1
    assert saved["gemini-3.7-flash"]["rate_limited"] == 1
    assert saved["gemini-3.7-flash"]["prompt_tokens"] == 10
    assert saved["gemini-3.7-flash"]["total_tokens"] == 30
    assert saved["gemini-3.1-flash-image-preview"]["errors"] == 1

    # Simulate restart by creating a new RuntimeState instance
    state2 = RuntimeState()
    assert state2.model_stats["gemini-3.7-flash"].requests == 2
    assert state2.model_stats["gemini-3.7-flash"].success == 1
    assert state2.model_stats["gemini-3.7-flash"].rate_limited == 1
    assert state2.model_stats["gemini-3.7-flash"].total_tokens == 30
    assert state2.model_stats["gemini-3.1-flash-image-preview"].errors == 1


def test_account_rotator_state_persistence(tmp_path, monkeypatch):
    rotator_file = tmp_path / "data" / "rotator_state.json"
    monkeypatch.setenv("AISTUDIO_ROTATOR_STATE_FILE", str(rotator_file))
    monkeypatch.setenv("AISTUDIO_PERSIST_ROTATOR", "1")

    mock_store = MagicMock(spec=AccountStore)
    mock_store.list_accounts.return_value = [
        AccountMeta(
            id="acc_test1",
            name="Account 1",
            email="acc1@gmail.com",
            created_at="2026-09-21 00:00:00",
        ),
        AccountMeta(
            id="acc_test2",
            name="Account 2",
            email="acc2@gmail.com",
            created_at="2026-09-21 00:00:00",
        ),
    ]

    rotator1 = AccountRotator(mock_store)
    rotator1.record_success("acc_test1", "gemini-3.7-flash")
    rotator1.record_rate_limited("acc_test1", "gemini-3.7-flash")
    rotator1.record_rate_limited("acc_test1", "gemini-3.7-flash")
    rotator1.record_rate_limited(
        "acc_test1", "gemini-3.7-flash"
    )  # triggers 3rd 429 -> locks to midnight
    rotator1.record_auth_error("acc_test2", "gemini-3.7-flash", cooldown_seconds=600.0)

    assert rotator_file.is_file()
    saved = json.loads(rotator_file.read_text(encoding="utf-8"))
    assert "acc_test1" in saved
    assert saved["acc_test1"]["requests"] == 4
    assert saved["acc_test1"]["rate_limited"] == 3
    assert "gemini-3.7-flash" in saved["acc_test1"]["model_rate_limited_dates"]
    assert saved["acc_test2"]["auth_errors"] == 1
    assert saved["acc_test2"]["auth_cooldown"] > 0

    # Simulate restart by creating a new AccountRotator instance
    rotator2 = AccountRotator(mock_store)
    all_stats = rotator2.get_all_stats()
    assert all_stats["acc_test1"]["requests"] == 4
    assert all_stats["acc_test1"]["rate_limited"] == 3
    assert not rotator2._stats["acc_test1"].is_available("gemini-3.7-flash")
    assert not rotator2._stats["acc_test2"].is_available("gemini-3.7-flash")


def test_resolve_config_file_initializes_from_template_when_missing(
    tmp_path, monkeypatch
):
    from aistudio_api import config
    from aistudio_api.config import resolve_config_file

    target_config = tmp_path / "data" / "config.yaml"
    assert not target_config.exists()
    template = tmp_path / "synthetic-template.yaml"
    template.write_text("api_keys: []\nmodel_defaults: {}\n", encoding="utf-8")
    monkeypatch.setattr(config, "_TEMPLATE_CONFIG_PATHS", (template,))

    monkeypatch.setenv("AISTUDIO_CONFIG_FILE", str(target_config))
    resolved = resolve_config_file()

    assert resolved == target_config
    assert target_config.is_file()
    content = target_config.read_text(encoding="utf-8")
    assert "model_defaults" in content or "api_keys" in content


def test_resolve_config_file_never_overwrites_existing(tmp_path, monkeypatch):
    from aistudio_api.config import resolve_config_file

    target_config = tmp_path / "data" / "config.yaml"
    target_config.parent.mkdir(parents=True, exist_ok=True)
    target_config.write_text("custom_key: preserved_value\n", encoding="utf-8")

    monkeypatch.setenv("AISTUDIO_CONFIG_FILE", str(target_config))
    resolved = resolve_config_file()

    assert resolved == target_config
    assert target_config.read_text(encoding="utf-8") == "custom_key: preserved_value\n"


@pytest.mark.parametrize("body_failure", [False, True])
@pytest.mark.parametrize("close_failure", [False, True])
async def test_lifespan_flushes_pending_statistics_before_discarding_state(
    tmp_path, monkeypatch, body_failure, close_failure
):
    from aistudio_api.api import app as application
    from aistudio_api.application import account_rotator
    from aistudio_api.config import settings
    from aistudio_api.infrastructure.account import account_store
    from aistudio_api.infrastructure.browser import browser_engine

    stats_path, rotator_path = tmp_path / "stats.json", tmp_path / "rotation.json"
    monkeypatch.setenv("AISTUDIO_STATS_FILE", str(stats_path))
    monkeypatch.setenv("AISTUDIO_ROTATOR_STATE_FILE", str(rotator_path))
    monkeypatch.setattr(settings, "persist_stats", True)
    monkeypatch.setattr(settings, "persist_rotator", True)
    monkeypatch.setattr(settings, "browser_idle_timeout", 0)
    state = RuntimeState()
    store = MagicMock(spec=AccountStore)
    store.list_accounts.return_value = [SimpleNamespace(id="acc_12345678")]
    rotator = AccountRotator(store)
    client = SimpleNamespace(close=AsyncMock(
        side_effect=RuntimeError("synthetic close failure") if close_failure else None,
    ))
    monkeypatch.setattr(application, "runtime_state", state)
    monkeypatch.setattr(application, "AIStudioClient", lambda **_kwargs: client)
    monkeypatch.setattr(account_store, "AccountStore", lambda: store)
    monkeypatch.setattr(account_rotator, "init_rotator", lambda _store: rotator)
    monkeypatch.setattr(browser_engine, "install_process_cleanup_handlers", lambda: None)
    pending = []
    try:
        async with application.lifespan(application.app):
            state.record("gemini-synthetic", "success", {"total_tokens": 1})
            state.record("gemini-synthetic", "success", {"total_tokens": 2})
            rotator.record_success("acc_12345678", "gemini-synthetic")
            rotator.record_success("acc_12345678", "gemini-synthetic")
            pending = [state._save_task, rotator._save_task]
            assert all(task is not None and not task.done() for task in pending)
            if body_failure:
                raise RuntimeError("synthetic lifespan body failure")
    except RuntimeError as exc:
        assert body_failure and str(exc) == "synthetic lifespan body failure"
    try:
        assert all(task.done() for task in pending)
        assert state._save_task is rotator._save_task is None
        assert state.client is None and state.rotator is None
        saved = json.loads(stats_path.read_text())
        assert saved["gemini-synthetic"]["requests"] == 2
        assert saved["gemini-synthetic"]["total_tokens"] == 3
        assert json.loads(rotator_path.read_text())["acc_12345678"]["requests"] == 2
    finally:
        # Even a red run must not leave timers modifying another test's files.
        for task in pending:
            if task is not None and not task.done():
                task.cancel()
        import asyncio

        await asyncio.gather(*pending, return_exceptions=True)


async def test_shutdown_attempts_both_saves_and_releases_references_if_one_fails(monkeypatch):
    from aistudio_api.api import app as application
    from aistudio_api.application import account_rotator
    from aistudio_api.config import settings
    from aistudio_api.infrastructure.account import account_store
    from aistudio_api.infrastructure.browser import browser_engine

    state = SimpleNamespace(browser_port=19222, flush_stats=AsyncMock(side_effect=OSError("synthetic disk failure")))
    rotator = SimpleNamespace(flush_state=AsyncMock())
    client = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(application, "runtime_state", state)
    monkeypatch.setattr(application, "AIStudioClient", lambda **_kwargs: client)
    monkeypatch.setattr(account_store, "AccountStore", lambda: SimpleNamespace(list_accounts=list))
    monkeypatch.setattr(account_rotator, "init_rotator", lambda _store: rotator)
    monkeypatch.setattr(browser_engine, "install_process_cleanup_handlers", lambda: None)
    monkeypatch.setattr(settings, "browser_idle_timeout", 0)
    with pytest.raises(OSError, match="synthetic disk failure"):
        async with application.lifespan(application.app):
            pass
    client.close.assert_awaited_once()
    rotator.flush_state.assert_awaited_once()
    assert state.client is state.account_service is state.rotator is None
