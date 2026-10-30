"""Thought diagnostics stay useful at WARNING without retaining any payload."""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from aistudio_api.api.error_middleware import ErrorRecordsMiddleware
from aistudio_api.api.schemas import GeminiGenerateContentRequest
from aistudio_api.application.chat_service import normalize_gemini_request
from aistudio_api.domain.errors import RequestError
from aistudio_api.domain.models import Candidate, ContentPart
from aistudio_api.infrastructure.gateway.wire_parser import parse_usage_metadata
from aistudio_api.infrastructure.utils import (
    error_records as errors,
    response_diagnostics as diagnostics,
)


@pytest.fixture
def diagnostic_store(monkeypatch):
    store = diagnostics.ResponseDiagnosticStore()
    monkeypatch.setattr(diagnostics, "get_response_diagnostic_store", lambda: store)
    monkeypatch.setattr(diagnostics, "logger", MagicMock())
    monkeypatch.setattr(errors, "_store", errors.ErrorRecordStore())
    return store


@contextmanager
def trace():
    token = errors.bind_request_context()
    try:
        diagnostics.initialize_response_diagnostics(stream=True)
        diagnostics.configure_generation(model="gemini-synthetic", auto_continue=False)
        yield errors.get_request_context()
    finally:
        errors.reset_request_context(token)


@pytest.mark.parametrize("outcome", ["failed", "cancelled"])
def test_failure_summary_is_warning_without_content_flags(diagnostic_store, outcome):
    with trace():
        diagnostics.begin_upstream_call()
        diagnostics.finish_response_diagnostics(outcome, 200)
    diagnostics.logger.warning.assert_called_once()
    diagnostics.logger.info.assert_not_called()
    item = diagnostic_store.list_records()["items"][0]
    assert item["outcome"] == outcome
    assert item["flags"] == []


def test_timeout_summary_survives_warning_log_level(diagnostic_store, monkeypatch, caplog):
    warning_logger = logging.getLogger("synthetic_thought_diagnostics")
    monkeypatch.setattr(diagnostics, "logger", warning_logger)
    with caplog.at_level(logging.WARNING, logger=warning_logger.name), trace():
        diagnostics.begin_upstream_call()
        errors.record_failure(RequestError(504, "SYNTHETIC_PRIVATE_TIMEOUT"),
                              source="gemini", operation="generate_attempt")
        diagnostics.finish_response_diagnostics("failed", 200)
    summaries = [record for record in caplog.records
                 if record.getMessage().startswith("response_diagnostic ")]
    assert len(summaries) == 1 and summaries[0].levelno == logging.WARNING
    assert "SYNTHETIC_PRIVATE_TIMEOUT" not in summaries[0].getMessage()
    assert diagnostic_store.list_records()["items"][0]["error_kind"] == "timeout"


@pytest.mark.parametrize("outcome", ["success", "recovered"])
def test_ordinary_completion_respects_info_log_level(diagnostic_store, outcome):
    with trace():
        diagnostics.begin_upstream_call()
        diagnostics.observe_candidate(Candidate(parts=[ContentPart(text="synthetic")],
                                                finish_reason=1))
        diagnostics.finish_response_diagnostics(outcome, 200)
    diagnostics.logger.info.assert_called_once()
    diagnostics.logger.warning.assert_not_called()


def test_thought_part_count_includes_empty_and_nontext_parts(diagnostic_store):
    with trace():
        diagnostics.begin_upstream_call()
        diagnostics.observe_candidate(Candidate(parts=[
            ContentPart(text="SYNTHETIC_PRIVATE_THOUGHT", thought=True),
            ContentPart(text="", thought=True, thought_signature="SYNTHETIC_SIGNATURE"),
            ContentPart(thought=True, executable_code={"code": "SYNTHETIC_PRIVATE_CODE"}),
            ContentPart(text="synthetic body"),
        ], finish_reason=1))
        diagnostics.finish_response_diagnostics("success", 200)
    item = diagnostic_store.list_records()["items"][0]
    assert item["thought_parts"] == 3
    assert item["empty_thought_parts"] == 1
    encoded = json.dumps(item)
    assert "SYNTHETIC_PRIVATE" not in encoded and "SYNTHETIC_SIGNATURE" not in encoded


@pytest.mark.parametrize("thinking,expected", [
    (None, (False, None, None)),
    ({"thinkingLevel": "HIGH"}, (True, 1, 3)),
    ([], (True, None, None)),
    ([1, None, None, 2], (True, 1, 2)),
    ([0, "SYNTHETIC_PRIVATE_REQUEST", None, 9000], (True, None, None)),
    ([True, None, None, True], (True, None, None)),
])
def test_normalized_request_records_only_explicit_flag_and_verified_enums(
    diagnostic_store, thinking, expected,
):
    request = GeminiGenerateContentRequest.model_validate({
        "contents": [{"parts": [{"text": "SYNTHETIC_PRIVATE_PROMPT"}]}],
        "tools": [],
        "generationConfig": {} if thinking is None else {"thinkingConfig": thinking},
    })
    with trace() as context:
        normalized = normalize_gemini_request(request, "gemini-synthetic")
        if isinstance(thinking, list):
            assert normalized.generation_config_overrides["thinking_config"] == thinking
        assert "SYNTHETIC_PRIVATE" not in str(vars(context.response_diagnostics))
        diagnostics.finish_response_diagnostics("success", 200)
    item = diagnostic_store.list_records()["items"][0]
    assert (item["thinking_explicit"], item["thinking_mode"], item["thinking_level"]) == expected
    assert "SYNTHETIC_PRIVATE" not in json.dumps(item)


def _usage(tokens):
    return [1, 2, 3, None, None, None, None, None, None, tokens]


def test_prompt_size_uses_final_report_of_first_reporting_call_without_summing_retries(diagnostic_store):
    with trace():
        diagnostics.begin_upstream_call()
        parse_usage_metadata([0, 0, 0])
        parse_usage_metadata([80_000, 5, 80_005])
        parse_usage_metadata([80_001, 8, 80_009])
        diagnostics.begin_upstream_call()
        parse_usage_metadata([90_000, 2, 90_002])
        diagnostics.finish_response_diagnostics("recovered", 200)
    assert diagnostic_store.list_records()["items"][0]["prompt_tokens"] == 80_001


@pytest.mark.parametrize("tokens,expected", [(None, None), (True, None), (-1, None), (0, 0), (80_000, 80_000)])
def test_prompt_usage_keeps_unknown_distinct_from_zero(diagnostic_store, tokens, expected):
    with trace():
        diagnostics.begin_upstream_call()
        parse_usage_metadata([tokens, 2, None])
        diagnostics.finish_response_diagnostics("success", 200)
    assert diagnostic_store.list_records()["items"][0]["prompt_tokens"] == expected


async def test_request_size_counts_consumed_bytes_before_generation_without_retaining_body(diagnostic_store):
    body = [b'{"synthetic":', b'"SYNTHETIC_PRIVATE_INPUT"}']
    messages = iter([{"type": "http.request", "body": body[0], "more_body": True},
                     {"type": "http.request", "body": body[1], "more_body": False}])

    async def receive():
        return next(messages)

    async def send(message):
        pass

    async def endpoint(scope, receive, send):
        assert (await receive())["more_body"]
        assert not (await receive())["more_body"]
        diagnostics.configure_generation(model="gemini-synthetic", auto_continue=False)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    await ErrorRecordsMiddleware(endpoint)({"type": "http", "method": "POST", "headers": [],
        "path": "/v1beta/models/gemini-synthetic:generateContent"}, receive, send)
    item = diagnostic_store.list_records()["items"][0]
    assert item["request_body_bytes"] == sum(map(len, body))
    assert "SYNTHETIC_PRIVATE_INPUT" not in json.dumps(item)


def test_latest_usage_in_each_upstream_call_is_summed_without_duplicate_chunks(diagnostic_store):
    with trace():
        diagnostics.begin_upstream_call()
        parse_usage_metadata(_usage(10))
        parse_usage_metadata(_usage(14))
        parse_usage_metadata(_usage(14))
        parse_usage_metadata([1, 2, 3])  # Missing usage cannot imply zero thinking.
        diagnostics.begin_upstream_call()
        parse_usage_metadata(_usage("7"))  # Reuse the parser's numeric normalization.
        parse_usage_metadata(_usage(9))
        parse_usage_metadata(_usage(8))  # The final cumulative report is authoritative.
        diagnostics.finish_response_diagnostics("success", 200)
    assert diagnostic_store.list_records()["items"][0]["thought_tokens"] == 22


@pytest.mark.parametrize("usage,expected", [
    (None, None), ([1, 2, 3], None), (_usage(True), None),
    (_usage(-1), None), (_usage(0), 0),
])
def test_absent_reasoning_usage_stays_unknown_and_explicit_zero_is_preserved(
    diagnostic_store, usage, expected,
):
    with trace():
        diagnostics.begin_upstream_call()
        parse_usage_metadata(usage)
        diagnostics.finish_response_diagnostics("success", 200)
    assert diagnostic_store.list_records()["items"][0]["thought_tokens"] == expected


def test_store_revalidates_all_new_fields_and_token_totals_are_bounded(diagnostic_store):
    with trace():
        diagnostics.note_thinking_configuration(
            explicit=True, thinking_config=[1, "SYNTHETIC_PRIVATE", None, 4],
        )
        diagnostics.begin_upstream_call()
        parse_usage_metadata(_usage(diagnostics.MAX_COUNT + 1))
        diagnostics.begin_upstream_call()
        parse_usage_metadata(_usage(1))
        diagnostics.finish_response_diagnostics("success", 200)
    original = diagnostic_store.list_records()["items"][0]
    assert original["thought_tokens"] == diagnostics.MAX_COUNT
    diagnostic_store.record({
        **original, "thinking_explicit": "SYNTHETIC_PRIVATE", "thinking_mode": True,
        "thinking_level": ["SYNTHETIC_PRIVATE"], "thought_tokens": True,
        "thought_parts": "SYNTHETIC_PRIVATE",
    })
    changed = diagnostic_store.list_records()["items"][0]
    assert changed["thinking_explicit"] is changed["thinking_mode"] is changed["thinking_level"] is None
    assert changed["thought_tokens"] is None and changed["thought_parts"] == 0
    assert "SYNTHETIC_PRIVATE" not in json.dumps(changed)
