#!/usr/bin/env python3
"""Optional hardened launcher using systemd credentials; see deploy/README.md."""
from __future__ import annotations

import json
import logging
import os
import secrets
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / "src"))
os.chdir(SOURCE)

credential_dir = Path(os.environ["CREDENTIALS_DIRECTORY"])
credentials = json.loads((credential_dir / "app-secrets").read_text())
admin_password = credentials["admin_password"]
if len(admin_password) < 6:
    raise RuntimeError("Application credentials failed validation")

# No passwords in process command lines, environment or application config dumps.
from aistudio_api.config import settings  # noqa: E402 - resolve local source and credentials first

settings.web_password = admin_password
# API keys are maintained by the owner in the web UI's mutable configuration.
# Do not retain the former startup key as an invisible second credential.
settings.api_keys = frozenset()
settings.dump_requests = False
settings.dump_to_file = False
settings.dump_raw_response = False
settings.dump_dir = "/var/lib/aistudio-api/request-dumps"
settings.log_level = "WARNING"

# Explicit CDP loopback binding; discard upstream wildcard Origin access.
from aistudio_api.infrastructure.browser import browser_engine  # noqa: E402 - apply private settings first

_original_build_args = browser_engine.build_chromium_args


def private_browser_args(*args, **kwargs):
    values = _original_build_args(*args, **kwargs)
    values = [v for v in values if not v.startswith(("--remote-debugging-address=", "--remote-allow-origins="))]
    values.append("--remote-debugging-address=127.0.0.1")
    return values


browser_engine.build_chromium_args = private_browser_args

# Startup prewarm can send an upstream probe. Defer browser/model bootstrap
# until the owner actually makes an inference request, including after imports.
from aistudio_api.infrastructure.gateway.client import AIStudioClient  # noqa: E402 - configure runtime before app import


async def conditional_warmup(self: AIStudioClient):  # noqa: ARG001 - match warmup's signature
    return None


AIStudioClient.warmup = conditional_warmup

from fastapi import Depends, HTTPException, Request, Response  # noqa: E402 - app imports must follow runtime patches
from public_access_guard import install_public_access_guard  # noqa: E402 - import alongside the configured app

from aistudio_api.api.app import app  # noqa: E402 - install runtime patches before constructing the app
from aistudio_api.api.dependencies import require_api_key  # noqa: E402 - import alongside the configured app

install_public_access_guard(app)


def configured_api_key_dependency(request: Request):
    """Fail closed and keep website authentication separate from API keys."""
    from public_access_guard import _token

    from aistudio_api.infrastructure.gateway.model_defaults import (
        get_configured_api_keys,
    )

    token = _token(request.scope, "api")
    try:
        candidates = get_configured_api_keys() - {settings.web_password}
        accepted = bool(token) and any(
            secrets.compare_digest(token.encode("utf-8"), candidate.encode("utf-8"))
            for candidate in candidates
        )
    except Exception:
        accepted = False
    if not accepted:
        raise HTTPException(status_code=401, detail="Invalid or missing API key", headers={"WWW-Authenticate": "Bearer"})


# FastAPI routes have already captured the upstream dependency. Override that
# object explicitly so its static-key/admin-password/anonymous fallbacks cannot
# authorize a request even if a route is reached outside the public middleware.
app.dependency_overrides[require_api_key] = configured_api_key_dependency


@app.get("/__deployment/api-auth", include_in_schema=False, dependencies=[Depends(require_api_key)])
async def deployment_api_auth():
    """Local verification only; makes no Google request."""
    return Response(status_code=204)


class SecretRedaction(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        for value in (admin_password,):
            message = message.replace(value, "[REDACTED]")
        record.msg = message
        record.args = ()
        return True


for handler in logging.getLogger().handlers:
    handler.addFilter(SecretRedaction())

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app, host="127.0.0.1", port=8080, workers=1,
        log_level="warning", access_log=False, proxy_headers=False,
        limit_concurrency=16, timeout_keep_alive=2,
    )
