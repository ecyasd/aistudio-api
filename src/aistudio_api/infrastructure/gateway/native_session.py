"""Native resources under the existing account-generation and request leases."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import AsyncGenerator, Coroutine
from contextlib import aclosing, suppress
from dataclasses import dataclass, field
from pathlib import Path

from anyio import CancelScope

from aistudio_api.config import settings
from aistudio_api.domain.errors import BrowserTransportError, RequestError
from aistudio_api.infrastructure.account.storage_state import (
    atomic_write_json,
    auth_file_version,
    locked_file,
)
from aistudio_api.infrastructure.browser.cdp_client import CDPPage
from aistudio_api.infrastructure.gateway.native_bridge import NativeBridge
from aistudio_api.infrastructure.gateway.session import BrowserSession
from aistudio_api.infrastructure.gateway.session_auth import (
    get_auth_generation,
    get_auth_user_from_meta,
)
from aistudio_api.infrastructure.gateway.session_state import (
    SessionContext,
    SessionState,
)
from aistudio_api.infrastructure.gateway.wire_codec import AistudioWireCodec
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent
from aistudio_api.infrastructure.utils.response_diagnostics import note_timing

RPC_BASE = "https://alkalimakersuite-pa.clients6.google.com/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/"


@dataclass
class NativeAccount:
    session: str
    version: str
    original: dict = field(repr=False)
    closed: bool = False
    checkpoint_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


async def owned_cleanup(operation: Coroutine[object, object, None]) -> None:
    """Join one bounded cleanup even when a request is cancelled repeatedly."""
    task = asyncio.create_task(operation)
    cancelled = False
    with CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        task.result()
    if cancelled:
        raise asyncio.CancelledError


class NativeSession(BrowserSession):
    uses_native_signer = True

    def __init__(self, port: int, signer_url: str) -> None:
        super().__init__(port)
        self.bridge = NativeBridge(signer_url)

    def get_status(self) -> dict[str, object]:
        return {**super().get_status(), "backend": "browserless", "process_running": False}

    async def _owned_post(self, path: str, payload: dict[str, object], timeout: float) -> dict:
        task = asyncio.create_task(self.bridge.post(path, payload, timeout=timeout))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            with CancelScope(shield=True):
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                with suppress(Exception, asyncio.CancelledError):
                    task.result()
                if path == "/v1/proof/mint" and (task.cancelled() or task.exception() is not None):
                    context = self.request_context
                    if context is not None and context.native is not None and context.native.session == payload.get("session"):
                        self._failed_snapshot_contexts.add(context.context_id)
            raise

    async def transition_to_account(self, auth_file: str | None, force_refresh: bool = False) -> SessionContext:
        async with self._transition_lock:
            generation = get_auth_generation(auth_file)
            current = self.active_context
            if (not force_refresh and self.is_ready and current is not None
                    and current.auth_file == auth_file and current.auth_generation == generation):
                return current
            if not auth_file:
                raise RequestError(503, "No account is selected")
            for retiring in list(self._retiring_contexts):
                if retiring is self._request_context.get() and retiring.in_flight:
                    raise RequestError(503, "Cannot refresh an owned native lease")
                if retiring.in_flight:
                    await retiring.drain_event.wait()
                await self._release_context(retiring)
            if current is not None and current.auth_file == auth_file:
                if current is self._request_context.get() and current.in_flight:
                    raise RequestError(503, "Cannot refresh an owned native lease")
                if current.in_flight:
                    await current.drain_event.wait()
                self._retiring_contexts.append(current)
                await self._release_context(current)
                current = None
            self._gate.set_preparing()
            candidate: SessionContext | None = None
            try:
                with locked_file(Path(auth_file)):
                    original = json.loads(Path(auth_file).read_bytes())
                    version = auth_file_version(auth_file)
                if not version or not isinstance(original, dict) or not original.get("cookies"):
                    raise RequestError(503, "Selected account has no credential export")
                native = NativeAccount(uuid.uuid4().hex, version, original)
                candidate = SessionContext(
                    page=None, snap_key="native", auth_user=get_auth_user_from_meta(auth_file),
                    auth_file=auth_file, base_template={}, auth_generation=generation, native=native,
                )
                await self._owned_post("/v1/session/open", {
                    "session": native.session, "cookies": original, "authUser": int(candidate.auth_user),
                }, 95)
                if get_auth_generation(auth_file) != generation or auth_file_version(auth_file) != version:
                    raise RequestError(503, "Credentials changed during native preparation")
                self._auth_file, self._auth_user = auth_file, candidate.auth_user
                self._account_cleared = False
                self._last_activity_time = time.time()
                self._gate.set_ready(candidate)
                if current is not None:
                    self._retiring_contexts.append(current)
                    task = asyncio.create_task(self._drain_retiring_context(current))
                    self._background_tasks.add(task)
                    task.add_done_callback(self._background_tasks.discard)
                return candidate
            except BaseException:
                async def rollback() -> None:
                    if candidate is not None and candidate.native is not None:
                        with suppress(Exception):
                            await self.bridge.close_session(candidate.native.session)
                        candidate.native.closed = True
                    if current is not None and current.is_usable():
                        self._gate.set_ready(current)
                    else:
                        self._gate.set_failed(RequestError(503, "Native session preparation failed"))
                await owned_cleanup(rollback())
                raise

    async def _release_context(self, context: SessionContext) -> None:
        async with self._retirement_lock:
            if context not in self._retiring_contexts:
                return
            await self.persist_context_cookies(context)
            if context.native is not None:
                await self.bridge.close_session(context.native.session)
                context.native.closed = True
            self._retiring_contexts.remove(context)
            self._failed_snapshot_contexts.discard(context.context_id)

    async def _close_internal(self) -> None:
        current = self.active_context
        self._gate.reset()
        if current is not None and current not in self._retiring_contexts:
            self._retiring_contexts.append(current)
        for context in list(self._retiring_contexts):
            if context.in_flight:
                await context.drain_event.wait()
            await self._release_context(context)
        pending = list(self._background_tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._background_tasks.clear()
        self._last_stop_reason = "closed"

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            await self.bridge.close()

    async def check_idle_timeout(self) -> bool:
        timeout = settings.browser_idle_timeout
        if timeout <= 0 or self._in_flight or not self.is_ready:
            return False
        async with self._transition_lock:
            if (self._in_flight or self.state == SessionState.PREPARING
                    or time.time() - self._last_activity_time < timeout):
                return False
            await self._close_internal()
            self._last_stop_reason = "idle"
            return True

    async def is_alive(self, timeout_s: float = 1.5) -> bool:  # noqa: ARG002 - session interface
        return self.is_ready

    async def capture_template(self, model: str) -> dict[str, object]:
        await self.ensure_ready()
        model = model.removeprefix("models/")
        return {"url": RPC_BASE + "GenerateContent", "headers": {},
                "body": json.dumps(["models/" + model, [], None, None, None, None, None, None, None, None, 1])}

    async def generate_snapshot(self, contents: list[AistudioContent]) -> str:  # noqa: ARG002 - codec signs the final body
        # Sign only after the codec has produced the final transmitted contents.
        return ""

    async def finalize_request(self, body: str, model: str) -> str:
        from .build_assistant import rewrite_history
        request = AistudioWireCodec().decode(body)
        data = json.loads(body)
        if model.removeprefix("models/").endswith("-build"):
            request.contents = rewrite_history(request.contents)
            data[1] = [content.to_wire() for content in request.contents]
        values = []
        for content in request.contents:
            for part in content.parts:
                if part.text is not None:
                    values.append(part.text)
                elif part.inline_data is not None:
                    values.append(part.inline_data[1])
                elif part.file_id is not None:
                    values.append(part.file_id)
                elif part.executable_code is not None:
                    values.append(part.executable_code[1])
                elif part.code_execution_result is not None:
                    values.append(part.code_execution_result[1])
                else:
                    values.append("")
        context = await self.ensure_ready()
        assert context.native is not None
        async with self._snapshot_lock:
            if context.context_id in self._failed_snapshot_contexts:
                raise BrowserTransportError("Native signing context is unavailable")
            try:
                proof = await self._owned_post("/v1/proof/mint", {
                    "session": context.native.session,
                    "digest": hashlib.sha256(" ".join(values).encode()).hexdigest(),
                    "pagePrompt": " ".join(p.text or "" for p in request.contents[-1].parts),
                }, 12)
                if not isinstance(proof.get("proof"), str) or not proof["proof"]:
                    raise BrowserTransportError("Local signer returned no proof")
            except asyncio.CancelledError:
                raise
            except Exception:
                self._failed_snapshot_contexts.add(context.context_id)
                raise
        data.extend([None] * max(0, 5 - len(data)))
        data[4] = proof["proof"]
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    async def send_streaming_request(
        self, *, body: str, timeout_ms: int = 120000, url: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> AsyncGenerator[tuple[str, object], None]:
        if (url is not None and url != RPC_BASE + "GenerateContent") or headers:
            raise BrowserTransportError("Native transport accepts only its own RPC identity")
        from .build_assistant import stream_assistant
        async with self.request_scope() as context:
            assert context.native is not None
            data = json.loads(body)
            note_timing("transport_dispatched")
            if str(data[0]).endswith("-build"):
                events = stream_assistant(self.bridge, context.native.session, data, timeout_ms / 1000)
            else:
                events = self.bridge.stream(context.native.session, "GenerateContent", data, timeout_ms / 1000)
            async with aclosing(events):
                async for event in events:
                    yield event

    async def send_hooked_request(
        self, *, body: str, timeout_ms: int = 120000, url: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        status = 0
        result = bytearray()
        async with aclosing(self.send_streaming_request(body=body, timeout_ms=timeout_ms, url=url, headers=headers)) as events:
            async for kind, value in events:
                if kind == "status":
                    status = int(str(value))
                elif isinstance(value, bytes):
                    result.extend(value)
        return status, bytes(result)

    async def persist_context_cookies(self, context: SessionContext | None = None) -> None:
        target = context or self.request_context
        if target is None or target.native is None or not target.auth_file or target.native.closed:
            return
        native = target.native
        async with native.checkpoint_lock:
            with suppress(Exception):
                result = await self.bridge.post("/v1/session/state", {"session": native.session}, timeout=3)
                state = result.get("state")
                if not isinstance(state, dict) or not isinstance(state.get("cookies"), list) or not state["cookies"]:
                    return
                names = {c.get("name") for c in state["cookies"] if isinstance(c, dict) and c.get("value")}
                if not names.intersection({"SAPISID", "__Secure-3PAPISID"}):
                    return
                original_names = {c.get("name") for c in native.original.get("cookies", []) if isinstance(c, dict)}
                if any(original_names & family and not names & family for family in (
                    {"SID", "__Secure-1PSID", "__Secure-3PSID"},
                    {"SAPISID", "APISID", "__Secure-1PAPISID", "__Secure-3PAPISID"},
                )):
                    return
                merged = {**native.original, "cookies": state["cookies"]}
                with locked_file(Path(target.auth_file)):
                    if get_auth_generation(target.auth_file) != target.auth_generation:
                        return
                    if not atomic_write_json(Path(target.auth_file), merged, expected_version=native.version):
                        return
                    native.original = merged
                    native.version = auth_file_version(target.auth_file) or native.version

    async def cleanup_stream_page(self, page: CDPPage | None = None) -> None:  # noqa: ARG002 - no browser resource
        return None

    async def detect_account_email(self, auth_file: str) -> str | None:  # noqa: ARG002 - do not start a browser
        return None
