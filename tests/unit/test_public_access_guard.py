"""Offline behavior checks for the public guard, using only the standard library."""
import importlib.util
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

SPEC = importlib.util.spec_from_file_location(
    "public_access_guard",
    Path(__file__).resolve().parents[2] / "deploy" / "public_access_guard.py",
)
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


class PublicGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = SimpleNamespace(web_password="admin-" + "a" * 45, api_keys={"runtime-api"})
        self.configured_keys = {"configured-api"}
        self.now = 1000.0
        self.calls = 0
        self.received = 0

        async def app(scope, receive, send):
            self.calls += 1
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        self.middleware = guard.PublicAccessGuard(
            app, settings_provider=lambda: self.settings,
            api_keys_provider=lambda: self.configured_keys, clock=lambda: self.now,
        )

    async def request(self, path="/stats", *, token=None, headers=(), query=b"", client="203.0.113.2", method="GET", kind="http"):
        emitted = []
        request_headers = list(headers)
        if token is not None:
            request_headers.append((b"authorization", ("Bearer " + token).encode()))

        async def receive():
            self.received += 1
            raise AssertionError("The guard must not consume a body")

        async def send(message):
            emitted.append(message)

        await self.middleware({
            "type": kind, "path": path, "method": method,
            "query_string": query, "headers": request_headers,
            "client": (client, 12000),
        }, receive, send)
        return emitted[0]["status"], dict(emitted[0]["headers"]), emitted[-1].get("body", b"")

    async def test_management_requires_current_admin_bearer(self):
        self.assertEqual((await self.request(token="runtime-api"))[0], 401)
        self.assertEqual((await self.request(token="configured-api"))[0], 401)
        self.assertEqual((await self.request(token=self.settings.web_password))[0], 204)
        self.assertEqual((await self.request(headers=[(b"x-api-key", self.settings.web_password.encode())]))[0], 401)
        self.assertEqual((await self.request(token=self.settings.web_password, query=b"key=ignored"))[0], 401)
        self.assertEqual((await self.request(token=self.settings.web_password, query=b"API_KEY="))[0], 401)
        self.settings.web_password = "short"
        self.assertEqual((await self.request(token="short"))[0], 204)
        self.settings.web_password = ""
        self.assertEqual((await self.request(client="203.0.113.3"))[0], 401)
        self.assertEqual(self.received, 0)

    async def test_api_and_management_credentials_are_separate_even_if_configured_equal(self):
        self.settings.web_password = "chosen-short-value"
        self.settings.api_keys = {"chosen-short-value"}
        self.configured_keys.add("chosen-short-value")
        self.assertEqual((await self.request(token="chosen-short-value"))[0], 204)
        self.assertEqual((await self.request("/v1beta/models", token="chosen-short-value"))[0], 401)
        self.assertEqual((await self.request("/v1beta/models", token="configured-api"))[0], 204)
        self.assertEqual((await self.request(token="configured-api"))[0], 401)

    async def test_diagnostic_routes_require_management_bearer(self):
        for index, path in enumerate(("/errors", "/browser/status")):
            client = f"203.0.113.{20 + index}"
            self.assertEqual((await self.request(path, token=self.settings.web_password, client=client))[0], 204)
            self.assertEqual((await self.request(path, token="configured-api", client=client))[0], 401)
            self.assertEqual((await self.request(path, query=("key=" + self.settings.web_password).encode(), client=client))[0], 401)
            self.assertEqual((await self.request(path, headers=[(b"x-api-key", self.settings.web_password.encode())], client=client))[0], 401)

    async def test_per_ip_window_and_valid_token_cannot_be_locked(self):
        for _ in range(6):
            self.assertEqual((await self.request())[0], 401)
        status, headers, body = await self.request()
        self.assertEqual(status, 429)
        self.assertEqual(headers[b"retry-after"], b"900")
        self.assertEqual(json.loads(body)["error"]["status"], "RESOURCE_EXHAUSTED")
        self.assertEqual((await self.request(token=self.settings.web_password))[0], 204)
        self.assertEqual((await self.request("/v1beta/models", token="configured-api"))[0], 204)
        self.assertEqual((await self.request(client="203.0.113.3"))[0], 401)
        self.now += 900
        self.assertEqual((await self.request())[0], 401)

    async def test_api_keys_are_dynamic_and_static_startup_keys_are_rejected(self):
        self.assertEqual((await self.request("/v1beta/models", token="configured-api"))[0], 204)
        self.assertEqual((await self.request("/v1beta/models", token="runtime-api"))[0], 401)
        self.configured_keys = {"fresh-key"}
        self.assertEqual((await self.request("/v1beta/models", token="configured-api"))[0], 401)
        self.assertEqual((await self.request("/v1beta/models", token="fresh-key"))[0], 204)
        self.settings.api_keys = {"replacement-runtime"}
        self.assertEqual((await self.request("/v1beta/models", token="replacement-runtime"))[0], 401)
        self.assertEqual((await self.request("/v1beta/models", token=self.settings.web_password))[0], 401)
        self.assertEqual((await self.request("/__deployment/api-auth", token="fresh-key"))[0], 204)

    async def test_api_query_and_header_precedence_is_preserved(self):
        self.configured_keys = {"fresh-key"}
        self.assertEqual((await self.request("/v1beta/models", token="fresh-key", query=b"key=wrong"))[0], 401)
        self.assertEqual((await self.request("/v1beta/models", token="wrong", query=b"key=wrong&key=fresh-key"))[0], 204)
        self.assertEqual((await self.request("/v1beta/models", headers=[(b"x-goog-api-key", b"fresh-key"), (b"x-api-key", b"wrong")]))[0], 204)
        self.assertEqual((await self.request("/v1beta/models", headers=[(b"x-api-key", b"fresh-key")]))[0], 204)

    async def test_alias_model_paths_keep_api_authentication(self):
        # ASGI exposes the decoded path, including the selector's extra slash.
        # The public guard must authenticate aliases exactly like base models.
        selectors = ("gemini-offline", "抗截断/gemini-offline")
        suffixes = ("", ":generateContent", ":streamGenerateContent")
        for selector in selectors:
            for suffix in suffixes:
                path = f"/v1beta/models/{selector}{suffix}"
                method = "POST" if suffix else "GET"
                with self.subTest(path=path):
                    self.assertEqual((await self.request(path, method=method))[0], 401)
                    self.assertEqual((await self.request(
                        path, method=method, token="configured-api",
                    ))[0], 204)
                    self.assertEqual((await self.request(
                        path, method=method, token=self.settings.web_password,
                        client="203.0.113.3",
                    ))[0], 401)
        self.assertEqual(self.received, 0)

    async def test_empty_or_failed_api_configuration_fails_closed(self):
        self.configured_keys.clear()
        for token in (None, "runtime-api", self.settings.web_password):
            self.assertEqual((await self.request("/v1beta/models", token=token))[0], 401)
        self.assertEqual(self.calls, 0)

        def failed_config():
            raise ValueError("Configuration unavailable")

        self.middleware.api_keys_provider = failed_config
        self.assertEqual((await self.request("/v1beta/models", token="configured-api"))[0], 401)
        self.assertEqual(self.calls, 0)

    async def test_global_failure_window_does_not_lock_valid_authentication(self):
        self.middleware.failures = guard.FailureWindow(global_limit=2)
        self.assertEqual((await self.request(client="203.0.113.1"))[0], 401)
        self.assertEqual((await self.request(client="203.0.113.2"))[0], 401)
        self.assertEqual((await self.request(client="203.0.113.3"))[0], 429)
        self.assertEqual((await self.request(token=self.settings.web_password))[0], 204)
        self.now += 900
        self.assertEqual((await self.request())[0], 401)

    async def test_real_ip_is_trusted_only_from_loopback(self):
        for _ in range(6):
            self.assertEqual((await self.request(client="127.0.0.1", headers=[(b"x-real-ip", b"198.51.100.4")]))[0], 401)
        self.assertEqual((await self.request(client="127.0.0.1", headers=[(b"x-real-ip", b"198.51.100.4")]))[0], 429)
        self.assertEqual((await self.request(client="127.0.0.1", headers=[(b"x-real-ip", b"198.51.100.5")]))[0], 401)
        for _ in range(6):
            await self.request(client="198.51.100.8", headers=[(b"x-real-ip", b"198.51.100.9")])
        self.assertEqual((await self.request(client="198.51.100.8", headers=[(b"x-real-ip", b"198.51.100.10")]))[0], 429)

    async def test_static_spa_and_unrelated_protocols_pass_through(self):
        for path in ("/", "/login", "/settings", "/assets/index.js", "/auth/check"):
            self.assertEqual((await self.request(path))[0], 204)
        self.assertEqual((await self.request("/accounts", headers=[(b"accept", b"text/html")]))[0], 204)
        self.assertEqual((await self.request("/accounts"))[0], 401)
        self.assertEqual((await self.request("/api/accounts", headers=[(b"accept", b"text/html")]))[0], 401)
        self.assertEqual((await self.request("/any", kind="lifespan"))[0], 204)

    async def test_overlong_and_malformed_tokens_are_rejected_without_body_reads(self):
        self.assertEqual((await self.request(token="x" * 4097))[0], 401)
        self.assertEqual((await self.request(token="密码"))[0], 401)
        self.assertEqual((await self.request("/v1beta/models", query=b"key=" + b"x" * 17000))[0], 401)
        self.assertEqual((await self.request(token=self.settings.web_password, query=b"&".join([b"x=1"] * 65)))[0], 401)
        self.assertEqual(self.received, 0)

    async def test_sensitive_paths_are_protected_at_component_boundaries(self):
        paths = ("/stats/", "/rotation/accounts", "/config/yaml", "/api-keys/anything", "/accounts/import-cookies", "/api/accounts/import-bundle")
        for index, path in enumerate(paths, start=1):
            self.assertEqual((await self.request(path, client=f"198.51.100.{index}"))[0], 401)
        self.assertEqual((await self.request("/configuration"))[0], 204)

    def test_failure_state_is_bounded_and_expires(self):
        state = guard.FailureWindow(max_ip_entries=3, global_limit=1000)
        for i in range(10):
            state.record(str(i), 1000)
        self.assertEqual(len(state.by_ip), 3)
        self.assertEqual(len(state.global_failures), 10)
        state.retry_after("new", 1900)
        self.assertFalse(state.by_ip)
        self.assertFalse(state.global_failures)

    def test_dump_disable_patches_module_and_imported_references(self):
        logger = SimpleNamespace()
        app_module = SimpleNamespace()
        settings = SimpleNamespace(dump_requests=True, dump_to_file=True, dump_raw_response=True)
        guard.disable_request_dumps(logger_module=logger, app_module=app_module, settings=settings)
        logger.set_dump_requests(True)
        logger.set_dump_to_file(True)
        self.assertFalse(logger.is_dump_requests_enabled())
        self.assertFalse(logger.is_dump_to_file_enabled())
        self.assertFalse(app_module.is_dump_requests_enabled())
        self.assertIsNone(logger.dump_request_exchange(sensitive="must not be logged"))
        self.assertIsNone(app_module.dump_request_exchange(sensitive="must not be logged"))
        self.assertFalse(settings.dump_requests)
        self.assertFalse(settings.dump_to_file)
        self.assertFalse(settings.dump_raw_response)


if __name__ == "__main__":
    unittest.main(verbosity=2)
