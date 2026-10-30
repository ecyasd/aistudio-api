"""E2E and compatibility tests using the official google-genai SDK."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from google import genai
from google.genai import errors, types

from aistudio_api.api.app import app
from aistudio_api.api.dependencies import get_client
from aistudio_api.domain.errors import RequestError, UsageLimitExceeded
from aistudio_api.domain.models import Candidate, ContentPart, ModelOutput


@pytest.fixture
def mock_client(monkeypatch, tmp_path) -> MagicMock:
    """Supply account metadata and a leased fake browser to the real API path."""
    from aistudio_api.api.state import runtime_state
    from aistudio_api.application import api_service_gemini
    from aistudio_api.application.account_rotator import AccountStats
    from aistudio_api.infrastructure.gateway import model_defaults

    monkeypatch.setattr(model_defaults, "get_configured_api_keys", lambda: {"test-key"})

    client = MagicMock()
    account = SimpleNamespace(id="sdk-offline-account")
    auth_path = str(tmp_path / "fake-auth.json")
    context = SimpleNamespace(auth_file=auth_path, context_id=1)
    stats = AccountStats(account_id=account.id)

    @asynccontextmanager
    async def request_scope():
        yield context

    client.session = SimpleNamespace(
        _auth_file=auth_path, is_ready=True, request_scope=request_scope
    )
    account_service = SimpleNamespace(
        list_accounts=lambda: [account],
        get_active_account=lambda: account,
        get_account_auth_path=lambda account_id: auth_path,
    )
    rotator = SimpleNamespace(
        get_account_stats=lambda account_id: stats,
        is_account_available=lambda account_id, model=None: True,
        record_success=MagicMock(),
        record_rate_limited=MagicMock(),
        record_auth_error=MagicMock(),
        record_session_expired=MagicMock(),
        record_error=MagicMock(),
    )
    monkeypatch.setattr(runtime_state, "client", client)
    monkeypatch.setattr(runtime_state, "account_service", account_service)
    monkeypatch.setattr(runtime_state, "rotator", rotator)
    monkeypatch.setattr(runtime_state, "record", MagicMock())
    monkeypatch.setattr(api_service_gemini, "try_switch_account", AsyncMock(return_value=False))
    monkeypatch.setattr(api_service_gemini, "SSE_HEARTBEAT_INTERVAL_SECONDS", 0.005)
    return client


@pytest.mark.asyncio
async def test_genai_sdk_aio_non_streaming(mock_client: MagicMock) -> None:
    """Test async non-streaming generation with official Google GenAI SDK."""
    mock_client.generate_content = AsyncMock(
        return_value=ModelOutput(
            candidates=[Candidate(text="Hello from AI Studio!", finish_reason=1)],
            usage={
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "total_tokens": 30,
            },
        )
    )

    app.dependency_overrides[get_client] = lambda: mock_client

    try:
        sdk_client = genai.Client(
            api_key="test-key",
            http_options=types.HttpOptions(
                base_url="http://testserver",
            ),
        )
        async with sdk_client.aio as aio_client:
            transport_obj = httpx.ASGITransport(app=app)
            aio_client._api_client._async_httpx_client = httpx.AsyncClient(
                transport=transport_obj,
                base_url="http://testserver",
            )

            response = await aio_client.models.generate_content(
                model="gemini-3.8-flash",
                contents="Hi",
            )

            assert response.text == "Hello from AI Studio!"
            assert response.candidates is not None
            assert len(response.candidates) == 1
            candidate = response.candidates[0]
            assert candidate.index == 0
            assert candidate.finish_reason == types.FinishReason.STOP
            assert response.usage_metadata is not None
            assert response.usage_metadata.prompt_token_count == 10
            assert response.usage_metadata.candidates_token_count == 20
            assert response.usage_metadata.total_token_count == 30
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("stream", [False, True])
async def test_genai_sdk_opt_in_continuation_keeps_one_account_and_sums_usage(
    mock_client: MagicMock, stream: bool,
) -> None:
    """The SDK extension reaches both real API handlers without extra leases."""
    from aistudio_api.api.state import runtime_state

    calls: list[dict[str, object]] = []
    closed: list[bool] = []

    def output(index: int) -> ModelOutput:
        text = ["first ", "second"][index]
        return ModelOutput(
            candidates=[Candidate(
                text=text, parts=[ContentPart(text=text)],
                finish_reason=[2, 1][index],
            )],
            response_id=f"offline-round-{index}",
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )

    async def generate(**kwargs):
        calls.append(kwargs)
        return output(len(calls) - 1)

    async def generate_stream(**kwargs):
        calls.append(kwargs)
        result = output(len(calls) - 1)
        try:
            yield "response_id", result.response_id
            yield "parts", result.candidates[0].parts
            yield "finish_reason", result.candidates[0].finish_reason
            yield "usage", result.usage
            yield "done", None
        finally:
            closed.append(True)

    mock_client.generate_content = generate
    mock_client.stream_generate_content = generate_stream
    app.dependency_overrides[get_client] = lambda: mock_client
    try:
        sdk = genai.Client(
            api_key="test-key", http_options=types.HttpOptions(base_url="http://testserver"),
        )
        config = types.GenerateContentConfig(
            tools=[], max_output_tokens=5,
            http_options=types.HttpOptions(extra_body={"autoContinue": True}),
        )
        async with sdk.aio as aio:
            aio._api_client._async_httpx_client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver",
            )
            if stream:
                result = [part async for part in await aio.models.generate_content_stream(
                    model="continuation-offline", contents="offline prompt", config=config,
                )]
                assert "".join(part.text or "" for part in result) == "first second"
                final = result[-1]
                assert closed == [True, True]
            else:
                final = await aio.models.generate_content(
                    model="continuation-offline", contents="offline prompt", config=config,
                )
                assert final.text == "first second"
            assert final.candidates[0].finish_reason == types.FinishReason.STOP
            assert final.usage_metadata.prompt_token_count == 20
            assert final.usage_metadata.candidates_token_count == 10
            assert final.usage_metadata.total_token_count == 30
        assert len(calls) == 2
        assert all(call["max_tokens"] == 5 for call in calls)
        runtime_state.rotator.record_success.assert_called_once()
        assert runtime_state.record.call_args.args[2]["total_tokens"] == 30
    finally:
        app.dependency_overrides.clear()


async def test_nonstream_failed_continuation_never_replays_first_round(
    mock_client: MagicMock,
) -> None:
    from aistudio_api.api.state import runtime_state
    from aistudio_api.domain.errors import IncompleteResponseError

    mock_client.generate_content = AsyncMock(side_effect=[
        ModelOutput(candidates=[Candidate(
            text="partial", parts=[ContentPart(text="partial")], finish_reason=2,
        )]),
        IncompleteResponseError(),
    ])
    app.dependency_overrides[get_client] = lambda: mock_client
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver",
        ) as http:
            response = await http.post(
                "/v1beta/models/continuation-offline:generateContent",
                headers={"x-goog-api-key": "test-key"},
                json={"contents": [{"parts": [{"text": "offline prompt"}]}],
                      "tools": [], "autoContinue": True},
            )
        assert response.status_code == 502
        assert mock_client.generate_content.await_count == 2
        runtime_state.rotator.record_success.assert_not_called()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("value", ["true", 1, None, [], {}])
async def test_auto_continue_requires_explicit_json_boolean(
    mock_client: MagicMock, value: object,
) -> None:
    app.dependency_overrides[get_client] = lambda: mock_client
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver",
        ) as http:
            response = await http.post(
                "/v1beta/models/continuation-offline:generateContent",
                headers={"x-goog-api-key": "test-key"},
                json={"contents": [{"parts": [{"text": "offline prompt"}]}],
                      "autoContinue": value},
            )
        assert response.status_code == 400
        mock_client.generate_content.assert_not_called()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("stream", [False, True])
def test_continuation_deadline_has_gemini_deadline_status(stream: bool) -> None:
    from fastapi import HTTPException

    from aistudio_api.application import api_service_gemini as api

    error = RequestError(504, "Automatic continuation exceeded the request time budget")
    classified = error if stream else HTTPException(504, detail={"message": str(error)})
    assert api.classify_gemini_error_payload(classified)[::2] == (504, "DEADLINE_EXCEEDED")


@pytest.mark.asyncio
async def test_genai_sdk_aio_streaming(mock_client: MagicMock) -> None:
    """Test async streaming generation with official Google GenAI SDK."""

    async def fake_stream(
        *_args: object, **_kwargs: object
    ) -> AsyncGenerator[tuple[str, object], None]:
        # Exercise SDK skipping of keepalive separators before any model chunk.
        await asyncio.sleep(0.02)
        yield ("thinking", "Thinking about the greeting...")
        yield ("body", "Hello ")
        yield ("body", "World!")
        yield ("finish_reason", 1)
        yield (
            "usage",
            {
                "prompt_tokens": 5,
                "completion_tokens": 15,
                "total_tokens": 20,
            },
        )

    mock_client.stream_generate_content = fake_stream
    app.dependency_overrides[get_client] = lambda: mock_client

    try:
        sdk_client = genai.Client(
            api_key="test-key",
            http_options=types.HttpOptions(
                base_url="http://testserver",
            ),
        )
        async with sdk_client.aio as aio_client:
            transport_obj = httpx.ASGITransport(app=app)
            aio_client._api_client._async_httpx_client = httpx.AsyncClient(
                transport=transport_obj,
                base_url="http://testserver",
            )

            chunks = []
            async for chunk in await aio_client.models.generate_content_stream(
                model="gemini-3.8-flash",
                contents="Hi",
            ):
                chunks.append(chunk)

            assert len(chunks) == 4

            # Chunk 0: thinking
            assert chunks[0].candidates is not None
            cand0 = chunks[0].candidates[0]
            assert cand0.content is not None
            assert cand0.content.parts is not None
            assert cand0.content.parts[0].thought is True
            assert cand0.content.parts[0].text == "Thinking about the greeting..."
            assert cand0.index == 0

            # Chunk 1: text part 1
            assert chunks[1].text == "Hello "
            assert chunks[1].candidates is not None
            assert chunks[1].candidates[0].index == 0

            # Chunk 2: text part 2
            assert chunks[2].text == "World!"
            assert chunks[2].candidates is not None
            assert chunks[2].candidates[0].index == 0

            # Chunk 3: usage + STOP finishReason
            assert chunks[3].candidates is not None
            assert chunks[3].candidates[0].finish_reason == types.FinishReason.STOP
            assert chunks[3].usage_metadata is not None
            assert chunks[3].usage_metadata.total_token_count == 20
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_genai_sdk_function_calling_and_response_flow(
    mock_client: MagicMock,
) -> None:
    """Test full multi-turn function calling flow with Google GenAI SDK."""
    captured_requests: list[object] = []

    async def fake_generate(
        model: str,
        contents: list[object],
        **kwargs: object,
    ) -> ModelOutput:
        captured_requests.append((model, contents))
        if len(captured_requests) == 1:
            return ModelOutput(
                candidates=[
                    Candidate(
                        text="",
                        function_calls=[
                            {
                                "name": "get_current_weather",
                                "args": {"location": "San Francisco"},
                            }
                        ],
                    )
                ],
                usage={"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
            )
        return ModelOutput(
            candidates=[
                Candidate(
                    text="The current weather in San Francisco is sunny and 18°C."
                )
            ],
            usage={"prompt_tokens": 25, "completion_tokens": 15, "total_tokens": 40},
        )

    mock_client.generate_content = AsyncMock(side_effect=fake_generate)
    app.dependency_overrides[get_client] = lambda: mock_client

    try:
        sdk_client = genai.Client(
            api_key="test-key",
            http_options=types.HttpOptions(base_url="http://testserver"),
        )
        async with sdk_client.aio as aio_client:
            aio_client._api_client._async_httpx_client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            )

            # Turn 1: user asks question, model returns function call
            turn1_resp = await aio_client.models.generate_content(
                model="gemini-3.8-flash",
                contents="What is the weather in San Francisco?",
            )
            assert turn1_resp.function_calls is not None
            assert len(turn1_resp.function_calls) == 1
            assert turn1_resp.function_calls[0].name == "get_current_weather"
            assert turn1_resp.function_calls[0].args == {"location": "San Francisco"}

            # Turn 2: user supplies function execution result, model answers
            turn2_contents = [
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_text(
                            text="What is the weather in San Francisco?"
                        )
                    ],
                ),
                types.Content(
                    role="model",
                    parts=[
                        types.Part.from_function_call(
                            name="get_current_weather",
                            args={"location": "San Francisco"},
                        )
                    ],
                ),
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_function_response(
                            name="get_current_weather",
                            response={"temp": "18C", "condition": "sunny"},
                        )
                    ],
                ),
            ]
            turn2_resp = await aio_client.models.generate_content(
                model="gemini-3.8-flash",
                contents=turn2_contents,
            )
            assert (
                turn2_resp.text
                == "The current weather in San Francisco is sunny and 18°C."
            )
            assert len(captured_requests) == 2
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_genai_sdk_standard_api_error_parsing(mock_client: MagicMock) -> None:
    """Verify google-genai SDK APIError parses standard Google error format."""
    mock_client.generate_content = AsyncMock(
        side_effect=UsageLimitExceeded("Daily quota exhausted for model")
    )
    app.dependency_overrides[get_client] = lambda: mock_client

    try:
        sdk_client = genai.Client(
            api_key="test-key",
            http_options=types.HttpOptions(base_url="http://testserver"),
        )
        async with sdk_client.aio as aio_client:
            aio_client._api_client._async_httpx_client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            )
            with pytest.raises(errors.APIError) as exc_info:
                await aio_client.models.generate_content(
                    model="gemini-3.8-flash",
                    contents="Hello",
                )
            err = exc_info.value
            assert err.code == 429
            assert err.status == "RESOURCE_EXHAUSTED"
            assert "temporarily rate limited" in str(err.message).lower()
            assert "daily quota" not in str(err.message).lower()
            assert "00:00" not in str(err.message)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_genai_sdk_tool_config_forwarding(mock_client: MagicMock) -> None:
    """Verify google-genai SDK tool_config (mode=ANY) is parsed and forwarded."""
    mock_client.generate_content = AsyncMock(
        return_value=ModelOutput(
            candidates=[Candidate(text="Called")],
            usage={"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
        )
    )
    app.dependency_overrides[get_client] = lambda: mock_client

    try:
        sdk_client = genai.Client(
            api_key="test-key",
            http_options=types.HttpOptions(base_url="http://testserver"),
        )
        async with sdk_client.aio as aio_client:
            aio_client._api_client._async_httpx_client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            )
            await aio_client.models.generate_content(
                model="gemini-3.8-flash",
                contents="What is the weather?",
                config=types.GenerateContentConfig(
                    tools=[
                        types.Tool(
                            function_declarations=[
                                types.FunctionDeclaration(
                                    name="get_weather",
                                    description="Get weather",
                                    parameters=types.Schema(
                                        type=types.Type.OBJECT,
                                        properties={
                                            "city": types.Schema(type=types.Type.STRING)
                                        },
                                    ),
                                )
                            ]
                        )
                    ],
                    tool_config=types.ToolConfig(
                        function_calling_config=types.FunctionCallingConfig(
                            mode=types.FunctionCallingConfigMode.ANY,
                        )
                    ),
                ),
            )
            assert mock_client.generate_content.called
            call_kwargs = mock_client.generate_content.call_args.kwargs
            assert "tool_config" in call_kwargs
            assert call_kwargs["tool_config"] == [None, [2]]
    finally:
        app.dependency_overrides.clear()
