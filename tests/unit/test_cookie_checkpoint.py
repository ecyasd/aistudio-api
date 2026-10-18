"""Synthetic Cookie rotations, checkpoint integrity, and import races."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from aistudio_api.infrastructure.account.account_store import _atomic_write_json
from aistudio_api.infrastructure.account.cookie_parser import (
    calculate_sapisid_hash,
    parse_cookie_string,
    parse_raw_cookies,
)
from aistudio_api.infrastructure.account.storage_state import atomic_write_json
from aistudio_api.infrastructure.browser.cdp_client import CDPPage
from aistudio_api.infrastructure.gateway.session_auth import (
    bind_auth_file,
    load_cookies_to_page,
    navigate_to_aistudio,
    save_browser_cookies,
    verify_account_identity,
)
from aistudio_api.infrastructure.gateway.transport import _ensure_authorization_header


def _cookies(generation="original"):
    return [
        {"name": "SID", "value": f"synthetic-{generation}", "domain": ".google.com", "path": "/"},
        {"name": "SAPISID", "value": f"synthetic-sign-{generation}", "domain": ".google.com", "path": "/"},
        {"name": "LSID", "value": "synthetic-accounts-only", "domain": "accounts.google.com", "path": "/"},
    ]


def _auth(tmp_path):
    path = tmp_path / "synthetic-account" / "auth.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"cookies": _cookies(), "origins": [{"origin": "https://example.invalid", "localStorage": []}]}))
    (path.parent / "meta.json").write_text(json.dumps({"id": "synthetic", "auth_user": "0", "email": "owner@example.invalid"}))
    return path


def _page(cookies=None):
    return SimpleNamespace(
        url="https://aistudio.google.com/prompts/new_chat",
        is_closed=lambda: False,
        evaluate=AsyncMock(return_value="owner@example.invalid"),
        get_cookies=AsyncMock(return_value=cookies or _cookies("rotated")),
        clear_cookies=AsyncMock(),
        set_cookies=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_full_context_cookie_read_keeps_accounts_only_domains():
    browser = SimpleNamespace(send=AsyncMock(return_value={"cookies": _cookies()}))
    target = SimpleNamespace(send=AsyncMock())
    page = CDPPage(target, "synthetic-target", "synthetic-context", browser)
    assert await page.get_cookies() == _cookies()
    browser.send.assert_awaited_once_with("Storage.getCookies", {"browserContextId": "synthetic-context"})
    target.send.assert_not_called()


@pytest.mark.asyncio
async def test_signature_uses_rotated_jar_even_with_populated_old_cache():
    page = _page()
    page._cached_cookies = {"SAPISID": "synthetic-obsolete"}
    result = await _ensure_authorization_header(page, {}, "2")
    expected = calculate_sapisid_hash({"SAPISID": "synthetic-sign-rotated"})
    assert result["Authorization"] == expected
    assert result["X-Goog-AuthUser"] == "2"
    page.get_cookies.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_refresh_does_not_rehash_an_unbounded_stale_cache():
    page = _page()
    page._cached_cookies = {"SAPISID": "synthetic-obsolete"}
    page.get_cookies.side_effect = TimeoutError("synthetic CDP failure")
    assert "Authorization" not in await _ensure_authorization_header(page, {})


@pytest.mark.asyncio
async def test_verified_checkpoint_preserves_complete_jar_and_origins(tmp_path):
    auth = _auth(tmp_path)
    page = _page()
    assert await load_cookies_to_page(page, str(auth))
    assert await save_browser_cookies(page, str(auth))
    saved = json.loads(auth.read_text())
    assert saved["cookies"] == _cookies("rotated")
    assert saved["origins"][0]["origin"] == "https://example.invalid"
    # A second rotation updates the page's expected version after its own save.
    page.get_cookies.return_value = _cookies("rotated-twice")
    assert await save_browser_cookies(page, str(auth))
    assert json.loads(auth.read_text())["cookies"] == _cookies("rotated-twice")


@pytest.mark.asyncio
async def test_verified_full_jar_can_retire_cookie_and_rotate_its_family(tmp_path):
    auth = _auth(tmp_path)
    page = _page()
    assert bind_auth_file(page, str(auth))
    replacement = [
        {"name": "__Secure-1PSID", "value": "synthetic-new-sid", "domain": ".google.com"},
        {"name": "__Secure-1PAPISID", "value": "synthetic-new-api", "domain": ".google.com"},
    ]
    page.get_cookies.return_value = replacement
    assert await save_browser_cookies(page, str(auth))
    assert json.loads(auth.read_text())["cookies"] == replacement


@pytest.mark.asyncio
async def test_checkpoint_cannot_replace_newer_reimport(tmp_path):
    auth = _auth(tmp_path)
    page = _page()
    assert await load_cookies_to_page(page, str(auth))
    newer = {"cookies": _cookies("new-import"), "origins": []}

    async def import_during_snapshot():
        _atomic_write_json(auth, newer)
        return _cookies("old-page-rotation")

    page.get_cookies.side_effect = import_during_snapshot
    assert not await save_browser_cookies(page, str(auth))
    assert json.loads(auth.read_text()) == newer


@pytest.mark.asyncio
async def test_checkpoint_failure_preserves_original_complete_file(tmp_path, monkeypatch):
    auth = _auth(tmp_path)
    page = _page()
    assert bind_auth_file(page, str(auth))
    before = auth.read_bytes()

    def fail_replace(*args):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr("aistudio_api.infrastructure.account.storage_state.Path.replace", fail_replace)
    assert not await save_browser_cookies(page, str(auth))
    assert auth.read_bytes() == before
    assert list(auth.parent.glob(".auth.json.*")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot", [[], [{"name": "NID", "value": "synthetic-telemetry"}], [{"name": "SID", "value": "synthetic-partial"}]])
async def test_empty_or_incomplete_snapshot_cannot_destroy_credentials(tmp_path, snapshot):
    auth = _auth(tmp_path)
    page = _page()
    page.get_cookies.return_value = snapshot
    assert bind_auth_file(page, str(auth))
    before = auth.read_bytes()
    assert not await save_browser_cookies(page, str(auth))
    assert auth.read_bytes() == before


@pytest.mark.asyncio
async def test_page_cannot_checkpoint_another_accounts_file(tmp_path):
    auth = _auth(tmp_path)
    other = tmp_path / "another-auth.json"
    other.write_bytes(auth.read_bytes())
    page = _page()
    assert bind_auth_file(page, str(auth))
    before = other.read_bytes()
    assert not await save_browser_cookies(page, str(other))
    assert other.read_bytes() == before


@pytest.mark.asyncio
async def test_login_or_wrong_identity_cannot_replace_cookie_export(tmp_path):
    auth = _auth(tmp_path)
    page = _page()
    assert bind_auth_file(page, str(auth))
    before = auth.read_bytes()
    page.url = "https://accounts.google.com/signin"
    assert not await save_browser_cookies(page, str(auth))
    page.url = "https://aistudio.google.com/prompts/new_chat"
    page.evaluate.return_value = "other@example.invalid"
    assert not await save_browser_cookies(page, str(auth))
    assert auth.read_bytes() == before


@pytest.mark.asyncio
async def test_known_email_cannot_be_verified_by_route_only(tmp_path):
    auth = _auth(tmp_path)
    page = _page()
    page.evaluate.side_effect = [None, False]
    assert not await verify_account_identity(page, str(auth))
    assert page.evaluate.call_args.args[1]["authUser"] == ""


@pytest.mark.asyncio
async def test_reinject_requires_navigation_and_actual_non_login_result(monkeypatch):
    page = _page()
    page.url = "https://accounts.google.com/signin"
    page.evaluate.side_effect = lambda expression, *args, **kwargs: page.url if "location.href" in expression else True
    page.goto = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    reload = AsyncMock(return_value=True)
    monkeypatch.setattr("aistudio_api.infrastructure.gateway.session_auth.load_cookies_to_page", reload)
    from aistudio_api.domain.errors import SessionExpiredError

    with pytest.raises(SessionExpiredError):
        await navigate_to_aistudio(page, auth_file="synthetic-not-read")
    assert page.goto.await_count == 2


def test_netscape_http_only_prefix_is_a_cookie_record():
    parsed = parse_raw_cookies("#HttpOnly_.google.com\tTRUE\t/\tTRUE\t1800000000\tSID\tsynthetic-only\n# ordinary comment")
    assert parsed == {"SID": "synthetic-only"}


def test_structured_export_retains_real_scope_flags_and_expiration():
    raw = [
        {"name": "SID", "value": "synthetic-root", "domain": ".google.com", "path": "/", "expirationDate": 4102444800, "httpOnly": True, "sameSite": "lax", "secure": True},
        {"name": "SID", "value": "synthetic-host", "domain": "accounts.google.com", "path": "/signin", "session": True, "expires": 4102444800, "httpOnly": True},
        {"name": "__Host-GAPS", "value": "synthetic-host-only", "domain": "accounts.google.com", "path": "/", "secure": True, "expires": -1},
    ]
    cookies = parse_cookie_string(json.dumps(raw))["cookies"]
    assert len(cookies) == 3
    assert cookies[0]["domain"] == ".google.com"
    assert cookies[0]["expires"] == 4102444800
    assert cookies[0]["httpOnly"] is True
    assert cookies[0]["sameSite"] == "Lax"
    assert cookies[1]["path"] == "/signin"
    assert "expires" not in cookies[1]
    assert "expires" not in cookies[2]


def test_netscape_preserves_host_scope_http_only_and_session_semantics():
    cookies = parse_cookie_string(
        "#HttpOnly_accounts.google.com\tFALSE\t/signin\tTRUE\t0\tLSID\tsynthetic-login\n"
        ".google.com\tTRUE\t/\tTRUE\t4102444800\tSID\tsynthetic-root"
    )["cookies"]
    assert cookies[0]["domain"] == "accounts.google.com"
    assert cookies[0]["path"] == "/signin"
    assert cookies[0]["httpOnly"] is True
    assert "expires" not in cookies[0]
    assert cookies[1]["expires"] == 4102444800


def test_header_without_expiry_does_not_invent_180_days():
    assert "expires" not in parse_cookie_string("SID=synthetic-header")["cookies"][0]


def test_expired_exported_auth_cookie_is_reported_without_secret():
    with pytest.raises(ValueError, match="已过期") as error:
        parse_cookie_string(json.dumps([{"name": "SID", "value": "synthetic-sensitive-marker", "domain": ".google.com", "expires": 1}]))
    assert "synthetic-sensitive-marker" not in str(error.value)


def test_structured_import_does_not_flatten_through_http_refresh(monkeypatch):
    from aistudio_api.infrastructure.account.cookie_refresher import (
        load_cookies_from_string,
    )

    refresh = MagicMock(side_effect=AssertionError("structured import must use the browser"))
    monkeypatch.setattr("aistudio_api.infrastructure.account.cookie_refresher._refresh_session_cookies", refresh)
    cookies = load_cookies_from_string(json.dumps([{"name": "SID", "value": "synthetic", "domain": ".google.com", "expires": 4102444800}]))
    assert cookies[0]["expires"] == 4102444800
    refresh.assert_not_called()


@pytest.mark.asyncio
async def test_cdp_reinjection_omits_server_only_attributes_and_session_expiry():
    target = SimpleNamespace(send=AsyncMock())
    page = CDPPage(target, "synthetic")
    await page.set_cookies([{"name": "SID", "value": "synthetic", "domain": ".google.com", "path": "/", "expires": -1, "session": True, "size": 200, "priority": "High", "sourcePort": 443, "sourceScheme": "Secure", "sameParty": False, "partitionKey": {"topLevelSite": "https://google.com", "hasCrossSiteAncestor": False}}])
    cookie = target.send.call_args.args[1]["cookies"][0]
    assert not ({"expires", "session", "size", "priority", "sourcePort", "sourceScheme", "sameParty"} & cookie.keys())
    assert cookie["partitionKey"] == {"topLevelSite": "https://google.com", "hasCrossSiteAncestor": False}


@pytest.mark.asyncio
async def test_signature_ignores_other_hosts_paths_and_expired_duplicate_names():
    page = _page()
    page.get_cookies.return_value = [
        *_cookies(),
        {"name": "SAPISID", "value": "synthetic-wrong-host", "domain": "accounts.google.com", "path": "/"},
        {"name": "SAPISID", "value": "synthetic-wrong-path", "domain": ".google.com", "path": "/unrelated"},
        {"name": "SAPISID", "value": "synthetic-expired", "domain": "aistudio.google.com", "expires": 1},
    ]
    result = await _ensure_authorization_header(page, {})
    assert result["Authorization"] == calculate_sapisid_hash({"SAPISID": "synthetic-sign-original"})


@pytest.mark.asyncio
async def test_account_probe_stops_at_local_cap_when_all_indices_return_403(monkeypatch):
    from aistudio_api.infrastructure.account.cookie_parser import (
        probe_google_accounts_infinite,
    )

    client = SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(status_code=403)))

    class FakeClient:
        async def __aenter__(self):
            return client

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr("httpx.AsyncClient", lambda **kwargs: FakeClient())
    result = await probe_google_accounts_infinite("SID=synthetic-only", max_accounts=3)
    assert [item["auth_user"] for item in result] == ["0", "1", "2"]
    assert client.post.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 257])
async def test_account_probe_rejects_invalid_local_cap(limit):
    from aistudio_api.infrastructure.account.cookie_parser import (
        probe_google_accounts_infinite,
    )

    with pytest.raises(ValueError, match="探测范围"):
        await probe_google_accounts_infinite("SID=synthetic", max_accounts=limit)


def test_compare_and_swap_rejects_stale_version(tmp_path):
    target = tmp_path / "synthetic.json"
    atomic_write_json(target, {"generation": "new-import"})
    before = target.read_bytes()
    assert not atomic_write_json(target, {"generation": "old-browser"}, expected_version="outdated")
    assert target.read_bytes() == before


@pytest.mark.asyncio
async def test_bundle_content_accepts_native_cookie_array():
    from aistudio_api.api.routes_accounts import ImportBundleRequest, import_bundle
    from aistudio_api.infrastructure.account.account_store import AccountMeta

    service = MagicMock()
    service.save_account_from_cookies.return_value = AccountMeta(
        id="synthetic", name="Synthetic", email=None, created_at="2026-01-01", auth_user="0"
    )
    result = await import_bundle(
        ImportBundleRequest(content=json.dumps({"accounts": [{"cookies": _cookies()}]})),
        account_service=service,
        runtime_state=SimpleNamespace(rotator=None),
    )
    assert result.imported_count == 1
    saved = service.save_account_from_cookies.call_args.kwargs["storage_state"]
    assert {cookie["name"] for cookie in saved["cookies"]} == {"SID", "SAPISID", "LSID"}
