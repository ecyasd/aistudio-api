"""Persistent, bounded metadata history. Contains no request or response content."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

from aistudio_api.config import resolve_data_dir

_lock = threading.RLock()
MAX_REQUESTS = 5000
MAX_EVENTS = 10000


def enabled() -> bool:
    return os.getenv("AISTUDIO_TELEMETRY_PERSIST", "1").lower() not in {"0", "false"}


def retention_seconds() -> int:
    return (
        max(1, min(30, int(os.getenv("AISTUDIO_TELEMETRY_RETENTION_DAYS", "7"))))
        * 86400
    )


def database_path() -> Path:
    return Path(
        os.getenv("AISTUDIO_TELEMETRY_DB") or resolve_data_dir() / "telemetry.sqlite3"
    )


def save(
    kind: str, identifier: str, timestamp: float, payload: dict[str, object]
) -> None:
    if not enabled():
        return
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock, closing(sqlite3.connect(path, timeout=2)) as connection, connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS observations (kind TEXT, id TEXT, time REAL, payload TEXT, PRIMARY KEY(kind,id))"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS observed_time ON observations(kind,time)"
        )
        connection.execute(
            "INSERT OR REPLACE INTO observations VALUES (?,?,?,?)",
            (kind, identifier, timestamp, json.dumps(payload, ensure_ascii=False)),
        )
        connection.execute(
            "DELETE FROM observations WHERE time < ?",
            (time.time() - retention_seconds(),),
        )
        for category, limit in (("request", MAX_REQUESTS), ("event", MAX_EVENTS)):
            connection.execute(
                "DELETE FROM observations WHERE kind=? AND id IN (SELECT id FROM observations WHERE kind=? ORDER BY time DESC LIMIT -1 OFFSET ?)",
                (category, category, limit),
            )


def recent(kind: str, limit: int) -> list[dict[str, object]]:
    path = database_path()
    if not enabled() or not path.exists():
        return []
    with _lock, closing(sqlite3.connect(path, timeout=2)) as connection:
        rows = connection.execute(
            "SELECT payload FROM observations WHERE kind=? AND time >= ? ORDER BY time DESC LIMIT ?",
            (kind, time.time() - retention_seconds(), limit),
        ).fetchall()
        return [json.loads(row[0]) for row in rows]
