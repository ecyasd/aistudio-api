"""Generation ownership and the HTTP adapter's local preflight contract."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from aistudio_api.application import generation
from aistudio_api.application.chat_service import NormalizedGeminiRequest
from aistudio_api.application.inference_limits import InferenceLimiter
from aistudio_api.domain.errors import RequestError
from aistudio_api.domain.models import Candidate, ContentPart, ModelOutput


@pytest.fixture
def execution(monkeypatch):
    account = ContextVar("synthetic-generation-account", default=None)
    owners, leases = [], []
    context = SimpleNamespace(auth_file="synthetic-account", context_id=7, auth_generation=3)

    @asynccontextmanager
    async def request_scope():
        token = account.set(context)
        owners.append(asyncio.current_task())
        leases.append(context)
        try:
            yield context
        finally:
            assert account.get() is context
            leases.remove(context)
            account.reset(token)

    async def checkpoint(received):
        assert received is account.get() is context
        owners.append(asyncio.current_task())

    output = ModelOutput(candidates=[Candidate(parts=[ContentPart(text="reply")], finish_reason=1)])

    async def chunks(**_kwargs):
        assert account.get() is context
        owners.append(asyncio.current_task())
        yield "parts", output.candidates[0].parts
        yield "finish_reason", 1
        yield "usage", {"total_tokens": 2}

    async def complete(**_kwargs):
        assert account.get() is context
        owners.append(asyncio.current_task())
        return output

    client = SimpleNamespace(
        session=SimpleNamespace(request_scope=request_scope, persist_context_cookies=checkpoint),
        generate_content=complete, stream_generate_content=chunks,
    )
    limiter = InferenceLimiter(concurrency=1, max_waiting=0)
    record = MagicMock()
    monkeypatch.setattr(generation, "inference_limiter", limiter)
    monkeypatch.setattr(generation, "ensure_active_account", AsyncMock())
    monkeypatch.setattr(generation, "account_id_for_auth_file", lambda _path: "synthetic-account")
    monkeypatch.setattr(generation, "record_rotator_event", record)
    monkeypatch.setattr(generation.runtime_state, "record", MagicMock())
    request = NormalizedGeminiRequest(
        model="gemini-synthetic", contents=[], system_instruction=None, tools=None,
        safety_settings=None, capture_prompt="synthetic", capture_images=None,
    )
    return SimpleNamespace(
        client=client, request=request, owners=owners, leases=leases, account=account,
        limiter=limiter, record=record, output=output,
    )


@pytest.mark.parametrize("stream", [False, True])
async def test_both_modes_release_their_original_lease_and_record_one_success(execution, stream):
    env = execution
    recover = AsyncMock(return_value=False)
    run = generation.GenerationRun(env.client, env.request, auto_continue=False, recover=recover)
    events = []

    async def deliver(event):
        assert env.account.get() is env.leases[0]
        events.append(event)
        return True

    if stream:
        result = await run.stream(deliver)
        assert result.finish_reason == 1
        assert events[0].payload == env.output.candidates[0].parts
    else:
        assert await run.generate() is env.output
    assert not env.leases and env.account.get() is None
    assert len(set(env.owners)) == 1
    assert env.limiter.active == 0
    env.record.assert_called_once_with(
        "success", model="gemini-synthetic", account_id="synthetic-account", auth_generation=3,
    )
    recover.assert_not_awaited()
    with pytest.raises(RuntimeError, match="only be consumed once"):
        await run.generate()


async def test_discarded_attempt_metadata_never_crosses_a_retry(execution):
    env = execution
    calls, delivered, failures = [], [], []

    async def chunks(**_kwargs):
        calls.append(True)
        if len(calls) == 1:
            yield "response_id", "discarded"
            yield "usage", {"total_tokens": 999}
            raise RequestError(503, "synthetic retry")
        yield "response_id", "current"
        yield "parts", [ContentPart(text="reply")]
        yield "finish_reason", 1
        yield "usage", {"total_tokens": 2}

    async def recover(failure):
        assert not env.leases
        failures.append(failure)
        return True

    async def deliver(event):
        delivered.append(event)
        return True

    env.client.stream_generate_content = chunks
    run = generation.GenerationRun(env.client, env.request, auto_continue=False, recover=recover)
    result = await run.stream(deliver)
    assert len(calls) == 2 and len(failures) == 1
    assert not failures[0].replay_forbidden
    assert result.response_id == "current" and result.usage == {"total_tokens": 2}
    assert all(event.response_id == "current" for event in delivered)


async def test_publication_blocks_replay_even_if_recovery_requests_it(execution):
    env = execution
    calls = []

    async def chunks(**_kwargs):
        calls.append(True)
        yield "parts", [ContentPart(text="published")]
        raise RequestError(503, "synthetic failure after output")

    env.client.stream_generate_content = chunks
    recover = AsyncMock(return_value=True)
    run = generation.GenerationRun(env.client, env.request, auto_continue=False, recover=recover)

    async def deliver(_event):
        return True

    with pytest.raises(RequestError):
        await run.stream(deliver)
    assert len(calls) == 1 and not env.leases and env.limiter.active == 0
    assert recover.await_args.args[0].replay_forbidden
    env.record.assert_not_called()


@pytest.mark.parametrize("replaced_credentials", [False, True])
@pytest.mark.parametrize("failure", ["cdp_closed", "browser_cancelled"])
async def test_dispatched_nonstream_failure_never_replays(
    execution, monkeypatch, replaced_credentials, failure,
):
    from fastapi import HTTPException

    from aistudio_api.api import gemini
    from aistudio_api.api.schemas import GeminiGenerateContentRequest
    from aistudio_api.infrastructure.browser.cdp_client import CDPPage
    from aistudio_api.infrastructure.browser.scripts import (
        HOOKED_CLEANUP_JS,
        HOOKED_REQUEST_JS,
    )
    from aistudio_api.infrastructure.gateway.transport import XHRStreamTransport

    env = execution
    page = MagicMock(spec=CDPPage)
    page.get_cookies = AsyncMock(return_value=[])
    dispatched, cleaned = [], []

    async def evaluate(script, args=None, **_kwargs):
        if script == HOOKED_REQUEST_JS:
            dispatched.append(args["rid"])
            if failure == "cdp_closed":
                raise RuntimeError("CDP connection closed: synthetic-private-detail")
            return {"status": 0, "body": "cancelled"}
        if script == HOOKED_CLEANUP_JS:
            cleaned.append(args)
        return None

    page.evaluate = AsyncMock(side_effect=evaluate)
    transport = XHRStreamTransport()

    async def complete(**_kwargs):
        await transport.send_hooked_request(
            page, url="https://synthetic.invalid", headers={}, body="[]", timeout_ms=500,
        )
        raise AssertionError("Failed delivery cannot return a completed generation")

    env.client.generate_content = complete
    env.client.clear_templates = MagicMock()
    switch = AsyncMock(return_value=True)
    monkeypatch.setattr(gemini, "try_switch_account", switch)
    monkeypatch.setattr(
        gemini, "account_generation_is_current", lambda *_args: not replaced_credentials,
    )
    run = gemini._generation_run(
        env.client, env.request, GeminiGenerateContentRequest(contents=[]), "gemini-synthetic",
    )
    with pytest.raises(HTTPException) as caught:
        await run.generate()
    assert len(dispatched) == 1 and cleaned == dispatched
    assert caught.value.status_code == 503
    assert caught.value.detail["type"] == "browser_transport_error"
    assert "synthetic-private-detail" not in str(caught.value.detail)
    switch.assert_not_awaited()
    env.client.clear_templates.assert_not_called()
    env.record.assert_not_called()
    assert not env.leases and env.account.get() is None and env.limiter.active == 0
    assert not transport._cleanup_tasks


@pytest.mark.parametrize("stream", [False, True])
async def test_completed_generation_survives_checkpoint_reaching_request_deadline(
    execution, monkeypatch, stream,
):
    env = execution
    checkpoint_closed = asyncio.Event()
    recover = AsyncMock(return_value=False)
    run = generation.GenerationRun(
        env.client, env.request, auto_continue=True, recover=recover,
    )

    async def checkpoint(context):
        assert context is env.account.get() is env.leases[0]
        # The upstream response completed within its budget. Only the optional
        # checkpoint crosses the remaining request deadline.
        try:
            await asyncio.Event().wait()
        finally:
            checkpoint_closed.set()

    async def complete(*_args, **_kwargs):
        run._deadline = asyncio.get_running_loop().time() + 0.01
        return env.output

    async def chunks(*_args, **_kwargs):
        yield "parts", env.output.candidates[0].parts
        yield "finish_reason", 1
        run._deadline = asyncio.get_running_loop().time() + 0.01

    env.client.session.persist_context_cookies = checkpoint
    monkeypatch.setattr(generation.continuation, "generate_content", complete)
    monkeypatch.setattr(generation.continuation, "stream_generate_content", chunks)
    monkeypatch.setattr(generation, "COOKIE_CHECKPOINT_TIMEOUT_SECONDS", 0.05)
    if stream:
        result = await run.stream(AsyncMock(return_value=True))
        assert result.finish_reason == 1
    else:
        assert await run.generate() is env.output

    assert checkpoint_closed.is_set()
    assert not env.leases and env.account.get() is None and env.limiter.active == 0
    recover.assert_not_awaited()
    env.record.assert_called_once_with(
        "success", model="gemini-synthetic", account_id="synthetic-account", auth_generation=3,
    )


@pytest.mark.parametrize("stream", [False, True])
async def test_diagnostics_track_actual_leases_across_retry(execution, monkeypatch, stream):
    from aistudio_api.infrastructure.utils import error_records

    env = execution
    ids = iter(["acc_11111111", "acc_22222222"])
    monkeypatch.setattr(generation, "account_id_for_auth_file", lambda _path: next(ids))
    calls = []

    async def complete(**_kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise RequestError(503, "synthetic unpublished failure")
        return env.output

    async def chunks(**_kwargs):
        await complete()
        yield "parts", env.output.candidates[0].parts
        yield "finish_reason", 1

    env.client.generate_content = complete
    env.client.stream_generate_content = chunks
    token = error_records.bind_request_context()
    try:
        run = generation.GenerationRun(env.client, env.request, auto_continue=False, recover=AsyncMock(return_value=True))
        if stream:
            await run.stream(AsyncMock(return_value=True))
        else:
            await run.generate()
        context = error_records.get_request_context()
        assert context is not None and context.account_ids == ["acc_11111111", "acc_22222222"]
        assert not env.leases
    finally:
        error_records.reset_request_context(token)


@pytest.mark.parametrize("stream", [False, True])
async def test_invalid_input_does_not_occupy_inference_capacity(monkeypatch, stream):
    from fastapi import HTTPException

    from aistudio_api.api import gemini
    from aistudio_api.api.schemas import GeminiGenerateContentRequest

    slot = MagicMock(side_effect=AssertionError("Invalid input entered admission"))
    monkeypatch.setattr(generation.inference_limiter, "slot", slot)
    request = GeminiGenerateContentRequest(contents=[])
    with pytest.raises(HTTPException) as error:
        await gemini.handle_gemini_generate_content(
            "gemini-synthetic", request, SimpleNamespace(), stream=stream,
        )
    assert error.value.status_code == 400
    slot.assert_not_called()
