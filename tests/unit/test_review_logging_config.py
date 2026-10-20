"""Logging changes validate before writes and publish only after persistence."""

from unittest.mock import MagicMock

import pytest
import yaml
from fastapi import HTTPException

from aistudio_api.api import routes_system as routes
from aistudio_api.infrastructure.gateway import model_defaults
from aistudio_api.infrastructure.utils import logger


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "synthetic-config.yaml"
    path.write_text("api_keys: [synthetic-key]\nmodel_defaults: {}\n", encoding="utf-8")
    monkeypatch.setenv("AISTUDIO_CONFIG_FILE", str(path))
    model_defaults.invalidate_config_cache()
    yield path
    model_defaults.invalidate_config_cache()


@pytest.fixture
def runtime_setters(monkeypatch):
    setters = {}
    for name in ("set_log_level", "set_dump_requests", "set_dump_to_file", "set_dump_dir"):
        setters[name] = MagicMock()
        monkeypatch.setattr(logger, name, setters[name])
    return setters


async def test_failed_logging_save_does_not_change_runtime(config_path, runtime_setters, monkeypatch):
    before = config_path.read_bytes()

    def failed(**_):
        raise OSError("synthetic persistence failure")

    monkeypatch.setattr(model_defaults, "update_configured_logging_settings", failed)
    with pytest.raises((OSError, HTTPException)):
        await routes.update_logging_config(routes.LoggingConfigRequest(
            level="DEBUG", dump_requests=True, dump_to_file=True, dump_dir="synthetic-dir",
        ))
    assert config_path.read_bytes() == before
    for setter in runtime_setters.values():
        setter.assert_not_called()


@pytest.mark.parametrize("logging_config", [
    {"dump_requests": "false"}, {"dump_to_file": "false"},
    {"dump_requests": 1}, {"dump_to_file": []},
    {"level": "not-a-level"}, {"level": {"value": "DEBUG"}}, "DEBUG",
])
async def test_invalid_yaml_logging_is_rejected_before_persistence(
    config_path, runtime_setters, logging_config,
):
    before = config_path.read_bytes()
    with pytest.raises(HTTPException) as error:
        await routes.update_config_yaml(routes.ConfigYamlUpdateRequest(
            yaml_content=yaml.safe_dump({"logging": logging_config}),
        ))
    assert error.value.status_code == 400
    assert config_path.read_bytes() == before
    for setter in runtime_setters.values():
        setter.assert_not_called()


@pytest.mark.parametrize("raw", ["false", "0", 1, [True]])
def test_legacy_malformed_boolean_cannot_enable_payload_dump(config_path, raw):
    config_path.write_text(yaml.safe_dump({"logging": {"dump_requests": raw, "dump_to_file": raw}}))
    settings = model_defaults.get_configured_logging_settings()
    assert settings["dump_requests"] is False
    assert settings["dump_to_file"] is False


async def test_valid_yaml_logging_applies_only_after_the_file_is_saved(config_path, runtime_setters):
    seen = []
    runtime_setters["set_log_level"].side_effect = lambda level: seen.append(
        (level, yaml.safe_load(config_path.read_text())["logging"]["level"])
    )
    await routes.update_config_yaml(routes.ConfigYamlUpdateRequest(
        yaml_content="logging:\n  level: warning\n  dump_requests: false\n  dump_to_file: false\n",
    ))
    assert seen == [("WARNING", "WARNING")]
    assert model_defaults.get_configured_api_keys() == frozenset({"synthetic-key"})


async def test_yaml_null_logging_fields_apply_their_disabled_defaults(config_path, runtime_setters):
    await routes.update_config_yaml(routes.ConfigYamlUpdateRequest(
        yaml_content="logging:\n  level: null\n  dump_requests: null\n  dump_to_file: null\n  dump_dir: null\n",
    ))
    runtime_setters["set_log_level"].assert_called_once_with("INFO")
    runtime_setters["set_dump_requests"].assert_called_once_with(False)
    runtime_setters["set_dump_to_file"].assert_called_once_with(False)
    runtime_setters["set_dump_dir"].assert_called_once_with("")
