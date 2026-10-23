"""Initialize Compose's writable config directory without replacing any config."""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path


def prepare_config(project: Path) -> str:
    """Retain the legacy file; refuse to choose between different existing files."""
    legacy = project / "config.yaml"
    destination = project / "runtime-config" / "config.yaml"
    if legacy.is_symlink() or destination.is_symlink():
        raise ValueError("Configuration files must be regular files")
    if destination.exists():
        if not destination.is_file():
            raise ValueError("Compose configuration is not a regular file")
        if legacy.is_file() and legacy.read_bytes() != destination.read_bytes():
            raise ValueError("Both configurations exist and differ; no file was changed")
        return "preserved"
    source = legacy if legacy.is_file() else project / "config.example.yaml"
    contents = source.read_bytes()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".config-", dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(0o600)
        # An atomic, no-clobber install also protects a concurrently created file.
        destination.hardlink_to(temporary)
    finally:
        temporary.unlink(missing_ok=True)
    return "migrated" if source == legacy else "initialized"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        result = prepare_config(args.project)
    except (OSError, ValueError):
        raise SystemExit("Compose config preparation failed; existing files were retained") from None
    print("Compose config " + result + "; configuration contents were omitted")


if __name__ == "__main__":
    main()
