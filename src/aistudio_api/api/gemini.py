"""Gemini HTTP responses and bounded SSE delivery."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import cast

from anyio import CancelScope
from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse
from starlette.types import Message, Receive, Scope, Send

from aistudio_api.api.gemini_errors import project_gemini_error
from aistudio_api.api.response_models import (
    GeminiCandidateResponse,
    GeminiContentResponse,
    GeminiGenerateContentResponse,
)
from aistudio_api.api.responses import to_gemini_parts, to_gemini_usage_metadata
from aistudio_api.api.schemas import GeminiGenerateContentRequest
from aistudio_api.api.sse_delivery import (
    FIRST_SSE_SEND_STATE_KEY,
    SSE_DELIVERY_STATE_KEY,
    SSE_SEND_TIMEOUT_SECONDS,
    SSEDelivery,
)
from aistudio_api.application.account_orchestrator import validate_account_availability
from aistudio_api.application.chat_service import (
    NormalizedGeminiRequest,
    normalize_gemini_request,
)
from aistudio_api.application.generation import (
    GenerationDeadlineExceeded,
    GenerationEvent,
    GenerationRun,
)
from aistudio_api.application.generation_errors import GenerationFailure
from aistudio_api.application.model_selection import parse_model_selection
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.utils.error_records import (
    has_final_failure,
    record_failure,
)
from aistudio_api.infrastructure.utils.logger import get_logger
from aistudio_api.infrastructure.utils.response_diagnostics import (
    configure_generation,
    note_coalesced_events,
    observe_queue_bytes,
)

logger = get_logger("api.gemini")
SSE_HEARTBEAT_INTERVAL_SECONDS = 15.0
SSE_BUFFER_MAX_BYTES = 32 * 1024
SSE_BUFFER_MAX_EVENTS = 8
WIRE_TO_GEMINI_FINISH_REASON: dict[int, str] = {
    0: "FINISH_REASON_UNSPECIFIED",
    1: "STOP",
    2: "MAX_TOKENS",
    3: "SAFETY",
    4: "RECITATION",
    5: "LANGUAGE",
    6: "OTHER",
    7: "BLOCKLIST",
    8: "PROHIBITED_CONTENT",
    9: "SPII",
    10: "MALFORMED_FUNCTION_CALL",
    11: "IMAGE_SAFETY",
    12: "UNEXPECTED_TOOL_CALL",
    13: "TOO_MANY_TOOL_CALLS",
    14: "IMAGE_PROHIBITED_CONTENT",
    15: "NO_IMAGE",
    16: "IMAGE_RECITATION",
    17: "IMAGE_OTHER",
}


def to_gemini_finish_reason(wire_code: int | None) -> str:
    if wire_code is None:
        return "FINISH_REASON_UNSPECIFIED"
    return WIRE_TO_GEMINI_FINISH_REASON.get(wire_code, "FINISH_REASON_UNSPECIFIED")


async def handle_gemini_generate_content(
    model_path: str,
    req: GeminiGenerateContentRequest,
    client: AIStudioClient,
    *,
    stream: bool,
):
    # Resolve before account admission, cooldowns, capture caches or statistics.
    # A virtual selector must never create an independent upstream quota bucket.
    try:
        try:
            selection = parse_model_selection(model_path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        model_path = selection.base_model
        if selection.auto_continue:
            # Choosing the variant is itself the opt-in, including clients that send
            # an explicit default false. Never mutate the caller's request object.
            req = req.model_copy(update={"autoContinue": True})
        configure_generation(model=model_path, auto_continue=req.autoContinue)
        if stream:
            normalized = _preflight_gemini_stream(req, model_path)
            return _build_gemini_streaming_response(
                client=client, req=req, model_path=model_path, prepared_request=normalized
            )

        return await _generate_non_streaming(model_path, req, client)
    except GenerationFailure as exc:
        projected = project_gemini_error(exc)
        raise HTTPException(
            projected.status_code,
            detail={"message": projected.message, "type": exc.description.reason.value},
            headers=projected.headers,
        ) from exc


async def _generate_non_streaming(
    model_path: str, req: GeminiGenerateContentRequest, client: AIStudioClient,
):
    try:
        normalized = normalize_gemini_request(req, model_path)
    except (ValueError, RequestValidationError) as exc:
        raise HTTPException(400, detail={"message": str(exc)}) from exc
    try:
        output = await GenerationRun(client, normalized, auto_continue=req.autoContinue).generate()
    except GenerationDeadlineExceeded as exc:
        raise HTTPException(504, detail={
            "message": "Automatic continuation exceeded the request time budget",
            "type": "upstream_timeout",
        }) from exc
    return GeminiGenerateContentResponse(
        candidates=[
            GeminiCandidateResponse(
                content=GeminiContentResponse(
                    parts=to_gemini_parts(
                        output.text,
                        function_calls=output.function_calls,
                        function_responses=output.function_responses,
                        thinking=output.thinking,
                        images=output.images,
                        reasoning_images=output.reasoning_images,
                        source_parts=(output.candidates[0].parts or None)
                        if output.candidates else None,
                    ),
                ),
                finishReason=to_gemini_finish_reason(
                    output.candidates[0].finish_reason
                    if output.candidates
                    else None
                ),
                index=0,
            )
        ],
        usageMetadata=to_gemini_usage_metadata(output.usage),
        modelVersion=normalized.model,
        responseId=output.response_id or None,
    )


def format_sse_event(
    event_type: str,
    text: object,
    *,
    model_version: str | None = None,
    response_id: str | None = None,
    thought_signature: str | None = None,
) -> str | None:
    """Format a single Gemini streaming event into SSE chunk string."""
    if not text:
        return None

    parts: list[dict[str, object]] = []
    if event_type == "parts":
        parts = [
            part.model_dump(mode="json", exclude_none=True)
            for part in to_gemini_parts("", source_parts=text if isinstance(text, list) else [])
        ]
        if not parts:
            return None
    elif event_type == "thought_signature":
        parts.append({"text": "", "thoughtSignature": str(text)})
    elif event_type == "body":
        p: dict[str, object] = {"text": str(text)}
        if thought_signature:
            p["thoughtSignature"] = thought_signature
        parts.append(p)
    elif event_type == "thinking":
        p = {"text": str(text), "thought": True}
        if thought_signature:
            p["thoughtSignature"] = thought_signature
        parts.append(p)
    elif event_type == "tool_calls":
        fc_list = text if isinstance(text, list) else []
        parts = [
            part.model_dump(mode="json", exclude_none=True)
            for part in to_gemini_parts("", function_calls=fc_list)
        ]
        if thought_signature and parts:
            parts[0].setdefault("thoughtSignature", thought_signature)
    elif event_type == "images":
        img_list = text if isinstance(text, list) else []
        parts = [
            part.model_dump(mode="json", exclude_none=True)
            for part in to_gemini_parts("", images=img_list)
        ]
        if thought_signature and parts:
            parts[0].setdefault("thoughtSignature", thought_signature)
    elif event_type == "reasoning_images":
        r_img_list = text if isinstance(text, list) else []
        parts = [
            part.model_dump(mode="json", exclude_none=True)
            for part in to_gemini_parts("", reasoning_images=r_img_list)
        ]
        if thought_signature and parts:
            parts[0].setdefault("thoughtSignature", thought_signature)
    else:
        return None

    payload: dict[str, object] = {
        "candidates": [{"content": {"role": "model", "parts": parts}, "index": 0}]
    }
    if model_version:
        payload["modelVersion"] = model_version
    if response_id:
        payload["responseId"] = response_id
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def format_sse_usage(
    final_usage: dict[str, object] | None,
    *,
    finish_reason: str = "STOP",
    model_version: str | None = None,
    response_id: str | None = None,
) -> str:
    """Format final completion usage metadata into SSE chunk string."""
    effective_usage = final_usage or {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    usage_dict = to_gemini_usage_metadata(effective_usage).model_dump(
        mode="json", exclude_none=True
    )
    payload: dict[str, object] = {
        "candidates": [
            {
                "content": {"role": "model", "parts": []},
                "finishReason": finish_reason,
                "index": 0,
            }
        ],
        "usageMetadata": usage_dict,
    }
    if model_version:
        payload["modelVersion"] = model_version
    if response_id:
        payload["responseId"] = response_id
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def format_sse_error(exc: Exception) -> str:
    """Format exception into standard Gemini SSE error chunk."""
    projected = project_gemini_error(exc)
    code, msg, status_str = projected.status_code, projected.message, projected.status
    error_type = projected.error_type
    return (
        "data: "
        + json.dumps(
            {
                "error": {
                    "code": code,
                    "message": msg,
                    "status": status_str,
                    **({"type": error_type} if error_type else {}),
                }
            },
            ensure_ascii=False,
        )
        + "\n\n"
    )


def _preflight_gemini_stream(
    req: GeminiGenerateContentRequest, model_path: str
) -> NormalizedGeminiRequest:
    """Reject known local failures before committing the streaming HTTP status.

    This only reads account metadata and normalizes the request. Browser startup,
    account activation and upstream authentication remain inside the stream.
    """
    try:
        normalized = normalize_gemini_request(req, model_path)
    except (ValueError, RequestValidationError) as exc:
        raise HTTPException(400, detail={"message": str(exc)}) from exc

    validate_account_availability(normalized.model)
    return normalized


@dataclass(frozen=True)
class _QueuedSSEItem:
    value: str | Exception | None
    size: int = 0
    coalesceable: bool = False


class _SSEBuffer:
    """Single-producer FIFO, bounded by UTF-8 bytes and event count.

    One indivisible oversized event may occupy the empty buffer on its own;
    splitting a signed/multimodal Part or waiting for space forever is invalid.
    The caller still owns the yielded write and at most one blocked put.
    """

    def __init__(
        self,
        *,
        max_bytes: int = SSE_BUFFER_MAX_BYTES,
        max_events: int = SSE_BUFFER_MAX_EVENTS,
        coalesce: bool = False,
    ) -> None:
        self.max_bytes = max(1, int(max_bytes))
        self.max_events = max(1, int(max_events))
        self.coalesce = coalesce
        self.buffered_bytes = 0
        self._items: deque[_QueuedSSEItem] = deque()
        self._not_empty = asyncio.Event()
        self._not_full = asyncio.Event()
        self._not_full.set()
        self._first_data_sent = False
        self._backlog_enabled = False

    def allow_backlog(self) -> None:
        """Expand only after the first yield resumes (the first send finished)."""
        self._backlog_enabled = True
        self._not_full.set()

    def _can_put(self, size: int) -> bool:
        if not self._items:
            return True
        return (
            len(self._items) < (self.max_events if self._backlog_enabled else 1)
            and self.buffered_bytes + size <= self.max_bytes
        )

    async def put(self, item: str | Exception | None) -> None:
        if isinstance(item, str):
            size = len(item.encode("utf-8"))
            while not self._can_put(size):
                self._not_full.clear()
                await self._not_full.wait()
            coalesceable = (
                item.startswith("data: ")
                and not item.startswith('data: {"error":')
            )
            self._items.append(_QueuedSSEItem(item, size, coalesceable))
            self.buffered_bytes += size
            observe_queue_bytes(self.buffered_bytes)
            if not self._can_put(1):
                self._not_full.clear()
        else:
            self._items.append(_QueuedSSEItem(item, 0, False))
        self._not_empty.set()

    async def get(self) -> str | Exception | None:
        while not self._items:
            self._not_empty.clear()
            await self._not_empty.wait()
        first = self._items.popleft()
        if not isinstance(first.value, str):
            if not self._items:
                self._not_empty.clear()
            self._not_full.set()
            return first.value
        freed = first.size
        value = first.value
        if self.coalesce and self._first_data_sent and first.coalesceable:
            batch = [value]
            while self._items and self._items[0].coalesceable:
                next_item = self._items.popleft()
                assert isinstance(next_item.value, str)
                batch.append(next_item.value)
                freed += next_item.size
            if len(batch) > 1:
                note_coalesced_events(len(batch) - 1)
                value = "".join(batch)
        if first.coalesceable:
            self._first_data_sent = True
        self.buffered_bytes = max(0, self.buffered_bytes - freed)
        if not self._items:
            self._not_empty.clear()
        self._not_full.set()
        return value


async def _sse_with_heartbeats(
    source: Callable[[Callable[[str], Awaitable[None]]], Awaitable[None]],
    *,
    interval: float,
    max_buffer_bytes: int = SSE_BUFFER_MAX_BYTES,
    max_buffer_events: int = 1,
    coalesce: bool = False,
    delivery: SSEDelivery | None = None,
) -> AsyncGenerator[str, None]:
    """Run generation and close its resources in one task, with a bounded byte/event buffer."""
    buffer = _SSEBuffer(
        max_bytes=max_buffer_bytes,
        max_events=max_buffer_events,
        coalesce=coalesce,
    )

    async def produce() -> None:
        try:
            await source(buffer.put)
        except Exception as exc:
            await buffer.put(exc)
        else:
            await buffer.put(None)

    producer = asyncio.create_task(produce(), name="gemini-sse-producer")
    pending: asyncio.Task[str | Exception | None] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(buffer.get(), name="gemini-sse-read")
            done, _ = await asyncio.wait(
                {pending, producer},
                timeout=interval,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                # Empty SSE separators flush bytes without dispatching a model
                # event. google-genai parses comment lines as JSON, so a normal
                # ': ping' comment would break otherwise compatible SDK streams.
                yield "\n\n"
                continue
            if pending not in done:
                # A cancelled producer must not leave a live connection sending
                # heartbeats forever. Normal completion already queued its end.
                producer.result()
            item = await pending
            pending = None
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            yield item
            if delivery is None or delivery.first_sent is None or delivery.first_sent.is_set():
                buffer.allow_backlog()
    finally:
        # Cancel the producer even while it is blocked on queue backpressure.
        # Its aclosing block closes all upstream scopes in the same task.
        producer.cancel()
        if pending is not None:
            pending.cancel()
        with CancelScope(shield=True):
            for task in (producer, pending):
                if task is not None:
                    with suppress(asyncio.CancelledError, Exception):
                        await task


class _GeminiStreamingResponse(StreamingResponse):
    def __init__(
        self, content: AsyncGenerator[str, None], *,
        delivery: SSEDelivery | None = None,
        media_type: str | None = None, headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(content, media_type=media_type, headers=headers)
        self._delivery = delivery or SSEDelivery()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        state = scope.setdefault("state", {})
        first_sent = state.get(FIRST_SSE_SEND_STATE_KEY)
        if isinstance(first_sent, asyncio.Event):
            self._delivery.first_sent = first_sent
        self._delivery.send_timeout_seconds = SSE_SEND_TIMEOUT_SECONDS
        state[SSE_DELIVERY_STATE_KEY] = self._delivery
        await super().__call__(scope, receive, send)

    async def stream_response(self, send: Send) -> None:
        async def bounded_send(message: Message) -> None:
            async with asyncio.timeout(SSE_SEND_TIMEOUT_SECONDS):
                await send(message)

        try:
            await super().stream_response(bounded_send)
        finally:
            # A failed ASGI send can stop between iterator yields without closing
            # the iterator. Always cancel its producer and close upstream scopes.
            with CancelScope(shield=True):
                await cast(AsyncGenerator[str, None], self.body_iterator).aclose()


def _build_gemini_streaming_response(
    *, client: AIStudioClient, req: GeminiGenerateContentRequest, model_path: str,
    prepared_request: NormalizedGeminiRequest | None = None,
) -> StreamingResponse:
    async def produce(emit: Callable[[str], Awaitable[None]]) -> None:
        normalized = None
        run: GenerationRun | None = None
        try:
            normalized = prepared_request or normalize_gemini_request(req, model_path)
            run = GenerationRun(client, normalized, auto_continue=req.autoContinue)

            async def deliver(event: GenerationEvent) -> bool:
                chunk = format_sse_event(
                    event.kind, event.payload, model_version=event.model,
                    response_id=event.response_id,
                )
                if not chunk:
                    return False
                await emit(chunk)
                return True

            complete = await run.stream(deliver)
            finish_reason = (
                to_gemini_finish_reason(complete.finish_reason)
                if isinstance(complete.finish_reason, int) or complete.finish_reason is None
                else complete.finish_reason
            )
            await emit(format_sse_usage(
                complete.usage, finish_reason=finish_reason,
                model_version=complete.model, response_id=complete.response_id,
            ))
        except GenerationDeadlineExceeded as exc:
            failure = HTTPException(504, detail={
                "message": "Automatic continuation exceeded the request time budget",
                "type": "upstream_timeout",
            })
            record_failure(exc, source="gemini_stream", operation="stream_generate_content",
                           http_status=504)
            await emit(format_sse_error(failure))
        except Exception as exc:
            if not has_final_failure():
                record_failure(
                    exc, source="gemini_stream",
                    operation="inference_admission" if run is not None and not run.admitted else "stream_generate_content",
                    model=normalized.model if normalized else model_path,
                    account_id=run.identity.account_id if run is not None else None,
                    http_status=project_gemini_error(exc).status_code,
                )
            await emit(format_sse_error(exc))

    delivery = SSEDelivery()
    return _GeminiStreamingResponse(
        _sse_with_heartbeats(
            produce,
            interval=SSE_HEARTBEAT_INTERVAL_SECONDS,
            max_buffer_events=SSE_BUFFER_MAX_EVENTS,
            coalesce=True,
            delivery=delivery,
        ),
        delivery=delivery,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
