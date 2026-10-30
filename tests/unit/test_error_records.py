"""Synthetic recent-diagnostics tests without real requests or credentials."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from aistudio_api.api import routes_errors
from aistudio_api.domain.errors import (
    AccountIdentityMismatchError,
    AccountIdentityUnavailableError,
    AuthError,
    RequestError,
    SessionExpiredError,
    UsageLimitExceeded,
)
from aistudio_api.infrastructure.utils import error_records as records


@pytest.fixture
def store(monkeypatch):
    value = records.ErrorRecordStore()
    monkeypatch.setattr(records, "get_error_record_store", lambda: value)
    return value


def _failure(store, exc=None, **kwargs):
    return store.record(exc or RuntimeError("SYNTHETIC_SECRET"),
                        source="api", operation="generate", **kwargs)


def test_default_window_is_bounded_memory_and_new_process_starts_empty():
    with patch("builtins.open", side_effect=AssertionError("No file access permitted")):
        store = records.ErrorRecordStore()
        event = _failure(store)
        page = store.list_records()
        fresh_store = records.ErrorRecordStore()
        fresh_page = fresh_store.list_records()
    assert page["retention"] == {"max_records": 100, "minutes": 10}
    assert page["items"] == [event]
    assert fresh_page["items"] == []
    assert "persistence" not in page
    assert "persisted" not in event


async def test_asyncio_deadline_and_admission_timeout_are_not_client_cancellation():
    try:
        async with asyncio.timeout(0.001):
            await asyncio.Event().wait()
    except TimeoutError as exc:
        timeout = exc
    assert isinstance(timeout.__cause__, asyncio.CancelledError)
    assert records.classify_failure(timeout)[:2] == (504, "timeout")
    wrapped = HTTPException(503, "Admission queue timed out")
    wrapped.__cause__ = timeout
    assert records.classify_failure(wrapped, http_status=503)[:2] == (503, "timeout")
    assert records.classify_failure(wrapped)[:2] == (503, "timeout")
    assert records.classify_failure(asyncio.CancelledError())[:2] == (499, "cancelled")


def test_exception_messages_request_queries_and_unrecognized_metadata_are_excluded(store):
    exc = RuntimeError("Cookie: SID=SYNTHETIC_SECRET; Authorization=Bearer TEST_TOKEN "
                       "https://synthetic.invalid/?key=QUERY_KEY prompt=PRIVATE_PROMPT")
    _failure(store, exc, model="https://synthetic.invalid/?key=QUERY_KEY",
             account_id="someone@example.invalid")
    event = store.list_records()["items"][0]
    payload = json.dumps(event)
    for secret in ("SYNTHETIC_SECRET", "TEST_TOKEN", "QUERY_KEY", "PRIVATE_PROMPT", "someone@"):
        assert secret not in payload
    assert event["model"] is None
    assert event["account_id"] is None
    assert event["exception_type"] == "RuntimeError"


def test_exception_str_is_not_evaluated(store):
    class SensitiveError(Exception):
        def __str__(self):
            raise AssertionError("Exception message must never be evaluated")

    assert _failure(store, SensitiveError())["exception_type"] == "SensitiveError"


@pytest.mark.parametrize(("exc", "status", "category"), [
    (SessionExpiredError("synthetic"), 401, "session_expired"),
    (AuthError("synthetic"), 401, "authentication"),
    (UsageLimitExceeded("synthetic"), 429, "rate_limit"),
    (TimeoutError("synthetic"), 504, "timeout"),
    (ConnectionRefusedError("synthetic"), 502, "network"),
    (RequestError(503, "synthetic"), 503, "upstream"),
    (RequestError(422, "synthetic"), 422, "validation"),
    (IndexError("synthetic"), 500, "internal"),
    (asyncio.CancelledError(), 499, "cancelled"),
])
def test_error_categories_have_stable_safe_descriptions(store, exc, status, category):
    event = _failure(store, exc)
    assert (event["status"], event["category"]) == (status, category)
    assert "synthetic" not in event["summary"]


@pytest.mark.parametrize(("error_type", "code", "summary"), [
    (AccountIdentityMismatchError, "upstream_account_mismatch", "身份与所选账号不一致"),
    (AccountIdentityUnavailableError, "upstream_account_unavailable", "身份暂时无法核验"),
])
def test_identity_errors_have_distinct_safe_codes_without_evaluating_messages(
    store, error_type, code, summary,
):
    class SensitiveIdentityError(error_type):
        def __str__(self):
            raise AssertionError("Identity diagnostics must not read exception messages")

    event = _failure(store, SensitiveIdentityError("SYNTHETIC_EMAIL_SECRET"))
    assert (event["status"], event["category"], event["code"]) == (503, "authentication", code)
    assert summary in event["summary"]
    assert "SYNTHETIC_EMAIL_SECRET" not in json.dumps(event)


def test_http_wrapper_preserves_typed_identity_and_relative_origin_frame(store):
    from fastapi import HTTPException

    namespace = {"AccountIdentityMismatchError": AccountIdentityMismatchError}
    exec(compile(
        "def prepare():\n    raise AccountIdentityMismatchError('SYNTHETIC_EMAIL_SECRET')\n",
        "/private/SYNTHETIC_PATH_SECRET/aistudio_api/infrastructure/gateway/session_auth.py", "exec",
    ), namespace)
    try:
        try:
            namespace["prepare"]()
        except AccountIdentityMismatchError as original:
            raise HTTPException(503, "SYNTHETIC_HTTP_SECRET") from original
    except HTTPException as wrapped:
        event = _failure(store, wrapped, http_status=503)
    assert event["exception_type"] == "AccountIdentityMismatchError"
    assert event["code"] == "upstream_account_mismatch"
    assert event["frames"] == [{
        "file": "aistudio_api/infrastructure/gateway/session_auth.py", "line": 2, "function": "prepare",
    }]
    assert "SYNTHETIC" not in json.dumps(event)


def test_identity_error_keeps_its_type_when_chained_from_internal_failure(store):
    try:
        try:
            raise RuntimeError("SYNTHETIC_ORIGINAL_SECRET")
        except RuntimeError as original:
            raise AccountIdentityUnavailableError("SYNTHETIC_EMAIL_SECRET") from original
    except AccountIdentityUnavailableError as error:
        event = _failure(store, error)
    assert event["code"] == "upstream_account_unavailable"
    assert event["exception_type"] == "AccountIdentityUnavailableError"
    assert "SYNTHETIC" not in json.dumps(event)


def test_arbitrary_exception_codes_cannot_enter_diagnostics(store):
    error = RuntimeError("SYNTHETIC_SECRET")
    error.code = "SYNTHETIC_PRIVATE_CODE"
    event = _failure(store, error)
    assert event["code"] == "internal"
    assert "SYNTHETIC" not in json.dumps(event)


def test_index_error_contains_only_relative_source_locations(store):
    namespace = {}
    exec(compile("def trigger():\n    return [][0]\n", "/private/SYNTHETIC_SECRET/aistudio_api/application/example.py", "exec"), namespace)
    try:
        namespace["trigger"]()
    except IndexError as exc:
        event = _failure(store, exc)
    assert event["frames"] == [{"file": "aistudio_api/application/example.py", "line": 2, "function": "trigger"}]
    assert "索引越界" in event["summary"]
    assert "SYNTHETIC_SECRET" not in json.dumps(event)


def test_context_deduplicates_same_attempt_but_retains_retry_and_terminal(store):
    token = records.bind_request_context("req_ab12cd34")
    try:
        exc = TimeoutError("synthetic")
        first = records.record_failure(exc, source="api", operation="generate", attempt=0, retrying=True)
        duplicate = records.record_failure(exc, source="api", operation="generate", attempt=0, retrying=True)
        assert duplicate["id"] == first["id"]
        assert records.has_recorded_failure() and not records.has_final_failure()
        assert records.is_recorded_exception(exc)
        final = records.record_failure(exc, source="api", operation="generate", attempt=1)
        assert final["id"] != first["id"]
        assert records.has_final_failure()
        assert {event["request_id"] for event in store.list_records()["items"]} == {"req_ab12cd34"}
    finally:
        records.reset_request_context(token)
    assert records.get_request_id() is None


@pytest.mark.asyncio
async def test_child_task_shares_outcome_and_preserves_sse_transport_status(store):
    token = records.bind_request_context()
    try:
        context = records.get_request_context()
        context.transport_status = 200

        async def child():
            records.record_failure(RequestError(503, "synthetic"), source="api", operation="stream", retrying=True)

        await asyncio.create_task(child())
        assert records.has_recorded_failure()
        records.finish_request("recovered", 200)
        event = store.list_records()["items"][0]
        assert (event["status"], event["transport_status"], event["outcome"]) == (503, 200, "recovered")
    finally:
        records.reset_request_context(token)


def test_management_warning_does_not_mark_request_failed(store):
    token = records.bind_request_context()
    try:
        records.record_failure(RuntimeError("synthetic"), source="management_warning", operation="import")
        assert records.has_recorded_failure()
        assert not records.has_final_failure()
        assert records.get_request_context().warning
        records.finish_request("warning", 200)
        event = store.list_records()["items"][0]
        assert event["outcome"] == "warning"
        assert (event["status"], event["transport_status"], event["category"]) == (503, 200, "browser")
        assert "登录凭据已保存，但浏览器注入失败" in event["summary"]
    finally:
        records.reset_request_context(token)


def test_capacity_filtering_and_pagination():
    store = records.ErrorRecordStore(max_records=4)
    ids = [_failure(store, RequestError(429 if index % 2 else 503))["id"] for index in range(7)]
    page = store.list_records(limit=2)
    assert page["total"] == 4
    assert [event["id"] for event in page["items"]] == list(reversed(ids[-2:]))
    assert page["has_more"]
    second = store.list_records(limit=2, cursor=page["next_cursor"])
    assert second["total"] == 4
    assert not second["has_more"]
    assert not {item["id"] for item in page["items"]} & {item["id"] for item in second["items"]}
    assert all(item["status"] == 429 for item in store.list_records(status=429)["items"])
    assert all(item["category"] == "upstream" for item in store.list_records(category="upstream")["items"])


def test_ten_minute_window_expires_old_entries_before_reads_and_new_records():
    clock = [datetime(2026, 10, 18, 12, 0, tzinfo=UTC)]
    store = records.ErrorRecordStore(clock=lambda: clock[0])
    first = _failure(store)
    clock[0] += timedelta(minutes=9)
    recent = _failure(store)
    clock[0] += timedelta(minutes=2)
    assert [event["id"] for event in store.list_records()["items"]] == [recent["id"]]
    assert first["id"] != recent["id"]
    clock[0] += timedelta(minutes=11)
    last = _failure(store)
    assert store.list_records()["items"] == [last]


def test_default_capacity_cannot_grow_under_repeated_errors(store):
    ids = [_failure(store)["id"] for _ in range(140)]
    page = store.list_records(limit=100)
    assert page["total"] == 100
    assert [event["id"] for event in page["items"]] == list(reversed(ids[-100:]))


def test_unavailable_diagnostics_cannot_replace_original_error(monkeypatch):
    def unavailable():
        raise RuntimeError("SYNTHETIC_SECRET")

    token = records.bind_request_context()
    try:
        monkeypatch.setattr(records, "get_error_record_store", unavailable)
        assert records.record_failure(RuntimeError(), source="api", operation="generate") == {"recorded": False}
        records.finish_request("failed", 500)
    finally:
        records.reset_request_context(token)


def test_concurrent_thread_writes_are_complete_and_bounded(store):
    with ThreadPoolExecutor(max_workers=5) as pool:
        events = list(pool.map(lambda _: _failure(store), range(35)))
    page = store.list_records(limit=100)
    assert page["total"] == 35
    assert {event["id"] for event in events} == {event["id"] for event in page["items"]}


@pytest.mark.asyncio
async def test_errors_route_accepts_filters_and_rejects_unbounded_queries(store, monkeypatch):
    _failure(store, RequestError(503))
    _failure(store, RequestError(429))
    monkeypatch.setattr(routes_errors, "get_error_record_store", lambda: store)
    app = FastAPI()
    app.include_router(routes_errors.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://synthetic") as client:
        response = await client.get("/errors", params={"status": 429, "category": "rate_limit", "limit": 1})
        assert response.status_code == 200
        assert response.json()["total"] == 1
        assert response.json()["items"][0]["status"] == 429
        assert response.json()["retention"] == {"minutes": 10, "max_records": 100}
        for params in ({"limit": 101}, {"status": 200}, {"cursor": "bad"}):
            assert (await client.get("/errors", params=params)).status_code == 422
        assert (await client.get("/errors", params={"category": "unknown"})).status_code == 400
