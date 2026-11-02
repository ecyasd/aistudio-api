"""Offline management race and logging-state probes; synthetic data only."""

import asyncio
from types import SimpleNamespace

import pytest

from aistudio_api.api import routes_accounts, routes_system
from aistudio_api.application.account_service import AccountService
from aistudio_api.infrastructure.account.account_store import AccountStore


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replacement", ["logging: {level: INFO, dump_to_file: false}", "model_defaults: {}"]
)
async def test_yaml_removing_dump_field_restores_runtime_default(
    tmp_path, monkeypatch, replacement
):
    from aistudio_api.config import settings

    monkeypatch.setattr(settings, "dump_requests", False)
    monkeypatch.setattr(settings, "dump_to_file", False)
    from aistudio_api.infrastructure.gateway import model_defaults
    from aistudio_api.infrastructure.utils import logger

    config = tmp_path / "config.yaml"
    config.write_text(
        "logging: {level: INFO, dump_requests: false, dump_to_file: false}\n",
        encoding="utf8",
    )
    monkeypatch.setattr(routes_system, "_resolve_config_path", lambda _: config)
    monkeypatch.setattr(model_defaults, "_resolve_config_path", lambda _: config)
    monkeypatch.setattr(logger, "_dump_requests_override", None)
    monkeypatch.setattr(logger, "_dump_to_file_override", None)
    monkeypatch.setattr(logger, "_current_level", "INFO")
    baseline = config.read_text(encoding="utf8")
    await routes_system.update_config_yaml(
        routes_system.ConfigYamlUpdateRequest(
            yaml_content="logging: {level: INFO, dump_requests: true, dump_to_file: false}",
            baseline_yaml_content=baseline,
        )
    )
    assert logger.is_dump_requests_enabled()
    baseline = config.read_text(encoding="utf8")
    await routes_system.update_config_yaml(
        routes_system.ConfigYamlUpdateRequest(
            yaml_content=replacement, baseline_yaml_content=baseline
        )
    )
    saved = model_defaults.get_configured_logging_settings()
    assert saved["dump_requests"] is False
    assert logger.is_dump_requests_enabled() is False


@pytest.mark.asyncio
async def test_old_email_detection_preserves_new_import_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "aistudio_api.infrastructure.account.account_store._resolve_legacy_auth_file",
        lambda: None,
    )
    store = AccountStore(tmp_path / "accounts")
    store.save_account(
        name="old",
        email="old@example.invalid",
        storage_state={"cookies": []},
        account_id="synthetic",
    )
    service = AccountService(store)
    entered, finish, importer_entered = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    transition_lock = asyncio.Lock()

    class Browser:
        async def detect_account_email(self, auth_path):
            async with transition_lock:
                entered.set()
                await finish.wait()
                return "old@example.invalid"

        async def import_cookies(self, cookies, **kwargs):
            importer_entered.set()
            async with transition_lock:
                return 1

    state = SimpleNamespace(client=SimpleNamespace(_session=Browser()), rotator=None)
    detecting = asyncio.create_task(
        routes_accounts.detect_account_email(
            "synthetic", account_service=service, runtime_state=state
        )
    )
    await entered.wait()
    old_generation = store.get_account("synthetic").auth_generation
    importing = asyncio.create_task(
        routes_accounts.import_cookies(
            routes_accounts.ImportCookiesRequest(
                cookies="SID=synthetic-new",
                email="new@example.invalid",
                account_id="synthetic",
            ),
            account_service=service,
            runtime_state=state,
        )
    )
    await importer_entered.wait()
    assert store.get_account("synthetic").email == "new@example.invalid"
    replacement_generation = store.get_account("synthetic").auth_generation
    assert old_generation != replacement_generation
    finish.set()
    await asyncio.gather(detecting, importing)
    current = store.get_account("synthetic")
    assert current.auth_generation == replacement_generation
    assert current.email == "new@example.invalid"
