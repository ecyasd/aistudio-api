#!/usr/bin/env python3
"""Root-owned, code-only GitHub release installer. Never print credentials."""

from __future__ import annotations

import argparse
import ast
import contextlib
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

if os.name == "posix":
    import fcntl

MAX_COMPRESSED = 16 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
MAX_MEMBERS = 512
MAX_MANIFEST = 256 * 1024
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")
STATIC_SUFFIXES = {
    ".js",
    ".css",
    ".html",
    ".svg",
    ".png",
    ".ico",
    ".webp",
    ".jpg",
    ".jpeg",
    ".gif",
    ".woff",
    ".woff2",
    ".ttf",
    ".json",
}


class DeploymentError(Exception):
    """Carries only a fixed, non-sensitive failure category."""


def require(value: object, code: str) -> None:
    if not value:
        raise DeploymentError(code)


def digest(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


@dataclass(frozen=True)
class Paths:
    base: Path = Path("/opt/aistudio-api")
    etc: Path = Path("/etc/aistudio-api")
    data: Path = Path("/var/lib/aistudio-api")

    @property
    def source(self) -> Path:
        return self.base / "source"

    @property
    def guard(self) -> Path:
        return self.base / "public_access_guard.py"

    @property
    def journal(self) -> Path:
        return self.etc / "deployment-transaction.json"

    @property
    def last(self) -> Path:
        return self.etc / "deployment-last.json"


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_name("." + path.name + "-" + uuid.uuid4().hex)
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(0o600)
        temporary.replace(path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_link(path: Path, target: Path) -> None:
    temporary = path.with_name("." + path.name + "-" + uuid.uuid4().hex)
    try:
        temporary.symlink_to(target)
        temporary.replace(path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def sync_directory(path: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def allowed_file(name: str) -> bool:
    if "\\" in name or ":" in name or "\x00" in name or len(name) > 240:
        return False
    path = PurePosixPath(name)
    if path.is_absolute() or any(
        part in (".", "..") or part.startswith(".") for part in path.parts
    ):
        return False
    if path.as_posix() != name:
        return False
    if name == "runtime/public_access_guard.py":
        return True
    if path.parts[:2] != ("src", "aistudio_api") or len(path.parts) < 3:
        return False
    if path.parts[2] == "static":
        return len(path.parts) >= 4 and path.suffix in STATIC_SUFFIXES
    return path.suffix in {".py", ".js"} and "__pycache__" not in path.parts


class LimitedReader(io.RawIOBase):
    def __init__(self, stream: gzip.GzipFile, limit: int):
        self.stream, self.remaining = stream, limit

    def read(self, size: int = -1) -> bytes:
        amount = self.remaining + 1 if size < 0 else min(size, self.remaining + 1)
        value = self.stream.read(amount)
        self.remaining -= len(value)
        require(self.remaining >= 0, "archive-expanded-limit")
        return value


def extract_release(
    archive_path: Path, destination: Path, commit: str, expected: str
) -> dict[str, object]:
    """Extract manually; tar links, duplicate paths and oversized headers are rejected."""
    require(archive_path.stat().st_size <= MAX_COMPRESSED, "archive-compressed-limit")
    with archive_path.open("rb") as source:
        require(
            hashlib.file_digest(source, "sha256").hexdigest() == expected,
            "archive-digest",
        )
    members: dict[str, str] = {}
    manifest = None
    expanded = 0
    with (
        archive_path.open("rb") as compressed,
        gzip.GzipFile(fileobj=compressed) as uncompressed,
    ):
        bounded = LimitedReader(uncompressed, MAX_EXPANDED + MAX_MEMBERS * 4096)
        with tarfile.open(fileobj=bounded, mode="r|") as archive:
            for item in archive:
                require(len(members) < MAX_MEMBERS, "archive-member-limit")
                require(item.isfile() and not item.issparse(), "archive-file-type")
                require(item.name not in members, "archive-duplicate")
                require(item.mode == 0o644 and not item.pax_headers, "archive-mode")
                require(
                    item.name == "manifest.json" or allowed_file(item.name),
                    "archive-path",
                )
                expanded += item.size
                require(
                    item.size >= 0 and expanded <= MAX_EXPANDED,
                    "archive-expanded-limit",
                )
                require(
                    item.name != "manifest.json" or item.size <= MAX_MANIFEST,
                    "manifest-size",
                )
                content = archive.extractfile(item)
                require(content is not None, "archive-body")
                assert content is not None
                blob = content.read(item.size + 1)
                require(len(blob) == item.size, "archive-truncated")
                members[item.name] = digest(blob)
                if item.name == "manifest.json":
                    manifest = json.loads(blob)
                    continue
                if item.name.endswith(".py"):
                    compile(blob, item.name, "exec")
                target = destination / item.name
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                with target.open("xb") as output:
                    output.write(blob)
                    output.flush()
                    os.fsync(output.fileno())
                target.chmod(0o644)
        # Consume gzip's trailer and reject a decompression bomb after tar EOF.
        while bounded.read(64 * 1024):
            pass
    require(
        isinstance(manifest, dict) and set(manifest) == {"version", "commit", "files"},
        "manifest-schema",
    )
    assert isinstance(manifest, dict)
    require(
        type(manifest["version"]) is int
        and manifest["version"] == 1
        and manifest["commit"] == commit,
        "manifest-version-commit",
    )
    entries = manifest["files"]
    require(
        isinstance(entries, list) and len(entries) == len(members) - 1,
        "manifest-members",
    )
    assert isinstance(entries, list)
    declared: set[str] = set()
    for entry in entries:
        require(
            isinstance(entry, dict) and set(entry) == {"path", "sha256", "mode"},
            "manifest-entry",
        )
        assert isinstance(entry, dict)
        name = entry["path"]
        require(
            isinstance(name, str) and allowed_file(name) and name not in declared,
            "manifest-path",
        )
        require(
            type(entry["mode"]) is int
            and entry["mode"] == 0o644
            and isinstance(entry["sha256"], str)
            and DIGEST.fullmatch(entry["sha256"])
            and name in members
            and entry["sha256"] == members.get(name),
            "manifest-digest-mode",
        )
        declared.add(name)
    require(declared == set(members) - {"manifest.json"}, "manifest-members")
    require(
        {
            "src/aistudio_api/__init__.py",
            "src/aistudio_api/api/app.py",
            "src/aistudio_api/static/index.html",
            "runtime/public_access_guard.py",
        }
        <= declared,
        "manifest-required-files",
    )
    for directory in destination.rglob("*"):
        if directory.is_dir():
            directory.chmod(0o755)
    destination.chmod(0o755)
    return manifest


class ReleaseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        require(
            parsed.scheme == "https"
            and parsed.hostname
            in {
                "github.com",
                "release-assets.githubusercontent.com",
                "objects.githubusercontent.com",
            }
            and not parsed.username
            and parsed.port in (None, 443),
            "release-redirect",
        )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward an authentication header/query to a redirected host.
        del req, fp, code, msg, headers, newurl
        return


def download_release(url: str, destination: Path) -> None:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), ReleaseRedirect()
    )
    with opener.open(url, timeout=30) as response, destination.open("xb") as output:
        total = 0
        while True:
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            require(total <= MAX_COMPRESSED, "archive-compressed-limit")
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    destination.chmod(0o600)


def request(
    path: str,
    token: str | None = None,
    *,
    base: str = "http://127.0.0.1:8080",
    body: bytes | None = None,
) -> tuple[int, object]:
    headers = {"Connection": "close", "Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=body, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        response = opener.open(req, timeout=15)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        blob = response.read(2 * 1024 * 1024 + 1)
        require(len(blob) <= 2 * 1024 * 1024, "diagnostic-size")
        try:
            value = json.loads(blob) if blob else None
        except (ValueError, UnicodeError):
            value = None
        status = response.status
        require(isinstance(status, int), "diagnostic-status")
        assert isinstance(status, int)
        return status, value


def service(action: str) -> None:
    require(action in {"stop", "start"}, "service-action")
    subprocess.run(
        ["systemctl", action, "aistudio-api"],
        check=True,
        timeout=100,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_healthy(admin: str) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        with contextlib.suppress(Exception):
            if request("/stats", admin)[0] == 200:
                return
        time.sleep(1)
    raise DeploymentError("startup-health")


def wait_idle(admin: str) -> None:
    deadline, quiet = time.monotonic() + 90, None
    while time.monotonic() < deadline:
        code, state = request("/browser/status", admin)
        require(code == 200 and isinstance(state, dict), "browser-status")
        assert isinstance(state, dict)
        connections = 0
        for name in ("tcp", "tcp6"):
            for line in Path("/proc/net", name).read_text().splitlines()[1:]:
                fields = line.split()
                if fields[1].rsplit(":", 1)[1].upper() == "1F90" and fields[3] == "01":
                    connections += 1
        if state.get("in_flight") == 0 and connections == 0:
            quiet = quiet or time.monotonic()
            if time.monotonic() - quiet >= 3:
                return
        else:
            quiet = None
        time.sleep(0.5)
    raise DeploymentError("idle-window")


def metadata_hashes(paths: Paths, accounts: object) -> dict[str, str]:
    require(isinstance(accounts, list), "account-list")
    assert isinstance(accounts, list)
    files = [paths.data / "accounts/registry.json"]
    for account in accounts:
        require(isinstance(account, dict), "account-metadata")
        assert isinstance(account, dict)
        name = str(account.get("id", ""))
        require(
            bool(re.fullmatch(r"[a-zA-Z0-9_.@-]+", name)) and ".." not in name,
            "account-id",
        )
        files.append(paths.data / "accounts" / name / "meta.json")
    return {
        str(path.relative_to(paths.data)): digest(path.read_bytes()) for path in files
    }


def protected_snapshot(
    paths: Paths, accounts: object, totals: object
) -> dict[str, object]:
    return {
        "config": digest((paths.data / "config.yaml").read_bytes()),
        "secrets": digest((paths.etc / "secrets.json").read_bytes()),
        "runner": digest((paths.base / "secure-runner.py").read_bytes()),
        "metadata": metadata_hashes(paths, accounts),
        "accounts": digest(json.dumps(accounts, sort_keys=True).encode()),
        "totals": totals,
    }


def warmup_disabled(path: Path) -> bool:
    module = ast.parse(path.read_bytes())
    for node in module.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "conditional_warmup":
            return (
                len(node.body) == 1
                and isinstance(node.body[0], ast.Return)
                and isinstance(node.body[0].value, ast.Constant)
                and node.body[0].value.value is None
            )
    return False


def credentials(paths: Paths) -> tuple[str, list[str]]:
    admin = json.loads((paths.etc / "secrets.json").read_bytes())["admin_password"]
    config = yaml.safe_load((paths.data / "config.yaml").read_bytes())
    raw = config.get("api_keys", [])
    require(
        isinstance(admin, str) and len(admin) >= 6 and isinstance(raw, list),
        "credentials-structure",
    )
    keys = [item.get("key", "") if isinstance(item, dict) else item for item in raw]
    require(all(isinstance(key, str) for key in keys), "keys-structure")
    return admin, [key for key in keys if key and key != admin]


def diagnostics(admin: str, keys: list[str], public_base: str | None = None) -> None:
    require(request("/stats", admin)[0] == 200, "management-auth")
    require(request("/__deployment/api-auth", admin)[0] == 401, "auth-separation")
    require(request("/__deployment/api-auth")[0] == 401, "anonymous-auth")
    for key in keys:
        require(request("/__deployment/api-auth", key)[0] == 204, "existing-key-bearer")
        require(
            request("/__deployment/api-auth?" + urllib.parse.urlencode({"key": key}))[0]
            == 204,
            "existing-key-query",
        )
        require(request("/stats", key)[0] == 401, "key-management-separation")
        if public_base:
            endpoint = "/v1beta/models/deployment-validation:generateContent"
            require(
                request(endpoint, key, base=public_base, body=b"{")[0] in (400, 422),
                "public-key-bearer",
            )
            require(
                request(
                    endpoint + "?" + urllib.parse.urlencode({"key": key}),
                    base=public_base,
                    body=b"{",
                )[0]
                in (400, 422),
                "public-key-query",
            )


def checked_release(paths: Paths, value: str) -> Path:
    path = Path(value)
    require(
        path.is_absolute()
        and path.parent.parent == paths.base / "releases"
        and SHA.fullmatch(path.parent.name)
        and path.name == "source",
        "journal-release-path",
    )
    require(
        not path.is_symlink() and not path.parent.is_symlink(), "release-path-symlink"
    )
    return path


def recover(paths: Paths) -> bool:
    """Restore only code paths; called by ExecStartPre without starting systemd."""
    if not paths.journal.exists():
        return False
    require(not paths.journal.is_symlink(), "journal-symlink")
    journal = json.loads(paths.journal.read_bytes())
    require(
        type(journal.get("version")) is int
        and journal["version"] == 1
        and journal.get("phase") in {"installing", "committed"},
        "journal-version",
    )
    checked_release(paths, journal["new_source"])
    if journal["phase"] != "committed":
        previous = journal["old_source"]
        if previous["kind"] == "directory":
            backup = Path(previous["path"])
            require(
                backup.parent == paths.base
                and backup.name.startswith("source.pre-github-")
                and not backup.is_symlink(),
                "journal-backup-path",
            )
            if backup.is_dir():
                if paths.source.is_symlink():
                    paths.source.unlink()
                require(not paths.source.exists(), "recovery-source-conflict")
                backup.replace(paths.source)
            else:
                require(
                    paths.source.is_dir() and not paths.source.is_symlink(),
                    "recovery-source-missing",
                )
        else:
            require(previous["kind"] == "symlink", "journal-source-kind")
            atomic_link(paths.source, checked_release(paths, previous["path"]))
        old_guard = journal["old_guard"]
        if old_guard["kind"] == "file":
            backup = Path(old_guard["path"])
            require(
                backup.parent == paths.etc
                and backup.name.startswith("deployment-guard-")
                and not backup.is_symlink(),
                "journal-guard-path",
            )
            require(backup.is_file(), "recovery-guard-missing")
            temporary = paths.guard.with_name(".guard-" + uuid.uuid4().hex)
            shutil.copyfile(backup, temporary)
            temporary.chmod(0o644)
            with temporary.open("rb") as output:
                os.fsync(output.fileno())
            temporary.replace(paths.guard)
            sync_directory(paths.base)
        else:
            require(
                old_guard["kind"] == "symlink"
                and old_guard["path"]
                == str(paths.source / "runtime/public_access_guard.py"),
                "journal-guard-link",
            )
            atomic_link(paths.guard, Path(old_guard["path"]))
        atomic_json(
            paths.last,
            {
                "ok": False,
                "phase": "code-rolled-back",
                "commit": journal["commit"],
                "credentialsChanged": False,
                "modelInferenceInvoked": False,
            },
        )
    paths.journal.unlink()
    return True


def rollout(
    paths: Paths, release: Path, commit: str, artifact_digest: str, public_base: str
) -> dict[str, object]:
    require(warmup_disabled(paths.base / "secure-runner.py"), "startup-warmup-policy")
    admin, keys = credentials(paths)
    require(keys, "configured-keys-required")
    diagnostics(admin, keys)
    wait_idle(admin)
    accounts_code, accounts = request("/api/accounts", admin)
    stats_code, stats = request("/stats", admin)
    require(
        accounts_code == 200 and stats_code == 200 and isinstance(stats, dict),
        "baseline-api",
    )
    assert isinstance(stats, dict)
    stopped = False
    committed = False
    try:
        stopped = True
        service("stop")
        baseline = protected_snapshot(paths, accounts, stats["totals"])
        if paths.source.is_symlink():
            old_source = {
                "kind": "symlink",
                "path": str(
                    checked_release(paths, str(paths.source.resolve(strict=True)))
                ),
            }
        else:
            require(paths.source.is_dir(), "source-directory")
            old_source = {
                "kind": "directory",
                "path": str(paths.base / ("source.pre-github-" + uuid.uuid4().hex)),
            }
        if paths.guard.is_symlink():
            require(
                paths.guard.readlink()
                == paths.source / "runtime/public_access_guard.py",
                "guard-link-target",
            )
            old_guard = {"kind": "symlink", "path": str(paths.guard.readlink())}
        else:
            backup = paths.etc / ("deployment-guard-" + uuid.uuid4().hex + ".py")
            shutil.copyfile(paths.guard, backup)
            backup.chmod(0o600)
            with backup.open("rb") as output:
                os.fsync(output.fileno())
            old_guard = {"kind": "file", "path": str(backup)}
        journal = {
            "version": 1,
            "phase": "installing",
            "commit": commit,
            "new_source": str(release),
            "old_source": old_source,
            "old_guard": old_guard,
        }
        atomic_json(paths.journal, journal)
        if old_source["kind"] == "directory":
            paths.source.replace(old_source["path"])
        atomic_link(paths.guard, paths.source / "runtime/public_access_guard.py")
        atomic_link(paths.source, release)
        service("start")
        wait_healthy(admin)
        diagnostics(admin, keys, public_base)
        accounts_after_code, accounts_after = request("/api/accounts", admin)
        stats_after_code, stats_after = request("/stats", admin)
        browser_code, browser = request("/browser/status", admin)
        require(
            accounts_after_code == 200
            and stats_after_code == 200
            and isinstance(stats_after, dict),
            "after-api",
        )
        assert isinstance(stats_after, dict)
        require(
            protected_snapshot(paths, accounts_after, stats_after["totals"])
            == baseline,
            "protected-data-changed",
        )
        require(
            browser_code == 200
            and isinstance(browser, dict)
            and browser.get("status") == "not_started",
            "browser-started",
        )
        require(
            warmup_disabled(paths.base / "secure-runner.py"), "startup-warmup-policy"
        )
        result = {
            "ok": True,
            "commit": commit,
            "sha256": artifact_digest,
            "status": "deployed",
            "checks": {
                "protectedDataUnchanged": True,
                "existingKeysAuthenticated": True,
                "managementAuthSeparated": True,
                "publicAuthWithoutInference": True,
                "browserNotStarted": True,
                "startupWarmupDisabled": True,
                "noModelInference": True,
                "noCookieSessionFilesRead": True,
            },
            "rollbackCommit": Path(old_source["path"]).parent.name
            if old_source["kind"] == "symlink"
            else None,
        }
        journal["phase"] = "committed"
        atomic_json(paths.journal, journal)
        committed = True
        atomic_json(paths.last, result)
        paths.journal.unlink()
        return result
    finally:
        if not committed and stopped:
            try:
                service("stop")
                recover(paths)
            finally:
                service("start")
                wait_healthy(admin)


def load_config(paths: Paths) -> dict[str, str]:
    path = paths.etc / "deployment.json"
    info = path.stat()
    require(
        not path.is_symlink() and info.st_uid == 0 and not info.st_mode & 0o022,
        "deployment-config-permissions",
    )
    config = json.loads(path.read_bytes())
    require(
        isinstance(config, dict)
        and set(config) == {"repository", "branch", "public_base_url"}
        and config["repository"] == "ecyasd/aistudio-api"
        and config["branch"] == "master"
        and isinstance(config["public_base_url"], str),
        "deployment-config",
    )
    origin = urllib.parse.urlsplit(config["public_base_url"])
    require(
        origin.scheme == "https"
        and origin.hostname
        and not origin.username
        and not origin.password
        and not origin.path
        and not origin.query
        and not origin.fragment
        and origin.port in (None, 443)
        and not re.search(r"[\x00-\x20\x7f]", config["public_base_url"]),
        "deployment-public-origin",
    )
    return config


def trusted_directory(path: Path) -> None:
    require(path.is_dir() and not path.is_symlink(), "trusted-directory")
    info = path.stat()
    require(
        info.st_uid == 0 and not info.st_mode & 0o022, "trusted-directory-permissions"
    )


def deploy(paths: Paths, commit: str, artifact_digest: str) -> dict[str, object]:
    require(
        SHA.fullmatch(commit) and DIGEST.fullmatch(artifact_digest),
        "deployment-arguments",
    )
    config = load_config(paths)
    releases = paths.base / "releases"
    require(not releases.is_symlink(), "releases-symlink")
    releases.mkdir(mode=0o755, exist_ok=True)
    releases.chmod(0o755)
    trusted_directory(releases)
    stage = releases / (".staging-" + uuid.uuid4().hex)
    stage.mkdir(mode=0o700)
    try:
        archive = stage / "release.tar.gz"
        source = stage / "source"
        source.mkdir(mode=0o700)
        url = f"https://github.com/{config['repository']}/releases/download/deploy-{commit}/release.tar.gz"
        download_release(url, archive)
        manifest = extract_release(archive, source, commit, artifact_digest)
        destination = releases / commit
        if destination.exists() or destination.is_symlink():
            require(
                destination.is_dir() and not destination.is_symlink(),
                "release-existing-path",
            )
            marker = json.loads((destination / "deployment-artifact.json").read_bytes())
            require(
                marker == {"commit": commit, "sha256": artifact_digest},
                "release-existing-digest",
            )
            entries = manifest["files"]
            assert isinstance(entries, list)
            for entry in entries:
                target = destination / "source" / entry["path"]
                require(
                    target.is_file()
                    and not target.is_symlink()
                    and digest(target.read_bytes()) == entry["sha256"],
                    "release-existing-code",
                )
        else:
            archive.unlink()
            atomic_json(
                stage / "deployment-artifact.json",
                {"commit": commit, "sha256": artifact_digest},
            )
            stage.chmod(0o755)
            stage.replace(destination)
        trusted_directory(destination)
        trusted_directory(destination / "source")
        for installed in (destination / "source").rglob("*"):
            require(not installed.is_symlink(), "release-installed-symlink")
            info = installed.stat()
            require(
                info.st_uid == 0 and not info.st_mode & 0o022,
                "release-installed-permissions",
            )
        if (
            paths.source.is_symlink()
            and paths.source.resolve() == destination / "source"
        ):
            admin, keys = credentials(paths)
            require(
                keys and warmup_disabled(paths.base / "secure-runner.py"),
                "current-release-policy",
            )
            diagnostics(admin, keys, config["public_base_url"])
            result = {
                "ok": True,
                "commit": commit,
                "sha256": artifact_digest,
                "status": "already_current",
                "checks": {
                    "existingKeysAuthenticated": True,
                    "managementAuthSeparated": True,
                    "publicAuthWithoutInference": True,
                    "startupWarmupDisabled": True,
                    "noModelInference": True,
                    "noCookieSessionFilesRead": True,
                },
                "rollbackCommit": None,
            }
            atomic_json(paths.last, result)
            return result
        return rollout(
            paths,
            destination / "source",
            commit,
            artifact_digest,
            config["public_base_url"],
        )
    finally:
        if stage.exists():
            require(
                stage.parent == releases
                and stage.name.startswith(".staging-")
                and not stage.is_symlink(),
                "stage-cleanup-path",
            )
            shutil.rmtree(stage)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit")
    parser.add_argument("--sha256")
    parser.add_argument("--recover", action="store_true")
    arguments = parser.parse_args()
    paths = Paths()
    try:
        require(os.name == "posix" and os.geteuid() == 0, "root-required")
        os.umask(0o077)
        trusted_directory(paths.base)
        trusted_directory(paths.etc)
        require(
            arguments.recover or bool(arguments.commit and arguments.sha256),
            "deployment-arguments",
        )
        require(
            not arguments.recover or not (arguments.commit or arguments.sha256),
            "deployment-arguments",
        )
        descriptor = os.open(
            paths.etc / "deployment.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w") as lock:
            try:
                fcntl.flock(
                    lock, fcntl.LOCK_EX | (fcntl.LOCK_NB if arguments.recover else 0)
                )
            except BlockingIOError:
                # ExecStartPre invoked by a live deployer must not roll it back
                # or wait on its lock while that deployer awaits systemctl start.
                require(arguments.recover, "deployment-lock")
                print(json.dumps({"ok": True, "recovery": "active-deployer"}))
                return 0
            if arguments.recover:
                changed = recover(paths)
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "recovery": "code-restored" if changed else "clean",
                        }
                    )
                )
                return 0
            if paths.journal.exists():
                # A previous deployer may have died after starting the candidate.
                # Normal deployment must stop it before restoring code. The
                # separate boot-time --recover path runs before the app starts.
                service("stop")
                recover(paths)
                service("start")

            def interrupted(_signum, _frame):
                raise DeploymentError("interrupted")

            signal.signal(signal.SIGTERM, interrupted)
            signal.signal(signal.SIGINT, interrupted)
            result = deploy(paths, arguments.commit, arguments.sha256)
            print(json.dumps(result, sort_keys=True))
            return 0
    except BaseException as error:
        rolled_back = False
        with contextlib.suppress(Exception):
            rolled_back = (
                json.loads(paths.last.read_bytes()).get("phase") == "code-rolled-back"
            )
        print(
            json.dumps(
                {
                    "ok": False,
                    "phase": "deployment",
                    "errorType": type(error).__name__,
                    "rolledBack": rolled_back,
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
