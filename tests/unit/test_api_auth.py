from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request

from aistudio_api.api.dependencies import require_api_key, require_web_auth
from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway import model_defaults


@pytest.fixture(autouse=True)
def dynamic_keys(monkeypatch):
    keys = {"secret-token"}
    monkeypatch.setattr(model_defaults, "get_configured_api_keys", lambda: keys)
    return keys


def _build_client() -> httpx.AsyncClient:
    app = FastAPI()

    @app.get("/protected", dependencies=[Depends(require_api_key)])
    async def protected():
        return {"ok": True}

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


@pytest.mark.asyncio
async def test_empty_dynamic_keys_fail_closed(monkeypatch, dynamic_keys):
    dynamic_keys.clear()
    monkeypatch.setattr(settings, "api_keys", frozenset())
    async with _build_client() as client:
        response = await client.get("/protected")
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_bearer_token_is_accepted(monkeypatch):
    monkeypatch.setattr(settings, "api_keys", frozenset({"secret-token"}))
    async with _build_client() as client:
        response = await client.get(
            "/protected", headers={"Authorization": "Bearer secret-token"}
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}


@pytest.mark.asyncio
async def test_x_api_key_is_accepted(monkeypatch):
    monkeypatch.setattr(settings, "api_keys", frozenset({"secret-token"}))
    async with _build_client() as client:
        response = await client.get("/protected", headers={"X-API-Key": "secret-token"})
        assert response.status_code == 200
        assert response.json() == {"ok": True}


@pytest.mark.asyncio
async def test_x_goog_api_key_is_accepted(monkeypatch):
    monkeypatch.setattr(settings, "api_keys", frozenset({"secret-token"}))
    async with _build_client() as client:
        response = await client.get(
            "/protected", headers={"x-goog-api-key": "secret-token"}
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}


@pytest.mark.asyncio
async def test_query_param_key_is_accepted(monkeypatch):
    monkeypatch.setattr(settings, "api_keys", frozenset({"secret-token"}))
    async with _build_client() as client:
        response = await client.get("/protected?key=secret-token")
        assert response.status_code == 200
        assert response.json() == {"ok": True}


@pytest.mark.asyncio
async def test_missing_or_invalid_api_key_returns_401(monkeypatch):
    monkeypatch.setattr(settings, "api_keys", frozenset({"secret-token"}))
    async with _build_client() as client:
        response = await client.get("/protected")
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"


def test_dependencies_timing_safe_auth(monkeypatch):
    """Verify constant-time comparison and empty password guard."""
    monkeypatch.setattr(settings, "web_password", "super_secret_admin_pass")
    monkeypatch.setattr(settings, "api_keys", frozenset({"key123"}))

    # require_web_auth with correct password
    req_good = MagicMock(spec=Request)
    req_good.method = "POST"
    req_good.headers = {"authorization": "Bearer super_secret_admin_pass"}
    req_good.query_params = {}
    require_web_auth(req_good)

    # require_web_auth with wrong password
    req_bad = MagicMock(spec=Request)
    req_bad.method = "POST"
    req_bad.headers = {"authorization": "Bearer wrong"}
    req_bad.query_params = {}
    with pytest.raises(HTTPException) as exc_info:
        require_web_auth(req_bad)
    assert exc_info.value.status_code == 401

    # require_api_key matching admin web_password
    req_admin_api = MagicMock(spec=Request)
    req_admin_api.headers = {"x-api-key": "super_secret_admin_pass"}
    req_admin_api.query_params = {}
    with pytest.raises(HTTPException) as exc_info:
        require_api_key(req_admin_api)
    assert exc_info.value.status_code == 401

    # Empty web_password guard
    monkeypatch.setattr(settings, "web_password", "")
    req_empty_pass = MagicMock(spec=Request)
    req_empty_pass.headers = {"x-api-key": ""}
    req_empty_pass.query_params = {}
    with pytest.raises(HTTPException):
        require_api_key(req_empty_pass)


def test_require_web_auth_rejects_html_accept_spoofing(monkeypatch):
    """验证客户端伪造 Accept: text/html 无法绕过管理控制台密码校验。"""
    monkeypatch.setattr(settings, "web_password", "super_secret_admin_pass")

    req_spoof = MagicMock(spec=Request)
    req_spoof.method = "GET"
    req_spoof.headers = {"accept": "text/html,application/xhtml+xml"}
    req_spoof.query_params = {}
    with pytest.raises(HTTPException) as exc_info:
        require_web_auth(req_spoof)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_static_keys_and_admin_equal_dynamic_key_cannot_call(monkeypatch, dynamic_keys):
    monkeypatch.setattr(settings, "api_keys", frozenset({"old-static"}))
    monkeypatch.setattr(settings, "web_password", "admin-only")
    dynamic_keys.add("admin-only")
    async with _build_client() as client:
        for value in ("old-static", "admin-only", "错误密钥"):
            response = await client.get("/protected", params={"key": value})
            assert response.status_code == 401


def test_management_only_accepts_bearer_without_sensitive_query(monkeypatch):
    monkeypatch.setattr(settings, "web_password", "admin-only")
    for headers, query in (
        ({"x-api-key": "admin-only"}, {}),
        ({}, {"key": "admin-only"}),
        ({"authorization": "Bearer admin-only"}, {"password": "anything"}),
    ):
        req = MagicMock(spec=Request, headers=headers, query_params=query)
        with pytest.raises(HTTPException) as error:
            require_web_auth(req)
        assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_dynamic_configuration_failure_fails_closed(monkeypatch):
    def failed():
        raise OSError("synthetic configuration read failure")
    monkeypatch.setattr(model_defaults, "get_configured_api_keys", failed)
    async with _build_client() as client:
        response = await client.get("/protected", params={"key": "secret-token"})
        assert response.status_code == 401
