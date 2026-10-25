"""A failed snapshot queue must retire its page without replaying output."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from fastapi import HTTPException

from aistudio_api.application import api_service_gemini as service
from aistudio_api.infrastructure.browser.cdp_client import CDPClient, CDPPage
from aistudio_api.infrastructure.gateway import session as session_module
from aistudio_api.infrastructure.gateway.session_state import SessionContext
from aistudio_api.infrastructure.gateway.wire_codec import InvalidRequestTemplateError
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart


@pytest.fixture
def snapshot_env(monkeypatch):
    monkeypatch.setattr(session_module, "derive_profile_dir", lambda _auth: None)
    monkeypatch.setattr(session_module, "get_auth_generation", lambda _auth: None)
    monkeypatch.setattr(session_module, "get_auth_user_from_meta", lambda _auth: "0")
    monkeypatch.setattr(session_module, "load_cookies_to_page", AsyncMock(return_value=True))
    monkeypatch.setattr(session_module, "navigate_to_aistudio", AsyncMock())
    monkeypatch.setattr(
        session_module, "bootstrap_botguard_and_template",
        AsyncMock(return_value=("synthetic", {})),
    )
    session = session_module.BrowserSession(port=1)
    session._auth_file = None
    session.persist_context_cookies = AsyncMock()
    old_page = MagicMock(spec=CDPPage)
    old_page.target_id = "synthetic-old"
    old_page.is_closed.return_value = False
    old_page.evaluate = AsyncMock(return_value="synthetic-old-snapshot")
    new_page = MagicMock(spec=CDPPage)
    new_page.target_id = "synthetic-new"
    new_page.is_closed.return_value = False
    new_page.evaluate = AsyncMock(return_value="synthetic-new-snapshot")
    client = MagicMock(spec=CDPClient)
    client.create_new_page = AsyncMock(return_value=new_page)
    session._cdp_client = client
    session._proc = MagicMock()
    session._proc.is_alive.return_value = True
    context = SessionContext(
        page=old_page, snap_key="synthetic", auth_user="0", auth_file=None,
        base_template={}, auth_generation=None,
    )
    session.set_ready_context(context)
    contents = [AistudioContent(role="user", parts=[AistudioPart(text="offline")])]
    return SimpleNamespace(
        session=session, old=old_page, new=new_page, context=context,
        client=client, contents=contents,
    )


async def _leased_snapshot(env):
    async with env.session.request_scope():
        return await env.session.generate_snapshot(env.contents)


async def test_snapshot_failure_stops_same_page_retries_and_invalidates_readiness(snapshot_env):
    env = snapshot_env
    env.old.evaluate.side_effect = TimeoutError()

    with pytest.raises(InvalidRequestTemplateError):
        await _leased_snapshot(env)

    assert env.old.evaluate.await_count == 1
    assert not env.session.is_ready
    assert env.context.in_flight == 0


async def test_concurrent_snapshot_failures_share_one_replacement_page(snapshot_env):
    env = snapshot_env
    started, fail = asyncio.Event(), asyncio.Event()

    async def fail_snapshot(*_args, **_kwargs):
        started.set()
        await fail.wait()
        raise TimeoutError

    env.old.evaluate.side_effect = fail_snapshot
    first = asyncio.create_task(_leased_snapshot(env))
    await started.wait()
    second = asyncio.create_task(_leased_snapshot(env))
    await asyncio.sleep(0)
    assert env.context.in_flight == 2
    fail.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(result, InvalidRequestTemplateError) for result in results)
    assert env.old.evaluate.await_count == 1
    assert env.context.in_flight == 0

    # Both later requests must use the same replacement, never the hanging page.
    snapshots = await asyncio.gather(_leased_snapshot(env), _leased_snapshot(env))
    assert snapshots == ["synthetic-new-snapshot", "synthetic-new-snapshot"]
    assert env.client.create_new_page.await_count == 1
    env.old.close.assert_awaited_once()
    assert env.old.evaluate.await_count == 1
    assert env.new.evaluate.await_count == 2
    assert env.session.active_context is not env.context


async def test_snapshot_failure_keeps_existing_signed_lease_until_it_drains(snapshot_env):
    env = snapshot_env
    held, finish = asyncio.Event(), asyncio.Event()
    env.old.evaluate.side_effect = TimeoutError()
    env.session._transport.send_hooked_request = AsyncMock(return_value=(200, b"synthetic"))

    async def existing_signed_request():
        async with env.session.request_scope():
            held.set()
            await finish.wait()
            return await env.session.send_hooked_request(
                body="[]", timeout_ms=100, url="https://example.invalid", headers={}
            )

    existing = asyncio.create_task(existing_signed_request())
    await held.wait()
    with pytest.raises(InvalidRequestTemplateError):
        await _leased_snapshot(env)
    recovery = asyncio.create_task(env.session.ensure_ready())
    await asyncio.sleep(0)
    env.old.close.assert_not_awaited()
    env.client.create_new_page.assert_not_awaited()
    finish.set()
    assert await existing == (200, b"synthetic")
    recovered = await recovery
    assert recovered.page is env.new
    assert env.session._transport.send_hooked_request.await_args.args[0] is env.old
    env.old.close.assert_awaited_once()


async def test_late_snapshot_failure_does_not_invalidate_replacement_context(snapshot_env, monkeypatch):
    env = snapshot_env
    started, fail = asyncio.Event(), asyncio.Event()

    async def fail_old_snapshot(*_args, **_kwargs):
        started.set()
        await fail.wait()
        raise TimeoutError

    env.old.evaluate.side_effect = fail_old_snapshot
    old_request = asyncio.create_task(_leased_snapshot(env))
    await started.wait()
    replacement = SessionContext(
        page=env.new, snap_key="synthetic", auth_user="0", auth_file=None,
        base_template={}, auth_generation="synthetic-new-generation",
    )
    monkeypatch.setattr(session_module, "get_auth_generation", lambda _auth: "synthetic-new-generation")
    env.session.set_ready_context(replacement)
    env.session._retiring_contexts.append(env.context)
    fail.set()
    with pytest.raises(InvalidRequestTemplateError):
        await old_request
    assert env.session.is_ready
    assert env.session.active_context is replacement
    assert await _leased_snapshot(env) == "synthetic-new-snapshot"
    await env.session._release_context(env.context)
    assert env.session.is_ready
    env.new.close.assert_not_awaited()


async def test_cancelling_snapshot_lock_waiter_does_not_invalidate_context(snapshot_env):
    env = snapshot_env
    await env.session._snapshot_lock.acquire()
    try:
        waiting = asyncio.create_task(_leased_snapshot(env))
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    finally:
        env.session._snapshot_lock.release()
    assert env.session.is_ready
    assert env.context.in_flight == 0
    env.old.evaluate.assert_not_awaited()


async def test_cancelled_snapshot_waits_for_its_existing_read_without_starting_another(snapshot_env):
    env = snapshot_env
    started, finish = asyncio.Event(), asyncio.Event()

    async def finish_snapshot(*_args, **_kwargs):
        started.set()
        await finish.wait()
        return "synthetic-finished-snapshot"

    env.old.evaluate.side_effect = finish_snapshot
    request = asyncio.create_task(_leased_snapshot(env))
    await started.wait()
    request.cancel()
    await asyncio.sleep(0)
    assert not request.done()
    assert env.context.in_flight == 1
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert env.old.evaluate.await_count == 1
    assert env.context.in_flight == 0
    assert env.session.is_ready


async def test_cancelled_hanging_snapshot_retains_original_deadline_and_closes_read(snapshot_env, monkeypatch):
    env = snapshot_env
    monkeypatch.setattr(session_module, "SNAPSHOT_TIMEOUT_SECONDS", 0.03, raising=False)
    started, closed = asyncio.Event(), asyncio.Event()

    async def hang(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    env.old.evaluate.side_effect = hang
    request = asyncio.create_task(_leased_snapshot(env))
    await started.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(request, timeout=0.5)
    assert closed.is_set()
    assert env.old.evaluate.await_count == 1
    assert env.context.in_flight == 0
    assert not env.session.is_ready
    assert not [
        task for task in asyncio.all_tasks()
        if task.get_name() == "aistudio-snapshot-read" and not task.done()
    ]


async def test_anyio_level_cancellation_preserves_successful_snapshot_context(snapshot_env):
    env = snapshot_env
    started = asyncio.Event()
    cancelled = []

    async def finish_snapshot(*_args, **_kwargs):
        started.set()
        await asyncio.sleep(0.01)
        return "synthetic-finished-snapshot"

    async def request():
        try:
            await _leased_snapshot(env)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    env.old.evaluate.side_effect = finish_snapshot
    async with anyio.create_task_group() as group:
        group.start_soon(request)
        await started.wait()
        group.cancel_scope.cancel()
    assert cancelled == [True]
    assert env.context.in_flight == 0
    assert env.old.evaluate.await_count == 1
    assert env.session.is_ready


async def test_invalid_snapshot_does_not_replay_after_output(snapshot_env, monkeypatch):
    client = SimpleNamespace(
        session=snapshot_env.session, clear_templates=MagicMock(),
    )
    reset = AsyncMock()
    monkeypatch.setattr(client.session, "reset_if_active", reset)
    with pytest.raises(HTTPException) as failure:
        await service._handle_attempt_exception(
            InvalidRequestTemplateError("synthetic snapshot unavailable"),
            attempt=0, model_path="gemini-offline", normalized_model="gemini-offline",
            client=client, has_yielded_data=True,
            request_context_id=snapshot_env.context.context_id,
        )
    assert failure.value.status_code == 503
    reset.assert_not_awaited()


def test_real_snapshot_javascript_retains_a_pending_queue_until_page_replacement():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to replay the actual snapshot JavaScript")
    script_path = (
        Path(__file__).resolve().parents[2]
        / "src/aistudio_api/infrastructure/browser/js/snapshot_generate.js"
    )
    replay = """
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
let calls = 0;
function page(hang) {
    return {default_MakerSuite: {snapshot() {
        calls++;
        return hang ? new Promise(() => {}) : Promise.resolve('synthetic');
    }}, __AISTUDIO__: {service: {}, snapKey: 'snapshot'}};
}
function run(window) { return vm.runInNewContext('(' + source + ')', {window}); }
async function observe(promise) {
    return await Promise.race([
        promise.then(() => 'resolved'),
        new Promise(resolve => setTimeout(() => resolve('pending'), 10)),
    ]);
}
(async () => {
    const snapshot = run(page(true));
    const first = await observe(snapshot('first'));
    const next = await observe(snapshot('next'));
    const oldCalls = calls;
    const replacement = await run(page(false))('new-page');
    process.stdout.write(JSON.stringify({first, next, oldCalls, replacement}));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    completed = subprocess.run(
        [node, "-e", replay, str(script_path)],
        check=True, capture_output=True, text=True, timeout=10,
    )
    assert json.loads(completed.stdout) == {
        "first": "pending", "next": "pending", "oldCalls": 1,
        "replacement": "synthetic",
    }
