"""Runtime model rules and previews share values without sharing read effects."""

from __future__ import annotations

import json
import os

import pytest

from aistudio_api import config as configuration
from aistudio_api.infrastructure.gateway import model_defaults as rules


def document(temperature, top_p):
    return {"api_keys": ["synthetic-key"], "model_defaults": {
        "profiles": [{"name": "synthetic", "match": {"prefixes": ["gemini-"]},
                      "generation_config_defaults": {"temperature": temperature}}],
        "models": {"gemini-synthetic": {"generation_config_defaults": {"top_p": top_p}}},
    }}


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(document(0.2, 0.3)), encoding="utf-8")
    monkeypatch.setenv("AISTUDIO_CONFIG_FILE", str(path))
    rules.invalidate_config_cache()
    yield path
    rules.invalidate_config_cache()


def test_direct_file_edit_recompiles_profiles_and_overrides(config_path):
    initial = rules.resolve_model_defaults("gemini-synthetic")
    assert initial.generation_config_overrides() == {"temperature": 0.2, "top_p": 0.3}
    config_path.write_text(json.dumps(document(1.2, 0.9)), encoding="utf-8")
    stamp = config_path.stat()
    os.utime(config_path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 2_000_000_000))
    current = rules.resolve_model_defaults("gemini-synthetic")
    preview, _ = rules.preview_model_defaults("gemini-synthetic")
    assert current.generation_config_overrides() == {"temperature": 1.2, "top_p": 0.9}
    assert current == preview


def test_saved_preview_never_fills_or_changes_runtime_caches(config_path):
    before = dict(rules._CONFIG_CACHE), dict(rules._API_KEYS_CACHE)
    preview, _ = rules.preview_model_defaults("gemini-synthetic")
    assert preview.generation_config_overrides() == {"temperature": 0.2, "top_p": 0.3}
    assert (dict(rules._CONFIG_CACHE), dict(rules._API_KEYS_CACHE)) == before


def test_missing_saved_preview_does_not_initialize_configuration(tmp_path, monkeypatch):
    path = tmp_path / "missing-parent" / "config.yaml"
    template = tmp_path / "synthetic-template.yaml"
    template.write_text(json.dumps(document(0.2, 0.3)), encoding="utf-8")
    monkeypatch.setenv("AISTUDIO_CONFIG_FILE", str(path))
    monkeypatch.setattr(configuration, "_TEMPLATE_CONFIG_PATHS", (template,))
    rules.invalidate_config_cache()
    try:
        rules.preview_model_defaults("gemini-synthetic")
        assert not path.exists() and not path.parent.exists()
        assert not rules._CONFIG_CACHE
    finally:
        rules.invalidate_config_cache()


def test_one_resolution_uses_one_document_during_a_file_replacement(config_path, monkeypatch):
    original_profiles = rules._profiles
    changed = False

    def replace_after_profiles(config):
        nonlocal changed
        result = original_profiles(config)
        if not changed:
            changed = True
            config_path.write_text(json.dumps(document(1.2, 0.9)), encoding="utf-8")
            stamp = config_path.stat()
            os.utime(config_path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 2_000_000_000))
        return result

    monkeypatch.setattr(rules, "_profiles", replace_after_profiles)
    first = rules.resolve_model_defaults("gemini-synthetic").generation_config_overrides()
    second = rules.resolve_model_defaults("gemini-synthetic").generation_config_overrides()
    assert first == {"temperature": 0.2, "top_p": 0.3}
    assert second == {"temperature": 1.2, "top_p": 0.9}
