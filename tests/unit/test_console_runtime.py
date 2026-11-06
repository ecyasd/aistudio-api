from unittest.mock import AsyncMock

import httpx
import pytest
import yaml

from aistudio_api.api.app import app
from aistudio_api.config import settings
from aistudio_api.infrastructure.runtime_options import active_options


@pytest.mark.asyncio
async def test_runtime_admin_only_save_requires_restart_and_preserves_dynamic_keys(
    tmp_path, monkeypatch
):
    config = tmp_path / "config.yaml"
    config.write_text("api_keys: [fixture-model-key]\n", encoding="utf-8")
    monkeypatch.setattr(settings, "config_file", str(config))
    monkeypatch.setattr(settings, "web_password", "fixture-admin")
    monkeypatch.setenv("AISTUDIO_ALLOW_RESTART", "1")
    monkeypatch.setattr("aistudio_api.infrastructure.utils.telemetry.restarting", False)
    restart = AsyncMock()
    monkeypatch.setattr(
        "aistudio_api.api.routes_observability.restart_process", restart
    )
    active_options.cache_clear()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.post("/api/console/restart")).status_code == 401
            client.headers["Authorization"] = "Bearer fixture-admin"
            before = (await client.get("/api/console/runtime")).json()
            response = await client.put(
                "/api/console/runtime", json={"engine": "browser"}
            )
            assert response.status_code == 200
            assert response.json()["active"]["engine"] == "browserless"
            assert response.json()["restart_required"] is True
            assert yaml.safe_load(config.read_text())["api_keys"] == [
                "fixture-model-key"
            ]
            assert (
                await client.put("/api/console/runtime", json={"engine": "arbitrary"})
            ).status_code == 400
            assert (await client.post("/api/console/restart")).status_code == 202
            restart.assert_awaited_once()
            assert before["instance_id"] == response.json()["instance_id"]
    finally:
        active_options.cache_clear()


@pytest.mark.asyncio
async def test_restart_refuses_active_requests_and_disabled_environment(monkeypatch):
    from aistudio_api.infrastructure.utils import telemetry

    monkeypatch.setattr(settings, "web_password", "fixture-admin")
    monkeypatch.setenv("AISTUDIO_ALLOW_RESTART", "0")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer fixture-admin"},
    ) as client:
        assert (await client.post("/api/console/restart")).status_code == 409
        monkeypatch.setenv("AISTUDIO_ALLOW_RESTART", "1")
        monkeypatch.setattr(telemetry, "active", {"fixture": object()})
        assert (await client.post("/api/console/restart")).status_code == 409
