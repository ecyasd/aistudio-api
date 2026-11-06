"""HTTP failures must expose useful categories without credentials or response bodies."""

from __future__ import annotations

import json
import logging
import traceback
from unittest.mock import AsyncMock, patch

import h11
import httpx
import pytest

from aistudio_api.application.api_service_gemini import (
    classify_gemini_error_payload,
    format_sse_error,
)
from aistudio_api.domain.errors import TransportError
from aistudio_api.infrastructure.gateway.client import PreparedRequest
from aistudio_api.infrastructure.gateway.streaming import StreamingGateway
from aistudio_api.infrastructure.gateway.transport import HTTPStreamTransport
from aistudio_api.infrastructure.waa.signer import RemoteProofSigner

COOKIE_MARKER = "fixture-cookie-must-not-appear"


def _reject_invalid_cookie(request: httpx.Request) -> httpx.Response:
    """Exercise h11's real header validation, then HTTPX's public exception type."""
    try:
        h11.Request(method=request.method, target="/", headers=request.headers.raw)
    except h11.LocalProtocolError as exc:
        assert COOKIE_MARKER in str(exc)
        raise httpx.LocalProtocolError(str(exc)) from exc
    raise AssertionError("The fixture Cookie must be rejected by the protocol parser")


def _assert_safe_error(exc: Exception, logs: str, category: str) -> None:
    status, message, _status_name = classify_gemini_error_payload(exc)
    assert status == 502
    assert category in message
    assert COOKIE_MARKER not in message
    assert COOKIE_MARKER not in format_sse_error(exc)
    assert COOKIE_MARKER not in logs
    assert COOKIE_MARKER not in "".join(traceback.format_exception(exc))


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_invalid_cookie_header_is_redacted_from_transport_errors(caplog, stream):
    transport = HTTPStreamTransport()
    transport._client = httpx.AsyncClient(
        transport=httpx.MockTransport(_reject_invalid_cookie)
    )
    cookies = {"SAPISID": COOKIE_MARKER + "\r\ninvalid"}
    request = PreparedRequest(
        url="https://example.invalid/generate", headers={}, body="{}"
    )

    class Session:
        def send_streaming_request(self, **kwargs):
            kwargs.pop("ctx", None)
            return transport.send_streaming_request(**kwargs, cookies=cookies)

    try:
        with (
            caplog.at_level(logging.DEBUG, logger="aistudio.transport"),
            pytest.raises(TransportError) as caught,
        ):
            if stream:
                gateway = StreamingGateway(session=Session())  # type: ignore[arg-type]
                async for _event in gateway.stream_chat(
                    request=request, model="gemini-fixture"
                ):
                    pass
            else:
                await transport.send_request(
                    url=request.url,
                    headers=request.headers,
                    body=request.body,
                    cookies=cookies,
                )
        _assert_safe_error(caught.value, caplog.text, "LocalProtocolError")
    finally:
        await transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 401, 500])
async def test_proof_server_error_body_is_not_exposed(caplog, status):
    def handler(request: httpx.Request) -> httpx.Response:
        assert COOKIE_MARKER in json.loads(request.content)["cookies"]
        return httpx.Response(
            status, json={"ok": False, "error": "Rejected SAPISID=" + COOKIE_MARKER}
        )

    signer = RemoteProofSigner(server_url="https://example.invalid")
    signer._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with (
            caplog.at_level(logging.DEBUG, logger="aistudio.waa"),
            pytest.raises(TransportError) as caught,
        ):
            await signer.mint_proof(cookies={"SAPISID": COOKIE_MARKER})
        category = "invalid proof response" if status == 200 else f"HTTP {status}"
        _assert_safe_error(caught.value, caplog.text, category)
    finally:
        await signer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_class", [httpx.ConnectError, httpx.ReadTimeout, ValueError]
)
async def test_proof_client_exceptions_are_redacted_without_changing_retries(
    caplog, error_class
):
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise error_class("Underlying request contained SAPISID=" + COOKIE_MARKER)

    signer = RemoteProofSigner(server_url="https://example.invalid")
    original_client = httpx.AsyncClient
    try:
        with (
            patch(
                "aistudio_api.infrastructure.waa.signer.httpx.AsyncClient",
                side_effect=lambda **kwargs: original_client(
                    transport=httpx.MockTransport(handler), **kwargs
                ),
            ),
            patch(
                "aistudio_api.infrastructure.waa.signer.asyncio.sleep", new=AsyncMock()
            ),
            caplog.at_level(logging.DEBUG, logger="aistudio.waa"),
            pytest.raises(TransportError) as caught,
        ):
            await signer.mint_proof(cookies={"SAPISID": COOKIE_MARKER})
        assert attempts == 2
        assert "2 attempts" in str(caught.value)
        _assert_safe_error(caught.value, caplog.text, error_class.__name__)
    finally:
        await signer.close()
