"""Usage sums are observed metadata, not generated-text estimates."""
import json

import httpx
from fastapi import Depends, FastAPI

from aistudio_api.api.dependencies import require_web_auth
from aistudio_api.api.routes_system import protected_router
from aistudio_api.config import settings
from aistudio_api.infrastructure.utils import request_usage


def test_response_lifecycle_deduplicates_cumulative_reports_across_retries(tmp_path, monkeypatch):
    from aistudio_api.infrastructure.gateway.wire_parser import parse_usage_metadata
    from aistudio_api.infrastructure.utils import error_records, response_diagnostics
    monkeypatch.setattr(request_usage, "database_path", lambda: tmp_path / "usage.sqlite3")
    token = error_records.bind_request_context()
    try:
        response_diagnostics.initialize_response_diagnostics(stream=True)
        response_diagnostics.configure_generation(model="gemini-synthetic", auto_continue=False)
        response_diagnostics.begin_upstream_call()
        parse_usage_metadata([10, 2, 12, 5])
        parse_usage_metadata([10, 3, 13, 5])
        response_diagnostics.begin_upstream_call()
        parse_usage_metadata([20, 4, 24, 8])
        response_diagnostics.finish_response_diagnostics("recovered", 200)
    finally:
        error_records.reset_request_context(token)
    result = request_usage.query_requests()
    assert result["total"] == 1
    assert result["totals"]["prompt_tokens"] == 30
    assert result["totals"]["completion_tokens"] == 7
    assert result["totals"]["cached_tokens"] == 13
    assert result["items"][0]["usage_complete"] == 1


def summary(number=1):
    return {"request_id": f"req_{number:08x}", "model": "gemini-synthetic", "outcome": "success",
            "upstream_calls": 2, "duration_ms": 120, "timings_ms": {"first_body": 20},
            "prompt": "DO-NOT-STORE", "credentials": "DO-NOT-STORE"}


def test_ledger_counts_one_request_and_only_reported_usage(tmp_path, monkeypatch):
    monkeypatch.setattr(request_usage, "database_path", lambda: tmp_path / "usage.sqlite3")
    request_usage.record_request(summary(), {1: {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25, "cached_tokens": 10},
                                           2: {"prompt_tokens": 25, "completion_tokens": 7, "total_tokens": 32}})
    report = request_usage.query_requests()
    assert report["total"] == 1
    assert report["totals"]["prompt_tokens"] == 45 and report["totals"]["completion_tokens"] == 12
    assert report["items"][0]["usage_complete"]
    request_usage.record_request(summary(2), {})
    request_usage.record_request(summary(3), {1: {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})
    rows = request_usage.query_requests()["items"]
    assert rows[0]["prompt_tokens"] == 0 and not rows[0]["usage_complete"]
    assert rows[1]["prompt_tokens"] is None
    assert b"DO-NOT-STORE" not in (tmp_path / "usage.sqlite3").read_bytes()
    assert 'prompt"' not in json.dumps(report)


def test_retention_and_filters_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(request_usage, "database_path", lambda: tmp_path / "usage.sqlite3")
    monkeypatch.setattr(request_usage, "MAX_RECORDS", 2)
    for number in range(1, 5):
        value = summary(number)
        value["model"] = "gemini-synthetic-build" if number == 4 else "gemini-synthetic"
        request_usage.record_request(value, {})
    assert request_usage.query_requests()["total"] == 2
    rows = request_usage.query_requests(model="gemini-synthetic-build")["items"]
    assert len(rows) == 1 and rows[0]["channel"] == "build"
    assert request_usage.query_requests(model="' OR 1=1 --")["total"] == 0


async def test_requests_api_requires_management_bearer(tmp_path, monkeypatch):
    monkeypatch.setattr(request_usage, "database_path", lambda: tmp_path / "absent.sqlite3")
    monkeypatch.setattr(settings, "web_password", "synthetic-admin-password")
    app = FastAPI()
    app.include_router(protected_router, dependencies=[Depends(require_web_auth)])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        assert (await client.get("/requests")).status_code == 401
        assert (await client.get("/requests", headers={"Authorization": "Bearer synthetic-api-key"})).status_code == 401
        response = await client.get("/requests", headers={"Authorization": "Bearer synthetic-admin-password"})
        assert response.status_code == 200 and response.json()["total"] == 0
        assert (await client.get("/requests?days=999", headers={"Authorization": "Bearer synthetic-admin-password"})).status_code == 422
    assert not (tmp_path / "absent.sqlite3").exists()
