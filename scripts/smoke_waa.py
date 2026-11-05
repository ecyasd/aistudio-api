"""Verify native JS and an empty signer without accounts or Google requests."""
from __future__ import annotations

import argparse
import contextlib
import json
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--expected-sha")
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    test = subprocess.run([str(binary), "-self-test"], capture_output=True, timeout=20, check=True)
    result = json.loads(test.stdout)
    if not result.get("ok") or (args.expected_sha and result.get("commit") != args.expected_sha):
        raise ValueError("Native JS self-test failed")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="aistudio-waa-smoke-") as directory:
        process = subprocess.Popen([str(binary), "-listen", f"127.0.0.1:{port}", "-state-dir", directory],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            deadline = time.monotonic() + 15
            while True:
                try:
                    with opener.open(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                        health = json.load(response)
                    if health.get("ok") is not True or health.get("workers") != 0:
                        raise ValueError("Unexpected native worker during smoke")
                    break
                except OSError:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("Empty signer could not start") from None
                    time.sleep(0.1)
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if process.returncode != 0:
            raise RuntimeError("Native signer did not stop cleanly")
    print(json.dumps({"ok": True, "commit": result["commit"], "jsEvaluated": True,
                      "workers": 0, "googleContacted": False, "modelRequests": 0}))


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        main()
