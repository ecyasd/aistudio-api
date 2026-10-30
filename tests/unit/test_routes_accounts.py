"""Tests for accounts routes, cache isolation headers, and /api/accounts prefix."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from aistudio_api.api.app import app
from aistudio_api.application.account_service import AccountService
from aistudio_api.application.runtime import runtime_state
from aistudio_api.config import settings
from aistudio_api.infrastructure.account.account_store import AccountMeta


@pytest.fixture(autouse=True)
def setup_mock_account_service(monkeypatch):
    monkeypatch.setattr(settings, "web_password", "synthetic-management-only")
    mock_svc = MagicMock(spec=AccountService)
    sample_acc = AccountMeta(
        id="acc_1",
        name="Test Account 1",
        email="test1@gmail.com",
        created_at="2026-10-17T00:00:00Z",
        last_used=None,
        auth_user="0",
        cookie_id="cookie_1",
    )
    mock_svc.list_accounts.return_value = [sample_acc]
    mock_svc.get_active_account.return_value = sample_acc

    orig = runtime_state.account_service
    runtime_state.account_service = mock_svc
    try:
        yield mock_svc
    finally:
        runtime_state.account_service = orig


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
        headers={"Authorization": "Bearer synthetic-management-only"},
    )


async def test_no_active_account_is_normal_empty_state_without_error_pollution(setup_mock_account_service, monkeypatch):
    from aistudio_api.infrastructure.utils import error_records

    store = error_records.ErrorRecordStore()
    monkeypatch.setattr(error_records, "get_error_record_store", lambda: store)
    setup_mock_account_service.get_active_account.return_value = None
    async with _client() as client:
        for _ in range(3):
            response = await client.get("/api/accounts/active")
            assert response.status_code == 200
            assert response.json() is None
    assert not store.list_records()["items"], "routine empty-account polling is not a generation error"


@pytest.mark.parametrize("created_at", ["2026-10-30T12:34:56Z", "2026-10-30 12:34:56", ""])
async def test_legacy_group_identity_matches_listing_and_deletion(setup_mock_account_service, monkeypatch, created_at):
    from urllib.parse import quote

    from aistudio_api.api.routes_accounts import to_account_response

    account = AccountMeta(id="legacy", name="synthetic", email=None, created_at=created_at)
    service = setup_mock_account_service
    service.list_accounts.return_value = [account]
    service.get_active_account.return_value = None
    service.delete_account.return_value = True
    monkeypatch.setattr(runtime_state, "rotator", None)
    monkeypatch.setattr(runtime_state, "client", None)
    cookie_id = to_account_response(account).cookie_id
    async with _client() as client:
        response = await client.delete("/api/accounts/group/" + quote(cookie_id, safe=""))
    assert response.status_code == 200
    assert response.json() == {"deleted": 1}
    service.delete_account.assert_called_once_with("legacy")


@pytest.mark.asyncio
async def test_accounts_html_navigation_headers():
    """Verify that visiting /accounts as HTML returns the SPA HTML with cache isolation headers."""
    async with _client() as client:
        for path in ("/accounts", "/accounts/"):
            resp = await client.get(path, headers={"Accept": "text/html,application/xhtml+xml"})
            assert resp.status_code == 200
            assert "<!DOCTYPE html>" in resp.text
            assert "text/html" in resp.headers.get("Content-Type", "")
            assert "Accept" in resp.headers.get("Vary", "")
            assert "no-store" in resp.headers.get("Cache-Control", "")


@pytest.mark.asyncio
async def test_accounts_json_api_headers():
    """Verify that calling /accounts with Accept: application/json returns JSON with cache isolation headers."""
    async with _client() as client:
        resp = await client.get("/accounts", headers={"Accept": "application/json"})
        assert resp.status_code == 200
        assert "application/json" in resp.headers.get("Content-Type", "")
        assert "Accept" in resp.headers.get("Vary", "")
        assert "no-store" in resp.headers.get("Cache-Control", "")
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["id"] == "acc_1"


@pytest.mark.asyncio
async def test_api_accounts_endpoint_headers():
    """Verify that the dedicated /api/accounts endpoint returns JSON with cache isolation headers."""
    async with _client() as client:
        for path in ("/api/accounts", "/api/accounts/"):
            resp = await client.get(path, headers={"Accept": "application/json"})
            assert resp.status_code == 200
            assert "application/json" in resp.headers.get("Content-Type", "")
            assert "Accept" in resp.headers.get("Vary", "")
            assert "no-store" in resp.headers.get("Cache-Control", "")
            data = resp.json()
            assert isinstance(data, list)
            assert len(data) == 1
            assert data[0]["id"] == "acc_1"


@pytest.mark.asyncio
async def test_api_accounts_and_legacy_accounts_parity():
    """Verify that /api/accounts and /accounts return identical payload for JSON clients."""
    async with _client() as client:
        resp_legacy = await client.get("/accounts", headers={"Accept": "application/json"})
        resp_api = await client.get("/api/accounts", headers={"Accept": "application/json"})
        assert resp_legacy.status_code == 200
        assert resp_api.status_code == 200
        assert resp_legacy.json() == resp_api.json()


@pytest.mark.asyncio
async def test_issue_5_cache_isolation_regression():
    """Regression test for Issue #5:
    Ensure that a client requesting HTML followed by JSON (or vice-versa) receives distinct
    content types and both responses carry Vary: Accept and Cache-Control: no-store headers.
    """
    async with _client() as client:
        html_resp = await client.get("/accounts", headers={"Accept": "text/html"})
        assert html_resp.status_code == 200
        assert "<!DOCTYPE html>" in html_resp.text
        assert "Accept" in html_resp.headers.get("Vary", "")
        assert "no-store" in html_resp.headers.get("Cache-Control", "")

        json_resp = await client.get("/accounts", headers={"Accept": "application/json"})
        assert json_resp.status_code == 200
        assert "application/json" in json_resp.headers.get("Content-Type", "")
        assert "Accept" in json_resp.headers.get("Vary", "")
        assert "no-store" in json_resp.headers.get("Cache-Control", "")
        assert isinstance(json_resp.json(), list)


@pytest.mark.asyncio
async def test_api_accounts_active_endpoint():
    """Verify subroutes under /api/accounts like /api/accounts/active work."""
    async with _client() as client:
        resp = await client.get("/api/accounts/active", headers={"Accept": "application/json"})
        assert resp.status_code == 200
        assert "Accept" in resp.headers.get("Vary", "")
        assert "no-store" in resp.headers.get("Cache-Control", "")
        data = resp.json()
        assert data["id"] == "acc_1"


@pytest.mark.asyncio
async def test_detect_email_uses_isolated_target_when_browser_is_cold(
    setup_mock_account_service, monkeypatch
):
    service = setup_mock_account_service
    account = service.list_accounts.return_value[0]
    service.get_account.return_value = account
    service.get_account_auth_path.return_value = Path("synthetic/target/auth.json")
    session = SimpleNamespace(
        page=None,
        _auth_file="synthetic/active/auth.json",
        detect_account_email=AsyncMock(return_value="detected@example.com"),
        switch_auth=AsyncMock(),
    )
    monkeypatch.setattr(runtime_state, "client", SimpleNamespace(_session=session))

    async with _client() as client:
        response = await client.post("/api/accounts/acc_1/detect-email")

    assert response.status_code == 200
    session.detect_account_email.assert_awaited_once_with(
        str(Path("synthetic/target/auth.json"))
    )
    session.switch_auth.assert_not_awaited()
    service.set_active_account.assert_not_called()
    service.update_account_email.assert_called_once_with("acc_1", "detected@example.com")
    assert session._auth_file == "synthetic/active/auth.json"


@pytest.mark.asyncio
async def test_detect_email_missing_auth_does_not_use_active_account(
    setup_mock_account_service, monkeypatch
):
    service = setup_mock_account_service
    service.get_account.return_value = service.list_accounts.return_value[0]
    service.get_account_auth_path.return_value = None
    session = SimpleNamespace(detect_account_email=AsyncMock())
    monkeypatch.setattr(runtime_state, "client", SimpleNamespace(_session=session))

    async with _client() as client:
        response = await client.post("/api/accounts/acc_1/detect-email")

    assert response.status_code == 404
    session.detect_account_email.assert_not_awaited()
    service.update_account_email.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("file_path", ["/etc/private.json", ""])
async def test_import_bundle_rejects_server_paths_even_with_inline_content(
    setup_mock_account_service, monkeypatch, file_path
):
    def forbidden_read(*args, **kwargs):
        pytest.fail("import-bundle must not read any server file")

    monkeypatch.setattr(Path, "read_text", forbidden_read)
    async with _client() as client:
        response = await client.post(
            "/api/accounts/import-bundle",
            json={"file_path": file_path, "content": "[]"},
        )

    assert response.status_code == 400
    assert "file_path" in response.text
    setup_mock_account_service.save_account_from_cookies.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload_field", ["accounts", "content"])
async def test_import_bundle_keeps_inline_imports(
    setup_mock_account_service, monkeypatch, payload_field
):
    service = setup_mock_account_service
    service.save_account_from_cookies.return_value = service.list_accounts.return_value[0]
    monkeypatch.setattr(runtime_state, "rotator", None)
    accounts = [
        {
            "name": "Synthetic account",
            "cookies": json.dumps(
                [{"name": "SAPISID", "value": "synthetic", "domain": ".google.com"}]
            ),
        }
    ]
    payload = {payload_field: accounts if payload_field == "accounts" else json.dumps(accounts)}
    async with _client() as client:
        response = await client.post("/api/accounts/import-bundle", json=payload)

    assert response.status_code == 200
    assert response.json()["imported_count"] == 1
    service.save_account_from_cookies.assert_called_once()


@pytest.mark.parametrize("bad_index", [1, 2])
@pytest.mark.parametrize("field", ["accounts", "content"])
async def test_bundle_preflights_every_cookie_before_saving(
    setup_mock_account_service, monkeypatch, bad_index, field
):
    from aistudio_api.infrastructure.account import cookie_parser

    service = setup_mock_account_service
    monkeypatch.setattr(runtime_state, "rotator", None)
    parsed = []

    def parse(raw):
        parsed.append(raw)
        if raw == "invalid":
            raise ValueError("synthetic private cookie detail")
        return {"cookies": [{"name": "SAPISID", "value": "synthetic", "domain": ".google.com"}]}

    monkeypatch.setattr(cookie_parser, "parse_cookie_string", parse)
    items = [{"name": "synthetic", "cookies": "valid"} for _ in range(3)]
    items[bad_index]["cookies"] = "invalid"
    payload = {field: json.dumps(items) if field == "content" else items}
    async with _client() as client:
        response = await client.post("/api/accounts/import-bundle", json=payload)
    assert response.status_code == 400
    assert str(bad_index + 1) in response.json()["error"]["message"]
    assert "private cookie" not in response.text
    assert len(parsed) == bad_index + 1
    service.save_account_from_cookies.assert_not_called()
    service.set_active_account.assert_not_called()
