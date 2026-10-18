"""Incremental, bounded framing for AI Studio's JSON response batches."""

from __future__ import annotations

import json
from collections.abc import Generator

from aistudio_api.domain.errors import RequestError
from aistudio_api.infrastructure.gateway.wire_parser import parse_response_chunk

XSSI_PREFIX = ")]}'"
MAX_PENDING_JSON_CHARS = 16 * 1024 * 1024
MAX_JSON_NESTING = 128


class IncrementalJSONStreamParser:
    """Validate ``[[chunk, ...], ...]`` while releasing each complete chunk.

    Only the active chunk is retained. Closing the outer JSON document is
    required even when a generation finish marker has already been observed.
    """

    def __init__(self) -> None:
        self.buffer = ""
        self._pos = 0
        self._state = "preamble"
        self._in_string = False
        self._escape = False
        self._stack: list[str] = []

    @staticmethod
    def _invalid() -> RequestError:
        return RequestError(502, "Upstream response is malformed or truncated")

    def feed(self, data: str) -> Generator[list[object], None, None]:
        self.buffer += data

        if len(self.buffer) > MAX_PENDING_JSON_CHARS:
            raise RequestError(502, "Upstream response chunk exceeds the buffer limit")

        if self._state == "preamble":
            self.buffer = self.buffer.lstrip()
            if not self.buffer or (
                XSSI_PREFIX.startswith(self.buffer) and self.buffer != XSSI_PREFIX
            ):
                return
            if self.buffer.startswith(XSSI_PREFIX):
                self.buffer = self.buffer[len(XSSI_PREFIX) :]
            self._state = "root"

        while self._pos < len(self.buffer):
            ch = self.buffer[self._pos]
            if self._state == "chunk":
                if self._escape:
                    self._escape = False
                elif self._in_string:
                    if ch == "\\":
                        self._escape = True
                    elif ch == '"':
                        self._in_string = False
                elif ch == '"':
                    self._in_string = True
                elif ch in "[{":
                    self._stack.append(ch)
                    if len(self._stack) > MAX_JSON_NESTING:
                        raise self._invalid()
                elif ch in "]}":
                    expected = "[" if ch == "]" else "{"
                    if not self._stack or self._stack.pop() != expected:
                        raise self._invalid()
                    if not self._stack:
                        chunk_text = self.buffer[: self._pos + 1]
                        try:
                            chunk = json.loads(chunk_text)
                        except (json.JSONDecodeError, RecursionError) as exc:
                            raise self._invalid() from exc
                        self.buffer = self.buffer[self._pos + 1 :]
                        self._pos = 0
                        self._state = "after_chunk"
                        yield chunk
                        continue
                self._pos += 1
                continue

            # Validate wrapper separators as well as the chunk JSON itself.
            if ch.isspace():
                self._pos += 1
                continue
            state = self._state
            if state == "root" and ch == "[":
                self._state = "batch_or_end"
            elif state in ("batch_or_end", "batch") and ch == "[":
                self._state = "chunk_or_end"
            elif state == "batch_or_end" and ch == "]":
                self._state = "complete"
            elif state in ("chunk_or_end", "next_chunk") and ch == "[":
                self.buffer = self.buffer[self._pos :]
                self._pos = 1
                self._stack = ["["]
                self._state = "chunk"
                continue
            elif state in ("chunk_or_end", "after_chunk") and ch == "]":
                self._state = "after_batch"
            elif state == "after_chunk" and ch == ",":
                self._state = "next_chunk"
            elif state == "after_batch" and ch == ",":
                self._state = "batch"
            elif state == "after_batch" and ch == "]":
                self._state = "complete"
            else:
                raise self._invalid()
            self._pos += 1

        if self._state != "chunk":
            self.buffer = ""
            self._pos = 0

    def finish(self) -> Generator[list[object], None, None]:
        if self._state != "complete":
            raise self._invalid()
        yield from ()


def classify_chunk(chunk: list) -> tuple[str, object]:
    candidate = parse_response_chunk(chunk)
    if candidate.thinking:
        return ("thinking", candidate.thinking)
    if candidate.function_calls:
        return ("tool_calls", candidate.function_calls)
    if candidate.text:
        return ("body", candidate.text)
    if candidate.thought_signature:
        return ("thought_signature", candidate.thought_signature)
    return ("unknown", "")
