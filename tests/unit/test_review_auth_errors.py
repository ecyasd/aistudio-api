"""Keep caller authentication failures distinct from service Google failures."""

import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from aistudio_api.api import gemini as service, gemini_errors
from aistudio_api.application import (
    generation as generation_execution,
    generation_recovery as recovery,
)
from aistudio_api.application.generation_errors import GenerationFailure
from aistudio_api.domain.errors import AuthError, SessionExpiredError
from aistudio_api.infrastructure.utils.common import atomic_write_json


@pytest.mark.parametrize("error,category", [
    (SessionExpiredError("synthetic-upstream-secret"), "upstream_auth_expired"),
    (AuthError("synthetic-upstream-secret"), "upstream_auth_failed"),
])
async def test_upstream_auth_is_503_for_http_and_sse(monkeypatch, caplog, error, category):
    monkeypatch.setattr(recovery, "record_rotator_event", MagicMock())
    monkeypatch.setattr(generation_execution, "record_rotator_event", recovery.record_rotator_event)
    with pytest.raises(GenerationFailure) as caught:
        await recovery._handle_attempt_exception(
            error, attempt=0, model_path="models/test", normalized_model="test",
            client=SimpleNamespace(clear_templates=MagicMock()), allow_retry=False,
        )
    assert caught.value.description.status == 503
    assert caught.value.description.reason.value == category
    payload = json.loads(service.format_sse_error(caught.value)[6:])["error"]
    assert payload["code"] == 503 and payload["status"] == "UNAVAILABLE"
    assert payload["type"] == category
    assert "keep your API key" in payload["message"]
    assert "synthetic-upstream-secret" not in payload["message"]
    assert "synthetic-upstream-secret" not in caplog.text


def test_caller_auth_error_stays_401():
    error = HTTPException(401, detail={"message": "Invalid API key"})
    assert gemini_errors.classify_gemini_error_payload(error)[0] == 401
    assert gemini_errors.gemini_error_type(error) is None


def test_atomic_json_is_private_even_with_permissive_umask(tmp_path):
    path = tmp_path / "synthetic-auth.json"
    previous = os.umask(0)
    try:
        atomic_write_json(path, {"synthetic": True})
        atomic_write_json(path, {"synthetic": False})
    finally:
        os.umask(previous)
    assert json.loads(path.read_text()) == {"synthetic": False}
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("*.tmp.*"))
