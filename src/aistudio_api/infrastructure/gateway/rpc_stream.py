"""Incrementally decode complete MakerSuite messages and trailing RPC status."""

import json
from collections.abc import Iterator

from aistudio_api.domain.errors import TransportError, classify_error


class RPCStreamDecoder:
    def __init__(self) -> None:
        self.depth = 0
        self.in_string = False
        self.escape = False
        self.buffer = ""
        self.start: int | None = None
        self.message_start: int | None = None
        self.position = 0

    def feed(self, text: str) -> Iterator[list]:
        self.buffer += text
        while self.position < len(self.buffer):
            char = self.buffer[self.position]
            if self.in_string:
                if self.escape:
                    self.escape = False
                elif char == "\\":
                    self.escape = True
                elif char == '"':
                    self.in_string = False
            elif char == '"':
                self.in_string = True
            elif char == "[":
                self.depth += 1
                if self.depth == 2:
                    self.start = self.position
                elif self.depth == 3:
                    self.message_start = self.position
            elif char == "]":
                if self.depth == 3 and self.message_start is not None:
                    value = self.parse(
                        self.buffer[self.message_start : self.position + 1]
                    )
                    self.buffer = self.buffer[self.position + 1 :]
                    self.position = -1
                    self.message_start = self.start = None
                    yield value
                elif self.depth == 2 and self.start is not None:
                    value = self.parse(self.buffer[self.start : self.position + 1])
                    if value and type(value[0]) is int and value[0] != 0:
                        status = {3: 400, 5: 404, 7: 403, 8: 429, 16: 401}.get(
                            value[0], 502
                        )
                        raise classify_error(status, "Build assistant RPC rejected")
                    self.start = None
                self.depth -= 1
                if self.depth < 0:
                    raise TransportError("Malformed assistant stream")
            self.position += 1

    def finish(self) -> None:
        if self.depth or self.in_string:
            raise TransportError("Truncated assistant stream")

    @staticmethod
    def parse(value: str) -> list:
        try:
            return json.loads(value)
        except ValueError as exc:
            raise TransportError("Malformed assistant message") from exc
