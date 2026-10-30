"""Gemini HTTP mapping, error policy and bounded SSE delivery."""

from __future__ import annotations

import asyncio
import json
import re
import traceback
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import suppress
from typing import TypedDict, cast

from anyio import CancelScope
from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse
from starlette.types import Message, Send

from aistudio_api.api.response_models import (
    GeminiCandidateResponse,
    GeminiContentResponse,
    GeminiGenerateContentResponse,
)
from aistudio_api.api.responses import to_gemini_parts, to_gemini_usage_metadata
from aistudio_api.api.schemas import GeminiGenerateContentRequest
from aistudio_api.application.account_orchestrator import (
    AUTH_GENERATION_UNTRACKED,
    MAX_RETRIES,
    AccountPreparationError,
    account_generation_is_current,
    quota_unavailable_error,
    record_rotator_event,
    try_switch_account,
    validate_account_availability,
)
from aistudio_api.application.chat_service import (
    NormalizedGeminiRequest,
    normalize_gemini_request,
)
from aistudio_api.application.generation import (
    AttemptFailure,
    GenerationDeadlineExceeded,
    GenerationEvent,
    GenerationRun,
)
from aistudio_api.application.model_selection import parse_model_selection
from aistudio_api.application.runtime import runtime_state
from aistudio_api.domain.errors import (
    AccountIdentityMismatchError,
    AccountIdentityUnavailableError,
    AistudioError,
    AuthError,
    BrowserTransportError,
    IncompleteResponseError,
    RequestError,
    SessionExpiredError,
    UsageLimitExceeded,
    is_browser_session_hang_error,
    is_transient_rpc_error,
    parse_upstream_grpc_error,
)
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.gateway.wire_codec import InvalidRequestTemplateError
from aistudio_api.infrastructure.utils.error_records import (
    has_final_failure,
    record_failure,
)
from aistudio_api.infrastructure.utils.logger import get_logger
from aistudio_api.infrastructure.utils.response_diagnostics import (
    configure_generation,
)

logger = get_logger("api.gemini")
SSE_HEARTBEAT_INTERVAL_SECONDS = 15.0
SSE_SEND_TIMEOUT_SECONDS = 30.0
ACCOUNT_IDENTITY_MESSAGES = {
    "upstream_account_mismatch": (
        "AI Studio account identity does not match the selected account. "
        "Check the selected Google account on the management page; keep your API key."
    ),
    "upstream_account_unavailable": (
        "AI Studio could not verify the selected Google account. "
        "Retry later or check the account on the management page; keep your API key."
    ),
}


class _GenerationSwitchKwargs(TypedDict, total=False):
    failed_auth_generation: object


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


def clean_upstream_error_message(raw_msg: str) -> str:
    """Extract a clean, readable error message from Google/JSPB upstream error strings."""
    if not raw_msg:
        return ""
    text = raw_msg.strip()
    if "[,[" not in text and "[null,[" not in text and not text.startswith("[,"):
        return text
    inner = text
    while True:
        m = re.match(r"^HTTP\s+\d+:\s*(.*)$", inner)
        if m:
            inner = m.group(1).strip()
        else:
            break
    _, msg = parse_upstream_grpc_error(inner)
    return msg or inner

def classify_gemini_error_payload(exc: Exception) -> tuple[int, str, str]:
    """Extract standard Gemini HTTP status code, message, and status string from an exception."""
    if isinstance(exc, AccountPreparationError):
        exc = exc.original_error
    if isinstance(exc, (AccountIdentityMismatchError, AccountIdentityUnavailableError)):
        return 503, ACCOUNT_IDENTITY_MESSAGES[exc.code], "UNAVAILABLE"
    if isinstance(exc, SessionExpiredError):
        return (
            503,
            ("The API key is valid, but upstream Google sessions have expired. "
             "Re-import Google cookies on the management page; keep your API key."),
            "UNAVAILABLE",
        )
    if isinstance(exc, AuthError):
        return 503, "Upstream Google authentication failed. Check accounts on the management page; keep your API key.", "UNAVAILABLE"
    if isinstance(exc, UsageLimitExceeded):
        return 429, str(exc), "RESOURCE_EXHAUSTED"
    if isinstance(exc, (ValueError, RequestValidationError)):
        return 400, str(exc), "INVALID_ARGUMENT"
    if isinstance(exc, HTTPException):
        status = exc.status_code
        detail = exc.detail
        raw_msg = detail.get("message") if isinstance(detail, dict) else detail
        msg = str(raw_msg) if raw_msg is not None else ""
        clean_msg = clean_upstream_error_message(msg)
        status_map = {
            400: "INVALID_ARGUMENT",
            401: "UNAUTHENTICATED",
            403: "PERMISSION_DENIED",
            404: "NOT_FOUND",
            429: "RESOURCE_EXHAUSTED",
            500: "INTERNAL",
            503: "UNAVAILABLE",
            504: "DEADLINE_EXCEEDED",
        }
        return (
            status,
            clean_msg,
            status_map.get(status, "INTERNAL" if status >= 500 else "UNKNOWN"),
        )
    if isinstance(exc, RequestError):
        status = exc.status if 400 <= exc.status <= 599 else 502
        clean_msg = clean_upstream_error_message(str(exc))
        status_map = {
            400: "INVALID_ARGUMENT",
            401: "UNAUTHENTICATED",
            403: "PERMISSION_DENIED",
            404: "NOT_FOUND",
            429: "RESOURCE_EXHAUSTED",
            500: "INTERNAL",
            503: "UNAVAILABLE",
            504: "DEADLINE_EXCEEDED",
        }
        return (
            status,
            clean_msg,
            status_map.get(status, "INTERNAL" if status >= 500 else "UNKNOWN"),
        )
    if isinstance(exc, BrowserTransportError):
        return 503, "Browser response delivery failed", "UNAVAILABLE"
    if isinstance(exc, TimeoutError):
        return 504, "Upstream preparation or generation timed out", "DEADLINE_EXCEEDED"
    if isinstance(exc, AistudioError):
        return 500, "Internal server error", "INTERNAL"

    return 500, "Internal server error", "INTERNAL"


async def handle_attempt_exception(
    exc: Exception,
    *,
    attempt: int,
    model_path: str,
    normalized_model: str | None,
    client: AIStudioClient,
    has_yielded_data: bool = False,
    account_id: str | None = None,
    request_context_id: int | None = None,
    request_auth_generation: object = AUTH_GENERATION_UNTRACKED,
) -> bool:
    """Preserve the account and error type when browser preparation fails."""
    operation = "account_prepare" if isinstance(exc, AccountPreparationError) else "generate_attempt"
    if isinstance(exc, AccountPreparationError):
        account_id = exc.account_id
        request_context_id = None
        request_auth_generation = exc.auth_generation
        exc = exc.original_error
    if (
        account_id is not None
        and request_auth_generation is not AUTH_GENERATION_UNTRACKED
        and not account_generation_is_current(account_id, request_auth_generation)
    ):
        # The owner replaced these credentials while this request held its old
        # lease. Retrying before output may use the new generation; failures
        # after output still terminate, without quarantining the replacement.
        if (
            not has_yielded_data
            and attempt < MAX_RETRIES - 1
            and not isinstance(exc, (TimeoutError, BrowserTransportError))
        ):
            record_failure(exc, source="gemini", operation=operation,
                           model=normalized_model or model_path, account_id=account_id,
                           attempt=attempt + 1, retrying=True)
            return True
        account_id = None
    try:
        retry = await _handle_attempt_exception(
            exc,
            attempt=attempt,
            model_path=model_path,
            normalized_model=normalized_model,
            client=client,
            has_yielded_data=has_yielded_data,
            account_id=account_id,
            request_context_id=request_context_id,
            request_auth_generation=request_auth_generation,
        )
    except AccountPreparationError as preparation_error:
        record_failure(exc, source="gemini", operation=operation,
                       model=normalized_model or model_path, account_id=account_id,
                       attempt=attempt + 1, retrying=True)
        if (
            preparation_error.auth_generation is not AUTH_GENERATION_UNTRACKED
            and not account_generation_is_current(
                preparation_error.account_id, preparation_error.auth_generation,
            )
        ):
            record_failure(preparation_error.original_error, source="gemini",
                           operation="account_prepare", model=normalized_model or model_path,
                           account_id=preparation_error.account_id, attempt=attempt + 1,
                           retrying=True)
            return True
        # Switching itself can fail while preparing a different account. Mark
        # that actual target once and finish; do not recursively rotate forever.
        try:
            retry = await _handle_attempt_exception(
                preparation_error.original_error,
                attempt=attempt,
                model_path=model_path,
                normalized_model=normalized_model,
                client=client,
                has_yielded_data=has_yielded_data,
                account_id=preparation_error.account_id,
                allow_retry=False,
            )
        except Exception as terminal:
            record_failure(preparation_error.original_error, source="gemini",
                           operation="account_prepare", model=normalized_model or model_path,
                           account_id=preparation_error.account_id, attempt=attempt + 1,
                           http_status=classify_gemini_error_payload(terminal)[0])
            raise
        record_failure(preparation_error.original_error, source="gemini",
                       operation="account_prepare", model=normalized_model or model_path,
                       account_id=preparation_error.account_id, attempt=attempt + 1,
                       retrying=bool(retry))
        return retry
    except Exception as terminal:
        record_failure(exc, source="gemini", operation=operation,
                       model=normalized_model or model_path, account_id=account_id,
                       attempt=attempt + 1,
                       http_status=classify_gemini_error_payload(terminal)[0])
        raise
    record_failure(exc, source="gemini", operation=operation,
                   model=normalized_model or model_path, account_id=account_id,
                   attempt=attempt + 1, retrying=bool(retry))
    return retry


async def _handle_attempt_exception(
    exc: Exception,
    *,
    attempt: int,
    model_path: str,
    normalized_model: str | None,
    client: AIStudioClient,
    has_yielded_data: bool = False,
    account_id: str | None = None,
    request_context_id: int | None = None,
    allow_retry: bool = True,
    request_auth_generation: object = AUTH_GENERATION_UNTRACKED,
) -> bool:
    """Handle per-attempt error classification and account switching.

    Returns True if the operation should be retried, or raises HTTPException / re-raises.
    """
    target_model = normalized_model or model_path
    can_retry = allow_retry and not has_yielded_data and attempt < MAX_RETRIES - 1
    generation_kwargs: _GenerationSwitchKwargs = (
        {} if request_auth_generation is AUTH_GENERATION_UNTRACKED
        else {"failed_auth_generation": request_auth_generation}
    )

    if isinstance(exc, HTTPException):
        raise exc

    if isinstance(exc, InvalidRequestTemplateError):
        client.clear_templates()
        if can_retry and attempt == 0:
            if request_context_id is not None:
                await client.session.reset_if_active(request_context_id)
            return True
        raise HTTPException(
            503, detail={"message": str(exc), "type": "session_unavailable"}
        ) from exc

    if isinstance(exc, (ValueError, RequestValidationError)):
        raise HTTPException(
            400, detail={"message": str(exc), "type": "bad_request"}
        ) from exc

    if isinstance(exc, IncompleteResponseError) and attempt == 0 and can_retry:
        # No response Part has reached the client, so discard this unpublished
        # attempt and retry once. A protocol EOF does not quarantine or rotate
        # an otherwise healthy account.
        logger.warning("Gemini 上游响应未完整结束，未输出内容时重试一次")
        client.clear_templates()
        return True

    if isinstance(exc, (AccountIdentityMismatchError, AccountIdentityUnavailableError)):
        logger.warning("账号身份验证失败: type=%s", type(exc).__name__)
        client.clear_templates()
        if can_retry:
            if await try_switch_account(
                model=target_model,
                failed_account_id=account_id,
                failed_context_id=request_context_id,
                is_auth_error=True,
                **generation_kwargs,
            ):
                logger.info("账号身份验证失败后切换至可用账号重试 (%d/%d)", attempt + 1, MAX_RETRIES)
                return True
        else:
            record_rotator_event("auth_error", model=target_model, account_id=account_id)
        raise HTTPException(
            503,
            detail={"message": ACCOUNT_IDENTITY_MESSAGES[exc.code], "type": exc.code},
        ) from exc

    if isinstance(exc, SessionExpiredError):
        logger.warning("账号会话失效，已被重定向到登录页: type=%s", type(exc).__name__)
        client.clear_templates()
        record_rotator_event("session_expired", model=target_model, account_id=account_id)
        if can_retry and await try_switch_account(
            model=target_model,
            failed_account_id=account_id,
            is_auth_error=True,
            is_session_expired=True,
            failure_recorded=True,
            **generation_kwargs,
        ):
            logger.info(
                "已切换至其他可用健康账号重试 (%d/%d)", attempt + 1, MAX_RETRIES
            )
            return True
        raise HTTPException(
            503,
            detail={
                "message": "The API key is valid, but upstream Google sessions have expired. Re-import Google cookies on the management page; keep your API key.",
                "type": "upstream_auth_expired",
            },
        ) from exc

    if isinstance(exc, AuthError):
        logger.warning("账号 403 权限拒绝: type=%s", type(exc).__name__)
        client.clear_templates()
        if can_retry:
            # The switch lock must decide whether this 403 belongs to an older
            # context that another request has already refreshed successfully.
            if await try_switch_account(
                model=target_model,
                failed_account_id=account_id,
                failed_context_id=request_context_id,
                is_auth_error=True,
                **generation_kwargs,
            ):
                logger.info("切换至可用账号重试 (%d/%d)", attempt + 1, MAX_RETRIES)
                return True
        else:
            record_rotator_event("auth_error", model=target_model, account_id=account_id)
        raise HTTPException(
            503, detail={"message": "Upstream Google authentication failed. Check accounts on the management page; keep your API key.", "type": "upstream_auth_failed"}
        ) from exc

    if isinstance(exc, UsageLimitExceeded):
        runtime_state.record(target_model, "rate_limited")
        record_rotator_event("rate_limited", model=target_model, account_id=account_id)
        if can_retry and await try_switch_account(
            model=target_model, failed_account_id=account_id, **generation_kwargs,
        ):
            logger.info(
                "429 触发限额，切换账号 (%d/%d): model=%s",
                attempt + 1,
                MAX_RETRIES,
                target_model,
            )
            return True
        logger.warning("全部账号在模型 %s 上均处于冷却状态", target_model)
        raise quota_unavailable_error(target_model) from exc

    if (
        isinstance(exc, RequestError)
        and exc.status == 204
        and attempt == 0
        and can_retry
    ):
        logger.warning("Gemini 收到 204，清理模板缓存后重试一次")
        client.clear_templates()
        return True

    # 针对 Google 上游偶发且完全随机的 RPC 路由 404 (Ambiguous request for service '' and method '/GenerativeService.*')：
    # 此报错为上游端点偶发抖动，在未产生输出时当场立即重试（严禁切号以防浪费配额，其他 404 严格按原样报错抛出）
    if is_transient_rpc_error(exc) and can_retry:
        logger.warning(
            "检测到 Google 上游偶发 404 (Ambiguous RPC method)，当场立即重试 (%d/%d): type=%s",
            attempt + 1,
            MAX_RETRIES,
            type(exc).__name__,
        )
        client.clear_templates()
        return True

    # The transport has already waited for its idle budget and, for a lost
    # push, attempted to repair the original stream. The broad session-hang
    # matcher also accepts "timeout"/"CDP"; do not let it rotate accounts and
    # start another multi-minute generation for these terminal failures.
    if isinstance(exc, TimeoutError):
        raise HTTPException(
            504, detail={"message": "Upstream preparation or generation timed out", "type": "upstream_timeout"}
        ) from exc

    if isinstance(exc, BrowserTransportError):
        raise HTTPException(
            503, detail={"message": "Browser response delivery failed", "type": "browser_transport_error"}
        ) from exc

    if is_browser_session_hang_error(exc):
        logger.warning(
            "模板或 BotGuard 捕获超时 (%d/%d): type=%s，切换账号重试",
            attempt + 1,
            MAX_RETRIES,
            type(exc).__name__,
        )
        client.clear_templates()
        if can_retry and await try_switch_account(
            model=target_model, failed_account_id=account_id, is_auth_error=False,
            **generation_kwargs,
        ):
            logger.info("已切换账号重试 (%d/%d)", attempt + 1, MAX_RETRIES)
            return True
        if (
            attempt < 2
            and can_retry
            and request_context_id is not None
            and await client.session.reset_if_active(request_context_id)
        ):
            return True
    if isinstance(exc, RequestError):
        clean_msg = clean_upstream_error_message(str(exc))
        status_code = exc.status if 400 <= exc.status <= 599 else 502
        error_type = (
            "bad_request"
            if status_code == 400
            else "not_found"
            if status_code == 404
            else "upstream_error"
        )
        logger.warning("Gemini 上游请求错误 (%d): type=%s", status_code, type(exc).__name__)
        raise HTTPException(
            status_code, detail={"message": clean_msg, "type": error_type}
        ) from exc

    if isinstance(exc, AistudioError):
        runtime_state.record(target_model, "errors")
        record_rotator_event("error", model=target_model, account_id=account_id)
        logger.warning("Gemini 服务端异常: type=%s", type(exc).__name__)
        raise HTTPException(
            500, detail={"message": "Internal server error", "type": "server_error"}
        ) from exc

    runtime_state.record(target_model, "errors")
    record_rotator_event("error", model=target_model, account_id=account_id)
    # Keep actionable stack locations without logging source lines, locals,
    # captured request templates, exception arguments, or account credentials.
    frames = [
        {"file": frame.filename.replace("\\", "/").rsplit("/", 1)[-1],
         "line": frame.lineno, "function": frame.name}
        for frame in traceback.extract_tb(exc.__traceback__)[-12:]
    ]
    logger.error("Gemini 服务器异常: type=%s frames=%s", type(exc).__name__, frames)
    raise HTTPException(
        500, detail={"message": "Internal server error", "type": "server_error"}
    ) from exc


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


def _generation_run(
    client: AIStudioClient, normalized: NormalizedGeminiRequest,
    req: GeminiGenerateContentRequest, model_path: str,
) -> GenerationRun:
    async def recover(failure: AttemptFailure) -> bool:
        identity = failure.identity
        return await handle_attempt_exception(
            failure.error, attempt=failure.number, model_path=model_path,
            normalized_model=failure.model, client=client,
            has_yielded_data=failure.replay_forbidden,
            account_id=identity.account_id, request_context_id=identity.context_id,
            request_auth_generation=identity.auth_generation,
        )

    return GenerationRun(client, normalized, auto_continue=req.autoContinue, recover=recover)


async def _generate_non_streaming(
    model_path: str, req: GeminiGenerateContentRequest, client: AIStudioClient,
):
    try:
        normalized = normalize_gemini_request(req, model_path)
    except (ValueError, RequestValidationError) as exc:
        raise HTTPException(400, detail={"message": str(exc)}) from exc
    try:
        output = await _generation_run(client, normalized, req, model_path).generate()
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


def gemini_error_type(exc: Exception) -> str | None:
    """Expose only fixed service-auth categories, never upstream error data."""
    if isinstance(exc, AccountPreparationError):
        exc = exc.original_error
    if isinstance(exc, (AccountIdentityMismatchError, AccountIdentityUnavailableError)):
        return exc.code
    if isinstance(exc, SessionExpiredError):
        return "upstream_auth_expired"
    if isinstance(exc, AuthError):
        return "upstream_auth_failed"
    if isinstance(exc, HTTPException) and isinstance(exc.detail, dict):
        value = exc.detail.get("type")
        if value in (
            "upstream_auth_expired", "upstream_auth_failed",
            "upstream_account_mismatch", "upstream_account_unavailable",
        ):
            return value
    return None


def format_sse_error(exc: Exception) -> str:
    """Format exception into standard Gemini SSE error chunk."""
    code, msg, status_str = classify_gemini_error_payload(exc)
    error_type = gemini_error_type(exc)
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


async def _sse_with_heartbeats(
    source: Callable[[Callable[[str], Awaitable[None]]], Awaitable[None]], *, interval: float,
) -> AsyncGenerator[str, None]:
    """Run generation and close its resources in one task, with one bounded queue."""
    queue: asyncio.Queue[str | Exception | None] = asyncio.Queue(maxsize=1)

    async def produce() -> None:
        try:
            await source(queue.put)
        except Exception as exc:
            await queue.put(exc)
        else:
            await queue.put(None)

    producer = asyncio.create_task(produce(), name="gemini-sse-producer")
    pending: asyncio.Task[str | Exception | None] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(queue.get(), name="gemini-sse-read")
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
            run = _generation_run(client, normalized, req, model_path)

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
                    http_status=classify_gemini_error_payload(exc)[0],
                )
            await emit(format_sse_error(exc))

    return _GeminiStreamingResponse(
        _sse_with_heartbeats(produce, interval=SSE_HEARTBEAT_INTERVAL_SECONDS),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
