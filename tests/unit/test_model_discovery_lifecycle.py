"""Catalog reads borrow ready contexts without activating an account."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from aistudio_api.infrastructure.browser.cdp_client import CDPPage
from aistudio_api.infrastructure.gateway.model_discovery import ModelDiscoveryService
from aistudio_api.infrastructure.gateway.session import BrowserSession
from aistudio_api.infrastructure.gateway.session_state import SessionContext


def ready_session():
    session = BrowserSession(port=1)
    page = MagicMock(spec=CDPPage)
    page.is_closed.return_value = False

    async def close():
        page.is_closed.return_value = True

    page.close = AsyncMock(side_effect=close)
    context = SessionContext(
        page=page, snap_key="synthetic", auth_user="2", auth_file=None,
        base_template={},
    )
    session.set_ready_context(context)
    session.persist_context_cookies = AsyncMock()
    return session, context


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "cancelled", "fallback"])
async def test_catalog_read_keeps_retiring_page_alive_until_exit(outcome):
    session, context = ready_session()
    started, release = asyncio.Event(), asyncio.Event()
    service = ModelDiscoveryService()
    models = [{"id": "gemini-synthetic"}]

    async def fetch(page, *, auth_user):
        assert page is context.page and auth_user == "2"
        started.set()
        await release.wait()
        assert not page.is_closed()
        if outcome == "fallback":
            raise RuntimeError("synthetic catalog failure")
        return models

    service._fetch_via_page = AsyncMock(side_effect=fetch)
    service._fetch_via_http = AsyncMock(return_value=models)
    query = asyncio.create_task(service.get_models(
        session=session, cookies={"SID": "synthetic"},
    ))
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert context.in_flight == 1
        assert session.get_status()["in_flight"] == 1
        await session.clear_account()
        await asyncio.sleep(0)
        context.page.close.assert_not_awaited()
        if outcome == "cancelled":
            query.cancel()
            with pytest.raises(asyncio.CancelledError):
                await query
        else:
            release.set()
            assert await query == models
        await asyncio.gather(*session._background_tasks)
        context.page.close.assert_awaited_once()
        assert context.in_flight == 0
        assert session.get_status()["in_flight"] == 0
    finally:
        release.set()
        query.cancel()
        await asyncio.gather(query, return_exceptions=True)
        await asyncio.gather(*session._background_tasks)


@pytest.mark.asyncio
async def test_catalog_read_never_prepares_a_missing_or_transitioning_context():
    session, context = ready_session()
    session.ensure_ready = AsyncMock(side_effect=AssertionError("must not prepare"))
    session._gate.set_preparing()
    service = ModelDiscoveryService()
    service._fetch_via_page = AsyncMock(side_effect=AssertionError("retiring page"))
    service._fetch_via_http = AsyncMock(return_value=[{"id": "synthetic"}])
    assert await service.get_models(session=session, cookies={"SID": "synthetic"}) == [{"id": "synthetic"}]
    assert context.in_flight == 0
    session.ensure_ready.assert_not_awaited()
    service._fetch_via_page.assert_not_awaited()

    await session.clear_account()
    await asyncio.gather(*session._background_tasks)
    service._cached_models = []
    assert await service.get_models(session=session, cookies={"SID": "synthetic"}) == [{"id": "synthetic"}]
    session.ensure_ready.assert_not_awaited()


@pytest.mark.asyncio
async def test_catalog_read_prevents_idle_close_without_preparing(monkeypatch):
    session, context = ready_session()
    session._proc = MagicMock()
    session._proc.is_alive.return_value = True
    session._close_internal = AsyncMock()
    monkeypatch.setattr("aistudio_api.infrastructure.gateway.session.settings.browser_idle_timeout", 1)
    async with session.existing_context_scope() as leased:
        assert leased is context
        session._last_activity_time = 0
        assert not await session.check_idle_timeout()
    assert context.in_flight == 0
    assert session.get_status()["in_flight"] == 0
    session._close_internal.assert_not_awaited()
