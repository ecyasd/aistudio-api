"""Tests for system and configuration routes."""

from __future__ import annotations

import httpx
import pytest

from aistudio_api.api.app import app
from aistudio_api.config import settings


@pytest.fixture(autouse=True)
def management_password(monkeypatch):
    monkeypatch.setattr(settings, "web_password", "synthetic-management-only")


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
        headers={"Authorization": "Bearer synthetic-management-only"},
    )


@pytest.mark.asyncio
async def test_health_endpoint():
    async with _client() as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_auth_check_endpoint():
    async with _client() as client:
        resp = await client.get("/auth/check")
        assert resp.status_code == 200
        data = resp.json()
        assert "auth_enabled" in data


@pytest.mark.asyncio
async def test_spa_endpoints():
    import asyncio

    async with _client() as client:
        responses = await asyncio.gather(
            *[
                client.get(path, headers={"Accept": "text/html"})
                for path in ("/", "/login", "/accounts", "/settings")
            ]
        )
        for resp in responses:
            assert resp.status_code == 200
            assert "<!DOCTYPE html>" in resp.text
            assert "Accept" in resp.headers.get("Vary", "")
            assert "no-store" in resp.headers.get("Cache-Control", "")


@pytest.mark.asyncio
async def test_get_config_endpoint():
    async with _client() as client:
        resp = await client.get("/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "port" in data
        assert "browser_port" in data
        assert "yaml_content" in data


@pytest.mark.asyncio
async def test_update_config_yaml_validation():
    async with _client() as client:
        resp = await client.put(
            "/config/yaml", json={"yaml_content": "invalid: [yaml: broken"}
        )
        assert resp.status_code == 400

        resp_non_dict = await client.put(
            "/config/yaml", json={"yaml_content": "- item1\n- item2"}
        )
        assert resp_non_dict.status_code == 400


@pytest.mark.parametrize("rule", [
    "profiles: {wrong: shape}",
    "profiles: [42]",
    "profiles: [{match: {prefixes: gemini-}}]",
    "profiles: [{generation_config_defaults: {thinking_config: {level: TYPO}}}]",
    "profiles: [{generation_config_defaults: {image_output_mode: TYPO}}]",
    "profiles: [{generation_config_defaults: {temperature: .nan}}]",
    "models: {other: {generation_config_defaults: {top_k: broken}}}",
])
async def test_bad_model_rule_does_not_poison_config_or_cached_models(monkeypatch, tmp_path, rule):
    from aistudio_api.infrastructure.gateway.model_defaults import (
        invalidate_config_cache,
        resolve_model_defaults,
    )
    path = tmp_path / "isolated.yaml"
    original = "extension: {kept: true}\napi_keys: []\nmodel_defaults:\n  profiles: []\n"
    path.write_text(original, encoding="utf-8")
    monkeypatch.setenv("AISTUDIO_CONFIG_FILE", str(path))
    invalidate_config_cache()
    before = resolve_model_defaults("gemini-unrelated")
    async with _client() as client:
        response = await client.put("/config/yaml", json={"yaml_content": "model_defaults:\n  " + rule + "\n"})
    assert response.status_code == 400
    assert path.read_text(encoding="utf-8") == original
    assert resolve_model_defaults("gemini-unrelated") == before


@pytest.mark.parametrize("key", ["fixture/with/slash", ".", "..", "fixture/中文?#%"])
async def test_api_keys_can_be_managed_without_path_encoding(monkeypatch, tmp_path, key):
    path = tmp_path / "keys.yaml"
    path.write_text("api_keys: []\n", encoding="utf-8")
    monkeypatch.setenv("AISTUDIO_CONFIG_FILE", str(path))
    async with _client() as client:
        created = await client.post("/api-keys", json={"name": "before", "key": key})
        assert created.status_code == 200
        renamed = await client.put("/api-keys", json={"key": key, "name": "after"})
        assert renamed.status_code == 200
        assert renamed.json()["key"] == key and renamed.json()["name"] == "after"
        deleted = await client.request("DELETE", "/api-keys", json={"key": key})
        assert deleted.status_code == 200
        assert (await client.get("/api-keys")).json() == []


@pytest.mark.asyncio
async def test_rotation_status_and_clear_cooldown():
    from unittest.mock import MagicMock

    from aistudio_api.application.account_rotator import AccountRotator
    from aistudio_api.application.runtime import runtime_state

    mock_rotator = MagicMock(spec=AccountRotator)
    mock_rotator.get_all_stats.return_value = {"acc_1": {"requests": 1}}

    runtime_state.rotator = mock_rotator
    async with _client() as client:
        resp = await client.get("/rotation")
        assert resp.status_code == 200
        data = resp.json()
        assert data["mode"] == "sticky"
        assert "accounts" in data

        resp_clear = await client.post(
            "/rotation/clear-cooldown", json={"account_id": "acc_1"}
        )
        assert resp_clear.status_code == 200
        mock_rotator.clear_cooldown.assert_called_once_with("acc_1", model=None)
