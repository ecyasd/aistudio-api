"""Runtime authentication guard for the explicitly published HTTPS routes.

This module is independent of FastAPI. It never consumes request bodies or logs
request data. Credential providers are consulted for every protected request so
administrator changes to configured API keys take effect without a restart.
"""
from __future__ import annotations

import importlib
import ipaddress
import json
import math
import secrets
import time
from collections import OrderedDict, deque
from typing import Any
from urllib.parse import parse_qsl

MAX_TOKEN_LENGTH = 4096
MAX_QUERY_LENGTH = 16384
_MANAGEMENT_ROOTS = ("/stats", "/rotation", "/config", "/api-keys", "/api/accounts", "/errors", "/browser/status")
_SENSITIVE_QUERY_NAMES = frozenset(
    (
        "key", "api_key", "api-key", "apikey", "x-api-key", "x-goog-api-key",
        "authorization", "token", "access_token", "password", "admin_password",
        "web_password", "secret",
    )
)


def _runtime_settings():
    from aistudio_api.config import settings

    return settings


def _runtime_api_keys():
    from aistudio_api.infrastructure.gateway.model_defaults import (
        get_configured_api_keys,
    )

    return get_configured_api_keys()


def _first_header(scope, name):
    for key, value in scope.get("headers", ()):
        if key.lower() == name:
            return value.decode("latin-1")
    return ""


def _bearer(scope):
    authorization = _first_header(scope, b"authorization").strip()
    scheme, _, token = authorization.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _request_kind(scope):
    path = scope.get("path", "")
    if path == "/v1beta" or path.startswith("/v1beta/"):
        return "api"
    if path == "/__deployment/api-auth":
        return "api"
    if any(path == root or path.startswith(root + "/") for root in _MANAGEMENT_ROOTS):
        return "management"
    if path == "/accounts" or path.startswith("/accounts/"):
        # The project exposes its legacy JSON account routes at /accounts, but
        # GET /accounts with a browser Accept header is the public SPA shell.
        accept = _first_header(scope, b"accept").lower()
        if (
            path.rstrip("/") == "/accounts"
            and scope.get("method") == "GET"
            and "text/html" in accept
            and "application/json" not in accept
        ):
            return None
        return "management"
    return None


def _source_ip(scope):
    client = scope.get("client")
    host = str(client[0]) if client else "unknown"
    try:
        peer = ipaddress.ip_address(host)
    except ValueError:
        return "unknown"
    if peer.is_loopback:
        # Caddy must overwrite this header with its TCP remote host. Headers
        # from an external direct peer are never trusted.
        real_ip = _first_header(scope, b"x-real-ip").strip()
        try:
            return str(ipaddress.ip_address(real_ip))
        except ValueError:
            pass
    return str(peer)


def _token(scope, kind):
    query_bytes = scope.get("query_string", b"")
    if len(query_bytes) > MAX_QUERY_LENGTH:
        return ""
    try:
        # Starlette QueryParams.get selects the LAST occurrence of a query key.
        pairs = parse_qsl(
            query_bytes.decode("latin-1"), keep_blank_values=True,
            max_num_fields=64, errors="replace",
        )
    except (ValueError, UnicodeError):
        return ""
    if kind == "management":
        if any(name.lower() in _SENSITIVE_QUERY_NAMES for name, _ in pairs):
            return ""
        value = _bearer(scope)
    else:
        value = dict(pairs).get("key", "").strip()
        if not value:
            value = _first_header(scope, b"x-goog-api-key").strip()
        if not value:
            value = _first_header(scope, b"x-api-key").strip()
        if not value:
            value = _bearer(scope)
    return value if 0 < len(value) <= MAX_TOKEN_LENGTH else ""


def _constant_match(token, candidates):
    if not token:
        return False
    token_bytes = token.encode("utf-8")
    matched = False
    for candidate in candidates:
        if isinstance(candidate, str) and 0 < len(candidate) <= MAX_TOKEN_LENGTH:
            # Do not stop at a matching key or depend on ASCII-only input.
            matched |= secrets.compare_digest(token_bytes, candidate.encode("utf-8"))
    return matched


class FailureWindow:
    """Bounded sliding failure windows; valid authentication never enters it."""

    def __init__(self, *, per_ip_limit=6, global_limit=1000, window=900, max_ip_entries=4096):
        if min(per_ip_limit, global_limit, window, max_ip_entries) <= 0:
            raise ValueError("Failure window limits must be positive")
        self.per_ip_limit = per_ip_limit
        self.global_limit = global_limit
        self.window = window
        self.max_ip_entries = max_ip_entries
        self.by_ip = OrderedDict()
        self.global_failures = deque()

    def _clean(self, now):
        cutoff = now - self.window
        while self.global_failures and self.global_failures[0] <= cutoff:
            self.global_failures.popleft()
        # Records are ordered by their most recent recorded failure.
        while self.by_ip:
            first = next(iter(self.by_ip))
            if self.by_ip[first][-1] > cutoff:
                break
            self.by_ip.popitem(last=False)

    def retry_after(self, ip, now):
        self._clean(now)
        failures = self.by_ip.get(ip)
        if failures:
            while failures and failures[0] <= now - self.window:
                failures.popleft()
            if not failures:
                del self.by_ip[ip]
        deadlines = []
        if failures and len(failures) >= self.per_ip_limit:
            deadlines.append(failures[0] + self.window)
        if len(self.global_failures) >= self.global_limit:
            deadlines.append(self.global_failures[0] + self.window)
        return max(1, math.ceil(max(deadlines) - now)) if deadlines else 0

    def record(self, ip, now):
        self._clean(now)
        # Callers record only when retry_after returned zero. These extra bounds
        # also keep the data structure bounded if it is used independently.
        if len(self.global_failures) < self.global_limit:
            self.global_failures.append(now)
        failures = self.by_ip.get(ip)
        if failures is None:
            while len(self.by_ip) >= self.max_ip_entries:
                self.by_ip.popitem(last=False)
            failures = self.by_ip[ip] = deque()
        while failures and failures[0] <= now - self.window:
            failures.popleft()
        if len(failures) < self.per_ip_limit:
            failures.append(now)
        self.by_ip.move_to_end(ip)


class PublicAccessGuard:
    """ASGI auth gate and failed-auth limiter, without reading request bodies."""

    def __init__(
        self, app, *, settings_provider=None, api_keys_provider=None, clock=None,
        per_ip_limit=6, global_limit=1000, window=900, max_ip_entries=4096,
    ):
        self.app = app
        self.settings_provider = settings_provider or _runtime_settings
        self.api_keys_provider = api_keys_provider or _runtime_api_keys
        self.clock = clock or time.monotonic
        self.failures = FailureWindow(
            per_ip_limit=per_ip_limit, global_limit=global_limit,
            window=window, max_ip_entries=max_ip_entries,
        )

    def _authorized(self, scope, kind):
        token = _token(scope, kind)
        if not token:
            return False
        try:
            settings = self.settings_provider()
            admin = settings.web_password
            # The owner chooses the password. An empty value still fails closed
            # instead of inheriting upstream's anonymous-admin behavior.
            current_admin = admin if isinstance(admin, str) and admin.strip() else ""
            if kind == "management":
                return _constant_match(token, (current_admin,))
            # Only the live configured API keys grant inference access. Startup
            # credentials and management authentication must not remain API keys
            # after an administrator rotates keys in the website.
            current_keys = (
                key for key in self.api_keys_provider() if key != current_admin
            )
            return _constant_match(token, current_keys)
        except Exception:
            # No configuration failure may silently turn authentication off.
            return False

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        kind = _request_kind(scope)
        if kind is None or self._authorized(scope, kind):
            return await self.app(scope, receive, send)
        now = self.clock()
        source = _source_ip(scope)
        retry_after = self.failures.retry_after(source, now)
        status = 429 if retry_after else 401
        if not retry_after:
            self.failures.record(source, now)
        body = json.dumps(
            {"error": {
                "code": status,
                "message": "Too many failed authentication attempts. Try again later."
                if retry_after else "Invalid or missing credentials.",
                "status": "RESOURCE_EXHAUSTED" if retry_after else "UNAUTHENTICATED",
            }}, separators=(",", ":"),
        ).encode("utf-8")
        headers = [
            (b"content-type", b"application/json"),
            (b"cache-control", b"no-store"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
        if retry_after:
            headers.append((b"retry-after", str(retry_after).encode("ascii")))
        else:
            headers.append((b"www-authenticate", b"Bearer"))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})
        return None


def _disabled_dump(*_args, **_kwargs):
    return None


def _dump_is_disabled(*_args, **_kwargs):
    return False


def disable_request_dumps(
    *, logger_module: Any = None, app_module: Any = None, settings: Any = None
):
    """Prevent settings-page changes from re-enabling sensitive payload dumps."""
    if logger_module is None:
        logger_module = importlib.import_module("aistudio_api.infrastructure.utils.logger")
    if app_module is None:
        app_module = importlib.import_module("aistudio_api.api.app")
    if settings is None:
        settings = _runtime_settings()
    settings.dump_requests = False
    settings.dump_to_file = False
    settings.dump_raw_response = False
    logger_module._dump_requests_override = False
    logger_module._dump_to_file_override = False
    logger_module._dump_dir_override = "/var/lib/aistudio-api/request-dumps"
    logger_module.dump_request_exchange = _disabled_dump
    logger_module.is_dump_requests_enabled = _dump_is_disabled
    logger_module.is_dump_to_file_enabled = _dump_is_disabled
    logger_module.set_dump_requests = _disabled_dump
    logger_module.set_dump_to_file = _disabled_dump
    # app.py imports these functions by value; patch those references as well.
    app_module.dump_request_exchange = _disabled_dump
    app_module.is_dump_requests_enabled = _dump_is_disabled


def install_public_access_guard(app, **guard_options):
    """Call after importing the app and before uvicorn starts serving requests."""
    disable_request_dumps()
    app.add_middleware(PublicAccessGuard, **guard_options)
    from aistudio_api.api.error_middleware import ErrorRecordsMiddleware

    app.add_middleware(ErrorRecordsMiddleware)
    return app
