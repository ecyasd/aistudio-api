"""Own generation attempts, admission and the actual account-context lease.

HTTP delivery is injected only for incremental output. The same task prepares,
advances and closes the upstream request; no worker or checkpoint outlives it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, aclosing
from dataclasses import dataclass, field
from typing import TypeVar

from aistudio_api.application import continuation
from aistudio_api.application.account_orchestrator import (
    AUTH_GENERATION_UNTRACKED,
    MAX_RETRIES,
    account_id_for_auth_file,
    ensure_active_account,
    quota_unavailable_error,
    record_rotator_event,
)
from aistudio_api.application.chat_service import NormalizedGeminiRequest
from aistudio_api.application.inference_limits import inference_limiter
from aistudio_api.application.runtime import runtime_state
from aistudio_api.config import settings
from aistudio_api.domain.models import ModelOutput
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.utils.error_records import note_request_account
from aistudio_api.infrastructure.utils.logger import get_logger
from aistudio_api.infrastructure.utils.response_diagnostics import (
    note_attempt,
    note_timing,
)

logger = get_logger("generation")
COOKIE_CHECKPOINT_TIMEOUT_SECONDS = 3.0
T = TypeVar("T")


class GenerationDeadlineExceeded(TimeoutError):
    """The continuation budget expired, rather than an upstream idle timeout."""


@dataclass(frozen=True)
class AttemptIdentity:
    account_id: str | None = None
    context_id: int | None = None
    auth_generation: object = AUTH_GENERATION_UNTRACKED


@dataclass(frozen=True)
class AttemptFailure:
    error: Exception
    number: int
    model: str
    identity: AttemptIdentity
    replay_forbidden: bool


@dataclass(frozen=True)
class GenerationEvent:
    kind: str
    payload: object
    model: str
    response_id: str | None


@dataclass(frozen=True)
class GenerationCompletion:
    model: str
    usage: dict[str, object] | None
    finish_reason: int | str | None
    response_id: str | None


@dataclass
class _Attempt:
    number: int
    identity: AttemptIdentity = field(default_factory=AttemptIdentity)
    published: bool = False
    usage: dict[str, object] | None = None


async def _persist_successful_cookies(client: AIStudioClient, context: object) -> None:
    """Keep optional checkpointing in its original task, lease and small budget."""
    persist = getattr(client.session, "persist_context_cookies", None)
    if persist is None:
        return
    try:
        async with asyncio.timeout(COOKIE_CHECKPOINT_TIMEOUT_SECONDS):
            await persist(context)
    except Exception as exc:
        logger.warning("保存已验证会话 Cookie 失败: type=%s", type(exc).__name__)


class GenerationRun:
    """One request, one admission, and one retry/lease/accounting implementation.

    Call either generate() or stream() once. An incremental delivery callback
    returns true only when it accepts client-visible content; metadata alone
    never commits the original request. Backpressure is outside upstream waits.
    """

    def __init__(
        self,
        client: AIStudioClient,
        request: NormalizedGeminiRequest,
        *,
        auto_continue: bool,
        recover: Callable[[AttemptFailure], Awaitable[bool]],
    ) -> None:
        self.client = client
        self.request = request
        self.auto_continue = auto_continue
        self.recover = recover
        self.identity = AttemptIdentity()
        self.admitted = False
        self._started = False
        self._deadline: float | None = None

    async def _wait(self, work: Awaitable[T]) -> T:
        budget = asyncio.timeout_at(self._deadline)
        try:
            async with budget:
                return await work
        except TimeoutError as exc:
            if budget.expired():
                raise GenerationDeadlineExceeded from exc
            raise

    async def _execute(self, operation: Callable[[_Attempt], Awaitable[T]]) -> T:
        if self._started:
            raise RuntimeError("A generation run can only be consumed once")
        self._started = True
        if self.auto_continue:
            self._deadline = asyncio.get_running_loop().time() + settings.timeout_stream
        async with AsyncExitStack() as admission:
            await self._wait(admission.enter_async_context(inference_limiter.slot()))
            self.admitted = True
            last_error: Exception | None = None
            for number in range(MAX_RETRIES):
                attempt = _Attempt(number)
                self.identity = attempt.identity
                note_attempt(number)
                try:
                    note_timing("account_preparation_started")
                    await self._wait(ensure_active_account(number, model=self.request.model))
                    note_timing("account_ready")
                    async with AsyncExitStack() as lease:
                        context = await self._wait(
                            lease.enter_async_context(self.client.session.request_scope())
                        )
                        attempt.identity = self.identity = AttemptIdentity(
                            account_id_for_auth_file(context.auth_file),
                            context.context_id,
                            getattr(context, "auth_generation", AUTH_GENERATION_UNTRACKED),
                        )
                        note_request_account(attempt.identity.account_id)
                        result = await operation(attempt)
                        await self._wait(_persist_successful_cookies(self.client, context))
                    identity = attempt.identity
                    if identity.auth_generation is AUTH_GENERATION_UNTRACKED:
                        record_rotator_event("success", model=self.request.model,
                                             account_id=identity.account_id)
                    else:
                        record_rotator_event("success", model=self.request.model,
                                             account_id=identity.account_id,
                                             auth_generation=identity.auth_generation)
                    runtime_state.record(self.request.model, "success", attempt.usage)
                    return result
                except GenerationDeadlineExceeded:
                    raise
                except Exception as exc:
                    committed_round = isinstance(exc, continuation.ContinuationError)
                    if isinstance(exc, continuation.ContinuationError):
                        exc = exc.original_error
                    last_error = exc
                    failure = AttemptFailure(
                        exc, number, self.request.model, attempt.identity,
                        attempt.published or committed_round,
                    )
                    retry = await self._wait(self.recover(failure))
                    if retry and not failure.replay_forbidden:
                        continue
                    raise exc
            raise quota_unavailable_error(self.request.model) from last_error

    async def generate(self) -> ModelOutput:
        async def complete(attempt: _Attempt) -> ModelOutput:
            output = await self._wait(continuation.generate_content(
                self.client, self.request, auto_continue=self.auto_continue,
            ))
            attempt.usage = output.usage
            return output

        return await self._execute(complete)

    async def stream(
        self, deliver: Callable[[GenerationEvent], Awaitable[bool]],
    ) -> GenerationCompletion:
        async def incremental(attempt: _Attempt) -> GenerationCompletion:
            finish_reason: int | str | None = None
            response_id: str | None = None
            async with aclosing(continuation.stream_generate_content(
                self.client, self.request, auto_continue=self.auto_continue,
                force_refresh_capture=attempt.number > 0,
            )) as upstream:
                while True:
                    try:
                        kind, payload = await self._wait(anext(upstream))
                    except StopAsyncIteration:
                        break
                    if kind == "usage":
                        attempt.usage = payload if isinstance(payload, dict) else None
                    elif kind == "finish_reason":
                        finish_reason = payload if isinstance(payload, int) else str(payload)
                    elif kind == "response_id":
                        response_id = str(payload)
                    elif kind != "thought_signature" or attempt.published:
                        # No continuation timer spans delivery/backpressure.
                        accepted = await deliver(GenerationEvent(
                            kind, payload, self.request.model, response_id,
                        ))
                        attempt.published |= accepted
            return GenerationCompletion(
                self.request.model, attempt.usage, finish_reason, response_id,
            )

        return await self._execute(incremental)
