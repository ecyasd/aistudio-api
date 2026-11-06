import asyncio
import json
from typing import cast
from unittest.mock import AsyncMock

import pytest

from aistudio_api.domain.errors import TransportError, UsageLimitExceeded
from aistudio_api.infrastructure.gateway.assistant import (
    encode_history,
    stream_assistant,
)
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.gateway.rpc_stream import RPCStreamDecoder
from aistudio_api.infrastructure.gateway.session import GatewaySession, SessionContext
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart


def message(text, *, state=1, number=1, usage=None, thought=False):
    part = [None, text] + [None] * 10 + [int(thought)]
    step: list = [None] * 17
    step[0], step[11], step[12], step[15] = [[[part], "model"]], number, state, usage
    return [None, None, [step], "applet-fixture"]


class FakeSession:
    def __init__(self, messages=None):
        self.last_signer_headers = {"X-Fixture": "signed"}
        self.calls = []
        self.messages = messages or [message("hello", usage=["10", "3", "2"])]

    async def ensure_ready(self):
        return SessionContext(cookies={"SID": "fixture"})

    async def generate_snapshot(self, contents, **kwargs):
        self.signed_contents = contents
        return "fixture-proof"

    async def send_request(self, *, url, body, **kwargs):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, json.loads(body)))
        return 200, json.dumps(
            {
                "CreateApplet": ["applet-fixture"],
                "CodeAssistantOffline": ["generation-fixture", "applet-fixture"],
            }.get(method, [])
        ).encode()

    async def send_streaming_request(self, **kwargs):
        yield "status", 200
        raw = json.dumps([self.messages]).encode()
        for i in range(0, len(raw), 3):
            yield "chunk", raw[i : i + 3]


@pytest.mark.asyncio
async def test_assistant_wire_snapshot_deltas_usage_and_owned_cleanup():
    session = FakeSession(
        [message("你", state=2), message("你好", usage=["10", "3", "2"])]
    )
    events = [
        event
        async for event in stream_assistant(
            cast(GatewaySession, session),
            model="gemini-test-build",
            contents=[AistudioContent("user", [AistudioPart(text="hi")])],
            system=None,
            request={},
        )
    ]
    assert [value for kind, value in events if kind == "body"] == ["你", "好"]
    assert (
        "usage",
        {
            "prompt_tokens": 12,
            "completion_tokens": 3,
            "cached_tokens": 2,
            "total_tokens": 15,
        },
    ) in events
    submitted = next(
        body for method, body in session.calls if method == "CodeAssistantOffline"
    )
    assert submitted[1] == "fixture-proof" and submitted[7] == "models/gemini-test"
    assert submitted[18] == [1, None, None, 3]
    assert session.calls[-1] == ("DeleteApplet", ["applet-fixture"])


@pytest.mark.asyncio
async def test_assistant_missing_terminal_cancels_and_does_not_report_success():
    session = FakeSession([message("partial", state=2)])
    events = []
    with pytest.raises(TransportError):
        async for event in stream_assistant(
            cast(GatewaySession, session),
            model="gemini-test-build",
            contents=[],
            system=None,
            request={},
        ):
            events.append(event)
    assert not any(kind == "done" for kind, _ in events)
    assert any(
        method == "CancelCodeAssistantOfflineGeneration" for method, _ in session.calls
    )
    assert session.calls[-1][0] == "DeleteApplet"


def test_history_preserves_model_roles_and_tool_transcripts():
    encoded = encode_history(
        [
            AistudioContent(
                "model",
                [
                    AistudioPart(text="earlier"),
                    AistudioPart(text="private reasoning", thought=True),
                ],
            ),
            AistudioContent(
                "user",
                [AistudioPart(function_response=("read_file", {"result": "ok"}))],
            ),
        ]
    )
    assert all(item.role == "user" for item in encoded)
    assert "assistant_turn" in (encoded[0].parts[0].text or "")
    assert "private reasoning" not in str(encoded)
    assert "function_results" in (encoded[1].parts[0].text or "")


def test_rpc_trailing_error_and_truncation():
    parser = RPCStreamDecoder()
    with pytest.raises(UsageLimitExceeded):
        list(parser.feed('[[[null,null,[]]],[8,"quota"]]'))
    parser = RPCStreamDecoder()
    list(parser.feed("[[[null,null,[]]"))
    with pytest.raises(TransportError):
        parser.finish()


@pytest.mark.asyncio
async def test_pg_and_assistant_enter_upstream_concurrently(monkeypatch):
    """A shared client must enter both upstream calls before either is released."""
    entered = set()
    both = asyncio.Event()
    release = asyncio.Event()

    async def upstream(channel):
        entered.add(channel)
        if len(entered) == 2:
            both.set()
        await release.wait()
        yield "body", channel
        yield "finish_reason", 1
        yield "done", None

    async def assistant(*args, **kwargs):
        async for item in upstream("assistant"):
            yield item

    async def pg(**kwargs):
        async for item in upstream("playground"):
            yield item

    monkeypatch.setattr(
        "aistudio_api.infrastructure.gateway.assistant.stream_assistant", assistant
    )
    client = AIStudioClient()
    monkeypatch.setattr(client, "prepare_request", AsyncMock(return_value=object()))
    monkeypatch.setattr(client._streaming_gateway, "stream_chat", pg)

    async def collect(model):
        return [item async for item in client.stream_generate_content(model=model)]

    tasks = [
        asyncio.create_task(collect(model))
        for model in ["gemini-test", "gemini-test-build"]
    ]
    await asyncio.wait_for(both.wait(), timeout=1)
    assert entered == {"playground", "assistant"}
    release.set()
    results = await asyncio.gather(*tasks)
    assert results[0][0] == ("body", "playground")
    assert results[1][0] == ("body", "assistant")
    await client.close()


@pytest.mark.asyncio
async def test_assistant_local_stop_handles_boundaries_and_cancels_task():
    session = FakeSession(
        [message("hello<ST", state=2), message("hello<STOP>hidden", state=2)]
    )
    events = [
        item
        async for item in stream_assistant(
            cast(GatewaySession, session),
            model="gemini-test-build",
            contents=[],
            system=None,
            request={"generationConfig": {"stopSequences": ["<STOP>"]}},
        )
    ]
    assert "".join(str(value) for kind, value in events if kind == "body") == "hello"
    assert any(
        method == "CancelCodeAssistantOfflineGeneration" for method, _ in session.calls
    )
    assert events[-1] == ("done", None)


@pytest.mark.asyncio
async def test_assistant_question_tool_is_visible_and_finishes():
    question = message("", number=2)
    action: list = [None] * 22
    action[21] = [
        "question",
        None,
        None,
        [
            ["tag", "ask_question"],
            [
                "questions",
                json.dumps(
                    [{"question": "Which file?", "options": ["app.py", "main.py"]}]
                ),
            ],
        ],
    ]
    question[2][0][9] = action
    session = FakeSession([question])
    events = [
        item
        async for item in stream_assistant(
            cast(GatewaySession, session),
            model="gemini-test-build",
            contents=[],
            system=None,
            request={},
        )
    ]
    assert (
        "".join(str(value) for kind, value in events if kind == "body")
        == "Which file?\n- app.py\n- main.py"
    )


def test_catalog_exposes_pg_and_build_without_conflating_access_modes():
    from aistudio_api.infrastructure.gateway.model_discovery import (
        ModelDiscoveryService,
    )

    row: list = [None] * 85
    row[7] = ["generateContent"]
    row[0], row[3], row[64], row[82] = (
        "models/gemini-fixture",
        "Fixture",
        [1, 25, 48],
        [3],
    )
    models = ModelDiscoveryService()._parse_raw_models([[row]])
    assert [item["id"] for item in models] == ["gemini-fixture", "gemini-fixture-build"]
    assert models[1]["channels"] == ["assistant"]
    assert models[1]["accessModes"] == [3]


@pytest.mark.asyncio
async def test_openai_http_uses_real_assistant_adapter(monkeypatch):
    import httpx

    from aistudio_api.api.app import app
    from aistudio_api.api.dependencies import get_client

    monkeypatch.setattr(
        "aistudio_api.infrastructure.gateway.model_defaults.get_configured_api_keys",
        lambda: {"fixture-call"},
    )
    client = AIStudioClient()
    session = FakeSession()
    client._session = cast(GatewaySession, session)
    app.dependency_overrides[get_client] = lambda: client
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer fixture-call"},
        ) as http:
            response = await http.post(
                "/v1/chat/completions",
                json={
                    "model": "gemini-fixture-build",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "hello"
        assert response.json()["usage"]["total_tokens"] == 15
        assert session.calls[-1][0] == "DeleteApplet"
    finally:
        app.dependency_overrides.pop(get_client, None)
