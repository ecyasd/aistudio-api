"""Synthetic identity evidence; never opens real accounts or sessions."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aistudio_api.domain.errors import (
    AccountIdentityMismatchError,
    AccountIdentityUnavailableError,
)
from aistudio_api.infrastructure.browser.scripts import ACCOUNT_IDENTITY_JS
from aistudio_api.infrastructure.gateway.session_auth import (
    _account_identity_status,
    bind_auth_file,
    navigate_to_aistudio,
    save_browser_cookies,
    verify_account_identity,
)


def _metadata(tmp_path, auth_user="1"):
    auth = tmp_path / "auth.json"
    auth.write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
    (tmp_path / "meta.json").write_text(json.dumps({
        "id": "synthetic", "auth_user": auth_user, "email": "owner@example.invalid",
    }), encoding="utf-8")
    return str(auth)


@pytest.mark.asyncio
async def test_identity_reads_live_url_after_same_document_navigation(tmp_path):
    auth = _metadata(tmp_path)

    async def evaluate(script, *args, **kwargs):
        if script == ACCOUNT_IDENTITY_JS:
            return {"url": "https://aistudio.google.com/u/1/prompts/new_chat", "email": "owner@example.invalid"}
        return False

    page = SimpleNamespace(
        url="https://aistudio.google.com/u/0/prompts/new_chat",
        is_closed=lambda: False,
        evaluate=AsyncMock(side_effect=evaluate),
    )
    assert await verify_account_identity(page, auth)


@pytest.mark.asyncio
async def test_invalid_metadata_never_verifies_an_account(tmp_path):
    auth = _metadata(tmp_path)
    (tmp_path / "meta.json").write_text("{", encoding="utf-8")
    page = SimpleNamespace(url="https://aistudio.google.com/", evaluate=AsyncMock())
    assert not await verify_account_identity(page, auth)


def _page(*snapshots):
    pending = list(snapshots)
    current = pending[-1]

    async def evaluate(script, *args, **kwargs):
        nonlocal current
        if script == ACCOUNT_IDENTITY_JS:
            if pending:
                current = pending.pop(0)
            return current
        if "location.href" in script:
            return current["url"]
        return True

    return SimpleNamespace(
        url="https://aistudio.google.com/prompts/new_chat",
        is_closed=lambda: False,
        evaluate=AsyncMock(side_effect=evaluate),
        goto=AsyncMock(),
        wait_for_timeout=AsyncMock(),
        get_cookies=AsyncMock(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("url", "email", "status"), [
    ("https://aistudio.google.com/u/1/prompts/new_chat", "OWNER@example.invalid", "verified"),
    ("https://aistudio.google.com/prompts/new_chat?authuser=1", "owner@example.invalid", "verified"),
    ("https://aistudio.google.com/u/1/prompts/new_chat?authuser=01", "owner@example.invalid", "verified"),
    ("https://aistudio.google.com/u/0/prompts/new_chat", "owner@example.invalid", "mismatch"),
    ("https://aistudio.google.com/u/1/prompts/new_chat", "other@example.invalid", "mismatch"),
    ("https://aistudio.google.com/u/1/prompts/new_chat", None, "unavailable"),
    ("https://aistudio.google.com/u/1/prompts/new_chat?authuser=0", "owner@example.invalid", "unavailable"),
    ("https://aistudio.google.com/u/1/prompts/new_chat?authuser=1&authuser=1", "owner@example.invalid", "unavailable"),
    ("https://aistudio.google.com/u/1/prompts/new_chat?authuser=", "owner@example.invalid", "unavailable"),
    ("https://aistudio.google.com/u/bad/prompts/new_chat", "owner@example.invalid", "unavailable"),
    ("https://accounts.google.com/signin", "owner@example.invalid", "unavailable"),
    ("https://aistudio.google.com.evil.invalid/u/1/", "owner@example.invalid", "unavailable"),
])
async def test_live_route_and_active_email_must_agree(tmp_path, url, email, status):
    auth = _metadata(tmp_path)
    page = _page({"url": url, "email": email})
    assert await _account_identity_status(page, auth) == status


@pytest.mark.asyncio
async def test_navigation_waits_for_hydration_before_checkpoint(tmp_path):
    auth = _metadata(tmp_path)
    url = "https://aistudio.google.com/u/1/prompts/new_chat"
    page = _page({"url": url, "email": None}, {"url": url, "email": "owner@example.invalid"})
    save = AsyncMock()
    await navigate_to_aistudio(page, auth_user="1", auth_file=auth, save_cookies_cb=save)
    page.wait_for_timeout.assert_awaited_once_with(250)
    save.assert_awaited_once_with(auth_file=auth)
    page.goto.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(("email", "error"), [
    (None, AccountIdentityUnavailableError),
    ("other@example.invalid", AccountIdentityMismatchError),
])
async def test_navigation_distinguishes_unavailable_from_proven_mismatch(tmp_path, email, error):
    auth = _metadata(tmp_path)
    page = _page({"url": "https://aistudio.google.com/u/1/prompts/new_chat", "email": email})
    save = AsyncMock()
    with pytest.raises(error):
        await navigate_to_aistudio(page, auth_user="1", auth_file=auth, save_cookies_cb=save)
    save.assert_not_awaited()
    assert page.goto.await_count == 2
    assert page.wait_for_timeout.await_count == 22


@pytest.mark.asyncio
async def test_bound_recovery_page_cannot_use_unpinned_discovery_bypass(tmp_path):
    auth = _metadata(tmp_path)
    page = _page({"url": "https://aistudio.google.com/u/1/prompts/new_chat", "email": "other@example.invalid"})
    assert bind_auth_file(page, auth)
    assert await _account_identity_status(page, None) == "mismatch"


@pytest.mark.asyncio
async def test_explicit_unbound_discovery_remains_available():
    page = _page({"url": "https://aistudio.google.com/prompts/new_chat", "email": None})
    assert await verify_account_identity(page, None)


@pytest.mark.asyncio
async def test_missing_metadata_and_unreadable_browser_are_unavailable(tmp_path):
    auth = _metadata(tmp_path)
    page = _page({"url": "https://aistudio.google.com/u/1/", "email": "owner@example.invalid"})
    page.evaluate.side_effect = TimeoutError("synthetic CDP timeout")
    assert await _account_identity_status(page, auth) == "unavailable"
    (tmp_path / "meta.json").unlink()
    assert await _account_identity_status(page, auth) == "unavailable"


@pytest.mark.asyncio
async def test_checkpoint_rejects_wrong_identity_and_leaves_file_unchanged(tmp_path):
    auth = _metadata(tmp_path)
    page = _page({"url": "https://aistudio.google.com/u/1/", "email": "other@example.invalid"})
    assert bind_auth_file(page, auth)
    before = (tmp_path / "auth.json").read_bytes()
    assert not await save_browser_cookies(page, auth)
    page.get_cookies.assert_not_awaited()
    assert (tmp_path / "auth.json").read_bytes() == before


@pytest.mark.asyncio
async def test_identity_wait_cancellation_never_checkpoints(tmp_path):
    import asyncio

    auth = _metadata(tmp_path)
    page = _page({"url": "https://aistudio.google.com/u/1/", "email": None})
    page.wait_for_timeout.side_effect = asyncio.CancelledError()
    save = AsyncMock()
    with pytest.raises(asyncio.CancelledError):
        await navigate_to_aistudio(page, auth_user="1", auth_file=auth, save_cookies_cb=save)
    save.assert_not_awaited()
