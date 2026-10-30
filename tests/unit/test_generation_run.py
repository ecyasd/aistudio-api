"""Generation ownership and the HTTP adapter's local preflight contract."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from aistudio_api.application import (
    generation,
    generation as generation_execution,
    generation_recovery as recovery,
)
from aistudio_api.application.chat_service import NormalizedGeminiRequest
from aistudio_api.application.generation_errors import GenerationFailure
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
async def test_default_generation_run_needs_no_http_recovery_callback(execution, stream):
    env = execution
    run = generation.GenerationRun(env.client, env.request, auto_continue=False)

    async def deliver(_event):
        return True

    if stream:
        assert (await run.stream(deliver)).finish_reason == 1
    else:
        assert await run.generate() is env.output
    assert not env.leases and env.limiter.active == 0
    env.record.assert_called_once()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("kind,status,attempts,counter", [
    ("timeout", 504, 1, None),
    ("transport", 503, 1, None),
    ("auth", 503, 1, None),
    ("quota", 429, 1, "rate_limited"),
    ("request", 502, 1, None),
    ("incomplete", 502, 2, None),
    ("empty", 502, 2, None),
    ("rpc", 404, 5, None),
    ("internal", 500, 1, "errors"),
])
async def test_default_recovery_preserves_status_attempts_and_accounting(
    execution, monkeypatch, stream, kind, status, attempts, counter,
):
    from aistudio_api.domain.errors import (
        AuthError,
        BrowserTransportError,
        IncompleteResponseError,
        UsageLimitExceeded,
    )

    env = execution
    failures = {
        "timeout": TimeoutError("synthetic-private-detail"),
        "transport": BrowserTransportError("synthetic-private-detail"),
        "auth": AuthError("synthetic-private-detail"),
        "quota": UsageLimitExceeded("synthetic-private-detail"),
        "request": RequestError(502, "synthetic request failure"),
        "incomplete": IncompleteResponseError(),
        "empty": RequestError(204, "synthetic empty response"),
        "rpc": RequestError(404, "Ambiguous request for service '' GenerateContent"),
        "internal": RuntimeError("synthetic-private-detail"),
    }
    error = failures[kind]
    calls = []

    async def complete(**_kwargs):
        calls.append(True)
        raise error

    async def chunks(**_kwargs):
        await complete()
        yield "body", "unreachable"

    async def switch(**_kwargs):
        assert not env.leases
        return False

    env.client.generate_content = complete
    env.client.stream_generate_content = chunks
    env.client.clear_templates = MagicMock()
    switch_mock = AsyncMock(side_effect=switch)
    records = MagicMock()
    monkeypatch.setattr(recovery, "try_switch_account", switch_mock)
    monkeypatch.setattr(recovery, "record_rotator_event", env.record)
    monkeypatch.setattr(recovery, "account_generation_is_current", lambda *_args: True)
    monkeypatch.setattr(recovery, "record_failure", records)
    monkeypatch.setattr(recovery, "quota_unavailable_error", lambda _model: GenerationFailure(
        429, reason="rate_limit_exceeded", message="synthetic cooldown", retry_after_seconds=17,
    ))
    run = generation.GenerationRun(env.client, env.request, auto_continue=False)
    with pytest.raises(GenerationFailure) as caught:
        if stream:
            await run.stream(AsyncMock(return_value=True))
        else:
            await run.generate()
    assert caught.value.description.status == status
    assert caught.value.original_error is error
    assert len(calls) == attempts
    assert records.call_count == attempts
    assert all(call.args[0] is error for call in records.call_args_list)
    if counter:
        generation.runtime_state.record.assert_called_once_with(env.request.model, counter)
    else:
        generation.runtime_state.record.assert_not_called()
    if kind in {"auth", "quota"}:
        switch_mock.assert_awaited_once()
    else:
        switch_mock.assert_not_awaited()
    if kind in {"timeout", "transport", "auth", "quota", "internal"}:
        assert "synthetic-private-detail" not in caught.value.description.public_message
    assert not env.leases and env.limiter.active == 0


@pytest.mark.parametrize("kind", ["timeout", "transport"])
@pytest.mark.parametrize("number", [0, 4])
async def test_replaced_recovery_preparation_target_keeps_its_bounded_exception(
    execution, monkeypatch, kind, number,
):
    from aistudio_api.application.account_orchestrator import AccountPreparationError
    from aistudio_api.domain.errors import AuthError, BrowserTransportError

    env = execution
    source_error = AuthError("synthetic source auth")
    target_error = TimeoutError() if kind == "timeout" else BrowserTransportError()
    env.client.clear_templates = MagicMock()
    switch = AsyncMock(side_effect=AccountPreparationError(
        "replacement", target_error, auth_generation=2,
    ))
    records = MagicMock()
    monkeypatch.setattr(recovery, "try_switch_account", switch)
    monkeypatch.setattr(recovery, "record_rotator_event", env.record)
    monkeypatch.setattr(recovery, "record_failure", records)
    monkeypatch.setattr(recovery, "account_generation_is_current", lambda account, _generation: account == "source")
    result = await recovery.recover_attempt(env.client, generation.AttemptFailure(
        source_error, number, env.request.model,
        generation.AttemptIdentity("source", 1, 3), False,
    ))
    if number == 0:
        assert isinstance(result, recovery.RetryAttempt)
        switch.assert_awaited_once()
        assert [call.args[0] for call in records.call_args_list] == [source_error, target_error]
        assert all(call.kwargs["retrying"] for call in records.call_args_list)
    else:
        assert isinstance(result, recovery.StopGeneration)
        assert result.failure.description.status == 503
        switch.assert_not_awaited()
    assert not env.owners and not env.leases


@pytest.mark.parametrize("wrapped", [False, True])
async def test_normalized_terminal_never_reenters_recovery_or_gains_a_cause_cycle(
    execution, monkeypatch, wrapped,
):
    from aistudio_api.application.account_orchestrator import AccountPreparationError

    terminal = GenerationFailure(503, reason="account_not_ready", message="synthetic unavailable")
    error = AccountPreparationError("replacement", terminal, auth_generation=2) if wrapped else terminal
    records, switch, current = MagicMock(), AsyncMock(), MagicMock()
    monkeypatch.setattr(recovery, "record_failure", records)
    monkeypatch.setattr(recovery, "try_switch_account", switch)
    monkeypatch.setattr(recovery, "account_generation_is_current", current)
    result = await recovery.recover_attempt(execution.client, generation.AttemptFailure(
        error, 0, execution.request.model, generation.AttemptIdentity(), False,
    ))
    assert isinstance(result, recovery.StopGeneration) and result.failure is terminal
    assert terminal.original_error is None
    records.assert_not_called()
    switch.assert_not_awaited()
    current.assert_not_called()


@pytest.mark.parametrize("stream", [False, True])
async def test_both_modes_release_their_original_lease_and_record_one_success(execution, monkeypatch, stream):
    env = execution
    recover = AsyncMock()
    monkeypatch.setattr(generation, "recover_attempt", recover)
    run = generation.GenerationRun(env.client, env.request, auto_continue=False)
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


async def test_discarded_attempt_metadata_never_crosses_a_retry(execution, monkeypatch):
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

    async def recover(_client, failure):
        assert not env.leases
        failures.append(failure)
        return recovery.RetryAttempt()

    async def deliver(event):
        delivered.append(event)
        return True

    env.client.stream_generate_content = chunks
    monkeypatch.setattr(generation, "recover_attempt", recover)
    run = generation.GenerationRun(env.client, env.request, auto_continue=False)
    result = await run.stream(deliver)
    assert len(calls) == 2 and len(failures) == 1
    assert not failures[0].replay_forbidden
    assert result.response_id == "current" and result.usage == {"total_tokens": 2}
    assert all(event.response_id == "current" for event in delivered)


async def test_publication_blocks_replay_even_if_recovery_requests_it(execution, monkeypatch):
    env = execution
    calls = []

    async def chunks(**_kwargs):
        calls.append(True)
        yield "parts", [ContentPart(text="published")]
        raise RequestError(503, "synthetic failure after output")

    env.client.stream_generate_content = chunks
    recover = AsyncMock(return_value=recovery.RetryAttempt())
    monkeypatch.setattr(generation, "recover_attempt", recover)
    run = generation.GenerationRun(env.client, env.request, auto_continue=False)

    async def deliver(_event):
        return True

    with pytest.raises(GenerationFailure):
        await run.stream(deliver)
    assert len(calls) == 1 and not env.leases and env.limiter.active == 0
    assert recover.await_args.args[1].replay_forbidden
    env.record.assert_not_called()


@pytest.mark.parametrize("replaced_credentials", [False, True])
@pytest.mark.parametrize("failure", ["cdp_closed", "browser_cancelled"])
async def test_dispatched_nonstream_failure_never_replays(
    execution, monkeypatch, replaced_credentials, failure,
):

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
    monkeypatch.setattr(recovery, "try_switch_account", switch)
    monkeypatch.setattr(
        recovery, "account_generation_is_current", lambda *_args: not replaced_credentials,
    )
    run = generation_execution.GenerationRun(env.client, env.request, auto_continue=GeminiGenerateContentRequest(contents=[]).autoContinue)
    with pytest.raises(GenerationFailure) as caught:
        await run.generate()
    assert len(dispatched) == 1 and cleaned == dispatched
    assert caught.value.description.status == 503
    assert caught.value.description.reason.value == "browser_transport_error"
    assert "synthetic-private-detail" not in str(caught.value.description.public_message)
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
    recover = AsyncMock()
    monkeypatch.setattr(generation, "recover_attempt", recover)
    run = generation.GenerationRun(env.client, env.request, auto_continue=True)

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
        monkeypatch.setattr(generation, "recover_attempt", AsyncMock(return_value=recovery.RetryAttempt()))
        run = generation.GenerationRun(env.client, env.request, auto_continue=False)
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
