"""Authentication, cookies, and identity verification helpers for AI Studio sessions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from aistudio_api.domain.errors import (
    AccountIdentityMismatchError,
    AccountIdentityUnavailableError,
    SessionExpiredError,
)
from aistudio_api.infrastructure.account.storage_state import (
    atomic_write_json,
    auth_file_version,
)
from aistudio_api.infrastructure.browser.cdp_client import CDPPage
from aistudio_api.infrastructure.browser.scripts import (
    ACCOUNT_IDENTITY_JS,
    EXTRACT_EMAIL_JS,
)
from aistudio_api.infrastructure.utils.logger import get_logger

log = get_logger("session.auth")

AI_STUDIO_URL = (
    "https://aistudio.google.com/prompts/new_chat?model=gemini-3.7-flash&temporary=true"
)
AI_STUDIO_URL_FALLBACK = (
    "https://aistudio.google.com/app/prompts/new_chat?temporary=true"
)
GOOGLE_LOGIN_BOOTSTRAP_URL = (
    "https://accounts.google.com/ServiceLogin?continue=https://aistudio.google.com"
)


def is_login_page_url(url: str | None) -> bool:
    """True if URL matches Google account signin / login paths."""
    if not url:
        return False
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        path = (parsed.path or "").lower()
        query = (parsed.query or "").lower()
    except Exception:
        return False
    if host == "accounts.google.com" or host.endswith(".accounts.google.com"):
        return True
    return (
        "signin" in path
        or "servicelogin" in path
        or "accountchooser" in path
        or "signin" in query
        or "servicelogin" in query
    )


def derive_profile_dir(auth_file: str | None) -> str | None:
    """Derive user data profile directory corresponding to an auth file."""
    if not auth_file:
        return None
    return str(Path(auth_file).resolve().parent / "profile")


def get_auth_user_from_meta(auth_file: str | None) -> str:
    """Read auth_user index from meta.json adjacent to auth_file."""
    if auth_file:
        try:
            meta_path = Path(auth_file).parent / "meta.json"
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                return str(meta.get("auth_user") or "0")
        except Exception:
            pass
    return "0"


def get_aistudio_urls(
    auth_user: str = "0", model: str = "gemini-3.7-flash"
) -> list[str]:
    """Build AI Studio navigation URLs for target auth_user."""
    if auth_user and auth_user != "0":
        return [
            f"https://aistudio.google.com/u/{auth_user}/prompts/new_chat?model={model}&temporary=true",
            f"https://aistudio.google.com/u/{auth_user}/app/prompts/new_chat?temporary=true",
        ]
    return [AI_STUDIO_URL, AI_STUDIO_URL_FALLBACK]


def get_auth_file_version(auth_file: str | None) -> str | None:
    return auth_file_version(auth_file) if auth_file else None


def bind_auth_file(
    page: CDPPage, auth_file: str, *, expected_version: str | None = None
) -> bool:
    """Bind a page to the credential generation it actually imported."""
    version = get_auth_file_version(auth_file)
    if version is None or (expected_version is not None and version != expected_version):
        return False
    page._auth_file_path = str(Path(auth_file).resolve())
    page._auth_file_version = version
    return True


async def load_cookies_to_page(page: CDPPage, auth_file: str | None) -> bool:
    """Inject cookies from auth_file into target page with session-conflict filtering."""
    if not auth_file or not Path(auth_file).is_file():
        return False
    try:
        raw_data = Path(auth_file).read_bytes()
        data = json.loads(raw_data)
        cookies = data.get("cookies") or []
        if not isinstance(cookies, list) or not cookies:
            return False

        # Record the exact bytes loaded, before yielding to a concurrent import.
        page._auth_file_path = str(Path(auth_file).resolve())
        page._auth_file_version = hashlib.sha256(raw_data).hexdigest()

        await page.clear_cookies()

        cleaned_cookies = [
            c
            for c in cookies
            if str(c.get("name") or "")
            not in {
                "OSID",
                "__Secure-OSID",
                "SIDCC",
                "__Secure-1PSIDCC",
                "__Secure-3PSIDCC",
            }
        ]
        await page.set_cookies(cleaned_cookies or cookies)
        log.info(
            "已向页面载入 %d 个 Cookie (auth_file=%s)",
            len(cleaned_cookies or cookies),
            auth_file,
        )
        return True
    except Exception as e:
        log.warning("向页面注入 Cookie 失败: %s", e)
        return False


async def extract_account_email(page: CDPPage | None) -> str | None:
    """Extract authenticated Google email from page DOM."""
    if page is None or page.is_closed():
        return None
    try:
        raw_email = await page.evaluate(EXTRACT_EMAIL_JS)
        if raw_email and isinstance(raw_email, str) and "@" in raw_email:
            return raw_email.strip()
    except Exception as e:
        log.debug("从页面提取账号邮箱失败: %s", e)
    return None


IdentityStatus = Literal["verified", "mismatch", "unavailable"]


async def _account_identity_status(page: CDPPage, auth_file: str | None) -> IdentityStatus:
    """Compare live active-account evidence without guessing from unrelated DOM."""
    if not auth_file:
        bound_file = vars(page).get("_auth_file_path")
        auth_file = bound_file if isinstance(bound_file, str) else None
    if not auth_file:
        # Unpinned pages are used only by explicit account-email discovery.
        # Request contexts always supply their account metadata.
        return "verified"
    meta_path = Path(auth_file).parent / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            return "unavailable"
        auth_user = str(meta.get("auth_user", "0"))
        expected_email = meta.get("email") or ""
        if not re.fullmatch(r"[0-9]+", auth_user) or not isinstance(expected_email, str):
            return "unavailable"
        expected_email = expected_email.strip().lower()
        snapshot = await page.evaluate(ACCOUNT_IDENTITY_JS, timeout_s=2.0)
    except Exception:
        return "unavailable"
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("url"), str):
        return "unavailable"
    try:
        current = urlsplit(snapshot["url"])
        if (
            current.scheme != "https"
            or current.hostname != "aistudio.google.com"
            or is_login_page_url(snapshot["url"])
        ):
            return "unavailable"
        route = re.match(r"^/u/([0-9]+)(?:/|$)", current.path)
        if current.path.startswith("/u/") and not route:
            return "unavailable"
        query = parse_qs(current.query, keep_blank_values=True).get("authuser", [])
        if len(query) > 1 or (query and not re.fullmatch(r"[0-9]+", query[0])):
            return "unavailable"
        if route and query and int(route[1]) != int(query[0]):
            return "unavailable"
        actual_user = int(route[1] if route else query[0] if query else "0")
        if actual_user != int(auth_user):
            return "mismatch"
    except (ValueError, TypeError):
        return "unavailable"
    discovered = snapshot.get("email")
    if not isinstance(discovered, str) or "@" not in discovered:
        return "unavailable"
    if expected_email and discovered.strip().lower() != expected_email:
        return "mismatch"
    return "verified"


async def verify_account_identity(page: CDPPage, auth_file: str | None) -> bool:
    """Checkpoint only when live account and route agree with the selected file."""
    return await _account_identity_status(page, auth_file) == "verified"


async def save_browser_cookies(
    page: CDPPage,
    auth_file: str | None,
    cookies: list[dict[str, object]] | None = None,
) -> bool:
    """Checkpoint a verified full jar without overwriting a newer import."""
    if not auth_file:
        return False
    try:
        auth_path = Path(auth_file).resolve()
        bound_path = vars(page).get("_auth_file_path")
        version = vars(page).get("_auth_file_version")
        if bound_path != str(auth_path) or not isinstance(version, str):
            return False
        if (
            page.is_closed()
            or not await verify_account_identity(page, auth_file)
        ):
            return False
        current_cookies = cookies
        if current_cookies is None:
            current_cookies = await page.get_cookies()
        if not isinstance(current_cookies, list) or not current_cookies:
            return False
        if any(not isinstance(cookie, dict) or not cookie.get("name") for cookie in current_cookies):
            return False
        existing = json.loads(auth_path.read_bytes())
        old_cookies = existing.get("cookies") or []
        auth_families = (
            {"SID", "__Secure-1PSID", "__Secure-3PSID"},
            {"APISID", "SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID"},
        )
        old_names = {cookie.get("name") for cookie in old_cookies if isinstance(cookie, dict)}
        new_names = {cookie.get("name") for cookie in current_cookies if cookie.get("value")}
        if not any(new_names & family for family in auth_families) or any(
            old_names & family and not new_names & family for family in auth_families
        ):
            # Empty/partial snapshots must never erase the last usable export.
            # A verified full jar may legitimately retire one cookie in a
            # family; do not resurrect it or block rotation of its replacement.
            return False
        data = {"cookies": current_cookies, "origins": existing.get("origins", [])}
        if not atomic_write_json(auth_path, data, expected_version=version):
            log.debug("Cookie checkpoint skipped after a newer credential import")
            return False
        page._auth_file_version = hashlib.sha256(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        log.info("已保存 %d 个 Cookie 至 %s", len(current_cookies), auth_file)
        return True
    except Exception as e:
        log.debug("保存 Cookie 失败: %s", e)
        return False


async def bootstrap_google_session(page: CDPPage) -> None:
    """Navigate through Google login bootstrap surface."""
    await page.goto(
        GOOGLE_LOGIN_BOOTSTRAP_URL, wait_until="domcontentloaded", timeout_s=30.0
    )
    await page.wait_for_timeout(3000)
    for url in (AI_STUDIO_URL, AI_STUDIO_URL_FALLBACK):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout_s=30.0)
            await page.wait_for_timeout(1500)
            if "accounts.google.com" not in (page.url or ""):
                return
        except Exception:
            continue
    raise RuntimeError(f"bootstrap stayed on login flow: url={page.url}")


async def navigate_to_aistudio(
    page: CDPPage,
    auth_user: str = "0",
    auth_file: str | None = None,
    verify_identity_cb: Callable[[CDPPage], Awaitable[bool]] | None = None,
    save_cookies_cb: Callable[..., Awaitable[None]] | None = None,
) -> None:
    """Navigate page to AI Studio with region and session-expiration checks."""
    last_exc = None
    target_urls = get_aistudio_urls(auth_user=auth_user)
    for url in target_urls:
        try:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout_s=20.0)
            except RuntimeError as nav_exc:
                if "net::ERR_ABORTED" in str(nav_exc):
                    curr = page.url or ""
                    with suppress(Exception):
                        eval_url = await page.evaluate(
                            "() => window.location.href", timeout_s=2.0
                        )
                        if eval_url:
                            curr = str(eval_url)
                    expected_match = (
                        f"/u/{auth_user}/"
                        if auth_user and auth_user != "0"
                        else "aistudio.google.com"
                    )
                    curr_parsed = urlsplit(curr)
                    curr_host = (curr_parsed.hostname or "").lower()
                    is_aistudio = (
                        curr_host == "aistudio.google.com"
                        or curr_host.endswith(".aistudio.google.com")
                    )
                    if is_aistudio and (
                        expected_match in curr_parsed.path
                        or expected_match == "aistudio.google.com"
                    ):
                        log.debug("页面导航被终止但已处于 AI Studio 域: %s", curr)
                    else:
                        raise
                else:
                    raise

            current_url = page.url or ""
            with suppress(Exception):
                eval_url = await page.evaluate(
                    "() => window.location.href", timeout_s=2.0
                )
                if eval_url:
                    current_url = str(eval_url)

            if "available-regions" in current_url:
                raise RuntimeError(
                    f"Google AI Studio 地区限制 (IP 漏了/不支持): {current_url}"
                )
            if is_login_page_url(current_url):
                log.warning(
                    "访问 AI Studio 后检测到重定向至登录页: %s，尝试重新注入 Cookie...",
                    current_url,
                )
                if await load_cookies_to_page(page, auth_file):
                    # Injection changes the jar, not the current login document.
                    # Retry the target once and verify its actual destination.
                    await page.goto(url, wait_until="domcontentloaded", timeout_s=20.0)
                    current_url = page.url or ""
                    with suppress(Exception):
                        eval_url = await page.evaluate("() => window.location.href", timeout_s=2.0)
                        if eval_url:
                            current_url = str(eval_url)
                if is_login_page_url(current_url):
                    raise SessionExpiredError(
                        f"Cookie 认证失败，已被重定向到 Google 登录页。 (url={current_url})"
                    )
                if "available-regions" in current_url:
                    raise RuntimeError(f"Google AI Studio 地区限制: {current_url}")

            for _ in range(15):
                with suppress(Exception):
                    if await page.evaluate("() => !!window.default_MakerSuite"):
                        break
                await page.wait_for_timeout(500)

            identity_status: IdentityStatus = "unavailable"
            # MakerSuite can exist before the current-account control hydrates.
            # Wait locally with a fixed budget; missing evidence is not a
            # confirmed mismatch and must never authorize Cookie checkpoints.
            try:
                async with asyncio.timeout(4.0):
                    for attempt in range(12):
                        if verify_identity_cb is not None:
                            identity_status = (
                                "verified" if await verify_identity_cb(page) else "unavailable"
                            )
                        else:
                            identity_status = await _account_identity_status(page, auth_file)
                        if identity_status == "verified":
                            break
                        if attempt < 11:
                            await page.wait_for_timeout(250)
            except TimeoutError:
                pass

            if identity_status == "verified":
                if save_cookies_cb is not None:
                    await save_cookies_cb(auth_file=auth_file)
                else:
                    await save_browser_cookies(page, auth_file)
                return
            if identity_status == "mismatch":
                raise AccountIdentityMismatchError(
                    "AI Studio account identity does not match the selected account"
                )
            raise AccountIdentityUnavailableError("AI Studio account identity is not yet available")
        except Exception as exc:
            if "地区限制" in str(exc) or "Cookie 认证失败" in str(exc):
                raise exc
            log.debug("访问 %s 失败: %s", url, exc)
            last_exc = exc

    if last_exc is not None:
        raise last_exc
