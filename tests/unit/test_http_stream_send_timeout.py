"""Offline review probes: actual FastAPI middleware versus direct SSE response."""

import asyncio

import pytest
from starlette.requests import ClientDisconnect

from aistudio_api.api import gemini
from aistudio_api.api.error_middleware import ErrorRecordsMiddleware
from aistudio_api.api.sse_delivery import SSEDelivery


@pytest.mark.asyncio
@pytest.mark.parametrize("asgi_spec", ["2.3", "2.4"])
@pytest.mark.parametrize(
    "real_app", [False, True], ids=["direct-control", "actual-fastapi-stack"]
)
@pytest.mark.parametrize(
    "source_finishes", [False, True], ids=["upstream-waits", "short-completed-response"]
)
async def test_outer_sse_send_obeys_configured_deadline(
    monkeypatch, real_app, source_finishes, asgi_spec
):
    """A 20ms send budget must end a stalled outer send before 150ms."""
    budget = 0.020
    monkeypatch.setattr(gemini, "SSE_SEND_TIMEOUT_SECONDS", budget)
    source_closed = asyncio.Event()
    write_started = asyncio.Event()

    async def source(emit):
        try:
            await emit('data: {"candidates": []}\n\n')
            if not source_finishes:
                await asyncio.Event().wait()
        finally:
            source_closed.set()

    delivery = SSEDelivery()
    response = gemini._GeminiStreamingResponse(
        gemini._sse_with_heartbeats(
            source, interval=60, max_buffer_events=8, coalesce=True, delivery=delivery
        ),
        delivery=delivery,
        media_type="text/event-stream",
    )
    overrides = None
    if real_app:
        from aistudio_api.api import routes_gemini
        from aistudio_api.api.app import app
        from aistudio_api.api.dependencies import get_client, require_api_key

        async def synthetic_handler(*args, **kwargs):
            return response

        monkeypatch.setattr(
            routes_gemini, "handle_gemini_generate_content", synthetic_handler
        )
        overrides = dict(app.dependency_overrides)
        app.dependency_overrides[get_client] = lambda: object()
        app.dependency_overrides[require_api_key] = lambda: None
    else:

        async def endpoint(scope, receive, send):
            await receive()
            await response(scope, receive, send)

        app = ErrorRecordsMiddleware(endpoint)

    body_sent = False

    async def receive():
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {
                "type": "http.request",
                "body": b'{"contents":[{"parts":[{"text":"synthetic"}]}]}',
                "more_body": False,
            }
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            write_started.set()
            await asyncio.Event().wait()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": asgi_spec},
        "method": "POST",
        "http_version": "1.1",
        "scheme": "http",
        "root_path": "",
        "server": ("synthetic.invalid", 80),
        "client": ("127.0.0.1", 1234),
        "path": "/v1beta/models/gemini-synthetic:streamGenerateContent",
        "raw_path": b"/v1beta/models/gemini-synthetic:streamGenerateContent",
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
    }
    application = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(write_started.wait(), 1)
        done, _ = await asyncio.wait({application}, timeout=0.150)
        assert application in done, (
            "outer HTTP send remains stalled past the configured SSE send budget; "
            f"source_closed={source_closed.is_set()}"
        )

        def is_send_failure(error):
            if isinstance(error, (TimeoutError, ClientDisconnect)):
                return True
            return isinstance(error, BaseExceptionGroup) and any(
                is_send_failure(child) for child in error.exceptions
            )

        assert is_send_failure(application.exception())
    finally:
        application.cancel()
        await asyncio.gather(application, return_exceptions=True)
        if overrides is not None:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(overrides)
        assert source_closed.is_set()
