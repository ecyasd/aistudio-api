from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from aistudio_api.api.gemini_errors import classify_gemini_error_payload
from aistudio_api.api.responses import (
    to_gemini_parts,
    to_gemini_usage_metadata,
)
from aistudio_api.application.generation_errors import GenerationFailure
from aistudio_api.domain.errors import (
    AuthError,
    RequestError,
    SessionExpiredError,
    UsageLimitExceeded,
)


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("reason,status,retry_after,error_type", [
    ("rate_limit_exceeded", 429, 17, None),
    ("upstream_auth_expired", 503, None, "upstream_auth_expired"),
])
def test_application_failure_projection_preserves_preparation_and_public_fields(
    wrapped, reason, status, retry_after, error_type,
):
    from aistudio_api.api.gemini_errors import project_gemini_error
    from aistudio_api.application.account_orchestrator import AccountPreparationError
    from aistudio_api.application.generation_errors import GenerationFailure

    failure = GenerationFailure(
        status, reason=reason, message="safe public message",
        retry_after_seconds=retry_after,
        original_error=RuntimeError("synthetic-private-detail"),
    )
    error = AccountPreparationError("synthetic-account", failure) if wrapped else failure
    projected = project_gemini_error(error)
    assert projected.status_code == status
    assert projected.message == "safe public message"
    assert projected.error_type == error_type
    assert projected.headers == ({"Retry-After": "17"} if retry_after else None)
    assert "synthetic-private-detail" not in repr(projected)


@asynccontextmanager
async def _offline_request_scope():
    yield SimpleNamespace(auth_file=None, context_id=1)


def test_to_gemini_usage_metadata_uses_visible_and_reasoning_tokens():
    assert to_gemini_usage_metadata(
        {
            "prompt_tokens": 9,
            "completion_tokens": 316,
            "total_tokens": 325,
            "completion_tokens_details": {
                "reasoning_tokens": 290,
                "visible_tokens": 26,
            },
        }
    ).model_dump(mode="json") == {
        "promptTokenCount": 9,
        "candidatesTokenCount": 26,
        "thoughtsTokenCount": 290,
        "totalTokenCount": 325,
    }


def test_to_gemini_parts_keeps_function_call_and_response_parts():
    parts = to_gemini_parts(
        "",
        function_calls=[{"name": "getWeather", "args": {"city": "Shanghai"}}],
        function_responses=[{"name": "getWeather", "args": {"temperature": "24C"}}],
    )

    assert [part.model_dump(mode="json", exclude_none=True) for part in parts] == [
        {"functionCall": {"name": "getWeather", "args": {"city": "Shanghai"}}},
        {
            "functionResponse": {
                "name": "getWeather",
                "response": {"temperature": "24C"},
            }
        },
    ]


def test_to_gemini_parts_can_emit_thought_part():
    assert [
        part.model_dump(mode="json", exclude_none=True)
        for part in to_gemini_parts("答案", thinking="思考")
    ] == [
        {"text": "思考", "thought": True},
        {"text": "答案"},
    ]


def test_to_gemini_parts_can_emit_inline_image_data():
    from aistudio_api.domain.models import GeneratedImage

    parts = to_gemini_parts(
        "说明",
        images=[GeneratedImage(mime="image/png", data=b"png-bytes", size=9)],
    )

    assert [part.model_dump(mode="json", exclude_none=True) for part in parts] == [
        {"text": "说明"},
        {"inlineData": {"mimeType": "image/png", "data": "cG5nLWJ5dGVz"}},
    ]


def test_classify_gemini_error_payload():
    """Verify exception mapping to Gemini HTTP status and reason code."""
    assert classify_gemini_error_payload(SessionExpiredError("expired")) == (
        503,
        ("The API key is valid, but upstream Google sessions have expired. "
         "Re-import Google cookies on the management page; keep your API key."),
        "UNAVAILABLE",
    )
    assert classify_gemini_error_payload(AuthError("auth forbidden")) == (
        503,
        "Upstream Google authentication failed. Check accounts on the management page; keep your API key.",
        "UNAVAILABLE",
    )
    assert classify_gemini_error_payload(UsageLimitExceeded("quota exceeded")) == (
        429,
        "quota exceeded",
        "RESOURCE_EXHAUSTED",
    )
    assert classify_gemini_error_payload(ValueError("invalid input")) == (
        400,
        "invalid input",
        "INVALID_ARGUMENT",
    )
    assert classify_gemini_error_payload(RequestError(429, "rate limit")) == (
        429,
        "HTTP 429: rate limit",
        "RESOURCE_EXHAUSTED",
    )
    # JSPB bracket array error from upstream Google MakerSuite
    jspb_400 = 'HTTP 400: [,[3,"Invalid value (), Unexpected list for single non-message field.",[["type.googleapis.com/google.rpc.BadRequest"]]]]'
    assert classify_gemini_error_payload(RequestError(400, jspb_400)) == (
        400,
        "Invalid value (), Unexpected list for single non-message field.",
        "INVALID_ARGUMENT",
    )


def test_clean_upstream_error_message():
    from aistudio_api.application.generation_errors import clean_upstream_error_message

    assert clean_upstream_error_message("") == ""
    assert clean_upstream_error_message("Normal plain error") == "Normal plain error"
    assert (
        clean_upstream_error_message("HTTP 429: rate limit") == "HTTP 429: rate limit"
    )
    jspb_nested = 'HTTP 400: [,[3,"Please enable tool_config.include_server_side_tool_invocations to use Built-in tools with Function calling.",[["type.googleapis.com/details"]]]]'
    assert (
        clean_upstream_error_message(jspb_nested)
        == "Please enable tool_config.include_server_side_tool_invocations to use Built-in tools with Function calling."
    )
    jspb_404 = 'HTTP 404: [,[5,"Requested entity was not found."]]'
    assert clean_upstream_error_message(jspb_404) == "Requested entity was not found."


@pytest.mark.asyncio
async def test_handle_attempt_exception_retries_ambiguous_rpc_404_in_place():
    from unittest.mock import MagicMock

    from aistudio_api.application.generation_recovery import (
        _recover_failure as handle_attempt_exception,
    )
    from aistudio_api.infrastructure.gateway.client import AIStudioClient

    mock_client = MagicMock(spec=AIStudioClient)
    mock_client.clear_templates = MagicMock()

    # 1. Ambiguous RPC 404 error should be retried in-place without raising
    ambiguous_err = RequestError(
        404,
        "Ambiguous request for service '' and method '/GenerativeService.StreamGenerateContent'. Please use fully qualified (unique) service and method names to call this method.",
    )
    should_retry = await handle_attempt_exception(
        ambiguous_err,
        attempt=0,
        model_path="gemini-3.7-flash",
        normalized_model="models/gemini-3.7-flash",
        client=mock_client,
        has_yielded_data=False,
    )
    assert should_retry is True
    mock_client.clear_templates.assert_called_once()

    # 2. Other legitimate 404 errors (e.g. model not found) MUST NOT be retried
    other_404 = RequestError(404, "Model 'models/nonexistent' not found.")
    with pytest.raises(GenerationFailure) as exc_info:
        await handle_attempt_exception(
            other_404,
            attempt=0,
            model_path="nonexistent",
            normalized_model="models/nonexistent",
            client=mock_client,
            has_yielded_data=False,
        )
    assert exc_info.value.description.status == 404

    # 3. If data has already been yielded to the client, retry MUST NOT happen
    with pytest.raises(GenerationFailure):
        await handle_attempt_exception(
            ambiguous_err,
            attempt=0,
            model_path="gemini-3.7-flash",
            normalized_model="models/gemini-3.7-flash",
            client=mock_client,
            has_yielded_data=True,
        )

@pytest.mark.asyncio
async def test_build_gemini_streaming_response_passes_requested_model(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    from aistudio_api.api.gemini import (
        _build_gemini_streaming_response,
    )
    from aistudio_api.api.schemas import (
        GeminiContent,
        GeminiGenerateContentRequest,
        GeminiPart,
    )
    from aistudio_api.infrastructure.gateway.client import AIStudioClient

    passed_model = None
    monkeypatch.setattr(
        "aistudio_api.application.generation.ensure_active_account", AsyncMock()
    )

    async def fake_stream_generate_content(*args, **kwargs):
        nonlocal passed_model
        passed_model = kwargs.get("model")
        yield ("chunk", b"data: test")
        yield ("finish_reason", "STOP")

    mock_client = MagicMock(spec=AIStudioClient)
    mock_client.stream_generate_content = MagicMock(side_effect=fake_stream_generate_content)
    mock_client.session.request_scope = _offline_request_scope

    req = GeminiGenerateContentRequest(
        contents=[GeminiContent(role="user", parts=[GeminiPart(text="hello")])]
    )
    resp = _build_gemini_streaming_response(
        client=mock_client,
        req=req,
        model_path="gemini-2.5-pro",
    )

    chunks = [chunk async for chunk in resp.body_iterator]
    assert len(chunks) > 0
    assert passed_model == "models/gemini-2.5-pro"


@pytest.mark.asyncio
async def test_streaming_metadata_event_does_not_block_failover(monkeypatch):
    """验证在仅接收到 thought_signature 等内部元数据事件时发生异常，不会误判为已发送数据而阻断重试。"""
    from unittest.mock import AsyncMock, MagicMock

    from aistudio_api.api.gemini import (
        _build_gemini_streaming_response,
    )
    from aistudio_api.api.schemas import (
        GeminiContent,
        GeminiGenerateContentRequest,
        GeminiPart,
    )
    from aistudio_api.domain.errors import RequestError
    from aistudio_api.infrastructure.gateway.client import AIStudioClient

    attempt_count = 0
    monkeypatch.setattr(
        "aistudio_api.application.generation.ensure_active_account", AsyncMock()
    )

    async def fake_stream_generate_content(*args, **kwargs):
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count == 1:
            # 首次尝试：仅产生内部元数据事件，尚未发出实际正文 chunk，随即遇到 204/抖动异常
            yield ("thought_signature", "sig_early")
            raise RequestError(204, "No content")
        # 第二次尝试：正常流式输出
        yield ("body", "Hello world after retry")
        yield ("finish_reason", "STOP")

    mock_client = MagicMock(spec=AIStudioClient)
    mock_client.stream_generate_content = MagicMock(side_effect=fake_stream_generate_content)
    mock_client.session.request_scope = _offline_request_scope
    mock_client.clear_templates = MagicMock()

    req = GeminiGenerateContentRequest(
        contents=[GeminiContent(role="user", parts=[GeminiPart(text="hello")])]
    )
    resp = _build_gemini_streaming_response(
        client=mock_client,
        req=req,
        model_path="gemini-3.7-flash",
    )

    chunks = [chunk async for chunk in resp.body_iterator]
    # 验证重试成功完成
    assert attempt_count == 2
    assert any("Hello world after retry" in str(c) for c in chunks)
