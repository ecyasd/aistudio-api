import base64
from unittest.mock import AsyncMock

import pytest

from aistudio_api.infrastructure.browser.runtime import request_scope, runtime
from aistudio_api.infrastructure.gateway.session import SessionContext


@pytest.mark.asyncio
async def test_browser_scope_closes_request_owned_process_on_cancellation(monkeypatch):
    browser = AsyncMock()
    context = AsyncMock()
    page = AsyncMock()
    page.url = "https://aistudio.google.com/prompts/new"
    browser.new_context.return_value = context
    context.new_page.return_value = page
    monkeypatch.setattr(runtime, "start", AsyncMock(return_value=browser))
    with pytest.raises(RuntimeError):
        async with request_scope():
            await runtime.page(
                SessionContext(cookies={"SID": "fixture"}), model="gemini-test"
            )
            raise RuntimeError("synthetic cancellation")
    browser.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_browser_stream_backpressure_and_owned_fetch_cleanup(monkeypatch):
    binding = None

    async def expose(_name, callback):
        nonlocal binding
        binding = callback

    async def evaluate(script, argument=None):
        if script.startswith("async"):
            assert binding is not None
            await binding(None, "status", 200)
            await binding(None, "base64", base64.b64encode(b"first").decode())
            await binding(None, "base64", base64.b64encode(b"second").decode())

    page = AsyncMock()
    page.expose_binding.side_effect = expose
    page.evaluate.side_effect = evaluate
    monkeypatch.setattr(runtime, "page", AsyncMock(return_value=page))
    events = runtime.stream(
        ctx=SessionContext(), url="https://fixture.invalid/rpc", headers={}, body="[]"
    )
    assert await anext(events) == ("status", 200)
    assert await anext(events) == ("chunk", b"first")
    await events.aclose()
    assert any("abort" in call.args[0] for call in page.evaluate.await_args_list)
