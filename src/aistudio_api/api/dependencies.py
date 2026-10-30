"""Shared FastAPI dependencies."""

from __future__ import annotations

import secrets
from collections.abc import Iterable

from fastapi import HTTPException, Request

from aistudio_api.application.runtime import runtime_state
from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("auth")

_SENSITIVE_QUERY_NAMES = frozenset({
    "key", "api_key", "api-key", "apikey", "x-api-key", "x-goog-api-key",
    "authorization", "token", "access_token", "password", "admin_password",
    "web_password", "secret",
})


def _constant_match(token: str | None, candidates: Iterable[str]) -> bool:
    if not token or len(token) > 4096:
        return False
    matched = False
    for candidate in candidates:
        if candidate and len(candidate) <= 4096:
            matched |= secrets.compare_digest(token.encode("utf-8"), candidate.encode("utf-8"))
    return matched


def _extract_request_token(request: Request) -> str | None:
    # 1. Google Gemini standard query param: ?key=...
    query_key = request.query_params.get("key")
    if query_key and query_key.strip():
        return query_key.strip()

    # 2. Google Gemini standard header: x-goog-api-key
    goog_key = (request.headers.get("x-goog-api-key") or "").strip()
    if goog_key:
        return goog_key

    # 3. Standard x-api-key header
    api_key = (request.headers.get("x-api-key") or "").strip()
    if api_key:
        return api_key

    # 4. Bearer token in Authorization header
    authorization = (request.headers.get("authorization") or "").strip()
    if not authorization:
        return None

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None

    token = token.strip()
    return token or None


def require_web_auth(request: Request) -> None:
    """保护管理控制台路由 (通过环境变量 AISTUDIO_WEB_PASSWORD 配置密码)。"""
    # Management credentials must never be accepted in URLs or API-key headers.
    authorization = (request.headers.get("authorization") or "").strip()
    scheme, _, bearer = authorization.partition(" ")
    token = bearer.strip() if scheme.lower() == "bearer" else ""
    if (
        not any(name.lower() in _SENSITIVE_QUERY_NAMES for name in request.query_params)
        and settings.web_password
        and settings.web_password.strip()
        and _constant_match(token, {settings.web_password})
    ):
        return
    client_ip = request.client.host if request.client else "unknown"
    logger.warning("管理控制台鉴权失败: 来自 %s 的密码无效", client_ip)
    raise HTTPException(
        status_code=401,
        detail="管理控制台鉴权失败，请输入正确的管理密码",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_api_key(request: Request) -> None:
    """保护对外 Gemini API 路由 (通过 config.yaml 中的 api_keys 配置)。"""
    from aistudio_api.infrastructure.gateway.model_defaults import (
        get_configured_api_keys,
    )

    try:
        # Only the owner's current dynamic keys authorize inference, even when
        # the application runs without the deployment guard or launcher.
        all_valid_keys = get_configured_api_keys() - {settings.web_password}
    except Exception:
        all_valid_keys = set()

    token = _extract_request_token(request)
    if not token:
        client_ip = request.client.host if request.client else "unknown"
        logger.warning("API 鉴权失败: 来自 %s 的请求缺少 API Key", client_ip)
        raise HTTPException(
            status_code=401,
            detail={
                "message": "Invalid or missing API key. Pass via 'x-goog-api-key' header, 'key' query parameter, or 'Authorization: Bearer <key>'",
                "type": "authentication_error",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    if _constant_match(token, all_valid_keys):
        return
    client_ip = request.client.host if request.client else "unknown"
    logger.warning("API 鉴权失败: 来自 %s 的 API Key 无效", client_ip)
    raise HTTPException(
        status_code=401,
        detail={
            "message": "Invalid API key provided",
            "type": "authentication_error",
        },
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_client() -> AIStudioClient:
    if runtime_state.client is None:
        raise HTTPException(
            503,
            detail={"message": "Client not initialized", "type": "service_unavailable"},
        )
    return runtime_state.client


def get_account_service():
    if runtime_state.account_service is None:
        raise HTTPException(
            503,
            detail={
                "message": "Account service not initialized",
                "type": "service_unavailable",
            },
        )
    return runtime_state.account_service

def get_runtime_state():
    return runtime_state
