"""Offline native signing, account lifetime, framing and Build regressions."""
import asyncio
import base64
import hashlib
import json
from contextlib import aclosing
from pathlib import Path

import httpx
import pytest

from aistudio_api.domain.errors import (
    BrowserTransportError,
    IncompleteResponseError,
    RequestError,
)
from aistudio_api.infrastructure.gateway.build_assistant import (
    AssistantDecoder,
    SparseJSON,
    stream_assistant,
)
from aistudio_api.infrastructure.gateway.native_bridge import (
    NativeBridge,
    local_signer_url,
)
from aistudio_api.infrastructure.gateway.native_session import NativeSession
from aistudio_api.infrastructure.gateway.wire_parser import parse_text_output


def account(root: Path, name: str) -> Path:
    directory = root / name
    directory.mkdir()
    path = directory / "auth.json"
    path.write_text(json.dumps({"cookies": [{"name": "SAPISID", "value": "synthetic-" + name, "domain": ".google.com", "path": "/"}], "origins": []}))
    (directory / "meta.json").write_text(json.dumps({"auth_user": "0", "auth_generation": name}))
    return path


class FakeBridge:
    def __init__(self):
        self.sessions = {}
        self.proofs = []
        self.closed = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.finished = False

    async def post(self, path, payload, timeout=15):
        session = payload["session"]
        if path.endswith("open"):
            self.sessions[session] = payload["cookies"]
        if path.endswith("state"):
            return {"ok": True, "state": self.sessions[session]}
        if path.endswith("mint"):
            self.proofs.append(payload)
            self.started.set()
            await self.release.wait()
            self.finished = True
            return {"ok": True, "proof": "synthetic-proof"}
        return {"ok": True}

    async def close_session(self, session):
        self.closed.append(session)

    async def close(self):
        return None


async def native_session():
    session = NativeSession(9222, "http://127.0.0.1:8081")
    await session.bridge.close()
    session.bridge = FakeBridge()
    return session


@pytest.mark.parametrize("url", ["https://waa.spinsnow.fun", "http://localhost:8081", "http://127.0.0.1:8081/redirect", "http://user:secret@127.0.0.1:8081", "http://127.0.0.1:8081?url=x"])
def test_signer_rejects_nonlocal_or_indirect_destination(url):
    with pytest.raises(ValueError):
        local_signer_url(url)


async def test_native_request_keeps_its_account_during_switch(tmp_path):
    session = await native_session()
    first, second = account(tmp_path, "first"), account(tmp_path, "second")
    session._auth_file = str(first)
    async with session.request_scope() as old:
        await session.transition_to_account(str(second))
        assert session.active_context is not old
        await session.finalize_request('["models/gemini-synthetic",[[[[null,"original"]],"user"]]]', "gemini-synthetic")
        assert session.bridge.proofs[-1]["session"] == old.native.session
        assert old.native.session not in session.bridge.closed
    await asyncio.gather(*session._background_tasks)
    assert old.native.session in session.bridge.closed
    await session.close()


async def test_cancelled_signature_is_joined_before_lease_release(tmp_path):
    session = await native_session()
    session._auth_file = str(account(tmp_path, "first"))
    session.bridge.release.clear()

    async def operation():
        async with session.request_scope():
            await session.finalize_request('["models/gemini-synthetic",[[[[null,"synthetic"]],"user"]]]', "gemini-synthetic")

    task = asyncio.create_task(operation())
    await session.bridge.started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and session.active_context.in_flight == 1
    session.bridge.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.bridge.finished and session.active_context.in_flight == 0
    assert session.is_ready
    await session.close()


async def test_checkpoint_cannot_overwrite_reimported_generation(tmp_path):
    session = await native_session()
    path = account(tmp_path, "first")
    context = await session.transition_to_account(str(path))
    meta = path.parent / "meta.json"
    meta.write_text('{"auth_generation":"replacement","auth_user":"0"}')
    replacement = '{"cookies":[{"name":"SAPISID","value":"synthetic-replacement"}],"owner_field":true}'
    path.write_text(replacement)
    await session.persist_context_cookies(context)
    assert path.read_text() == replacement
    await session.close()


async def test_proof_is_bound_to_final_transcribed_build_history(tmp_path):
    session = await native_session()
    session._auth_file = str(account(tmp_path, "first"))
    body = '["models/gemini-synthetic-build",[[[[null,"earlier"]],"model"],[[[null,"next"]],"user"]]]'
    async with session.request_scope():
        result = json.loads(await session.finalize_request(body, "gemini-synthetic-build"))
        expected = "<assistant_turn>\nearlier\n</assistant_turn> next"
        assert session.bridge.proofs[-1]["digest"] == hashlib.sha256(expected.encode()).hexdigest()
        assert result[4] == "synthetic-proof"
        assert all(content[1] == "user" for content in result[1])
    await session.close()


@pytest.mark.parametrize("ending", [b"", b'{"error":"synthetic failure"}\n', b'{"done":true}\n'])
async def test_native_stream_requires_explicit_clean_eof(ending):
    bridge = NativeBridge("http://127.0.0.1:8081")
    await bridge.client.aclose()
    data = b'{"status":200}\n' + json.dumps({"chunk": base64.b64encode(b"synthetic").decode()}).encode() + b"\n" + ending
    bridge.client = httpx.AsyncClient(base_url="http://127.0.0.1:8081", transport=httpx.MockTransport(lambda request: httpx.Response(200, content=data)))
    if ending == b'{"done":true}\n':
        assert await bridge.rpc("a" * 32, "GenerateContent", []) == (200, b"synthetic")
    else:
        with pytest.raises(BrowserTransportError):
            await bridge.rpc("a" * 32, "GenerateContent", [])
    await bridge.close()


async def test_native_stream_delivers_small_events_without_waiting_for_buffer_fill():
    class Source(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b'{"status":200}\n'
            await asyncio.Event().wait()

        async def aclose(self):
            self.closed = True
    source = Source()
    bridge = NativeBridge("http://127.0.0.1:8081")
    await bridge.client.aclose()
    bridge.client = httpx.AsyncClient(base_url="http://127.0.0.1:8081", transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=source)))
    async with aclosing(bridge.stream("a" * 32, "GenerateContent", [], 2)) as stream:
        assert await asyncio.wait_for(anext(stream), 0.5) == ("status", 200)
    assert source.closed
    await bridge.close()


def test_build_catalog_uses_only_explicit_upstream_capability(monkeypatch):
    from aistudio_api.api.routes_models import _build_variant, _continuation_variant
    from aistudio_api.infrastructure.gateway.model_discovery import (
        ModelDiscoveryService,
    )
    raw = [None] * 65
    raw[0], raw[3], raw[7], raw[64] = "models/gemini-synthetic", "Synthetic", ["generateContent"], [48]
    models = ModelDiscoveryService()._parse_raw_models([ [raw] ])
    monkeypatch.setenv("AISTUDIO_WAA_BACKEND", "browserless")
    build = _build_variant(models[0])
    assert build["id"] == "gemini-synthetic-build"
    assert _continuation_variant(build) is None
    raw[64] = []
    assert _build_variant(ModelDiscoveryService()._parse_raw_models([[raw]])[0]) is None


def message(text, *, completed=False, thought=False):
    part = [None] * 15
    part[1], part[12] = text, thought
    step = [None] * 16
    step[0] = [[[part], "model"]]
    step[11], step[12], step[15] = 1, 1 if completed else 2, [10, 5, 2]
    return [None, None, [step]]


def test_build_decoder_preserves_deltas_and_rejects_changed_prefix():
    decoder = AssistantDecoder()
    first = decoder.feed(message("Hello"))
    second = decoder.feed(message("Hello world", completed=True))
    raw = json.dumps([[*first, *second, decoder.finish_chunk()]])
    output = parse_text_output(raw)
    assert output.text == "Hello world"
    assert output.usage["prompt_tokens"] == 12
    assert output.usage["completion_tokens"] == 5
    with pytest.raises(RequestError, match="snapshot changed"):
        decoder.feed(message("replacement"))
    with pytest.raises(IncompleteResponseError):
        AssistantDecoder().finish_chunk()


def test_sparse_jspb_normalization_does_not_change_quoted_text():
    sparse = SparseJSON()
    source = "[,[" + "," + json.dumps('[,"x",]') + "],]"
    result = "".join(sparse.feed(char) for char in source)
    assert json.loads(result) == [None, [None, '[,"x",]'], None]


class BuildBridge:
    def __init__(self, incomplete=False):
        self.calls = []
        self.streams = 0
        self.incomplete = incomplete

    async def rpc(self, session, method, body, timeout_s=60):
        self.calls.append((session, method, body))
        return 200, json.dumps(["synthetic-applet" if method == "CreateApplet" else "synthetic-task"]).encode()

    async def stream(self, session, method, body, timeout_s):
        self.streams += 1
        yield "status", 200
        yield "chunk", json.dumps([[message("Build reply", completed=not self.incomplete)]], ensure_ascii=False).encode()


@pytest.mark.parametrize("incomplete", [False, True])
async def test_build_owns_creation_reopen_cancel_and_cleanup(incomplete):
    bridge = BuildBridge(incomplete)
    body = ["models/gemini-synthetic-build", [[[[None, "synthetic"]], "user"]], None, None, "synthetic-proof"]
    raw = bytearray()
    async def consume():
        async with aclosing(stream_assistant(bridge, "a" * 32, body, 5)) as events:
            async for kind, value in events:
                if kind == "chunk":
                    raw.extend(value)
    if incomplete:
        with pytest.raises(IncompleteResponseError):
            await consume()
        assert bridge.streams == 2
    else:
        await consume()
        assert parse_text_output(raw.decode()).text == "Build reply"
    assert [call[1] for call in bridge.calls] == ["CreateApplet", "CodeAssistantOffline", "CancelCodeAssistantOfflineGeneration", "DeleteApplet"]
    assert bridge.calls[-1][2] == ["synthetic-applet"]
    request = bridge.calls[1][2]
    assert request[1] == "synthetic-proof" and request[7] == "models/gemini-synthetic"
