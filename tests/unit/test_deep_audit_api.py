"""Offline regression checks for template recovery and lifecycle visibility."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from aistudio_api.api.routes_system import browser_status
from aistudio_api.application.generation_errors import GenerationFailure
from aistudio_api.application.generation_recovery import (
    _recover_failure as handle_attempt_exception,
)
from aistudio_api.infrastructure.gateway.wire_codec import InvalidRequestTemplateError


@pytest.mark.asyncio
async def test_invalid_internal_template_recovers_once_without_account_rotation(monkeypatch):
    rotate = AsyncMock()
    monkeypatch.setattr("aistudio_api.application.generation_recovery.try_switch_account", rotate)
    session = SimpleNamespace(reset_if_active=AsyncMock(return_value=True))
    client = SimpleNamespace(clear_templates=MagicMock(), session=session)
    kwargs = {"model_path": "synthetic", "normalized_model": "synthetic", "client": client,
              "account_id": "synthetic-account", "request_context_id": 73}
    assert await handle_attempt_exception(InvalidRequestTemplateError("synthetic"), attempt=0, **kwargs)
    session.reset_if_active.assert_awaited_once_with(73)
    with pytest.raises(GenerationFailure) as error:
        await handle_attempt_exception(InvalidRequestTemplateError("synthetic"), attempt=1, **kwargs)
    assert error.value.description.status == 503
    rotate.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_template_never_retries_after_stream_output():
    client = SimpleNamespace(clear_templates=MagicMock(), session=MagicMock())
    with pytest.raises(GenerationFailure) as error:
        await handle_attempt_exception(InvalidRequestTemplateError("synthetic"), attempt=0,
            model_path="synthetic", normalized_model="synthetic", client=client,
            has_yielded_data=True)
    assert error.value.description.status == 503
    client.session.reset_if_active.assert_not_called()


@pytest.mark.asyncio
async def test_browser_status_only_reads_lifecycle_snapshot():
    snapshot = {"status": "idle", "process_running": False, "in_flight": 0,
                "idle_timeout_seconds": 300}
    session = SimpleNamespace(get_status=MagicMock(return_value=snapshot),
                              ensure_ready=AsyncMock())
    state = SimpleNamespace(client=SimpleNamespace(session=session))
    assert await browser_status(state) == snapshot
    session.ensure_ready.assert_not_awaited()
    assert (await browser_status(SimpleNamespace(client=None)))["status"] == "not_started"
