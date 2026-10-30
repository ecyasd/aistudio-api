"""解析浏览器 cookie（支持 JSON 数组、Netscape 格式与 Header KV），并提供 Google 多账号探活。"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from collections.abc import Iterable
from typing import cast
from urllib.parse import urlsplit

from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("cookie_parser")


DEFAULT_API_KEY = "AIzaSyDdP816MREB3SkjZO04QXbjsigfcI0GWOs"
DEFAULT_EXT_BIN = "CAESAUwwATgEQABQBGICSlBwAHgBkAEAmAEB"
DEFAULT_ORIGIN = "https://aistudio.google.com"
DEFAULT_USER_AGENT = "grpc-web-javascript/0.1"

# Cookies that need httpOnly=False so JS can read them for SAPISIDHASH.
_AUTH_COOKIE_NAMES = {
    "SID",
    "APISID",
    "SAPISID",
    "__Secure-1PAPISID",
    "__Secure-3PAPISID",
}

_DOMAIN_OVERRIDES: dict[str, list[str]] = {
    "OSID": ["aistudio.google.com"],
    "__Secure-OSID": ["aistudio.google.com"],
    "__Secure-BUCKET": ["aistudio.google.com"],
    "OTZ": ["accounts.google.com"],
    "__Host-GAPS": ["accounts.google.com"],
    "__Host-1PLSID": ["accounts.google.com"],
    "__Host-3PLSID": ["accounts.google.com"],
    "LSID": ["accounts.google.com"],
    "SMSV": ["accounts.google.com"],
    "LSOLH": ["accounts.google.com"],
    "ACCOUNT_CHOOSER": ["accounts.google.com"],
    "__Secure-1PSIDTS": [".google.com", "aistudio.google.com", "accounts.google.com"],
    "__Secure-3PSIDTS": [".google.com", "aistudio.google.com", "accounts.google.com"],
}

# 外部导入时可安全丢弃的遥测、易失效挑战及旧环境绑定的临时 Cookie。
# 过滤这些 Cookie 既能让网页在当前 IP/环境重新自然协商下发全新凭据，
# 又能精简内存与 Cookie 存储体积（尤其对 Android 低内存环境极为友好）：
DISCARDABLE_IMPORT_COOKIE_NAMES: frozenset[str] = frozenset(
    {
        "_ga",
        "_gid",
        "_gat",
        "_gcl_au",
        "1P_JAR",
        "DV",
        "AEC",
        "NID",
        "SIDCC",
        "__Secure-1PSIDCC",
        "__Secure-3PSIDCC",
        "OSID",
        "__Secure-OSID",
    }
)


def parse_raw_cookies(raw: object) -> dict[str, str]:
    """万能 Cookie 解析器，支持：
    1. JSON 数组（EditThisCookie / Cookie-Editor 导出：`[{"name": "...", "value": "..."}, ...]`）
    2. JSON 对象（`{"cookies": [...]}` 或 `{"cookies": {"k": "v"}}`）
    3. Netscape / curl 格式（7 列 Tab 分隔）
    4. HTTP 请求头格式（`Cookie: k1=v1; k2=v2` 或带换行的多行键值对）
    """
    parsed: dict[str, str] = {}
    if not raw:
        return parsed

    text = raw.strip() if isinstance(raw, str) else json.dumps(raw)

    if re.match(r"^cookie:\s*", text, re.IGNORECASE):
        text = re.sub(r"^cookie:\s*", "", text, flags=re.IGNORECASE)

    # 1. 尝试 JSON 格式
    if text.startswith(("{", "[")):
        try:
            obj = json.loads(text)
            if isinstance(obj, list):
                for item in obj:
                    if (
                        isinstance(item, dict)
                        and item.get("name")
                        and item.get("value") is not None
                    ):
                        k = str(item["name"]).strip()
                        v = str(item["value"]).strip()
                        if k and v:
                            parsed[k] = v
                if parsed:
                    return parsed
            elif isinstance(obj, dict):
                if isinstance(obj.get("cookie"), str):
                    text = obj["cookie"]
                elif isinstance(obj.get("cookies"), str):
                    text = obj["cookies"]
                elif isinstance(obj.get("cookies"), list):
                    for item in obj["cookies"]:
                        if (
                            isinstance(item, dict)
                            and item.get("name")
                            and item.get("value") is not None
                        ):
                            k = str(item["name"]).strip()
                            v = str(item["value"]).strip()
                            if k and v:
                                parsed[k] = v
                    if parsed:
                        return parsed
                elif isinstance(obj.get("cookies"), dict):
                    for k, v in obj["cookies"].items():
                        if k and v is not None:
                            parsed[str(k).strip()] = str(v).strip()
                    return parsed
        except Exception:
            pass

    # 2. 按行与分号拆分解析
    lines = re.split(r"[\r\n;]+", text)
    ignored_keys = {
        "domain",
        "path",
        "expires",
        "samesite",
        "secure",
        "httponly",
        "priority",
        "hostonly",
    }

    for line in lines:
        trimmed = line.strip()
        # Netscape exports use this prefix to mark HttpOnly, not a comment.
        if trimmed.startswith("#HttpOnly_"):
            trimmed = trimmed[len("#HttpOnly_") :]
        if not trimmed or trimmed.startswith("#"):
            continue

        # Netscape 7 列 Tab 分隔
        if "\t" in trimmed:
            cols = trimmed.split("\t")
            if len(cols) >= 7:
                k = cols[5].strip() if len(cols) > 5 else ""
                v = cols[6].strip() if len(cols) > 6 else ""
                if k and v and k.lower() not in ignored_keys:
                    parsed[k] = v
                continue

        # 标准 KV：name=value
        if "=" in trimmed:
            k, _, v = trimmed.partition("=")
            k = k.strip()
            v = v.strip()
            k = re.sub(r"^cookie:\s*", "", k, flags=re.IGNORECASE)
            if (v.startswith('"') and v.endswith('"')) or (
                v.startswith("'") and v.endswith("'")
            ):
                v = v[1:-1]
            if k and v and k.lower() not in ignored_keys:
                parsed[k] = v

    return parsed


def calculate_sapisid_hash(
    cookies: dict[str, str], origin: str = DEFAULT_ORIGIN
) -> str:
    """计算 Google SAPISIDHASH 鉴权头部。"""
    sapisid = cookies.get("SAPISID") or cookies.get("__Secure-1PAPISID") or ""
    sapisid1p = cookies.get("__Secure-1PAPISID") or sapisid
    sapisid3p = cookies.get("__Secure-3PAPISID") or sapisid
    if not sapisid:
        return ""

    ts = int(time.time())

    def calc_hash(val: str) -> str:
        return hashlib.sha1(f"{ts} {val} {origin}".encode()).hexdigest()

    h1 = f"SAPISIDHASH {ts}_{calc_hash(sapisid)}"
    h2 = f"SAPISID1PHASH {ts}_{calc_hash(sapisid1p)}"
    h3 = f"SAPISID3PHASH {ts}_{calc_hash(sapisid3p)}"
    return f"{h1} {h2} {h3}"


def build_google_cookie_list(
    pairs: Iterable[tuple[str, str]] | dict[str, str],
    *,
    allow_url_targets: bool = False,
    discard_transient: bool = False,
) -> list[dict[str, object]]:
    """Build Playwright/CDP compatible cookies from name/value pairs."""
    seen: set[tuple[str, str, str]] = set()
    cookies: list[dict[str, object]] = []

    def _add_cookie(name: str, value: str, target: str) -> None:
        is_host_target = not target.startswith(".")

        if name.startswith("__Host-") and not allow_url_targets:
            return

        target_kind = "url" if allow_url_targets and is_host_target else "domain"
        target_value = f"https://{target}/" if target_kind == "url" else target
        key = (name, target_kind, target_value)
        if key in seen:
            return
        seen.add(key)

        cookie: dict[str, object] = {
            "name": name,
            "value": value,
            "secure": True,
            "httpOnly": name not in _AUTH_COOKIE_NAMES,
            "sameSite": "None",
        }
        if target_kind == "url":
            cookie["url"] = target_value
        else:
            cookie["domain"] = target
            cookie["path"] = "/"
        cookies.append(cookie)

    item_list: list[tuple[str, str]] = (
        [(str(k), str(v)) for k, v in pairs.items()]
        if isinstance(pairs, dict)
        else [(str(k), str(v)) for k, v in pairs]
    )
    for name, value in item_list:
        if discard_transient and (
            name in DISCARDABLE_IMPORT_COOKIE_NAMES
            or name.startswith(("_ga_", "_gcl_"))
        ):
            continue
        targets = _DOMAIN_OVERRIDES.get(name)
        if not targets:
            targets = [".google.com"]
        for target in targets:
            _add_cookie(name, value, target)

    return cookies


def parse_cookie_string(
    raw: object, *, discard_transient: bool = True
) -> dict[str, object]:
    """将任意格式 cookie 解析为 storage state dict，包含 cookies 和 origins 字段。"""
    exported = _structured_cookie_items(raw)
    if exported is not None:
        cookies: list[dict[str, object]] = []
        expired_auth_families: set[int] = set()
        valid_auth_families: set[int] = set()
        auth_families = (
            {"SID", "__Secure-1PSID", "__Secure-3PSID"},
            {"APISID", "SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID"},
        )
        for item in exported:
            cookie = _normalize_export_cookie(item)
            if cookie is None:
                continue
            name = str(cookie["name"])
            expired = "expires" in cookie and float(cast("str | float", cookie["expires"])) <= time.time()
            for index, family in enumerate(auth_families):
                if name in family:
                    (expired_auth_families if expired else valid_auth_families).add(index)
            if expired or (discard_transient and (
                name in DISCARDABLE_IMPORT_COOKIE_NAMES or name.startswith(("_ga_", "_gcl_"))
            )):
                continue
            cookies.append(cookie)
        if expired_auth_families - valid_auth_families:
            raise ValueError("导入的 Google 认证 Cookie 已过期，请重新导出当前登录会话")
        return {"cookies": cookies, "origins": []}

    cookie_dict = parse_raw_cookies(raw)
    return {
        "cookies": build_google_cookie_list(
            cookie_dict, allow_url_targets=False, discard_transient=discard_transient
        ),
        "origins": [],
    }


def _structured_cookie_items(raw: object) -> list[dict[str, object]] | None:
    """Recognize exports that carry real scope, expiry, and HttpOnly attributes."""
    obj = raw
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith(("[", "{")):
            try:
                obj = json.loads(text)
            except (ValueError, TypeError):
                return None
        elif "\t" in text:
            items: list[dict[str, object]] = []
            for line in text.splitlines():
                line = line.strip()
                http_only = line.startswith("#HttpOnly_")
                if http_only:
                    line = line[len("#HttpOnly_") :]
                if not line or line.startswith("#"):
                    continue
                columns = line.split("\t")
                if len(columns) < 7:
                    continue
                items.append({
                    "domain": (
                        "." + columns[0].lstrip(".")
                        if columns[1].upper() == "TRUE"
                        else columns[0].lstrip(".")
                    ), "path": columns[2],
                    "secure": columns[3].upper() == "TRUE", "expires": columns[4],
                    "name": columns[5], "value": columns[6], "httpOnly": http_only,
                })
            return items
    if isinstance(obj, dict):
        obj = obj.get("cookies")
    if isinstance(obj, list):
        return [item for item in obj if isinstance(item, dict)]
    return None


def is_structured_cookie_export(raw: object) -> bool:
    return _structured_cookie_items(raw) is not None


def _normalize_export_cookie(item: dict[str, object]) -> dict[str, object] | None:
    name = str(item.get("name") or "").strip()
    value = item.get("value")
    if not name or value is None or str(value) == "":
        return None
    domain = str(item.get("domain") or "").strip().lower()
    url = str(item.get("url") or "")
    if not domain and url:
        from urllib.parse import urlsplit
        domain = (urlsplit(url).hostname or "").lower()
    if domain and not (domain.lstrip(".") == "google.com" or domain.endswith(".google.com")):
        return None
    # Name/value-only JSON uses the header mapping, while retaining any real
    # expiry/flags present in the export.
    if not domain:
        domain = _DOMAIN_OVERRIDES.get(name, [".google.com"])[0]
    cookie: dict[str, object] = {
        "name": name, "value": str(value), "domain": domain,
        "path": str(item.get("path") or "/"),
        "secure": bool(item.get("secure", True)),
        "httpOnly": bool(item.get("httpOnly", name not in _AUTH_COOKIE_NAMES)),
    }
    same_site = str(item.get("sameSite") or "").lower()
    if same_site in {"none", "no_restriction", "lax", "strict"}:
        cookie["sameSite"] = "None" if same_site == "no_restriction" else same_site.capitalize()
    expiry = item.get("expires", item.get("expirationDate"))
    if not item.get("session") and expiry is not None:
        try:
            expires = float(cast("str | float", expiry))
        except (TypeError, ValueError):
            raise ValueError("Cookie 到期时间格式无效") from None
        if not math.isfinite(expires):
            raise ValueError("Cookie 到期时间格式无效")
        if expires > 0:
            cookie["expires"] = expires
    if name.startswith("__Host-") and (domain.startswith(".") or cookie["path"] != "/" or not cookie["secure"]):
        raise ValueError("Host Cookie 的域名或安全属性无效")
    if name.startswith("__Secure-") and not cookie["secure"]:
        raise ValueError("Secure Cookie 缺少安全属性")
    partition_key = item.get("partitionKey")
    if isinstance(partition_key, dict) and partition_key.get("topLevelSite"):
        cookie["partitionKey"] = {
            "topLevelSite": str(partition_key["topLevelSite"]),
            "hasCrossSiteAncestor": bool(partition_key.get("hasCrossSiteAncestor", False)),
        }
    return cookie

def select_signing_cookies(
    cookies: list[dict[str, object]], *, page_url: str = DEFAULT_ORIGIN + "/"
) -> dict[str, str]:
    """Select the Cookie values applicable to this page's AI Studio origin."""
    origin_host = "aistudio.google.com"
    page_path = urlsplit(page_url).path or "/"
    candidates: dict[str, tuple[tuple[int, int], str]] = {}
    for cookie in cookies:
        if not isinstance(cookie, dict) or not cookie.get("name"):
            continue
        domain = str(cookie.get("domain") or "")
        if not domain and cookie.get("url"):
            domain = urlsplit(str(cookie["url"])).hostname or ""
        bare_domain = domain.lower().lstrip(".")
        if not bare_domain or not (
            origin_host == bare_domain
            or (domain.startswith(".") and origin_host.endswith("." + bare_domain))
        ):
            continue
        path = str(cookie.get("path") or "/")
        if not (
            page_path == path
            or page_path.startswith(path if path.endswith("/") else path + "/")
        ):
            continue
        expiry = cookie.get("expires")
        if expiry is not None and not cookie.get("session"):
            try:
                if 0 < float(cast("str | float", expiry)) <= time.time():
                    continue
            except (TypeError, ValueError):
                continue
        name = str(cookie["name"])
        rank = (len(path), len(bare_domain))
        if name not in candidates or rank > candidates[name][0]:
            candidates[name] = (rank, str(cookie.get("value") or ""))
    return {name: value for name, (_, value) in candidates.items()}


async def probe_google_accounts_infinite(
    raw_or_dict: object,
    *,
    max_fails_in_a_row: int = 1,
    max_accounts: int = 32,
) -> list[dict[str, object]]:
    """一路向下探测多账号登录索引（u/0, u/1, u/2...），直到探测失败为止。

    Returns:
        有效账号列表，例如 [{"auth_user": "0", "status_code": 200, "name": "Google Account (u/0)"}, ...]
    """
    if not 1 <= max_accounts <= 256 or max_fails_in_a_row < 1:
        raise ValueError("账号探测范围无效")
    storage = parse_cookie_string(raw_or_dict, discard_transient=False)
    items = storage.get("cookies")
    cookie_dict = select_signing_cookies(
        [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    )
    if not cookie_dict:
        return []

    import httpx

    from aistudio_api.config import settings

    cookie_header = "; ".join(f"{k}={v}" for k, v in cookie_dict.items())
    valid_accounts: list[dict[str, object]] = []

    auth_user_idx = 0
    consecutive_failures = 0

    async with httpx.AsyncClient(timeout=10.0, proxy=settings.proxy_url) as client:
        # A service-wide 403 does not prove an unbounded number of sessions.
        # This is a local resource bound, not a claimed Google account limit.
        while auth_user_idx < max_accounts:
            u_index = str(auth_user_idx)
            auth_header = calculate_sapisid_hash(cookie_dict)
            headers = {
                "Content-Type": "application/json+protobuf",
                "X-User-Agent": DEFAULT_USER_AGENT,
                "X-Goog-Api-Key": DEFAULT_API_KEY,
                "X-Goog-AuthUser": u_index,
                "X-Goog-Ext-519733851-bin": DEFAULT_EXT_BIN,
                "X-AIStudio-Visit-Id": f"v1_{uuid.uuid4()}",
                "Origin": DEFAULT_ORIGIN,
                "Referer": f"{DEFAULT_ORIGIN}/",
                "Cookie": cookie_header,
            }
            if auth_header:
                headers["Authorization"] = auth_header

            url = "https://alkalimakersuite-pa.clients6.google.com/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/ListModels"
            try:
                resp = await client.post(url, headers=headers, content="[]")
                # Only a successful RPC array proves this slot. Generic 400/403
                # responses may apply to every index and must not create accounts.
                if resp.status_code == 200 and isinstance(resp.json(), list):
                    consecutive_failures = 0
                    valid_accounts.append(
                        {
                            "auth_user": u_index,
                            "status_code": resp.status_code,
                            "name": f"Google Account (u/{u_index})",
                        }
                    )
                    logger.info(
                        "子账号 u/%s 探活成功 (HTTP %d)", u_index, resp.status_code
                    )
                elif resp.status_code == 401:
                    consecutive_failures += 1
                    logger.debug("子账号 u/%s 探活返回 401 (未授权)", u_index)
                else:
                    consecutive_failures += 1
                    logger.debug(
                        "子账号 u/%s 探活返回 HTTP %d", u_index, resp.status_code
                    )
            except Exception as e:
                consecutive_failures += 1
                logger.debug("子账号 u/%s 探活异常: %s", u_index, e)

            if consecutive_failures >= max_fails_in_a_row:
                break

            auth_user_idx += 1

    return valid_accounts
