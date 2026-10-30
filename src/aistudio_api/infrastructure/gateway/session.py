"""Shared browser session management for gateway operations."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing, asynccontextmanager, suppress
from contextvars import ContextVar
from hashlib import sha256
from pathlib import Path

from anyio import CancelScope

from aistudio_api.config import settings
from aistudio_api.infrastructure.browser.browser_engine import (
    ChromiumProcess,
    launch_chromium_process,
)
from aistudio_api.infrastructure.browser.cdp_client import CDPClient, CDPPage
from aistudio_api.infrastructure.browser.scripts import (
    DOM_GC_CLEANUP_JS,
    SNAPSHOT_GENERATE_JS,
)
from aistudio_api.infrastructure.gateway.botguard_bootstrap import (
    BOTGUARD_BOOTSTRAP_PROMPT,
    bootstrap_botguard_and_template,
)
from aistudio_api.infrastructure.gateway.session_auth import (
    bind_auth_file,
    bootstrap_google_session,
    derive_profile_dir,
    extract_account_email,
    get_auth_file_version,
    get_auth_generation,
    get_auth_user_from_meta,
    is_login_page_url,
    load_cookies_to_page,
    navigate_to_aistudio,
    save_browser_cookies,
)
from aistudio_api.infrastructure.gateway.session_state import (
    SessionContext,
    SessionReadinessGate,
    SessionState,
)
from aistudio_api.infrastructure.gateway.transport import XHRStreamTransport
from aistudio_api.infrastructure.gateway.wire_codec import InvalidRequestTemplateError
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent
from aistudio_api.infrastructure.utils.logger import get_logger
from aistudio_api.infrastructure.utils.response_diagnostics import (
    note_session_preparation,
    note_timing,
)

log = get_logger("session")

_is_login_page_url = is_login_page_url
SNAPSHOT_TIMEOUT_SECONDS = 10.0

DEFAULT_BOOTSTRAP_TEMPLATE: dict[str, object] = {
    "url": "https://alkalimakersuite-pa.clients6.google.com/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/GenerateContent",
    "headers": {
        "Content-Type": "application/json+protobuf",
        "X-User-Agent": "grpc-web-javascript/0.1",
        "X-Goog-Api-Key": "AIzaSyDdP816MREB3SkjZO04QXbjsigfcI0GWOs",
        "X-Goog-AuthUser": "0",
        "Referer": "https://aistudio.google.com/",
    },
    "body": '["models/gemini-3.7-flash"]',
}


class BrowserSession:
    """Coordinates browser lifecycle, readiness gate, and blue-green tab transitions."""

    def __init__(self, port: int):
        self.port = port
        self._auth_file: str | None = settings.auth_file or None
        self._auth_user: str | None = None
        self._account_cleared = False
        self._profile_dir = derive_profile_dir(self._auth_file)
        self._proc: ChromiumProcess | None = None
        self._cdp_client: CDPClient | None = None

        self._gate = SessionReadinessGate()
        self._transition_lock = asyncio.Lock()
        self._retirement_lock = asyncio.Lock()
        self._snapshot_lock = asyncio.Lock()
        self._failed_snapshot_contexts: set[int] = set()
        self._transport = XHRStreamTransport()
        self._retiring_contexts: list[SessionContext] = []
        self._background_tasks: set[asyncio.Task[object]] = set()
        self._in_flight: int = 0
        self._last_activity_time: float = time.time()
        self._stream_cleanup_count: int = 0
        self._last_stop_reason = "not_started"
        self._request_context: ContextVar[SessionContext | None] = ContextVar(
            f"browser_session_{id(self)}", default=None
        )

    @property
    def state(self) -> SessionState:
        if self._gate.state == SessionState.READY and not self.is_ready:
            return SessionState.FAILED
        return self._gate.state

    @property
    def is_ready(self) -> bool:
        context = self._gate.active_context
        return (
            self._gate.is_ready()
            and context is not None
            and context.context_id not in self._failed_snapshot_contexts
        ) and (
            self._proc is None or self._proc.is_alive()
        )

    def get_status(self) -> dict[str, object]:
        """Describe local lifecycle state without probing Google or reading cookies."""
        process_running = self._proc is not None and self._proc.is_alive()
        if self.state == SessionState.PREPARING:
            status = "starting"
        elif self.state == SessionState.FAILED:
            status = "failed"
        elif self.is_ready:
            status = "ready"
        elif process_running:
            status = "starting"
        else:
            status = self._last_stop_reason
        return {
            "status": status,
            "process_running": process_running,
            "in_flight": self._in_flight,
            "idle_timeout_seconds": getattr(settings, "browser_idle_timeout", 0),
        }

    @property
    def active_context(self) -> SessionContext | None:
        return self._gate.active_context

    @property
    def request_context(self) -> SessionContext | None:
        """Return the context leased by this request, or the current active one."""
        return self._request_context.get() or self.active_context

    @property
    def page(self) -> CDPPage | None:
        return self._gate.active_context.page if self._gate.active_context else None

    @property
    def snap_key(self) -> str | None:
        return self._gate.active_context.snap_key if self._gate.active_context else None

    @property
    def base_template(self) -> dict[str, object] | None:
        return (
            self._gate.active_context.base_template
            if self._gate.active_context
            else None
        )

    def set_ready_context(self, context: SessionContext) -> None:
        """Set active ready context (used by transitions and test fixtures)."""
        self._gate.set_ready(context)

    def get_current_auth_user(self) -> str:
        """获取当前活跃账号的 auth_user 编号（0, 1, 2...）。"""
        if self._auth_user is not None:
            return self._auth_user
        if self._gate.active_context is not None:
            return self._gate.active_context.auth_user
        return get_auth_user_from_meta(self._auth_file)

    @asynccontextmanager
    async def existing_context_scope(self) -> AsyncIterator[SessionContext | None]:
        """Borrow a ready context without starting, refreshing or probing it."""
        bound = self._request_context.get()
        if bound is not None:
            yield bound
            return
        # No await between checking readiness and registering the lease: a
        # transition must not retire this page before it sees our in-flight work.
        ctx = self.active_context if self.is_ready else None
        if ctx is None:
            yield None
            return
        self._in_flight += 1
        self._last_activity_time = time.time()
        ctx.incr_in_flight()
        token = self._request_context.set(ctx)
        try:
            yield ctx
        finally:
            self._request_context.reset(token)
            ctx.decr_in_flight()
            self._in_flight = max(0, self._in_flight - 1)
            self._last_activity_time = time.time()

    @asynccontextmanager
    async def request_scope(self):
        """Lease one context for template, snapshot, and transport together."""
        bound = self._request_context.get()
        if bound is not None:
            yield bound
            return
        self._in_flight += 1
        self._last_activity_time = time.time()
        try:
            ctx = await self.ensure_ready()
            ctx.incr_in_flight()
            token = self._request_context.set(ctx)
            try:
                yield ctx
            finally:
                self._request_context.reset(token)
                ctx.decr_in_flight()
        finally:
            self._in_flight = max(0, self._in_flight - 1)
            self._last_activity_time = time.time()

    async def ensure_ready(self, timeout_s: float = 60.0) -> SessionContext:
        """Wait until session is 100% READY."""
        bound = self._request_context.get()
        if bound is not None:
            return bound
        async with asyncio.timeout(timeout_s):
            while True:
                if self._account_cleared:
                    raise RuntimeError("No account is selected")
                if (
                    self.is_ready and self._gate.active_context is not None
                    and self._gate.active_context.auth_generation == get_auth_generation(self._auth_file)
                ):
                    note_session_preparation("warm_reuse", settings.browser_idle_timeout)
                    return self._gate.active_context

                if self.state != SessionState.PREPARING:
                    await self.transition_to_account(self._auth_file, force_refresh=False)

                # Owner imports and deletions can finish after READY is signaled
                # but before a queued waiter resumes. Recheck before leasing.
                await self._gate.wait_ready(timeout_s=timeout_s)

    async def ensure_context(self) -> CDPPage:
        """Ensure active verified CDPPage is available, auto-reconnecting if dropped."""
        page = self.page
        if page is not None and not page.is_closed():
            if await page.is_alive(timeout_s=1.0):
                return page
            log.warning("浏览器页面无响应，重新连接...")
        await self._close_internal()
        res = await self._ensure_browser_cdp()
        return res or (await self.ensure_ready()).page

    async def is_alive(self, timeout_s: float = 1.5) -> bool:
        if self._proc is not None and not self._proc.is_alive():
            return False
        page = self.page
        if page is None or page.is_closed():
            return False
        return await page.is_alive(timeout_s=timeout_s)

    async def check_idle_timeout(self) -> bool:
        idle_timeout = getattr(settings, "browser_idle_timeout", 0)
        if idle_timeout <= 0:
            return False
        async with self._transition_lock:
            # The monitor may have waited behind startup, a switch, or a new
            # request. Recheck the lease and activity only after owning the lock.
            if self._in_flight > 0 or self._gate.state == SessionState.PREPARING:
                return False
            if self._proc is None:
                return False
            idle_duration = time.time() - self._last_activity_time
            if idle_duration >= idle_timeout:
                log.info(
                    "浏览器已空闲 %.1f 秒 (阈值=%d 秒)，回收进程释放内存",
                    idle_duration,
                    idle_timeout,
                )
                await self._close_internal()
                self._last_stop_reason = "idle"
                return True
        return False

    async def switch_auth(self, auth_file: str | None) -> None:
        """Switch auth using blue-green transition without killing in-flight streams."""
        await self.transition_to_account(auth_file, force_refresh=True)

    async def clear_account(self) -> None:
        """Stop new leases without authenticating; drain existing account work."""
        async with self._transition_lock:
            self._account_cleared = True
            self._auth_file = None
            self._auth_user = None
            self._profile_dir = None
            old_context = self.active_context
            self._gate.reset()
            self._last_stop_reason = "no_account"
            if old_context is not None:
                self._retiring_contexts.append(old_context)
                drain = asyncio.create_task(
                    self._drain_retiring_context(old_context), name="drain_deleted_account",
                )
                self._background_tasks.add(drain)
                drain.add_done_callback(self._background_tasks.discard)

    async def transition_to_account(
        self, auth_file: str | None, force_refresh: bool = False
    ) -> SessionContext:
        """Execute atomic session transition to target auth file."""
        async with self._transition_lock:
            self._last_activity_time = time.time()
            auth_generation = get_auth_generation(auth_file)
            if (
                not force_refresh
                and self.is_ready
                and self._gate.active_context is not None
                and self._gate.active_context.auth_file == auth_file
                and self._gate.active_context.auth_generation == auth_generation
            ):
                note_session_preparation("warm_reuse", settings.browser_idle_timeout)
                return self._gate.active_context

            current = self.active_context
            if current is not None and current.auth_file != auth_file:
                reason = "account_change"
            elif current is not None and current.auth_generation != auth_generation:
                reason = "credentials_change"
            elif self._last_stop_reason == "idle":
                reason = "idle_recycle"
            elif self._last_stop_reason == "not_started" and self._proc is None:
                reason = "service_start"
            elif self._proc is None or not self._proc.is_alive():
                reason = "browser_recovery"
            elif force_refresh:
                reason = "forced_refresh"
            elif current is not None and current.page.is_closed():
                reason = "page_recovery"
            else:
                reason = "session_not_ready"
            note_session_preparation(reason, settings.browser_idle_timeout)

            # Keep at most an active and a preparing/retiring account page on 1 GB.
            for context in list(self._retiring_contexts):
                if context is self._request_context.get() and context.in_flight:
                    raise RuntimeError("Cannot refresh CDP while owning a retiring request lease")
                if context.in_flight:
                    await context.drain_event.wait()
                await self._release_context(context)

            old_context = self._gate.active_context
            same_account_refresh = bool(
                old_context is not None and old_context.auth_file == auth_file
            )
            if (
                same_account_refresh
                and old_context is not None
                and old_context is self._request_context.get()
                and old_context.in_flight
            ):
                raise RuntimeError("Cannot refresh CDP while owning its active request lease")
            old_auth_file = self._auth_file
            old_auth_user = self._auth_user
            old_profile_dir = self._profile_dir
            self._gate.set_preparing()
            target_auth_user = get_auth_user_from_meta(auth_file)
            new_page: CDPPage | None = None

            try:
                if same_account_refresh and old_context is not None:
                    # The previous page may still rotate its cookies. Finish its
                    # final checkpoint before the replacement binds to disk;
                    # otherwise that checkpoint invalidates every future CAS
                    # write from the replacement despite no owner reimport.
                    while old_context.in_flight:
                        await old_context.drain_event.wait()
                    self._retiring_contexts.append(old_context)
                    await self._release_context(old_context)
                    old_context = None

                # Real browser mode
                if self._proc is None or not self._proc.is_alive():
                    note_timing("browser_launch_started")
                    await self._close_internal()
                    old_context = None
                    self._gate.set_preparing()
                    self._profile_dir = derive_profile_dir(auth_file)
                    await self._ensure_browser_cdp()
                    note_timing("browser_ready")

                assert self._cdp_client is not None

                new_page = await self._cdp_client.create_new_page(block_assets=True)
                note_timing("browser_context_ready")
                new_page.set_liveness_checker(
                    lambda: self._proc is None or self._proc.is_alive()
                )

                if auth_file and not await load_cookies_to_page(new_page, auth_file):
                    raise RuntimeError("Could not load target account cookies")
                note_timing("browser_cookies_loaded")
                await navigate_to_aistudio(
                    new_page,
                    auth_user=target_auth_user,
                    auth_file=auth_file,
                )
                note_timing("browser_navigation_ready")

                async def _reload_cb(page: CDPPage) -> bool:
                    return await load_cookies_to_page(page, auth_file)

                snap_key, base_template = await bootstrap_botguard_and_template(
                    new_page,
                    reload_cookies_cb=_reload_cb,
                    is_proc_alive_cb=lambda: (
                        self._proc is None or self._proc.is_alive()
                    ),
                    auth_user=target_auth_user,
                )

                new_context = SessionContext(
                    page=new_page,
                    snap_key=snap_key,
                    auth_user=target_auth_user,
                    auth_file=auth_file,
                    base_template=base_template,
                    auth_generation=auth_generation,
                )
                await self.persist_context_cookies(new_context)
                note_timing("session_checkpoint_done")

                if get_auth_generation(auth_file) != auth_generation:
                    raise RuntimeError("Account credentials changed during session preparation")

                self._auth_file = auth_file
                self._account_cleared = False
                self._auth_user = target_auth_user
                self._profile_dir = derive_profile_dir(auth_file)
                self._last_stop_reason = "not_started"
                self._last_activity_time = time.time()
                self._gate.set_ready(new_context)
                if (
                    old_context is not None
                    and old_context.page.target_id != new_page.target_id
                ):
                    self._retiring_contexts.append(old_context)
                    drain_task = asyncio.create_task(
                        self._drain_retiring_context(old_context),
                        name=f"drain_{old_context.auth_user}",
                    )
                    self._background_tasks.add(drain_task)
                    drain_task.add_done_callback(self._background_tasks.discard)
                log.info(
                    "会话蓝绿平滑热切成功: auth_file=%s, u/%s (活跃在途=%d)",
                    auth_file,
                    target_auth_user,
                    self._in_flight,
                )
                return new_context

            except BaseException as exc:
                # ASGI cancellation can recur at every await. Dispose only our
                # candidate and restore the previously ready context first.
                with CancelScope(shield=True):
                    if new_page is not None and self._cdp_client is not None:
                        with suppress(Exception):
                            await self._cdp_client.close_target(new_page.target_id)
                    self._auth_file = old_auth_file
                    self._auth_user = old_auth_user
                    self._profile_dir = old_profile_dir
                    if (
                        old_context is not None
                        and old_context.is_usable()
                        and self._proc is not None
                        and self._proc.is_alive()
                    ):
                        self._gate.set_ready(old_context)
                    else:
                        await self._close_internal()
                        self._last_stop_reason = "failed"
                        self._gate.set_failed(
                            exc if isinstance(exc, Exception)
                            else RuntimeError("Session transition cancelled")
                        )
                log.error("会话切换失败: %s", exc)
                raise

    async def _drain_retiring_context(
        self,
        context: SessionContext,
    ) -> None:
        try:
            while context.in_flight > 0:
                await context.drain_event.wait()
            await self._release_context(context)
        except Exception:
            log.exception("释放已退出账号的浏览器上下文失败")

    async def _release_context(self, context: SessionContext) -> None:
        async with self._retirement_lock:
            if context not in self._retiring_contexts:
                return
            try:
                if not context.page.is_closed():
                    await self.persist_context_cookies(context)
                    await context.page.close()
            finally:
                if self._cdp_client:
                    await self._cdp_client.close_target(context.page.target_id)
                self._retiring_contexts.remove(context)
                self._failed_snapshot_contexts.discard(context.context_id)

    async def ensure_botguard_service(self, force_refresh: bool = False) -> CDPPage:
        page = self.page
        curr_url = page.url if page else ""
        if "available-regions" in curr_url:
            raise RuntimeError(
                f"Google AI Studio 地区限制: 访问被重定向至 available-regions ({curr_url})"
            )
        if force_refresh or not self._gate.is_ready():
            await self.transition_to_account(
                self._auth_file, force_refresh=force_refresh
            )
        return (await self.ensure_ready()).page

    async def capture_template(self, model: str) -> dict[str, object]:
        if self._proc is not None and not self._proc.is_alive():
            raise RuntimeError("Browser process died during template capture")

        ctx = await self.ensure_ready()
        base = dict(ctx.base_template)
        normalized_model = model if model.startswith("models/") else f"models/{model}"
        try:
            body_val = base.get("body")
            if (
                isinstance(body_val, str)
                and body_val.startswith('["models/')
                and "," in body_val
            ):
                first_comma = body_val.find(",")
                base["body"] = f'["{normalized_model}"' + body_val[first_comma:]
            elif isinstance(body_val, str) and body_val.startswith("["):
                body_list = json.loads(body_val)
                if isinstance(body_list, list) and len(body_list) > 0:
                    body_list[0] = normalized_model
                    base["body"] = json.dumps(body_list, separators=(",", ":"))
        except Exception as e:
            log.debug("Failed to adapt model into base template body: %s", e)
        return base

    async def generate_snapshot(self, contents: list[AistudioContent]) -> str:
        ctx = await self.ensure_ready()
        if ctx.context_id in self._failed_snapshot_contexts:
            raise InvalidRequestTemplateError("BotGuard signing context is unavailable")
        hasher = sha256()
        first = True
        for content in contents:
            for part in content.parts:
                if part.text is not None:
                    value = str(part.text)
                elif part.inline_data is not None:
                    value = str(part.inline_data[1])
                elif part.file_id is not None:
                    value = str(part.file_id)
                elif part.executable_code is not None:
                    value = str(part.executable_code[1])
                elif part.code_execution_result is not None:
                    value = str(part.code_execution_result[1])
                else:
                    value = ""
                if not first:
                    hasher.update(b" ")
                hasher.update(value.encode("utf-8"))
                first = False

        content_hash = hasher.hexdigest()

        async with self._snapshot_lock:
            # Another lease may have failed while this caller waited for signing.
            if ctx.context_id in self._failed_snapshot_contexts:
                raise InvalidRequestTemplateError("BotGuard signing context is unavailable")

            async def read_snapshot() -> str:
                async with asyncio.timeout(SNAPSHOT_TIMEOUT_SECONDS):
                    snapshot = await ctx.page.evaluate(
                        SNAPSHOT_GENERATE_JS, args=content_hash,
                        timeout_s=SNAPSHOT_TIMEOUT_SECONDS,
                    )
                    if isinstance(snapshot, str) and snapshot:
                        return snapshot
                    raise InvalidRequestTemplateError("BotGuard signing returned no snapshot")

            snapshot_read = asyncio.create_task(read_snapshot(), name="aistudio-snapshot-read")
            try:
                return await asyncio.shield(snapshot_read)
            except asyncio.CancelledError:
                # Cancelling CDP's waiter cannot settle the page's JS snapQueue.
                # Join only this read under its original deadline before releasing
                # the lease, including repeated AnyIO or asyncio cancellation.
                with CancelScope(shield=True):
                    while not snapshot_read.done():
                        try:
                            await asyncio.shield(snapshot_read)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break
                    if snapshot_read.cancelled():
                        self._failed_snapshot_contexts.add(ctx.context_id)
                    else:
                        try:
                            snapshot_read.result()
                        except Exception:
                            self._failed_snapshot_contexts.add(ctx.context_id)
                raise
            except Exception as exc:
                # More evaluations would queue behind an unresolved Promise.
                # Existing bounded recovery replaces this page after leases drain.
                self._failed_snapshot_contexts.add(ctx.context_id)
                raise InvalidRequestTemplateError("BotGuard signing context is unavailable") from exc

    async def send_hooked_request(
        self,
        *,
        body: str,
        timeout_ms: int,
        url: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        async with self.request_scope() as ctx:
            captured_url = url or str(ctx.base_template.get("url") or "")
            hdrs_val = ctx.base_template.get("headers")
            captured_headers = headers or (
                {str(k): str(v) for k, v in hdrs_val.items()}
                if isinstance(hdrs_val, dict)
                else {}
            )
            return await self._transport.send_hooked_request(
                ctx.page,
                url=captured_url,
                headers=captured_headers,
                body=body,
                timeout_ms=timeout_ms,
                auth_user=ctx.auth_user,
            )

    async def send_streaming_request(
        self,
        *,
        body: str,
        timeout_ms: int,
        url: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> AsyncGenerator[tuple[str, object], None]:
        async with self.request_scope() as ctx:
            captured_url = url or str(ctx.base_template.get("url") or "")
            hdrs_val = ctx.base_template.get("headers")
            captured_headers = headers or (
                {str(k): str(v) for k, v in hdrs_val.items()}
                if isinstance(hdrs_val, dict)
                else {}
            )
            try:
                async with aclosing(
                    self._transport.send_streaming_request(
                        ctx.page,
                        url=captured_url,
                        headers=captured_headers,
                        body=body,
                        timeout_ms=timeout_ms,
                        auth_user=ctx.auth_user,
                    )
                ) as events:
                    async for event in events:
                        yield event
            finally:
                await self.cleanup_stream_page(ctx.page)

    async def cleanup_stream_page(self, page: CDPPage | None = None) -> None:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            # The transport already aborted this request's rid. Optional DOM
            # maintenance must not retain its lease or delay the next roll.
            return
        self._stream_cleanup_count += 1
        target_page = page or (
            self.active_context.page if self.active_context else None
        )
        if (
            target_page is not None
            and not target_page.is_closed()
            and (self._in_flight <= 0 or (self._stream_cleanup_count % 5 == 0))
        ):
            with suppress(Exception):
                # Maintenance is optional even on successful completion. Keep
                # a stalled CDP command out of the main request's long timeout.
                async with asyncio.timeout(0.25):
                    await target_page.evaluate(DOM_GC_CLEANUP_JS, timeout_s=0.25)
                    if self._in_flight <= 0 and (self._stream_cleanup_count % 10 == 0):
                        await target_page.collect_garbage()
                        import gc

                        gc.collect()

    async def import_cookies(
        self, cookie_string: str, auth_file: str | None = None
    ) -> int:
        from aistudio_api.infrastructure.account.cookie_refresher import (
            load_cookies_from_string,
        )

        pw_cookies = load_cookies_from_string(cookie_string)
        target_auth = auth_file or self._auth_file
        if not target_auth:
            return len(pw_cookies)
        imported_version = get_auth_file_version(target_auth)
        if imported_version is None:
            return len(pw_cookies)

        async with self._temporary_auth_page() as page:
            if not bind_auth_file(
                page, target_auth, expected_version=imported_version
            ):
                # A later owner import already replaced this credential file
                # while this request waited for the browser transition lock.
                return len(pw_cookies)
            await page.set_cookies(pw_cookies)
            with suppress(Exception):
                await bootstrap_google_session(page)
            browser_cookies = await page.get_cookies()
            final_cookies = browser_cookies or pw_cookies
            await save_browser_cookies(
                page, auth_file=target_auth, cookies=final_cookies
            )
            return len(final_cookies)

    @asynccontextmanager
    async def _temporary_auth_page(self):
        """Serialize short-lived auth pages and release cold browser processes."""
        async with self._transition_lock:
            self._in_flight += 1
            page = None
            try:
                for context in list(self._retiring_contexts):
                    if context.in_flight:
                        await context.drain_event.wait()
                    await self._release_context(context)
                await self._ensure_browser_cdp()
                assert self._cdp_client is not None
                page = await self._cdp_client.create_new_page(block_assets=True)
                yield page
            finally:
                if page is not None and self._cdp_client is not None:
                    with suppress(Exception):
                        await self._cdp_client.close_target(page.target_id)
                self._in_flight = max(0, self._in_flight - 1)
                self._last_activity_time = time.time()
                if self.active_context is None and self._in_flight == 0:
                    await self._close_internal()

    async def detect_account_email(self, auth_file: str) -> str | None:
        """Identify an account in an isolated page without inference or state writes."""
        async with self._temporary_auth_page() as page:
            if not await load_cookies_to_page(page, auth_file):
                raise RuntimeError("Could not load target account cookies")
            await navigate_to_aistudio(
                page, auth_user=get_auth_user_from_meta(auth_file), auth_file=None
            )
            return await extract_account_email(page)

    async def extract_account_email(self, page: CDPPage | None = None) -> str | None:
        target = page or (self.active_context.page if self.active_context else None)
        return await extract_account_email(target)

    async def reload_current_account_cookies(self, page: CDPPage | None = None) -> bool:
        target = page or (self.active_context.page if self.active_context else None)
        return bool(target and await load_cookies_to_page(target, self._auth_file))

    async def _save_cookies(
        self,
        *,
        auth_file: str | None = None,
        cookies: list[dict[str, object]] | None = None,
    ) -> None:
        target = self.page
        if target is not None:
            await save_browser_cookies(
                target, auth_file=auth_file or self._auth_file, cookies=cookies
            )

    async def persist_context_cookies(
        self, context: SessionContext | None = None
    ) -> None:
        """Save a verified page only to the account owning that page's lease."""
        target = context or self.request_context
        if target is None or not target.auth_file or target.page.is_closed():
            return
        with suppress(Exception):
            await save_browser_cookies(target.page, auth_file=target.auth_file)

    async def _ensure_browser_cdp(self) -> CDPPage | None:
        if self._proc is not None and self._proc.is_alive() and self._cdp_client:
            return None

        if self._proc is not None or self._cdp_client is not None:
            preparing = self._gate.state == SessionState.PREPARING
            await self._close_internal()
            if preparing:
                self._gate.set_preparing()

        profile_dir = self._profile_dir
        if profile_dir:
            Path(profile_dir).mkdir(parents=True, exist_ok=True)

        self._proc = launch_chromium_process(
            port=self.port,
            user_data_dir=profile_dir,
            headless=settings.browser_headless,
        )

        self._cdp_client = CDPClient(port=self.port)
        try:
            await self._cdp_client.wait_until_ready()
        except BaseException:
            # Launch may leave an alive process that never serves CDP. Dispose
            # our process and client even on cancellation so a retry starts fresh.
            with CancelScope(shield=True):
                await self._close_internal()
                self._last_stop_reason = "failed"
            raise
        return None

    async def close(self) -> None:
        async with self._transition_lock:
            await self._close_internal()

    async def reset_if_active(self, context_id: int) -> bool:
        """Reset a failed context only while no request owns any browser page."""
        async with self._transition_lock:
            current = self.active_context
            if (
                current is None
                or current.context_id != context_id
                or self._in_flight > 0
                or current.in_flight > 0
                or any(ctx.in_flight > 0 for ctx in self._retiring_contexts)
            ):
                return False
            await self._close_internal()
            return True

    async def _close_internal(self) -> None:
        contexts = [*self._retiring_contexts]
        if self.active_context is not None:
            contexts.append(self.active_context)
        # Freeze new leases before any await in cleanup. A new request will wait
        # on the transition lock rather than acquire a page being disposed.
        self._gate.reset()
        self._last_stop_reason = "closed"
        pending = list(self._background_tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._background_tasks.clear()
        for context in contexts:
            await self.persist_context_cookies(context)
        self._retiring_contexts.clear()

        if self._cdp_client is not None:
            with suppress(Exception):
                await self._cdp_client.close()
            self._cdp_client = None

        if self._proc is not None:
            with suppress(Exception):
                self._proc.terminate()
            self._proc = None

        self._failed_snapshot_contexts.clear()


__all__ = [
    "BOTGUARD_BOOTSTRAP_PROMPT",
    "DEFAULT_BOOTSTRAP_TEMPLATE",
    "BrowserSession",
    "_is_login_page_url",
]
