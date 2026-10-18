"""The release boundary must exclude runtime data and preserve reproducibility."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/build_release.py"
SPEC = importlib.util.spec_from_file_location("build_release", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def git(repo, *args):
    return (
        subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .strip()
    )


@pytest.fixture
def candidate(tmp_path):
    repo = tmp_path / "repository"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "offline@example.invalid")
    git(repo, "config", "user.name", "Offline Release Test")
    package = repo / "src/aistudio_api"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VERSION = 1\n")
    (repo / "deploy").mkdir()
    (repo / "deploy/public_access_guard.py").write_text("SAFE_GUARD = True\n")
    (repo / "deploy/secure_runner.py").write_text("MUST_NOT_SHIP = True\n")
    (repo / "config.yaml").write_text("api_keys: [synthetic-excluded-key]\n")
    (repo / ".env").write_text("SYNTHETIC_EXCLUDED_VALUE=yes\n")
    (repo / "accounts").mkdir()
    (repo / "accounts/auth.json").write_text('{"synthetic": true}')
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Synthetic release fixture")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text('<script src="/assets/main.js"></script>')
    (assets / "assets").mkdir()
    (assets / "assets/main.js").write_text("console.log('synthetic');\n")
    return repo, assets, tmp_path / "output"


def test_release_uses_git_commit_and_excludes_all_runtime_inputs(candidate):
    repo, assets, output = candidate
    commit = git(repo, "rev-parse", "HEAD")
    # A working-tree modification cannot masquerade as code from that commit.
    (repo / "src/aistudio_api/__init__.py").write_text("UNCOMMITTED_VALUE = True\n")
    result = builder.build_release(repo, assets, output, commit)
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["version"] == 1 and manifest["commit"] == result["commit"] == commit
    with tarfile.open(output / "release.tar.gz", "r:gz") as archive:
        names = set(archive.getnames())
        assert names == {
            "manifest.json",
            "runtime/public_access_guard.py",
            "src/aistudio_api/__init__.py",
            "src/aistudio_api/static/index.html",
            "src/aistudio_api/static/assets/main.js",
        }
        assert (
            archive.extractfile("src/aistudio_api/__init__.py").read()
            == b"VERSION = 1\n"
        )
        for entry in manifest["files"]:
            member = archive.getmember(entry["path"])
            content = archive.extractfile(member).read()
            assert hashlib.sha256(content).hexdigest() == entry["sha256"]
            assert member.mode == entry["mode"] == 0o644
            assert member.uid == member.gid == member.mtime == 0


def test_release_is_byte_reproducible_and_rejects_wrong_commit(candidate):
    repo, assets, output = candidate
    first = builder.build_release(repo, assets, output)
    original = (output / "release.tar.gz").read_bytes()
    os.utime(assets / "index.html", (1, 1))
    second = builder.build_release(repo, assets, output)
    assert original == (output / "release.tar.gz").read_bytes()
    assert first == second
    with pytest.raises(builder.ReleaseError, match="expected release SHA"):
        builder.build_release(repo, assets, output, "0" * 40)


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        "cookies.json",
        "config.yaml",
        "node_modules/hidden.js",
        "accounts/hidden.js",
    ],
)
def test_static_assets_reject_runtime_data_even_under_the_static_prefix(
    candidate, name
):
    repo, assets, output = candidate
    source = assets / name
    source.parent.mkdir(exist_ok=True)
    source.write_text("synthetic-excluded-content")
    with pytest.raises(builder.ReleaseError):
        builder.build_release(repo, assets, output)


def test_tracked_code_symlink_is_rejected_without_reading_its_target(candidate):
    repo, assets, output = candidate
    if os.name == "nt":
        pytest.skip("Linux symlink behavior is verified in CI")
    (repo / "src/aistudio_api/linked.py").symlink_to(repo / "config.yaml")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Synthetic forbidden symlink")
    with pytest.raises(builder.ReleaseError, match="links"):
        builder.build_release(repo, assets, output)
