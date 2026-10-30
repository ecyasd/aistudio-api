"""Framework-free terminal generation failures and compatible public messages."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from aistudio_api.domain.errors import parse_upstream_grpc_error


class FailureReason(StrEnum):
    SERVICE_BUSY = "service_busy"
    RATE_LIMITED = "rate_limit_exceeded"
    SESSION_UNAVAILABLE = "session_unavailable"
    BAD_REQUEST = "bad_request"
    ACCOUNT_MISMATCH = "upstream_account_mismatch"
    ACCOUNT_UNAVAILABLE = "upstream_account_unavailable"
    AUTH_EXPIRED = "upstream_auth_expired"
    AUTH_FAILED = "upstream_auth_failed"
    TIMEOUT = "upstream_timeout"
    TRANSPORT = "browser_transport_error"
    NOT_FOUND = "not_found"
    UPSTREAM = "upstream_error"
    INTERNAL = "server_error"
    ACCOUNT_NOT_READY = "account_not_ready"
    NO_ACCOUNTS = "no_accounts"


@dataclass(frozen=True)
class FailureDescription:
    reason: FailureReason
    status: int
    public_message: str
    retry_after_seconds: int | None = None


class GenerationFailure(Exception):
    """A terminal application result; its original cause is never serialized."""

    def __init__(
        self,
        status: int,
        *,
        reason: str,
        message: str,
        retry_after_seconds: int | None = None,
        original_error: Exception | None = None,
    ) -> None:
        self.description = FailureDescription(
            FailureReason(reason),
            status,
            message,
            retry_after_seconds,
        )
        self.original_error = original_error
        super().__init__(self.description.reason.value)


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
