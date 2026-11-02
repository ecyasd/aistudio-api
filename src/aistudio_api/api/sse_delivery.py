"""Request-local first-write acknowledgement shared across HTTP middleware."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

FIRST_SSE_SEND_STATE_KEY = "aistudio_first_sse_send"
SSE_DELIVERY_STATE_KEY = "aistudio_sse_delivery"
SSE_SEND_TIMEOUT_SECONDS = 30.0


@dataclass
class SSEDelivery:
    # None when iterating a response directly without the outer middleware.
    first_sent: asyncio.Event | None = None
    send_timeout_seconds: float = SSE_SEND_TIMEOUT_SECONDS
