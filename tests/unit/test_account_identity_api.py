"""Offline HTTP/SSE handling for selected-account identity failures."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from aistudio_api.api import gemini as service, gemini_errors
from aistudio_api.api.app import http_exception_handler
from aistudio_api.api.dependencies import get_client
from aistudio_api.api.routes_gemini import router
from aistudio_api.application import (
    generation as generation_execution,
    generation_recovery as recovery,
)
from aistudio_api.application.account_orchestrator import AccountPreparationError
from aistudio_api.application.generation_errors import GenerationFailure
from aistudio_api.domain.errors import (
    AccountIdentityMismatchError,
    AccountIdentityUnavailableError,
)
from aistudio_api.domain.models import Candidate, ModelOutput

IDENTITY_ERRORS = [AccountIdentityMismatchError, AccountIdentityUnavailableError]
SYNTHETIC_DIAGNOSTIC = "synthetic-private-identity@example.invalid"
REQUEST = {"contents": [{"role": "user", "parts": [{"text": "offline"}]}]}


@pytest.fixture
def identity_api(monkeypatch):
    accounts = [SimpleNamespace(id="selected-account")]
    context = SimpleNamespace(auth_file="offline/selected-account", context_id=7)
    leased = []

    @asynccontextmanager
    async def request_scope():
        leased.append(context)
        try:
            yield context
        finally:
            leased.remove(context)

    client = SimpleNamespace(
        generate_content=AsyncMock(side_effect=AssertionError("unexpected model call")),
        stream_generate_content=MagicMock(side_effect=AssertionError("unexpected model call")),
        clear_templates=MagicMock(),
        session=SimpleNamespace(request_scope=request_scope),
    )
    monkeypatch.setattr(recovery.runtime_state, "account_service", SimpleNamespace(
        list_accounts=lambda: accounts,
        get_active_account=lambda: accounts[0],
        get_account_auth_path=lambda account_id: f"offline/{account_id}",
    ))
    monkeypatch.setattr(recovery.runtime_state, "rotator", SimpleNamespace(
        get_account_stats=lambda account_id: None,
    ))
    monkeypatch.setattr(recovery.runtime_state, "record", MagicMock())
    monkeypatch.setattr(recovery, "record_rotator_event", MagicMock())
    monkeypatch.setattr(generation_execution, "record_rotator_event", recovery.record_rotator_event)
    monkeypatch.setattr(generation_execution, "ensure_active_account", AsyncMock())
    monkeypatch.setattr(recovery, "try_switch_account", AsyncMock(return_value=False))
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(GenerationFailure, http_exception_handler)
    app.dependency_overrides[get_client] = lambda: client
    return SimpleNamespace(app=app, client=client, accounts=accounts, leased=leased)


def _sse_payloads(text):
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


@pytest.mark.parametrize("error_type", IDENTITY_ERRORS)
@pytest.mark.parametrize("wrapped", [False, True])
def test_identity_error_classification_preserves_type_without_raw_identity(error_type, wrapped):
    error = error_type(SYNTHETIC_DIAGNOSTIC)
    if wrapped:
        error = AccountPreparationError("selected-account", error)
    status, message, status_name = gemini_errors.classify_gemini_error_payload(error)
    assert status == 503 and status_name == "UNAVAILABLE"
    assert SYNTHETIC_DIAGNOSTIC not in message
    assert "keep your API key" in message
    assert "expired" not in message
    assert gemini_errors.gemini_error_type(error) == error_type.code
    assert json.loads(service.format_sse_error(error)[6:])["error"]["type"] == error_type.code


@pytest.mark.parametrize("error_type", IDENTITY_ERRORS)
@pytest.mark.parametrize("stream", [False, True])
async def test_preparation_identity_error_reaches_http_or_sse_without_model_call(
    identity_api, error_type, stream,
):
    async def prepare(*args, **kwargs):
        # A concurrent registry change must not change the failed target.
        identity_api.accounts.insert(0, SimpleNamespace(id="other-account"))
        raise AccountPreparationError("selected-account", error_type(SYNTHETIC_DIAGNOSTIC))

    generation_execution.ensure_active_account.side_effect = prepare
    suffix = "streamGenerateContent" if stream else "generateContent"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=identity_api.app), base_url="http://offline",
    ) as http:
        response = await http.post(f"/v1beta/gemini-test:{suffix}", json=REQUEST)
    assert response.status_code == (200 if stream else 503)
    payloads = _sse_payloads(response.text) if stream else [response.json()]
    assert len(payloads) == 1
    assert payloads[0]["error"]["code"] == 503
    assert payloads[0]["error"]["status"] == "UNAVAILABLE"
    assert payloads[0]["error"]["type"] == error_type.code
    assert SYNTHETIC_DIAGNOSTIC not in response.text
    assert "expired" not in response.text
    identity_api.client.generate_content.assert_not_awaited()
    identity_api.client.stream_generate_content.assert_not_called()
    identity_api.client.clear_templates.assert_called_once()
    recovery.runtime_state.record.assert_not_called()
    switch = recovery.try_switch_account.await_args.kwargs
    assert switch["failed_account_id"] == "selected-account"
    assert switch["failed_context_id"] is None
    assert switch["is_auth_error"] is True
    assert not switch.get("is_session_expired")
    assert not identity_api.leased


@pytest.mark.parametrize("error_type", IDENTITY_ERRORS)
async def test_identity_error_after_output_stays_in_band_without_retry(identity_api, error_type):
    async def upstream(**kwargs):
        yield "body", "synthetic partial reply"
        identity_api.accounts.insert(0, SimpleNamespace(id="other-account"))
        raise error_type(SYNTHETIC_DIAGNOSTIC)

    identity_api.client.stream_generate_content.side_effect = upstream
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=identity_api.app), base_url="http://offline",
    ) as http:
        response = await http.post("/v1beta/gemini-test:streamGenerateContent", json=REQUEST)
    payloads = _sse_payloads(response.text)
    assert len(payloads) == 2
    assert "candidates" in payloads[0]
    assert payloads[-1]["error"]["code"] == 503
    assert payloads[-1]["error"]["type"] == error_type.code
    assert SYNTHETIC_DIAGNOSTIC not in response.text
    assert "finishReason" not in response.text
    assert "usageMetadata" not in response.text
    recovery.try_switch_account.assert_not_awaited()
    recovery.record_rotator_event.assert_called_once_with(
        "auth_error", model="models/gemini-test", account_id="selected-account",
    )
    recovery.runtime_state.record.assert_not_called()
    assert identity_api.client.stream_generate_content.call_count == 1
    assert not identity_api.leased


@pytest.mark.parametrize("stream", [False, True])
async def test_failover_identity_failure_keeps_target_type_and_stops(identity_api, stream):
    first_error = AccountIdentityMismatchError(SYNTHETIC_DIAGNOSTIC)
    generation_execution.ensure_active_account.side_effect = AccountPreparationError("selected-account", first_error)
    recovery.try_switch_account.side_effect = AccountPreparationError(
        "failover-account", AccountIdentityUnavailableError(SYNTHETIC_DIAGNOSTIC),
    )
    suffix = "streamGenerateContent" if stream else "generateContent"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=identity_api.app), base_url="http://offline",
    ) as http:
        response = await http.post(f"/v1beta/gemini-test:{suffix}", json=REQUEST)
    payload = _sse_payloads(response.text)[0] if stream else response.json()
    assert payload["error"]["code"] == 503
    assert payload["error"]["type"] == "upstream_account_unavailable"
    assert SYNTHETIC_DIAGNOSTIC not in response.text
    recovery.try_switch_account.assert_awaited_once()
    recovery.record_rotator_event.assert_called_once_with(
        "auth_error", model="models/gemini-test", account_id="failover-account",
    )
    identity_api.client.generate_content.assert_not_awaited()
    identity_api.client.stream_generate_content.assert_not_called()
    assert not identity_api.leased


@pytest.mark.parametrize("error_type", IDENTITY_ERRORS)
@pytest.mark.parametrize("stream", [False, True])
async def test_identity_failover_prepares_again_before_success(identity_api, error_type, stream):
    generation_execution.ensure_active_account.side_effect = [
        AccountPreparationError("selected-account", error_type(SYNTHETIC_DIAGNOSTIC)),
        None,
    ]
    recovery.try_switch_account.return_value = True
    identity_api.client.generate_content.side_effect = None
    identity_api.client.generate_content.return_value = ModelOutput(
        candidates=[Candidate(text="synthetic verified reply", finish_reason=1)],
    )

    async def upstream(**kwargs):
        yield "body", "synthetic verified reply"
        yield "finish_reason", 1

    identity_api.client.stream_generate_content.side_effect = upstream
    suffix = "streamGenerateContent" if stream else "generateContent"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=identity_api.app), base_url="http://offline",
    ) as http:
        response = await http.post(f"/v1beta/gemini-test:{suffix}", json=REQUEST)
    assert response.status_code == 200
    payloads = _sse_payloads(response.text) if stream else [response.json()]
    assert all("error" not in payload for payload in payloads)
    assert payloads[-1]["candidates"][0]["finishReason"] == "STOP"
    assert "synthetic verified reply" in response.text
    generation_execution.ensure_active_account.assert_has_awaits([
        call(0, model="models/gemini-test"),
        call(1, model="models/gemini-test"),
    ])
    assert generation_execution.ensure_active_account.await_count == 2
    recovery.try_switch_account.assert_awaited_once()
    recovery.record_rotator_event.assert_called_once_with(
        "success", model="models/gemini-test", account_id="selected-account",
    )
    assert not identity_api.leased
