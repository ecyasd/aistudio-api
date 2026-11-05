"""Local SpiderMonkey RPC bridge; no public relay or ambient HTTP proxy."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from collections.abc import AsyncGenerator
from contextlib import aclosing
from urllib.parse import urlsplit

import httpx

from aistudio_api.domain.errors import BrowserTransportError as TransportError

MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_LINE_BYTES = 64 * 1024


def local_signer_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username or parsed.password or parsed.path not in {"", "/"}
        or parsed.query or parsed.fragment or not parsed.port
    ):
        raise ValueError("WAA signer must use an explicit loopback HTTP port")
    return value.rstrip("/")


class NativeBridge:
    def __init__(self, url: str) -> None:
        self.client = httpx.AsyncClient(
            base_url=local_signer_url(url), trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(15, connect=3),
        )

    async def post(self, path: str, payload: dict[str, object], timeout: float = 15) -> dict:
        try:
            async with self.client.stream("POST", path, json=payload, timeout=timeout) as response:
                data = bytearray()
                async for piece in response.aiter_bytes():
                    data.extend(piece)
                    if len(data) > MAX_RESPONSE_BYTES:
                        raise TransportError("Local signer response exceeded its limit")
                if response.status_code != 200:
                    raise TransportError("Local signer operation failed")
                result = json.loads(data)
                if not isinstance(result, dict) or result.get("ok") is not True:
                    raise TransportError("Invalid local signer response")
                return result
        except httpx.TimeoutException as exc:
            raise TimeoutError("Local signer operation timed out") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise TransportError("Local signer is unavailable") from exc

    async def close_session(self, session: str) -> None:
        # An interrupted HTTP operation may still be releasing its server lease.
        async with asyncio.timeout(5):
            while True:
                response = await self.client.post("/v1/session/close", json={"session": session})
                if response.status_code == 200:
                    return
                if response.status_code != 409:
                    raise TransportError("Local signer session cleanup failed")
                await asyncio.sleep(0.05)

    async def stream(
        self, session: str, method: str, body: list, timeout_s: float,
    ) -> AsyncGenerator[tuple[str, object], None]:
        payload = {"session": session, "method": method, "body": body}
        status_seen = done = False
        retained = 0
        try:
            async with self.client.stream(
                "POST", "/v1/rpc/stream", json=payload, timeout=timeout_s,
            ) as response:
                if response.status_code != 200:
                    raise TransportError("Local signer RPC failed")
                pending = bytearray()
                async for piece in response.aiter_bytes():
                    pending.extend(piece)
                    while b"\n" in pending:
                        line, _, tail = pending.partition(b"\n")
                        pending = bytearray(tail)
                        if len(line) > MAX_LINE_BYTES or done:
                            raise TransportError("Invalid local signer stream framing")
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            raise TransportError("Invalid local signer stream event")
                        if set(event) == {"status"} and not status_seen:
                            status = event["status"]
                            if type(status) is not int or not 100 <= status <= 599:
                                raise TransportError("Invalid upstream status")
                            status_seen = True
                            yield "status", status
                        elif set(event) == {"chunk"} and status_seen:
                            chunk = base64.b64decode(event["chunk"], validate=True)
                            retained += len(chunk)
                            if retained > MAX_RESPONSE_BYTES:
                                raise TransportError("Upstream response exceeded its limit")
                            yield "chunk", chunk
                        elif event == {"done": True} and status_seen:
                            done = True
                        else:
                            raise TransportError("Upstream stream did not complete")
                    if len(pending) > MAX_LINE_BYTES:
                        raise TransportError("Local signer stream line exceeded its limit")
                if not done or pending:
                    raise TransportError("Local signer stream ended before completion")
        except httpx.TimeoutException as exc:
            raise TimeoutError("Local signer stream timed out") from exc
        except (httpx.HTTPError, ValueError, TypeError, binascii.Error) as exc:
            raise TransportError("Local signer stream failed") from exc

    async def rpc(self, session: str, method: str, body: list, timeout_s: float = 60) -> tuple[int, bytes]:
        status = 0
        data = bytearray()
        async with aclosing(self.stream(session, method, body, timeout_s)) as events:
            async for kind, value in events:
                if kind == "status":
                    status = int(str(value))
                elif isinstance(value, bytes):
                    data.extend(value)
        return status, bytes(data)

    async def close(self) -> None:
        await self.client.aclose()
