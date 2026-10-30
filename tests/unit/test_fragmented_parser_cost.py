"""Fragmented large values remain bounded without copying the growing prefix."""
from __future__ import annotations

import json

import pytest

from aistudio_api.domain.errors import RequestError
from aistudio_api.infrastructure.gateway import stream_parser


class BoundedConcatFragment(str):
    def __radd__(self, prefix):
        if len(prefix) > 512:
            raise AssertionError("the accumulated JSON prefix was copied for a new fragment")
        return str(prefix) + str(self)


def test_fragmented_active_chunk_does_not_copy_its_growing_prefix():
    parser = stream_parser.IncrementalJSONStreamParser()
    assert list(parser.feed('[[["')) == []
    for _ in range(128):
        assert list(parser.feed(BoundedConcatFragment("x" * 128))) == []
    assert list(parser.feed('"]]]')) == [["x" * 16384]]
    assert list(parser.finish()) == []
    assert parser.buffer == ""


def test_fragmented_size_limit_includes_archived_prefix(monkeypatch):
    monkeypatch.setattr(stream_parser, "MAX_PENDING_JSON_CHARS", 100)
    parser = stream_parser.IncrementalJSONStreamParser()
    list(parser.feed('[[["'))
    for _ in range(5):
        list(parser.feed("x" * 16))
    with pytest.raises(RequestError, match="buffer limit"):
        list(parser.feed("x" * 32))


def test_fragmented_escapes_and_multiple_chunks_preserve_exact_payload():
    expected = [[{"escaped": '\\"[{}]中文' * 100}], [None, "second"]]
    raw = ")]}'\n" + json.dumps([expected])
    parser = stream_parser.IncrementalJSONStreamParser()
    received = [item for char in raw for item in parser.feed(char)]
    received.extend(parser.finish())
    assert received == expected
    assert parser.buffer == ""
