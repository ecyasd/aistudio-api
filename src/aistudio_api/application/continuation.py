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
from itertools import chain
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
PART_OVERHEAD_BYTES = 128
CONTENT_OVERHEAD_BYTES = 64
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
        (part.text is None or isinstance(part.text, str))
        and type(part.thought) is bool
        and (part.thought_signature is None or isinstance(part.thought_signature, str))
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
    if normalized.capture_images:
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
    remaining = MAX_HISTORY_BYTES
    contents = chain(
        normalized.contents,
        (normalized.system_instruction,)
        if normalized.system_instruction is not None
        else (),
    )
    for content in contents:
        remaining -= CONTENT_OVERHEAD_BYTES + len(content.role.encode("utf-8"))
        if remaining <= 0:
            return 0
        for part in content.parts:
            if not _plain_input_part(part):
                return 0
            # The character check avoids allocating an encoded copy of a huge
            # already-rejected string. Object overhead is bounded as well.
            cost = _part_cost(part.text, part.thought_signature, remaining)
            if cost > remaining:
                return 0
            remaining -= cost
            if remaining <= 0:
                return 0
    return remaining


def _plain_part(part: ContentPart) -> bool:
    return (
        (part.text is None or isinstance(part.text, str))
        and type(part.thought) is bool
        and isinstance(part.thought_signature, str)
        and part.image is None
        and part.function_call is None
        and part.function_response is None
    )


def _part_cost(text: str | None, signature: str | None, remaining: int) -> int:
    cost = PART_OVERHEAD_BYTES
    for value in (text, signature):
        if value is not None:
            if len(value) > remaining - cost:
                return remaining + 1
            cost += len(value.encode("utf-8"))
    return cost


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


class _History:
    """Bound retained Parts and append each signed response as its own turn."""

    def __init__(self, normalized: NormalizedGeminiRequest, remaining: int):
        self.normalized = normalized
        self.remaining = remaining
        self.output_remaining = MAX_CONTINUATION_BYTES
        self.contents: list[AistudioContent] | None = None
        self.safe = True

    def consume(self, part: ContentPart) -> bool:
        if not self.safe or not _plain_part(part):
            self.safe = False
            return False
        cost = _part_cost(
            part.text,
            part.thought_signature,
            min(self.remaining, self.output_remaining),
        )
        if cost > min(self.remaining, self.output_remaining):
            self.safe = False
            return False
        self.remaining -= cost
        self.output_remaining -= cost
        return True

    def next_contents(self, parts: list[ContentPart]) -> list[AistudioContent] | None:
        turn_cost = (
            2 * CONTENT_OVERHEAD_BYTES
            + PART_OVERHEAD_BYTES
            + len(CONTINUE_PROMPT.encode("utf-8"))
            + len("modeluser")
        )
        if not self.safe or turn_cost > self.remaining:
            self.safe = False
            return None
        self.remaining -= turn_cost
        # Keep the actual round boundaries: signatures must never migrate into
        # an accumulated model turn containing content from another response.
        self.contents = [
            *deepcopy(
                self.normalized.contents if self.contents is None else self.contents
            ),
            AistudioContent(
                role="model",
                parts=[
                    AistudioPart(
                        text=part.text,
                        thought=part.thought,
                        thought_signature=part.thought_signature or None,
                    )
                    for part in parts
                ],
            ),
            AistudioContent(role="user", parts=[AistudioPart(text=CONTINUE_PROMPT)]),
        ]
        return self.contents


class _RoundParts:
    """Accept complete native Parts or unsigned legacy body events, never both."""

    def __init__(self, history: _History):
        self.history = history
        self.parts: list[ContentPart] = []
        self.mode: str | None = None
        self.has_visible_text = False

    def disable(self) -> None:
        self.history.safe = False
        self.parts.clear()

    def append(self, part: ContentPart, *, mode: str = "parts") -> None:
        if self.mode is not None and self.mode != mode:
            self.disable()
            return
        self.mode = mode
        if not self.history.consume(part):
            self.disable()
            return
        self.parts.append(replace(part))
        self.has_visible_text = self.has_visible_text or bool(
            part.text and not part.thought
        )

    def candidate(self, candidate: Candidate) -> None:
        if candidate.continuation_unsafe or any(
            (
                candidate.images,
                candidate.reasoning_images,
                candidate.function_calls,
                candidate.function_responses,
                candidate.sources,
                candidate.code_output,
            )
        ):
            self.disable()
            return
        if candidate.parts:
            for part in candidate.parts:
                self.append(part)
                if not self.history.safe:
                    return
            visible = "".join(
                part.text or "" for part in self.parts if not part.thought
            )
            thinking = "".join(part.text or "" for part in self.parts if part.thought)
            last_signature = next(
                (
                    part.thought_signature
                    for part in reversed(self.parts)
                    if part.thought_signature
                ),
                "",
            )
            if (
                visible != candidate.text
                or thinking != candidate.thinking
                or (
                    candidate.thought_signature
                    and candidate.thought_signature != last_signature
                )
            ):
                self.disable()
        elif candidate.thinking or candidate.thought_signature:
            # A flattened summary cannot identify which original Part owned a
            # signature, nor its order relative to thinking and visible text.
            self.disable()
        else:
            self.append(ContentPart(text=candidate.text), mode="body")


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
        thought_signature=after.thought_signature or before.thought_signature,
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
    history = _History(normalized, budget)
    try:
        async with asyncio.timeout(settings.timeout_stream):
            output = await client.generate_content(**_arguments(normalized))
            current = output
            for _ in range(MAX_EXTRA_ROUNDS):
                if len(current.candidates) != 1:
                    break
                candidate = current.candidates[0]
                retained = _RoundParts(history)
                retained.candidate(candidate)
                if (
                    type(candidate.finish_reason) is not int
                    or candidate.finish_reason != MAX_TOKENS_FINISH_REASON
                    or not history.safe
                    or not retained.has_visible_text
                ):
                    break
                contents = history.next_contents(retained.parts)
                if contents is None:
                    break
                current = await client.generate_content(
                    **_arguments(normalized, contents)
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
                if event[0] != "continuation_unsafe":
                    yield event
        return

    history = _History(normalized, budget)
    contents: list[AistudioContent] | None = None
    usage: dict[str, object] = {}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.timeout_stream
    try:
        for round_index in range(MAX_EXTRA_ROUNDS + 1):
            finish: int | None = None
            completed = False
            round_usage: dict[str, object] = {}
            retained = _RoundParts(history)
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
                    elif event_type == "continuation_unsafe":
                        retained.disable()
                    else:
                        if event_type == "parts" and isinstance(payload, list):
                            for part in payload:
                                if isinstance(part, ContentPart):
                                    retained.append(part)
                                else:
                                    retained.disable()
                        elif event_type == "body" and isinstance(payload, str):
                            retained.append(ContentPart(text=payload), mode="body")
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
                and history.safe
                and retained.has_visible_text
            ):
                contents = history.next_contents(retained.parts)
                if contents is not None:
                    continue
            yield "finish_reason", finish
            yield "usage", usage or None
            yield "done", None
            return
    except TimeoutError as exc:
        raise RequestError(
            504, "Automatic continuation exceeded the request time budget"
        ) from exc


__all__ = ["ContinuationError", "generate_content", "stream_generate_content"]
