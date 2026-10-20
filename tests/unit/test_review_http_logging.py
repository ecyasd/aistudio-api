"""HTTP summaries must not retain path credentials or raw exception arguments."""

import asyncio
import importlib
import logging

import httpx
import pytest
from fastapi import FastAPI, Request

from aistudio_api.api.error_middleware import ErrorRecordsMiddleware
from aistudio_api.infrastructure.utils import error_records

application = importlib.import_module("aistudio_api.api.app")


@pytest.mark.parametrize("fails", [False, True])
async def test_http_logging_uses_fixed_operation_without_private_input(monkeypatch, caplog, fails):
    monkeypatch.setattr(application, "is_dump_requests_enabled", lambda: False)
    app = FastAPI()
    app.middleware("http")(application.logging_and_dump_middleware)

    @app.delete("/api-keys/{value}")
    async def endpoint(value: str):
        if fails:
            raise RuntimeError("SYNTHETIC_PRIVATE_EXCEPTION")
        return {"ok": bool(value)}

    with caplog.at_level(logging.INFO, logger=application.logger.name):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://synthetic.invalid",
        ) as client:
            response = await client.delete("/api-keys/SYNTHETIC_PRIVATE_KEY")
    assert response.status_code == (500 if fails else 200)
    messages = "\n".join(record.getMessage() for record in caplog.records
                         if record.name == application.logger.name)
    assert "api_key_management" in messages
    assert "SYNTHETIC_PRIVATE" not in messages
    assert "/api-keys/" not in messages
    if fails:
        assert "RuntimeError" in messages


async def test_unhandled_http_failure_is_not_reraised_to_server_logs(monkeypatch):
    monkeypatch.setattr(application, "is_dump_requests_enabled", lambda: False)
    app = FastAPI()
    app.middleware("http")(application.logging_and_dump_middleware)
    app.add_middleware(ErrorRecordsMiddleware)
    store = error_records.ErrorRecordStore()
    monkeypatch.setattr(error_records, "get_error_record_store", lambda: store)

    @app.get("/stats")
    async def endpoint():
        raise RuntimeError("SYNTHETIC_PRIVATE_EXCEPTION")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://synthetic.invalid"
    ) as client:
        response = await client.get("/stats")
    assert response.status_code == 500
    assert response.json()["error"]["status"] == "INTERNAL"
    assert "SYNTHETIC_PRIVATE" not in response.text
    events = store.list_records()["items"]
    assert len(events) == 1
    assert events[0]["exception_type"] == "RuntimeError"
    assert events[0]["outcome"] == "failed"
    assert events[0]["request_id"] == response.headers["x-request-id"]
    assert "SYNTHETIC_PRIVATE" not in str(events)


async def test_http_cancellation_propagates_without_becoming_an_internal_error(monkeypatch):
    monkeypatch.setattr(application, "is_dump_requests_enabled", lambda: False)
    request = Request({"type": "http", "method": "GET", "path": "/stats", "headers": []})

    async def cancelled(_request):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await application.logging_and_dump_middleware(request, cancelled)
