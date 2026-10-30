"""Read-only model previews and exact, bounded account/request diagnostics."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
import yaml
from fastapi import Depends, FastAPI

from aistudio_api.api import routes_errors, routes_system
from aistudio_api.api.dependencies import require_web_auth
from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway import model_defaults as rules
from aistudio_api.infrastructure.utils import (
    error_records as errors,
    response_diagnostics as diagnostics,
)


@pytest.fixture
def management(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("api_keys: [synthetic-private-key]\nmodel_defaults:\n  profiles: []\n", encoding="utf-8")
    monkeypatch.setenv("AISTUDIO_CONFIG_FILE", str(config))
    monkeypatch.setattr(settings, "web_password", "synthetic-manager")
    monkeypatch.setattr(rules, "_resolve_config_path", lambda _path: config)
    monkeypatch.setattr(routes_system, "_resolve_config_path", lambda _path: config)
    rules.invalidate_config_cache()
    app = FastAPI()
    app.include_router(routes_system.protected_router, dependencies=[Depends(require_web_auth)])
    app.include_router(routes_errors.router, dependencies=[Depends(require_web_auth)])
    yield app, config
    rules.invalidate_config_cache()


def client_for(app, *, authenticated=True):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://synthetic.invalid",
                            headers={"Authorization": "Bearer synthetic-manager"} if authenticated else {})


async def test_preview_matches_runtime_without_saving_or_polluting_cache(management):
    app, path = management
    before = path.read_bytes()
    saved = rules.resolve_model_defaults("gemini-test")
    draft = {"api_keys": ["MUST_NOT_BE_RETURNED"], "private_extension": "MUST_NOT_BE_RETURNED",
             "model_defaults": {"drop_unsupported_params": True, "profiles": [
                 {"name": "all", "match": {"prefixes": ["gemini-"]}, "is_image_model": True,
                  "default_tools": ["google_search"], "generation_config_defaults": {"temperature": 0.2}},
                 {"name": "specific", "match": {"contains": ["test"]}, "default_tools": ["image_search"],
                  "generation_config_defaults": {"temperature": 0.6, "top_k": 4}},
             ], "models": {"gemini-test": {"is_image_model": False, "default_tools": [],
                 "generation_config_defaults": {"temperature": 0.9, "unknown_extension": 7},
                 "drop_unsupported_params": False}}}}
    async with client_for(app) as client:
        response = await client.post("/config/model-preview", json={
            "model": "models/抗截断/gemini-test", "yaml_content": yaml.safe_dump(draft),
        })
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    result = response.json()
    assert result["model"] == "models/gemini-test" and result["auto_continue"] is True
    assert [r["name"] for r in result["matched_rules"]] == ["all", "specific", "gemini-test"]
    effective = result["effective"]
    assert not effective["is_image_model"] and not effective["drop_unsupported_params"]
    assert effective["default_tools"] == []
    assert effective["generation_config_defaults"] == {"temperature": 0.9, "top_k": 4}
    assert result["ignored_generation_keys"] == ["unknown_extension"]
    assert "MUST_NOT_BE_RETURNED" not in response.text and "synthetic-private-key" not in response.text
    assert path.read_bytes() == before and rules.resolve_model_defaults("gemini-test") == saved
    # Saving the same synthetic draft through the runtime reader gives the
    # identical resolved values; preview and runtime share one compiler.
    path.write_text(yaml.safe_dump(draft), encoding="utf-8")
    rules.invalidate_config_cache()
    runtime = rules.resolve_model_defaults("gemini-test")
    assert runtime.generation_config_overrides()["temperature"] == effective["generation_config_defaults"]["temperature"]
    assert list(runtime.default_tools) == effective["default_tools"]
    assert runtime.drop_unsupported_params == effective["drop_unsupported_params"]


async def test_preview_uses_same_builtin_fallbacks_and_saved_rules(management):
    app, path = management
    path.write_text("api_keys: [synthetic-private-key]\n", encoding="utf-8")
    rules.invalidate_config_cache()
    async with client_for(app) as client:
        for draft in ({}, {"model_defaults": {"models": {}}}):
            response = await client.post("/config/model-preview", json={
                "model": "gemini-test-image", "yaml_content": yaml.safe_dump(draft),
            })
            result = response.json()
            assert result["effective"]["is_image_model"] is True
            assert result["effective"]["generation_config_defaults"]["thinking_config"] == [1, None, None, 4]
        response = await client.post("/config/model-preview", json={"model": "gemini-test-image"})
        assert response.json()["source"] == "saved"
        assert "synthetic-private-key" not in response.text


@pytest.mark.parametrize("content", ["model_defaults: [oops]", "[", "- list", "model_defaults:\n  profiles:\n    - generation_config_defaults:\n        thinking_config: {level: SECRET_INVALID_ENUM}"])
async def test_preview_rejects_invalid_drafts_without_writes_or_raw_errors(management, content):
    app, path = management
    before = path.read_bytes()
    async with client_for(app) as client:
        response = await client.post("/config/model-preview", json={"model": "gemini-test", "yaml_content": content})
    assert response.status_code == 400
    assert "SECRET_INVALID_ENUM" not in response.text
    assert path.read_bytes() == before


async def test_preview_requires_management_auth(management):
    app, _ = management
    async with client_for(app, authenticated=False) as client:
        response = await client.post("/config/model-preview", json={"model": "gemini-test", "yaml_content": "{}"})
    assert response.status_code == 401


async def test_context_filters_run_before_pagination_and_include_cross_account_request(management, monkeypatch):
    app, _ = management
    store, summaries = errors.ErrorRecordStore(), diagnostics.ResponseDiagnosticStore()
    monkeypatch.setattr(routes_errors, "get_error_record_store", lambda: store)
    monkeypatch.setattr(routes_errors, "get_response_diagnostic_store", lambda: summaries)
    for number, account in enumerate(["acc_11111111", "acc_22222222", "acc_11111111"]):
        token = errors.bind_request_context("req_11111111" if number < 2 else "req_22222222")
        try:
            store.record(TimeoutError(), source="gemini", operation="generate_attempt", account_id=account)
        finally:
            errors.reset_request_context(token)
    summaries.record({"request_id": "req_11111111", "time": datetime.now(UTC).isoformat(),
                      "account_ids": ["acc_11111111", "acc_22222222", "invalid-secret"], "outcome": "failed"})
    summaries.record({"request_id": "req_22222222", "time": datetime.now(UTC).isoformat(),
                      "account_ids": ["acc_11111111"], "outcome": "success"})
    async with client_for(app) as client:
        response = await client.get("/errors", params={"account_id": "acc_11111111", "limit": 1, "include_diagnostics": True})
        first = response.json()
        assert first["total"] == 2 and first["has_more"]
        second = (await client.get("/errors", params={"account_id": "acc_11111111", "limit": 1, "cursor": first["next_cursor"]})).json()
        assert len(second["items"]) == 1 and not second["has_more"]
        result = (await client.get("/errors", params={"request_id": "req_11111111", "include_diagnostics": True})).json()
        assert {r["account_id"] for r in result["items"]} == {"acc_11111111", "acc_22222222"}
        assert len(result["diagnostics"]["items"]) == 1
        assert result["diagnostics"]["items"][0]["account_ids"] == ["acc_11111111", "acc_22222222"]
        assert "invalid-secret" not in json.dumps(result)
        assert (await client.get("/errors", params={"account_id": "not-an-account"})).status_code == 422


def test_request_membership_is_bounded_and_ignores_unsafe_ids():
    token = errors.bind_request_context()
    try:
        diagnostics.initialize_response_diagnostics(stream=True)
        diagnostics.configure_generation(model="gemini-test", auto_continue=False)
        for number in range(40):
            errors.note_request_account(f"acc_{number:08x}")
        errors.note_request_account("not-safe-account")
        context = errors.get_request_context()
        assert context is not None and len(context.account_ids) == 32
        assert context.account_ids == list(dict.fromkeys(context.account_ids))
    finally:
        errors.reset_request_context(token)
