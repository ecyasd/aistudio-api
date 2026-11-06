"""Offline regressions for exported credentials and account import side effects."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from aistudio_api.api import routes_accounts
from aistudio_api.api.state import RuntimeState
from aistudio_api.application.account_service import AccountService
from aistudio_api.infrastructure.account import cookie_formats, cookie_parser
from aistudio_api.infrastructure.account.account_store import AccountStore
from aistudio_api.infrastructure.account.cookie_formats import CookieImportError
from aistudio_api.infrastructure.account.credential_import import (
    normalize_account_import,
    normalize_bundle_import,
)
from aistudio_api.infrastructure.gateway.client import AIStudioClient

COOKIE_VALUES = {"SID": "fixture-session", "SAPISID": "fixture-signing"}
COOKIE_RECORDS = [
    {"name": key, "value": value, "domain": ".google.com"}
    for key, value in COOKIE_VALUES.items()
]


@pytest.mark.parametrize(
    "document",
    [
        "Cookie: SID=fixture-session; SAPISID=fixture-signing",
        "SID=fixture-session\nSAPISID=fixture-signing",
        COOKIE_VALUES,
        COOKIE_RECORDS,
        {"cookies": COOKIE_VALUES},
        {"cookies": COOKIE_RECORDS, "origins": []},
        {"cookie": "SID=fixture-session; SAPISID=fixture-signing"},
        {"storage_state": {"cookies": COOKIE_RECORDS}},
        {"storageState": {"cookies": COOKIE_RECORDS}},
        "#HttpOnly_.google.com\tTRUE\t/\tTRUE\t0\tSID\tfixture-session\n.google.com\tTRUE\t/\tTRUE\t0\tSAPISID\tfixture-signing",
    ],
)
def test_supported_export_formats_share_one_normalizer(document):
    item = normalize_account_import(document)
    cookies = item.storage_state["cookies"]
    assert isinstance(cookies, list)
    assert {cookie["name"]: cookie["value"] for cookie in cookies} == COOKIE_VALUES
    assert item.cookie_count == 2
    assert item.auth_user == "0"


def test_native_pairing_payload_extracts_supported_metadata_only():
    document = {
        "id": "fixture-account",
        "label": "Imported fixture",
        "auth_user": 2,
        "g1Tier": "TIER0",
        "timezone": "Asia/Tokyo",
        "fingerprint": {"config": {"example": "fixture-fingerprint"}},
        "storage_state": {
            "cookies": COOKIE_RECORDS,
            "origins": [
                {
                    "origin": "https://example.com",
                    "localStorage": [
                        {"name": "fixture", "value": "fixture-local-storage"}
                    ],
                }
            ],
            "aistudio2api": {
                "source": {"email": "Fixture@Example.com"},
                "oauth": {"refresh_token": "fixture-oauth"},
            },
        },
    }
    item = normalize_account_import("\ufeff" + json.dumps(document))
    assert (item.account_id, item.name, item.email, item.auth_user, item.tier) == (
        "fixture-account",
        "Imported fixture",
        "fixture@example.com",
        "2",
        "Plus",
    )
    saved = json.dumps(item.storage_state)
    assert set(item.storage_state) == {"cookies"}
    assert all(
        marker not in saved
        for marker in ("fixture-oauth", "fixture-fingerprint", "fixture-local-storage")
    )
    assert (
        normalize_account_import(document, overrides={"auth_user": "0"}).auth_user
        == "0"
    )


def test_cookie_scope_expiry_and_conflicts_are_checked_before_flattening(monkeypatch):
    monkeypatch.setattr(cookie_formats.time, "time", lambda: 1000)
    records = [
        {"name": "SAPISID", "value": "fixture-other-site", "domain": "evilgoogle.com"},
        {
            "name": "SAPISID",
            "value": "fixture-suffix",
            "domain": "google.com.example.com",
        },
        {
            "name": "SAPISID",
            "value": "fixture-login-only",
            "domain": "accounts.google.com",
        },
        {
            "name": "SAPISID",
            "value": "fixture-host-only",
            "domain": "google.com",
            "hostOnly": True,
        },
        {
            "name": "SAPISID",
            "value": "fixture-wrong-path",
            "domain": ".google.com",
            "path": "/accounts",
        },
        {
            "name": "SAPISID",
            "value": "fixture-expired",
            "domain": ".google.com",
            "expirationDate": 999,
        },
        {
            "name": "SAPISID",
            "value": "fixture-partitioned",
            "domain": ".google.com",
            "partitionKey": "https://example.com",
        },
        {
            "name": "SAPISID",
            "value": "fixture-good",
            "domain": ".google.com",
            "expires": 1001,
        },
        {
            "name": "SAPISID",
            "value": "fixture-good",
            "domain": ".google.com",
            "expires": -1,
        },
    ]
    assert cookie_parser.parse_raw_cookies(records) == {"SAPISID": "fixture-good"}
    records.append(
        {"name": "SAPISID", "value": "fixture-conflict", "domain": ".google.com"}
    )
    with pytest.raises(CookieImportError, match="冲突") as caught:
        normalize_account_import(records)
    assert "fixture-" not in str(caught.value)


@pytest.mark.parametrize(
    "document",
    [
        '{"cookies": "SAPISID=fixture-private",',
        [{"name": "SAPISID", "value": "fixture-private\r\nInjected: header"}],
        [{"name": "bad:name", "value": "fixture-private"}],
        [{"name": "SAPISID", "value": {"nested": "fixture-private"}}],
        [{"name": "SAPISID", "value": "fixture-private", "expires": "tomorrow"}],
        [{"name": "SAPISID", "value": "fixture-private", "expires": float("inf")}],
        ".google.com\tTRUE\t/\tTRUE\tinvalid\tSAPISID\tfixture-private",
        {"cookies": "SID=fixture-private"},
        {"cookies": COOKIE_VALUES, "auth_user": True},
        {"cookies": COOKIE_VALUES, "authUser": "-1"},
        {"cookies": COOKIE_VALUES, "id": "../escape"},
        {"cookies": COOKIE_VALUES, "id": ".."},
        {"cookies": COOKIE_VALUES, "tier": "unknown"},
    ],
)
def test_invalid_exports_have_actionable_errors_without_values(document):
    with pytest.raises(CookieImportError) as caught:
        normalize_account_import(document)
    assert "fixture-private" not in str(caught.value)


@pytest.mark.parametrize(
    "field,expected",
    [
        ("SAPISID", "SAPISID"),
        ("__Secure-3PAPISID", "__Secure-3PAPISID"),
        ("APISID", "APISID"),
    ],
)
def test_signature_cookie_validation_matches_existing_signer_fallbacks(field, expected):
    item = normalize_account_import({field: "fixture-signing"})
    assert item.storage_state["cookies"] == [
        {"name": expected, "value": "fixture-signing"}
    ]
    assert cookie_parser.calculate_sapisid_hash({field: "fixture-signing"})


@pytest.mark.parametrize("key", ["accounts", "profiles"])
def test_bundle_preserves_structured_cookies_and_aliases(key):
    items = normalize_bundle_import(
        {
            key: [
                {
                    "id": "fixture-one",
                    "label": "One",
                    "cookies": COOKIE_RECORDS,
                    "authUser": 1,
                    "g1Tier": "TIER1",
                },
                {
                    "id": "fixture-two",
                    "name": "Two",
                    "storage_state": {"cookies": COOKIE_VALUES},
                    "auth_user": "2",
                    "tier": "TIER2",
                },
            ]
        }
    )
    assert [
        (item.account_id, item.name, item.auth_user, item.tier) for item in items
    ] == [("fixture-one", "One", "1", "Pro"), ("fixture-two", "Two", "2", "Ultra")]


@pytest.mark.parametrize(
    "document",
    [
        {"accounts": []},
        {"accounts": "invalid"},
        {"accounts": [{"id": "same", "cookies": COOKIE_VALUES}] * 2},
        {"accounts": [{"email": "fixture@example.com", "cookies": COOKIE_VALUES}] * 2},
        {"accounts": [{"cookies": COOKIE_VALUES}] * 101},
    ],
)
def test_invalid_or_duplicate_bundles_are_rejected(document):
    with pytest.raises(CookieImportError):
        normalize_bundle_import(document)


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_endpoint", [False, True])
async def test_bundle_preflight_rejects_everything_before_probe_or_save(
    tmp_path, monkeypatch, legacy_endpoint
):
    service = AccountService(AccountStore(accounts_dir=tmp_path / "accounts"))
    state = RuntimeState(client=None, rotator=None)
    email, tier = AsyncMock(), AsyncMock()
    monkeypatch.setattr(routes_accounts, "probe_account_email", email)
    monkeypatch.setattr(routes_accounts, "probe_account_benefit_tier", tier)
    content = json.dumps(
        {"accounts": [{"cookies": COOKIE_RECORDS}, {"cookies": "SID=fixture-private"}]}
    )
    with pytest.raises(HTTPException) as caught:
        if legacy_endpoint:
            await routes_accounts.import_cookies(
                routes_accounts.ImportCookiesRequest(cookies=content), service, state
            )
        else:
            await routes_accounts.import_bundle(
                routes_accounts.ImportBundleRequest(content=content), service, state
            )
    assert caught.value.status_code == 400
    assert "第 2 个账号" in caught.value.detail
    assert "fixture-private" not in caught.value.detail
    assert service.list_accounts() == []
    email.assert_not_awaited()
    tier.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["single", "bundle", "probe"])
async def test_reimport_refreshes_active_session_with_in_flight_context_preserved(
    tmp_path, monkeypatch, entrypoint
):
    store = AccountStore(accounts_dir=tmp_path / "accounts")
    service = AccountService(store)
    account = store.save_account(
        name="Existing",
        email="fixture@example.com",
        storage_state={"cookies": [{"name": "SAPISID", "value": "fixture-old"}]},
    )
    client = AIStudioClient()
    try:
        await service.activate_account(account.id, client.session)
        original = await client.session.ensure_ready()
        state = RuntimeState(client=client, rotator=None)
        payload = {
            "id": account.id,
            "name": "Updated",
            "email": "fixture@example.com",
            "cookies": COOKIE_RECORDS,
        }
        if entrypoint == "single":
            await routes_accounts.import_cookies(
                routes_accounts.ImportCookiesRequest(
                    cookies=json.dumps(payload), probe_identity=False
                ),
                service,
                state,
            )
        elif entrypoint == "bundle":
            await routes_accounts.import_bundle(
                routes_accounts.ImportBundleRequest(
                    accounts=[routes_accounts.BundleAccountItem(**payload)],
                    probe_identity=False,
                ),
                service,
                state,
            )
        else:
            monkeypatch.setattr(
                cookie_parser,
                "probe_google_accounts_infinite",
                AsyncMock(
                    return_value=[{"auth_user": "0", "email": "fixture@example.com"}]
                ),
            )
            await routes_accounts.probe_and_import(
                routes_accounts.ProbeAndImportRequest(cookies=json.dumps(payload)),
                service,
                state,
            )
        current = await client.session.ensure_ready()
        assert current.account_id == account.id
        assert current.cookies == COOKIE_VALUES
        assert current is not original
        assert original.cookies == {"SAPISID": "fixture-old"}
        assert len(service.list_accounts()) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 403])
async def test_blanket_denial_does_not_create_infinite_identities(monkeypatch, status):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(status, json=[])
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)
    monkeypatch.setattr(
        cookie_parser, "probe_account_emails_map", AsyncMock(return_value={})
    )
    assert await cookie_parser.probe_google_accounts_infinite(COOKIE_VALUES) == []
    assert client.post.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate_email,expected", [(False, 10), (True, 1)])
async def test_identity_discovery_has_hard_limit_and_stops_repeated_email(
    monkeypatch, duplicate_email, expected
):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(200, json=[0])
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)
    emails = {
        str(index): f"fixture-{0 if duplicate_email else index}@example.com"
        for index in range(30)
    }
    monkeypatch.setattr(
        cookie_parser, "probe_account_emails_map", AsyncMock(return_value=emails)
    )
    result = await cookie_parser.probe_google_accounts_infinite(
        COOKIE_VALUES, max_accounts=999
    )
    assert len(result) == expected
    assert client.post.await_count <= 20


def test_tabs_in_header_and_empty_netscape_fields_remain_valid():
    assert cookie_parser.parse_raw_cookies("Cookie:\tSAPISID=fixture-signing") == {
        "SAPISID": "fixture-signing"
    }
    document = ".google.com\tTRUE\t/\tTRUE\t0\tSAPISID\tfixture-signing\n.example.com\tTRUE\t/\tFALSE\t0\tempty\t"
    assert cookie_parser.parse_raw_cookies(document) == {"SAPISID": "fixture-signing"}


@pytest.mark.parametrize(
    "document",
    [
        "\ud800",
        {"name": "\ud800", "cookies": COOKIE_VALUES},
        [{"name": "SAPISID", "value": "fixture-signing", "expires": 10**400}],
        {"cookies": {"SAPISID": "x" * (cookie_formats.MAX_IMPORT_BYTES + 1)}},
    ],
)
def test_invalid_unicode_and_size_overflows_are_safe_import_errors(document):
    with pytest.raises(CookieImportError):
        normalize_account_import(document)


def test_native_pairing_email_label_fallback_is_limited_to_email_labels():
    payload = {
        "label": "Fixture@Example.com",
        "storage_state": {"cookies": COOKIE_RECORDS},
    }
    assert normalize_account_import(payload).email == "fixture@example.com"
    payload["label"] = "Personal browser"
    assert normalize_account_import(payload).email is None


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_id", ["review..second", "linked-account"])
async def test_all_destinations_are_preflighted_before_any_account_is_written(
    tmp_path, invalid_id
):
    directory = tmp_path / "accounts"
    directory.mkdir()
    if invalid_id == "linked-account":
        outside = tmp_path / "outside"
        outside.mkdir()
        (directory / invalid_id).symlink_to(outside, target_is_directory=True)
    service = AccountService(AccountStore(accounts_dir=directory))
    content = json.dumps(
        {
            "accounts": [
                {"id": "first", "cookies": COOKIE_VALUES},
                {"id": invalid_id, "cookies": COOKIE_VALUES},
            ]
        }
    )
    with pytest.raises(HTTPException) as caught:
        await routes_accounts.import_bundle(
            routes_accounts.ImportBundleRequest(content=content, probe_identity=False),
            service,
            RuntimeState(),
        )
    assert caught.value.status_code == 400
    assert service.list_accounts() == []
    assert not (directory / "first").exists()


@pytest.mark.asyncio
async def test_updating_cookie_by_id_preserves_omitted_login_index(tmp_path):
    store = AccountStore(accounts_dir=tmp_path / "accounts")
    account = store.save_account(
        name="Saved",
        email="fixture@example.com",
        storage_state={"cookies": []},
        auth_user="3",
    )
    response = await routes_accounts.import_cookies(
        routes_accounts.ImportCookiesRequest(
            account_id=account.id,
            cookies="SAPISID=fixture-signing",
            probe_identity=False,
        ),
        AccountService(store),
        RuntimeState(),
    )
    assert response.auth_user == "3"
    assert response.name == "Saved"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body",
    [(401, "[]"), (403, "[]"), (500, "[]"), (200, "invalid"), (200, "[99]")],
)
async def test_failed_tier_detection_does_not_downgrade_saved_paid_account(
    tmp_path, monkeypatch, status, body
):
    store = AccountStore(accounts_dir=tmp_path / "accounts")
    account = store.save_account(
        name="Paid fixture",
        email="fixture@example.com",
        storage_state={"cookies": COOKIE_RECORDS},
        tier="Pro",
    )
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(status, text=body)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)
    response = await routes_accounts.detect_account_tier(
        account.id, AccountService(store)
    )
    assert response.tier == "Pro"


@pytest.mark.asyncio
async def test_verified_free_tier_remains_distinct_from_failed_detection(monkeypatch):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(200, json=[0])
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)
    assert await cookie_parser.probe_account_benefit_tier(COOKIE_VALUES) == "Free"
