"""Synthetic HTTP coverage for selectable anti-truncation model aliases."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from aistudio_api.api.app import app
from aistudio_api.api.schemas import GeminiGenerateContentRequest
from aistudio_api.api.state import runtime_state
from aistudio_api.application import api_service_gemini as api
from aistudio_api.application.account_rotator import AccountStats
from aistudio_api.application.chat_service import normalize_gemini_request
from aistudio_api.domain.models import Candidate, ContentPart, ModelOutput
from aistudio_api.infrastructure.gateway import model_defaults
from aistudio_api.infrastructure.gateway.model_discovery import ModelDiscoveryService

BASE_MODEL = "models/gemini-offline"
ALIAS_MODEL = "models/抗截断/gemini-offline"


@pytest.fixture
def alias_env(monkeypatch, tmp_path):
    models = [
        {"id": "gemini-offline", "displayName": "Synthetic Gemini",
         "description": "Offline text model", "inputTokenLimit": 12345,
         "outputTokenLimit": 678},
        {"id": "gemma-offline", "displayName": "Synthetic Gemma"},
        {"id": "gemini-offline-image", "is_image_model": True},
        {"id": "gemini-offline-special", "is_image_model": True},
        {"id": "veo-offline", "is_video_model": True},
        {"id": "gemini-offline-tts", "is_audio_model": True},
        {"id": "gemini-offline-audio", "is_audio_model": True},
        {"id": "gemini-embedding-offline"},
    ]
    discovery = AsyncMock(return_value=models)
    # Never run discovery against a browser or an account file in these tests.
    monkeypatch.setattr(ModelDiscoveryService, "get_models", discovery)
    monkeypatch.setattr(
        ModelDiscoveryService, "_fetch_via_page",
        AsyncMock(side_effect=AssertionError("unexpected upstream model discovery")),
    )
    monkeypatch.setattr(
        ModelDiscoveryService, "_fetch_via_http",
        AsyncMock(side_effect=AssertionError("unexpected upstream model discovery")),
    )
    monkeypatch.setattr(model_defaults, "get_configured_api_keys", lambda: {"offline-key"})
    context = SimpleNamespace(auth_file=str(tmp_path / "fake-auth.json"), context_id=31)
    account = SimpleNamespace(id="offline-account")
    stats = AccountStats(account_id=account.id)
    leases = []
    released = []
    calls = []
    stream_closed = []

    @asynccontextmanager
    async def request_scope():
        assert not leases
        leases.append(context)
        try:
            yield context
        finally:
            leases.remove(context)
            released.append(context.context_id)

    def output(index):
        text = "first " if index == 0 else "second"
        return ModelOutput(
            candidates=[Candidate(
                text=text, parts=[ContentPart(text=text)],
                finish_reason=2 if index == 0 else 1,
            )],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )

    async def generate(**kwargs):
        assert leases == [context]
        calls.append(kwargs)
        return output(len(calls) - 1)

    async def generate_stream(**kwargs):
        assert leases == [context]
        calls.append(kwargs)
        result = output(len(calls) - 1)
        try:
            yield "parts", result.candidates[0].parts
            yield "finish_reason", result.candidates[0].finish_reason
            yield "usage", result.usage
            yield "done", None
        finally:
            stream_closed.append(True)

    client = SimpleNamespace(
        session=SimpleNamespace(
            _auth_file=context.auth_file, is_ready=True, request_scope=request_scope,
        ),
        generate_content=AsyncMock(side_effect=generate),
        stream_generate_content=MagicMock(side_effect=generate_stream),
        clear_templates=MagicMock(),
    )
    client._session = client.session
    rotator = SimpleNamespace(
        get_account_stats=lambda _: stats,
        is_account_available=lambda _, model=None: stats.is_available(model),
        record_success=MagicMock(side_effect=lambda _, model=None: stats.record_success(model)),
        record_rate_limited=MagicMock(), record_auth_error=MagicMock(),
        record_session_expired=MagicMock(), record_error=MagicMock(),
    )
    monkeypatch.setattr(runtime_state, "client", client)
    monkeypatch.setattr(runtime_state, "account_service", SimpleNamespace(
        list_accounts=lambda: [account], get_active_account=lambda: account,
        get_account_auth_path=lambda _: context.auth_file,
    ))
    monkeypatch.setattr(runtime_state, "rotator", rotator)
    monkeypatch.setattr(runtime_state, "record", MagicMock())
    monkeypatch.setattr(api, "try_switch_account", AsyncMock(return_value=False))
    original_ensure = api.ensure_active_account
    ensure = AsyncMock(wraps=original_ensure)
    monkeypatch.setattr(api, "ensure_active_account", ensure)
    return SimpleNamespace(
        models=models, models_before=deepcopy(models), discovery=discovery,
        client=client, stats=stats, leases=leases, released=released,
        calls=calls, stream_closed=stream_closed, ensure=ensure,
    )


def _http():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://offline",
        headers={"x-goog-api-key": "offline-key"},
    )


def _sse_payloads(response):
    return [json.loads(line[6:]) for line in response.text.splitlines()
            if line.startswith("data: ")]


async def test_http_catalog_includes_text_aliases_without_mutating_discovery(alias_env):
    env = alias_env
    async with _http() as http:
        response = await http.get("/v1beta/models")
    assert response.status_code == 200
    listed = response.json()["models"]
    names = [item["name"] for item in listed]
    assert names.count(ALIAS_MODEL) == 1
    assert "models/抗截断/gemma-offline" in names
    for model in env.models:
        assert "models/" + model["id"] in names
    assert not any(
        item["name"].startswith("models/抗截断/")
        and item["name"] not in {ALIAS_MODEL, "models/抗截断/gemma-offline"}
        for item in listed
    )
    selected = next(item for item in listed if item["name"] == ALIAS_MODEL)
    assert selected["inputTokenLimit"] == 12345
    assert selected["outputTokenLimit"] == 678
    assert selected["supportedGenerationMethods"] == ["generateContent"]
    assert env.models == env.models_before
    assert not env.calls
    assert not env.leases


async def test_http_single_alias_matches_catalog_and_unknown_alias_is_404(alias_env):
    async with _http() as http:
        listing = (await http.get("/v1beta/models")).json()["models"]
        response = await http.get("/v1beta/" + ALIAS_MODEL)
        missing = await http.get("/v1beta/models/抗截断/gemini-unknown-offline-987")
    assert response.status_code == 200
    assert response.json() == next(item for item in listing if item["name"] == ALIAS_MODEL)
    assert missing.status_code == 404
    assert missing.json()["error"]["status"] == "NOT_FOUND"
    assert alias_env.models == alias_env.models_before
    assert not alias_env.calls


@pytest.mark.parametrize("model", ["gemini-offline-image", "gemini-offline-special"])
async def test_http_single_nontext_model_has_no_antitruncation_alias(alias_env, model):
    async with _http() as http:
        response = await http.get("/v1beta/models/抗截断/" + model)
    assert response.status_code == 404
    assert response.json()["error"]["status"] == "NOT_FOUND"
    assert not alias_env.calls


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    ("model", "body_flag", "expected_rounds"),
    [(BASE_MODEL, False, 1), (BASE_MODEL, True, 2),
     (ALIAS_MODEL, False, 2), (ALIAS_MODEL, True, 2)],
)
async def test_http_selection_reaches_upstream_admission_and_stats_as_base(
    alias_env, stream, model, body_flag, expected_rounds,
):
    env = alias_env
    payload = {
        "contents": [{"parts": [{"text": "offline prompt"}]}],
        "tools": [], "autoContinue": body_flag,
        "generationConfig": {"maxOutputTokens": 5},
    }
    original = deepcopy(payload)
    method = "streamGenerateContent" if stream else "generateContent"
    async with _http() as http:
        response = await http.post(f"/v1beta/{model}:{method}", json=payload)
    assert response.status_code == 200
    frames = _sse_payloads(response) if stream else [response.json()]
    assert all("error" not in frame for frame in frames)
    text = "".join(
        part.get("text", "")
        for frame in frames for candidate in frame.get("candidates", [])
        for part in candidate["content"]["parts"]
    )
    assert text == ("first second" if expected_rounds == 2 else "first ")
    assert all(frame["modelVersion"] == BASE_MODEL for frame in frames)
    assert frames[-1]["candidates"][0]["finishReason"] == (
        "STOP" if expected_rounds == 2 else "MAX_TOKENS"
    )
    assert frames[-1]["usageMetadata"]["totalTokenCount"] == 15 * expected_rounds
    assert len(env.calls) == expected_rounds
    assert all(call["model"] == BASE_MODEL for call in env.calls)
    assert all(call["max_tokens"] == 5 for call in env.calls)
    assert all(not call["tools"] for call in env.calls)
    env.ensure.assert_awaited_once_with(0, model=BASE_MODEL)
    runtime_state.rotator.record_success.assert_called_once_with(
        "offline-account", model=BASE_MODEL,
    )
    assert runtime_state.record.call_args.args[:2] == (BASE_MODEL, "success")
    assert env.stats.model_requests == {"gemini-offline": 1}
    assert env.released == [31]
    assert not env.leases
    assert payload == original
    if stream:
        assert len(env.stream_closed) == expected_rounds


@pytest.mark.parametrize("stream", [False, True])
async def test_alias_respects_existing_base_model_cooldown_before_upstream(alias_env, stream):
    env = alias_env
    env.stats.record_rate_limited(BASE_MODEL)
    method = "streamGenerateContent" if stream else "generateContent"
    async with _http() as http:
        response = await http.post(
            f"/v1beta/{ALIAS_MODEL}:{method}",
            json={"contents": [{"parts": [{"text": "offline"}]}], "tools": []},
        )
    assert response.status_code == 429
    assert not env.calls
    assert not env.leases
    assert set(env.stats.model_cooldowns) == {"gemini-offline"}


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("selector", ["抗截断/", "抗截断/抗截断/gemini-offline"])
async def test_invalid_alias_fails_before_account_admission(alias_env, stream, selector):
    method = "streamGenerateContent" if stream else "generateContent"
    async with _http() as http:
        response = await http.post(
            f"/v1beta/models/{selector}:{method}",
            json={"contents": [{"parts": [{"text": "offline"}]}]},
        )
    assert response.status_code == 400
    assert response.json()["error"]["status"] == "INVALID_ARGUMENT"
    alias_env.ensure.assert_not_awaited()
    assert not alias_env.calls
    assert not alias_env.leases


@pytest.mark.parametrize("stream", [False, True])
async def test_alias_enables_mode_without_mutating_request_or_explicit_tools(
    alias_env, stream, monkeypatch,
):
    env = alias_env
    request = GeminiGenerateContentRequest.model_validate({
        "contents": [{"parts": [{"text": "offline prompt"}]}],
        "tools": [{"functionDeclarations": [{
            "name": "offline_function", "parameters": {"type": "OBJECT"},
        }]}],
        "autoContinue": False,
    })
    before = request.model_dump()
    expected_tools = normalize_gemini_request(request, BASE_MODEL).tools
    flag_calls = []
    name = "stream_generate_content" if stream else "generate_content"
    original = getattr(api.continuation, name)

    def observed(*args, **kwargs):
        flag_calls.append(kwargs["auto_continue"])
        return original(*args, **kwargs)

    monkeypatch.setattr(api.continuation, name, observed)
    response = await api.handle_gemini_generate_content(
        ALIAS_MODEL, request, env.client, stream=stream,
    )
    if stream:
        _ = [frame async for frame in response.body_iterator]
    assert flag_calls == [True]
    assert env.calls
    assert all(call["model"] == BASE_MODEL for call in env.calls)
    assert all(call["tools"] == expected_tools for call in env.calls)
    assert expected_tools
    assert request.model_dump() == before
    assert request.autoContinue is False
    assert not env.leases
