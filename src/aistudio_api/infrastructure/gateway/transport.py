"""XHR replay and streaming event collection transport layer."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import TYPE_CHECKING

from anyio import CancelScope

from aistudio_api.domain.errors import BrowserTransportError
from aistudio_api.infrastructure.account.cookie_parser import (
    calculate_sapisid_hash,
    select_signing_cookies,
)
from aistudio_api.infrastructure.browser.scripts import (
    HOOKED_CLEANUP_JS,
    HOOKED_REQUEST_JS,
    STREAM_ACK_JS,
    STREAM_CLEANUP_JS,
    STREAM_RESEND_JS,
    STREAM_STATE_JS,
    STREAMING_INIT_JS,
    build_hooked_request_args,
    build_streaming_init_args,
)

if TYPE_CHECKING:
    from aistudio_api.infrastructure.browser.cdp_client import CDPPage

from aistudio_api.infrastructure.utils.logger import get_logger
from aistudio_api.infrastructure.utils.response_diagnostics import (
    note_timing,
    note_transport_wait,
)

logger = get_logger("transport")

# Response and buffering budgets for the existing 1 GB deployment. Crossing a
# limit aborts the request; never drop chunks and return a corrupted success.
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_READ_BYTES = 1024 * 1024
MAX_CHUNK_BYTES = 64 * 1024
MAX_STREAM_BUFFER_BYTES = 512 * 1024
MAX_TOTAL_BUFFER_BYTES = 8 * 1024 * 1024
MAX_STREAM_EVENTS = 32
CONSUMER_TIMEOUT_S = 30.0
CLEANUP_TIMEOUT_S = 2.0
STREAM_PROBE_INTERVAL_S = 20.0
STREAM_PROBE_TIMEOUT_S = 2.0
STREAM_RECOVERY_BINDING = "__aistudio_stream_resume__"


def _signing_cookies(page: CDPPage | None, cookies: list[dict[str, object]]) -> dict[str, str]:
    return select_signing_cookies(cookies, page_url=str(getattr(page, "url", "") or "/"))


async def _ensure_authorization_header(
    page: CDPPage, headers: dict[str, str], auth_user: str = "0"
) -> dict[str, str]:
    """Ensure fresh SAPISIDHASH Authorization and X-Goog-AuthUser header are populated in Python."""
    incoming_auth = ""
    for k, v in headers.items():
        if k.lower() == "authorization" and v:
            incoming_auth = str(v)
            break

    clean_headers = {
        k: v
        for k, v in headers.items()
        if k.lower()
        not in ("host", "content-length", "authorization", "x-goog-authuser")
    }
    clean_headers["X-Goog-AuthUser"] = str(auth_user or "0")

    # Google can rotate the actual jar while the page stays alive. A freshly
    # timestamped hash of an indefinitely cached SAPISID still authenticates
    # with an obsolete credential. Read this context before every replay.
    cookie_dict = None
    with suppress(Exception):
        current = await page.get_cookies()
        if isinstance(current, list):
            cookie_dict = _signing_cookies(page, current)
    # Never fall back to the globally active account: this page may be retiring.
    fresh_auth = calculate_sapisid_hash(cookie_dict) if cookie_dict else ""
    if fresh_auth:
        clean_headers["Authorization"] = fresh_auth
    elif incoming_auth:
        clean_headers["Authorization"] = incoming_auth

    return clean_headers


class XHRStreamTransport:
    """Handles in-browser XHR request execution, streaming event collection, and resource cleanup."""

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue[dict[str, object]]] = {}
        self._queue_bytes: dict[str, int] = {}
        self._buffered_bytes = 0
        self._pages: dict[str, CDPPage] = {}
        self._failed: set[str] = set()
        self._received_seq: dict[str, int] = {}
        self._cleanup_tasks: set[asyncio.Task[None]] = set()

    async def _cleanup_browser(self, page: CDPPage, script: str, rid: str) -> None:
        with suppress(Exception):
            await asyncio.wait_for(
                page.evaluate(script, rid, timeout_s=CLEANUP_TIMEOUT_S),
                timeout=CLEANUP_TIMEOUT_S,
            )

    def _schedule_cleanup(
        self, page: CDPPage, script: str, rid: str
    ) -> asyncio.Task[None]:
        task = asyncio.create_task(self._cleanup_browser(page, script, rid))
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)
        return task

    def _clear_buffer(self, rid: str) -> None:
        self._buffered_bytes -= self._queue_bytes.pop(rid, 0)
        queue = self._queues.get(rid)
        if queue is not None:
            while not queue.empty():
                queue.get_nowait()

    def _fail_stream(self, rid: str, reason: str) -> bool:
        queue = self._queues.get(rid)
        if queue is None:
            return False
        if rid in self._failed:
            return True
        self._failed.add(rid)
        self._clear_buffer(rid)
        queue.put_nowait({"type": "error", "message": reason})
        page = self._pages.get(rid)
        if page is not None:
            self._schedule_cleanup(page, STREAM_CLEANUP_JS, rid)
        return True

    @property
    def in_flight_count(self) -> int:
        """Return number of currently active streaming requests."""
        return len(self._queues)

    def abort_stream(self, rid: str, reason: str = "stream_aborted") -> bool:
        """Abort a specific streaming request by rid if it is active."""
        return self._fail_stream(rid, reason)

    def close(self, reason: str = "transport_closed") -> None:
        """Close transport and notify any remaining in-flight queues on process exit."""
        for rid in list(self._queues):
            self._fail_stream(rid, reason)

    def _ensure_page_binding(self, page: CDPPage) -> None:
        if getattr(page, "has_stream_binding", False):
            return
        page.has_stream_binding = True

        def on_stream_push(payload_str: str) -> None:
            try:
                data = json.loads(payload_str)
                if isinstance(data, dict):
                    rid = str(data.get("rid") or "")
                    q = self._queues.get(rid)
                    if q is not None and rid not in self._failed:
                        seq = data.get("seq")
                        if type(seq) is int and seq > 0:
                            previous = self._received_seq.get(rid, 0)
                            # Recovery can re-send the one unacknowledged
                            # browser event. Never deliver its text twice.
                            if seq <= previous:
                                return
                            if seq != previous + 1:
                                self._fail_stream(rid, "stream event sequence invalid")
                                return
                        text = data.get("text")
                        size = len(text.encode("utf-8")) if isinstance(text, str) else 0
                        # A UTF-8 decoder can carry at most three previous bytes
                        # into the next 64 KiB raw chunk.
                        if (
                            size > MAX_CHUNK_BYTES + 4
                            or self._queue_bytes.get(rid, 0) + size
                            > MAX_STREAM_BUFFER_BYTES
                            or self._buffered_bytes + size > MAX_TOTAL_BUFFER_BYTES
                        ):
                            self._fail_stream(rid, "stream buffer limit exceeded")
                            return
                        try:
                            q.put_nowait(data)
                            if type(seq) is int and seq > 0:
                                self._received_seq[rid] = seq
                            self._queue_bytes[rid] = (
                                self._queue_bytes.get(rid, 0) + size
                            )
                            self._buffered_bytes += size
                        except asyncio.QueueFull:
                            self._fail_stream(rid, "stream buffer limit exceeded")
            except Exception as e:
                logger.debug("分发流式推送数据失败: %s", e)

        if hasattr(page, "on_binding"):
            page.on_binding("__aistudio_stream_push__", on_stream_push)
            page.on_binding(STREAM_RECOVERY_BINDING, on_stream_push)
        elif hasattr(page, "cdp") and hasattr(page.cdp, "on"):

            def listener(params: dict[str, object]) -> None:
                if params.get("name") in {"__aistudio_stream_push__", STREAM_RECOVERY_BINDING}:
                    on_stream_push(str(params.get("payload") or ""))

            page.cdp.on("Runtime.bindingCalled", listener)

    async def _probe_silent_stream(
        self, page: CDPPage, rid: str, *, idle_s: float, remaining_s: float,
        allow_repair: bool,
    ) -> tuple[dict[str, object] | None, bool]:
        """Inspect counters; restore at most one lost push without another fetch."""
        queue = self._queues[rid]
        phase = "state_missing"
        repaired = False
        waiting_for_event = False
        try:
            async with asyncio.timeout(min(remaining_s, STREAM_PROBE_TIMEOUT_S)):
                state = await page.evaluate(STREAM_STATE_JS, rid, timeout_s=STREAM_PROBE_TIMEOUT_S)
                if not queue.empty():
                    return queue.get_nowait(), False
                if not isinstance(state, dict):
                    raise BrowserTransportError("CDP stream state disappeared")
                raw_phase = state.get("phase")
                if isinstance(raw_phase, str) and raw_phase in {"fetch_pending", "read_pending", "awaiting_ack"}:
                    phase = raw_phase
                binding_missing = state.get("bindingReady") is False
                if binding_missing:
                    phase = "binding_missing"
                if phase not in {"awaiting_ack", "binding_missing"}:
                    return None, False
                seq = state.get("seq")
                if not allow_repair:
                    raise BrowserTransportError("CDP stream event delivery stalled")
                # Use a separate binding without detaching the shared channel:
                # another request can still be using this same page.
                if hasattr(page, "add_binding"):
                    await page.add_binding(STREAM_RECOVERY_BINDING)
                else:
                    await page.cdp.send("Runtime.addBinding", {"name": STREAM_RECOVERY_BINDING})
                repaired = True
                if not queue.empty():
                    return queue.get_nowait(), True
                if type(seq) is not int or seq < 0:
                    raise BrowserTransportError("CDP stream event delivery stalled")
                if raw_phase == "awaiting_ack" and seq <= self._received_seq.get(rid, 0):
                    raise BrowserTransportError("CDP stream event delivery stalled")
                resent = await page.evaluate(
                    STREAM_RESEND_JS, {"rid": rid, "seq": seq}, timeout_s=STREAM_PROBE_TIMEOUT_S,
                )
                if resent is not True:
                    raise BrowserTransportError("CDP stream event delivery stalled")
                if raw_phase != "awaiting_ack":
                    return None, True
                waiting_for_event = True
                event = await queue.get()
                return event, True
        except TimeoutError:
            if not queue.empty():
                return queue.get_nowait(), repaired
            if remaining_s <= STREAM_PROBE_TIMEOUT_S:
                raise TimeoutError("streaming response timed out before completion") from None
            if waiting_for_event:
                raise BrowserTransportError("CDP stream event delivery stalled") from None
            phase = "page_unresponsive"
            raise BrowserTransportError("CDP stream state probe timed out") from None
        except BrowserTransportError:
            raise
        except Exception as exc:
            # A probe/registration/resend failure belongs to the already
            # dispatched stream. Do not let native CDP errors enter the broad
            # session-hang policy and dispatch a new model request.
            raise BrowserTransportError("CDP stream recovery failed") from exc
        finally:
            note_transport_wait(phase, int(idle_s * 1000), repair_attempted=repaired)

    async def send_hooked_request(
        self,
        page: CDPPage,
        *,
        url: str,
        headers: dict[str, str],
        body: str,
        timeout_ms: int,
        auth_user: str = "0",
    ) -> tuple[int, bytes]:
        """Replay request via XHR inside the browser page context."""
        timeout_s = max(timeout_ms / 1000, 0.001)
        deadline = asyncio.get_running_loop().time() + timeout_s
        async with asyncio.timeout_at(deadline):
            clean_headers = await _ensure_authorization_header(
                page, headers, auth_user=auth_user
            )
        remaining = max(deadline - asyncio.get_running_loop().time(), 0.001)
        rid = uuid.uuid4().hex
        args = build_hooked_request_args(
            url=url,
            headers=clean_headers,
            body=body,
            timeout_s=remaining,
            rid=rid,
            max_response_bytes=MAX_RESPONSE_BYTES,
            max_read_bytes=MAX_READ_BYTES,
        )

        try:
            result = await asyncio.wait_for(
                page.evaluate(HOOKED_REQUEST_JS, args, timeout_s=remaining + 1.0),
                timeout=remaining + 1.0,
            )
            res_dict: dict[str, object] = result if isinstance(result, dict) else {}
            status = int(str(res_dict.get("status") or 0))
            raw_text = str(res_dict.get("body") or "")
            if status == 0:
                safe_reason = (
                    raw_text
                    if raw_text
                    in {
                        "timeout",
                        "cancelled",
                        "response read failed",
                        "response size limit exceeded",
                    }
                    else "invalid browser response"
                )
                if safe_reason == "timeout":
                    raise TimeoutError("replay failed: timeout")
                raise RuntimeError(f"replay failed: {safe_reason}")
            raw_body = raw_text.encode("utf-8")
            if len(raw_body) > MAX_RESPONSE_BYTES:
                raise RuntimeError("replay failed: response size limit exceeded")
            return status, raw_body
        finally:
            with CancelScope(shield=True):
                await asyncio.shield(self._schedule_cleanup(page, HOOKED_CLEANUP_JS, rid))

    async def send_streaming_request(
        self,
        page: CDPPage,
        *,
        url: str,
        headers: dict[str, str],
        body: str,
        timeout_ms: int,
        auth_user: str = "0",
    ) -> AsyncGenerator[tuple[str, object], None]:
        """Send a streaming request, yielding ('status', int) and ('chunk', bytes) events.

        Guarantees V8 window cleanup on termination/timeout and prevents silent truncation.
        """
        timeout_s = max(timeout_ms / 1000, 0.001)
        deadline = asyncio.get_running_loop().time() + timeout_s
        # Cookie refresh, binding registration and request initialization share
        # the first-response budget. A stalled CDP command must not bypass it.
        async with asyncio.timeout_at(deadline):
            clean_headers = await _ensure_authorization_header(
                page, headers, auth_user=auth_user
            )
            self._ensure_page_binding(page)
            if hasattr(page, "add_binding"):
                await page.add_binding("__aistudio_stream_push__")
            elif hasattr(page, "cdp") and hasattr(page.cdp, "send"):
                await page.cdp.send(
                    "Runtime.addBinding", {"name": "__aistudio_stream_push__"}
                )
        rid = uuid.uuid4().hex

        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(
            maxsize=MAX_STREAM_EVENTS
        )
        self._queues[rid] = queue
        self._queue_bytes[rid] = 0
        self._pages[rid] = page
        self._received_seq[rid] = 0

        init_args = build_streaming_init_args(
            url=url,
            headers=clean_headers,
            body=body,
            timeout_s=timeout_s,
            rid=rid,
            max_response_bytes=MAX_RESPONSE_BYTES,
            max_chunk_bytes=MAX_CHUNK_BYTES,
            max_read_bytes=MAX_READ_BYTES,
            consumer_timeout_s=min(timeout_s, CONSUMER_TIMEOUT_S),
        )
        status_sent = False
        is_terminal = False
        last_progress_at = asyncio.get_running_loop().time()
        last_probe_at = last_progress_at
        repaired_stream = False

        try:
            async with asyncio.timeout_at(deadline):
                note_timing("transport_dispatched")
                receipt = await page.evaluate(
                    STREAMING_INIT_JS,
                    init_args,
                    timeout_s=max(deadline - asyncio.get_running_loop().time(), 0.001),
                )
                if (isinstance(receipt, dict) and receipt.get("started") is False
                        and receipt.get("reason") == "binding_unavailable"):
                    # The browser confirmed it has not called fetch. Its global
                    # binding can disappear while CDPPage's registration cache
                    # still says installed. Add a separate native channel; never
                    # detach a shared channel used by another active stream.
                    if hasattr(page, "add_binding"):
                        await page.add_binding(STREAM_RECOVERY_BINDING)
                    else:
                        await page.cdp.send("Runtime.addBinding", {"name": STREAM_RECOVERY_BINDING})
                    note_transport_wait("binding_missing", 0, repair_attempted=True)
                    receipt = await page.evaluate(
                        STREAMING_INIT_JS, init_args,
                        timeout_s=max(deadline - asyncio.get_running_loop().time(), 0.001),
                    )
                    if not isinstance(receipt, dict) or receipt.get("started") is not True:
                        raise BrowserTransportError("CDP stream binding unavailable before dispatch")
                if isinstance(receipt, dict) and receipt.get("started") is True:
                    note_timing("browser_request_started")
            last_progress_at = asyncio.get_running_loop().time()
            while not is_terminal:
                if page.is_closed() is True:
                    raise RuntimeError("Browser page closed during streaming")
                now = asyncio.get_running_loop().time()
                remaining = deadline - now
                if remaining <= 0:
                    break

                step_timeout = min(remaining, 1.0)
                next_probe_in = STREAM_PROBE_INTERVAL_S - (now - max(last_progress_at, last_probe_at))
                step_timeout = min(step_timeout, max(0.001, next_probe_in))
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=step_timeout)
                except TimeoutError:
                    if page.is_closed() is True:
                        raise RuntimeError(
                            "Browser page closed during streaming"
                        ) from None
                    now = asyncio.get_running_loop().time()
                    if now >= deadline:
                        break
                    if now - max(last_progress_at, last_probe_at) < STREAM_PROBE_INTERVAL_S:
                        continue
                    last_probe_at = now
                    event, repaired = await self._probe_silent_stream(
                        page, rid, idle_s=now - last_progress_at,
                        remaining_s=max(deadline - now, 0.001), allow_repair=not repaired_stream,
                    )
                    repaired_stream = repaired_stream or repaired
                    if event is None:
                        continue
                etype = str(event.get("type") or "")
                event_text = event.get("text")
                event_size = (
                    len(event_text.encode("utf-8"))
                    if isinstance(event_text, str)
                    else 0
                )
                self._queue_bytes[rid] = self._queue_bytes.get(rid, 0) - event_size
                self._buffered_bytes -= event_size
                if etype == "status":
                    status = int(str(event.get("status") or 0))
                    yield ("status", status)
                    status_sent = True
                    deadline = asyncio.get_running_loop().time() + timeout_s
                elif etype == "chunk":
                    text = str(event.get("text") or "")
                    if text:
                        yield ("chunk", text.encode("utf-8"))
                        deadline = asyncio.get_running_loop().time() + timeout_s
                elif etype == "error":
                    message = str(event.get("message") or "unknown error")
                    if message in {"timeout", "slow consumer timeout"}:
                        raise TimeoutError(f"streaming request failed: {message}")
                    raise RuntimeError(f"streaming request failed: {message}")
                elif etype == "aborted":
                    raise RuntimeError("streaming request failed: cancelled")
                elif etype == "done":
                    is_terminal = True
                seq = event.get("seq")
                if isinstance(seq, int) and seq > 0:
                    await asyncio.wait_for(
                        page.evaluate(
                            STREAM_ACK_JS,
                            {"rid": rid, "seq": seq},
                            timeout_s=CLEANUP_TIMEOUT_S,
                        ),
                        timeout=CLEANUP_TIMEOUT_S,
                    )
                last_progress_at = asyncio.get_running_loop().time()
            if not status_sent:
                raise TimeoutError("streaming request timeout: no response status")
            if not is_terminal:
                raise TimeoutError("streaming response timed out before completion")
        finally:
            self._clear_buffer(rid)
            self._queues.pop(rid, None)
            self._pages.pop(rid, None)
            self._failed.discard(rid)
            self._received_seq.pop(rid, None)
            with CancelScope(shield=True):
                await asyncio.shield(self._schedule_cleanup(page, STREAM_CLEANUP_JS, rid))
