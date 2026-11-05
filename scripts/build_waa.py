"""Build only the Linux amd64 local signer with an explicit SpiderMonkey SDK."""
from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deps", type=Path, required=True, help="Unpacked v152.0.4-aistudio2api.1 Linux amd64 SDK")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True, help="Build cache outside the source checkout")
    parser.add_argument("--go", default="go")
    parser.add_argument("--zig", help="Zig executable; use the linux-amd64 musl SDK when cross-compiling")
    parser.add_argument("--expected-sha")
    args = parser.parse_args()
    dependencies = args.deps.resolve(strict=True)
    if not (dependencies / "lib/libjs_static.a").is_file() or not (dependencies / "include/jsapi.h").is_file():
        raise ValueError("Invalid SpiderMonkey SDK")
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    cache = args.cache.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment.update(GOOS="linux", GOARCH="amd64", CGO_ENABLED="1", GOTOOLCHAIN="local",
                       GOCACHE=str(cache / "go-build"), GOMODCACHE=str(cache / "go-modules"),
                       CGO_CPPFLAGS=shlex.quote("-I" + (dependencies / "include").as_posix()),
                       CGO_LDFLAGS=shlex.quote("-L" + (dependencies / "lib").as_posix()))
    flags = "-s -w -linkmode external -extldflags '-Wl,--strip-all'"
    if args.expected_sha:
        actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[1], text=True).strip()
        if not re.fullmatch(r"[a-f0-9]{40}", args.expected_sha) or actual != args.expected_sha:
            raise ValueError("Native source commit mismatch")
    if args.zig:
        zig = Path(args.zig).resolve(strict=True).as_posix()
        environment.update(CC='"' + zig + '" cc -target x86_64-linux-musl',
                           CXX='"' + zig + '" c++ -target x86_64-linux-musl')
        flags = "-s -w -linkmode external -extldflags '-static'"
    elif os.name == "nt":
        raise ValueError("Cross-compiling on Windows requires --zig")
    elif not shutil.which("g++"):
        raise ValueError("Install a C++ compiler or supply --zig")
    if args.expected_sha:
        flags += " -X main.buildCommit=" + args.expected_sha
    return subprocess.run([args.go, "build", "-trimpath", "-tags", "netgo,osusergo", "-ldflags=" + flags,
                           "-o", str(output), "./cmd/waa-signer"],
                          cwd=Path(__file__).resolve().parents[1] / "native/waa-signer",
                          env=environment, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
