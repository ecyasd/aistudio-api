"""A completed response must not wait on a stalled optional Cookie checkpoint."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from aistudio_api.api import gemini as service
from aistudio_api.api.schemas import GeminiGenerateContentRequest
from aistudio_api.application import (
    generation as generation_execution,
    generation_recovery as recovery,
)
from aistudio_api.domain.models import Candidate, ContentPart, ModelOutput


@pytest.fixture
def checkpoint_budget(monkeypatch):
    monkeypatch.setattr(generation_execution, "COOKIE_CHECKPOINT_TIMEOUT_SECONDS", 0.01, raising=False)


async def test_checkpoint_success_keeps_the_exact_leased_context(
    checkpoint_budget, tmp_path
):
    context = object()
    saved = tmp_path / "synthetic-checkpoint.json"
    saved.write_text('{"checkpoint":"previous"}', encoding="utf-8")

    async def save(received):
        assert received is context
        saved.write_text('{"checkpoint":"new verified"}', encoding="utf-8")

    persist = AsyncMock(side_effect=save)
    client = SimpleNamespace(session=SimpleNamespace(persist_context_cookies=persist))

    await generation_execution._persist_successful_cookies(client, context)

    persist.assert_awaited_once_with(context)
    assert json.loads(saved.read_text(encoding="utf-8")) == {
        "checkpoint": "new verified"
    }


async def test_checkpoint_timeout_preserves_last_saved_data_and_closes_in_place(
    checkpoint_budget, tmp_path
):
    previous = tmp_path / "synthetic-checkpoint.json"
    previous.write_text('{"checkpoint":"last verified"}', encoding="utf-8")
    entered = asyncio.Event()
    closed = asyncio.Event()
    owner = asyncio.current_task()
    context = object()

    async def stalled_persist(received):
        assert received is context
        assert asyncio.current_task() is owner
        entered.set()
        try:
            await asyncio.Event().wait()
            previous.write_text('{"checkpoint":"new verified"}', encoding="utf-8")
        finally:
            closed.set()

    client = SimpleNamespace(
        session=SimpleNamespace(persist_context_cookies=stalled_persist)
    )
    async with asyncio.timeout(1):
        await generation_execution._persist_successful_cookies(client, context)

    assert entered.is_set()
    assert closed.is_set()
    assert json.loads(previous.read_text(encoding="utf-8")) == {
        "checkpoint": "last verified"
    }


async def test_checkpoint_failure_remains_optional(checkpoint_budget):
    persist = AsyncMock(side_effect=RuntimeError("synthetic checkpoint unavailable"))
    client = SimpleNamespace(session=SimpleNamespace(persist_context_cookies=persist))

    await generation_execution._persist_successful_cookies(client, object())

    persist.assert_awaited_once()


async def test_checkpoint_cancellation_propagates_and_closes_in_place(monkeypatch):
    monkeypatch.setattr(generation_execution, "COOKIE_CHECKPOINT_TIMEOUT_SECONDS", 60, raising=False)
    entered = asyncio.Event()
    closed = asyncio.Event()

    async def stalled_persist(_context):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    client = SimpleNamespace(
        session=SimpleNamespace(persist_context_cookies=stalled_persist)
    )
    task = asyncio.create_task(generation_execution._persist_successful_cookies(client, object()))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


@pytest.mark.parametrize("stream", [False, True])
async def test_completed_response_releases_lease_after_checkpoint_budget(
    checkpoint_budget, monkeypatch, stream
):
    context = SimpleNamespace(auth_file="synthetic-account", context_id=1)
    leases = []
    checkpoint_closed = asyncio.Event()

    @asynccontextmanager
    async def request_scope():
        leases.append(context)
        try:
            yield context
        finally:
            leases.remove(context)

    async def persist(received):
        assert received is context
        assert leases == [context]
        try:
            await asyncio.Event().wait()
        finally:
            checkpoint_closed.set()

    async def upstream(**_kwargs):
        yield "parts", [ContentPart(text="completed reply")]
        yield "finish_reason", 1
        yield "usage", {"total_tokens": 2}
        yield "done", None

    client = SimpleNamespace(
        session=SimpleNamespace(
            request_scope=request_scope, persist_context_cookies=persist
        ),
        stream_generate_content=upstream,
        generate_content=AsyncMock(
            return_value=ModelOutput(
                candidates=[
                    Candidate(
                        text="completed reply", finish_reason=1,
                        parts=[ContentPart(text="completed reply")],
                    )
                ],
                usage={"total_tokens": 2},
            )
        ),
    )
    monkeypatch.setattr(generation_execution, "ensure_active_account", AsyncMock())
    monkeypatch.setattr(service, "validate_account_availability", lambda _model: None)
    monkeypatch.setattr(generation_execution, "account_id_for_auth_file", lambda _path: "account-1")
    monkeypatch.setattr(recovery, "record_rotator_event", MagicMock())
    monkeypatch.setattr(generation_execution, "record_rotator_event", recovery.record_rotator_event)
    monkeypatch.setattr(recovery.runtime_state, "record", MagicMock())
    req = GeminiGenerateContentRequest.model_validate(
        {"contents": [{"role": "user", "parts": [{"text": "synthetic question"}]}]}
    )

    async def complete():
        response = await service.handle_gemini_generate_content(
            "gemini-test", req, client, stream=stream
        )
        if stream:
            chunks = [chunk async for chunk in response.body_iterator]
            final = json.loads(chunks[-1][6:])
            assert final["candidates"][0]["finishReason"] == "STOP"
            assert final["usageMetadata"]["totalTokenCount"] == 2
        else:
            assert response.candidates[0].finishReason == "STOP"
        return response

    await asyncio.wait_for(complete(), 1)

    assert checkpoint_closed.is_set()
    assert leases == []
    recovery.record_rotator_event.assert_called_once_with(
        "success", model="models/gemini-test", account_id="account-1"
    )
    assert not [
        task for task in asyncio.all_tasks()
        if not task.done() and task.get_name().startswith("gemini-sse-")
    ]
