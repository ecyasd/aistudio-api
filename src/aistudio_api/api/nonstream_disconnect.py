"""Cancel abandoned nonstream inference after the application consumes its body."""
from __future__ import annotations

import asyncio

from anyio import CancelScope
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class NonStreamingDisconnectMiddleware:
    """Own cancellation above HTTP middleware, without buffering request data.

    A JSON endpoint does not read ASGI receive while awaiting generation. Its
    client can therefore disappear without cancelling the inference task. Only
    the nonstream generation route needs this extra receive listener; streaming
    responses retain their existing disconnect and cleanup behavior.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and scope.get("path", "").startswith("/v1beta/")
            and scope.get("path", "").endswith(":generateContent")
        ):
            await self.app(scope, receive, send)
            return

        body_consumed = asyncio.Event()
        disconnect_observed = asyncio.Event()
        response_completed = False

        async def receive_body() -> Message:
            if body_consumed.is_set():
                # The watcher is the sole raw receiver after the body. Existing
                # HTTP middleware can still listen for the same disconnect.
                await disconnect_observed.wait()
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request" and not message.get("more_body", False):
                body_consumed.set()
            return message

        async def wait_for_disconnect() -> None:
            # Never compete with request parsing or keep a copy of its body.
            await body_consumed.wait()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    disconnect_observed.set()
                    return

        async def send_response(message: Message) -> None:
            nonlocal response_completed
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                response_completed = True

        async def invoke_application() -> None:
            await self.app(scope, receive_body, send_response)

        application = asyncio.create_task(invoke_application(), name="nonstream-application")
        disconnected = asyncio.create_task(
            wait_for_disconnect(), name="nonstream-disconnect"
        )
        try:
            await asyncio.wait({application, disconnected}, return_when=asyncio.FIRST_COMPLETED)
            # Completion wins a simultaneous disconnect, preserving its result
            # or original exception rather than inventing a cancelled request.
            if application.done():
                application.result()
                return
            if response_completed:
                # ASGI servers may return http.disconnect after the last body
                # even on success. Preserve normal application finalization.
                await application
                return
            disconnected.result()
            # An observed disconnect permits ASGI to return without a response.
            # Raising CancelledError here would make Uvicorn log an ASGI 500.
            # The outer diagnostics middleware already observed http.disconnect.
        finally:
            for task in (application, disconnected):
                if not task.done():
                    task.cancel()
            # Await resource cleanup on disconnect and external cancellation.
            # Do not detach work that could retain an inference slot or lease.
            with CancelScope(shield=True):
                await asyncio.gather(application, disconnected, return_exceptions=True)
