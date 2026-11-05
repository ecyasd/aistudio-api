"""Bounded metadata ledger for completed client requests and observed usage."""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

from aistudio_api.config import resolve_data_dir

MAX_RECORDS = 1000
RETENTION_DAYS = 30
_lock = threading.RLock()
_REQUEST = re.compile(r"req_[a-f0-9]{8,32}\Z")
_MODEL = re.compile(r"(?:gemini|gemma)[a-z0-9_.-]{0,90}\Z")
_COLUMNS = (
    "request_id", "time", "model", "channel", "outcome", "stream", "duration_ms",
    "first_body_ms", "upstream_calls", "reported_calls", "usage_complete",
    "prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens",
)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
 request_id TEXT PRIMARY KEY, time REAL NOT NULL, model TEXT, channel TEXT NOT NULL,
 outcome TEXT NOT NULL, stream INTEGER NOT NULL, duration_ms INTEGER NOT NULL,
 first_body_ms INTEGER, upstream_calls INTEGER NOT NULL, reported_calls INTEGER NOT NULL,
 usage_complete INTEGER NOT NULL, prompt_tokens INTEGER, completion_tokens INTEGER,
 total_tokens INTEGER, cached_tokens INTEGER
)
"""


def database_path() -> Path:
    return resolve_data_dir() / "request_usage.sqlite3"


def _prune(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM requests WHERE time < ?", (time.time() - RETENTION_DAYS * 86400,))
    connection.execute("DELETE FROM requests WHERE request_id IN (SELECT request_id FROM requests ORDER BY time DESC, request_id DESC LIMIT -1 OFFSET ?)", (MAX_RECORDS,))


def record_request(summary: dict, calls: dict[int, dict[str, int]]) -> None:
    """Accept only a fixed schema; no headers, account IDs, errors or content."""
    request_id = summary.get("request_id")
    model = str(summary.get("model") or "").removeprefix("models/")
    if not isinstance(request_id, str) or not _REQUEST.fullmatch(request_id):
        return
    if summary.get("outcome") not in {"success", "recovered", "failed", "cancelled"}:
        return
    upstream = max(0, min(int(summary.get("upstream_calls") or 0), 32))
    observed = [value for key, value in calls.items() if 0 < key <= upstream]
    totals = {}
    for name in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens"):
        values = [value[name] for value in observed if name in value]
        totals[name] = sum(values) if values else None
    complete = upstream > 0 and len(observed) == upstream and all(
        all(key in value for key in ("prompt_tokens", "completion_tokens", "total_tokens"))
        for value in observed
    )
    values = (
        request_id, time.time(), model if _MODEL.fullmatch(model) else None,
        "build" if model.endswith("-build") else "playground", summary["outcome"],
        bool(summary.get("stream")), int(summary.get("duration_ms") or 0),
        (summary.get("timings_ms") or {}).get("first_body"), upstream, len(observed), complete,
        *(totals[name] for name in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens")),
    )
    with _lock, closing(sqlite3.connect(database_path(), timeout=2)) as connection, connection:
        connection.execute(_SCHEMA)
        connection.execute("INSERT OR REPLACE INTO requests VALUES (" + ",".join("?" for _ in values) + ")", values)
        _prune(connection)


def query_requests(*, days: int = 7, model: str = "", page: int = 1, page_size: int = 25) -> dict:
    empty = {"items": [], "total": 0, "page": page, "page_size": page_size,
             "models": [], "totals": {"requests": 0, "success": 0, "failed": 0, "cancelled": 0,
                                       "upstream_calls": 0, "reported_calls": 0, "usage_complete": 0,
                                       "prompt_tokens": None, "completion_tokens": None,
                                       "total_tokens": None, "cached_tokens": None},
             "retention": {"max_records": MAX_RECORDS, "days": RETENTION_DAYS}}
    path = database_path()
    if not path.exists():
        return empty
    where = "time >= ?"
    parameters: list[object] = [time.time() - days * 86400]
    if model:
        where += " AND model = ?"
        parameters.append(model)
    with _lock, closing(sqlite3.connect(path, timeout=2)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.execute(_SCHEMA)
        _prune(connection)
        rows = connection.execute("SELECT * FROM requests WHERE " + where + " ORDER BY time DESC, request_id DESC LIMIT ? OFFSET ?",
                                  [*parameters, page_size, (page - 1) * page_size]).fetchall()
        aggregates = connection.execute("SELECT COUNT(*) requests, SUM(outcome IN ('success','recovered')) success, SUM(outcome='failed') failed, SUM(outcome='cancelled') cancelled, SUM(upstream_calls) upstream_calls, SUM(reported_calls) reported_calls, SUM(usage_complete) usage_complete, SUM(prompt_tokens) prompt_tokens, SUM(completion_tokens) completion_tokens, SUM(total_tokens) total_tokens, SUM(cached_tokens) cached_tokens FROM requests WHERE " + where,
                                        parameters).fetchone()
        totals = dict(aggregates)
        for key in ("requests", "success", "failed", "cancelled", "upstream_calls", "reported_calls", "usage_complete"):
            totals[key] = totals[key] or 0
        return {**empty, "items": [{name: row[name] for name in _COLUMNS} for row in rows],
                "total": totals["requests"], "totals": totals,
                "models": [row[0] for row in connection.execute("SELECT DISTINCT model FROM requests WHERE model IS NOT NULL ORDER BY model")]}
