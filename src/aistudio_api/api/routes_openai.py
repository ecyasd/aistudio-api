"""SillyTavern Chat Completions adapter over the shared Gemini pipeline."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing
from typing import Literal, cast

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from aistudio_api.api.dependencies import get_client, get_runtime_state
from aistudio_api.api.response_models import GeminiGenerateContentResponse
from aistudio_api.api.routes_models import list_models
from aistudio_api.api.schemas import GeminiGenerateContentRequest
from aistudio_api.api.state import RuntimeState
from aistudio_api.application.api_service_gemini import handle_gemini_generate_content
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.utils.telemetry import current, failed

router = APIRouter(prefix="/v1")


class Message(BaseModel):
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | list[dict[str, object]] | None = None
    tool_calls: list[dict[str, object]] | None = None
    tool_call_id: str | None = None


class ChatRequest(BaseModel):
    model: str = Field(min_length=1, max_length=128)
    messages: list[Message] = Field(min_length=1)
    stream: bool = False
    stream_options: dict[str, bool] | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    top_k: int | None = Field(default=None, ge=1)
    max_tokens: int | None = Field(default=None, ge=1)
    max_completion_tokens: int | None = Field(default=None, ge=1)
    stop: str | list[str] | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    seed: int | None = None
    reasoning_effort: str | None = None
    tools: list[dict[str, object]] | None = None
    tool_choice: str | dict[str, object] | None = None
    response_format: dict[str, object] | None = None
    n: Literal[1] = 1


def invalid(message: str) -> HTTPException:
    return HTTPException(
        400, detail={"message": message, "type": "invalid_request_error"}
    )


def content_parts(
    content: str | list[dict[str, object]] | None,
) -> list[dict[str, object]]:
    if isinstance(content, str):
        return [{"text": content}] if content else []
    parts: list[dict[str, object]] = []
    for part in content or []:
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            parts.append({"text": part["text"]})
        elif part.get("type") == "image_url":
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, dict) else None
            if (
                not isinstance(url, str)
                or not url.startswith("data:image/")
                or ";base64," not in url
            ):
                raise invalid("Images must use a base64 data URL")
            header, data = url.split(",", 1)
            parts.append(
                {"inlineData": {"mimeType": header[5:].split(";")[0], "data": data}}
            )
        else:
            raise invalid("Unsupported message content type")
    return parts


def to_gemini(req: ChatRequest) -> GeminiGenerateContentRequest:
    contents: list[dict[str, object]] = []
    instructions: list[dict[str, object]] = []
    call_names: dict[str, str] = {}
    for index, message in enumerate(req.messages):
        parts = content_parts(message.content)
        if message.role in {"system", "developer"}:
            instructions.extend(parts)
            continue
        if message.role == "tool":
            name = call_names.get(message.tool_call_id or "")
            if not name:
                raise invalid("Tool result must match a preceding tool call")
            parts = [
                {
                    "functionResponse": {
                        "id": message.tool_call_id,
                        "name": name,
                        "response": {"result": message.content},
                    }
                }
            ]
        for call in message.tool_calls or []:
            function = call.get("function")
            if not isinstance(function, dict) or not isinstance(
                function.get("name"), str
            ):
                raise invalid("Invalid function call")
            try:
                args = json.loads(str(function.get("arguments", "{}")))
            except ValueError as exc:
                raise invalid("Function arguments must be JSON") from exc
            call_id = str(call.get("id") or "")
            call_names[call_id] = function["name"]
            parts.append(
                {
                    "functionCall": {
                        "id": call_id,
                        "name": function["name"],
                        "args": args,
                    }
                }
            )
        if parts:
            contents.append(
                {
                    "role": "model" if message.role == "assistant" else "user",
                    "parts": parts,
                }
            )
        elif index == len(req.messages) - 1:
            raise invalid("The final conversation message must not be empty")
    if not contents:
        raise invalid("At least one nonempty conversation message is required")
    config: dict[str, object] = {
        key: value
        for key, value in {
            "temperature": req.temperature,
            "topP": req.top_p,
            "topK": req.top_k,
            "maxOutputTokens": req.max_completion_tokens or req.max_tokens,
            "stopSequences": [req.stop] if isinstance(req.stop, str) else req.stop,
            "presencePenalty": req.presence_penalty,
            "frequencyPenalty": req.frequency_penalty,
            "seed": req.seed,
            "reasoningEffort": req.reasoning_effort,
        }.items()
        if value is not None
    }
    if req.response_format:
        kind = req.response_format.get("type")
        if kind in {"json_object", "json_schema"}:
            config["responseMimeType"] = "application/json"
            schema = req.response_format.get("json_schema")
            if isinstance(schema, dict) and isinstance(schema.get("schema"), dict):
                config["responseSchema"] = schema["schema"]
        elif kind != "text":
            raise invalid("Unsupported response_format")
    payload: dict[str, object] = {"contents": contents, "generationConfig": config}
    if instructions:
        payload["systemInstruction"] = {"parts": instructions}
    if req.tools:
        declarations = []
        for tool in req.tools:
            if tool.get("type") != "function" or not isinstance(
                tool.get("function"), dict
            ):
                raise invalid("Only function tools are supported")
            declarations.append(tool["function"])
        payload["tools"] = [{"functionDeclarations": declarations}]
    if req.tool_choice:
        choice = req.tool_choice
        if isinstance(choice, str):
            if choice not in {"auto", "none", "required"}:
                raise invalid("Invalid tool_choice")
            call_config: dict[str, object] = {
                "mode": {"auto": "AUTO", "none": "NONE", "required": "ANY"}[choice]
            }
        else:
            function = choice.get("function")
            if not isinstance(function, dict) or not isinstance(
                function.get("name"), str
            ):
                raise invalid("Invalid named tool_choice")
            call_config = {"mode": "ANY", "allowedFunctionNames": [function["name"]]}
        payload["toolConfig"] = {"functionCallingConfig": call_config}
    return GeminiGenerateContentRequest.model_validate(payload)


def usage_from_gemini(raw: dict) -> dict:
    return {
        "prompt_tokens": raw.get("promptTokenCount", 0),
        "completion_tokens": raw.get("candidatesTokenCount", 0),
        "total_tokens": raw.get("totalTokenCount", 0),
        "completion_tokens_details": {
            "reasoning_tokens": raw.get("thoughtsTokenCount", 0)
        },
        "prompt_tokens_details": {
            "cached_tokens": raw.get("cachedContentTokenCount") or 0
        },
    }


def finish_reason(reason: object, tools: bool) -> str:
    if tools:
        return "tool_calls"
    if reason == "MAX_TOKENS":
        return "length"
    return (
        "stop"
        if reason in {None, "STOP", "FINISH_REASON_UNSPECIFIED"}
        else "content_filter"
    )


def parts_delta(parts: list[dict], tool_offset: int = 0) -> tuple[dict, int]:
    delta: dict = {}
    calls = []
    for part in parts:
        if part.get("text"):
            key = "reasoning_content" if part.get("thought") else "content"
            delta[key] = delta.get(key, "") + part["text"]
        if function := part.get("functionCall"):
            calls.append(
                {
                    "index": tool_offset + len(calls),
                    "id": function.get("id") or f"call_{tool_offset + len(calls)}",
                    "type": "function",
                    "function": {
                        "name": function["name"],
                        "arguments": json.dumps(
                            function.get("args") or {}, ensure_ascii=False
                        ),
                    },
                }
            )
        if part.get("inlineData"):
            raise invalid(
                "OpenAI chat output supports text and function calls; use Gemini for image output"
            )
    if calls:
        delta["tool_calls"] = calls
    return delta, len(calls)


async def openai_events(
    response: StreamingResponse,
    model: str,
    completion_id: str,
    created: int,
    include_usage: bool,
) -> AsyncIterator[str]:
    tools = 0
    ended = False
    final_usage = None
    base = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
    }

    def chunk(delta: dict, reason: str | None = None) -> str:
        payload = {
            **base,
            "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
        }
        if include_usage:
            payload["usage"] = None
        return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

    yield chunk({"role": "assistant", "content": ""})
    # The shared Gemini producer emits one complete SSE event per yield.
    producer = cast(AsyncGenerator[str | bytes, None], response.body_iterator)
    try:
        async with aclosing(producer):
            async for raw in producer:
                text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
                payload = json.loads(text.removeprefix("data: ").strip())
                if "error" in payload:
                    error = payload["error"]
                    yield (
                        "data: "
                        + json.dumps(
                            {
                                "error": {
                                    "message": error["message"],
                                    "type": "upstream_error",
                                    "code": error.get("code"),
                                }
                            }
                        )
                        + "\n\n"
                    )
                    return
                if "usageMetadata" in payload:
                    final_usage = usage_from_gemini(payload["usageMetadata"])
                for candidate in payload.get("candidates", []):
                    delta, count = parts_delta(
                        candidate.get("content", {}).get("parts", []), tools
                    )
                    tools += count
                    if delta:
                        yield chunk(delta)
                    if candidate.get("finishReason"):
                        ended = True
                        yield chunk(
                            {}, finish_reason(candidate["finishReason"], tools > 0)
                        )
    except HTTPException as exc:
        failed(exc.status_code)
        detail = exc.detail
        message = (
            detail.get("message", "Unsupported output")
            if isinstance(detail, dict)
            else "Unsupported output"
        )
        yield (
            "data: "
            + json.dumps(
                {
                    "error": {
                        "message": message,
                        "type": "invalid_request_error",
                        "code": exc.status_code,
                    }
                }
            )
            + "\n\n"
        )
        return
    if not ended:
        yield 'data: {"error":{"message":"Upstream stream ended without a finish event","type":"upstream_error"}}\n\n'
        return
    if include_usage and final_usage is not None:
        yield (
            "data: "
            + json.dumps({**base, "choices": [], "usage": final_usage})
            + "\n\n"
        )
    yield "data: [DONE]\n\n"


@router.get("/models")
async def openai_models(state: RuntimeState = Depends(get_runtime_state)):
    models = await list_models(state)
    return {
        "object": "list",
        "data": [
            {
                "id": item.name.removeprefix("models/"),
                "object": "model",
                "created": 0,
                "owned_by": "google",
                "channels": item.channels,
            }
            for item in models.models
        ],
    }


@router.post("/chat/completions")
async def chat_completions(
    req: ChatRequest, client: AIStudioClient = Depends(get_client)
):
    gemini = to_gemini(req)
    observation = current.get()
    completion_id = "chatcmpl-" + (
        observation.id if observation else str(time.time_ns())
    )
    created = int(time.time())
    response = await handle_gemini_generate_content(
        req.model, gemini, client, stream=req.stream
    )
    if isinstance(response, StreamingResponse):
        return StreamingResponse(
            openai_events(
                response,
                req.model,
                completion_id,
                created,
                bool(req.stream_options and req.stream_options.get("include_usage")),
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    assert isinstance(response, GeminiGenerateContentResponse)
    candidate = response.candidates[0]
    delta, tools = parts_delta(
        [part.model_dump(exclude_none=True) for part in candidate.content.parts]
    )
    for call in delta.get("tool_calls", []):
        call.pop("index", None)
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": req.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": None, **delta},
                "finish_reason": finish_reason(candidate.finishReason, tools > 0),
            }
        ],
        "usage": usage_from_gemini(
            response.usageMetadata.model_dump() if response.usageMetadata else {}
        ),
    }
