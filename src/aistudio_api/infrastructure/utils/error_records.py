"""A short in-memory window of safe diagnostics for the live management view."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import re
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from aistudio_api.domain.errors import (
    AccountIdentityMismatchError,
    AccountIdentityUnavailableError,
    AuthError,
    RequestError,
    SessionExpiredError,
    UsageLimitExceeded,
)

MAX_RECORDS = 100
RETENTION_MINUTES = 10
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z")
_MODEL = re.compile(r"(?:models/)?(?:gemini|gemma)[a-z0-9_.-]{0,90}\Z")
_ACCOUNT = re.compile(r"(?:acc_[a-f0-9]{8}|[a-f0-9]{32}|[a-f0-9-]{36})\Z")
_REQUEST = re.compile(r"(?:req_[a-f0-9]{8,32}|[a-f0-9]{32}|[a-f0-9-]{36})\Z")
_EVENT_ID = re.compile(r"[a-f0-9]{32}\Z")
_CATEGORIES: dict[str, str] = {
    "session_expired": "Google 登录状态已过期，请重新导入登录凭据。",
    "authentication": "账号认证失败，请检查登录状态与访问权限。",
    "rate_limit": "请求受到配额或频率限制，请稍后重试。",
    "timeout": "请求或浏览器准备超时。",
    "browser": "浏览器会话准备或连接失败。",
    "network": "连接上游服务时发生网络错误。",
    "validation": "请求参数未通过校验。",
    "upstream": "上游服务返回错误。",
    "internal": "服务内部发生错误，请参考异常类型与安全堆栈位置。",
    "cancelled": "请求已取消或客户端已断开连接。",
}
_IDENTITY_DETAILS: dict[str, str] = {
    "upstream_account_mismatch": "AI Studio 当前身份与所选账号不一致，已阻止继续使用该会话。",
    "upstream_account_unavailable": "AI Studio 账号身份暂时无法核验，浏览器可能尚未就绪；已阻止继续使用该会话。",
}
_BROWSER_EXCEPTIONS = {
    "InvalidRequestTemplateError", "BrowserError", "TargetClosedError", "CDPError",
}
_NETWORK_EXCEPTIONS = {
    "ConnectError", "ReadError", "WriteError", "RemoteProtocolError", "ProxyError",
    "ConnectionClosed", "ConnectionClosedError", "ConnectionClosedOK",
    "WebSocketException", "ConnectionError", "BrokenPipeError",
    "ConnectionResetError", "ConnectionRefusedError", "gaierror",
}
_store: ErrorRecordStore | None = None
_REGISTRY_LOCK = threading.RLock()


@dataclass
class RequestErrorContext:
    """A shared object so child tasks report into their originating request."""

    request_id: str
    operation: str = "http_request"
    recorded_event_keys: dict[tuple[int, int | None, bool], dict[str, Any]] = field(
        default_factory=dict
    )
    exception_ids: set[int] = field(default_factory=set)
    _exception_references: list[BaseException] = field(default_factory=list, repr=False)
    final_failure: bool = False
    warning: bool = False
    transport_status: int | None = None
    start_monotonic: float = field(default_factory=time.monotonic)
    response_diagnostics: Any | None = field(default=None, repr=False)


_REQUEST_CONTEXT: contextvars.ContextVar[RequestErrorContext | None] = (
    contextvars.ContextVar("error_record_request_context", default=None)
)


def bind_request_context(request_id: str | None = None) -> contextvars.Token:
    safe_id = request_id if isinstance(request_id, str) and _REQUEST.fullmatch(request_id) else None
    return _REQUEST_CONTEXT.set(RequestErrorContext(safe_id or f"req_{uuid.uuid4().hex}"))


def reset_request_context(token: contextvars.Token) -> None:
    _REQUEST_CONTEXT.reset(token)


def get_request_context() -> RequestErrorContext | None:
    return _REQUEST_CONTEXT.get()


def get_request_id() -> str | None:
    context = get_request_context()
    return context.request_id if context else None


def has_final_failure() -> bool:
    context = get_request_context()
    return bool(context and context.final_failure)


def has_recorded_failure() -> bool:
    context = get_request_context()
    return bool(context and context.recorded_event_keys)


def is_recorded_exception(exc: BaseException) -> bool:
    context = get_request_context()
    return bool(context and id(exc) in context.exception_ids)


def _safe_token(value: object, fallback: str = "unknown") -> str:
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else fallback


def _safe_optional(value: object, pattern: re.Pattern[str]) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _http_status(value: object) -> int | None:
    return value if type(value) is int and 400 <= value <= 599 else None


def _unwrap(exc: BaseException) -> BaseException:
    # Preserve typed failures across preparation/HTTP wrappers without looking at
    # their messages. Follow only explicit causes, with a bound for malformed chains.
    seen = {id(exc)}
    for _ in range(8):
        if isinstance(exc, (AuthError, RequestError, UsageLimitExceeded, TimeoutError)):
            break
        original = getattr(exc, "original_error", None)
        if not isinstance(original, BaseException):
            original = exc.__cause__
        if not isinstance(original, BaseException) or id(original) in seen:
            break
        seen.add(id(original))
        exc = original
    return exc


def _failure_code(exc: BaseException, category: str) -> str:
    if isinstance(exc, AccountIdentityMismatchError):
        return "upstream_account_mismatch"
    if isinstance(exc, AccountIdentityUnavailableError):
        return "upstream_account_unavailable"
    return category


def classify_failure(exc: BaseException, http_status: int | None = None) -> tuple[int, str, str]:
    """Return a stable status/category/summary; exception messages are never returned."""
    error = _unwrap(exc)
    status = _http_status(http_status) or _http_status(getattr(error, "status_code", None))
    status = status or _http_status(getattr(error, "status", None))
    name = type(error).__name__
    if isinstance(error, asyncio.CancelledError) or status == 499:
        category, status = "cancelled", status or 499
    elif isinstance(error, SessionExpiredError):
        category, status = "session_expired", status or 401
    elif isinstance(error, AuthError) or status in (401, 403):
        category, status = "authentication", status or 401
    elif isinstance(error, UsageLimitExceeded) or status == 429:
        category, status = "rate_limit", status or 429
    elif isinstance(error, TimeoutError) or "Timeout" in name or status in (408, 504):
        category, status = "timeout", status or 504
    elif name in _BROWSER_EXCEPTIONS:
        category, status = "browser", status or 503
    elif name in _NETWORK_EXCEPTIONS:
        category, status = "network", status or 502
    elif status in (400, 404, 405, 409, 413, 415, 422):
        category = "validation"
    elif isinstance(error, RequestError) or (status is not None and status >= 500):
        category, status = "upstream", status or 502
    else:
        category, status = "internal", status or 500
    summary = _CATEGORIES[category]
    if isinstance(error, (AccountIdentityMismatchError, AccountIdentityUnavailableError)):
        summary = _IDENTITY_DETAILS[_failure_code(error, category)]
    if isinstance(error, IndexError):
        summary = "服务内部出现列表索引越界，请参考安全堆栈位置。"
    return status or 500, category, summary


def _safe_frames(exc: BaseException) -> list[dict[str, object]]:
    frames: list[dict[str, object]] = []
    tb = _unwrap(exc).__traceback__
    while tb is not None:
        filename = tb.tb_frame.f_code.co_filename.replace("\\", "/")
        marker = "/aistudio_api/"
        if marker in filename:
            candidate = "aistudio_api/" + filename.rsplit(marker, 1)[1]
            if re.fullmatch(r"aistudio_api/[A-Za-z0-9_./-]{1,180}\.py", candidate):
                frames.append({
                    "file": candidate,
                    "line": max(1, min(int(tb.tb_lineno), 10_000_000)),
                    "function": _safe_token(tb.tb_frame.f_code.co_name),
                })
        tb = tb.tb_next
    return frames[-8:]


def _normalize_record(raw: object) -> dict[str, Any] | None:
    """Only fixed, bounded metadata enters the recent diagnostic window."""
    if not isinstance(raw, dict):
        return None
    event_id = _safe_optional(raw.get("id"), _EVENT_ID)
    category = raw.get("category")
    status = _http_status(raw.get("status"))
    timestamp = raw.get("time")
    if not event_id or not isinstance(category, str) or category not in _CATEGORIES or status is None or not isinstance(timestamp, str):
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        timestamp = parsed.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError):
        return None
    frames = []
    raw_frames = raw.get("frames")
    for frame in (raw_frames if isinstance(raw_frames, list) else [])[:8]:
        if not isinstance(frame, dict):
            continue
        filename = frame.get("file")
        if not isinstance(filename, str) or not re.fullmatch(r"aistudio_api/[A-Za-z0-9_./-]{1,180}\.py", filename):
            continue
        line = frame.get("line")
        if type(line) is not int or not 1 <= line <= 10_000_000:
            continue
        frames.append({"file": filename, "line": line, "function": _safe_token(frame.get("function"))})
    exception_type = _safe_token(raw.get("exception_type"), "Exception")
    source = _safe_token(raw.get("source"))
    summary = _CATEGORIES[category]
    code = raw.get("code")
    code = code if isinstance(code, str) and code in _IDENTITY_DETAILS and category == "authentication" else category
    if code in _IDENTITY_DETAILS:
        summary = _IDENTITY_DETAILS[code]
    if exception_type == "IndexError":
        summary = "服务内部出现列表索引越界，请参考安全堆栈位置。"
    if source == "management_warning":
        category = "browser"
        code = category
        summary = "登录凭据已保存，但浏览器注入失败；实际调用时会重新尝试加载。"
    attempt = raw.get("attempt")
    return {
        "id": event_id, "time": timestamp, "status": status, "category": category, "code": code,
        "summary": summary, "exception_type": exception_type, "frames": frames,
        "request_id": _safe_optional(raw.get("request_id"), _REQUEST),
        "source": source,
        "operation": _safe_token(raw.get("operation")),
        "model": _safe_optional(raw.get("model"), _MODEL),
        "account_id": _safe_optional(raw.get("account_id"), _ACCOUNT),
        "attempt": attempt if type(attempt) is int and 0 <= attempt <= 1000 else None,
        "retrying": raw.get("retrying") is True,
        "outcome": raw.get("outcome") if isinstance(raw.get("outcome"), str) and raw.get("outcome") in {
            "retrying", "failed", "recovered", "cancelled", "warning"
        } else ("retrying" if raw.get("retrying") is True else "failed"),
        "transport_status": raw.get("transport_status") if type(raw.get("transport_status")) is int
        and 100 <= raw["transport_status"] <= 599 else None,
    }


class ErrorRecordStore:
    """Thread-safe bounded memory; a new service process always starts empty."""

    def __init__(self, *, max_records: int = MAX_RECORDS,
                 retention_minutes: int = RETENTION_MINUTES,
                 clock: Callable[[], datetime] | None = None):
        self.max_records = max(1, min(int(max_records), MAX_RECORDS))
        self.retention_minutes = max(1, min(int(retention_minutes), RETENTION_MINUTES))
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._records: deque[dict[str, Any]] = deque(maxlen=self.max_records)

    def _now(self) -> datetime:
        return self._clock().astimezone(UTC)

    def _prune(self) -> None:
        cutoff = self._now() - timedelta(minutes=self.retention_minutes)
        self._records = deque(
            (record for record in self._records
             if datetime.fromisoformat(record["time"].replace("Z", "+00:00")) >= cutoff),
            maxlen=self.max_records,
        )

    def record(self, exc: BaseException, *, source: str, operation: str,
               model: str | None = None, account_id: str | None = None,
               attempt: int | None = None, retrying: bool = False,
               http_status: int | None = None) -> dict[str, Any]:
        context = get_request_context()
        safe_attempt = attempt if type(attempt) is int and 0 <= attempt <= 1000 else None
        dedup_key = (id(exc), safe_attempt, bool(retrying))
        with self._lock:
            if context and dedup_key in context.recorded_event_keys:
                return dict(context.recorded_event_keys[dedup_key])
            status, category, _ = classify_failure(exc, http_status)
            if source == "management_warning":
                status, category = _http_status(http_status) or 503, "browser"
            error = _unwrap(exc)
            event = _normalize_record({
                "id": uuid.uuid4().hex,
                "time": self._now().isoformat(timespec="milliseconds"),
                "status": status, "category": category,
                "code": _failure_code(error, category),
                "exception_type": type(error).__name__, "frames": _safe_frames(error),
                "request_id": context.request_id if context else f"req_{uuid.uuid4().hex}",
                "source": source, "operation": operation, "model": model,
                "account_id": account_id, "attempt": safe_attempt,
                "retrying": bool(retrying),
                "transport_status": context.transport_status if context else None,
                "outcome": "warning" if source == "management_warning" else (
                    "retrying" if retrying else "cancelled" if category == "cancelled" else "failed"
                ),
            })
            assert event is not None
            self._records.append(event)
            self._prune()
            if context:
                context.recorded_event_keys[dedup_key] = event
                context.exception_ids.update((id(exc), id(error)))
                context._exception_references.extend((exc, error))
                context.warning = context.warning or source == "management_warning"
                if source != "management_warning":
                    context.final_failure = context.final_failure or not retrying
            return dict(event)

    def finish_request(self, request_id: str, outcome: str,
                       transport_status: int | None = None) -> None:
        if outcome not in {"failed", "recovered", "cancelled", "warning"}:
            return
        with self._lock:
            self._prune()
            for record in self._records:
                if record["request_id"] == request_id:
                    record["outcome"] = outcome
                    if type(transport_status) is int and 100 <= transport_status <= 599:
                        record["transport_status"] = transport_status

    def list_records(self, *, limit: int = 50, status: int | None = None,
                     category: str | None = None, cursor: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._prune()
            records = [dict(event) for event in reversed(self._records)
                       if (status is None or event["status"] == status)
                       and (category is None or event["category"] == category)]
            total = len(records)
            offset = 0
            if cursor:
                offset = next((index + 1 for index, item in enumerate(records)
                               if item["id"] == cursor), len(records))
            page = records[offset:offset + max(1, min(int(limit), 100))]
            has_more = offset + len(page) < total
            return {
                "items": page, "total": total, "has_more": has_more,
                "next_cursor": page[-1]["id"] if page and has_more else None,
                "retention": {"max_records": self.max_records, "minutes": self.retention_minutes},
            }


def get_error_record_store() -> ErrorRecordStore:
    global _store
    with _REGISTRY_LOCK:
        if _store is None:
            _store = ErrorRecordStore()
        return _store


def record_failure(exc: BaseException, *, source: str, operation: str,
                   model: str | None = None, account_id: str | None = None,
                   attempt: int | None = None, retrying: bool = False,
                   http_status: int | None = None) -> dict[str, Any]:
    """Record safe metadata without replacing the API's original error."""
    try:
        return get_error_record_store().record(
            exc, source=source, operation=operation, model=model, account_id=account_id,
            attempt=attempt, retrying=retrying, http_status=http_status,
        )
    except Exception:
        return {"recorded": False}


def finish_request(outcome: str, transport_status: int | None = None) -> None:
    context = get_request_context()
    if context is None:
        return
    if type(transport_status) is int and 100 <= transport_status <= 599:
        context.transport_status = transport_status
    with contextlib.suppress(Exception):
        get_error_record_store().finish_request(context.request_id, outcome, context.transport_status)
