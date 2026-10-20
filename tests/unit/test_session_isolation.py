"""Hermetic account isolation, request leases, and temporary-page regression tests."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aistudio_api.infrastructure.browser.cdp_client import (
    CDPClient,
    CDPConnection,
    CDPPage,
)
from aistudio_api.infrastructure.gateway.capture import (
    CapturedRequest,
    RequestCaptureService,
)
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.gateway.session import BrowserSession
from aistudio_api.infrastructure.gateway.session_state import SessionContext
from aistudio_api.infrastructure.gateway.transport import XHRStreamTransport


def _page(target_id: str) -> MagicMock:
    page = MagicMock(spec=CDPPage)
    page.target_id = target_id
    page.is_closed.return_value = False
    return page


def _context(page: CDPPage, auth_file: str = "fake-old", auth_user: str = "0"):
    return SessionContext(
        page=page,
        snap_key="fake-snapshot-key",
        auth_user=auth_user,
        auth_file=auth_file,
        base_template={
            "url": f"https://example.invalid/{auth_user}",
            "headers": {"X-Context": auth_user},
            "body": '["models/fake",[[[[null,"old"]],"user"]],null,[null,null,null,128,0.5,0.8,16],"old_snapshot"]',
        },
    )


def _session_with_context():
    session = BrowserSession(port=1)
    session._proc = MagicMock()
    session._proc.is_alive.return_value = True
    session._cdp_client = MagicMock(spec=CDPClient)
    context = _context(_page("old"))
    session.set_ready_context(context)
    session._auth_file = context.auth_file
    session._auth_user = context.auth_user
    return session, context


@pytest.mark.asyncio
async def test_completed_request_drain_signal_does_not_close_new_request():
    session, context = _session_with_context()
    context.incr_in_flight()
    context.decr_in_flight()
    assert context.drain_event.is_set()
    context.incr_in_flight()
    assert not context.drain_event.is_set()
    session._retiring_contexts.append(context)
    drain = asyncio.create_task(session._drain_retiring_context(context))
    await asyncio.sleep(0)
    assert not drain.done()
    context.page.close.assert_not_called()
    context.decr_in_flight()
    await drain
    context.page.close.assert_awaited_once()
    assert not session._retiring_contexts


@pytest.mark.asyncio
async def test_template_snapshot_and_transport_remain_on_leased_context():
    session, old = _session_with_context()
    new_page = _page("new")
    session._cdp_client.create_new_page.return_value = new_page
    session._transport.send_hooked_request = AsyncMock(return_value=(200, b"ok"))

    async def snapshot_and_switch(*_args, **_kwargs):
        await session.transition_to_account("fake-new", force_refresh=True)
        return "fake-snapshot"

    old.page.evaluate.side_effect = snapshot_and_switch
    service = RequestCaptureService(session)
    with (
        patch(
            "aistudio_api.infrastructure.gateway.session.get_auth_user_from_meta",
            return_value="1",
        ),
        patch(
            "aistudio_api.infrastructure.gateway.session.load_cookies_to_page",
            AsyncMock(return_value=True),
        ),
        patch(
            "aistudio_api.infrastructure.gateway.session.navigate_to_aistudio",
            AsyncMock(),
        ),
        patch(
            "aistudio_api.infrastructure.gateway.session.bootstrap_botguard_and_template",
            AsyncMock(
                return_value=(
                    "new-snap",
                    {"url": "https://example.invalid/1", "body": "[]"},
                )
            ),
        ),
    ):
        async with session.request_scope():
            captured = await service.capture(prompt="fake", model="models/fake")
            assert captured is not None
            assert captured.url == "https://example.invalid/0"
            assert session.active_context.page is new_page
            assert await session.ensure_ready() is old
            await session.send_hooked_request(body=captured.body, timeout_ms=100)
            args, kwargs = session._transport.send_hooked_request.call_args
            assert args[0] is old.page
            assert kwargs["auth_user"] == "0"
            old.page.close.assert_not_called()
        await asyncio.gather(*session._background_tasks)
    assert old.in_flight == 0
    old.page.close.assert_awaited_once()
    assert session.active_context.page is new_page


@pytest.mark.asyncio
async def test_failed_transition_disposes_new_context_and_keeps_old_ready():
    session, old = _session_with_context()
    new_page = _page("new")
    session._cdp_client.create_new_page.return_value = new_page
    with (
        patch(
            "aistudio_api.infrastructure.gateway.session.get_auth_user_from_meta",
            return_value="1",
        ),
        patch(
            "aistudio_api.infrastructure.gateway.session.load_cookies_to_page",
            AsyncMock(return_value=True),
        ),
        patch(
            "aistudio_api.infrastructure.gateway.session.navigate_to_aistudio",
            AsyncMock(),
        ),
        patch(
            "aistudio_api.infrastructure.gateway.session.bootstrap_botguard_and_template",
            AsyncMock(side_effect=RuntimeError("fake bootstrap failure")),
        ),
        pytest.raises(RuntimeError, match="fake bootstrap failure"),
    ):
        await session.transition_to_account("fake-new", force_refresh=True)
    assert session.is_ready
    assert session.active_context is old
    assert session._auth_file == "fake-old"
    assert session._auth_user == "0"
    old.page.close.assert_not_called()
    session._cdp_client.close_target.assert_awaited_once_with("new")


@pytest.mark.asyncio
async def test_request_cancellation_releases_lease_and_context_binding():
    session, context = _session_with_context()
    entered = asyncio.Event()

    async def request():
        async with session.request_scope():
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(request())
    await entered.wait()
    assert context.in_flight == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert context.in_flight == 0
    assert session._in_flight == 0
    assert session._request_context.get() is None
    async with session.request_scope():
        assert context.in_flight == 1
        assert not context.drain_event.is_set()


@pytest.mark.asyncio
async def test_nested_attempt_scope_owns_one_lease_until_outer_scope_exits():
    session, context = _session_with_context()
    async with session.request_scope() as outer:
        async with session.request_scope() as inner:
            assert outer is inner is context
            assert context.in_flight == session._in_flight == 1
        assert context.in_flight == session._in_flight == 1
        assert session.request_context is context
    assert context.in_flight == session._in_flight == 0
    assert session._request_context.get() is None


@pytest.mark.asyncio
async def test_browser_recovery_only_resets_the_failed_unleased_context():
    session, context = _session_with_context()
    session._close_internal = AsyncMock()
    assert not await session.reset_if_active(context.context_id + 1)
    async with session.request_scope():
        assert not await session.reset_if_active(context.context_id)
    retired = _context(_page("retired"))
    retired.incr_in_flight()
    session._retiring_contexts.append(retired)
    assert not await session.reset_if_active(context.context_id)
    session._close_internal.assert_not_called()
    retired.decr_in_flight()
    assert await session.reset_if_active(context.context_id)
    session._close_internal.assert_awaited_once()


@pytest.mark.asyncio
async def test_retiring_context_stays_registered_until_disposal_finishes():
    session, context = _session_with_context()
    session._retiring_contexts.append(context)
    disposing = asyncio.Event()
    disposed = asyncio.Event()

    async def close_target(_target_id):
        disposing.set()
        await disposed.wait()

    session._cdp_client.close_target.side_effect = close_target
    first = asyncio.create_task(session._release_context(context))
    await disposing.wait()
    assert context in session._retiring_contexts
    second = asyncio.create_task(session._release_context(context))
    await asyncio.sleep(0)
    assert not second.done()
    disposed.set()
    await asyncio.gather(first, second)
    context.page.close.assert_awaited_once()
    session._cdp_client.close_target.assert_awaited_once_with(context.page.target_id)
    assert not session._retiring_contexts


@pytest.mark.asyncio
async def test_cancelled_transition_disposes_candidate_and_restores_old_context():
    session, old = _session_with_context()
    new_page = _page("cancelled")
    session._cdp_client.create_new_page.return_value = new_page
    preparing = asyncio.Event()

    async def bootstrap(*_args, **_kwargs):
        preparing.set()
        await asyncio.Event().wait()

    with (
        patch(
            "aistudio_api.infrastructure.gateway.session.get_auth_user_from_meta",
            return_value="1",
        ),
        patch(
            "aistudio_api.infrastructure.gateway.session.load_cookies_to_page",
            AsyncMock(return_value=True),
        ),
        patch(
            "aistudio_api.infrastructure.gateway.session.navigate_to_aistudio",
            AsyncMock(),
        ),
        patch(
            "aistudio_api.infrastructure.gateway.session.bootstrap_botguard_and_template",
            side_effect=bootstrap,
        ),
    ):
        task = asyncio.create_task(
            session.transition_to_account("fake-new", force_refresh=True)
        )
        await preparing.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert session.active_context is old
    assert session.is_ready
    assert session._auth_file == "fake-old"
    old.page.close.assert_not_called()
    session._cdp_client.close_target.assert_awaited_once_with("cancelled")


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_transport_initialization_failure_releases_queue_and_js_state(cancelled):
    transport = XHRStreamTransport()
    page = _page("transport")
    error = asyncio.CancelledError() if cancelled else RuntimeError("fake init failure")
    page.evaluate.side_effect = [error, None]
    with (
        patch(
            "aistudio_api.infrastructure.gateway.transport._ensure_authorization_header",
            AsyncMock(return_value={}),
        ),
        pytest.raises(type(error)),
    ):
        async for _ in transport.send_streaming_request(
            page,
            url="https://example.invalid/generate",
            headers={},
            body="[]",
            timeout_ms=100,
        ):
            pass
    assert transport.in_flight_count == 0
    assert page.evaluate.await_count == 2


@pytest.mark.asyncio
async def test_cold_email_detection_isolated_and_has_no_bootstrap_or_state_write():
    session = BrowserSession(port=1)
    original_auth = session._auth_file
    page = _page("email")
    client = MagicMock(spec=CDPClient)
    client.create_new_page.return_value = page
    proc = MagicMock()

    async def start_browser():
        session._cdp_client = client
        session._proc = proc

    session._ensure_browser_cdp = AsyncMock(side_effect=start_browser)
    with (
        patch(
            "aistudio_api.infrastructure.gateway.session.get_auth_user_from_meta",
            return_value="3",
        ),
        patch(
            "aistudio_api.infrastructure.gateway.session.load_cookies_to_page",
            AsyncMock(return_value=True),
        ),
        patch(
            "aistudio_api.infrastructure.gateway.session.navigate_to_aistudio",
            AsyncMock(),
        ) as navigate,
        patch(
            "aistudio_api.infrastructure.gateway.session.extract_account_email",
            AsyncMock(return_value="fake@example.invalid"),
        ),
        patch(
            "aistudio_api.infrastructure.gateway.session.bootstrap_botguard_and_template",
            AsyncMock(),
        ) as bootstrap,
    ):
        assert (
            await session.detect_account_email("fake-target") == "fake@example.invalid"
        )
    navigate.assert_awaited_once_with(page, auth_user="3", auth_file=None)
    bootstrap.assert_not_called()
    client.close_target.assert_awaited_once_with("email")
    client.close.assert_awaited_once()
    proc.terminate.assert_called_once()
    assert session.active_context is None
    assert session._auth_file == original_auth


@pytest.mark.asyncio
async def test_email_failure_keeps_live_account_and_disposes_temporary_page():
    session, old = _session_with_context()
    client = session._cdp_client
    client.create_new_page.return_value = _page("email")
    with (
        patch(
            "aistudio_api.infrastructure.gateway.session.load_cookies_to_page",
            AsyncMock(side_effect=RuntimeError("fake cookie failure")),
        ),
        pytest.raises(RuntimeError, match="fake cookie failure"),
    ):
        await session.detect_account_email("fake-target")
    assert session.active_context is old
    assert session._auth_file == "fake-old"
    client.close_target.assert_awaited_once_with("email")
    client.close.assert_not_called()


@pytest.mark.asyncio
async def test_cdp_pages_get_distinct_cookie_jars_and_dispose_them():
    client = CDPClient(port=1)
    client.wait_until_ready = AsyncMock()
    browser = MagicMock(spec=CDPConnection)
    client._browser_cdp = browser
    created = 0

    async def browser_command(method, params=None):
        nonlocal created
        if method == "Target.createBrowserContext":
            created += 1
            return {"browserContextId": f"jar-{created}"}
        if method == "Target.createTarget":
            return {"targetId": f"target-{created}"}
        return {}

    browser.send.side_effect = browser_command
    client.get_targets = AsyncMock(
        side_effect=lambda: [
            {
                "id": f"target-{created}",
                "webSocketDebuggerUrl": "ws://fake.invalid/page",
            }
        ]
    )
    connections = [MagicMock(spec=CDPConnection), MagicMock(spec=CDPConnection)]
    for connection in connections:
        connection._closed = False
    with (
        patch(
            "aistudio_api.infrastructure.browser.cdp_client.CDPConnection",
            side_effect=connections,
        ),
        patch.object(CDPPage, "init_domains", AsyncMock()),
    ):
        first = await client.create_new_page()
        second = await client.create_new_page()
    assert first.browser_context_id == "jar-1"
    assert second.browser_context_id == "jar-2"
    await first.clear_cookies()
    browser.send.assert_awaited_with(
        "Storage.clearCookies", {"browserContextId": "jar-1"}
    )
    await client.close_target(first.target_id)
    browser.send.assert_awaited_with(
        "Target.disposeBrowserContext", {"browserContextId": "jar-1"}
    )
    assert "jar-2" in client._browser_contexts
    await client.close()
    assert not client._browser_contexts
    assert not client._owned_pages


@pytest.mark.asyncio
async def test_closing_client_stream_closes_nested_generators_before_releasing_lease():
    session, context = _session_with_context()
    client = AIStudioClient(port=1)
    client._session = session
    client._streaming_gateway._session = session
    client.capture_request = AsyncMock(
        return_value=CapturedRequest(
            url="https://example.invalid/generate", headers={}, body="[]"
        )
    )
    finalized = asyncio.Event()

    async def transport(*_args, **_kwargs):
        try:
            yield "status", 200
            yield "chunk", b"[[[]]]"
            await asyncio.Event().wait()
        finally:
            finalized.set()

    session._transport.send_streaming_request = transport
    from aistudio_api.domain.models import Candidate, ContentPart

    content_part = ContentPart(text="fake piece")
    candidate = Candidate(
        parts=[content_part],
        finish_reason=None,
        text="fake piece",
    )
    with (
        patch(
            "aistudio_api.infrastructure.gateway.streaming.parse_response_chunk",
            return_value=candidate,
        ),
        patch(
            "aistudio_api.infrastructure.gateway.streaming.parse_chunk_usage",
            return_value=None,
        ),
    ):
        events = client.stream_generate_content(
            model="models/fake", capture_prompt="fake"
        )
        assert await anext(events) == ("parts", [content_part])
        assert context.in_flight == 1
        await events.aclose()
    assert finalized.is_set()
    assert context.in_flight == 0
    assert session._request_context.get() is None


@pytest.mark.asyncio
async def test_cdp_initialization_failure_disposes_partial_context():
    client = CDPClient(port=1)
    client.wait_until_ready = AsyncMock()
    browser = MagicMock(spec=CDPConnection)
    browser.send.side_effect = [
        {"browserContextId": "partial"},
        {"targetId": "target"},
        {},
    ]
    client._browser_cdp = browser
    client.get_targets = AsyncMock(
        return_value=[
            {"id": "target", "webSocketDebuggerUrl": "ws://fake.invalid/page"}
        ]
    )
    connection = MagicMock(spec=CDPConnection)
    with (
        patch(
            "aistudio_api.infrastructure.browser.cdp_client.CDPConnection",
            return_value=connection,
        ),
        patch.object(
            CDPPage,
            "init_domains",
            AsyncMock(side_effect=RuntimeError("fake init failure")),
        ),
        pytest.raises(RuntimeError, match="fake init failure"),
    ):
        await client.create_new_page()
    connection.close.assert_awaited_once()
    browser.send.assert_awaited_with(
        "Target.disposeBrowserContext", {"browserContextId": "partial"}
    )
    assert not client._browser_contexts
