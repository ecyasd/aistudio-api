"""Request-owned browser pages using the upstream WAA hooks and native fetch."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Route,
    async_playwright,
)

from aistudio_api.config import settings
from aistudio_api.domain.errors import SessionExpiredError, TransportError
from aistudio_api.infrastructure.waa.signer import extract_content_digest

if TYPE_CHECKING:
    from aistudio_api.infrastructure.gateway.session import SessionContext
    from aistudio_api.infrastructure.gateway.wire_types import AistudioContent

JS = Path(__file__).with_name("js")
_BROWSER_HEADERS = {
    "user-agent",
    "host",
    "cookie",
    "origin",
    "referer",
    "content-length",
    "accept-encoding",
}


@dataclass
class Lease:
    browser: Browser | None = None
    context: BrowserContext | None = None
    page: Page | None = None
    account: int | None = None
    bootstrapped: bool = False


_lease: ContextVar[Lease | None] = ContextVar("request_browser_lease", default=None)


class BrowserRuntime:
    def __init__(self) -> None:
        self.driver: Playwright | None = None
        self.browsers: set[Browser] = set()
        self.lock = asyncio.Lock()
        self.slots = asyncio.Semaphore(2)

    async def start(self, ctx: SessionContext) -> Browser:
        async with self.lock:
            if self.driver is None:
                self.driver = await async_playwright().start()
            seed = (
                10000
                + int(
                    hashlib.sha256(
                        (ctx.auth_file or ctx.cookies.get("SID", "default")).encode()
                    ).hexdigest()[:8],
                    16,
                )
                % 90000
            )
            browser = await self.driver.chromium.launch(
                executable_path=os.getenv(
                    "AISTUDIO_BROWSER_EXECUTABLE", "/opt/cloakbrowser/chrome"
                ),
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    f"--fingerprint={seed}",
                    "--fingerprint-platform=windows",
                    f"--fingerprint-timezone={settings.timezone}",
                    "--fingerprint-locale=en-US",
                ],
            )
            self.browsers.add(browser)
            return browser

    async def page(self, ctx: SessionContext, *, model: str, prompt: str = "") -> Page:
        lease = _lease.get()
        if lease is None:
            raise TransportError("Browser generation requires a request-owned scope")
        if lease.context is not None and lease.account != ctx.context_id:
            await lease.context.close()
            if lease.browser:
                await lease.browser.close()
                self.browsers.discard(lease.browser)
            lease.context = lease.page = None
            lease.bootstrapped = False
        if lease.page is None:
            browser = await self.start(ctx)
            lease.browser = browser
            lease.context = await browser.new_context(
                locale="en-US",
                timezone_id=settings.timezone,
                proxy={"server": settings.proxy_url} if settings.proxy_url else None,
            )
            lease.account = ctx.context_id
            await lease.context.add_cookies(
                [
                    {
                        "name": name,
                        "value": value,
                        "domain": ".google.com",
                        "path": "/",
                        "secure": True,
                    }
                    for name, value in ctx.cookies.items()
                ]
            )
            lease.page = await lease.context.new_page()

            # Never let page initialization create an unrequested generation or saved prompt.
            async def intercept(route: Route) -> None:
                url = route.request.url
                if "SavePrompt" in url or "CreatePrompt" in url:
                    await route.fulfill(
                        status=200, content_type="application/json+protobuf", body="[]"
                    )
                elif not lease.bootstrapped and "GenerateContent" in url:
                    await route.abort()
                else:
                    await route.continue_()

            await lease.page.route("**/*", intercept)
            await lease.page.goto(
                f"https://aistudio.google.com/prompts/new?authuser={ctx.auth_user}&model={model.removeprefix('models/')}",
                wait_until="domcontentloaded",
            )
            if "accounts.google.com" in lease.page.url:
                raise SessionExpiredError(
                    "Google session expired; import fresh cookies"
                )
        page = lease.page
        if prompt and not lease.bootstrapped:
            await page.wait_for_function(
                "() => !!window.default_MakerSuite", timeout=30000
            )
            result = await page.evaluate(
                JS.joinpath("install_hooks.js").read_text(encoding="utf-8-sig")
            )
            if not isinstance(result, str) or not (
                result.startswith("hooked:") or result == "already_hooked"
            ):
                raise TransportError("AI Studio WAA hook unavailable")
            await page.locator("textarea").first.fill(prompt)
            await page.locator("textarea").first.press("Control+Enter")
            await page.wait_for_function(
                "() => !!window.__AISTUDIO__?.service && !!window.__AISTUDIO__?.snapshot && !!window.__AISTUDIO__?.capturedTemplate?.body",
                timeout=45000,
            )
            lease.bootstrapped = True
        return page

    async def proof(
        self, contents: list[AistudioContent], ctx: SessionContext, model: str
    ) -> tuple[str, dict[str, str]]:
        prompt, digest = extract_content_digest(contents)
        page = await self.page(ctx, model=model, prompt=prompt or " ")
        value = await page.evaluate(
            JS.joinpath("snapshot_generate.js").read_text(encoding="utf-8-sig"),
            {"prompt": prompt, "hash": digest},
        )
        raw_headers = await page.evaluate(
            "() => window.__AISTUDIO__?.capturedTemplate?.headers || {}"
        )
        headers = (
            {str(key): str(value) for key, value in raw_headers.items()}
            if isinstance(raw_headers, dict)
            else {}
        )
        headers["User-Agent"] = await page.evaluate("() => navigator.userAgent")
        await page.evaluate(
            "() => { if(window.__AISTUDIO__) window.__AISTUDIO__.capturedTemplate = null; }"
        )
        if not isinstance(value, str) or not value:
            raise TransportError("Browser WAA proof is empty")
        return value, headers

    async def stream(
        self, *, ctx: SessionContext, url: str, headers: dict[str, str], body: str
    ) -> AsyncGenerator[tuple[str, object], None]:
        page = await self.page(ctx, model="gemini-3.7-flash")
        queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue(maxsize=1)
        name = "rpc_" + uuid.uuid4().hex

        async def deliver(_source: object, event: str, value: object) -> None:
            await queue.put((event, value))

        await page.expose_binding(name, deliver)
        clean_headers = {
            key: value
            for key, value in headers.items()
            if key.lower() not in _BROWSER_HEADERS
            and not key.lower().startswith("sec-")
        }

        async def fetch() -> None:
            try:
                await page.evaluate(
                    """async ({url, headers, body, name}) => {
                    const controller = new AbortController(); window[name + '_abort'] = controller;
                    try {
                        const response = await fetch(url, {method:'POST', headers, body, credentials:'include', signal:controller.signal});
                        await window[name]('status', response.status);
                        const reader = response.body.getReader();
                        try { while(true) { const {done, value} = await reader.read(); if(done) break;
                            let binary = ''; for(const byte of value) binary += String.fromCharCode(byte);
                            await window[name]('base64', btoa(binary));
                        } } finally { await reader.cancel(); reader.releaseLock(); }
                    } finally { delete window[name + '_abort']; }
                }""",
                    {"url": url, "headers": clean_headers, "body": body, "name": name},
                )
                await queue.put(("end", None))
            except Exception:
                await queue.put(("error", "Browser transport interrupted"))

        task = asyncio.create_task(fetch())
        try:
            while True:
                event, value = await queue.get()
                if event == "end":
                    return
                if event == "base64":
                    yield "chunk", base64.b64decode(str(value))
                else:
                    yield event, value
                if event == "error":
                    return
        finally:
            with suppress(Exception):
                await page.evaluate("name => window[name + '_abort']?.abort()", name)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            with suppress(Exception):
                await page.evaluate("name => { delete window[name]; }", name)

    async def close(self) -> None:
        for browser in list(self.browsers):
            await browser.close()
        self.browsers.clear()
        if self.driver:
            await self.driver.stop()
            self.driver = None


runtime = BrowserRuntime()


@asynccontextmanager
async def request_scope():
    async with runtime.slots:
        lease = Lease()
        token = _lease.set(lease)
        try:
            yield
        finally:
            try:
                if lease.browser:
                    await lease.browser.close()
                    runtime.browsers.discard(lease.browser)
            finally:
                _lease.reset(token)
