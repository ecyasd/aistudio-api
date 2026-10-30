"""Streaming replay workflow for chat completions."""

from __future__ import annotations

import codecs
import json
from collections.abc import AsyncGenerator
from contextlib import aclosing
from pathlib import Path

from aistudio_api.config import settings
from aistudio_api.domain.errors import (
    IncompleteResponseError,
    RequestError,
    classify_error,
)
from aistudio_api.infrastructure.gateway.capture import CapturedRequest
from aistudio_api.infrastructure.gateway.session import BrowserSession
from aistudio_api.infrastructure.gateway.stream_parser import (
    IncrementalJSONStreamParser,
)
from aistudio_api.infrastructure.gateway.terminal_body_replay import (
    TerminalBodyReplayGuard,
)
from aistudio_api.infrastructure.gateway.wire_parser import (
    is_response_chunk,
    parse_chunk_usage,
    parse_response_chunk,
)
from aistudio_api.infrastructure.utils.logger import get_logger
from aistudio_api.infrastructure.utils.response_diagnostics import note_timing

logger = get_logger("streaming")
MAX_ERROR_BODY_CHARS = 8192
MAX_STREAM_DUMP_CHARS = 16 * 1024 * 1024


def _dump_stream_exchange(
    *,
    model: str,
    url: str,
    modified_body: str,
    status_code: int,
    raw_response: str,
) -> None:
    if not settings.dump_raw_response:
        return

    out_dir = Path(settings.dump_raw_response_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_model = model.replace("/", "_")
    timestamp = __import__("time").strftime("%Y%m%d_%H%M%S")
    payload = {
        "kind": "stream_generate_content",
        "model": model,
        "url": url,
        "status_code": status_code,
        "modified_body": json.loads(modified_body),
        "raw_response": raw_response,
    }
    path = out_dir / f"aistudio_stream_generate_content_{safe_model}_{timestamp}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    logger.info("已落盘流式原始请求/响应: %s", path)


def _summarize_error_body(raw_response: str, limit: int = 500) -> str:
    text = raw_response.strip()
    if not text:
        return ""

    try:
        payload = json.loads(text)
        compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except json.JSONDecodeError:
        compact = " ".join(text.split())

    if len(compact) > limit:
        return compact[:limit] + "..."
    return compact


class StreamingGateway:
    def __init__(self, session: BrowserSession | None = None):
        self._session = session

    async def stream_chat(
        self,
        *,
        captured: CapturedRequest,
        model: str,
    ) -> AsyncGenerator[tuple[str, object | None], None]:
        if not captured:
            raise ValueError("captured request is required")
        if self._session is None:
            raise RuntimeError("browser session is required for streaming xhr replay")
        modified_body = captured.body

        parser = IncrementalJSONStreamParser()
        replay_guard = TerminalBodyReplayGuard()
        latest_usage: dict[str, object] | None = None
        latest_finish_reason: int | None = None
        latest_response_id: str | None = None
        raw_parts: list[str] = []
        retained_chars = 0
        retain_limit = (
            MAX_STREAM_DUMP_CHARS
            if settings.dump_raw_response
            else MAX_ERROR_BODY_CHARS
        )
        decoder = codecs.getincrementaldecoder("utf-8")()
        status_code = 0
        async with aclosing(
            self._session.send_streaming_request(
                body=modified_body,
                timeout_ms=settings.timeout_stream * 1000,
                url=captured.url if captured else None,
                headers=captured.headers if captured else None,
            )
        ) as events:
            async for event_type, payload in events:
                if event_type == "status" and payload and not status_code:
                    note_timing("upstream_headers")
                    status_code = int(str(payload))
                elif event_type == "chunk" and payload:
                    note_timing("upstream_first_byte")
                    if isinstance(payload, bytes):
                        try:
                            text_payload = decoder.decode(payload)
                        except UnicodeDecodeError as exc:
                            raise RequestError(
                                502, "Upstream response contains invalid UTF-8"
                            ) from exc
                    else:
                        text_payload = str(payload)
                    if (
                        settings.dump_raw_response or status_code != 200
                    ) and retained_chars < retain_limit:
                        retained = text_payload[: retain_limit - retained_chars]
                        raw_parts.append(retained)
                        retained_chars += len(retained)
                    if status_code != 200:
                        continue
                    for parsed_chunk in parser.feed(text_payload):
                        # JSON framing alone cannot validate a generation
                        # chunk. Do not let a prior finish marker hide a malformed
                        # sibling that non-streaming parsing would reject.
                        if not is_response_chunk(parsed_chunk):
                            raise RequestError(
                                502, "Upstream response contains a malformed generation chunk"
                            )
                        usage = parse_chunk_usage(parsed_chunk)
                        if usage:
                            latest_usage = usage
                        if (
                            isinstance(parsed_chunk, list)
                            and len(parsed_chunk) > 7
                            and isinstance(parsed_chunk[7], str)
                            and parsed_chunk[7]
                        ):
                            latest_response_id = parsed_chunk[7]
                            yield ("response_id", latest_response_id)
                        candidate = replay_guard.accept(parse_response_chunk(parsed_chunk))
                        if (
                            candidate.continuation_unsafe
                            or candidate.code_output
                            or candidate.sources
                        ):
                            # Internal eligibility marker, consumed by the
                            # continuation wrapper and never rendered as SSE.
                            yield ("continuation_unsafe", True)
                        if (
                            candidate.finish_reason is not None
                            and candidate.finish_reason > 0
                        ):
                            latest_finish_reason = candidate.finish_reason
                        if candidate.parts:
                            yield ("parts", candidate.parts)
        raw_response = "".join(raw_parts)
        _dump_stream_exchange(
            model=model,
            url=captured.url,
            modified_body=modified_body,
            status_code=status_code,
            raw_response=raw_response,
        )
        if status_code != 200:
            detail = _summarize_error_body(raw_response)
            if status_code in (401, 403, 429):
                raise classify_error(status_code, raw_response)
            if detail:
                raise RequestError(status_code, detail)
            raise RequestError(status_code, "")

        try:
            decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            raise RequestError(502, "Upstream response contains invalid UTF-8") from exc
        # EOF validates the outer JSON envelope; a finish marker alone cannot
        # turn a truncated document or a failed transport into success.
        list(parser.finish())
        if latest_finish_reason is None:
            raise IncompleteResponseError
        yield ("finish_reason", latest_finish_reason)
        yield ("usage", latest_usage)
        yield ("done", None)
