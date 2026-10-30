"""Request-scoped error diagnostics, including HTTP-200 streaming failures."""
from __future__ import annotations

import asyncio
import time
from collections import deque

from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from aistudio_api.infrastructure.utils.error_records import (
    bind_request_context,
    finish_request,
    get_request_context,
    has_final_failure,
    has_recorded_failure,
    record_failure,
    reset_request_context,
)
from aistudio_api.infrastructure.utils.response_diagnostics import (
    finish_response_diagnostics,
    initialize_response_diagnostics,
    note_timing,
    observe_request_body_bytes,
)


def request_operation(scope: Scope) -> str | None:
    """Only return code-defined operation names, never raw paths or query data."""
    path = scope.get("path", "")
    if path.startswith("/v1beta/"):
        if path.endswith(":streamGenerateContent"):
            return "stream_generate_content"
        if path.endswith(":generateContent"):
            return "generate_content"
        if path == "/v1beta/models" or path.startswith("/v1beta/models/"):
            return "models"
    if path.startswith(("/api/accounts", "/accounts")):
        if path in ("/accounts", "/accounts/") and "text/html" in dict(scope.get("headers", [])).get(b"accept", b"").decode("latin-1"):
            return None
        if path.endswith("/activate"):
            return "account_activate"
        if "import" in path:
            return "account_import"
        return "accounts"
    for prefix, operation in (("/stats", "stats"), ("/rotation", "rotation"),
                              ("/config", "configuration"), ("/api-keys", "api_key_management"),
                              ("/browser/status", "browser_status"), ("/auth", "authentication")):
        if path == prefix or path.startswith(prefix + "/"):
            return operation
    return None


def record_http_failure(exc: BaseException, status: int) -> None:
    context = get_request_context()
    if context is not None and not has_final_failure():
        record_failure(exc, source="http", operation=getattr(context, "operation", "http_request"), http_status=status)


class ErrorRecordsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._guard_failures: deque[float] = deque(maxlen=20)

    def _allow_guard_record(self) -> bool:
        """Keep auth floods from filling the diagnostic history or disk queue."""
        now = time.monotonic()
        while self._guard_failures and self._guard_failures[0] <= now - 60:
            self._guard_failures.popleft()
        if len(self._guard_failures) >= 20:
            return False
        self._guard_failures.append(now)
        return True

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        operation = request_operation(scope) if scope["type"] == "http" else None
        # Production installs this outside its public auth guard. The inner
        # application copy must share the original context rather than rebind.
        if operation is None or get_request_context() is not None:
            await self.app(scope, receive, send)
            return
        token = bind_request_context()
        context = get_request_context()
        assert context is not None
        context.operation = operation
        if operation in {"generate_content", "stream_generate_content"}:
            initialize_response_diagnostics(stream=operation == "stream_generate_content")
        scope.setdefault("state", {})["error_record_context"] = context
        status = None
        completed = False
        disconnected = False
        cancelled = False
        raised = False

        async def observe_receive() -> Message:
            nonlocal disconnected
            message = await receive()
            if message["type"] == "http.request":
                observe_request_body_bytes(len(message.get("body", b"")))
            if message["type"] == "http.disconnect" and not completed:
                disconnected = True
            return message

        async def observe_send(message: Message) -> None:
            nonlocal status, completed, disconnected
            if message["type"] == "http.response.start":
                status = message["status"]
                context.transport_status = status
                message = dict(message)
                message["headers"] = [
                    (key, value) for key, value in message.get("headers", [])
                    if key.lower() != b"x-request-id"
                ] + [(b"x-request-id", context.request_id.encode("ascii"))]
            try:
                await send(message)
            except OSError:
                disconnected = True
                raise
            if message["type"] == "http.response.start":
                note_timing("response_started")
            elif message["type"] == "http.response.body" and message.get("body", b"").startswith(b"data:"):
                # Empty heartbeat separators do not mean the model emitted a
                # token. Measure successful ASGI sends, including backpressure.
                note_timing("first_data_sent")
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                completed = True

        try:
            await self.app(scope, observe_receive, observe_send)
        except asyncio.CancelledError as exc:
            cancelled = True
            if not has_final_failure():
                record_failure(exc, source="http", operation=operation, http_status=499)
            raise
        except BaseException as exc:
            raised = True
            if not has_final_failure():
                record_failure(exc, source="http", operation=operation,
                               http_status=499 if disconnected else 500)
            raise
        finally:
            try:
                # HTTP middleware can forward an empty final body after its
                # inner stream was cancelled. That body cannot undo a peer
                # disconnect already observed while the response was active.
                if cancelled or disconnected:
                    if not has_final_failure():
                        record_failure(asyncio.CancelledError(), source="http", operation=operation, http_status=499)
                    finish_request("cancelled", transport_status=status)
                elif raised or has_final_failure():
                    finish_request("failed", transport_status=status)
                elif status is not None and status >= 400:
                    if status not in (401, 429) or self._allow_guard_record():
                        record_failure(HTTPException(status), source="http_auth", operation=operation, http_status=status)
                        finish_request("failed", transport_status=status)
                elif has_recorded_failure():
                    finish_request("warning" if getattr(context, "warning", False) else "recovered", transport_status=status)
            finally:
                outcome = (
                    "cancelled" if cancelled or disconnected
                    else "failed" if raised or has_final_failure() or not completed
                    or (status is not None and status >= 400)
                    else "recovered" if has_recorded_failure() else "success"
                )
                finish_response_diagnostics(outcome, transport_status=status)
                reset_request_context(token)
