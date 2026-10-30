"""Offline SSE status, heartbeat, backpressure and disconnect regressions."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def sse_env(monkeypatch):
    from aistudio_api.application import api_service_gemini as service
    from aistudio_api.application.account_rotator import AccountStats

    accounts = [SimpleNamespace(id="test-account", auth_generation=None)]
    stats = {"test-account": AccountStats(account_id="test-account")}
    context = SimpleNamespace(auth_file="offline/test-account", context_id=1, auth_generation=None)
    leased = []

    @asynccontextmanager
    async def request_scope():
        leased.append(context)
        try:
            yield context
        finally:
            leased.remove(context)

    client = SimpleNamespace(
        stream_generate_content=MagicMock(side_effect=AssertionError("unexpected model call")),
        generate_content=AsyncMock(side_effect=AssertionError("unexpected model call")),
        clear_templates=MagicMock(),
        session=SimpleNamespace(request_scope=request_scope),
    )
    account_service = SimpleNamespace(
        list_accounts=lambda: accounts,
        get_active_account=lambda: accounts[0] if accounts else None,
        get_account=lambda account_id: next((account for account in accounts if account.id == account_id), None),
        get_account_auth_path=lambda account_id: f"offline/{account_id}",
    )
    rotator = SimpleNamespace(get_account_stats=lambda account_id: stats.get(account_id))
    monkeypatch.setattr(service.runtime_state, "account_service", account_service)
    monkeypatch.setattr(service.runtime_state, "rotator", rotator)
    monkeypatch.setattr(service.runtime_state, "record", MagicMock())
    monkeypatch.setattr(service, "record_rotator_event", MagicMock())
    monkeypatch.setattr(service, "ensure_active_account", AsyncMock())
    monkeypatch.setattr(service, "try_switch_account", AsyncMock(return_value=False))
    monkeypatch.setattr(service, "SSE_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    return SimpleNamespace(
        service=service, accounts=accounts, stats=stats, client=client,
        context=context, leased=leased,
    )


@pytest.fixture
def request_body():
    from aistudio_api.api.schemas import GeminiGenerateContentRequest

    return GeminiGenerateContentRequest.model_validate(
        {"contents": [{"role": "user", "parts": [{"text": "offline test"}]}]}
    )


def _live_sse_tasks():
    return [
        task
        for task in asyncio.all_tasks()
        if not task.done() and task.get_name().startswith("gemini-sse-")
    ]


@pytest.mark.parametrize("stream", [False, True])
async def test_repeated_204_is_a_real_502_error(sse_env, request_body, stream):
    from fastapi import HTTPException

    from aistudio_api.domain.errors import RequestError

    async def stream_call(**kwargs):
        raise RequestError(204, "No generation content")
        yield  # pragma: no cover

    sse_env.client.stream_generate_content.side_effect = stream_call
    sse_env.client.generate_content.side_effect = RequestError(204, "No generation content")
    if stream:
        response = await sse_env.service.handle_gemini_generate_content(
            "gemini-test", request_body, sse_env.client, stream=True
        )
        result = "".join([chunk async for chunk in response.body_iterator])
        assert '"code": 502' in result and '"code": 204' not in result
        assert '"finishReason"' not in result
        assert sse_env.client.stream_generate_content.call_count == 2
    else:
        with pytest.raises(HTTPException) as error:
            await sse_env.service.handle_gemini_generate_content(
                "gemini-test", request_body, sse_env.client, stream=False
            )
        assert error.value.status_code == 502
        assert sse_env.client.generate_content.await_count == 2
    assert not sse_env.leased


@pytest.mark.parametrize("stream", [False, True])
async def test_native_preparation_timeout_returns_nonempty_504(sse_env, request_body, stream):
    from fastapi import HTTPException

    sse_env.service.ensure_active_account.side_effect = TimeoutError()
    operation = sse_env.service.handle_gemini_generate_content(
        "gemini-test", request_body, sse_env.client, stream=stream
    )
    if stream:
        response = await operation
        result = "".join([chunk async for chunk in response.body_iterator])
        assert '"code": 504' in result and "timed out" in result
    else:
        with pytest.raises(HTTPException) as error:
            await operation
        assert error.value.status_code == 504
        assert "timed out" in error.value.detail["message"]
    assert not sse_env.leased


@pytest.mark.parametrize("stream", [False, True])
async def test_continuation_retries_share_total_request_deadline(sse_env, request_body, monkeypatch, stream):
    from fastapi import HTTPException

    from aistudio_api.application.inference_limits import InferenceLimiter
    from aistudio_api.domain.errors import RequestError

    service = sse_env.service
    monkeypatch.setattr(service, "settings", SimpleNamespace(timeout_stream=0.11))
    limiter = InferenceLimiter(concurrency=1, wait_seconds=1)
    monkeypatch.setattr(service, "inference_limiter", limiter)
    calls, closed = [], []

    async def upstream(**kwargs):
        calls.append(True)
        try:
            await asyncio.sleep(0.045)
            raise RequestError(404, "Ambiguous request for service '' and method '/GenerativeService.GenerateContent'")
        finally:
            closed.append(True)

    async def streaming(**kwargs):
        await upstream(**kwargs)
        yield  # pragma: no cover

    sse_env.client.generate_content.side_effect = upstream
    sse_env.client.stream_generate_content.side_effect = streaming
    body = request_body.model_copy(update={"autoContinue": True})
    operation = service.handle_gemini_generate_content("gemini-test", body, sse_env.client, stream=stream)
    if stream:
        response = await operation
        result = "".join([chunk async for chunk in response.body_iterator])
        assert '"code": 504' in result and '"finishReason"' not in result
    else:
        with pytest.raises(HTTPException) as error:
            await operation
        assert error.value.status_code == 504
    assert len(calls) < 5 and len(calls) == len(closed)
    assert limiter.active == 0 and not sse_env.leased
    service.runtime_state.record.assert_not_called()


async def test_ordinary_progressing_stream_can_outlive_continuation_budget(sse_env, request_body, monkeypatch):
    service = sse_env.service
    monkeypatch.setattr(service, "settings", SimpleNamespace(timeout_stream=0.005))

    async def upstream(**kwargs):
        for _ in range(4):
            await asyncio.sleep(0.005)
            yield "body", "progress"
        yield "finish_reason", 1
        yield "done", None

    sse_env.client.stream_generate_content.side_effect = upstream
    response = await service.handle_gemini_generate_content("gemini-test", request_body, sse_env.client, stream=True)
    result = "".join([chunk async for chunk in response.body_iterator])
    assert result.count("progress") == 4 and '"finishReason": "STOP"' in result
    assert '"error"' not in result and not sse_env.leased


@pytest.mark.parametrize(
    ("condition", "expected"),
    [("invalid", 400), ("empty", 503), ("expired", 503), ("limited", 429)],
)
async def test_preflight_returns_real_http_error_without_starting_browser(
    sse_env, condition, expected
):
    import httpx
    from fastapi import FastAPI

    from aistudio_api.api.schemas import GeminiGenerateContentRequest

    app = FastAPI()
    service = sse_env.service
    stats = sse_env.stats["test-account"]
    if condition == "empty":
        sse_env.accounts.clear()
    elif condition == "expired":
        stats.session_expired = True
    elif condition == "limited":
        stats.model_cooldowns["gemini-test"] = time.time() + 3600

    # Resolve the request schema without a postponed local annotation, so this
    # deliberately small ASGI app needs no application lifespan or credentials.
    async def endpoint(req):
        return await service.handle_gemini_generate_content(
            "models/gemini-test", req, sse_env.client, stream=True
        )

    endpoint.__annotations__["req"] = GeminiGenerateContentRequest
    app.post("/stream")(endpoint)
    contents = [] if condition == "invalid" else [
        {"role": "user", "parts": [{"text": "offline test"}]}
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://offline"
    ) as http:
        response = await http.post("/stream", json={"contents": contents})
    assert response.status_code == expected
    assert "text/event-stream" not in response.headers["content-type"]
    sse_env.client.stream_generate_content.assert_not_called()
    service.ensure_active_account.assert_not_awaited()
    service.try_switch_account.assert_not_awaited()
    service.runtime_state.record.assert_not_called()


async def test_preflight_does_not_mutate_daily_stats_or_preselect_account(
    sse_env, request_body
):
    service = sse_env.service
    stats = sse_env.stats["test-account"]
    stats.auth_cooldown = time.time() + 3600
    stats.model_requests["gemini-test"] = 27
    stats.model_requests_dates["gemini-test"] = "2000-01-01"
    stats.model_rate_limited_dates["gemini-test"] = "2000-01-01"
    stats.model_cooldowns["gemini-test"] = time.time() + 3600
    before = stats.to_dict()

    response = await service.handle_gemini_generate_content(
        "gemini-test", request_body, sse_env.client, stream=True
    )
    assert response.status_code == 200
    assert stats.to_dict() == before
    service.ensure_active_account.assert_not_awaited()
    service.try_switch_account.assert_not_awaited()
    sse_env.client.stream_generate_content.assert_not_called()
    await response.body_iterator.aclose()


async def test_heartbeats_preserve_chunks_and_one_producer_context(sse_env):
    service = sse_env.service
    release = asyncio.Event()
    closed = asyncio.Event()
    context = ContextVar("offline-stream-context", default=None)
    owners = []

    async def source():
        token = context.set("pinned-account")
        try:
            owners.append(asyncio.current_task())
            await release.wait()
            for index in range(20):
                assert context.get() == "pinned-account"
                owners.append(asyncio.current_task())
                yield f"data: {index}\n\n"
        finally:
            context.reset(token)
            closed.set()

    stream = service._sse_with_heartbeats(source(), interval=0.01)
    assert await asyncio.wait_for(anext(stream), 1) == "\n\n"
    assert await asyncio.wait_for(anext(stream), 1) == "\n\n"
    release.set()
    chunks = [chunk async for chunk in stream]
    assert chunks == [f"data: {index}\n\n" for index in range(20)]
    assert len(set(owners)) == 1
    assert closed.is_set()
    assert context.get() is None
    assert not _live_sse_tasks()


async def test_slow_client_keeps_bounded_buffer_and_aclose_stops_producer(sse_env):
    produced = []
    closed = asyncio.Event()

    async def source():
        try:
            for index in range(1000):
                produced.append(index)
                yield f"data: {index}\n\n"
        finally:
            closed.set()

    stream = sse_env.service._sse_with_heartbeats(source(), interval=0.01)
    assert await anext(stream) == "data: 0\n\n"
    await asyncio.sleep(0.01)
    # One yielded chunk, one queued chunk and at most one blocked queue.put.
    assert len(produced) <= 3
    await stream.aclose()
    assert closed.is_set()
    assert not _live_sse_tasks()


async def test_cancelled_upstream_does_not_send_heartbeats_forever(sse_env):
    async def source():
        raise asyncio.CancelledError
        yield "unreachable"  # pragma: no cover

    stream = sse_env.service._sse_with_heartbeats(source(), interval=0.01)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(anext(stream), 1)
    assert not _live_sse_tasks()


async def test_cancel_during_account_initialization_closes_waiting_task(
    sse_env, request_body
):
    service = sse_env.service
    started = asyncio.Event()
    closed = asyncio.Event()

    async def initialize(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    service.ensure_active_account.side_effect = initialize
    response = service._build_gemini_streaming_response(
        client=sse_env.client, req=request_body, model_path="gemini-test"
    )
    pending = asyncio.create_task(anext(response.body_iterator))
    await asyncio.wait_for(started.wait(), 1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert closed.is_set()
    assert not _live_sse_tasks()
    service.runtime_state.record.assert_not_called()


async def test_disconnect_on_send_closes_upstream_in_its_original_context(
    sse_env, request_body
):
    service = sse_env.service
    closed = asyncio.Event()
    context = ContextVar("offline-upstream-context", default=None)

    async def upstream(**kwargs):
        token = context.set("request-scope")
        try:
            yield ("body", "first reply")
            await asyncio.Event().wait()
        finally:
            context.reset(token)
            closed.set()

    sse_env.client.stream_generate_content.side_effect = upstream
    response = service._build_gemini_streaming_response(
        client=sse_env.client, req=request_body, model_path="gemini-test"
    )

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            raise OSError("offline client disconnected")

    with pytest.raises(OSError, match="disconnected"):
        await response.stream_response(send)
    assert closed.is_set()
    assert not _live_sse_tasks()
    service.runtime_state.record.assert_not_called()


async def test_slow_client_send_timeout_releases_upstream_and_lease(sse_env, request_body, monkeypatch):
    service = sse_env.service
    monkeypatch.setattr(service, "SSE_SEND_TIMEOUT_SECONDS", 0.02)
    closed = asyncio.Event()

    async def upstream(**kwargs):
        try:
            yield ("body", "first reply")
            await asyncio.Event().wait()
        finally:
            closed.set()

    sse_env.client.stream_generate_content.side_effect = upstream
    response = service._build_gemini_streaming_response(
        client=sse_env.client, req=request_body, model_path="gemini-test"
    )

    async def stalled_send(message):
        if message["type"] == "http.response.body":
            await asyncio.Event().wait()

    with pytest.raises(TimeoutError):
        await response.stream_response(stalled_send)
    assert closed.is_set()
    assert not sse_env.leased
    assert not _live_sse_tasks()


async def test_asgi_disconnect_cancels_producer_before_first_model_chunk(
    sse_env, request_body
):
    service = sse_env.service
    sent_heartbeat = asyncio.Event()
    closed = asyncio.Event()

    async def upstream(**kwargs):
        try:
            await asyncio.Event().wait()
            yield ("body", "unreachable")  # pragma: no cover
        finally:
            # Cleanup contains real await points, as closing CDP/upstream I/O
            # does. An ASGI cancellation must wait for those scopes to close.
            await asyncio.sleep(0.01)
            closed.set()

    sse_env.client.stream_generate_content.side_effect = upstream
    response = service._build_gemini_streaming_response(
        client=sse_env.client, req=request_body, model_path="gemini-test"
    )

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            assert message["body"] == b"\n\n"
            sent_heartbeat.set()

    async def receive():
        await sent_heartbeat.wait()
        return {"type": "http.disconnect"}

    await asyncio.wait_for(
        response({"type": "http", "asgi": {"spec_version": "2.0"}}, receive, send),
        1,
    )
    assert closed.is_set()
    assert not _live_sse_tasks()
    service.runtime_state.record.assert_not_called()


@pytest.mark.parametrize("spec", ["2.0", "2.4"])
@pytest.mark.parametrize("phase", ["before_output", "during_output"])
async def test_repeated_interrupt_and_immediate_reroll_releases_only_old_request(
    sse_env, request_body, monkeypatch, spec, phase
):
    from starlette.requests import ClientDisconnect

    from aistudio_api.application.inference_limits import InferenceLimiter

    service = sse_env.service
    limiter = InferenceLimiter(concurrency=1, max_waiting=2, wait_seconds=1)
    monkeypatch.setattr(service, "inference_limiter", limiter)
    entered = asyncio.Event()
    calls, closed = [], []

    async def upstream(**kwargs):
        call = len(calls)
        calls.append(call)
        try:
            if call % 2 == 0:
                entered.set()
                if phase == "during_output":
                    yield "body", "old partial output"
                await asyncio.Event().wait()
            else:
                yield "body", "new roll completed"
                yield "finish_reason", 1
                yield "done", None
        finally:
            await asyncio.sleep(0.002)
            closed.append(call)

    sse_env.client.stream_generate_content.side_effect = upstream
    for _ in range(5):
        entered.clear()
        disconnected = asyncio.Event()
        old = service._build_gemini_streaming_response(client=sse_env.client, req=request_body, model_path="gemini-test")

        async def receive(disconnected=disconnected):
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message, disconnected=disconnected):
            if disconnected.is_set() and spec == "2.4":
                raise OSError("synthetic client abort")

        old_task = asyncio.create_task(old({"type": "http", "asgi": {"spec_version": spec}}, receive, send))
        await asyncio.wait_for(entered.wait(), 1)
        disconnected.set()
        # Start the new roll while the old request is still cleaning up.
        new = service._build_gemini_streaming_response(client=sse_env.client, req=request_body, model_path="gemini-test")

        async def collect(new=new):
            return "".join([chunk async for chunk in new.body_iterator])

        new_task = asyncio.create_task(collect())
        results = await asyncio.wait_for(asyncio.gather(old_task, new_task, return_exceptions=True), 1)
        assert results[0] is None or isinstance(results[0], ClientDisconnect)
        assert "new roll completed" in results[1] and '"finishReason": "STOP"' in results[1]
        assert not sse_env.leased and limiter.active == 0 and limiter.waiting == 0
        assert not _live_sse_tasks()
    assert len(calls) == len(closed) == 10
    assert service.runtime_state.record.call_count == 5
    sse_env.client.clear_templates.assert_not_called()
    service.try_switch_account.assert_not_awaited()


async def test_heartbeat_does_not_prevent_retry_or_change_usage(sse_env, request_body):
    from aistudio_api.domain.errors import RequestError

    service = sse_env.service
    attempts = []
    closed = []
    usage = {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13}

    async def upstream(**kwargs):
        attempt = len(attempts)
        attempts.append(kwargs)
        try:
            if attempt == 0:
                yield ("thought_signature", "metadata-only")
                await asyncio.sleep(0.03)
                raise RequestError(204, "No content")
            yield ("body", "completed after retry")
            yield ("usage", usage)
            yield ("finish_reason", 1)
        finally:
            closed.append(attempt)

    sse_env.client.stream_generate_content.side_effect = upstream
    response = await service.handle_gemini_generate_content(
        "gemini-test", request_body, sse_env.client, stream=True
    )
    chunks = [chunk async for chunk in response.body_iterator]
    assert "\n\n" in chunks
    assert len(attempts) == 2
    assert closed == [0, 1]
    assert attempts[0]["force_refresh_capture"] is False
    assert attempts[1]["force_refresh_capture"] is True
    payloads = [json.loads(chunk[6:]) for chunk in chunks if chunk.startswith("data: ")]
    assert payloads[0]["candidates"][0]["content"]["parts"][0]["text"] == "completed after retry"
    assert "thoughtSignature" not in payloads[0]["candidates"][0]["content"]["parts"][0]
    assert payloads[-1]["usageMetadata"]["totalTokenCount"] == 13
    service.runtime_state.record.assert_called_once_with(
        "models/gemini-test", "success", usage
    )
    assert not _live_sse_tasks()


async def test_error_after_text_stays_in_band_without_retry(sse_env, request_body):
    from aistudio_api.domain.errors import UsageLimitExceeded

    service = sse_env.service
    closed = asyncio.Event()

    async def upstream(**kwargs):
        try:
            yield ("body", "partial reply")
            raise UsageLimitExceeded("offline quota exhausted")
        finally:
            closed.set()

    sse_env.client.stream_generate_content.side_effect = upstream
    response = await service.handle_gemini_generate_content(
        "gemini-test", request_body, sse_env.client, stream=True
    )
    chunks = [chunk async for chunk in response.body_iterator]
    assert response.status_code == 200
    assert "partial reply" in chunks[0]
    error = json.loads(chunks[-1][6:])["error"]
    assert error["code"] == 429
    assert error["status"] == "RESOURCE_EXHAUSTED"
    service.try_switch_account.assert_not_awaited()
    assert sse_env.client.stream_generate_content.call_count == 1
    assert closed.is_set()
    assert not _live_sse_tasks()


async def test_state_change_after_preflight_is_still_an_in_band_error(
    sse_env, request_body
):
    from fastapi import HTTPException

    service = sse_env.service
    response = await service.handle_gemini_generate_content(
        "gemini-test", request_body, sse_env.client, stream=True
    )
    service.ensure_active_account.side_effect = HTTPException(503, "account removed")
    chunks = [chunk async for chunk in response.body_iterator]
    assert response.status_code == 200
    assert json.loads(chunks[-1][6:])["error"]["code"] == 503
    sse_env.client.stream_generate_content.assert_not_called()
    assert not _live_sse_tasks()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    ("outcome", "event", "status"),
    [("success", "success", 200), ("quota", "rate_limited", 429),
     ("expired", "session_expired", 503), ("auth", "auth_error", 503)],
)
async def test_in_flight_result_is_attributed_to_its_leased_account_after_switch(
    sse_env, request_body, stream, outcome, event, status
):
    from fastapi import HTTPException

    from aistudio_api.domain.errors import (
        AuthError,
        SessionExpiredError,
        UsageLimitExceeded,
    )
    from aistudio_api.domain.models import Candidate, ModelOutput

    service = sse_env.service
    account_a = sse_env.accounts[0]
    account_b = SimpleNamespace(id="other-account")
    sse_env.accounts.append(account_b)
    usage = {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}

    def finish_original_request():
        assert sse_env.leased == [sse_env.context]
        # Activation changes registry.active while A's actual request lease
        # remains pinned. Completion/error attribution must still use A.
        sse_env.accounts[:] = [account_b, account_a]
        if outcome == "quota":
            raise UsageLimitExceeded("offline quota")
        if outcome == "expired":
            raise SessionExpiredError("offline login redirect")
        if outcome == "auth":
            raise AuthError("offline forbidden")

    async def generate(**kwargs):
        finish_original_request()
        return ModelOutput(candidates=[Candidate(text="reply")], usage=usage)

    async def generate_stream(**kwargs):
        finish_original_request()
        yield ("body", "reply")
        yield ("usage", usage)

    async def failover(**kwargs):
        assert not sse_env.leased, "release the old lease before switching"
        assert kwargs["failed_account_id"] == "test-account"
        if kwargs.get("is_auth_error") and not kwargs.get("is_session_expired"):
            # Retryable 403 recording is intentionally serialized by the real
            # orchestrator so stale failures cannot re-lock a refreshed context.
            service.record_rotator_event(
                "auth_error", model="models/gemini-test", account_id="test-account"
            )
        return False

    sse_env.client.generate_content.side_effect = generate
    sse_env.client.stream_generate_content.side_effect = generate_stream
    service.try_switch_account.side_effect = failover
    if stream:
        response = await service.handle_gemini_generate_content(
            "gemini-test", request_body, sse_env.client, stream=True
        )
        chunks = [chunk async for chunk in response.body_iterator]
        if status != 200:
            assert json.loads(chunks[-1][6:])["error"]["code"] == status
    elif status == 200:
        await service.handle_gemini_generate_content(
            "gemini-test", request_body, sse_env.client, stream=False
        )
    else:
        with pytest.raises(HTTPException) as caught:
            await service.handle_gemini_generate_content(
                "gemini-test", request_body, sse_env.client, stream=False
            )
        assert caught.value.status_code == status

    service.record_rotator_event.assert_called_once_with(
        event, model="models/gemini-test", account_id="test-account",
        **({"auth_generation": None} if outcome == "success" else {}),
    )
    assert sse_env.accounts[0] is account_b
    assert not sse_env.leased
    if outcome == "expired":
        assert service.try_switch_account.await_args.kwargs["failure_recorded"] is True
    elif outcome == "auth":
        assert service.try_switch_account.await_args.kwargs["failed_context_id"] == 1
        assert not service.try_switch_account.await_args.kwargs.get("failure_recorded")
    assert not _live_sse_tasks()


async def test_expired_session_after_body_is_recorded_without_switching(
    sse_env, request_body
):
    from aistudio_api.domain.errors import SessionExpiredError

    async def upstream(**kwargs):
        yield ("body", "partial reply")
        sse_env.accounts.insert(0, SimpleNamespace(id="other-account"))
        raise SessionExpiredError("offline login redirect")

    sse_env.client.stream_generate_content.side_effect = upstream
    response = await sse_env.service.handle_gemini_generate_content(
        "gemini-test", request_body, sse_env.client, stream=True
    )
    chunks = [chunk async for chunk in response.body_iterator]
    assert json.loads(chunks[-1][6:])["error"]["code"] == 503
    sse_env.service.record_rotator_event.assert_called_once_with(
        "session_expired", model="models/gemini-test", account_id="test-account"
    )
    sse_env.service.try_switch_account.assert_not_awaited()
    assert not sse_env.leased
    assert not _live_sse_tasks()


async def test_last_attempt_failure_does_not_become_success(sse_env, request_body, monkeypatch):
    from aistudio_api.domain.errors import UsageLimitExceeded

    monkeypatch.setattr(sse_env.service, "MAX_RETRIES", 1)
    sse_env.service.try_switch_account.return_value = True

    async def upstream(**kwargs):
        raise UsageLimitExceeded("offline quota")
        yield ("body", "unreachable")  # pragma: no cover

    sse_env.client.stream_generate_content.side_effect = upstream
    response = await sse_env.service.handle_gemini_generate_content(
        "gemini-test", request_body, sse_env.client, stream=True
    )
    chunks = [chunk async for chunk in response.body_iterator]
    assert json.loads(chunks[-1][6:])["error"]["code"] == 429
    sse_env.service.try_switch_account.assert_not_awaited()
    assert all(call.args[1] != "success" for call in sse_env.service.runtime_state.record.call_args_list)
    assert not sse_env.leased


async def test_retry_drops_metadata_from_the_failed_attempt(sse_env, request_body):
    from aistudio_api.domain.errors import RequestError

    attempt = 0

    async def upstream(**kwargs):
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            yield ("usage", {"total_tokens": 999})
            yield ("response_id", "failed-response")
            yield ("finish_reason", 2)
            yield ("thought_signature", "failed-signature")
            raise RequestError(204, "No content")
        yield ("body", "successful reply")

    sse_env.client.stream_generate_content.side_effect = upstream
    response = await sse_env.service.handle_gemini_generate_content(
        "gemini-test", request_body, sse_env.client, stream=True
    )
    chunks = [chunk async for chunk in response.body_iterator if chunk.startswith("data: ")]
    payloads = [json.loads(chunk[6:]) for chunk in chunks]
    assert len(payloads) == 2
    assert all("responseId" not in payload for payload in payloads)
    assert "thoughtSignature" not in payloads[0]["candidates"][0]["content"]["parts"][0]
    assert payloads[-1]["candidates"][0]["finishReason"] == "FINISH_REASON_UNSPECIFIED"
    assert payloads[-1]["usageMetadata"]["totalTokenCount"] == 0
    assert not sse_env.leased


@pytest.mark.parametrize("stream", [False, True])
async def test_bootstrap_failure_keeps_the_attempted_account_when_active_changed(
    sse_env, request_body, stream
):
    from fastapi import HTTPException

    from aistudio_api.application.account_orchestrator import AccountPreparationError
    from aistudio_api.domain.errors import SessionExpiredError

    service = sse_env.service

    async def initialize(*args, **kwargs):
        sse_env.accounts.insert(0, SimpleNamespace(id="other-account"))
        raise AccountPreparationError(
            "test-account", SessionExpiredError("offline bootstrap login redirect")
        )

    service.ensure_active_account.side_effect = initialize
    if stream:
        response = await service.handle_gemini_generate_content(
            "gemini-test", request_body, sse_env.client, stream=True
        )
        chunks = [chunk async for chunk in response.body_iterator]
        assert json.loads(chunks[-1][6:])["error"]["code"] == 503
    else:
        with pytest.raises(HTTPException) as caught:
            await service.handle_gemini_generate_content(
                "gemini-test", request_body, sse_env.client, stream=False
            )
        assert caught.value.status_code == 503
    service.record_rotator_event.assert_called_once_with(
        "session_expired", model="models/gemini-test", account_id="test-account"
    )
    assert service.try_switch_account.await_args.kwargs["failed_account_id"] == "test-account"
    sse_env.client.generate_content.assert_not_awaited()
    sse_env.client.stream_generate_content.assert_not_called()
    assert not sse_env.leased
    assert not _live_sse_tasks()


@pytest.mark.parametrize("stream", [False, True])
async def test_failure_while_preparing_failover_target_ends_without_recursive_switch(
    sse_env, request_body, stream
):
    from fastapi import HTTPException

    from aistudio_api.application.account_orchestrator import AccountPreparationError
    from aistudio_api.domain.errors import SessionExpiredError, UsageLimitExceeded

    service = sse_env.service
    sse_env.accounts.append(SimpleNamespace(id="other-account"))

    async def generate(**kwargs):
        raise UsageLimitExceeded("offline original quota")

    async def generate_stream(**kwargs):
        raise UsageLimitExceeded("offline original quota")
        yield ("body", "unreachable")  # pragma: no cover

    async def switch(**kwargs):
        assert not sse_env.leased
        raise AccountPreparationError(
            "other-account", SessionExpiredError("offline failover login redirect")
        )

    sse_env.client.generate_content.side_effect = generate
    sse_env.client.stream_generate_content.side_effect = generate_stream
    service.try_switch_account.side_effect = switch
    if stream:
        response = await service.handle_gemini_generate_content(
            "gemini-test", request_body, sse_env.client, stream=True
        )
        chunks = [chunk async for chunk in response.body_iterator]
        assert json.loads(chunks[-1][6:])["error"]["code"] == 503
    else:
        with pytest.raises(HTTPException) as caught:
            await service.handle_gemini_generate_content(
                "gemini-test", request_body, sse_env.client, stream=False
            )
        assert caught.value.status_code == 503
    assert service.try_switch_account.await_count == 1
    events = service.record_rotator_event.call_args_list
    assert [(call.args[0], call.kwargs["account_id"]) for call in events] == [
        ("rate_limited", "test-account"), ("session_expired", "other-account")
    ]
    assert not sse_env.leased
    assert not _live_sse_tasks()


async def test_nonstream_invalid_request_does_not_bootstrap(sse_env, request_body):
    from fastapi import HTTPException

    request_body.contents.clear()
    with pytest.raises(HTTPException) as caught:
        await sse_env.service.handle_gemini_generate_content(
            "gemini-test", request_body, sse_env.client, stream=False
        )
    assert caught.value.status_code == 400
    sse_env.service.ensure_active_account.assert_not_awaited()
    sse_env.client.generate_content.assert_not_awaited()
    sse_env.service.runtime_state.record.assert_not_called()
