"""Bounded, content-free request observations shared by both API protocols."""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from collections import deque
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from aistudio_api.infrastructure.utils import telemetry_store

RETENTION_SECONDS = 7 * 86400
MAX_COMPLETED = 500
MAX_ACTIVE = 64
MAX_LOGS = 1000
_MODEL = re.compile(r"(?:gemini|gemma)[a-zA-Z0-9_.-]{0,100}\Z")
State = Literal["queued", "running", "completed", "failed", "cancelled"]
_EVENTS = {
    "received": "请求已接收",
    "preparing": "准备账号与请求",
    "upstream": "开始上游调用",
    "retry": "上游重试",
    "first_thought": "收到首个思考片段",
    "first_body": "收到首个正文片段",
    "completed": "请求完成",
    "failed": "请求失败",
    "cancelled": "请求已取消",
    "started": "服务已启动",
    "stopped": "服务已停止",
}


@dataclass
class Observation:
    id: str
    protocol: str
    started_at: float = field(default_factory=time.time)
    began: float = field(default_factory=time.monotonic)
    model: str | None = None
    stream: bool = False
    state: State = "queued"
    status: int = 0
    duration_ms: int = 0
    attempts: int = 0
    finish_reason: str | None = None
    body_chars: int = 0
    thought_chars: int = 0
    timings: dict[str, int] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)
    stages: list[dict[str, str | int]] = field(default_factory=list)
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    error_status: int | None = None

    def snapshot(self) -> dict[str, object]:
        return {
            "id": self.id,
            "protocol": self.protocol,
            "started_at": self.started_at,
            "model": self.model,
            "stream": self.stream,
            "state": self.state,
            "status": self.status,
            "error_status": self.error_status,
            "duration_ms": elapsed(self) if self.task else self.duration_ms,
            "attempts": self.attempts,
            "finish_reason": self.finish_reason,
            "body_chars": self.body_chars,
            "thought_chars": self.thought_chars,
            "timings": dict(self.timings),
            "usage": dict(self.usage),
            "stages": [dict(stage) for stage in self.stages],
        }


active: dict[str, Observation] = {}
completed: deque[Observation] = deque(maxlen=MAX_COMPLETED)
logs: deque[dict[str, object]] = deque(maxlen=MAX_LOGS)
current: ContextVar[Observation | None] = ContextVar(
    "request_observation", default=None
)
_sequence = int(time.time() * 1_000_000)


def elapsed(item: Observation) -> int:
    return max(0, int((time.monotonic() - item.began) * 1000))


def prune() -> None:
    cutoff = time.time() - RETENTION_SECONDS
    while (
        completed and completed[0].started_at + completed[0].duration_ms / 1000 < cutoff
    ):
        completed.popleft()
    while logs and float(str(logs[0]["time"])) < cutoff:
        logs.popleft()


def event(name: str, *, item: Observation | None = None) -> None:
    global _sequence
    if name not in _EVENTS:
        return
    item = item or current.get()
    _sequence += 1
    logs.append(
        {
            "id": _sequence,
            "time": time.time(),
            "level": "ERROR"
            if name == "failed"
            else "WARN"
            if name in {"retry", "cancelled"}
            else "INFO",
            "source": "request" if item else "service",
            "event": name,
            "message": _EVENTS[name],
            "request_id": item.id if item else None,
            "model": item.model if item else None,
            "elapsed_ms": elapsed(item) if item else None,
        }
    )
    telemetry_store.save("event", secrets.token_hex(12), time.time(), logs[-1])
    if item and len(item.stages) < 64:
        item.stages.append({"event": name, "elapsed_ms": elapsed(item)})
    prune()


def bind_request(model: str, stream: bool) -> None:
    if item := current.get():
        clean = model.removeprefix("models/")
        item.model = clean if _MODEL.fullmatch(clean) else None
        item.stream = stream


def attempt_start() -> None:
    if item := current.get():
        item.attempts += 1
        item.state = "running"
        event("retry" if item.attempts > 1 else "preparing")


def upstream_start() -> None:
    if item := current.get():
        item.timings.setdefault("prepared", elapsed(item))
        event("upstream")


def observe_part(kind: str, value: object) -> None:
    if (
        (item := current.get())
        and kind in {"body", "thinking"}
        and isinstance(value, str)
        and value
    ):
        name = "first_body" if kind == "body" else "first_thought"
        if name not in item.timings:
            item.timings[name] = elapsed(item)
            event(name)
        if kind == "body":
            item.body_chars += len(value)
        else:
            item.thought_chars += len(value)


def result(usage: dict[str, object] | None, reason: str) -> None:
    if item := current.get():
        item.state = "completed"
        item.finish_reason = (
            reason
            if reason
            in {
                "STOP",
                "MAX_TOKENS",
                "SAFETY",
                "RECITATION",
                "OTHER",
                "LANGUAGE",
                "BLOCKLIST",
                "PROHIBITED_CONTENT",
                "SPII",
                "MALFORMED_FUNCTION_CALL",
                "IMAGE_SAFETY",
                "UNEXPECTED_TOOL_CALL",
                "TOO_MANY_TOOL_CALLS",
                "IMAGE_PROHIBITED_CONTENT",
                "IMAGE_OTHER",
                "NO_IMAGE",
                "IMAGE_RECITATION",
                "MISSING_THOUGHT_SIGNATURE",
                "FINISH_REASON_UNSPECIFIED",
            }
            else "OTHER"
        )
        item.usage = {
            key: value
            for key, value in (usage or {}).items()
            if key
            in {"prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens"}
            and type(value) is int
            and value >= 0
        }


def failed(status: int) -> None:
    if item := current.get():
        item.state = "failed"
        item.error_status = status


class ObservationMiddleware:
    """Observe the full response lifetime, with request-owned cancellation."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        generation = path == "/v1/chat/completions" or (
            path.startswith("/v1beta/")
            and path.endswith((":generateContent", ":streamGenerateContent"))
        )
        if scope["type"] != "http" or scope.get("method") != "POST" or not generation:
            await self.app(scope, receive, send)
            return
        if len(active) >= MAX_ACTIVE:
            await JSONResponse(
                {"error": {"message": "Request capacity reached"}}, status_code=503
            )(scope, receive, send)
            return
        item = Observation(
            "req_" + secrets.token_hex(8),
            "openai" if path.startswith("/v1/") else "gemini",
        )
        active[item.id] = item
        token = current.set(item)
        event("received")
        queue: asyncio.Queue[Message] = asyncio.Queue(maxsize=1)
        response_sent = False
        response_started = False
        disconnected = False

        async def observed_send(message: Message) -> None:
            nonlocal response_sent, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                if item.state != "failed":
                    item.status = status
                if status >= 400 and item.state != "cancelled":
                    item.state = "failed"
                message = {
                    **message,
                    "headers": [
                        *message.get("headers", []),
                        (b"x-request-id", item.id.encode()),
                    ],
                }
            if message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                response_sent = True
            await send(message)

        async def pump() -> None:
            nonlocal disconnected
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    if response_sent:
                        return
                    disconnected = True
                    item.state = "cancelled"
                    if item.task:
                        item.task.cancel()
                    return
                await queue.put(message)

        async def serve() -> None:
            await self.app(scope, queue.get, observed_send)

        item.task = asyncio.create_task(serve())
        reader = asyncio.create_task(pump())
        try:
            await item.task
        except asyncio.CancelledError:
            item.state = "cancelled"
            host = asyncio.current_task()
            if disconnected:
                return
            if host and host.cancelling():
                raise
            item.error_status = 499
            error = {
                "error": {
                    "message": "Request cancelled",
                    "type": "cancelled",
                    "code": 499,
                }
            }
            if not response_started:
                await JSONResponse(error, status_code=499)(
                    scope, queue.get, observed_send
                )
            elif not response_sent:
                await observed_send(
                    {
                        "type": "http.response.body",
                        "body": b'data: {"error":{"message":"Request cancelled","type":"cancelled","code":499}}\n\n',
                        "more_body": False,
                    }
                )
        except OSError:
            item.state = "cancelled"
            raise
        except Exception:
            item.state = "failed"
            item.status = 500
            raise
        finally:
            reader.cancel()
            with suppress(asyncio.CancelledError):
                await reader
            item.task = None
            item.duration_ms = elapsed(item)
            if item.state in {"queued", "running"}:
                item.state = "failed"
            event(item.state)
            active.pop(item.id, None)
            completed.append(item)
            telemetry_store.save("request", item.id, time.time(), item.snapshot())
            prune()
            current.reset(token)
