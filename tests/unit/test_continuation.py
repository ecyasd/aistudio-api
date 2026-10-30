"""Offline contract coverage for bounded, opt-in MAX_TOKENS continuation."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from copy import deepcopy
from types import SimpleNamespace

import pytest

from aistudio_api.application import chat_service, continuation
from aistudio_api.application.chat_service import NormalizedGeminiRequest
from aistudio_api.domain.errors import RequestError
from aistudio_api.domain.models import (
    Candidate,
    ContentPart,
    GeneratedImage,
    ModelOutput,
)
from aistudio_api.infrastructure.gateway.model_defaults import ModelDefaults
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart


def request(**overrides):
    values = {
        "model": "models/plain-text",
        "contents": [AistudioContent("user", [AistudioPart(text="question")])],
        "system_instruction": None,
        "tools": None,
        "safety_settings": None,
        "capture_prompt": "question",
        "capture_images": None,
        "max_tokens": 10,
    }
    values.update(overrides)
    return NormalizedGeminiRequest(**values)


def output(text="A", finish=2, *, usage=None, **candidate):
    return ModelOutput(
        candidates=[
            Candidate(
                text=text,
                finish_reason=finish,
                parts=[ContentPart(text=text)],
                **candidate,
            )
        ],
        model="models/plain-text",
        response_id=f"response-{text}",
        usage=usage or {},
        raw_response="synthetic raw response",
    )


def events(text="A", finish=2, *, usage=None):
    return [
        ("response_id", f"response-{text}"),
        ("parts", [ContentPart(text=text)]),
        ("finish_reason", finish),
        ("usage", usage),
        ("done", None),
    ]


class FakeClient:
    def __init__(self, rounds, *, delays=None):
        self.rounds = rounds
        self.delays = delays or []
        self.calls = []
        self.closed = []

    async def generate_content(self, **kwargs):
        index = len(self.calls)
        self.calls.append(kwargs)
        if index < len(self.delays):
            await asyncio.sleep(self.delays[index])
        value = self.rounds[index]
        if isinstance(value, Exception):
            raise value
        return value

    async def stream_generate_content(self, **kwargs):
        index = len(self.calls)
        self.calls.append(kwargs)
        try:
            if index < len(self.delays):
                await asyncio.sleep(self.delays[index])
            value = self.rounds[index]
            if isinstance(value, Exception):
                raise value
            for event in value:
                if isinstance(event, Exception):
                    raise event
                yield event
        finally:
            self.closed.append(index)


async def stream(client, normalized=None, **kwargs):
    return [
        event
        async for event in continuation.stream_generate_content(
            client, normalized or request(), **kwargs
        )
    ]


@pytest.fixture(autouse=True)
def plain_model_defaults(monkeypatch):
    monkeypatch.setattr(
        continuation, "resolve_model_defaults", lambda _model: ModelDefaults()
    )


async def test_disabled_is_one_unchanged_call_with_all_normalized_arguments():
    normalized = request(
        temperature=0.5,
        top_p=0.8,
        top_k=20,
        tools=[["synthetic tool"]],
        tool_config=[1],
        safety_settings=[[1, 2]],
        generation_config_overrides={"response_schema": [6]},
    )
    result = output()
    client = FakeClient([result])
    assert await continuation.generate_content(client, normalized) is result
    assert len(client.calls) == 1
    assert client.calls[0] == {
        "model": normalized.model,
        "capture_prompt": normalized.capture_prompt,
        "capture_images": normalized.capture_images,
        "contents": normalized.contents,
        "system_instruction_content": normalized.system_instruction,
        "tools": normalized.tools,
        "tool_config": normalized.tool_config,
        "safety_settings": normalized.safety_settings,
        "temperature": normalized.temperature,
        "top_p": normalized.top_p,
        "top_k": normalized.top_k,
        "max_tokens": normalized.max_tokens,
        "generation_config_overrides": normalized.generation_config_overrides,
        "sanitize_plain_text": False,
    }


async def test_disabled_stream_preserves_every_event_and_refresh_flag():
    incomplete = [("parts", [ContentPart(text="unfinished")])]
    client = FakeClient([incomplete])
    assert await stream(client, force_refresh_capture=True) == incomplete
    assert client.calls[0]["force_refresh_capture"] is True
    assert client.closed == [0]


async def test_disabled_never_checks_model_defaults(monkeypatch):
    def forbidden(_model):
        raise AssertionError("disabled continuation must not inspect eligibility")

    monkeypatch.setattr(continuation, "resolve_model_defaults", forbidden)
    client = FakeClient([output()])
    await continuation.generate_content(client, request())
    await stream(FakeClient([events()]))


async def test_exact_text_history_same_client_two_extra_limit_and_no_mutation():
    normalized = request(
        system_instruction=AistudioContent("system", [AistudioPart(text="be precise")])
    )
    original = deepcopy(normalized)
    rounds = [output("A "), output(" A\n"), output("end", 1)]
    originals = deepcopy(rounds)
    client = FakeClient(rounds)
    result = await continuation.generate_content(client, normalized, auto_continue=True)
    assert result.text == "A  A\nend"
    assert result.candidates[0].finish_reason == 1
    assert [part.text for part in result.candidates[0].parts] == ["A ", " A\n", "end"]
    assert result.response_id == "response-end"
    assert len(client.calls) == 3
    assert normalized == original
    assert rounds == originals
    for index, text in ((1, "A "), (2, " A\n")):
        call = client.calls[index]
        assert call["max_tokens"] == 10  # Per-round cap, unchanged.
        assert call["contents"][: len(original.contents)] == original.contents
        assert len(call["contents"]) == len(original.contents) + 2 * index
        assert call["contents"][0] is not normalized.contents[0]
        assert call["contents"][-2] == AistudioContent(
            "model", [AistudioPart(text=text)]
        )
        assert call["contents"][-1] == AistudioContent(
            "user", [AistudioPart(text=continuation.CONTINUE_PROMPT)]
        )


async def test_still_max_tokens_after_third_round_is_never_falsified_to_stop():
    client = FakeClient([output("1"), output("2"), output("3"), output("forbidden")])
    result = await continuation.generate_content(client, request(), auto_continue=True)
    assert (result.text, result.candidates[0].finish_reason) == ("123", 2)
    assert len(client.calls) == 3


@pytest.mark.parametrize("finish", [1, 3, 4, 5, 6, 7, None, 0, "MAX_TOKENS", True])
async def test_only_real_numeric_max_tokens_can_continue(finish):
    result = output(finish=finish)
    client = FakeClient([result])
    assert (
        await continuation.generate_content(client, request(), auto_continue=True)
        is result
    )
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"capture_images": [("image/png", "synthetic")]},
        {"generation_config_overrides": {"response_mime_type": "application/json"}},
        {"generation_config_overrides": {"response_schema": []}},
        {"generation_config_overrides": {"image_output_mode": [2]}},
        {"generation_config_overrides": {"output_resolution": ["1:1", "1K"]}},
    ],
)
async def test_request_images_and_structured_output_skip_continuation(overrides):
    client = FakeClient([output()])
    await continuation.generate_content(
        client, request(**overrides), auto_continue=True
    )
    streaming = FakeClient([events()])
    assert await stream(streaming, request(**overrides), auto_continue=True) == events()
    assert len(client.calls) == len(streaming.calls) == 1


@pytest.mark.parametrize(
    "part",
    [
        AistudioPart(inline_data=("image/png", "synthetic")),
        AistudioPart(file_id="synthetic"),
        AistudioPart(function_call=("f", {})),
        AistudioPart(function_response=("f", {})),
        AistudioPart(executable_code=("PYTHON", "pass")),
        AistudioPart(code_execution_result=("OUTCOME_OK", "")),
    ],
)
@pytest.mark.parametrize("system", [False, True])
async def test_multimodal_history_and_system_are_excluded(part, system):
    normalized = request()
    content = AistudioContent("user", [part])
    if system:
        normalized.system_instruction = content
    else:
        normalized.contents.append(content)
    client = FakeClient([output()])
    await continuation.generate_content(client, normalized, auto_continue=True)
    assert len(client.calls) == 1


async def test_default_tools_are_preserved_and_do_not_disable_text_continuation(
    monkeypatch,
):
    from aistudio_api.api.schemas import GeminiGenerateContentRequest

    monkeypatch.setattr(
        chat_service,
        "resolve_model_defaults",
        lambda _model: ModelDefaults(default_tools=("google_search",)),
    )
    normalized = chat_service.normalize_gemini_request(
        GeminiGenerateContentRequest.model_validate(
            {"contents": [{"parts": [{"text": "question"}]}]}
        ),
        "gemini-3.7-flash",
    )
    assert normalized.tools
    client = FakeClient([output(), output("B", 1)])
    await continuation.generate_content(client, normalized, auto_continue=True)
    assert len(client.calls) == 2
    assert all(call["tools"] == normalized.tools for call in client.calls)


async def test_image_model_defaults_are_excluded(monkeypatch):
    monkeypatch.setattr(
        continuation,
        "resolve_model_defaults",
        lambda _model: ModelDefaults(is_image_model=True),
    )
    client = FakeClient([output()])
    await continuation.generate_content(client, request(), auto_continue=True)
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "fields",
    [
        {"thinking": "private thought"},
        {"images": [GeneratedImage("image/png", b"synthetic", 9)]},
        {"reasoning_images": [GeneratedImage("image/png", b"synthetic", 9)]},
        {"function_calls": [{"name": "f"}]},
        {"function_responses": [{"name": "f"}]},
        {"thought_signature": "synthetic"},
        {"sources": [{"uri": "https://example.invalid"}]},
        {"code_output": "1"},
    ],
)
async def test_non_plain_candidate_content_is_never_continued(fields):
    client = FakeClient([output(**fields)])
    await continuation.generate_content(client, request(), auto_continue=True)
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "part",
    [
        ContentPart(text="A", thought=True),
        ContentPart(image=GeneratedImage("image/png", b"synthetic", 9)),
        ContentPart(function_call={"name": "f"}),
        ContentPart(function_response={"name": "f"}),
        ContentPart(text=""),
    ],
)
async def test_non_plain_parts_preserved_and_not_continued(part):
    result = output()
    result.candidates[0].parts = [part]
    client = FakeClient([result])
    assert (
        await continuation.generate_content(client, request(), auto_continue=True)
        is result
    )
    assert len(client.calls) == 1
    round_events = events()
    round_events[1] = ("parts", [part])
    streaming = FakeClient([round_events])
    received = await stream(streaming, auto_continue=True)
    assert received[1] == ("parts", [part])
    assert len(streaming.calls) == 1


async def test_empty_and_multiple_candidates_do_not_continue():
    for result in (
        output(""),
        ModelOutput(),
        ModelOutput(candidates=[Candidate(), Candidate()]),
    ):
        client = FakeClient([result])
        assert (
            await continuation.generate_content(client, request(), auto_continue=True)
            is result
        )
        assert len(client.calls) == 1


def usage(prompt, visible, reasoning):
    return {
        "prompt_tokens": prompt,
        "completion_tokens": visible + reasoning,
        "total_tokens": prompt + visible + reasoning,
        "cached_tokens": 1,
        "prompt_tokens_details": {"text_tokens": prompt, "cache": {"hits": 1}},
        "completion_tokens_details": {
            "visible_tokens": visible,
            "reasoning_tokens": reasoning,
        },
    }


async def test_usage_aggregates_every_round_including_reasoning_and_full_prompts():
    usages = [usage(10, 2, 1), usage(15, 3, 2), usage(20, 4, 3)]
    original = deepcopy(usages)
    expected = {
        "prompt_tokens": 45,
        "completion_tokens": 15,
        "total_tokens": 60,
        "cached_tokens": 3,
        "prompt_tokens_details": {"text_tokens": 45, "cache": {"hits": 3}},
        "completion_tokens_details": {"visible_tokens": 9, "reasoning_tokens": 6},
    }
    client = FakeClient([output(str(i), usage=value) for i, value in enumerate(usages)])
    result = await continuation.generate_content(client, request(), auto_continue=True)
    assert result.usage == expected
    streaming = FakeClient(
        [events(str(i), usage=value) for i, value in enumerate(usages)]
    )
    received = await stream(streaming, auto_continue=True)
    assert [item for item in received if item[0] == "usage"] == [("usage", expected)]
    assert [item for item in received if item[0] == "finish_reason"] == [
        ("finish_reason", 2)
    ]
    assert [item for item in received if item[0] == "done"] == [("done", None)]
    assert usages == original


async def test_stream_preserves_parts_ids_exact_history_and_first_refresh_only():
    normalized = request()
    original = deepcopy(normalized)
    rounds = [events("a "), events(" a\n"), events("end", 1)]
    client = FakeClient(rounds)
    received = await stream(
        client, normalized, auto_continue=True, force_refresh_capture=True
    )
    assert [event for event in received if event[0] in {"parts", "response_id"}] == [
        event
        for items in rounds
        for event in items
        if event[0] in {"parts", "response_id"}
    ]
    assert received[-3:] == [("finish_reason", 1), ("usage", None), ("done", None)]
    assert [call["force_refresh_capture"] for call in client.calls] == [
        True,
        False,
        False,
    ]
    assert client.calls[2]["contents"][-2].parts[0].text == " a\n"
    assert len(client.calls[2]["contents"]) == len(normalized.contents) + 4
    assert normalized == original
    assert client.closed == [0, 1, 2]


@pytest.mark.parametrize("finish", [1, 3, 4, 5, 6])
async def test_stream_stop_and_safety_are_not_continued(finish):
    client = FakeClient([events(finish=finish)])
    received = await stream(client, auto_continue=True)
    assert received[-3:] == [("finish_reason", finish), ("usage", None), ("done", None)]
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "final_events",
    [
        [],
        [("done", None)],
        [("finish_reason", 1)],
        [("finish_reason", 0), ("done", None)],
        [("finish_reason", "MAX_TOKENS"), ("done", None)],
    ],
)
async def test_enabled_stream_does_not_fabricate_success_for_incomplete_round(
    final_events,
):
    client = FakeClient([[("parts", [ContentPart(text="partial")]), *final_events]])
    received = []
    with pytest.raises(RequestError, match="completion marker"):
        async for event in continuation.stream_generate_content(
            client, request(), auto_continue=True
        ):
            received.append(event)
    assert received == [("parts", [ContentPart(text="partial")])]
    assert len(client.calls) == 1
    assert client.closed == [0]


async def test_nonstream_later_error_exposes_original_and_does_not_retry():
    failure = RequestError(429, "synthetic")
    client = FakeClient([output(), failure, output("forbidden")])
    with pytest.raises(continuation.ContinuationError) as caught:
        await continuation.generate_content(client, request(), auto_continue=True)
    assert caught.value.original_error is failure
    assert len(client.calls) == 2


async def test_first_error_retains_original_type():
    failure = RequestError(429, "synthetic")
    client = FakeClient([failure])
    with pytest.raises(RequestError) as caught:
        await continuation.generate_content(client, request(), auto_continue=True)
    assert caught.value is failure
    assert len(client.calls) == 1


async def test_stream_later_failure_closes_round_and_never_emits_false_terminal_events():
    failure = RequestError(429, "synthetic")
    client = FakeClient([events(), [("parts", [ContentPart(text="partial")]), failure]])
    received = []
    with pytest.raises(RequestError) as caught:
        async for event in continuation.stream_generate_content(
            client, request(), auto_continue=True
        ):
            received.append(event)
    assert caught.value is failure
    assert [item[1][0].text for item in received if item[0] == "parts"] == [
        "A",
        "partial",
    ]
    assert not {item[0] for item in received}.intersection(
        {"finish_reason", "usage", "done"}
    )
    assert client.closed == [0, 1]
    assert len(client.calls) == 2


@pytest.mark.parametrize("enabled", [False, True])
async def test_stream_consumer_close_always_closes_upstream(enabled):
    client = FakeClient([events()])
    async with aclosing(
        continuation.stream_generate_content(client, request(), auto_continue=enabled)
    ) as upstream:
        assert await anext(upstream) == ("response_id", "response-A")
    assert client.closed == [0]
    assert len(client.calls) == 1


@pytest.mark.parametrize("enabled", [False, True])
async def test_stream_cancellation_closes_active_upstream_without_next_round(enabled):
    entered = asyncio.Event()
    closed = asyncio.Event()

    class BlockingClient:
        async def stream_generate_content(self, **kwargs):
            try:
                yield "parts", [ContentPart(text="partial")]
                entered.set()
                await asyncio.Event().wait()
            finally:
                closed.set()

    task = asyncio.create_task(stream(BlockingClient(), auto_continue=enabled))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


async def test_stream_cancellation_during_later_round_closes_it_without_replay():
    client = FakeClient([events(), events("B", 1)], delays=[0, 10])
    task = asyncio.create_task(stream(client, auto_continue=True))
    while len(client.calls) < 2:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(client.calls) == 2
    assert client.closed == [0, 1]


async def test_stream_consumer_disconnect_during_later_round_closes_it():
    client = FakeClient([events(), events("B"), events("forbidden")])
    async with aclosing(
        continuation.stream_generate_content(client, request(), auto_continue=True)
    ) as upstream:
        async for event in upstream:
            if event == ("response_id", "response-B"):
                break
    assert len(client.calls) == 2
    assert client.closed == [0, 1]


@pytest.mark.parametrize("text", ["12345", "界界"])
async def test_output_byte_cap_stops_future_calls_but_keeps_genuine_response(
    monkeypatch, text
):
    monkeypatch.setattr(
        continuation, "MAX_CONTINUATION_BYTES", continuation.PART_OVERHEAD_BYTES + 4
    )
    client = FakeClient([output(text)])
    result = await continuation.generate_content(client, request(), auto_continue=True)
    assert (result.text, result.candidates[0].finish_reason) == (text, 2)
    streaming = FakeClient([events(text)])
    received = await stream(streaming, auto_continue=True)
    assert received[1] == ("parts", [ContentPart(text=text)])
    assert received[-3:] == [("finish_reason", 2), ("usage", None), ("done", None)]
    assert len(client.calls) == len(streaming.calls) == 1


async def test_accumulated_stream_cap_stops_third_round_without_losing_chunks(
    monkeypatch,
):
    monkeypatch.setattr(
        continuation, "MAX_CONTINUATION_BYTES", 2 * continuation.PART_OVERHEAD_BYTES + 3
    )
    client = FakeClient([events("ab"), events("cd"), events("forbidden")])
    received = await stream(client, auto_continue=True)
    assert [item[1][0].text for item in received if item[0] == "parts"] == ["ab", "cd"]
    assert received[-3] == ("finish_reason", 2)
    assert len(client.calls) == 2


async def test_accumulated_nonstream_cap_keeps_second_response_and_stops_third(
    monkeypatch,
):
    monkeypatch.setattr(
        continuation, "MAX_CONTINUATION_BYTES", 2 * continuation.PART_OVERHEAD_BYTES + 3
    )
    client = FakeClient([output("ab"), output("cd"), output("forbidden")])
    result = await continuation.generate_content(client, request(), auto_continue=True)
    assert result.text == "abcd"
    assert result.candidates[0].finish_reason == 2
    assert len(client.calls) == 2


async def test_oversized_original_history_skips_continuation_without_copying(
    monkeypatch,
):
    monkeypatch.setattr(continuation, "MAX_HISTORY_BYTES", 1024)
    normalized = request(
        contents=[AistudioContent("user", [AistudioPart(text="x" * 1024)])]
    )
    client = FakeClient([output()])
    await continuation.generate_content(client, normalized, auto_continue=True)
    assert len(client.calls) == 1
    assert client.calls[0]["contents"] is normalized.contents


async def test_long_chinese_signed_history_above_one_mib_can_continue():
    normalized = request(contents=[AistudioContent("user", [AistudioPart(
        text="合成上下文" * 80_000, thought_signature="synthetic-signature",
    )])])
    client = FakeClient([output("first", finish=2), output("second", finish=1)])
    result = await continuation.generate_content(client, normalized, auto_continue=True)
    assert len(client.calls) == 2
    assert result.text == "firstsecond"
    assert client.calls[1]["contents"][0].parts[0] == normalized.contents[0].parts[0]
    assert len(normalized.contents) == 1


async def test_total_nonstream_deadline_includes_all_rounds(monkeypatch):
    monkeypatch.setattr(continuation, "settings", SimpleNamespace(timeout_stream=60))
    second_started = asyncio.Event()
    timeouts = []

    def timeout_factory(delay):
        context = asyncio.Timeout(asyncio.get_running_loop().time() + 60)
        timeouts.append((delay, context))
        return context

    class GatedClient(FakeClient):
        async def generate_content(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return output()
            second_started.set()
            await asyncio.Event().wait()
            raise AssertionError("the shared deadline must cancel round two")

    monkeypatch.setattr(continuation.asyncio, "timeout", timeout_factory)
    client = GatedClient([])
    task = asyncio.create_task(
        continuation.generate_content(client, request(), auto_continue=True)
    )
    await asyncio.wait_for(second_started.wait(), 10)
    assert len(timeouts) == 1
    assert timeouts[0][0] == 60
    timeouts[0][1].reschedule(asyncio.get_running_loop().time() - 1)
    with pytest.raises(continuation.ContinuationError) as caught:
        await task
    assert isinstance(caught.value.original_error, RequestError)
    assert caught.value.original_error.status == 504
    assert "time budget" in str(caught.value.original_error)
    assert len(client.calls) == 2


async def test_first_round_timeout_is_fixed_504_and_can_be_classified_normally(
    monkeypatch,
):
    monkeypatch.setattr(continuation, "settings", SimpleNamespace(timeout_stream=0.01))
    client = FakeClient([output()], delays=[10])
    with pytest.raises(RequestError, match="time budget") as caught:
        await continuation.generate_content(client, request(), auto_continue=True)
    assert caught.value.status == 504
    assert len(client.calls) == 1


async def test_total_stream_deadline_closes_active_round_and_emits_no_success(
    monkeypatch,
):
    monkeypatch.setattr(continuation, "settings", SimpleNamespace(timeout_stream=60))
    second_started = asyncio.Event()
    timeouts = []

    def timeout_factory(deadline):
        context = asyncio.Timeout(asyncio.get_running_loop().time() + 60)
        timeouts.append((deadline, context))
        return context

    class GatedClient(FakeClient):
        async def stream_generate_content(self, **kwargs):
            index = len(self.calls)
            self.calls.append(kwargs)
            try:
                if index == 0:
                    for event in events():
                        yield event
                else:
                    second_started.set()
                    await asyncio.Event().wait()
                    raise AssertionError("the shared deadline must cancel round two")
            finally:
                self.closed.append(index)

    monkeypatch.setattr(continuation.asyncio, "timeout_at", timeout_factory)
    client = GatedClient([])
    received = []

    async def consume():
        async for event in continuation.stream_generate_content(
            client, request(), auto_continue=True
        ):
            received.append(event)

    task = asyncio.create_task(consume())
    await asyncio.wait_for(second_started.wait(), 10)
    assert len(timeouts) > 1
    assert len({deadline for deadline, _context in timeouts}) == 1
    timeouts[-1][1].reschedule(asyncio.get_running_loop().time() - 1)
    with pytest.raises(RequestError, match="time budget") as caught:
        await task
    assert caught.value.status == 504
    assert client.closed == [0, 1]
    assert len(client.calls) == 2
    assert not {item[0] for item in received}.intersection(
        {"finish_reason", "usage", "done"}
    )


async def test_stream_deadline_does_not_cancel_suspended_consumers_work(monkeypatch):
    monkeypatch.setattr(continuation, "settings", SimpleNamespace(timeout_stream=0.01))
    client = FakeClient([events()])
    task = asyncio.current_task()
    assert task is not None
    prior_cancellations = task.cancelling()
    async with aclosing(
        continuation.stream_generate_content(client, request(), auto_continue=True)
    ) as upstream:
        assert await anext(upstream) == ("response_id", "response-A")
        # Consumer work is outside the generator and must not be cancelled by
        # its deadline, even while upstream is held open for this request.
        await asyncio.sleep(0.03)
        assert task.cancelling() == prior_cancellations
        assert not client.closed
        # FakeClient's next event is immediately available; expiry must still
        # reject it rather than depend on timeout_at getting an event-loop turn.
        with pytest.raises(RequestError, match="time budget") as caught:
            await anext(upstream)
        assert caught.value.status == 504
    assert len(client.calls) == 1
    assert client.closed == [0]
    assert task.cancelling() == prior_cancellations


async def test_nonstream_cancellation_remains_cancelled_error():
    client = FakeClient([output(), output("B", 1)], delays=[0, 10])
    task = asyncio.create_task(
        continuation.generate_content(client, request(), auto_continue=True)
    )
    while len(client.calls) < 2:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(client.calls) == 2
