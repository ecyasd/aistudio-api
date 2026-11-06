"""Offline protocol and request lifetime regressions; no Google traffic."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from aistudio_api.api.app import app
from aistudio_api.api.dependencies import get_client
from aistudio_api.api.routes_openai import ChatRequest, to_gemini
from aistudio_api.api.state import runtime_state
from aistudio_api.config import settings
from aistudio_api.domain.errors import UsageLimitExceeded
from aistudio_api.domain.models import Candidate, GeneratedImage, ModelOutput
from aistudio_api.infrastructure.gateway.client import AIStudioClient, PreparedRequest
from aistudio_api.infrastructure.utils import telemetry


@pytest.fixture(autouse=True)
def isolated_console(monkeypatch):
    telemetry.active.clear()
    telemetry.completed.clear()
    telemetry.logs.clear()
    monkeypatch.setattr(settings, "web_password", "synthetic-admin")
    monkeypatch.setattr(
        "aistudio_api.infrastructure.gateway.model_defaults.get_configured_api_keys",
        lambda: {"synthetic-call"},
    )
    monkeypatch.setattr(
        "aistudio_api.application.api_service_gemini.ensure_active_account", AsyncMock()
    )
    monkeypatch.setattr(
        "aistudio_api.application.api_service_gemini.record_rotator_event", MagicMock()
    )
    yield
    app.dependency_overrides.clear()
    telemetry.active.clear()


def client(token="synthetic-call"):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


def mock_generation():
    fake = MagicMock(spec=AIStudioClient)
    fake.generate_content = AsyncMock(
        return_value=ModelOutput(
            candidates=[Candidate(text="synthetic body", thinking="private reasoning")],
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        )
    )
    app.dependency_overrides[get_client] = lambda: fake
    return fake


@pytest.mark.asyncio
async def test_nonstream_chat_and_private_diagnostics():
    mock_generation()
    async with client() as http:
        response = await http.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-3.7-flash",
                "messages": [
                    {"role": "system", "content": "secret system prompt"},
                    {"role": "user", "content": "secret user prompt"},
                ],
            },
        )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "synthetic body"
    assert response.json()["usage"]["total_tokens"] == 30
    async with client("synthetic-admin") as http:
        requests = (await http.get("/api/console/requests")).json()["items"]
        logs = (await http.get("/api/console/logs")).json()["items"]
    assert requests[0]["id"] == response.headers["x-request-id"]
    assert requests[0]["state"] == "completed"
    assert requests[0]["body_chars"] == len("synthetic body")
    assert requests[0]["usage"]["total_tokens"] == 30
    for secret in (
        "secret system prompt",
        "secret user prompt",
        "synthetic body",
        "private reasoning",
        "synthetic-admin",
        "synthetic-call",
    ):
        assert secret not in json.dumps([requests, logs])


@pytest.mark.asyncio
async def test_stream_reasoning_usage_and_terminal_chunk():
    fake = mock_generation()

    async def stream(**_kwargs):
        yield "thinking", "synthetic thought"
        yield "body", "Hello "
        yield "body", "world"
        yield "finish_reason", 1
        yield "usage", {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    fake.stream_generate_content = stream
    async with client() as http:
        response = await http.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-3.7-flash",
                "stream": True,
                "stream_options": {"include_usage": True},
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    frames = [
        value.removeprefix("data: ") for value in response.text.strip().split("\n\n")
    ]
    assert frames[-1] == "[DONE]"
    chunks = [json.loads(frame) for frame in frames[:-1]]
    assert chunks[-1]["choices"] == []
    assert chunks[-1]["usage"]["total_tokens"] == 15
    assert (
        "".join(
            chunk["choices"][0]["delta"].get("content", "")
            for chunk in chunks
            if chunk["choices"]
        )
        == "Hello world"
    )
    assert any(
        chunk["choices"]
        and chunk["choices"][0]["delta"].get("reasoning_content") == "synthetic thought"
        for chunk in chunks
    )
    assert telemetry.completed[-1].state == "completed"
    assert telemetry.completed[-1].timings.keys() >= {"first_body", "first_thought"}


@pytest.mark.asyncio
async def test_stream_error_is_failed_without_done():
    fake = mock_generation()

    async def stream(**_kwargs):
        yield "body", "partial"
        raise UsageLimitExceeded("synthetic failure")

    fake.stream_generate_content = stream
    async with client() as http:
        response = await http.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-3.7-flash",
                "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    assert '"error"' in response.text
    assert "[DONE]" not in response.text
    assert telemetry.completed[-1].state == "failed"
    assert telemetry.completed[-1].status == 200
    assert telemetry.completed[-1].error_status == 429


@pytest.mark.asyncio
async def test_missing_terminal_marker_is_failed_without_done():
    fake = mock_generation()

    async def stream(**_kwargs):
        yield "body", "truncated body"
        yield "usage", {"total_tokens": 15}

    fake.stream_generate_content = stream
    async with client() as http:
        response = await http.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-3.7-flash",
                "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    assert '"error"' in response.text
    assert "[DONE]" not in response.text
    assert telemetry.completed[-1].state == "failed"
    assert telemetry.completed[-1].error_status == 502


@pytest.mark.asyncio
async def test_function_calls_stream_and_nonstream():
    fake = mock_generation()
    function = {"id": "call_test", "name": "lookup", "args": {"city": "Tokyo"}}
    fake.generate_content.return_value = ModelOutput(
        candidates=[Candidate(function_calls=[function], finish_reason=1)]
    )

    async def stream(**_kwargs):
        yield "tool_calls", [function]
        yield "finish_reason", 1

    fake.stream_generate_content = stream
    payload = {
        "model": "gemini-3.7-flash",
        "messages": [{"role": "user", "content": "hello"}],
    }
    async with client() as http:
        response = await http.post("/v1/chat/completions", json=payload)
        choice = response.json()["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        call = choice["message"]["tool_calls"][0]
        assert call["id"] == "call_test" and "index" not in call
        assert json.loads(call["function"]["arguments"]) == {"city": "Tokyo"}
        response = await http.post(
            "/v1/chat/completions", json={**payload, "stream": True}
        )
        frames = [
            json.loads(value.removeprefix("data: "))
            for value in response.text.strip().split("\n\n")[:-1]
        ]
        assert frames[1]["choices"][0]["delta"]["tool_calls"][0]["index"] == 0
        assert frames[-1]["choices"][0]["finish_reason"] == "tool_calls"


@pytest.mark.asyncio
async def test_unsupported_stream_output_returns_error_event():
    fake = mock_generation()

    async def stream(**_kwargs):
        yield "images", [GeneratedImage("image/png", b"synthetic-image", 15)]
        yield "finish_reason", 1

    fake.stream_generate_content = stream
    async with client() as http:
        response = await http.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-3.7-flash",
                "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    assert '"error"' in response.text
    assert "[DONE]" not in response.text
    assert telemetry.completed[-1].state == "failed"
    assert telemetry.completed[-1].error_status == 400


@pytest.mark.asyncio
async def test_management_bearer_only_and_key_isolation(monkeypatch):
    mock_generation()
    async with client() as http:
        assert (await http.get("/api/console/requests")).status_code == 401
        assert (
            await http.get("/api/console/logs?key=synthetic-admin")
        ).status_code == 401
    async with client("synthetic-admin") as http:
        assert (await http.get("/v1/models")).status_code == 401
        assert (
            await http.get("/api/console/requests?key=synthetic-admin")
        ).status_code == 401
    monkeypatch.setattr(
        "aistudio_api.infrastructure.gateway.model_defaults.get_configured_api_keys",
        lambda: set(),
    )
    monkeypatch.setattr(settings, "api_keys", frozenset({"synthetic-call"}))
    async with client() as http:
        assert (await http.get("/v1/models")).status_code == 401


@pytest.mark.asyncio
async def test_cancel_only_target_request_and_cleanup():
    fake = mock_generation()
    started = asyncio.Event()
    released = asyncio.Event()

    async def generate(**_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            released.set()

    fake.generate_content = generate
    async with client() as http, client("synthetic-admin") as admin:
        task = asyncio.create_task(
            http.post(
                "/v1/chat/completions",
                json={
                    "model": "gemini-3.7-flash",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )
        )
        await asyncio.wait_for(started.wait(), 2)
        snapshot = (await admin.get("/api/console/requests")).json()["items"][0]
        assert snapshot["state"] == "running"
        assert (
            await admin.post(f"/api/console/requests/{snapshot['id']}/cancel")
        ).status_code == 200
        await asyncio.wait_for(released.wait(), 2)
        result = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result[0], httpx.Response)
        assert result[0].status_code == 499
        assert not telemetry.active
        assert telemetry.completed[-1].state == "cancelled"
        assert (
            await admin.post(f"/api/console/requests/{snapshot['id']}/cancel")
        ).status_code == 409


def test_tools_system_and_parameters():
    req = ChatRequest.model_validate(
        {
            "model": "gemini-3.7-flash",
            "max_tokens": 256,
            "stop": ["END"],
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "hello"},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "arguments": '{"city":"Tokyo"}',
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "lookup", "parameters": {"type": "object"}},
                }
            ],
            "tool_choice": "required",
        }
    )
    converted = to_gemini(req)
    assert converted.systemInstruction is not None
    assert converted.generationConfig is not None
    assert converted.contents[-1].parts[0].functionResponse is not None
    assert converted.systemInstruction.parts[0].text == "system"
    assert converted.generationConfig.maxOutputTokens == 256
    assert converted.contents[-1].parts[0].functionResponse.name == "lookup"
    assert converted.toolConfig == {"functionCallingConfig": {"mode": "ANY"}}


def test_observation_retention_and_allowlist(monkeypatch):
    telemetry.completed.clear()
    for index in range(telemetry.MAX_COMPLETED + 50):
        telemetry.completed.append(telemetry.Observation(f"req_{index}", "gemini"))
    assert len(telemetry.completed) == telemetry.MAX_COMPLETED
    item = telemetry.completed[-1]
    token = telemetry.current.set(item)
    try:
        telemetry.bind_request("secret-user-input", False)
        telemetry.event("untrusted event text")
        telemetry.result({"prompt_tokens": 10, "raw_body": "secret"}, "secret-finish")
    finally:
        telemetry.current.reset(token)
    assert item.model is None
    assert item.usage == {"prompt_tokens": 10}
    assert item.finish_reason == "OTHER"
    monkeypatch.setattr(
        telemetry.time,
        "time",
        lambda: item.started_at + telemetry.RETENTION_SECONDS + 10,
    )
    telemetry.prune()
    assert not telemetry.completed


@pytest.mark.asyncio
async def test_prepared_replay_is_request_local():
    gateway = AIStudioClient()
    first = PreparedRequest("test", {}, "one")
    second = PreparedRequest("test", {}, "two")
    barrier = asyncio.Event()

    async def one():
        gateway.last_prepared_request = first
        await barrier.wait()
        assert gateway.last_prepared_request is first

    async def two():
        gateway.last_prepared_request = second
        barrier.set()
        await asyncio.sleep(0)
        assert gateway.last_prepared_request is second

    try:
        await asyncio.gather(one(), two())
        assert gateway.last_prepared_request is None
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_cancel_stream_closes_generator_without_success_marker():
    fake = mock_generation()
    started = asyncio.Event()
    released = asyncio.Event()

    async def stream(**_kwargs):
        try:
            yield "body", "partial"
            started.set()
            await asyncio.Event().wait()
        finally:
            released.set()

    fake.stream_generate_content = stream
    async with client() as http, client("synthetic-admin") as admin:
        task = asyncio.create_task(
            http.post(
                "/v1/chat/completions",
                json={
                    "model": "gemini-3.7-flash",
                    "stream": True,
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )
        )
        await asyncio.wait_for(started.wait(), 2)
        request_id = next(iter(telemetry.active))
        assert (
            await admin.post(f"/api/console/requests/{request_id}/cancel")
        ).status_code == 200
        response = await asyncio.wait_for(task, 2)
        assert "Request cancelled" in response.text
        assert "[DONE]" not in response.text
        assert released.is_set()
        assert not telemetry.active
        assert telemetry.completed[-1].state == "cancelled"
        # The shared client remains available to the next independent request.
        healthy = await http.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-3.7-flash",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        assert healthy.status_code == 200
        assert telemetry.completed[-1].state == "completed"


@pytest.mark.asyncio
async def test_client_disconnect_releases_nonstream_generation():
    fake = mock_generation()
    started = asyncio.Event()
    released = asyncio.Event()

    async def generate(**_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            released.set()

    fake.generate_content = generate
    incoming = asyncio.Queue()
    await incoming.put(
        {
            "type": "http.request",
            "body": json.dumps(
                {
                    "model": "gemini-3.7-flash",
                    "messages": [{"role": "user", "content": "hello"}],
                }
            ).encode(),
            "more_body": False,
        }
    )
    sent = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "server": ("test", 80),
        "client": ("test", 1000),
        "headers": [
            (b"authorization", b"Bearer synthetic-call"),
            (b"content-type", b"application/json"),
        ],
    }
    task = asyncio.create_task(app(scope, incoming.get, send))
    await asyncio.wait_for(started.wait(), 2)
    await incoming.put({"type": "http.disconnect"})
    await asyncio.wait_for(task, 2)
    assert released.is_set()
    assert not telemetry.active
    assert telemetry.completed[-1].state == "cancelled"
    assert not sent


@pytest.mark.asyncio
async def test_configuration_preserves_current_keys_and_rejects_bad_level(
    tmp_path, monkeypatch
):
    import yaml

    config = tmp_path / "config.yaml"
    config.write_text(
        "api_keys: [synthetic-call]\nmodel_defaults: {}\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        "aistudio_api.api.routes_system._resolve_config_path", lambda _value: config
    )
    async with client("synthetic-admin") as http:
        response = await http.put(
            "/config/yaml", json={"yaml_content": "api_keys: []\nmodel_defaults: {}\n"}
        )
        assert response.status_code == 200
        assert yaml.safe_load(config.read_text(encoding="utf-8"))["api_keys"] == [
            "synthetic-call"
        ]
        invalid = await http.put("/config/logging", json={"level": "NOT_A_LEVEL"})
        assert invalid.status_code == 400


@pytest.mark.asyncio
async def test_existing_account_selection_restored_only_on_generation(
    tmp_path, monkeypatch
):
    from aistudio_api.application.account_orchestrator import ensure_active_account

    account = MagicMock(id="acc_1", tier="Free")
    service = MagicMock()
    service.get_active_account.return_value = account
    service.get_account_auth_path.return_value = tmp_path / "synthetic-auth.json"
    fake = MagicMock(spec=AIStudioClient)
    fake.session.active_context = None
    fake.switch_auth = AsyncMock()
    monkeypatch.setattr(runtime_state, "account_service", service)
    monkeypatch.setattr(runtime_state, "client", fake)
    monkeypatch.setattr(runtime_state, "rotator", None)
    await ensure_active_account(0, "gemini-3.7-flash")
    fake.switch_auth.assert_awaited_once_with(str(tmp_path / "synthetic-auth.json"))
