"""Bounded response-shape counters; never retain generated text or signatures."""

from __future__ import annotations

import contextlib
import json
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from aistudio_api.domain.models import Candidate
from aistudio_api.infrastructure.utils.error_records import (
    _ACCOUNT,
    get_request_context,
)
from aistudio_api.infrastructure.utils.logger import get_logger

MAX_RECORDS = 100
RETENTION_MINUTES = 10
MAX_SEQUENCE = 32
MAX_COUNT = 2_147_483_647
SLOW_FIRST_BODY_MS = 10_000
LARGE_INPUT_TOKENS = 64_000
_MODEL = re.compile(r"(?:models/)?(?:gemini|gemma)[a-z0-9_.-]{0,90}\Z")
_REQUEST = re.compile(r"req_[a-f0-9]{8,32}\Z")
_PHASES = frozenset({"body", "thought", "empty_thought", "finish"})
_FLAGS = frozenset({
    "marker_in_body", "thought_after_body", "empty_thought", "content_after_finish",
    "multiple_upstream_calls", "retried", "auto_continued", "sequence_limited",
    "slow_first_body",
    "large_input",
    "continuation_skipped_budget", "continuation_skipped_unsupported",
})
_OUTCOMES = frozenset({"success", "recovered", "failed", "cancelled"})
_SESSION_REASONS = frozenset({
    "warm_reuse", "service_start", "idle_recycle", "account_change", "credentials_change",
    "forced_refresh", "browser_recovery", "page_recovery", "session_not_ready",
})
_TRANSPORT_PHASES = frozenset({
    "fetch_pending", "read_pending", "awaiting_ack", "binding_missing",
    "page_unresponsive", "state_missing",
})
_ERROR_KINDS = frozenset({
    "session_expired", "authentication", "rate_limit", "timeout", "browser",
    "network", "validation", "upstream", "internal", "cancelled",
})
_COUNTERS = (
    "duration_ms", "upstream_calls", "retries", "body_chars", "thought_chars",
    "thought_parts", "thought_after_body", "empty_thought_parts", "marker_count",
    "request_body_bytes",
    "transport_probes", "transport_repair_attempts", "transport_wait_ms",
)
_TIMINGS = (
    "admitted", "account_preparation_started", "account_ready", "session_ready",
    "capture_started", "template_ready", "snapshot_ready", "upstream_started",
    "transport_dispatched", "upstream_headers", "upstream_first_byte",
    "first_thought", "first_body", "response_started", "first_data_sent",
    "browser_launch_started", "browser_ready", "browser_context_ready",
    "browser_cookies_loaded", "browser_navigation_ready", "botguard_hooks_ready",
    "botguard_prompt_filled", "botguard_run_dispatched", "botguard_template_ready",
    "session_checkpoint_done", "botguard_editor_ready", "temporary_chat_ready",
    "browser_request_started",
)
logger = get_logger("response_diagnostics")
_store: ResponseDiagnosticStore | None = None
_registry_lock = threading.RLock()


def _count(value: object) -> int:
    return max(0, min(value, MAX_COUNT)) if type(value) is int else 0


def _thinking_mode(value: object) -> int | None:
    # The current verified mapping defines mode 1 only. Other request values
    # remain untouched by this diagnostic path and are reported as unknown.
    return value if type(value) is int and value == 1 else None


def _thinking_level(value: object) -> int | None:
    return value if type(value) is int and value in (1, 2, 3, 4) else None


def _normalize(raw: object) -> dict[str, Any] | None:
    """Construct a fresh allowlisted object, also at the logging boundary."""
    if not isinstance(raw, dict):
        return None
    request_id = raw.get("request_id")
    if not isinstance(request_id, str) or not _REQUEST.fullmatch(request_id):
        return None
    try:
        stamp = datetime.fromisoformat(str(raw.get("time", "")).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return None
        timestamp = stamp.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError):
        return None
    model = raw.get("model")
    result: dict[str, Any] = {
        "request_id": request_id,
        "account_ids": list(dict.fromkeys(
            value for value in raw.get("account_ids", [])[:32]
            if isinstance(value, str) and _ACCOUNT.fullmatch(value)
        )) if isinstance(raw.get("account_ids"), list) else [],
        "time": timestamp,
        "model": model if isinstance(model, str) and _MODEL.fullmatch(model) else None,
        "stream": raw.get("stream") is True,
        "auto_continue": raw.get("auto_continue") is True,
        "thinking_explicit": raw.get("thinking_explicit")
        if type(raw.get("thinking_explicit")) is bool else None,
        "thinking_mode": _thinking_mode(raw.get("thinking_mode")),
        "thinking_level": _thinking_level(raw.get("thinking_level")),
        "thought_tokens": _count(raw.get("thought_tokens"))
        if type(raw.get("thought_tokens")) is int and raw["thought_tokens"] >= 0 else None,
        "prompt_tokens": _count(raw.get("prompt_tokens"))
        if type(raw.get("prompt_tokens")) is int and raw["prompt_tokens"] >= 0 else None,
        "session_preparation_reason": raw.get("session_preparation_reason")
        if isinstance(raw.get("session_preparation_reason"), str) and raw["session_preparation_reason"] in _SESSION_REASONS else None,
        "browser_idle_timeout_s": _count(raw.get("browser_idle_timeout_s"))
        if type(raw.get("browser_idle_timeout_s")) is int and raw["browser_idle_timeout_s"] >= 0 else None,
        "transport_wait_phase": raw.get("transport_wait_phase")
        if isinstance(raw.get("transport_wait_phase"), str)
        and raw["transport_wait_phase"] in _TRANSPORT_PHASES else None,
        "outcome": raw.get("outcome")
        if isinstance(raw.get("outcome"), str) and raw["outcome"] in _OUTCOMES else "failed",
        "transport_status": raw.get("transport_status")
        if type(raw.get("transport_status")) is int and 100 <= raw["transport_status"] <= 599 else None,
        "error_kind": raw.get("error_kind")
        if isinstance(raw.get("error_kind"), str) and raw["error_kind"] in _ERROR_KINDS else None,
    }
    result.update({name: _count(raw.get(name)) for name in _COUNTERS})
    timings = raw.get("timings_ms")
    result["timings_ms"] = {
        name: timings[name] for name in _TIMINGS
        if type(timings.get(name)) is int and 0 <= timings[name] <= MAX_COUNT
    } if isinstance(timings, dict) else {}
    reasons = raw.get("finish_reasons")
    result["finish_reasons"] = [
        value for value in reasons[:MAX_SEQUENCE] if type(value) is int and 0 <= value <= 255
    ] if isinstance(reasons, list) else []
    phases = raw.get("phase_sequence")
    result["phase_sequence"] = [
        value for value in phases[:MAX_SEQUENCE] if isinstance(value, str) and value in _PHASES
    ] if isinstance(phases, list) else []
    flags = raw.get("flags")
    result["flags"] = sorted({
        value for value in flags[:len(_FLAGS)] if isinstance(value, str) and value in _FLAGS
    }) if isinstance(flags, list) else []
    return result


class ResponseDiagnosticStore:
    """Independent short memory window; every service process starts empty."""

    def __init__(self, *, max_records: int = MAX_RECORDS,
                 retention_minutes: int = RETENTION_MINUTES,
                 clock: Callable[[], datetime] | None = None):
        self.max_records = max(1, min(int(max_records), MAX_RECORDS))
        self.retention_minutes = max(1, min(int(retention_minutes), RETENTION_MINUTES))
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._records: deque[dict[str, Any]] = deque(maxlen=self.max_records)

    def _prune(self) -> None:
        cutoff = self._clock().astimezone(UTC) - timedelta(minutes=self.retention_minutes)
        self._records = deque((item for item in self._records
                               if datetime.fromisoformat(item["time"].replace("Z", "+00:00")) >= cutoff),
                              maxlen=self.max_records)

    def record(self, summary: dict[str, Any]) -> None:
        safe = _normalize(summary)
        if safe is None:
            return
        with self._lock:
            self._records.append(safe)
            self._prune()

    def list_records(self, *, limit: int = 50, account_id: str | None = None,
                     request_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._prune()
            # JSON round-trip copies the bounded nested arrays as well.
            items = [item for item in reversed(self._records)
                     if (account_id is None or account_id in item["account_ids"])
                     and (request_id is None or item["request_id"] == request_id)
                     ][:max(1, min(int(limit), MAX_RECORDS))]
            return {"items": json.loads(json.dumps(items)),
                    "retention": {"minutes": self.retention_minutes, "max_records": self.max_records}}


def get_response_diagnostic_store() -> ResponseDiagnosticStore:
    global _store
    with _registry_lock:
        if _store is None:
            _store = ResponseDiagnosticStore()
        return _store


@dataclass
class _Accumulator:
    stream: bool
    configured: bool = False
    model: str | None = None
    auto_continue: bool = False
    thinking_explicit: bool | None = None
    thinking_mode: int | None = None
    thinking_level: int | None = None
    upstream_calls: int = 0
    retries: int = 0
    body_chars: int = 0
    thought_chars: int = 0
    thought_parts: int = 0
    thought_tokens: int | None = None
    prompt_tokens: int | None = None
    session_preparation_reason: str | None = None
    browser_idle_timeout_s: int | None = None
    prompt_tokens_call: int | None = None
    request_body_bytes: int = 0
    transport_probes: int = 0
    transport_repair_attempts: int = 0
    transport_wait_ms: int = 0
    transport_wait_phase: str | None = None
    thought_after_body: int = 0
    empty_thought_parts: int = 0
    marker_count: int = 0
    finish_reasons: list[int] = field(default_factory=list)
    phase_sequence: list[str] = field(default_factory=list)
    flags: set[str] = field(default_factory=set)
    timings_ms: dict[str, int] = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)
    completed: bool = False
    # Only finite state and counters survive observing a Part; no text tail.
    marker_state: int = 0
    body_seen: bool = False
    finished: bool = False
    last_phase: str | None = None
    previous_thought_tokens: int = 0

    def phase(self, value: str) -> None:
        if self.last_phase == value:
            return
        self.last_phase = value
        if len(self.phase_sequence) < MAX_SEQUENCE:
            self.phase_sequence.append(value)
        else:
            self.flags.add("sequence_limited")

    def observe_body(self, text: str) -> None:
        for character in text:
            state = self.marker_state
            if state == 0:
                self.marker_state = 1 if character == "[" else 0
            elif state == 1:
                self.marker_state = 2 if character == ":" else 1 if character == "[" else 0
            elif state == 2:
                self.marker_state = 3 if "0" <= character <= "9" else 1 if character == "[" else 0
            elif "0" <= character <= "9":
                self.marker_state = 3
            else:
                if character == "]":
                    self.marker_count = _count(self.marker_count + 1)
                    self.flags.add("marker_in_body")
                self.marker_state = 1 if character == "[" else 0


def _current() -> _Accumulator | None:
    context = get_request_context()
    value = getattr(context, "response_diagnostics", None)
    return value if isinstance(value, _Accumulator) and not value.completed else None


def initialize_response_diagnostics(*, stream: bool) -> None:
    with contextlib.suppress(Exception):
        context = get_request_context()
        if context is not None and getattr(context, "response_diagnostics", None) is None:
            context.response_diagnostics = _Accumulator(stream=stream)


def configure_generation(*, model: str, auto_continue: bool) -> None:
    with contextlib.suppress(Exception):
        current = _current()
        if current is not None:
            current.configured = True
            current.model = model if isinstance(model, str) and _MODEL.fullmatch(model) else None
            current.auto_continue = auto_continue is True


def note_thinking_configuration(*, explicit: bool, thinking_config: object) -> None:
    """Inspect normalized overrides without retaining the request or its array."""
    with contextlib.suppress(Exception):
        current = _current()
        if current is not None:
            current.thinking_explicit = explicit if type(explicit) is bool else None
            current.thinking_mode = _thinking_mode(
                thinking_config[0] if isinstance(thinking_config, list) and thinking_config else None
            )
            current.thinking_level = _thinking_level(
                thinking_config[3]
                if isinstance(thinking_config, list) and len(thinking_config) > 3 else None
            )


def observe_request_body_bytes(size: int) -> None:
    """Count bytes already consumed by ASGI without keeping or rereading them."""
    with contextlib.suppress(Exception):
        current = _current()
        if current is not None and type(size) is int and size >= 0:
            current.request_body_bytes = _count(current.request_body_bytes + size)


def observe_prompt_tokens(prompt_tokens: int | None) -> None:
    """Keep the final prompt report of the first call that supplies usage.

    This represents input size, not billable aggregate usage across retries
    and continuation. Missing usage stays unknown rather than becoming zero.
    """
    with contextlib.suppress(Exception):
        current = _current()
        if (
            current is not None and type(prompt_tokens) is int and prompt_tokens >= 0
            and (current.prompt_tokens_call is None or current.prompt_tokens_call == current.upstream_calls)
        ):
            current.prompt_tokens_call = current.upstream_calls
            current.prompt_tokens = _count(prompt_tokens)


def observe_reasoning_tokens(reasoning_tokens: int | None) -> None:
    """Keep the latest cumulative usage in a call, summed across upstream calls."""
    with contextlib.suppress(Exception):
        current = _current()
        if current is not None and type(reasoning_tokens) is int and reasoning_tokens >= 0:
            current.thought_tokens = _count(
                current.previous_thought_tokens + _count(reasoning_tokens)
            )


def note_attempt(attempt: int) -> None:
    with contextlib.suppress(Exception):
        current = _current()
        if current is not None:
            current.retries = max(current.retries, _count(attempt))


def note_session_preparation(reason: str, idle_timeout_s: int) -> None:
    """Keep the first actual preparation decision, not later same-request reuse."""
    with contextlib.suppress(Exception):
        current = _current()
        if current is not None and current.session_preparation_reason is None and reason in _SESSION_REASONS:
            current.session_preparation_reason = reason
            current.browser_idle_timeout_s = _count(idle_timeout_s)


def note_timing(name: str) -> None:
    """First observed milestone, measured from ingress; never keep its payload.

    Retries and continuation must not overwrite the original first-byte/body
    timing. Missing milestones are omitted, distinct from a genuine zero.
    """
    with contextlib.suppress(Exception):
        current = _current()
        if current is not None and name in _TIMINGS and name not in current.timings_ms:
            current.timings_ms[name] = _count(int(max(0, time.monotonic() - current.started) * 1000))


def note_transport_wait(phase: str, elapsed_ms: int, *, repair_attempted: bool = False) -> None:
    """Retain fixed transport stages and counts, never a browser event payload."""
    with contextlib.suppress(Exception):
        current = _current()
        if current is not None and phase in _TRANSPORT_PHASES:
            current.transport_wait_phase = phase
            current.transport_wait_ms = _count(elapsed_ms)
            current.transport_probes = _count(current.transport_probes + 1)
            current.transport_repair_attempts = _count(current.transport_repair_attempts + int(repair_attempted))


def begin_upstream_call() -> None:
    with contextlib.suppress(Exception):
        current = _current()
        if current is not None:
            note_timing("upstream_started")
            current.upstream_calls = _count(current.upstream_calls + 1)
            current.previous_thought_tokens = current.thought_tokens or 0
            current.marker_state = 0
            current.body_seen = current.finished = False
            current.last_phase = None


def note_flag(name: str) -> None:
    """Only fixed diagnostic reasons may enter a request summary."""
    with contextlib.suppress(Exception):
        current = _current()
        if current is not None and name in _FLAGS:
            current.flags.add(name)


def note_continuation() -> None:
    with contextlib.suppress(Exception):
        current = _current()
        if current is not None:
            current.flags.add("auto_continued")


def observe_candidate(candidate: Candidate) -> None:
    with contextlib.suppress(Exception):
        current = _current()
        if current is None:
            return
        for part in candidate.parts:
            text = part.text if isinstance(part.text, str) else ""
            has_nontext_payload = any(value is not None for value in (
                part.image, part.function_call, part.function_response,
                part.executable_code, part.code_execution_result,
            ))
            if current.finished and (text or has_nontext_payload):
                current.flags.add("content_after_finish")
            if part.thought:
                current.thought_parts = _count(current.thought_parts + 1)
                if text:
                    note_timing("first_thought")
                    current.thought_chars = _count(current.thought_chars + len(text))
                    if current.body_seen:
                        current.thought_after_body = _count(current.thought_after_body + 1)
                        current.flags.add("thought_after_body")
                    current.phase("thought")
                elif not has_nontext_payload:
                    current.empty_thought_parts = _count(current.empty_thought_parts + 1)
                    current.flags.add("empty_thought")
                    current.phase("empty_thought")
                else:
                    current.phase("thought")
            elif text:
                note_timing("first_body")
                current.body_chars = _count(current.body_chars + len(text))
                current.body_seen = True
                current.observe_body(text)
                current.phase("body")
        reason = candidate.finish_reason
        if type(reason) is int and 0 < reason <= 255:
            if len(current.finish_reasons) < MAX_SEQUENCE:
                current.finish_reasons.append(reason)
            else:
                current.flags.add("sequence_limited")
            current.finished = True
            current.phase("finish")


def finish_response_diagnostics(outcome: str, transport_status: int | None = None) -> None:
    """Called by the owning middleware task, including on disconnect/failure."""
    with contextlib.suppress(Exception):
        current = _current()
        context = get_request_context()
        if current is None or context is None:
            return
        current.completed = True
        # Public auth/schema rejections already have rate-limited error
        # diagnostics. Only a request admitted to the generation handler gets
        # a response-shape summary or service-log line.
        if not current.configured:
            return
        if current.upstream_calls > 1:
            current.flags.add("multiple_upstream_calls")
        if current.retries:
            current.flags.add("retried")
        duration_ms = _count(int(max(0, time.monotonic() - current.started) * 1000))
        if current.timings_ms.get("first_body", duration_ms) >= SLOW_FIRST_BODY_MS:
            current.flags.add("slow_first_body")
        if current.prompt_tokens is not None and current.prompt_tokens >= LARGE_INPUT_TOKENS:
            current.flags.add("large_input")
        error_kind = "cancelled" if outcome == "cancelled" else None
        for event in context.recorded_event_keys.values():
            if event.get("category") in _ERROR_KINDS:
                error_kind = event["category"]
        summary = _normalize({
            "request_id": context.request_id, "time": datetime.now(UTC).isoformat(),
            "account_ids": context.account_ids,
            "model": current.model, "stream": current.stream,
            "auto_continue": current.auto_continue, "outcome": outcome,
            "thinking_explicit": current.thinking_explicit,
            "thinking_mode": current.thinking_mode, "thinking_level": current.thinking_level,
            "thought_tokens": current.thought_tokens,
            "prompt_tokens": current.prompt_tokens,
            "session_preparation_reason": current.session_preparation_reason,
            "browser_idle_timeout_s": current.browser_idle_timeout_s,
            "transport_wait_phase": current.transport_wait_phase,
            "transport_status": transport_status, "error_kind": error_kind,
            "duration_ms": duration_ms, "timings_ms": current.timings_ms,
            **{key: getattr(current, key) for key in _COUNTERS if key != "duration_ms"},
            "finish_reasons": current.finish_reasons, "phase_sequence": current.phase_sequence,
            "flags": sorted(current.flags),
        })
        if summary is not None:
            # Keep the same small allowlist in the existing service log so a
            # later investigation need not depend on the live ten-minute view.
            with contextlib.suppress(Exception):
                get_response_diagnostic_store().record(summary)
            with contextlib.suppress(Exception):
                log = logger.warning if summary["outcome"] in {"failed", "cancelled"} or current.flags.intersection({
                    "marker_in_body", "thought_after_body", "content_after_finish", "slow_first_body", "large_input",
                    "continuation_skipped_budget", "continuation_skipped_unsupported",
                }) else logger.info
                log("response_diagnostic %s", json.dumps(summary, separators=(",", ":")))
