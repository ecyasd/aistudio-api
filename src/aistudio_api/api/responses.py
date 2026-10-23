"""Gemini-compatible HTTP/SSE response helpers."""

from __future__ import annotations

import base64

from aistudio_api.api.response_models import (
    GeminiCodeExecutionResultPayload,
    GeminiExecutableCodePayload,
    GeminiFunctionCallPayload,
    GeminiFunctionResponsePayload,
    GeminiInlineDataResponse,
    GeminiPartResponse,
    GeminiUsageMetadata,
)
from aistudio_api.domain.models import ContentPart, GeneratedImage


def _coerce_usage_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped and (
            stripped.isdigit() or (stripped[0] in "+-" and stripped[1:].isdigit())
        ):
            return int(stripped)
    return 0


def to_gemini_usage_metadata(
    usage: dict[str, object] | None = None,
) -> GeminiUsageMetadata:
    usage = usage or {}
    completion_details = usage.get("completion_tokens_details")
    if not isinstance(completion_details, dict):
        completion_details = {}
    reasoning_tokens = _coerce_usage_int(completion_details.get("reasoning_tokens"))
    visible_tokens = _coerce_usage_int(completion_details.get("visible_tokens"))
    completion_tokens = _coerce_usage_int(usage.get("completion_tokens"))
    if completion_tokens == 0:
        completion_tokens = visible_tokens + reasoning_tokens
    candidates_tokens = (
        visible_tokens
        if "visible_tokens" in completion_details
        else max(0, completion_tokens - reasoning_tokens)
    )
    prompt_tokens = _coerce_usage_int(usage.get("prompt_tokens"))
    total_tokens = _coerce_usage_int(usage.get("total_tokens"))
    if total_tokens == 0 and (prompt_tokens or candidates_tokens or reasoning_tokens):
        total_tokens = prompt_tokens + candidates_tokens + reasoning_tokens
    return GeminiUsageMetadata(
        promptTokenCount=prompt_tokens,
        candidatesTokenCount=candidates_tokens,
        thoughtsTokenCount=reasoning_tokens,
        totalTokenCount=total_tokens,
    )


def to_gemini_parts(
    content: str,
    function_calls: list[dict[str, object]] | None = None,
    function_responses: list[dict[str, object]] | None = None,
    thinking: str = "",
    images: list[GeneratedImage] | None = None,
    reasoning_images: list[GeneratedImage] | None = None,
    source_parts: list[ContentPart] | None = None,
) -> list[GeminiPartResponse]:
    if source_parts is not None:
        # Native thought markers remain in internal history, but without any
        # payload they must not start another public reasoning phase. An
        # explicitly empty text or a signature still belongs to its own Part.
        return [
            _to_gemini_part(part)
            for part in source_parts
            if part.text is not None
            or part.image is not None
            or part.function_call is not None
            or part.function_response is not None
            or part.executable_code is not None
            or part.code_execution_result is not None
            or part.thought_signature
        ]
    parts: list[GeminiPartResponse] = []
    if thinking:
        parts.append(GeminiPartResponse(text=thinking, thought=True))
    for image in reasoning_images or []:
        parts.append(
            GeminiPartResponse(
                thought=True,
                thoughtSignature=image.thought_signature or None,
                inlineData=GeminiInlineDataResponse(
                    mimeType=image.mime,
                    data=base64.b64encode(image.data).decode("ascii"),
                ),
            )
        )
    if content:
        parts.append(GeminiPartResponse(text=content))
    for image in images or []:
        parts.append(
            GeminiPartResponse(
                thoughtSignature=image.thought_signature or None,
                inlineData=GeminiInlineDataResponse(
                    mimeType=image.mime,
                    data=base64.b64encode(image.data).decode("ascii"),
                ),
            )
        )
    for function_call in function_calls or []:
        fc_name = str(function_call.get("name") or "unknown")
        payload = GeminiFunctionCallPayload(name=fc_name)
        if "args" in function_call:
            payload.args = function_call["args"]
        elif "arguments" in function_call:
            payload.args = function_call["arguments"]
        else:
            raw_val = function_call.get("raw")
            if isinstance(raw_val, list) and len(raw_val) > 1:
                payload.args = raw_val[1]
        if payload.args is None:
            payload.args = {}
        fc_id = function_call.get("call_id") or function_call.get("id")
        if fc_id:
            payload.id = str(fc_id)
        sig = str(function_call.get("thought_signature") or "") or None
        parts.append(GeminiPartResponse(functionCall=payload, thoughtSignature=sig))
    for function_response in function_responses or []:
        fr_name = str(function_response.get("name") or "unknown")
        resp_payload = GeminiFunctionResponsePayload(name=fr_name)
        if "args" in function_response:
            resp_payload.response = function_response["args"]
        elif "arguments" in function_response:
            resp_payload.response = function_response["arguments"]
        else:
            raw_val = function_response.get("raw")
            if isinstance(raw_val, list) and len(raw_val) > 1:
                resp_payload.response = raw_val[1]
        fr_id = function_response.get("call_id") or function_response.get("id")
        if fr_id:
            resp_payload.id = str(fr_id)
        parts.append(GeminiPartResponse(functionResponse=resp_payload))
    if not parts:
        parts.append(GeminiPartResponse(text=""))
    return parts


def _to_gemini_part(part: ContentPart) -> GeminiPartResponse:
    """Convert one Part without merging it with or borrowing from a neighbor."""
    result = GeminiPartResponse(
        text=part.text,
        thought=True if part.thought else None,
        thoughtSignature=part.thought_signature or None,
    )
    if part.image is not None:
        result.inlineData = GeminiInlineDataResponse(
            mimeType=part.image.mime,
            data=base64.b64encode(part.image.data).decode("ascii"),
        )
    if part.function_call is not None:
        result.functionCall = to_gemini_parts("", function_calls=[part.function_call])[
            0
        ].functionCall
    if part.function_response is not None:
        result.functionResponse = to_gemini_parts(
            "", function_responses=[part.function_response]
        )[0].functionResponse
    if part.executable_code is not None:
        result.executableCode = GeminiExecutableCodePayload.model_validate(
            part.executable_code
        )
    if part.code_execution_result is not None:
        result.codeExecutionResult = GeminiCodeExecutionResultPayload.model_validate(
            part.code_execution_result
        )
    return result
