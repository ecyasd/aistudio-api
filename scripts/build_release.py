"""Build a deterministic, code-only release from one Git commit and built assets."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

SOURCE_PREFIX = "src/aistudio_api/"
STATIC_PREFIX = SOURCE_PREFIX + "static/"
GUARD_SOURCE = "deploy/public_access_guard.py"
GUARD_TARGET = "runtime/public_access_guard.py"
STATIC_SUFFIXES = {
    ".html",
    ".js",
    ".css",
    ".svg",
    ".png",
    ".jpg",
    ".webp",
    ".ico",
    ".woff",
    ".woff2",
}
FORBIDDEN_PARTS = {
    ".git",
    "node_modules",
    "__pycache__",
    "accounts",
    "sessions",
    "data",
    "config.yaml",
    "secrets.json",
    "auth.json",
    "cookies.json",
    "registry.json",
    "stats.json",
}
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_RELEASE_BYTES = 32 * 1024 * 1024


class ReleaseError(ValueError):
    """The candidate cannot satisfy the code-only release contract."""


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    ).stdout


def _safe_path(name: str) -> None:
    path = PurePosixPath(name)
    if path.is_absolute() or "\\" in name or ".." in path.parts:
        raise ReleaseError("Invalid release path")
    if any(
        part.casefold() in FORBIDDEN_PARTS or part.casefold().startswith(".env")
        for part in path.parts
    ):
        raise ReleaseError("Runtime data is forbidden in a release")


def _read_static(directory: Path) -> dict[str, bytes]:
    if directory.is_symlink() or not directory.is_dir():
        raise ReleaseError("A regular built static directory is required")
    files: dict[str, bytes] = {}
    for parent, directories, names in os.walk(directory, followlinks=False):
        base = Path(parent)
        if any((base / name).is_symlink() for name in [*directories, *names]):
            raise ReleaseError("Release assets cannot contain symbolic links")
        for name in names:
            source = base / name
            relative = source.relative_to(directory).as_posix()
            _safe_path(relative)
            if source.suffix.casefold() not in STATIC_SUFFIXES or not source.is_file():
                raise ReleaseError("Unexpected file in built static assets")
            if source.stat().st_size > MAX_FILE_BYTES:
                raise ReleaseError("Release asset exceeds its size limit")
            files[STATIC_PREFIX + relative] = source.read_bytes()
    if STATIC_PREFIX + "index.html" not in files:
        raise ReleaseError("Built frontend index.html is missing")
    return files


def build_release(
    repo: Path,
    static_dir: Path,
    output_dir: Path,
    expected_sha: str | None = None,
) -> dict[str, object]:
    """Read code from Git objects, never local configuration or account files."""
    repo = repo.resolve(strict=True)
    commit = _git(repo, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or (
        expected_sha is not None and expected_sha != commit
    ):
        raise ReleaseError("Git commit does not match the expected release SHA")
    files: dict[str, bytes] = {}
    tree = _git(repo, "ls-tree", "-r", "-z", commit, "--", SOURCE_PREFIX, GUARD_SOURCE)
    for record in tree.split(b"\0"):
        if not record:
            continue
        metadata, encoded_name = record.split(b"\t", 1)
        mode, kind, object_id = metadata.split()
        name = encoded_name.decode("utf-8")
        if name.startswith(STATIC_PREFIX):
            continue  # Always use this commit's freshly built frontend artifact.
        _safe_path(name)
        if kind != b"blob" or mode not in (b"100644", b"100755"):
            raise ReleaseError("Code releases cannot contain links or submodules")
        if name == GUARD_SOURCE:
            target = GUARD_TARGET
        elif name.startswith(SOURCE_PREFIX) and Path(name).suffix in {".py", ".js"}:
            target = name
        else:
            raise ReleaseError("Unexpected tracked file under the release allowlist")
        content = _git(repo, "cat-file", "blob", object_id.decode())
        if len(content) > MAX_FILE_BYTES:
            raise ReleaseError("Release code exceeds its size limit")
        files[target] = content
    if GUARD_TARGET not in files or SOURCE_PREFIX + "__init__.py" not in files:
        raise ReleaseError("Required application code or public guard is missing")
    files.update(_read_static(static_dir))
    if sum(map(len, files.values())) > MAX_RELEASE_BYTES:
        raise ReleaseError("Release exceeds its total size limit")
    manifest: dict[str, object] = {
        "version": 1,
        "commit": commit,
        "files": [
            {
                "path": name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "mode": 0o644,
            }
            for name, content in sorted(files.items())
        ],
    }
    manifest_bytes = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    archive = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=archive, mode="wb", filename="", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as tar,
    ):
        for name, content in sorted({**files, "manifest.json": manifest_bytes}.items()):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o644
            info.uid = info.gid = info.mtime = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(content))
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = archive.getvalue()
    (output_dir / "release.tar.gz").write_bytes(payload)
    (output_dir / "manifest.json").write_bytes(manifest_bytes)
    digest = hashlib.sha256(payload).hexdigest()
    (output_dir / "release.tar.gz.sha256").write_text(
        digest + "  release.tar.gz\n", encoding="ascii"
    )
    return {"commit": commit, "files": len(files), "sha256": digest}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--static-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("dist/release"))
    parser.add_argument("--expected-sha")
    args = parser.parse_args()
    print(
        json.dumps(
            build_release(
                args.repo, args.static_dir, args.output_dir, args.expected_sha
            ),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
