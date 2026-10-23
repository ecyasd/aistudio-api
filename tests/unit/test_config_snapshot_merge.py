"""Stale model drafts cannot restore settings saved by a separate card/tab."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import yaml
from fastapi import HTTPException

from aistudio_api.api import routes_system as routes
from aistudio_api.infrastructure.gateway import model_defaults
from aistudio_api.infrastructure.utils import logger
from aistudio_api.infrastructure.utils.config_store import (
    ConfigConflictError,
    mutate_config,
    save_model_config,
)


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(
        "api_keys: [synthetic-key]\nlogging:\n  level: INFO\n"
        "model_defaults:\n  drop_unsupported_params: false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AISTUDIO_CONFIG_FILE", str(path))
    for name in ("set_log_level", "set_dump_requests", "set_dump_to_file", "set_dump_dir"):
        monkeypatch.setattr(logger, name, MagicMock())
    model_defaults.invalidate_config_cache()
    yield path
    model_defaults.invalidate_config_cache()


async def test_model_save_before_logging_refresh_preserves_latest_settings(
    config_path, monkeypatch,
):
    baseline = config_path.read_text(encoding="utf-8")
    document = yaml.safe_load(baseline)
    document["model_defaults"]["drop_unsupported_params"] = True
    model_defaults.update_configured_logging_settings(level="ERROR")
    mutate_config(config_path, lambda current: {**current, "api_keys": ["synthetic-new-key"]})
    model_defaults.invalidate_config_cache()
    setter = MagicMock()
    monkeypatch.setattr(logger, "set_log_level", setter)

    await routes.update_config_yaml(routes.ConfigYamlUpdateRequest(
        yaml_content=yaml.safe_dump(document), baseline_yaml_content=baseline,
    ))

    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["model_defaults"]["drop_unsupported_params"] is True
    assert saved["logging"]["level"] == "ERROR"
    assert model_defaults.get_configured_api_keys() == frozenset({"synthetic-new-key"})
    setter.assert_not_called()


async def test_conflicting_yaml_logging_edit_fails_without_writing_or_applying(
    config_path, monkeypatch,
):
    baseline = config_path.read_text(encoding="utf-8")
    document = yaml.safe_load(baseline)
    document["logging"]["level"] = "DEBUG"
    model_defaults.update_configured_logging_settings(level="ERROR")
    before = config_path.read_bytes()
    setter = MagicMock()
    monkeypatch.setattr(logger, "set_log_level", setter)

    with pytest.raises(HTTPException) as error:
        await routes.update_config_yaml(routes.ConfigYamlUpdateRequest(
            yaml_content=yaml.safe_dump(document), baseline_yaml_content=baseline,
        ))
    assert error.value.status_code == 409
    assert config_path.read_bytes() == before
    setter.assert_not_called()


@pytest.mark.parametrize("current", [{"level": "INFO"}, {"level": "WARNING"}])
async def test_explicit_yaml_edit_allows_unchanged_or_identical_current_value(
    config_path, monkeypatch, current,
):
    baseline = config_path.read_text(encoding="utf-8")
    document = yaml.safe_load(baseline)
    document["logging"]["level"] = "warning"
    if current["level"] == "WARNING":
        model_defaults.update_configured_logging_settings(level="WARNING")
    setter = MagicMock()
    monkeypatch.setattr(logger, "set_log_level", setter)
    await routes.update_config_yaml(routes.ConfigYamlUpdateRequest(
        yaml_content=yaml.safe_dump(document), baseline_yaml_content=baseline,
    ))
    assert yaml.safe_load(config_path.read_text())["logging"]["level"] == "WARNING"
    setter.assert_called_once_with("WARNING")


def test_merge_preserves_unedited_unknown_fields_and_deletes_explicit_fields(config_path):
    baseline = yaml.safe_load(config_path.read_text())
    document = {"model_defaults": baseline["model_defaults"]}
    mutate_config(config_path, lambda current: {**current, "future_option": {"keep": True}})
    saved = save_model_config(config_path, document, baseline=baseline)
    assert "logging" not in saved
    assert saved["future_option"] == {"keep": True}
    assert saved["api_keys"] == ["synthetic-key"]


def test_concurrent_model_draft_conflict_is_atomic(config_path):
    baseline = yaml.safe_load(config_path.read_text())
    first = {**baseline, "model_defaults": {"models": {"synthetic": {"temperature": 1}}}}
    second = {**baseline, "model_defaults": {"models": {"synthetic": {"temperature": 2}}}}
    save_model_config(config_path, first, baseline=baseline)
    before = config_path.read_bytes()
    with pytest.raises(ConfigConflictError):
        save_model_config(config_path, second, baseline=baseline)
    assert config_path.read_bytes() == before


@pytest.mark.parametrize("edit_logging", [False, True])
async def test_handwritten_logging_is_compared_before_normalization(
    config_path, monkeypatch, edit_logging,
):
    baseline = "logging:\n  level: info\n  dump_requests: null\nmodel_defaults: {}\n"
    config_path.write_text(baseline, encoding="utf-8")
    document = yaml.safe_load(baseline)
    document["model_defaults"] = {"drop_unsupported_params": True}
    if edit_logging:
        document["logging"]["level"] = "ERROR"
    setter = MagicMock()
    monkeypatch.setattr(logger, "set_log_level", setter)
    await routes.update_config_yaml(routes.ConfigYamlUpdateRequest(
        yaml_content=yaml.safe_dump(document), baseline_yaml_content=baseline,
    ))
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["model_defaults"]["drop_unsupported_params"] is True
    if edit_logging:
        assert saved["logging"] == {"level": "ERROR", "dump_requests": False}
        setter.assert_called_once_with("ERROR")
    else:
        assert saved["logging"] == {"level": "info", "dump_requests": None}
        setter.assert_not_called()
