"""Offline reproductions for the supplied review; all credentials are synthetic."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aistudio_api import config
from aistudio_api.api.schemas import GeminiGenerateContentRequest
from aistudio_api.application.chat_service import normalize_gemini_request
from aistudio_api.infrastructure.account.cookie_parser import calculate_sapisid_hash
from aistudio_api.infrastructure.gateway.model_discovery import ModelDiscoveryService


@pytest.mark.parametrize("name,payload", [
    ("stats.json", {"gemini-test": {"requests": 3}}),
    ("rotator_state.json", {"accounts": {"synthetic": {}}}),
    ("other-metadata.json", {"last_used": 123}),
])
def test_auth_discovery_does_not_select_runtime_json(tmp_path, monkeypatch, name, payload):
    monkeypatch.delenv("AISTUDIO_AUTH_FILE", raising=False)
    monkeypatch.setattr(config, "_AUTH_SEARCH_ROOTS", [tmp_path])
    (tmp_path / name).write_text(json.dumps(payload), encoding="utf-8")
    assert config.discover_auth_file() is None


def test_auth_discovery_keeps_legacy_storage_state_files(tmp_path, monkeypatch):
    monkeypatch.delenv("AISTUDIO_AUTH_FILE", raising=False)
    monkeypatch.setattr(config, "_AUTH_SEARCH_ROOTS", [tmp_path])
    auth = tmp_path / "legacy-session.json"
    auth.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
    assert config.discover_auth_file() == str(auth)


def test_empty_history_is_removed_without_losing_signed_parts_or_empty_text():
    req = GeminiGenerateContentRequest.model_validate({"contents": [
        {"role": "user", "parts": [{"text": "old prompt"}]},
        {"role": "model", "parts": [{"thought": True}, {}]},
        {"role": "model", "parts": [{"thought": True, "thoughtSignature": "synthetic-signature"}]},
        {"role": "user", "parts": [{"text": ""}, {"text": "new prompt"}]},
    ], "tools": []})
    normalized = normalize_gemini_request(req, "gemini-test")
    assert len(normalized.contents) == 3
    assert all(content.parts for content in normalized.contents)
    assert normalized.contents[1].parts[0].thought_signature == "synthetic-signature"
    assert normalized.contents[1].parts[0].thought is True
    assert normalized.contents[2].parts[0].text == ""
    assert normalized.capture_prompt == "\nnew prompt"


@pytest.mark.parametrize("earlier", [[], [{"role": "user", "parts": [{"text": "old prompt"}]}]])
def test_empty_last_turn_is_rejected_instead_of_answering_previous_history(earlier):
    req = GeminiGenerateContentRequest.model_validate({"contents": [
        *earlier, {"role": "user", "parts": [{"thought": True}, {}]},
    ], "tools": []})
    with pytest.raises(ValueError, match="Last content"):
        normalize_gemini_request(req, "gemini-test")


def synthetic_cookies():
    return [
        {"name": "SAPISID", "value": "current-synthetic", "domain": ".google.com", "path": "/", "expires": 2000},
        {"name": "SAPISID", "value": "expired-synthetic", "domain": ".google.com", "path": "/", "expires": 500},
        {"name": "SAPISID", "value": "foreign-synthetic", "domain": ".example.invalid", "path": "/"},
    ]


async def test_page_model_discovery_uses_selected_slot_and_current_signing_cookie(monkeypatch):
    monkeypatch.setattr("time.time", lambda: 1000)
    page = SimpleNamespace(url="https://aistudio.google.com/u/2/prompts/new_chat",
        get_cookies=AsyncMock(return_value=synthetic_cookies()),
        evaluate=AsyncMock(return_value={"ok": True, "data": []}))
    service = ModelDiscoveryService()
    await service._fetch_via_page(page, auth_user="2")
    headers = page.evaluate.call_args.args[1]["headers"]
    assert headers["X-Goog-AuthUser"] == "2"
    assert headers["Authorization"] == calculate_sapisid_hash({"SAPISID": "current-synthetic"})
    assert "Cookie" not in headers


async def test_http_model_discovery_filters_expired_and_foreign_cookies(tmp_path, monkeypatch):
    monkeypatch.setattr("time.time", lambda: 1000)
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"cookies": synthetic_cookies()}), encoding="utf-8")
    session = SimpleNamespace(page=None, _auth_file=str(auth), get_current_auth_user=lambda: "2")
    service = ModelDiscoveryService()
    service._fetch_via_http = AsyncMock(return_value=[{"id": "gemini-synthetic"}])
    assert await service.get_models(session=session) == [{"id": "gemini-synthetic"}]
    service._fetch_via_http.assert_awaited_once_with({"SAPISID": "current-synthetic"}, "2")
