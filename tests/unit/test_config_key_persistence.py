"""API calling keys survive stale settings, concurrent saves and cache reloads."""
from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
import yaml

from aistudio_api.api.routes_system import (
    ConfigYamlUpdateRequest,
    CreateApiKeyRequest,
    UpdateApiKeyRequest,
    create_api_key,
    delete_api_key,
    update_api_key_name,
    update_config_yaml,
)
from aistudio_api.infrastructure.gateway.model_defaults import (
    get_configured_api_keys,
    invalidate_config_cache,
    update_configured_logging_settings,
)
from aistudio_api.infrastructure.utils.config_store import mutate_config


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("api_keys: [dummy-existing-client-key]\nmodel_defaults: {}\n")
    monkeypatch.setenv("AISTUDIO_CONFIG_FILE", str(path))
    invalidate_config_cache()
    yield path
    invalidate_config_cache()


@pytest.mark.asyncio
@pytest.mark.parametrize("submitted_keys", [None, [], ["dummy-obsolete-client-key"]])
async def test_model_settings_preserve_existing_key(config_path, submitted_keys):
    document = {"model_defaults": {"drop_unsupported_params": True}}
    if submitted_keys is not None:
        document["api_keys"] = submitted_keys
    await update_config_yaml(
        ConfigYamlUpdateRequest(yaml_content=yaml.safe_dump(document))
    )
    assert get_configured_api_keys() == frozenset({"dummy-existing-client-key"})
    assert yaml.safe_load(config_path.read_text())["model_defaults"] == {
        "drop_unsupported_params": True
    }


@pytest.mark.asyncio
async def test_stale_save_neither_removes_new_key_nor_restores_deleted_key(config_path):
    stale = config_path.read_text()
    await create_api_key(CreateApiKeyRequest(name="new", key="dummy-new-client-key"))
    await delete_api_key("dummy-existing-client-key")
    await update_config_yaml(ConfigYamlUpdateRequest(yaml_content=stale))
    assert get_configured_api_keys() == frozenset({"dummy-new-client-key"})
    invalidate_config_cache()
    assert get_configured_api_keys() == frozenset({"dummy-new-client-key"})


@pytest.mark.asyncio
async def test_create_preserves_legacy_string_keys(config_path):
    config_path.write_text("api_keys: dummy-first-key,dummy-second-key\n")
    invalidate_config_cache()
    await create_api_key(CreateApiKeyRequest(name="new", key="dummy-third-key"))
    assert get_configured_api_keys() == frozenset(
        {"dummy-first-key", "dummy-second-key", "dummy-third-key"}
    )


@pytest.mark.asyncio
async def test_stale_save_keeps_latest_key_metadata(config_path):
    stale = config_path.read_text()
    await update_api_key_name(
        "dummy-existing-client-key", UpdateApiKeyRequest(name="Renamed key")
    )
    expected = yaml.safe_load(config_path.read_text())["api_keys"]
    await update_config_yaml(ConfigYamlUpdateRequest(yaml_content=stale))
    assert yaml.safe_load(config_path.read_text())["api_keys"] == expected


@pytest.mark.asyncio
async def test_model_config_cannot_add_keys_when_none_are_configured(config_path):
    config_path.write_text("model_defaults: {}\n")
    invalidate_config_cache()
    await update_config_yaml(ConfigYamlUpdateRequest(
        yaml_content="api_keys: [dummy-injected-key]\nmodel_defaults: {}\n"
    ))
    assert "api_keys" not in yaml.safe_load(config_path.read_text())
    assert get_configured_api_keys() == frozenset()


def test_api_key_cache_detects_replacement_with_preserved_timestamp(config_path):
    original = config_path.stat()
    assert get_configured_api_keys() == frozenset({"dummy-existing-client-key"})
    replacement = config_path.with_name("replacement.yaml")
    replacement.write_text("api_keys: [dummy-replaced-client-key]\n")
    os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
    os.replace(replacement, config_path)
    assert config_path.stat().st_mtime_ns == original.st_mtime_ns
    assert get_configured_api_keys() == frozenset({"dummy-replaced-client-key"})


def test_concurrent_key_and_model_saves_do_not_lose_keys(config_path):
    stale = config_path.read_text()

    def create(index):
        asyncio.run(create_api_key(
            CreateApiKeyRequest(name=f"key-{index}", key=f"dummy-concurrent-{index}")
        ))

    def save(_):
        asyncio.run(update_config_yaml(ConfigYamlUpdateRequest(yaml_content=stale)))

    with ThreadPoolExecutor(max_workers=6) as executor:
        tasks = []
        for index in range(8):
            tasks.append(executor.submit(create, index))
            tasks.append(executor.submit(save, index))
        for task in tasks:
            task.result()
    assert get_configured_api_keys() == frozenset(
        {"dummy-existing-client-key", *(f"dummy-concurrent-{i}" for i in range(8))}
    )


def test_failed_atomic_write_retains_original_key_file(config_path, monkeypatch):
    original = config_path.read_bytes()

    def fail_replace(*_):
        raise OSError("simulated publication failure")

    monkeypatch.setattr(
        "aistudio_api.infrastructure.utils.config_store.os.replace", fail_replace
    )
    with pytest.raises(OSError, match="simulated publication failure"):
        mutate_config(config_path, lambda current: {"api_keys": ["dummy-other-key"]})
    assert config_path.read_bytes() == original
    assert list(config_path.parent.glob(".config.yaml.*")) == []


def test_logging_save_preserves_keys_and_private_mode(config_path):
    update_configured_logging_settings(level="WARNING")
    assert get_configured_api_keys() == frozenset({"dummy-existing-client-key"})
    assert yaml.safe_load(config_path.read_text())["logging"]["level"] == "WARNING"
    if os.name == "posix":
        assert config_path.stat().st_mode & 0o777 == 0o600
