"""Pure model-selector normalization and shared quota-key contracts."""

from __future__ import annotations

import pytest

from aistudio_api.api.schemas import GeminiGenerateContentRequest
from aistudio_api.application.account_rotator import AccountStats, normalize_model_key
from aistudio_api.application.chat_service import normalize_gemini_request
from aistudio_api.application.model_selection import parse_model_selection


@pytest.mark.parametrize(
    ("raw", "base", "enabled"),
    [
        ("gemini-offline", "models/gemini-offline", False),
        ("models/gemini-offline", "models/gemini-offline", False),
        ("gemma-offline", "models/gemma-offline", False),
        ("抗截断/gemini-offline", "models/gemini-offline", True),
        ("models/抗截断/gemini-offline", "models/gemini-offline", True),
        ("抗截断/gemma-offline", "models/gemma-offline", True),
    ],
)
def test_selector_has_one_canonical_base_and_explicit_mode(raw, base, enabled):
    selected = parse_model_selection(raw)
    assert selected.base_model == base
    assert selected.auto_continue is enabled


@pytest.mark.parametrize(
    "raw", ["抗截断/", "models/抗截断/", "抗截断/抗截断/gemini-offline"],
)
def test_empty_or_nested_antitruncation_selector_is_rejected(raw):
    with pytest.raises(ValueError):
        parse_model_selection(raw)


@pytest.mark.parametrize(
    "raw", ["抗截断/gemini-offline", "models/抗截断/gemini-offline"],
)
def test_request_normalization_always_removes_virtual_selector(raw):
    request = GeminiGenerateContentRequest.model_validate(
        {"contents": [{"parts": [{"text": "offline prompt"}]}], "tools": []}
    )
    normalized = normalize_gemini_request(request, raw)
    assert normalized.model == "models/gemini-offline"
    assert request.autoContinue is False
    assert request.tools == []


@pytest.mark.parametrize(
    "raw",
    ["gemini-offline", "models/gemini-offline", "抗截断/gemini-offline",
     "models/抗截断/gemini-offline"],
)
def test_all_selector_spellings_share_the_base_quota_key(raw):
    assert normalize_model_key(raw) == "gemini-offline"


@pytest.mark.parametrize(
    ("limited", "queried"),
    [
        ("models/gemini-offline", "抗截断/gemini-offline"),
        ("抗截断/gemini-offline", "models/gemini-offline"),
        ("models/抗截断/gemini-offline", "gemini-offline"),
    ],
)
def test_base_and_alias_share_request_counts_and_cooldown(limited, queried):
    stats = AccountStats(account_id="offline-account")
    stats.record_success("models/gemini-offline")
    stats.record_success("抗截断/gemini-offline")
    assert stats.model_requests == {"gemini-offline": 2}
    stats.record_rate_limited(limited)
    assert stats.model_requests == {"gemini-offline": 3}
    assert set(stats.model_cooldowns) == {"gemini-offline"}
    assert stats.is_available(queried) is False
    assert stats.get_cooldown_remaining(queried) > 0
    assert stats.is_available("gemini-other-offline") is True
