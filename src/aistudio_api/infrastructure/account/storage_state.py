"""Atomic, versioned writes shared by imports and browser Cookie checkpoints."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
import tempfile
import threading
from pathlib import Path

_locks: dict[Path, threading.RLock] = {}
_locks_guard = threading.Lock()
_ANY_VERSION = object()


def _path_lock(path: Path) -> threading.RLock:
    with _locks_guard:
        return _locks.setdefault(path.resolve(), threading.RLock())


def auth_file_version(path: str | Path) -> str | None:
    """Fingerprint bytes without exposing the stored credentials."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def atomic_write_json(
    path: Path, data: object, *, expected_version: object = _ANY_VERSION
) -> bool:
    """Replace complete JSON, optionally only if no import has replaced its bytes.

    Imports and checkpoints share this lock. The comparison and replacement are
    synchronous, so an asyncio request cannot interleave either. A failed write
    leaves the previous file intact, including its permissions.
    """
    target = path.resolve()
    with _path_lock(target):
        if expected_version is not _ANY_VERSION and auth_file_version(target) != expected_version:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o600
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        temporary_path = Path(temporary)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            temporary_path.chmod(mode)
            temporary_path.replace(target)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary_path.unlink()
        return True
