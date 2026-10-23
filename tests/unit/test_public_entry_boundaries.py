"""Public HTTP boundaries use synthetic credentials and never start a browser."""

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import Depends, FastAPI

from aistudio_api.api.app import app
from aistudio_api.api.dependencies import require_web_auth
from aistudio_api.config import settings


@pytest.mark.parametrize("password", [None, "", "   "])
async def test_management_without_a_configured_password_fails_closed(monkeypatch, password):
    monkeypatch.setattr(settings, "web_password", password)
    protected_app = FastAPI()

    @protected_app.get("/management", dependencies=[Depends(require_web_auth)])
    async def management():
        return {"private": True}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=protected_app), base_url="http://offline",
    ) as client:
        for headers in ({}, {"Authorization": "Bearer synthetic-api-key"}):
            response = await client.get("/management", headers=headers)
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == "Bearer"


async def test_public_health_and_login_discovery_with_missing_password(monkeypatch):
    monkeypatch.setattr(settings, "web_password", "")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://offline",
    ) as client:
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        assert set(health.json()) <= {"status", "busy"}
        discovery = await client.get("/auth/check")
        assert discovery.status_code == 200
        assert discovery.json() == {"auth_enabled": True}
        assert (await client.get("/config")).status_code == 401



async def test_default_lifespan_never_starts_browser_warmup(monkeypatch):
    application = importlib.import_module("aistudio_api.api.app")
    client = SimpleNamespace(warmup=AsyncMock(), close=AsyncMock())
    store = SimpleNamespace(list_accounts=list)
    monkeypatch.setattr(application, "AIStudioClient", lambda **kwargs: client)
    monkeypatch.setattr("aistudio_api.infrastructure.account.account_store.AccountStore", lambda: store)
    monkeypatch.setattr("aistudio_api.application.account_service.AccountService", MagicMock())
    monkeypatch.setattr("aistudio_api.application.account_rotator.init_rotator", lambda value: None)
    monkeypatch.setattr("aistudio_api.infrastructure.browser.browser_engine.install_process_cleanup_handlers", lambda: None)
    monkeypatch.setattr(settings, "browser_idle_timeout", 0)
    async with application.lifespan(app):
        await asyncio.sleep(0)
    client.warmup.assert_not_awaited()
    client.close.assert_awaited_once()
