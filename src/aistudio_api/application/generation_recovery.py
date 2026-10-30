"""Own bounded attempt recovery, actual-account effects and safe failure records."""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict

from aistudio_api.application.account_orchestrator import (
    AUTH_GENERATION_UNTRACKED,
    MAX_RETRIES,
    AccountPreparationError,
    account_generation_is_current,
    quota_unavailable_error,
    record_rotator_event,
    try_switch_account,
)
from aistudio_api.application.generation_errors import (
    ACCOUNT_IDENTITY_MESSAGES,
    GenerationFailure,
    clean_upstream_error_message,
)
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
)
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.gateway.wire_codec import InvalidRequestTemplateError
from aistudio_api.infrastructure.utils.error_records import record_failure
from aistudio_api.infrastructure.utils.logger import get_logger

if TYPE_CHECKING:
    from aistudio_api.application.generation import AttemptFailure

logger = get_logger("generation.recovery")


class _GenerationSwitchKwargs(TypedDict, total=False):
    failed_auth_generation: object


@dataclass(frozen=True)
class RetryAttempt:
    pass


@dataclass(frozen=True)
class StopGeneration:
    failure: GenerationFailure


async def recover_attempt(
    client: AIStudioClient, failure: AttemptFailure
) -> RetryAttempt | StopGeneration:
    identity = failure.identity
    try:
        retry = await _recover_failure(
            failure.error,
            attempt=failure.number,
            model_path=failure.model,
            normalized_model=failure.model,
            client=client,
            has_yielded_data=failure.replay_forbidden,
            account_id=identity.account_id,
            request_context_id=identity.context_id,
            request_auth_generation=identity.auth_generation,
        )
    except GenerationFailure as terminal:
        original = (
            failure.error.original_error
            if isinstance(failure.error, AccountPreparationError)
            else failure.error
        )
        if terminal.original_error is None and terminal is not original:
            terminal.original_error = (
                terminal.__cause__
                if isinstance(terminal.__cause__, Exception)
                else original
            )
        return StopGeneration(terminal)
    if retry:
        return RetryAttempt()
    return StopGeneration(
        GenerationFailure(
            500,
            reason="server_error",
            message="Internal server error",
            original_error=failure.error,
        )
    )


def _terminal_status(exc: Exception) -> int:
    return exc.description.status if isinstance(exc, GenerationFailure) else 500


async def _recover_failure(
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
    if isinstance(exc, GenerationFailure):
        raise exc
    operation = (
        "account_prepare"
        if isinstance(exc, AccountPreparationError)
        else "generate_attempt"
    )
    if isinstance(exc, AccountPreparationError):
        account_id = exc.account_id
        request_context_id = None
        request_auth_generation = exc.auth_generation
        exc = exc.original_error
        if isinstance(exc, GenerationFailure):
            raise exc
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
            record_failure(
                exc,
                source="gemini",
                operation=operation,
                model=normalized_model or model_path,
                account_id=account_id,
                attempt=attempt + 1,
                retrying=True,
            )
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
        record_failure(
            exc,
            source="gemini",
            operation=operation,
            model=normalized_model or model_path,
            account_id=account_id,
            attempt=attempt + 1,
            retrying=True,
        )
        if (
            preparation_error.auth_generation is not AUTH_GENERATION_UNTRACKED
            and not account_generation_is_current(
                preparation_error.account_id,
                preparation_error.auth_generation,
            )
        ):
            record_failure(
                preparation_error.original_error,
                source="gemini",
                operation="account_prepare",
                model=normalized_model or model_path,
                account_id=preparation_error.account_id,
                attempt=attempt + 1,
                retrying=True,
            )
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
            record_failure(
                preparation_error.original_error,
                source="gemini",
                operation="account_prepare",
                model=normalized_model or model_path,
                account_id=preparation_error.account_id,
                attempt=attempt + 1,
                http_status=_terminal_status(terminal),
            )
            raise
        record_failure(
            preparation_error.original_error,
            source="gemini",
            operation="account_prepare",
            model=normalized_model or model_path,
            account_id=preparation_error.account_id,
            attempt=attempt + 1,
            retrying=bool(retry),
        )
        return retry
    except Exception as terminal:
        record_failure(
            exc,
            source="gemini",
            operation=operation,
            model=normalized_model or model_path,
            account_id=account_id,
            attempt=attempt + 1,
            http_status=_terminal_status(terminal),
        )
        raise
    record_failure(
        exc,
        source="gemini",
        operation=operation,
        model=normalized_model or model_path,
        account_id=account_id,
        attempt=attempt + 1,
        retrying=bool(retry),
    )
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

    Returns True if the operation should be retried, or raises GenerationFailure / re-raises.
    """
    target_model = normalized_model or model_path
    can_retry = allow_retry and not has_yielded_data and attempt < MAX_RETRIES - 1
    generation_kwargs: _GenerationSwitchKwargs = (
        {}
        if request_auth_generation is AUTH_GENERATION_UNTRACKED
        else {"failed_auth_generation": request_auth_generation}
    )

    if isinstance(exc, GenerationFailure):
        raise exc

    if isinstance(exc, InvalidRequestTemplateError):
        client.clear_templates()
        if can_retry and attempt == 0:
            if request_context_id is not None:
                await client.session.reset_if_active(request_context_id)
            return True
        raise GenerationFailure(
            503, message=str(exc), reason="session_unavailable"
        ) from exc

    if isinstance(exc, ValueError):
        raise GenerationFailure(400, message=str(exc), reason="bad_request") from exc

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
                logger.info(
                    "账号身份验证失败后切换至可用账号重试 (%d/%d)",
                    attempt + 1,
                    MAX_RETRIES,
                )
                return True
        else:
            record_rotator_event(
                "auth_error", model=target_model, account_id=account_id
            )
        raise GenerationFailure(
            503, message=ACCOUNT_IDENTITY_MESSAGES[exc.code], reason=exc.code
        ) from exc

    if isinstance(exc, SessionExpiredError):
        logger.warning("账号会话失效，已被重定向到登录页: type=%s", type(exc).__name__)
        client.clear_templates()
        record_rotator_event(
            "session_expired", model=target_model, account_id=account_id
        )
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
        raise GenerationFailure(
            503,
            message="The API key is valid, but upstream Google sessions have expired. Re-import Google cookies on the management page; keep your API key.",
            reason="upstream_auth_expired",
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
            record_rotator_event(
                "auth_error", model=target_model, account_id=account_id
            )
        raise GenerationFailure(
            503,
            message="Upstream Google authentication failed. Check accounts on the management page; keep your API key.",
            reason="upstream_auth_failed",
        ) from exc

    if isinstance(exc, UsageLimitExceeded):
        runtime_state.record(target_model, "rate_limited")
        record_rotator_event("rate_limited", model=target_model, account_id=account_id)
        if can_retry and await try_switch_account(
            model=target_model,
            failed_account_id=account_id,
            **generation_kwargs,
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
        raise GenerationFailure(
            504,
            message="Upstream preparation or generation timed out",
            reason="upstream_timeout",
        ) from exc

    if isinstance(exc, BrowserTransportError):
        raise GenerationFailure(
            503,
            message="Browser response delivery failed",
            reason="browser_transport_error",
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
            model=target_model,
            failed_account_id=account_id,
            is_auth_error=False,
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
        logger.warning(
            "Gemini 上游请求错误 (%d): type=%s", status_code, type(exc).__name__
        )
        raise GenerationFailure(
            status_code, message=clean_msg, reason=error_type
        ) from exc

    if isinstance(exc, AistudioError):
        runtime_state.record(target_model, "errors")
        record_rotator_event("error", model=target_model, account_id=account_id)
        logger.warning("Gemini 服务端异常: type=%s", type(exc).__name__)
        raise GenerationFailure(
            500, message="Internal server error", reason="server_error"
        ) from exc

    runtime_state.record(target_model, "errors")
    record_rotator_event("error", model=target_model, account_id=account_id)
    # Keep actionable stack locations without logging source lines, locals,
    # captured request templates, exception arguments, or account credentials.
    frames = [
        {
            "file": frame.filename.replace("\\", "/").rsplit("/", 1)[-1],
            "line": frame.lineno,
            "function": frame.name,
        }
        for frame in traceback.extract_tb(exc.__traceback__)[-12:]
    ]
    logger.error("Gemini 服务器异常: type=%s frames=%s", type(exc).__name__, frames)
    raise GenerationFailure(
        500, message="Internal server error", reason="server_error"
    ) from exc
