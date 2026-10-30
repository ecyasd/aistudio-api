"""Native Async Python Chrome DevTools Protocol (CDP) client.

Replaces Node.js / Playwright with a zero-Node, pure-Python async WebSocket client
directly communicating with Chromium over DevTools JSON-RPC protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections.abc import Callable

import httpx
import websockets
from anyio import CancelScope
from websockets.asyncio.client import ClientConnection

from aistudio_api.infrastructure.browser.scripts import build_selector_match_js
from aistudio_api.infrastructure.utils.logger import get_logger

log = get_logger("cdp")

BLOCKED_URL_PATTERNS: list[str] = [
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.webp",
    "*.gif",
    "*.svg",
    "*.ico",
    "*.woff",
    "*.woff2",
    "*.ttf",
    "*.eot",
    "*.otf",
    "*.mp4",
    "*.webm",
    "*.mp3",
    "*.ogg",
    "*.wav",
    "*.flac",
    "*monaco-editor*",
    "*codemirror*",
    "*vs/editor*",
    "*vs/base*",
    "*mathjax*",
    "*katex*",
    "*google-analytics.com*",
    "*googletagmanager.com*",
    "*play.google.com/log*",
    "*bat.bing.com*",
    "*adservice.google.com*",
    "*pagead2.googlesyndication.com*",
    "*doubleclick*",
    "*doubleclick.net*",
    "*recaptcha*",
    "*google.com/recaptcha*",
    "*SavePrompt*",
    "*CreatePrompt*",
]


class CDPError(Exception):
    """Exception raised for CDP JSON-RPC errors."""

    def __init__(self, code: int | str, message: str, data: object = None):
        super().__init__(f"CDP error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class CDPConnection:
    """Manages an async WebSocket connection to a Chrome DevTools Protocol target."""

    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self.ws: ClientConnection | None = None
        self._next_id = 1
        self._futures: dict[int, asyncio.Future[dict[str, object]]] = {}
        self._listeners: dict[str, list[Callable[[dict[str, object]], object]]] = {}
        self._recv_task: asyncio.Task[None] | None = None
        self._background_tasks: set[asyncio.Task[object]] = set()
        self._closed = False
        self._close_in_progress = False
        self._liveness_checker: Callable[[], bool] | None = None

    def set_liveness_checker(self, checker: Callable[[], bool] | None) -> None:
        """Register a callback that returns False if the underlying browser process is dead."""
        self._liveness_checker = checker

    def fail_pending_futures(self, exc: Exception) -> None:
        """Immediately fail all pending command futures with the given exception."""
        self._closed = True
        for fut in list(self._futures.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._futures.clear()

    async def connect(self, timeout_s: float = 10.0) -> None:
        """Establish WebSocket connection and start receive loop."""
        self.ws = await asyncio.wait_for(
            websockets.connect(
                self.ws_url,
                max_size=None,
                ping_interval=5,
                ping_timeout=5,
            ),
            timeout=timeout_s,
        )
        self._closed = False
        self._recv_task = asyncio.create_task(
            self._recv_loop(),
            name=f"cdp_recv_{self.ws_url[-12:]}",
        )

    async def _recv_loop(self) -> None:
        """Background loop receiving and dispatching CDP messages."""
        try:
            while self.ws and not self._closed:
                raw = await self.ws.recv()
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue

                req_id = msg.get("id")
                if req_id is not None and req_id in self._futures:
                    fut = self._futures.pop(req_id)
                    if not fut.done():
                        if "error" in msg:
                            err = msg["error"]
                            fut.set_exception(
                                CDPError(
                                    err.get("code", -1),
                                    err.get("message", "unknown error"),
                                    err.get("data"),
                                )
                            )
                        else:
                            fut.set_result(msg.get("result", {}))

                method = msg.get("method")
                if method in (
                    "Inspector.targetCrashed",
                    "Inspector.detached",
                    "Target.targetCrashed",
                ):
                    raw_params = msg.get("params")
                    params: dict[str, object] = (
                        raw_params if isinstance(raw_params, dict) else {}
                    )
                    reason = str(params.get("reason") or "")
                    if reason == "target_closed":
                        log.debug("CDP 目标正常关闭: %s", reason)
                    else:
                        log.warning(
                            "CDP 目标崩溃或断开: %s (参数=%s)",
                            method,
                            msg.get("params"),
                        )
                    self.fail_pending_futures(
                        RuntimeError(f"CDP target terminated: {method}")
                    )
                    return

                if method and method in self._listeners:
                    params = msg.get("params", {})
                    for cb in list(self._listeners[method]):
                        try:
                            res = cb(params)
                            if asyncio.iscoroutine(res):
                                task = asyncio.create_task(res)
                                self._background_tasks.add(task)
                                task.add_done_callback(self._event_task_done)
                        except Exception as e:
                            log.debug("CDP 事件监听器处理 %s 异常: %s", method, e)

        except asyncio.CancelledError:
            pass
        except websockets.ConnectionClosed as e:
            if not self._closed:
                log.debug("CDP WebSocket 连接已关闭: %s", e)
        except Exception as e:
            if not self._closed:
                log.debug("CDP 接收循环异常: %s", e)
        finally:
            self._closed = True
            # Reject pending futures immediately
            for fut in list(self._futures.values()):
                if not fut.done():
                    fut.set_exception(RuntimeError("CDP connection closed"))
            self._futures.clear()

    def _event_task_done(self, task: asyncio.Task[object]) -> None:
        self._background_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            # Event parameters and exception arguments may contain page data.
            log.debug("CDP asynchronous event handler failed")

    async def send(
        self,
        method: str,
        params: dict[str, object] | None = None,
        timeout_s: float = 30.0,
    ) -> dict[str, object]:
        """Send a JSON-RPC command over CDP and await result."""
        if not self.ws or self._closed:
            raise RuntimeError("CDP connection is not open")

        if self._liveness_checker and not self._liveness_checker():
            self.fail_pending_futures(RuntimeError("Browser process has died"))
            raise RuntimeError("Browser process has died before CDP command dispatch")

        req_id = self._next_id
        self._next_id += 1

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        fut: asyncio.Future[dict[str, object]] = loop.create_future()
        self._futures[req_id] = fut

        payload = {
            "id": req_id,
            "method": method,
            "params": params or {},
        }
        try:
            # Sending can block on WebSocket backpressure too. One deadline
            # covers dispatch and reply rather than starting after dispatch.
            async with asyncio.timeout_at(deadline):
                try:
                    serialized = json.dumps(payload, ensure_ascii=False)
                    # JSON permits escaped lone surrogates, while WebSocket
                    # text must be UTF-8. Keep Unicode compact on the usual path.
                    if re.search(r"[\ud800-\udfff]", serialized):
                        serialized = json.dumps(payload)
                    await self.ws.send(serialized)
                except Exception as exc:
                    self._futures.pop(req_id, None)
                    self.fail_pending_futures(
                        RuntimeError("CDP connection closed during command dispatch")
                    )
                    raise RuntimeError(
                        "CDP connection closed during command dispatch"
                    ) from exc

                if self._liveness_checker is None:
                    return await fut
                while True:
                    if not self._liveness_checker():
                        self.fail_pending_futures(RuntimeError("Browser process has died"))
                        raise RuntimeError(
                            f"Browser process died while awaiting CDP command {method}"
                        )
                    try:
                        return await asyncio.wait_for(asyncio.shield(fut), timeout=0.2)
                    except TimeoutError:
                        if fut.done():
                            return fut.result()
        finally:
            self._futures.pop(req_id, None)
            if not fut.done():
                fut.cancel()
            elif not fut.cancelled():
                # A crash may reject the reply while dispatch is still blocked.
                fut.exception()

    def on(
        self, event: str, callback: Callable[[dict[str, object]], object]
    ) -> Callable[[], None]:
        """Register an event listener. Returns an unsubscribe function."""
        if event not in self._listeners:
            self._listeners[event] = []
        self._listeners[event].append(callback)

        def unsubscribe() -> None:
            self.remove_listener(event, callback)

        return unsubscribe

    def remove_listener(
        self, event: str, callback: Callable[[dict[str, object]], object]
    ) -> None:
        """Remove an event listener."""
        if event in self._listeners and callback in self._listeners[event]:
            self._listeners[event].remove(callback)

    async def close(self) -> None:
        """Close WebSocket connection cleanly."""
        # Cancelled listeners may call close from their own cleanup. The first
        # closer owns the join; reentrant calls must not cancel/join each other.
        if self._close_in_progress:
            return
        self._close_in_progress = True
        try:
            await self._close_connection()
        finally:
            self._close_in_progress = False

    async def _close_connection(self) -> None:
        self.fail_pending_futures(RuntimeError("CDP connection closed"))
        current_task = asyncio.current_task()
        pending = [task for task in self._background_tasks if task is not current_task]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._background_tasks.difference_update(pending)
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._recv_task
        if self.ws:
            # Retain the socket on failure so its owner can retry cleanup.
            await self.ws.close()
            self.ws = None


class CDPPage:
    def __init__(
        self,
        cdp: CDPConnection,
        target_id: str,
        browser_context_id: str | None = None,
        browser_cdp: CDPConnection | None = None,
    ):
        self.cdp = cdp
        self.target_id = target_id
        self.browser_context_id = browser_context_id
        self._browser_cdp = browser_cdp
        self._is_closed = False
        self._last_url: str = ""
        self._unsubscribers: list[Callable[[], None]] = []
        self.has_stream_binding: bool = False
        self._registered_bindings: set[str] = set()
        self._binding_lock = asyncio.Lock()
        self._cached_cookies: dict[str, str] | None = None
        self._auth_file_path: str | None = None
        self._auth_file_version: str | None = None
        self._auth_generation: str | None = None

    def _track_listener(self, unsub: Callable[[], None]) -> Callable[[], None]:
        self._unsubscribers.append(unsub)

        def wrapped() -> None:
            if unsub in self._unsubscribers:
                self._unsubscribers.remove(unsub)
            unsub()

        return wrapped

    @property
    def url(self) -> str:
        return self._last_url

    def is_closed(self) -> bool:
        return self._is_closed or self.cdp._closed

    async def is_alive(self, timeout_s: float = 1.0) -> bool:
        """Fast non-blocking probe to verify if the page target and WebSocket are responsive."""
        if self.is_closed():
            return False
        try:
            res = await self.cdp.send(
                "Runtime.evaluate",
                {"expression": "1", "returnByValue": True},
                timeout_s=timeout_s,
            )
            return bool(res and not res.get("exceptionDetails"))
        except Exception:
            return False

    def set_liveness_checker(self, checker: Callable[[], bool] | None) -> None:
        """Bind underlying browser process liveness checker to CDP connection."""
        self.cdp.set_liveness_checker(checker)

    async def init_domains(self, block_assets: bool = True) -> None:
        """Enable required CDP domains and install kernel-level asset filters."""
        await self.cdp.send("Page.enable")
        await self.cdp.send("Runtime.enable")
        await self.cdp.send("Network.enable")
        await self.cdp.send("DOM.enable")
        # Install native stream push binding
        with contextlib.suppress(Exception):
            await self.add_binding("__aistudio_stream_push__")

        # Track navigation URLs
        def on_navigated(params: dict[str, object]) -> None:
            raw_frame = params.get("frame")
            frame = raw_frame if isinstance(raw_frame, dict) else {}
            if not frame.get("parentId"):  # Main frame
                self._last_url = str(frame.get("url") or "")

        self._track_listener(self.cdp.on("Page.frameNavigated", on_navigated))

        # Listen for renderer crash and inspector detached events to fail fast
        def on_crashed(params: dict[str, object]) -> None:
            log.warning("CDP 页面渲染器崩溃: %s", params)
            self._is_closed = True
            self.cdp.fail_pending_futures(
                RuntimeError("Page target crashed (renderer terminated)")
            )

        def on_detached(params: dict[str, object]) -> None:
            reason = str(params.get("reason") or "detached")
            if reason == "target_closed":
                log.debug("CDP 页面检查器已分离 (目标关闭)")
            else:
                log.warning("CDP 页面检查器已分离: %s", reason)
            self._is_closed = True
            self.cdp.fail_pending_futures(
                RuntimeError(f"Page inspector detached: {reason}")
            )

        self._track_listener(self.cdp.on("Inspector.targetCrashed", on_crashed))
        self._track_listener(self.cdp.on("Target.targetCrashed", on_crashed))
        self._track_listener(self.cdp.on("Inspector.detached", on_detached))

        if block_assets:
            await self.set_blocked_urls(BLOCKED_URL_PATTERNS)

    async def set_blocked_urls(self, patterns: list[str]) -> None:
        """Block matching resource URLs inside Chromium's C++ network service."""
        try:
            await self.cdp.send("Network.setBlockedURLs", {"urls": patterns})
            log.debug("已配置网络资源黑名单拦截 (%d 条规则)", len(patterns))
        except Exception as e:
            log.warning("配置网络资源黑名单拦截失败: %s", e)

    async def add_binding(self, name: str) -> None:
        """Expose a global function in page JS that dispatches Runtime.bindingCalled events."""
        async with self._binding_lock:
            if name not in self._registered_bindings:
                await self.cdp.send("Runtime.addBinding", {"name": name})
                self._registered_bindings.add(name)

    def on_binding(
        self, name: str, callback: Callable[[str], object]
    ) -> Callable[[], None]:
        """Listen for Runtime.bindingCalled events for a specific binding name."""

        def listener(params: dict[str, object]) -> None:
            if params.get("name") == name:
                payload = str(params.get("payload") or "")
                callback(payload)

        return self._track_listener(self.cdp.on("Runtime.bindingCalled", listener))

    async def evaluate(
        self,
        expression: str,
        args: object = None,
        await_promise: bool = True,
        return_by_value: bool = True,
        timeout_s: float = 30.0,
    ) -> object:
        """Evaluate a JS expression or function in the page's main world."""
        expr = expression.strip()
        if expr.startswith("mw:"):
            expr = expr[3:].strip()

        if args is not None:
            clean_expr = expr.rstrip()
            while clean_expr.endswith(";"):
                clean_expr = clean_expr[:-1].rstrip()
            expr = f"({clean_expr})({json.dumps(args, ensure_ascii=False)})"
        else:
            is_arrow = bool(
                re.match(r"^(?:async\s+)?(?:\([^()]*\)|[a-zA-Z_$][\w$]*)\s*=>", expr)
            )
            is_fn = bool(re.match(r"^(?:async\s+)?function\b", expr))
            if is_arrow or is_fn:
                clean_expr = expr.rstrip()
                while clean_expr.endswith(";"):
                    clean_expr = clean_expr[:-1].rstrip()
                expr = f"({clean_expr})()"
        res = await self.cdp.send(
            "Runtime.evaluate",
            {
                "expression": expr,
                "awaitPromise": await_promise,
                "returnByValue": return_by_value,
                "includeCommandLineAPI": True,
            },
            timeout_s=timeout_s,
        )

        if "exceptionDetails" in res:
            raw_exc = res["exceptionDetails"]
            exc: dict[str, object] = raw_exc if isinstance(raw_exc, dict) else {}
            raw_inner = exc.get("exception")
            inner: dict[str, object] = raw_inner if isinstance(raw_inner, dict) else {}
            exc_text = str(
                inner.get("description") or exc.get("text") or "JS exception"
            )
            raise RuntimeError(f"CDP JS evaluation error: {exc_text}")

        raw_result_obj = res.get("result")
        result_obj: dict[str, object] = (
            raw_result_obj if isinstance(raw_result_obj, dict) else {}
        )
        val_type = result_obj.get("type")
        if val_type == "undefined":
            return None
        return result_obj.get("value")

    async def goto(
        self,
        url: str,
        timeout_s: float = 30.0,
        wait_until: str = "domcontentloaded",
    ) -> None:
        """Navigate to a URL and wait until DOM is loaded."""
        loop = asyncio.get_running_loop()
        nav_done: asyncio.Future[bool] = loop.create_future()

        event_name = (
            "Page.loadEventFired"
            if wait_until == "load"
            else "Page.domContentEventFired"
        )

        def on_event(_params: dict[str, object]) -> None:
            if not nav_done.done():
                nav_done.set_result(True)

        unsub = self.cdp.on(event_name, on_event)
        try:
            nav_res = await self.cdp.send(
                "Page.navigate", {"url": url}, timeout_s=timeout_s
            )
            if "errorText" in nav_res:
                raise RuntimeError(f"Navigation failed: {nav_res['errorText']}")

            # Also update current URL
            self._last_url = url
            await asyncio.wait_for(nav_done, timeout=timeout_s)
        except TimeoutError:
            # Timeout during DOM load is non-fatal for SPAs, update URL from location
            pass
        finally:
            unsub()
            # Update actual URL from window.location
            try:
                actual_url = await self.evaluate(
                    "() => window.location.href", timeout_s=5.0
                )
                if actual_url is not None:
                    self._last_url = str(actual_url)
            except Exception:
                pass

    async def title(self) -> str:
        """Get document title."""
        try:
            val = await self.evaluate("() => document.title", timeout_s=5.0)
            return str(val) if val is not None else ""
        except Exception:
            return ""

    async def content(self) -> str:
        """Get full HTML content of the page."""
        try:
            val = await self.evaluate(
                "() => document.documentElement.outerHTML", timeout_s=10.0
            )
            return str(val) if val is not None else ""
        except Exception:
            return ""

    async def wait_for_timeout(self, ms: float) -> None:
        """Wait for specified milliseconds."""
        await asyncio.sleep(ms / 1000.0)

    async def wait_for_selector(
        self,
        selector: str,
        timeout_s: float = 20.0,
        interval_s: float = 0.3,
    ) -> bool:
        """Wait until element matching selector is present in DOM."""
        deadline = asyncio.get_running_loop().time() + timeout_s
        check_expr = f"() => !!{build_selector_match_js(selector)}"
        while asyncio.get_running_loop().time() < deadline:
            try:
                found = await self.evaluate(check_expr, timeout_s=5.0)
                if found:
                    return True
            except Exception:
                pass
            await asyncio.sleep(interval_s)
        raise TimeoutError(f"Selector {selector!r} not found within {timeout_s}s")

    async def query_selector(self, selector: str) -> bool:
        """Check if an element exists."""
        check_expr = f"() => !!{build_selector_match_js(selector)}"
        try:
            return bool(await self.evaluate(check_expr))
        except Exception:
            return False

    async def click(self, selector: str, timeout_s: float = 5.0) -> bool:
        """Click an element matching selector."""
        expr = f"""() => {{
            const el = {build_selector_match_js(selector)};
            if (!el) return false;
            el.click();
            return true;
        }}"""
        return bool(await self.evaluate(expr, timeout_s=timeout_s))

    async def fill(self, selector: str, value: str) -> bool:
        """Fill an input or textarea element with value."""
        expr = f"""(args) => {{
            const el = {build_selector_match_js(selector)};
            if (!el) return false;
            el.value = args.value;
            el.dispatchEvent(new Event('input', {{ bubbles: true }}));
            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
            return true;
        }}"""
        return bool(await self.evaluate(expr, {"value": value}))

    async def send_control_enter(self, selector: str = "textarea") -> bool:
        """Focus target and dispatch Control+Enter shortcut."""
        expr = f"""() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (el) {{
                el.focus();
                const evDown = new KeyboardEvent('keydown', {{
                    key: 'Enter',
                    code: 'Enter',
                    keyCode: 13,
                    which: 13,
                    ctrlKey: true,
                    bubbles: true,
                    cancelable: true,
                }});
                el.dispatchEvent(evDown);
                const evUp = new KeyboardEvent('keyup', {{
                    key: 'Enter',
                    code: 'Enter',
                    keyCode: 13,
                    which: 13,
                    ctrlKey: true,
                    bubbles: true,
                    cancelable: true,
                }});
                el.dispatchEvent(evUp);
                return true;
            }}
            return false;
        }}"""
        try:
            # Also send raw Input.dispatchKeyEvent with Ctrl modifier for Chromium shortcut dispatch
            await self.evaluate(expr)
            await self.cdp.send(
                "Input.dispatchKeyEvent",
                {
                    "type": "rawKeyDown",
                    "modifiers": 2,  # Ctrl modifier
                    "windowsVirtualKeyCode": 13,
                    "code": "Enter",
                    "key": "Enter",
                    "text": "\r",
                    "unmodifiedText": "\r",
                },
            )
            await self.cdp.send(
                "Input.dispatchKeyEvent",
                {
                    "type": "keyUp",
                    "modifiers": 2,
                    "windowsVirtualKeyCode": 13,
                    "code": "Enter",
                    "key": "Enter",
                },
            )
            return True
        except Exception as e:
            log.debug("发送 Ctrl+Enter 失败: %s", e)
            return False

    async def get_cookies(self) -> list[dict[str, object]]:
        """Read the entire owned context, including accounts-only credentials.

        Network.getCookies without URLs returns only the page/subframe cookies;
        saving that partial view would remove other Google login cookies.
        """
        if self.browser_context_id and self._browser_cdp:
            res = await self._browser_cdp.send(
                "Storage.getCookies", {"browserContextId": self.browser_context_id}
            )
        else:
            res = await self.cdp.send("Storage.getCookies")
        raw_cookies = res.get("cookies")
        if isinstance(raw_cookies, list):
            self._cached_cookies = {
                str(c.get("name") or ""): str(c.get("value") or "")
                for c in raw_cookies
                if c.get("name")
            }
            return raw_cookies
        return []

    async def set_cookies(
        self, cookies: list[dict[str, object]] | list[dict[str, str]]
    ) -> None:
        """Inject cookies via CDP Network domain."""
        if not cookies:
            return

        formatted: list[dict[str, object]] = []
        for c in cookies:
            c_name = str(c.get("name", ""))
            item: dict[str, object] = {
                "name": c_name,
                "value": str(c.get("value", "")),
                "path": str(c.get("path", "/")),
                "secure": bool(c.get("secure", True)),
                "httpOnly": bool(c.get("httpOnly", False)),
            }
            domain = c.get("domain")
            url = c.get("url")
            if domain is not None:
                dom_str = str(domain)
                if c_name.startswith("__Host-"):
                    # Host-only cookies must not specify domain with leading dot
                    clean_domain = dom_str.lstrip(".")
                    item["url"] = f"https://{clean_domain}/"
                else:
                    item["domain"] = dom_str
            elif url is not None:
                item["url"] = str(url)
            else:
                item["domain"] = ".google.com"

            same_site = c.get("sameSite")
            if same_site is not None:
                ss_str = str(same_site).lower()
                if ss_str == "none":
                    item["sameSite"] = "None"
                elif ss_str == "lax":
                    item["sameSite"] = "Lax"
                elif ss_str == "strict":
                    item["sameSite"] = "Strict"

            if "expires" in c and c["expires"] is not None:
                # CDP snapshots use expires=-1/session=true for session cookies.
                # Omitting expiry preserves that meaning on reinjection.
                expires = float(str(c["expires"]))
                if not c.get("session") and expires > 0:
                    item["expires"] = expires

            partition_key = c.get("partitionKey")
            if isinstance(partition_key, dict) and partition_key.get("topLevelSite"):
                item["partitionKey"] = {
                    "topLevelSite": str(partition_key["topLevelSite"]),
                    "hasCrossSiteAncestor": bool(partition_key.get("hasCrossSiteAncestor", False)),
                }

            formatted.append(item)

        try:
            await self.cdp.send("Network.setCookies", {"cookies": formatted})
            log.debug("已通过 CDP 成功注入 %d 个 Cookie", len(formatted))
        except Exception as e:
            log.warning("批量通过 CDP 注入 Cookie 失败 (%s)，正在尝试逐条重试", e)
            for item in formatted:
                try:
                    await self.cdp.send("Network.setCookies", {"cookies": [item]})
                except Exception as ind_e:
                    log.debug("设置 Cookie %s 失败: %s", item.get("name"), ind_e)

        self._cached_cookies = {
            str(c.get("name") or ""): str(c.get("value") or "")
            for c in cookies
            if c.get("name")
        }

    async def clear_cookies(self) -> None:
        """Clear only this page's browser context cookies."""
        self._cached_cookies = None
        try:
            if self.browser_context_id and self._browser_cdp:
                await self._browser_cdp.send(
                    "Storage.clearCookies",
                    {"browserContextId": self.browser_context_id},
                )
            else:
                await self.cdp.send("Network.clearBrowserCookies")
        except Exception as e:
            log.debug("清除浏览器 Cookie 失败: %s", e)

    def on_request(
        self, callback: Callable[[dict[str, object]], object]
    ) -> Callable[[], None]:
        """Listen to outgoing HTTP requests."""

        def listener(params: dict[str, object]) -> None:
            raw_req = params.get("request")
            req: dict[str, object] = raw_req if isinstance(raw_req, dict) else {}
            data: dict[str, object] = {
                "request_id": params.get("requestId"),
                "url": str(req.get("url") or ""),
                "method": str(req.get("method") or "GET"),
                "headers": req.get("headers") or {},
                "post_data": str(req.get("postData") or ""),
            }
            callback(data)

        return self._track_listener(self.cdp.on("Network.requestWillBeSent", listener))

    def on_response(
        self, callback: Callable[[dict[str, object]], object]
    ) -> Callable[[], None]:
        """Listen to incoming HTTP responses."""

        def listener(params: dict[str, object]) -> None:
            raw_resp = params.get("response")
            resp: dict[str, object] = raw_resp if isinstance(raw_resp, dict) else {}
            data: dict[str, object] = {
                "request_id": params.get("requestId"),
                "url": str(resp.get("url") or ""),
                "status": int(str(resp.get("status") or 0)),
                "headers": resp.get("headers") or {},
                "mime_type": str(resp.get("mimeType") or ""),
            }
            callback(data)

        return self._track_listener(self.cdp.on("Network.responseReceived", listener))

    async def get_response_body(self, request_id: str) -> str:
        """Get response body for a completed request."""
        try:
            res = await self.cdp.send(
                "Network.getResponseBody", {"requestId": request_id}
            )
            body = str(res.get("body") or "")
            if res.get("base64Encoded"):
                import base64

                return base64.b64decode(body).decode("utf-8", errors="replace")
            return body
        except Exception as e:
            log.debug("获取响应体失败 (req_id=%s): %s", request_id, e)
            return ""

    async def collect_garbage(self) -> None:
        """Trigger V8 garbage collection via HeapProfiler or window.gc()."""
        try:
            await self.cdp.send("HeapProfiler.collectGarbage", timeout_s=5.0)
        except Exception:
            with contextlib.suppress(Exception):
                await self.evaluate(
                    "() => { if (typeof window.gc === 'function') window.gc(); }",
                    timeout_s=5.0,
                )

    async def close(self) -> None:
        """Close this page target and clear all registered listeners."""
        self._is_closed = True
        for unsub in list(self._unsubscribers):
            with contextlib.suppress(Exception):
                unsub()
        self._unsubscribers.clear()
        try:
            with contextlib.suppress(Exception):
                await self.cdp.send("Page.close")
        finally:
            # A cancelled Page.close command must still release its socket,
            # pending commands and event handlers before the owner retries.
            await self.cdp.close()


class CDPClient:
    """Manages connection and discovery of CDP targets on a Chromium debugging port."""

    def __init__(self, port: int, host: str = "127.0.0.1"):
        self.port = port
        self.host = host
        self.base_url = f"http://{host}:{port}"
        self.page: CDPPage | None = None
        self._browser_cdp: CDPConnection | None = None
        self._owned_pages: dict[str, CDPPage] = {}
        self._browser_contexts: set[str] = set()

    async def get_version(self) -> dict[str, object]:
        """Fetch /json/version info."""
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{self.base_url}/json/version")
            resp.raise_for_status()
            return resp.json()

    async def is_endpoint_alive(self, timeout_s: float = 1.0) -> bool:
        """Fast check to verify if Chromium's CDP HTTP port is responding."""
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.get(f"{self.base_url}/json/version")
                return resp.status_code == 200
        except Exception:
            return False

    async def get_targets(self) -> list[dict[str, object]]:
        """Fetch list of active targets via /json/list."""
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{self.base_url}/json/list")
            resp.raise_for_status()
            return resp.json()

    async def wait_until_ready(self, timeout_s: float = 15.0) -> None:
        """Wait until Chromium's CDP endpoint responds to HTTP requests."""
        deadline = asyncio.get_running_loop().time() + timeout_s
        last_err: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                await self.get_version()
                return
            except Exception as e:
                last_err = e
                await asyncio.sleep(0.2)
        raise TimeoutError(
            f"Chromium CDP endpoint on port {self.port} not ready after {timeout_s}s: {last_err}"
        )

    async def connect_page(self, block_assets: bool = True) -> CDPPage:
        """Connect to an existing page target or create one with auto-retry on transient drops."""
        await self.wait_until_ready()
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                targets = await self.get_targets()
                page_targets = [
                    t
                    for t in targets
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
                ]
                page_target = None
                if page_targets:
                    page_target = page_targets[0]
                    # Prune any spare about:blank or crashed tabs to save ~50MB RAM each
                    for spare in page_targets[1:]:
                        spare_id = str(spare.get("id") or "")
                        if spare_id:
                            with contextlib.suppress(Exception):
                                async with httpx.AsyncClient(timeout=2.0) as client:
                                    await client.put(
                                        f"{self.base_url}/json/close/{spare_id}"
                                    )
                if not page_target:
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        resp = await client.put(f"{self.base_url}/json/new")
                        resp.raise_for_status()
                        page_target = resp.json()

                ws_url = str(page_target.get("webSocketDebuggerUrl") or "")
                target_id = str(page_target.get("id") or "")

                conn = CDPConnection(ws_url)
                await conn.connect()

                page = CDPPage(conn, target_id)
                await page.init_domains(block_assets=block_assets)
                self.page = page
                return page
            except Exception as e:
                last_exc = e
                log.debug("连接页面第 %d 次尝试失败: %s", attempt + 1, e)
                if attempt < 2:
                    await asyncio.sleep(0.3)
        raise RuntimeError(
            f"Failed to connect page target after 3 attempts: {last_exc}"
        ) from last_exc

    async def create_new_page(self, block_assets: bool = True) -> CDPPage:
        """Create a page in its own disposable browser context and Cookie jar."""
        await self.wait_until_ready()
        if self._browser_cdp is not None and getattr(self._browser_cdp, "_closed", False) is True:
            await self._browser_cdp.close()
            self._browser_cdp = None
            self._browser_contexts.clear()
        if self._browser_cdp is None:
            version = await self.get_version()
            browser_ws = str(version.get("webSocketDebuggerUrl") or "")
            if not browser_ws:
                raise RuntimeError("Browser CDP WebSocket URL is unavailable")
            browser_connection = CDPConnection(browser_ws)
            try:
                await browser_connection.connect()
            except BaseException:
                with CancelScope(shield=True):
                    await browser_connection.close()
                raise
            self._browser_cdp = browser_connection
        browser_cdp = self._browser_cdp

        async def allocate_context() -> str:
            # Keep ownership of a remote side effect until its reply arrives.
            # Cancelling the caller must not discard the contextId future.
            result: dict[str, object] = {}
            with CancelScope(shield=True):
                result = await browser_cdp.send(
                    "Target.createBrowserContext", {"disposeOnDetach": True}
                )
            created_id = str(result.get("browserContextId") or "")
            if not created_id:
                raise RuntimeError("Browser did not create an isolated context")
            self._browser_contexts.add(created_id)
            return created_id

        allocation = asyncio.create_task(allocate_context(), name="cdp-context-create")
        try:
            context_id = await asyncio.shield(allocation)
        except BaseException:
            with CancelScope(shield=True):
                # Join this bounded CDP operation, including repeated external
                # cancellation, and dispose only the candidate it created.
                while not allocation.done():
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await asyncio.shield(allocation)
                if not allocation.cancelled():
                    with contextlib.suppress(Exception):
                        await self.dispose_browser_context(allocation.result())
            raise
        conn: CDPConnection | None = None
        try:
            target = await browser_cdp.send(
                "Target.createTarget",
                {"url": "about:blank", "browserContextId": context_id},
            )
            target_id = str(target.get("targetId") or "")
            ws_url = ""
            targets: list[dict[str, object]] = []
            for attempt in range(3):
                targets = await self.get_targets()
                page_target = next((t for t in targets if t.get("id") == target_id), {})
                ws_url = str(page_target.get("webSocketDebuggerUrl") or "")
                if ws_url:
                    break
                if attempt < 2:
                    await asyncio.sleep(0.05)
            if not target_id or not ws_url:
                raise RuntimeError("Isolated page CDP endpoint is unavailable")
            conn = CDPConnection(ws_url)
            await conn.connect()
            page = CDPPage(conn, target_id, context_id, browser_cdp)
            await page.init_domains(block_assets=block_assets)
            self._owned_pages[target_id] = page
            # Remove Chromium's spare default tab only after a workload tab exists.
            for spare in targets:
                spare_id = str(spare.get("id") or "")
                if spare.get("type") == "page" and spare_id not in self._owned_pages:
                    await self.close_target(spare_id)
            return page
        except BaseException:
            # The session has not received this page yet. It cannot dispose our
            # candidate if cancellation recurs during one of these awaits.
            with CancelScope(shield=True):
                if conn is not None:
                    with contextlib.suppress(Exception):
                        await conn.close()
                with contextlib.suppress(Exception):
                    await self.dispose_browser_context(context_id)
                if context_id not in self._browser_contexts:
                    for owned_id, owned in list(self._owned_pages.items()):
                        if owned.browser_context_id == context_id:
                            self._owned_pages.pop(owned_id)
            raise

    async def dispose_browser_context(self, context_id: str) -> None:
        """Destroy the owned context, including every tab and its Cookie jar."""
        if context_id in self._browser_contexts and self._browser_cdp:
            await self._browser_cdp.send(
                "Target.disposeBrowserContext", {"browserContextId": context_id}
            )
            self._browser_contexts.discard(context_id)

    async def close_target(self, target_id: str) -> None:
        """Close a specific target ID via /json/close/{target_id}."""
        page = self._owned_pages.get(target_id)
        if page is not None:
            # A failed/closed target can still own a socket and listeners. Keep
            # ownership until both page cleanup and context disposal complete so
            # cancellation or a failed disposal can be retried safely.
            await page.close()
            if getattr(page.cdp, "_close_in_progress", False) is True:
                # Concurrent calls and cancelled listeners can reenter close.
                # They return without joining the owning closer; only that
                # closer may dispose the context and remove page ownership.
                return
            if page.browser_context_id:
                await self.dispose_browser_context(page.browser_context_id)
            if self._owned_pages.get(target_id) is page:
                self._owned_pages.pop(target_id)
            return
        with contextlib.suppress(Exception):
            async with httpx.AsyncClient(timeout=3.0) as client:
                await client.put(f"{self.base_url}/json/close/{target_id}")

    async def close(self) -> None:
        """Close active page and release resources."""
        for target_id in list(self._owned_pages):
            with contextlib.suppress(Exception):
                await self.close_target(target_id)
        for context_id in list(self._browser_contexts):
            with contextlib.suppress(Exception):
                await self.dispose_browser_context(context_id)
        if self.page:
            await self.page.close()
            self.page = None
        if self._browser_cdp:
            await self._browser_cdp.close()
            self._browser_cdp = None
