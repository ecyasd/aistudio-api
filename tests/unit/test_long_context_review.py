"""Offline regressions for large-input overhead and account mutation races."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from aistudio_api.api import routes_accounts, routes_system
from aistudio_api.application import (
    account_orchestrator,
    generation as generation_execution,
    generation_recovery as recovery,
)
from aistudio_api.infrastructure.account.account_store import AccountMeta
from aistudio_api.infrastructure.browser.cdp_client import CDPConnection, CDPPage
from aistudio_api.infrastructure.gateway.session import BrowserSession
from aistudio_api.infrastructure.gateway.session_auth import (
    get_auth_generation,
    get_auth_user_from_meta,
)
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["activate", "delete", "delete_group", "next"])
async def test_management_account_changes_wait_for_inference_switch_lock(operation, monkeypatch):
    account = AccountMeta(id="synthetic", name="Synthetic", email=None, created_at="2026-01-01", cookie_id="group")
    replacement = AccountMeta(id="replacement", name="Replacement", email=None, created_at="2026-01-01")
    service = MagicMock()
    service.get_active_account.return_value = account
    service.list_accounts.return_value = [account, replacement] if operation == "delete_group" else [replacement]
    service.activate_account = AsyncMock(return_value=replacement)
    service.delete_account.return_value = True
    session = SimpleNamespace(switch_auth=AsyncMock(), clear_account=AsyncMock())
    rotator = MagicMock()
    rotator.get_next_account = AsyncMock(return_value=replacement)
    state = SimpleNamespace(client=SimpleNamespace(_session=session), rotator=rotator, account_service=service)
    monkeypatch.setattr(account_orchestrator, "_last_switch_time", 0.0)

    async def mutate():
        if operation == "activate":
            return await routes_accounts.activate_account(account.id, service, state)
        if operation == "delete":
            return await routes_accounts.delete_account(account.id, service, state)
        if operation == "delete_group":
            return await routes_accounts.delete_cookie_group("group", service, state)
        return await routes_system.force_next_account(state)

    async with account_orchestrator._get_switch_lock():
        task = asyncio.create_task(mutate())
        try:
            await asyncio.sleep(0)
            assert not task.done()
            service.activate_account.assert_not_called()
            service.delete_account.assert_not_called()
            rotator.get_next_account.assert_not_called()
        finally:
            if task.done():
                await task
    await task
    assert account_orchestrator._last_switch_time > 0


@pytest.mark.asyncio
async def test_native_binding_registration_is_shared_by_concurrent_requests():
    connection = MagicMock(spec=CDPConnection)
    connection.send = AsyncMock(return_value={})
    page = CDPPage(connection, target_id="synthetic")
    await asyncio.gather(*(page.add_binding("synthetic_binding") for _ in range(3)))
    await page.add_binding("synthetic_binding")
    connection.send.assert_awaited_once_with("Runtime.addBinding", {"name": "synthetic_binding"})


@pytest.mark.asyncio
async def test_failed_binding_registration_can_be_retried():
    connection = MagicMock(spec=CDPConnection)
    connection.send = AsyncMock(side_effect=[RuntimeError("synthetic failure"), {}])
    page = CDPPage(connection, target_id="synthetic")
    with pytest.raises(RuntimeError, match="synthetic failure"):
        await page.add_binding("synthetic_binding")
    await page.add_binding("synthetic_binding")
    assert connection.send.await_count == 2


@pytest.mark.asyncio
async def test_nonstream_retries_reuse_the_normalized_long_request(monkeypatch):
    from aistudio_api.api import gemini as service
    from aistudio_api.domain.errors import RequestError
    from aistudio_api.domain.models import Candidate, ContentPart, ModelOutput

    normalized = SimpleNamespace(model="models/synthetic")
    normalize = MagicMock(return_value=normalized)
    output = ModelOutput(candidates=[Candidate(parts=[ContentPart(text="synthetic")], finish_reason=1)],
                         model="models/synthetic", response_id="synthetic", usage={}, raw_response="")
    generate = AsyncMock(side_effect=[RequestError(429, "synthetic retry"), output])

    @asynccontextmanager
    async def scope():
        yield SimpleNamespace(auth_file=None, context_id=1)

    monkeypatch.setattr(service, "normalize_gemini_request", normalize)
    monkeypatch.setattr(generation_execution, "ensure_active_account", AsyncMock())
    monkeypatch.setattr(recovery, "_recover_failure", AsyncMock(return_value=True))
    monkeypatch.setattr(generation_execution, "_persist_successful_cookies", AsyncMock())
    monkeypatch.setattr(generation_execution, "account_id_for_auth_file", lambda _path: None)
    monkeypatch.setattr(recovery, "record_rotator_event", MagicMock())
    monkeypatch.setattr(generation_execution, "record_rotator_event", recovery.record_rotator_event)
    monkeypatch.setattr(recovery.runtime_state, "record", MagicMock())
    monkeypatch.setattr(generation_execution.continuation, "generate_content", generate)
    request = SimpleNamespace(contents=["synthetic long input"], autoContinue=False)
    await service._generate_non_streaming("models/synthetic", request,
                                        SimpleNamespace(session=SimpleNamespace(request_scope=scope)))
    normalize.assert_called_once_with(request, "models/synthetic")
    assert generate.await_count == 2
    assert all(call.args[1] is normalized for call in generate.await_args_list)


@pytest.mark.asyncio
async def test_incremental_snapshot_hash_preserves_separators_and_all_part_kinds(monkeypatch):
    from aistudio_api.infrastructure.browser.scripts import SNAPSHOT_GENERATE_JS

    page = SimpleNamespace(evaluate=AsyncMock(return_value="synthetic-snapshot"))
    session = BrowserSession(port=1)
    monkeypatch.setattr(session, "ensure_ready", AsyncMock(return_value=SimpleNamespace(page=page)))
    contents = [AistudioContent("user", [
        AistudioPart(text="合成文本"), AistudioPart(), AistudioPart(inline_data=("image/png", "base64")),
        AistudioPart(executable_code=("PYTHON", "code")), AistudioPart(code_execution_result=(1, "result")),
    ]), AistudioContent("model", [AistudioPart(text="")])]
    assert await session.generate_snapshot(contents) == "synthetic-snapshot"
    digest = sha256(" ".join(["合成文本", "", "base64", "code", "result", ""]).encode()).hexdigest()
    page.evaluate.assert_awaited_once_with(SNAPSHOT_GENERATE_JS, args=digest, timeout_s=10.0)


def test_metadata_cache_observes_import_replacement_and_deletion(tmp_path, monkeypatch):
    meta = tmp_path / "meta.json"
    meta.write_text('{"auth_user":"0","auth_generation":"old"}')
    auth = str(tmp_path / "auth.json")
    reads = []
    original_read = Path.read_text

    def read(path, *args, **kwargs):
        reads.append(path)
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    assert get_auth_generation(auth) == "old"
    assert get_auth_user_from_meta(auth) == "0"
    assert len(reads) == 1
    replacement = tmp_path / "replacement.json"
    replacement.write_text('{"auth_user":"2","auth_generation":"new"}')
    replacement.replace(meta)
    assert get_auth_generation(auth) == "new"
    assert get_auth_user_from_meta(auth) == "2"
    assert len(reads) == 2
    meta.unlink()
    assert get_auth_generation(auth) is None
    assert get_auth_user_from_meta(auth) == "0"


@pytest.mark.asyncio
async def test_cdp_arguments_keep_unicode_without_expanding_text_to_escape_sequences():
    connection = MagicMock(spec=CDPConnection)
    connection.send = AsyncMock(return_value={"result": {"value": "ok"}})
    page = CDPPage(connection, target_id="synthetic")
    await page.evaluate("args => args.body", {"body": '合成内容\n"quoted"'})
    expression = connection.send.call_args.args[1]["expression"]
    assert "合成内容" in expression
    assert "\\u5408" not in expression
    assert '\\n\\"quoted\\"' in expression
