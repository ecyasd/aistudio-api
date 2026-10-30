"""Recognize a narrow unsigned full-body replay in a completion chunk.

This is per upstream response. It never compares across requests, retries or
continuation rounds, and never retains text or rewrites signed/native Parts.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

from aistudio_api.domain.models import Candidate, ContentPart
from aistudio_api.infrastructure.utils.response_diagnostics import (
    note_terminal_body_replay,
)

MIN_REPLAY_CHARS = 256


def _unsigned_body(part: ContentPart) -> bool:
    return (
        isinstance(part.text, str)
        and not part.thought
        and not part.thought_signature
        and all(value is None for value in (
            part.image, part.function_call, part.function_response,
            part.executable_code, part.code_execution_result,
        ))
    )


class TerminalBodyReplayGuard:
    """Skip only a terminal copy of the entire multi-chunk unsigned body.

    Ordinary repeated deltas, short repetitions, new endings and anything with
    signatures, thoughts, tools or opaque content keep their original semantics.
    The digest and counters use constant memory and are never logged or stored.
    """

    def __init__(self) -> None:
        self._digest = hashlib.sha256()
        self._chars = 0
        self._chunks = 0
        self._eligible = True

    def accept(self, candidate: Candidate) -> Candidate:
        if candidate.continuation_unsafe or any(
            not _unsigned_body(part) for part in candidate.parts
        ):
            self._eligible = False
        if not self._eligible or not candidate.text:
            return candidate

        digest = hashlib.sha256()
        for part in candidate.parts:
            digest.update((part.text or "").encode("utf-8"))
        if (
            candidate.finish_reason in (1, 2)
            and self._chunks >= 2
            and self._chars >= MIN_REPLAY_CHARS
            and len(candidate.text) == self._chars
            and digest.digest() == self._digest.digest()
        ):
            note_terminal_body_replay(self._chars)
            return replace(candidate, text="", parts=[])

        for part in candidate.parts:
            self._digest.update((part.text or "").encode("utf-8"))
        self._chars += len(candidate.text)
        self._chunks += 1
        return candidate
