"""Serialize configuration changes and publish complete, private files atomically."""

from __future__ import annotations

import os
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

import yaml

_WRITE_LOCK = threading.RLock()


def api_key_items(raw: object) -> list[dict[str, str]]:
    """Normalize the key formats already accepted by the configuration reader."""
    if isinstance(raw, str):
        values: list[object] = [
            part for line in raw.splitlines() for part in line.split(",")
        ]
    elif isinstance(raw, list):
        values = list(raw)
    else:
        values = []
    items: list[dict[str, str]] = []
    for value in values:
        if isinstance(value, dict):
            key = str(value.get("key") or "").strip()
            name = str(value.get("name") or "API Key").strip()
            created = str(value.get("created_at") or "").strip()
        elif isinstance(value, str):
            key, name, created = value.strip(), "API Key", ""
        else:
            continue
        if key:
            items.append({"key": key, "name": name, "created_at": created})
    return items


def mutate_config(
    path: Path, change: Callable[[dict[str, object]], dict[str, object]]
) -> dict[str, object]:
    """Read, modify and atomically replace one document under a shared lock."""
    path = path.resolve()
    with _WRITE_LOCK:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ValueError("Configuration must be a mapping")
        updated = change(loaded)
        contents = yaml.safe_dump(updated, allow_unicode=True, sort_keys=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
                target.write(contents)
                target.flush()
                os.fsync(target.fileno())
            temporary.chmod(0o600)
            temporary.replace(path)
            if os.name == "posix":
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
        return updated


def save_model_config(path: Path, document: dict[str, object]) -> dict[str, object]:
    """Model settings cannot add, delete, restore or rotate API calling keys."""
    def preserve_keys(current: dict[str, object]) -> dict[str, object]:
        updated = dict(document)
        if "api_keys" in current:
            updated["api_keys"] = current["api_keys"]
        else:
            updated.pop("api_keys", None)
        return updated

    return mutate_config(path, preserve_keys)
