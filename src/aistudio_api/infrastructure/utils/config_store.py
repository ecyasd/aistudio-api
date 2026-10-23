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


class ConfigConflictError(ValueError):
    """A field edited from an old snapshot has changed in the stored config."""


def save_model_config(
    path: Path,
    document: dict[str, object],
    *,
    baseline: dict[str, object] | None = None,
) -> dict[str, object]:
    """Preserve keys and merge only fields changed from an editor's snapshot.

    Legacy callers without a baseline explicitly replace the YAML document.
    Baseline comparisons and publication share the same lock as key/log saves.
    """
    def preserve_keys(current: dict[str, object]) -> dict[str, object]:
        if baseline is None:
            updated = dict(document)
        else:
            missing = object()
            updated = dict(current)
            for key in (document.keys() | baseline.keys()) - {"api_keys"}:
                previous = baseline.get(key, missing)
                desired = document.get(key, missing)
                if desired == previous:
                    continue
                stored = current.get(key, missing)
                if stored != previous and stored != desired:
                    raise ConfigConflictError("Configuration changed since it was loaded")
                if desired is missing:
                    updated.pop(key, None)
                else:
                    updated[key] = desired
        if "api_keys" in current:
            updated["api_keys"] = current["api_keys"]
        else:
            updated.pop("api_keys", None)
        return updated

    return mutate_config(path, preserve_keys)
