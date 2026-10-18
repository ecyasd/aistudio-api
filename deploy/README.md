# Optional hardened Linux launcher

This fork includes the deployment middleware used with the reliability fixes.
It is opt-in: the regular CLI and Docker entrypoints retain upstream authentication
and startup behavior. To use the hardened launcher, run
`python deploy/secure_runner.py` from a checkout with the Python dependencies
installed.

The launcher expects systemd's `CREDENTIALS_DIRECTORY` to contain an `app-secrets`
JSON credential with an `admin_password` string. Use a separate, randomly generated
management password. Supply this through systemd `LoadCredential=`; keep the actual
credential outside the checkout. Set `AISTUDIO_CONFIG_FILE`, `AISTUDIO_DATA_DIR`,
`AISTUDIO_ACCOUNTS_DIR`, `AISTUDIO_STATS_FILE`, and `AISTUDIO_ROTATOR_STATE_FILE` to
writable persistent paths for the service user. Install the development test extras
only for validation.

The hardened launcher:

- Keeps management Bearer authentication separate from dynamic `config.yaml`
  API keys. An empty API key set denies API requests. Startup API key environment
  variables and the management password do not grant API access.
- Supports Gemini `key` query parameters and API key headers for inference.
- Limits failed authentication attempts per source and globally. Valid credentials
  remain usable during a failure cooldown.
- Disables request/response payload dumps even if a settings update enables them.
- Binds the backend and Chromium debugging to loopback and skips startup model
  probes. The first inference request starts browser/model initialization.
- Serves one worker, so recent error records and authentication limits share one
  in-memory process.

Put an HTTPS reverse proxy in front of backend port 8080. Keep backend port 8080
and browser debugging port 9222 private. Overwrite `X-Real-IP` with the proxy's TCP
peer address; never forward a client-supplied value. Keep access logs from recording
query strings containing API keys.

If the reverse proxy uses a route allowlist, allow the SPA routes
`/error-records` and `/error-records/`, and authenticated `GET /errors` and
`GET /browser/status` in addition to the existing application routes. The local
`/__deployment/api-auth` endpoint is an authenticated, inference-free diagnostic;
it does not need public exposure. A minimal public health response should contain
only status and busy state.

The real-time error page refreshes every five seconds while visible. Records live
only in memory for ten minutes, with a limit of 100 records, and disappear on
restart. Summaries omit raw exceptions, prompts, responses, cookies, and keys.
Browser idle/off status is normal before the first inference or after idle cleanup.
Cookie checkpointing preserves refreshed session state, but a Google-revoked or
expired login still requires an owner-authorized reimport.

Roll back code separately from data. Do not overwrite current configuration,
credentials, account state, or request statistics with an older code backup.

Offline middleware checks are in `tests/unit/test_public_access_guard.py` and
`tests/unit/test_error_records_integration.py`; they use synthetic credentials and
do not contact Google.
