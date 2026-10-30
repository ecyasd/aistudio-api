"""Pure, shared Gemini projection for application, HTTP and SSE failures."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError

from aistudio_api.application.account_orchestrator import AccountPreparationError
from aistudio_api.application.generation_errors import (
    ACCOUNT_IDENTITY_MESSAGES,
    GenerationFailure,
    clean_upstream_error_message,
)
from aistudio_api.domain.errors import (
    AccountIdentityMismatchError,
    AccountIdentityUnavailableError,
    AistudioError,
    AuthError,
    BrowserTransportError,
    RequestError,
    SessionExpiredError,
    UsageLimitExceeded,
)

AUTH_ERROR_TYPES = frozenset(
    {
        "upstream_auth_expired",
        "upstream_auth_failed",
        "upstream_account_mismatch",
        "upstream_account_unavailable",
    }
)


def _status_name(status: int) -> str:
    return {
        400: "INVALID_ARGUMENT",
        401: "UNAUTHENTICATED",
        403: "PERMISSION_DENIED",
        404: "NOT_FOUND",
        429: "RESOURCE_EXHAUSTED",
        500: "INTERNAL",
        503: "UNAVAILABLE",
        504: "DEADLINE_EXCEEDED",
    }.get(status, "INTERNAL" if status >= 500 else "UNKNOWN")


def classify_gemini_error_payload(exc: Exception) -> tuple[int, str, str]:
    """Extract standard Gemini HTTP status code, message, and status string from an exception."""
    if isinstance(exc, AccountPreparationError):
        exc = exc.original_error
    if isinstance(exc, GenerationFailure):
        description = exc.description
        return (
            description.status,
            description.public_message,
            _status_name(description.status),
        )
    if isinstance(exc, (AccountIdentityMismatchError, AccountIdentityUnavailableError)):
        return 503, ACCOUNT_IDENTITY_MESSAGES[exc.code], "UNAVAILABLE"
    if isinstance(exc, SessionExpiredError):
        return (
            503,
            (
                "The API key is valid, but upstream Google sessions have expired. "
                "Re-import Google cookies on the management page; keep your API key."
            ),
            "UNAVAILABLE",
        )
    if isinstance(exc, AuthError):
        return (
            503,
            "Upstream Google authentication failed. Check accounts on the management page; keep your API key.",
            "UNAVAILABLE",
        )
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
        return (
            status,
            clean_msg,
            _status_name(status),
        )
    if isinstance(exc, RequestError):
        status = exc.status if 400 <= exc.status <= 599 else 502
        clean_msg = clean_upstream_error_message(str(exc))
        return (
            status,
            clean_msg,
            _status_name(status),
        )
    if isinstance(exc, BrowserTransportError):
        return 503, "Browser response delivery failed", "UNAVAILABLE"
    if isinstance(exc, TimeoutError):
        return 504, "Upstream preparation or generation timed out", "DEADLINE_EXCEEDED"
    if isinstance(exc, AistudioError):
        return 500, "Internal server error", "INTERNAL"

    return 500, "Internal server error", "INTERNAL"


def gemini_error_type(exc: Exception) -> str | None:
    """Expose only fixed service-auth categories, never upstream error data."""
    if isinstance(exc, AccountPreparationError):
        exc = exc.original_error
    if isinstance(exc, GenerationFailure):
        reason = exc.description.reason.value
        return reason if reason in AUTH_ERROR_TYPES else None
    if isinstance(exc, (AccountIdentityMismatchError, AccountIdentityUnavailableError)):
        return exc.code
    if isinstance(exc, SessionExpiredError):
        return "upstream_auth_expired"
    if isinstance(exc, AuthError):
        return "upstream_auth_failed"
    if isinstance(exc, HTTPException) and isinstance(exc.detail, dict):
        value = exc.detail.get("type")
        if isinstance(value, str) and value in AUTH_ERROR_TYPES:
            return value
    return None


@dataclass(frozen=True)
class GeminiError:
    status_code: int
    message: str
    status: str
    error_type: str | None
    headers: Mapping[str, str] | None


def project_gemini_error(exc: Exception) -> GeminiError:
    code, message, status = classify_gemini_error_payload(exc)
    cause = exc.original_error if isinstance(exc, AccountPreparationError) else exc
    if isinstance(cause, GenerationFailure):
        seconds = cause.description.retry_after_seconds
        headers = {"Retry-After": str(seconds)} if seconds is not None else None
    else:
        headers = getattr(exc, "headers", None)
    return GeminiError(code, message, status, gemini_error_type(exc), headers)
