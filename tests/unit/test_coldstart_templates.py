"""Synthetic template capture regressions without a browser or upstream calls."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from aistudio_api.domain.errors import AuthError, RequestError, SessionExpiredError
from aistudio_api.infrastructure.gateway import botguard_bootstrap
from aistudio_api.infrastructure.gateway.session import DEFAULT_BOOTSTRAP_TEMPLATE
from aistudio_api.infrastructure.gateway.wire_codec import (
    AistudioWireCodec,
    InvalidRequestTemplateError,
    modify_body,
)
from aistudio_api.infrastructure.utils import error_records, response_diagnostics


@pytest.mark.parametrize(
    "raw_body",
    [
        "[]",
        DEFAULT_BOOTSTRAP_TEMPLATE["body"],
        "{}",
        "null",
        "not json",
        "[null,[]]",
        '["",[]]',
        '["models/synthetic",null]',
        '["models/synthetic",{},null,[]]',
        '["models/synthetic",[],"invalid",[]]',
        '["models/synthetic",[],null,{}]',
    ],
)
def test_invalid_captured_templates_are_internal_unavailable_errors(raw_body):
    with pytest.raises(InvalidRequestTemplateError) as raised:
        AistudioWireCodec().decode(raw_body)

    assert raised.value.status == 503
    assert isinstance(raised.value, RequestError)
    assert not isinstance(raised.value, (ValueError, AuthError, IndexError))
    assert "list index out of range" not in str(raised.value)


@pytest.mark.parametrize("tail", [[], [None], [None, None], [None, []]])
def test_jspb_omitted_optional_fields_can_be_rewritten(tail):
    raw_body = json.dumps(["models/synthetic", [], *tail])
    decoded = AistudioWireCodec().decode(raw_body)
    assert decoded.safety_settings is None
    assert decoded.generation_config.values == []

    rewritten = json.loads(
        modify_body(raw_body, model="models/synthetic", prompt="synthetic prompt")
    )
    assert rewritten[0] == "models/synthetic"
    assert rewritten[1] == [[[[None, "synthetic prompt"]], "user"]]
    assert isinstance(rewritten[3], list)


def test_bad_template_error_does_not_expose_captured_values():
    raw_body = '["models/synthetic",[],null,"secret-synthetic-canary"]'
    with pytest.raises(InvalidRequestTemplateError) as raised:
        AistudioWireCodec().decode(raw_body)
    assert "secret-synthetic-canary" not in str(raised.value)


class SyntheticCapturePage:
    """Only the page operations needed by the bootstrap state machine."""

    url = "https://aistudio.google.com/prompts/new_chat"

    def __init__(self):
        self.callback = None
        self.unsubscribed = False
        self.fill = AsyncMock()
        self.wait_for_timeout = AsyncMock()
        self.wait_for_selector = AsyncMock()

    def on_request(self, callback):
        self.callback = callback

        def unsubscribe():
            self.unsubscribed = True

        return unsubscribe

    async def evaluate(self, source):
        return True if "?.service" in source else ""

    def is_closed(self):
        return False


def _stub_capture(monkeypatch, page, request_bodies):
    monkeypatch.setattr(
        botguard_bootstrap,
        "install_page_hooks",
        AsyncMock(return_value="synthetic-key"),
    )
    monkeypatch.setattr(
        botguard_bootstrap, "enable_temporary_chat", AsyncMock(return_value=True)
    )

    async def click_run(_page):
        for body in request_bodies:
            assert page.callback is not None
            page.callback(
                {
                    "url": "https://example.invalid/MakerSuiteService/GenerateContent",
                    "post_data": body,
                    "headers": {},
                }
            )
        return True

    monkeypatch.setattr(botguard_bootstrap, "click_run_button", click_run)

    async def virtual_capture_tick(captured, poll_seconds=1.0):
        if not captured.is_set():
            await page.wait_for_timeout(int(poll_seconds * 1000))

    monkeypatch.setattr(botguard_bootstrap, "_wait_for_template_capture", virtual_capture_tick)


@pytest.mark.asyncio
async def test_bootstrap_ignores_bad_body_then_accepts_valid_request(monkeypatch):
    page = SyntheticCapturePage()
    valid_body = '["models/synthetic",[]]'
    _stub_capture(monkeypatch, page, ["[]", valid_body])

    snap_key, captured = await botguard_bootstrap.bootstrap_botguard_and_template(
        page, max_wait_seconds=1
    )

    assert snap_key == "synthetic-key"
    assert captured["body"] == valid_body
    assert AistudioWireCodec().decode(captured["body"]).model == "models/synthetic"
    assert page.unsubscribed


async def test_ready_editor_and_captured_template_skip_fixed_navigation_and_poll_waits(monkeypatch):
    page = SyntheticCapturePage()
    _stub_capture(monkeypatch, page, ['["models/synthetic",[]]'])
    original_evaluate = page.evaluate
    sources = []

    async def evaluate(source):
        sources.append(source)
        if source == "() => !!document.querySelector('textarea')":
            return True
        return await original_evaluate(source)

    page.evaluate = evaluate
    _, captured = await botguard_bootstrap.bootstrap_botguard_and_template(page, max_wait_seconds=2)
    assert captured["body"] == '["models/synthetic",[]]'
    waits = [call.args[0] for call in page.wait_for_timeout.await_args_list]
    assert 800 not in waits and 1000 not in waits
    assert 600 in waits  # Preserve the framework's existing input-settle delay.
    assert not any("mat-card" in source for source in sources)


async def test_absent_editor_retains_navigation_fallback_and_readiness_gate(monkeypatch):
    page = SyntheticCapturePage()
    _stub_capture(monkeypatch, page, ['["models/synthetic",[]]'])
    await botguard_bootstrap.bootstrap_botguard_and_template(page, max_wait_seconds=2)
    page.wait_for_timeout.assert_any_await(800)
    page.wait_for_selector.assert_awaited_once_with("textarea", timeout_s=15.0)


async def test_captured_template_without_service_keeps_polling_instead_of_spinning(monkeypatch):
    page = SyntheticCapturePage()
    _stub_capture(monkeypatch, page, ['["models/synthetic",[]]'])
    checks = 0

    async def evaluate(source):
        nonlocal checks
        if "?.service" in source:
            checks += 1
            return checks >= 2
        return ""

    page.evaluate = evaluate
    await botguard_bootstrap.bootstrap_botguard_and_template(page, max_wait_seconds=3)
    assert checks == 2
    assert sum(call.args == (1000,) for call in page.wait_for_timeout.await_args_list) == 1


async def test_native_capture_wait_wakes_when_the_valid_template_arrives():
    captured = asyncio.Event()
    waiter = asyncio.create_task(botguard_bootstrap._wait_for_template_capture(captured, poll_seconds=60))
    await asyncio.sleep(0)
    captured.set()
    await asyncio.wait_for(waiter, timeout=1)


async def test_native_capture_wait_remains_bounded_when_no_template_arrives():
    captured = asyncio.Event()
    await botguard_bootstrap._wait_for_template_capture(captured, poll_seconds=0)
    assert not captured.is_set()


async def test_capture_wait_cancellation_stops_probe_restores_text_and_unsubscribes(monkeypatch):
    page = SyntheticCapturePage()
    _stub_capture(monkeypatch, page, [])
    waiting = asyncio.Event()

    async def blocked_capture_wait(captured, poll_seconds=1.0):
        waiting.set()
        await captured.wait()

    monkeypatch.setattr(botguard_bootstrap, "_wait_for_template_capture", blocked_capture_wait)
    evaluate = AsyncMock(side_effect=page.evaluate)
    page.evaluate = evaluate
    task = asyncio.create_task(botguard_bootstrap.bootstrap_botguard_and_template(page, max_wait_seconds=2))
    await asyncio.wait_for(waiting.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert page.unsubscribed
    evaluate.assert_any_await(botguard_bootstrap.STOP_GENERATION_JS)
    assert page.fill.await_args_list[-1].args == ("textarea", "")


async def test_preparation_milestones_retain_only_allowlisted_times(monkeypatch):
    page = SyntheticCapturePage()
    _stub_capture(monkeypatch, page, ['["models/synthetic",[]]'])
    store = response_diagnostics.ResponseDiagnosticStore()
    monkeypatch.setattr(response_diagnostics, "get_response_diagnostic_store", lambda: store)
    context = error_records.bind_request_context()
    try:
        response_diagnostics.initialize_response_diagnostics(stream=True)
        response_diagnostics.configure_generation(model="gemini-synthetic", auto_continue=False)
        await botguard_bootstrap.bootstrap_botguard_and_template(page, max_wait_seconds=2)
        response_diagnostics.finish_response_diagnostics("success", 200)
    finally:
        error_records.reset_request_context(context)
    item = store.list_records()["items"][0]
    assert set(item["timings_ms"]) == {"botguard_hooks_ready", "botguard_prompt_filled",
                                      "botguard_run_dispatched", "botguard_template_ready"}
    assert all(type(value) is int and value >= 0 for value in item["timings_ms"].values())
    encoded = json.dumps(item)
    assert "synthetic-key" not in encoded and "example.invalid" not in encoded


@pytest.mark.asyncio
async def test_bootstrap_with_only_bad_body_fails_without_expiring_login(monkeypatch):
    page = SyntheticCapturePage()
    _stub_capture(monkeypatch, page, ["[]", '["models/synthetic"]'])

    with pytest.raises(InvalidRequestTemplateError) as raised:
        await botguard_bootstrap.bootstrap_botguard_and_template(
            page, max_wait_seconds=1
        )

    assert raised.value.status == 503
    assert not isinstance(raised.value, AuthError)
    assert page.unsubscribed


def _stub_recovery_navigation(monkeypatch, page):
    async def navigate(_page, *, auth_user, auth_file):
        assert auth_file is None
        page.url = f"https://aistudio.google.com/u/{auth_user}/prompts/new_chat"

    navigation = AsyncMock(side_effect=navigate)
    monkeypatch.setattr(botguard_bootstrap, "navigate_to_aistudio", navigation)
    return navigation


@pytest.mark.asyncio
async def test_textarea_login_redirect_recovers_by_navigation_and_fresh_hooks(
    monkeypatch,
):
    page = SyntheticCapturePage()
    _stub_capture(monkeypatch, page, ['["models/synthetic",[]]'])
    navigation = _stub_recovery_navigation(monkeypatch, page)
    reload_cookies = AsyncMock(return_value=True)
    hooks = AsyncMock(side_effect=["old-document-key", "new-document-key"])
    monkeypatch.setattr(botguard_bootstrap, "install_page_hooks", hooks)
    selector_calls = 0

    async def wait_for_selector(*args, **kwargs):
        nonlocal selector_calls
        selector_calls += 1
        if selector_calls == 1:
            page.url = "https://accounts.google.com/ServiceLogin"
            raise TimeoutError("synthetic redirect")

    page.wait_for_selector = AsyncMock(side_effect=wait_for_selector)
    snap_key, captured = await botguard_bootstrap.bootstrap_botguard_and_template(
        page, reload_cookies_cb=reload_cookies, max_wait_seconds=1, auth_user="3"
    )

    assert snap_key == "new-document-key"
    assert captured["body"] == '["models/synthetic",[]]'
    assert hooks.await_count == 2
    reload_cookies.assert_awaited_once_with(page)
    navigation.assert_awaited_once_with(page, auth_user="3", auth_file=None)
    assert selector_calls == 3  # first document, recovery gate, fresh capture
    assert page.unsubscribed


@pytest.mark.asyncio
async def test_capture_login_redirect_discards_old_document_template(monkeypatch):
    page = SyntheticCapturePage()
    _stub_capture(monkeypatch, page, [])
    navigation = _stub_recovery_navigation(monkeypatch, page)
    reload_cookies = AsyncMock(return_value=True)
    probe_count = 0

    async def click_run(_page):
        nonlocal probe_count
        probe_count += 1
        page.callback(
            {
                "url": "https://example.invalid/GenerateContent",
                "post_data": json.dumps([f"models/document-{probe_count}", []]),
            }
        )
        if probe_count == 1:
            page.url = "https://accounts.google.com/ServiceLogin"
        return True

    monkeypatch.setattr(botguard_bootstrap, "click_run_button", click_run)
    _, captured = await botguard_bootstrap.bootstrap_botguard_and_template(
        page, reload_cookies_cb=reload_cookies, max_wait_seconds=1, auth_user="2"
    )

    assert json.loads(captured["body"])[0] == "models/document-2"
    assert probe_count == 2
    reload_cookies.assert_awaited_once_with(page)
    navigation.assert_awaited_once_with(page, auth_user="2", auth_file=None)


@pytest.mark.asyncio
async def test_login_recovery_is_bounded_when_redirect_returns(monkeypatch):
    page = SyntheticCapturePage()
    page.url = "https://accounts.google.com/ServiceLogin"
    _stub_capture(monkeypatch, page, [])
    navigation = _stub_recovery_navigation(monkeypatch, page)
    reload_cookies = AsyncMock(return_value=True)

    async def click_run(_page):
        page.url = "https://accounts.google.com/ServiceLogin"
        return True

    monkeypatch.setattr(botguard_bootstrap, "click_run_button", click_run)
    with pytest.raises(SessionExpiredError):
        await botguard_bootstrap.bootstrap_botguard_and_template(
            page, reload_cookies_cb=reload_cookies, max_wait_seconds=1, auth_user="1"
        )

    reload_cookies.assert_awaited_once_with(page)
    navigation.assert_awaited_once_with(page, auth_user="1", auth_file=None)


@pytest.mark.asyncio
async def test_injection_without_ready_studio_is_unavailable_not_expired(monkeypatch):
    page = SyntheticCapturePage()
    page.url = "https://accounts.google.com/ServiceLogin"
    _stub_capture(monkeypatch, page, [])
    _stub_recovery_navigation(monkeypatch, page)
    reload_cookies = AsyncMock(return_value=True)
    page.wait_for_selector = AsyncMock(side_effect=TimeoutError("synthetic UI delay"))

    with pytest.raises(InvalidRequestTemplateError) as raised:
        await botguard_bootstrap.bootstrap_botguard_and_template(
            page, reload_cookies_cb=reload_cookies, max_wait_seconds=1
        )

    assert raised.value.status == 503
    assert not isinstance(raised.value, AuthError)
    reload_cookies.assert_awaited_once_with(page)


@pytest.mark.asyncio
async def test_bootstrap_waits_for_delayed_template_before_stopping_probe(monkeypatch):
    page = SyntheticCapturePage()
    _stub_capture(monkeypatch, page, [])
    ticks = 0
    body_emitted = False
    stop_count = 0
    valid_body = '["models/delayed",[]]'

    async def wait_for_timeout(ms):
        nonlocal ticks, body_emitted
        if ms != 1000:
            return
        ticks += 1
        if ticks == 4:
            # The snapshot hook exists immediately but request construction
            # takes longer than the former 2.5-second grace period.
            body_emitted = True
            page.callback(
                {
                    "url": "https://example.invalid/GenerateContent",
                    "post_data": valid_body,
                }
            )

    async def evaluate(source):
        nonlocal stop_count
        if source == botguard_bootstrap.STOP_GENERATION_JS:
            assert body_emitted, "the probe must not be stopped before capture"
            stop_count += 1
        return True if "?.service" in source else ""

    page.wait_for_timeout = AsyncMock(side_effect=wait_for_timeout)
    page.evaluate = evaluate
    _, captured = await botguard_bootstrap.bootstrap_botguard_and_template(
        page, max_wait_seconds=5
    )

    assert captured["body"] == valid_body
    assert ticks == 4
    assert stop_count == 1


@pytest.mark.asyncio
async def test_template_deadline_stops_failed_probe_and_unsubscribes(monkeypatch):
    page = SyntheticCapturePage()
    _stub_capture(monkeypatch, page, [])
    evaluate = AsyncMock(side_effect=page.evaluate)
    page.evaluate = evaluate

    with pytest.raises(InvalidRequestTemplateError):
        await botguard_bootstrap.bootstrap_botguard_and_template(
            page, max_wait_seconds=3
        )

    assert (
        sum(call.args == (1000,) for call in page.wait_for_timeout.await_args_list) == 3
    )
    assert any(
        call.args == (botguard_bootstrap.STOP_GENERATION_JS,)
        for call in evaluate.await_args_list
    )
    assert page.unsubscribed
