"""Offline request-to-diagnostics regressions using only synthetic state."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel

from aistudio_api.api.error_middleware import ErrorRecordsMiddleware
from aistudio_api.infrastructure.utils import error_records as records


@pytest.fixture
def diagnostic_store(tmp_path, monkeypatch):
    # A stale legacy path must be ignored by the memory-only implementation.
    path = tmp_path / "must-not-be-created.json"
    monkeypatch.setenv("AISTUDIO_ERROR_RECORDS_FILE", str(path))
    store = records.ErrorRecordStore()
    monkeypatch.setattr(records, "get_error_record_store", lambda: store)
    yield store
    assert not path.exists()
    payload = store.list_records()
    assert payload["retention"] == {"minutes": 10, "max_records": 100}
    assert "persistence" not in payload
    assert all("persisted" not in event for event in payload["items"])


def _diagnostic_payload(store):
    return json.dumps(store.list_records(), ensure_ascii=False)


def _application():
    from aistudio_api.api.app import (
        http_exception_handler,
        validation_exception_handler,
    )

    app = FastAPI()
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_middleware(ErrorRecordsMiddleware)
    return app


async def _post(app, path="/v1beta/models/gemini-test:generateContent", **kwargs):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://synthetic.invalid",
    ) as client:
        return await client.post(path, **kwargs)


@pytest.mark.parametrize("status", [400, 401, 403, 429, 503, 500])
async def test_http_failures_are_retained_once_with_request_identity(
    diagnostic_store, status
):
    app = _application()

    @app.post("/v1beta/models/gemini-test:generateContent")
    async def endpoint():
        raise HTTPException(status, detail="SYNTHETIC_PRIVATE_MESSAGE")

    response = await _post(app)
    assert response.status_code == status
    events = diagnostic_store.list_records()["items"]
    assert len(events) == 1
    assert events[0]["status"] == status
    assert events[0]["outcome"] == "failed"
    assert events[0]["transport_status"] == status
    assert events[0]["request_id"] == response.headers["x-request-id"]
    assert "SYNTHETIC_PRIVATE_MESSAGE" not in _diagnostic_payload(diagnostic_store)
    assert records.get_request_context() is None


async def test_validation_input_is_not_saved_or_echoed_in_diagnostics(diagnostic_store):
    class ImportInput(BaseModel):
        cookies: int

    app = _application()

    async def endpoint(body):
        raise AssertionError("Invalid input must not execute the endpoint")

    endpoint.__annotations__["body"] = ImportInput
    app.post("/api/accounts/import-cookies")(endpoint)
    response = await _post(
        app, "/api/accounts/import-cookies", json={"cookies": "SID=SYNTHETIC_COOKIE_SECRET"}
    )
    assert response.status_code == 400
    events = diagnostic_store.list_records()["items"]
    assert len(events) == 1
    assert events[0]["category"] == "validation"
    assert events[0]["exception_type"] == "RequestValidationError"
    payload = _diagnostic_payload(diagnostic_store)
    assert "SYNTHETIC_COOKIE_SECRET" not in payload
    assert "SID=" not in payload


async def test_unhandled_index_error_has_one_safe_internal_record(diagnostic_store):
    app = _application()

    @app.post("/v1beta/models/gemini-test:generateContent")
    async def endpoint():
        raise IndexError("SYNTHETIC_INTERNAL_SECRET")

    response = await _post(app)
    assert response.status_code == 500
    events = diagnostic_store.list_records()["items"]
    assert len(events) == 1
    assert events[0]["exception_type"] == "IndexError"
    assert events[0]["status"] == 500
    assert events[0]["outcome"] == "failed"
    assert "SYNTHETIC_INTERNAL_SECRET" not in _diagnostic_payload(diagnostic_store)


@pytest.fixture
def synthetic_generation(monkeypatch):
    from aistudio_api.api.schemas import GeminiGenerateContentRequest
    from aistudio_api.application import api_service_gemini as service

    context = SimpleNamespace(auth_file="synthetic-account", context_id=17)

    @asynccontextmanager
    async def request_scope():
        yield context

    client = SimpleNamespace(
        session=SimpleNamespace(request_scope=request_scope),
        clear_templates=MagicMock(),
        stream_generate_content=MagicMock(side_effect=AssertionError("No real model calls")),
    )
    monkeypatch.setattr(service, "ensure_active_account", AsyncMock())
    monkeypatch.setattr(service, "try_switch_account", AsyncMock(return_value=False))
    monkeypatch.setattr(service, "account_id_for_auth_file", lambda value: "acc_1234abcd")
    monkeypatch.setattr(service, "record_rotator_event", MagicMock())
    monkeypatch.setattr(service.runtime_state, "record", MagicMock())
    request = GeminiGenerateContentRequest.model_validate(
        {"contents": [{"role": "user", "parts": [{"text": "synthetic input"}]}]}
    )
    return service, client, request


async def test_sse_error_after_http_200_has_one_terminal_record(
    diagnostic_store, synthetic_generation
):
    from aistudio_api.domain.errors import RequestError

    service, client, request = synthetic_generation

    async def synthetic_stream(**kwargs):
        yield "body", "synthetic partial output"
        raise RequestError(503, "SYNTHETIC_UPSTREAM_SECRET")

    client.stream_generate_content.side_effect = synthetic_stream
    app = _application()

    @app.post("/v1beta/models/gemini-test:streamGenerateContent")
    async def endpoint():
        return service._build_gemini_streaming_response(
            client=client, req=request, model_path="gemini-test"
        )

    response = await _post(app, "/v1beta/models/gemini-test:streamGenerateContent")
    assert response.status_code == 200
    assert '"error"' in response.text
    assert "synthetic partial output" in response.text
    events = diagnostic_store.list_records()["items"]
    assert len(events) == 1
    assert events[0]["status"] == 503
    assert events[0]["transport_status"] == 200
    assert events[0]["outcome"] == "failed"
    assert events[0]["account_id"] == "acc_1234abcd"
    assert "SYNTHETIC_UPSTREAM_SECRET" not in _diagnostic_payload(diagnostic_store)


async def test_retry_recovered_response_updates_the_original_attempt(
    diagnostic_store, synthetic_generation
):
    from aistudio_api.infrastructure.gateway.wire_codec import (
        InvalidRequestTemplateError,
    )

    service, client, _ = synthetic_generation
    app = _application()

    @app.post("/v1beta/models/gemini-test:generateContent")
    async def endpoint():
        retry = await service.handle_attempt_exception(
            InvalidRequestTemplateError("SYNTHETIC_TEMPLATE_SECRET"),
            attempt=0, model_path="gemini-test", normalized_model="gemini-test",
            client=client, account_id="acc_1234abcd",
        )
        assert retry is True
        return {"ok": True}

    response = await _post(app)
    assert response.status_code == 200
    events = diagnostic_store.list_records()["items"]
    assert len(events) == 1
    assert events[0]["retrying"] is True
    assert events[0]["attempt"] == 1
    assert events[0]["outcome"] == "recovered"
    assert events[0]["transport_status"] == 200
    assert "SYNTHETIC_TEMPLATE_SECRET" not in _diagnostic_payload(diagnostic_store)


async def test_cold_start_failure_keeps_the_account_that_failed_preparation(
    diagnostic_store, synthetic_generation
):
    from aistudio_api.application.account_orchestrator import AccountPreparationError
    from aistudio_api.domain.errors import SessionExpiredError

    service, client, _ = synthetic_generation
    app = _application()

    @app.post("/v1beta/models/gemini-test:generateContent")
    async def endpoint():
        await service.handle_attempt_exception(
            AccountPreparationError("acc_abcd1234", SessionExpiredError("SYNTHETIC_COOKIE_SECRET")),
            attempt=0, model_path="gemini-test", normalized_model="gemini-test",
            client=client, account_id="acc_1234abcd",
        )

    response = await _post(app)
    assert response.status_code == 503
    events = diagnostic_store.list_records()["items"]
    assert len(events) == 1
    assert events[0]["account_id"] == "acc_abcd1234"
    assert events[0]["exception_type"] == "SessionExpiredError"
    assert events[0]["category"] == "session_expired"
    assert events[0]["outcome"] == "failed"
    assert "SYNTHETIC_COOKIE_SECRET" not in _diagnostic_payload(diagnostic_store)


async def test_client_cancel_is_a_499_record_without_replacing_cancellation(diagnostic_store):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise asyncio.CancelledError

    async def receive():
        raise AssertionError("Cancellation diagnostics must not consume a body")

    sent = []

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "path": "/v1beta/models/gemini-test:generateContent",
             "method": "POST", "headers": []}
    with pytest.raises(asyncio.CancelledError):
        await ErrorRecordsMiddleware(app)(scope, receive, send)
    events = diagnostic_store.list_records()["items"]
    assert len(events) == 1
    assert events[0]["status"] == 499
    assert events[0]["category"] == "cancelled"
    assert events[0]["outcome"] == "cancelled"
    assert events[0]["transport_status"] == 200
    assert records.get_request_context() is None


async def test_shared_context_crosses_base_http_child_tasks(diagnostic_store):
    app = _application()
    seen_contexts = []

    @app.middleware("http")
    async def base_http_layer(request, call_next):
        seen_contexts.append(records.get_request_context())
        return await call_next(request)

    @app.post("/v1beta/models/gemini-test:generateContent")
    async def endpoint():
        seen_contexts.append(records.get_request_context())
        records.record_failure(
            RuntimeError("SYNTHETIC_SECRET"), source="gemini", operation="generate_attempt",
            attempt=1, retrying=True,
        )
        return {"ok": True}

    # The production launcher also places a copy outside its auth middleware.
    response = await _post(ErrorRecordsMiddleware(app))
    assert response.status_code == 200
    assert seen_contexts[0] is seen_contexts[1]
    assert seen_contexts[0] is not None
    events = diagnostic_store.list_records()["items"]
    assert len(events) == 1
    assert events[0]["request_id"] == response.headers["x-request-id"]
    assert events[0]["outcome"] == "recovered"


async def test_errors_endpoint_requires_management_auth_without_starting_browser(
    diagnostic_store, monkeypatch
):
    from aistudio_api.api import routes_errors
    from aistudio_api.api.dependencies import require_web_auth
    from aistudio_api.config import settings
    from aistudio_api.infrastructure.gateway.client import AIStudioClient

    monkeypatch.setattr(settings, "web_password", "synthetic-management-password")
    unexpected = AsyncMock(side_effect=AssertionError("Diagnostics must not start the browser"))
    monkeypatch.setattr(AIStudioClient, "warmup", unexpected)
    monkeypatch.setattr(AIStudioClient, "generate_content", unexpected)
    monkeypatch.setattr(routes_errors, "get_error_record_store", lambda: diagnostic_store)
    app = _application()
    app.include_router(routes_errors.router, dependencies=[Depends(require_web_auth)])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://synthetic.invalid"
    ) as client:
        denied = await client.get("/errors")
        accepted = await client.get("/errors", headers={
            "Authorization": "Bearer synthetic-management-password",
        })
    assert denied.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json()["items"] == []
    assert "no-store" in accepted.headers.get("cache-control", "")
    unexpected.assert_not_awaited()


async def test_outer_guard_denials_are_recorded_without_reading_request_body(diagnostic_store):
    configured_path = os.getenv("AISTUDIO_TEST_PUBLIC_GUARD_FILE")
    path = Path(configured_path) if configured_path else (
        Path(__file__).resolve().parents[2] / "deploy" / "public_access_guard.py"
    )
    spec = importlib.util.spec_from_file_location("synthetic_public_access_guard", path)
    assert spec is not None and spec.loader is not None
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)

    async def unexpected_app(scope, receive, send):
        raise AssertionError("Rejected authentication must not execute the API")

    protected = guard.PublicAccessGuard(
        unexpected_app, settings_provider=lambda: SimpleNamespace(web_password="synthetic-admin"),
        api_keys_provider=lambda: {"synthetic-api-key"}, per_ip_limit=1,
    )
    app = ErrorRecordsMiddleware(protected)
    statuses = []

    async def receive():
        raise AssertionError("The outer middleware and guard must not consume the body")

    async def send(message):
        if message["type"] == "http.response.start":
            statuses.append(message["status"])

    for _ in range(2):
        scope = {"type": "http", "path": "/v1beta/models/gemini-test:generateContent",
                 "method": "POST", "headers": [(b"authorization", b"Bearer SYNTHETIC_KEY_SECRET")],
                 "query_string": b"key=SYNTHETIC_QUERY_SECRET", "client": ("203.0.113.2", 12000)}
        await app(scope, receive, send)
    assert statuses == [401, 429]
    events = diagnostic_store.list_records()["items"]
    assert len(events) == 2
    assert {event["status"] for event in events} == {401, 429}
    assert len({event["request_id"] for event in events}) == 2
    payload = _diagnostic_payload(diagnostic_store)
    assert "SYNTHETIC_KEY_SECRET" not in payload
    assert "SYNTHETIC_QUERY_SECRET" not in payload
    assert "203.0.113.2" not in payload
    assert all(event["outcome"] == "failed" for event in events)
