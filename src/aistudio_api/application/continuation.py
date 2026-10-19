"""Opt-in, bounded continuation of completed, token-limited text responses.

The caller owns account admission and the browser-context lease for the whole
operation. Each upstream round retains the original maxOutputTokens setting;
this module never retries an error or changes accounts.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import aclosing
from copy import deepcopy
from dataclasses import replace
from io import StringIO
from typing import TypedDict

from aistudio_api.application.chat_service import NormalizedGeminiRequest
from aistudio_api.config import settings
from aistudio_api.domain.errors import RequestError
from aistudio_api.domain.models import Candidate, ContentPart, ModelOutput
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.gateway.model_defaults import resolve_model_defaults
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart

MAX_EXTRA_ROUNDS = 2
MAX_CONTINUATION_BYTES = 512 * 1024
MAX_HISTORY_BYTES = 1024 * 1024
MAX_TOKENS_FINISH_REASON = 2
CONTINUE_PROMPT = (
    "Continue the preceding response from the exact point where it stopped. "
    "Output only the continuation, without repeating the preceding text."
)


class ContinuationError(Exception):
    """A later round failed; the caller must not replay the original request."""

    def __init__(self, original_error: Exception):
        self.original_error = original_error
        super().__init__("Automatic continuation failed after an upstream response")


class _Arguments(TypedDict):
    model: str
    capture_prompt: str
    capture_images: list[str | tuple[str, str]] | None
    contents: list[AistudioContent]
    system_instruction_content: AistudioContent | None
    tools: list[list[object]] | None
    tool_config: list[object] | None
    safety_settings: list[list[object]] | None
    temperature: float | None
    top_p: float | None
    top_k: int | None
    max_tokens: int | None
    generation_config_overrides: dict[str, object] | None
    sanitize_plain_text: bool


def _arguments(
    normalized: NormalizedGeminiRequest,
    contents: list[AistudioContent] | None = None,
) -> _Arguments:
    return {
        "model": normalized.model,
        "capture_prompt": normalized.capture_prompt,
        "capture_images": normalized.capture_images,
        "contents": normalized.contents if contents is None else contents,
        "system_instruction_content": normalized.system_instruction,
        "tools": normalized.tools,
        "tool_config": normalized.tool_config,
        "safety_settings": normalized.safety_settings,
        "temperature": normalized.temperature,
        "top_p": normalized.top_p,
        "top_k": normalized.top_k,
        "max_tokens": normalized.max_tokens,
        "generation_config_overrides": normalized.generation_config_overrides,
        "sanitize_plain_text": False,
    }


def _plain_input_part(part: AistudioPart) -> bool:
    return (
        isinstance(part.text, str)
        and not part.thought
        and not part.thought_signature
        and all(
            value is None
            for value in (
                part.inline_data,
                part.file_id,
                part.function_call,
                part.function_response,
                part.executable_code,
                part.code_execution_result,
            )
        )
    )


def _history_budget(normalized: NormalizedGeminiRequest) -> int:
    """Return remaining history bytes, or zero when continuation is unsafe."""
    if normalized.tools or normalized.tool_config or normalized.capture_images:
        return 0
    if resolve_model_defaults(normalized.model).is_image_model:
        return 0
    overrides = normalized.generation_config_overrides or {}
    if overrides.get("response_mime_type") not in (None, "", "text/plain"):
        return 0
    if any(
        overrides.get(key) is not None
        for key in ("response_schema", "image_output_mode", "output_resolution")
    ):
        return 0
    remaining = MAX_HISTORY_BYTES - len(CONTINUE_PROMPT.encode("utf-8")) - 128
    contents = normalized.contents
    if normalized.system_instruction is not None:
        contents = [*contents, normalized.system_instruction]
    for content in contents:
        remaining -= 32
        for part in content.parts:
            if not _plain_input_part(part):
                return 0
            # The character check avoids allocating an encoded copy of a huge
            # already-rejected string. Object overhead is bounded as well.
            text = part.text or ""
            if len(text) > remaining:
                return 0
            remaining -= len(text.encode("utf-8")) + 32
            if remaining <= 0:
                return 0
    return min(remaining, MAX_CONTINUATION_BYTES)


def _plain_part(part: ContentPart) -> bool:
    return (
        isinstance(part.text, str)
        and not part.thought
        and not part.thought_signature
        and part.image is None
        and part.function_call is None
        and part.function_response is None
    )


def _plain_candidate(candidate: Candidate) -> bool:
    return (
        bool(candidate.text)
        and not any(
            (
                candidate.thinking,
                candidate.images,
                candidate.reasoning_images,
                candidate.function_calls,
                candidate.function_responses,
                candidate.thought_signature,
                candidate.sources,
                candidate.code_output,
            )
        )
        and all(_plain_part(part) for part in candidate.parts)
        and (
            not candidate.parts
            or "".join(part.text or "" for part in candidate.parts) == candidate.text
        )
    )


def _merge_usage(
    previous: dict[str, object], current: dict[str, object]
) -> dict[str, object]:
    """Sum named integer counters, including nested reasoning/visible details."""
    merged = deepcopy(previous)
    for key, value in current.items():
        old = merged.get(key)
        if type(value) is int:
            merged[key] = value + (old if type(old) is int else 0)
        elif isinstance(value, dict):
            merged[key] = _merge_usage(old if isinstance(old, dict) else {}, value)
        elif value is not None or key not in merged:
            # Opaque wire breakdown arrays are metadata, not named counters.
            merged[key] = deepcopy(value)
    return merged


def _next_contents(
    normalized: NormalizedGeminiRequest, text: str
) -> list[AistudioContent]:
    return [
        *deepcopy(normalized.contents),
        AistudioContent(role="model", parts=[AistudioPart(text=text)]),
        AistudioContent(role="user", parts=[AistudioPart(text=CONTINUE_PROMPT)]),
    ]


def _candidate_parts(candidate: Candidate) -> list[ContentPart]:
    if candidate.parts:
        return candidate.parts
    parts = []
    if candidate.thinking:
        parts.append(ContentPart(text=candidate.thinking, thought=True))
    if candidate.text:
        parts.append(ContentPart(text=candidate.text))
    parts.extend(ContentPart(image=image) for image in candidate.images)
    parts.extend(
        ContentPart(image=image, thought=True) for image in candidate.reasoning_images
    )
    parts.extend(ContentPart(function_call=call) for call in candidate.function_calls)
    parts.extend(
        ContentPart(function_response=response)
        for response in candidate.function_responses
    )
    if candidate.thought_signature:
        parts.append(ContentPart(thought_signature=candidate.thought_signature))
    return parts


def _merge_outputs(previous: ModelOutput, current: ModelOutput) -> ModelOutput:
    if len(current.candidates) != 1:
        raise RequestError(502, "Continuation returned an invalid candidate count")
    before, after = previous.candidates[0], current.candidates[0]
    candidate = replace(
        after,
        text=before.text + after.text,
        thinking=before.thinking + after.thinking,
        images=[*before.images, *after.images],
        reasoning_images=[*before.reasoning_images, *after.reasoning_images],
        function_calls=[*before.function_calls, *after.function_calls],
        function_responses=[*before.function_responses, *after.function_responses],
        sources=[*before.sources, *after.sources],
        code_output=before.code_output + after.code_output,
        parts=[*_candidate_parts(before), *_candidate_parts(after)],
    )
    return replace(
        current,
        candidates=[candidate],
        usage=_merge_usage(previous.usage, current.usage),
        response_id=current.response_id or previous.response_id,
        raw_response="",
    )


async def generate_content(
    client: AIStudioClient,
    normalized: NormalizedGeminiRequest,
    *,
    auto_continue: bool = False,
) -> ModelOutput:
    budget = _history_budget(normalized) if auto_continue else 0
    if not budget:
        return await client.generate_content(**_arguments(normalized))

    output: ModelOutput | None = None
    try:
        async with asyncio.timeout(settings.timeout_stream):
            output = await client.generate_content(**_arguments(normalized))
            for _ in range(MAX_EXTRA_ROUNDS):
                if len(output.candidates) != 1:
                    break
                candidate = output.candidates[0]
                if (
                    type(candidate.finish_reason) is not int
                    or candidate.finish_reason != MAX_TOKENS_FINISH_REASON
                    or not _plain_candidate(candidate)
                    or len(candidate.text) > budget
                    or len(candidate.text.encode("utf-8")) > budget
                ):
                    break
                current = await client.generate_content(
                    **_arguments(normalized, _next_contents(normalized, candidate.text))
                )
                output = _merge_outputs(output, current)
            return output
    except Exception as exc:
        failure = (
            RequestError(504, "Automatic continuation exceeded the request time budget")
            if isinstance(exc, TimeoutError)
            else exc
        )
        if output is not None:
            raise ContinuationError(failure) from exc
        if failure is not exc:
            raise failure from exc
        raise


class _TextBuffer:
    """Retain only bounded text needed for a potential continuation request."""

    def __init__(self, budget: int):
        self.remaining = budget
        self.buffer = StringIO()
        self.safe = True

    def disable(self) -> None:
        self.safe = False
        self.buffer.close()

    def append(self, text: str) -> None:
        if not self.safe:
            return
        if len(text) > self.remaining:
            self.disable()
            return
        self.remaining -= len(text.encode("utf-8"))
        if self.remaining < 0:
            self.disable()
            return
        self.buffer.write(text)


async def stream_generate_content(
    client: AIStudioClient,
    normalized: NormalizedGeminiRequest,
    *,
    auto_continue: bool = False,
    force_refresh_capture: bool = False,
) -> AsyncGenerator[tuple[str, object | None], None]:
    budget = _history_budget(normalized) if auto_continue else 0
    if not budget:
        async with aclosing(
            client.stream_generate_content(
                **_arguments(normalized), force_refresh_capture=force_refresh_capture
            )
        ) as upstream:
            async for event in upstream:
                yield event
        return

    retained = _TextBuffer(budget)
    contents: list[AistudioContent] | None = None
    usage: dict[str, object] = {}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.timeout_stream
    try:
        for round_index in range(MAX_EXTRA_ROUNDS + 1):
            finish: int | None = None
            completed = False
            round_usage: dict[str, object] = {}
            round_has_text = False
            async with aclosing(
                client.stream_generate_content(
                    **_arguments(normalized, contents),
                    force_refresh_capture=force_refresh_capture and round_index == 0,
                )
            ) as upstream:
                while True:
                    # A timeout spanning ``yield`` would cancel arbitrary caller
                    # work while this generator is suspended (for example SSE
                    # backpressure). Enforce the same absolute deadline only
                    # while awaiting the next upstream event.
                    if loop.time() >= deadline:
                        raise TimeoutError
                    try:
                        async with asyncio.timeout_at(deadline):
                            event_type, payload = await anext(upstream)
                    except StopAsyncIteration:
                        break
                    if event_type == "finish_reason":
                        finish = payload if type(payload) is int else None
                    elif event_type == "usage":
                        round_usage = payload if isinstance(payload, dict) else {}
                    elif event_type == "done":
                        completed = True
                    else:
                        if event_type == "parts" and isinstance(payload, list):
                            for part in payload:
                                if isinstance(part, ContentPart) and _plain_part(part):
                                    text = part.text or ""
                                    retained.append(text)
                                    round_has_text = round_has_text or bool(text)
                                else:
                                    retained.disable()
                        elif event_type == "body" and isinstance(payload, str):
                            retained.append(payload)
                            round_has_text = round_has_text or bool(payload)
                        elif event_type != "response_id":
                            retained.disable()
                        yield event_type, payload
            if not completed or finish is None or finish <= 0:
                raise RequestError(
                    502, "Upstream response ended without a completion marker"
                )
            usage = _merge_usage(usage, round_usage)
            if (
                finish == MAX_TOKENS_FINISH_REASON
                and round_index < MAX_EXTRA_ROUNDS
                and retained.safe
                and round_has_text
            ):
                contents = _next_contents(normalized, retained.buffer.getvalue())
                continue
            yield "finish_reason", finish
            yield "usage", usage or None
            yield "done", None
            return
    except TimeoutError as exc:
        raise RequestError(
            504, "Automatic continuation exceeded the request time budget"
        ) from exc
    finally:
        retained.buffer.close()


__all__ = ["ContinuationError", "generate_content", "stream_generate_content"]
