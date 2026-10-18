"""Offline archive and transaction checks; all credentials below are synthetic."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import sys
import tarfile
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "github_deploy_under_test", Path(__file__).parents[2] / "deploy/github_deploy.py"
)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

COMMIT = "a" * 40
FILES = {
    "src/aistudio_api/__init__.py": b"",
    "src/aistudio_api/api/app.py": b"app = None\n",
    "src/aistudio_api/static/index.html": b"<html>synthetic</html>",
    "runtime/public_access_guard.py": b"guard = True\n",
}


def archive(tmp_path, *, extra=None, transform=None):
    manifest = {
        "version": 1,
        "commit": COMMIT,
        "files": [
            {"path": name, "sha256": hashlib.sha256(blob).hexdigest(), "mode": 420}
            for name, blob in FILES.items()
        ],
    }
    if transform:
        transform(manifest)
    members = [(name, blob, tarfile.REGTYPE, 420) for name, blob in FILES.items()]
    members.append(
        ("manifest.json", json.dumps(manifest).encode(), tarfile.REGTYPE, 420)
    )
    members.extend(extra or [])
    path = tmp_path / "release.tar.gz"
    with tarfile.open(path, "w:gz", format=tarfile.USTAR_FORMAT) as output:
        for name, blob, kind, mode in members:
            info = tarfile.TarInfo(name)
            info.size = len(blob)
            info.type = kind
            info.mode = mode
            info.linkname = (
                "/synthetic-outside"
                if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE)
                else ""
            )
            output.addfile(info, io.BytesIO(blob))
    destination = tmp_path / "candidate"
    destination.mkdir()
    return path, destination, hashlib.sha256(path.read_bytes()).hexdigest()


def test_archive_round_trip_keeps_only_code(tmp_path):
    path, destination, expected = archive(tmp_path)
    manifest = module.extract_release(path, destination, COMMIT, expected)
    assert manifest["version"] == 1
    assert {
        file.relative_to(destination).as_posix()
        for file in destination.rglob("*")
        if file.is_file()
    } == set(FILES)
    assert (destination / "runtime/public_access_guard.py").read_bytes() == FILES[
        "runtime/public_access_guard.py"
    ]


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../outside.py", tarfile.REGTYPE),
        ("/absolute.py", tarfile.REGTYPE),
        ("src/aistudio_api/../config.py", tarfile.REGTYPE),
        ("src\\aistudio_api\\oops.py", tarfile.REGTYPE),
        ("src/aistudio_api/static/../../config.yaml", tarfile.REGTYPE),
        ("config.yaml", tarfile.REGTYPE),
        ("runtime/secure-runner.py", tarfile.REGTYPE),
        ("deploy/github_deploy.py", tarfile.REGTYPE),
        ("src/aistudio_api/secret.py", tarfile.SYMTYPE),
        ("src/aistudio_api/secret.py", tarfile.LNKTYPE),
        ("src/aistudio_api/directory", tarfile.DIRTYPE),
    ],
)
def test_archive_rejects_unsafe_names_and_member_types(tmp_path, name, kind):
    path, destination, expected = archive(tmp_path, extra=[(name, b"", kind, 420)])
    with pytest.raises(module.DeploymentError):
        module.extract_release(path, destination, COMMIT, expected)


def test_archive_rejects_duplicate_member(tmp_path):
    path, destination, expected = archive(
        tmp_path, extra=[("src/aistudio_api/__init__.py", b"", tarfile.REGTYPE, 420)]
    )
    with pytest.raises(module.DeploymentError, match="archive-duplicate"):
        module.extract_release(path, destination, COMMIT, expected)


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_commit",
        "wrong_hash",
        "string_mode",
        "bool_mode",
        "bool_version",
        "extra_field",
        "missing_member_with_null_hash",
    ],
)
def test_archive_rejects_invalid_manifest(tmp_path, mutation):
    def transform(value):
        if mutation == "wrong_commit":
            value["commit"] = "b" * 40
        elif mutation == "wrong_hash":
            value["files"][0]["sha256"] = "0" * 64
        elif mutation == "string_mode":
            value["files"][0]["mode"] = "0644"
        elif mutation == "bool_mode":
            value["files"][0]["mode"] = True
        elif mutation == "bool_version":
            value["version"] = True
        elif mutation == "missing_member_with_null_hash":
            value["files"][0]["path"] = "src/aistudio_api/different.py"
            value["files"][0]["sha256"] = None
        else:
            value["unexpected"] = "value"

    path, destination, expected = archive(tmp_path, transform=transform)
    with pytest.raises(module.DeploymentError):
        module.extract_release(path, destination, COMMIT, expected)


@pytest.mark.parametrize("limit", ["compressed", "expanded", "members"])
def test_archive_limits_are_enforced(tmp_path, monkeypatch, limit):
    path, destination, expected = archive(tmp_path)
    monkeypatch.setattr(
        module,
        {
            "compressed": "MAX_COMPRESSED",
            "expanded": "MAX_EXPANDED",
            "members": "MAX_MEMBERS",
        }[limit],
        1,
    )
    with pytest.raises(module.DeploymentError):
        module.extract_release(path, destination, COMMIT, expected)


def test_archive_wrong_external_digest_rejected_before_writes(tmp_path):
    path, destination, _ = archive(tmp_path)
    with pytest.raises(module.DeploymentError, match="archive-digest"):
        module.extract_release(path, destination, COMMIT, "0" * 64)
    assert list(destination.iterdir()) == []


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip(
            "Root-owned symlink transactions require Linux filesystem semantics"
        )
    paths = module.Paths(tmp_path / "application", tmp_path / "etc", tmp_path / "data")
    for directory in (
        paths.base,
        paths.etc,
        paths.data,
        paths.source,
        paths.data / "accounts/synthetic",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    (paths.source / "old-code.py").write_text("old = True\n")
    paths.guard.write_text("old_guard = True\n")
    (paths.base / "secure-runner.py").write_text(
        "async def conditional_warmup(self):\n    return None\n"
    )
    (paths.etc / "secrets.json").write_text(
        json.dumps({"admin_password": "synthetic-management-only"})
    )
    (paths.data / "config.yaml").write_text("api_keys: [synthetic-api-only]\n")
    (paths.data / "accounts/registry.json").write_text('{"accounts":["synthetic"]}')
    (paths.data / "accounts/synthetic/meta.json").write_text('{"id":"synthetic"}')
    # Access to this synthetic Cookie fixture is forbidden even during checks.
    auth = paths.data / "accounts/synthetic/auth.json"
    auth.write_bytes(b"synthetic-cookie-must-never-be-read")
    real_read = Path.read_bytes

    def guarded_read(path):
        assert path != auth, "deployer must never read account Cookie/session files"
        return real_read(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read)
    accounts = [{"id": "synthetic", "name": "synthetic account"}]
    calls = []

    def request(path, token=None, *, base="http://127.0.0.1:8080", body=None):
        if base.startswith("https:"):
            assert body == b"{"
            return 422, {}
        if path.startswith("/__deployment/api-auth"):
            return (
                (204, None)
                if token == "synthetic-api-only" or "key=synthetic-api-only" in path
                else (401, {})
            )
        if token != "synthetic-management-only":
            return 401, {}
        if path == "/api/accounts":
            return 200, accounts
        if path == "/stats":
            return 200, {"totals": {"requests": 61}}
        if path == "/browser/status":
            return 200, {"status": "not_started", "in_flight": 0}
        raise AssertionError("Unexpected diagnostic endpoint")

    monkeypatch.setattr(module, "request", request)
    monkeypatch.setattr(module, "service", calls.append)
    monkeypatch.setattr(module, "wait_idle", lambda _admin: None)
    monkeypatch.setattr(module, "wait_healthy", lambda _admin: None)
    release = paths.base / "releases" / COMMIT / "source"
    (release / "runtime").mkdir(parents=True)
    (release / "runtime/public_access_guard.py").write_text("new_guard = True\n")
    (release / "new-code.py").write_text("new = True\n")
    return paths, release, calls


def test_initial_rollout_preserves_keys_accounts_stats_and_installs_links(deployment):
    paths, release, calls = deployment
    result = module.rollout(
        paths, release, COMMIT, "b" * 64, "https://synthetic.invalid"
    )
    assert (
        result["ok"] and result["sha256"] == "b" * 64 and result["status"] == "deployed"
    )
    assert result["rollbackCommit"] is None
    assert all(result["checks"].values())
    assert paths.source.is_symlink() and paths.source.resolve() == release
    assert paths.guard.resolve() == release / "runtime/public_access_guard.py"
    assert len(list(paths.base.glob("source.pre-github-*"))) == 1
    assert calls == ["stop", "start"]
    assert not paths.journal.exists()


@pytest.mark.parametrize(
    "exception", [module.DeploymentError("synthetic-failure"), KeyboardInterrupt()]
)
def test_verification_failure_or_interrupt_rolls_back_only_code(
    deployment, monkeypatch, exception
):
    paths, release, calls = deployment
    config = (paths.data / "config.yaml").read_bytes()
    secrets = (paths.etc / "secrets.json").read_bytes()
    original = module.diagnostics

    def fail_after_start(admin, keys, public_base=None):
        if public_base:
            raise exception
        original(admin, keys)

    monkeypatch.setattr(module, "diagnostics", fail_after_start)
    with pytest.raises(type(exception)):
        module.rollout(paths, release, COMMIT, "b" * 64, "https://synthetic.invalid")
    assert not paths.source.is_symlink()
    assert (paths.source / "old-code.py").is_file()
    assert (
        not paths.guard.is_symlink() and paths.guard.read_text() == "old_guard = True\n"
    )
    assert (paths.data / "config.yaml").read_bytes() == config
    assert (paths.etc / "secrets.json").read_bytes() == secrets
    assert calls == ["stop", "start", "stop", "start"]
    assert json.loads(paths.last.read_bytes())["phase"] == "code-rolled-back"
    assert not paths.journal.exists()


def test_recovery_after_first_directory_rename_is_idempotent(deployment):
    paths, release, _ = deployment
    backup = paths.base / "source.pre-github-synthetic"
    guard_backup = paths.etc / "deployment-guard-synthetic.py"
    guard_backup.write_bytes(paths.guard.read_bytes())
    module.atomic_json(
        paths.journal,
        {
            "version": 1,
            "phase": "installing",
            "commit": COMMIT,
            "new_source": str(release),
            "old_source": {"kind": "directory", "path": str(backup)},
            "old_guard": {"kind": "file", "path": str(guard_backup)},
        },
    )
    paths.source.rename(backup)
    assert module.recover(paths)
    assert (paths.source / "old-code.py").is_file()
    assert not module.recover(paths)


def test_interruption_during_stop_still_restarts_original_service(
    deployment, monkeypatch
):
    paths, release, calls = deployment

    def stop_interrupted(action):
        calls.append(action)
        if len(calls) == 1:
            raise KeyboardInterrupt

    monkeypatch.setattr(module, "service", stop_interrupted)
    with pytest.raises(KeyboardInterrupt):
        module.rollout(paths, release, COMMIT, "b" * 64, "https://synthetic.invalid")
    assert calls == ["stop", "stop", "start"]
    assert not paths.source.is_symlink()
    assert (paths.source / "old-code.py").is_file()
    assert not paths.journal.exists()


def test_committed_journal_is_not_rolled_back(deployment):
    paths, release, _ = deployment
    module.atomic_json(
        paths.journal,
        {
            "version": 1,
            "phase": "committed",
            "commit": COMMIT,
            "new_source": str(release),
        },
    )
    assert module.recover(paths)
    assert (paths.source / "old-code.py").is_file()
    assert not paths.last.exists()


def test_recovery_rejects_paths_outside_code_roots(deployment):
    paths, release, _ = deployment
    module.atomic_json(
        paths.journal,
        {
            "version": 1,
            "phase": "installing",
            "commit": COMMIT,
            "new_source": str(release),
            "old_source": {"kind": "directory", "path": str(paths.data)},
            "old_guard": {"kind": "file", "path": "unused"},
        },
    )
    with pytest.raises(module.DeploymentError, match="journal-backup-path"):
        module.recover(paths)
    assert (paths.data / "config.yaml").is_file()
