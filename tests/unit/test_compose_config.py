"""Compose migration preserves synthetic configs and keeps writes replaceable."""

import importlib.util
import os
from pathlib import Path, PurePosixPath

import pytest
import yaml

from aistudio_api.infrastructure.utils.config_store import mutate_config

REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("prepare_compose_config", REPO / "scripts/prepare_compose_config.py")
assert SPEC and SPEC.loader
preparer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preparer)


def test_compose_config_migration_preserves_legacy_and_allows_atomic_save(tmp_path):
    legacy = tmp_path / "config.yaml"
    original = b"api_keys: [synthetic-call-key]\ncustom: retained\n"
    legacy.write_bytes(original)
    assert preparer.prepare_config(tmp_path) == "migrated"
    destination = tmp_path / "runtime-config/config.yaml"
    assert destination.read_bytes() == original
    mutate_config(destination, lambda current: {**current, "model_defaults": {"custom": True}})
    assert yaml.safe_load(destination.read_text())["api_keys"] == ["synthetic-call-key"]
    assert legacy.read_bytes() == original
    if os.name != "nt":
        assert destination.stat().st_mode & 0o777 == 0o600


def test_compose_config_conflict_never_overwrites_either_copy(tmp_path):
    legacy = tmp_path / "config.yaml"
    legacy.write_bytes(b"api_keys: [synthetic-original]\n")
    destination = tmp_path / "runtime-config/config.yaml"
    destination.parent.mkdir()
    destination.write_bytes(b"api_keys: [synthetic-current]\n")
    with pytest.raises(ValueError, match="differ"):
        preparer.prepare_config(tmp_path)
    assert b"synthetic-original" in legacy.read_bytes()
    assert b"synthetic-current" in destination.read_bytes()


def test_compose_config_initializes_once_and_preserves_existing(tmp_path):
    (tmp_path / "config.example.yaml").write_text("api_keys: []\n")
    assert preparer.prepare_config(tmp_path) == "initialized"
    destination = tmp_path / "runtime-config/config.yaml"
    destination.write_text("api_keys: [synthetic-owner-key]\n")
    assert preparer.prepare_config(tmp_path) == "preserved"
    assert "synthetic-owner-key" in destination.read_text()


def test_compose_mounts_config_parent_instead_of_replace_target():
    service = yaml.safe_load((REPO / "docker-compose.yml").read_text())["services"]["aistudio-api"]
    configured = next(value.split("=", 1)[1] for value in service["environment"] if value.startswith("AISTUDIO_CONFIG_FILE="))
    mounts = [volume for volume in service["volumes"] if isinstance(volume, dict)]
    assert all(volume["target"] != configured for volume in mounts)
    parent = next(volume for volume in mounts if volume["target"] == str(PurePosixPath(configured).parent))
    assert parent["source"] == "./runtime-config"
    assert parent["bind"]["create_host_path"] is False
