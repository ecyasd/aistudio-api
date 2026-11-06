#!/usr/bin/env python3
"""Fixed repository -> verified Docker archive -> one local Compose service."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

REPOSITORY = "ecyasd/aistudio-api"
BASE = Path("/srv/aistudio-console")
ETC = Path("/etc/aistudio-console")


def run(*arguments: str, timeout: int = 180) -> None:
    subprocess.run(
        arguments,
        check=True,
        timeout=timeout,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def credentials() -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in (ETC / "runtime.env").read_text().splitlines()
        if line and not line.startswith("#")
    )


def request(
    path: str, token: str | None = None, base: str = "http://127.0.0.1:8080"
) -> tuple[int, object]:
    headers = {"Authorization": "Bearer " + token} if token else {}
    try:
        with urllib.request.urlopen(
            urllib.request.Request(base + path, headers=headers), timeout=10
        ) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, None


def compose(*arguments: str) -> None:
    run(
        "docker",
        "compose",
        "--env-file",
        str(ETC / "image.env"),
        "-f",
        str(BASE / "compose.yaml"),
        *arguments,
    )


def active(name: str) -> bool:
    return (
        subprocess.run(
            ["systemctl", "is-active", "--quiet", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def wait_idle() -> None:
    quiet = None
    for _ in range(120):
        result = subprocess.run(
            ["ss", "-Hnt", "state", "established", "( sport = :8080 )"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if not result.stdout.strip():
            quiet = quiet or time.monotonic()
            if time.monotonic() - quiet >= 3:
                return
        else:
            quiet = None
        time.sleep(0.5)
    raise RuntimeError("active-requests")


def verify() -> dict[str, bool]:
    secrets = credentials()
    admin = secrets["AISTUDIO_WEB_PASSWORD"]
    status, keys = request("/api-keys", admin)
    if status != 200 or not isinstance(keys, list):
        raise RuntimeError("management-key-policy")
    api = next(
        (
            str(item["key"])
            for item in keys
            if isinstance(item, dict) and item.get("key")
        ),
        "",
    )
    public = json.loads((ETC / "deployment.json").read_text())["public_base_url"]
    checks = {}
    for label, base in (("local", "http://127.0.0.1:8080"), ("public", public)):
        checks[label + "Health"] = request("/health", base=base)[0] == 200
        checks[label + "Management"] = (
            request("/api/console/requests", admin, base)[0] == 200
        )
        checks[label + "UnauthenticatedDenied"] = (
            request("/api/console/requests", base=base)[0] == 401
        )
        checks[label + "KeyCannotManage"] = (
            request("/api/console/requests", api, base)[0] == 401
        )
        checks[label + "PasswordCannotCall"] = (
            request("/v1/auth/check", admin, base)[0] == 401
        )
        checks[label + "ModelKeyPolicy"] = request("/v1/auth/check", api or None, base)[
            0
        ] == (200 if api else 401)
    checks["trafficGuardActive"] = active("aws-traffic-guard.timer")
    if not all(checks.values()):
        raise RuntimeError("health-or-auth")
    return checks


def main() -> dict[str, object]:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", required=True)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-f0-9]{40}", args.commit) or not re.fullmatch(
        r"[a-f0-9]{64}", args.sha256
    ):
        raise RuntimeError("invalid-reference")
    with (ETC / "deployment.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with urllib.request.urlopen(
            f"https://api.github.com/repos/{REPOSITORY}/commits/master", timeout=20
        ) as response:
            if json.load(response)["sha"] != args.commit:
                raise RuntimeError("master-advanced")
        run("/usr/bin/python3", "/usr/local/libexec/aws-traffic-guard.py", "gate")
        cache = Path("/var/cache/aistudio-console")
        cache.mkdir(mode=0o700, exist_ok=True)
        archive = cache / "image.tar.gz"
        url = f"https://github.com/{REPOSITORY}/releases/download/container-{args.commit}/image.tar.gz"
        digest = hashlib.sha256()
        size = 0
        try:
            with (
                urllib.request.urlopen(url, timeout=90) as response,
                archive.open("wb") as output,
            ):
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > 1024 * 1024 * 1024:
                        raise RuntimeError("image-size")
                    digest.update(chunk)
                    output.write(chunk)
            if digest.hexdigest() != args.sha256:
                raise RuntimeError("image-checksum")
            run("docker", "load", "-i", str(archive), timeout=240)
        finally:
            archive.unlink(missing_ok=True)
        image = "aistudio-console:" + args.commit
        result = subprocess.run(
            [
                "docker",
                "image",
                "inspect",
                image,
                "--format",
                '{{ index .Config.Labels "org.opencontainers.image.revision" }}',
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        if result.stdout.strip() != args.commit:
            raise RuntimeError("image-revision")
        previous = (
            (ETC / "image.env").read_text() if (ETC / "image.env").exists() else None
        )
        old = [
            name
            for name in (
                "aistudio-api.service",
                "aistudio-waa.service",
                "aistudio2api.service",
            )
            if active(name)
        ]
        wait_idle()
        for name in old:
            run("systemctl", "stop", name)
        unit = Path("/etc/systemd/system/aistudio-api.service")
        dropins = Path("/etc/systemd/system/aistudio-api.service.d")
        legacy = ETC / "legacy-transition"
        first_container = not (ETC / "container-service-installed").exists()
        if first_container:
            legacy.mkdir(mode=0o700, exist_ok=True)
            shutil.copyfile(unit, legacy / "api.service")
            if dropins.exists():
                shutil.move(str(dropins), str(legacy / "service.d"))
            shutil.copyfile(ETC / "container.service", unit)
            unit.chmod(0o644)
            run("systemctl", "daemon-reload")
        (ETC / "image.env").write_text("AISTUDIO_IMAGE=" + image + "\n")
        (ETC / "image.env").chmod(0o600)
        try:
            run("systemctl", "restart", "aistudio-api.service")
            checks = verify()
        except Exception:
            with contextlib.suppress(Exception):
                compose("down")
            if previous:
                (ETC / "image.env").write_text(previous)
                run("systemctl", "restart", "aistudio-api.service")
            else:
                if first_container:
                    shutil.copyfile(legacy / "api.service", unit)
                    if (legacy / "service.d").exists():
                        shutil.move(str(legacy / "service.d"), str(dropins))
                    run("systemctl", "daemon-reload")
                for name in reversed(old):
                    run("systemctl", "start", name)
            raise
        (ETC / "container-service-installed").touch(mode=0o600)
        record = {
            "ok": True,
            "commit": args.commit,
            "sha256": args.sha256,
            "status": "deployed",
            "checks": checks,
            "rollbackCommit": None,
        }
        (ETC / "deployment-last.json").write_text(json.dumps(record) + "\n")
        return record


if __name__ == "__main__":
    try:
        print(json.dumps(main()))
    except Exception as error:
        print(
            json.dumps(
                {
                    "ok": False,
                    "errorType": type(error).__name__,
                    "detailsWithheld": True,
                }
            )
        )
        raise SystemExit(1) from None
