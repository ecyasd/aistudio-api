"""Build code assistant RPC adapter, following AIS2 docs/build.md section 8.

Each request owns an empty applet; client history is transcribed in full. No
conversation or account credentials are added to the local diagnostic ledger.
"""

from __future__ import annotations

import asyncio
import codecs
import json
import re
import uuid
from collections.abc import AsyncGenerator
from contextlib import aclosing, suppress
from typing import TYPE_CHECKING

from aistudio_api.domain.errors import (
    RequestError,
    TransportError,
    classify_error,
    parse_upstream_grpc_error,
)
from aistudio_api.infrastructure.gateway.rpc_stream import RPCStreamDecoder
from aistudio_api.infrastructure.gateway.session import (
    DEFAULT_UPSTREAM_HEADERS,
    GENERATE_CONTENT_URL,
)
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart
from aistudio_api.infrastructure.utils.logger import get_logger

if TYPE_CHECKING:
    from aistudio_api.infrastructure.gateway.session import (
        GatewaySession,
    )

logger = get_logger("assistant")
WORKSPACE_TOOLS = [
    "run_command",
    "manage_task",
    "schedule",
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
    "view_file",
    "delete_file",
    "list_dir",
    "delete_dir",
    "move",
    "compile_applet",
    "lint_applet",
    "install_applet_dependencies",
    "install_applet_package",
    "restart_dev_server",
    "ask_question",
    "show_aistudio_ui",
    "set_up_firebase",
    "deploy_firebase",
    "rpc_action",
    "generate_image",
    "set_up_oauth",
    "remove_oauth",
    "search_web",
]
MODEL_TRANSCRIPT = "Your own earlier replies in this conversation appear in the user message between <assistant_turn> and </assistant_turn>. Continue from the latest content and never write these tags yourself."


def encode_history(contents: list[AistudioContent]) -> list[AistudioContent]:
    result = []
    for content in contents:
        parts = []
        for part in content.parts:
            if part.thought:
                continue
            if part.inline_data or part.file_id:
                raise RequestError(
                    400,
                    "Build code assistant currently accepts text and function history; use PG for attachments",
                )
            if part.function_call or part.function_response:
                value = part.function_call or part.function_response
                assert value
                kind = "function_calls" if part.function_call else "function_results"
                key = "arguments" if part.function_call else "response"
                parts.append(
                    AistudioPart(
                        text=f"```{kind}\n{json.dumps([{'name': value[0], key: value[1]}], ensure_ascii=False)}\n```"
                    )
                )
            elif part.text:
                parts.append(AistudioPart(text=part.text))
        if content.role == "model":
            parts = [
                AistudioPart(text="<assistant_turn>\n"),
                *parts,
                AistudioPart(text="\n</assistant_turn>"),
            ]
        if parts:
            result.append(AistudioContent(role="user", parts=parts))
    return result


def instructions(
    request: dict, system: AistudioContent | None
) -> tuple[str, list[dict]]:
    tools = request.get("tools") or []
    declarations = [
        item for tool in tools for item in tool.get("functionDeclarations", [])
    ]
    mode = (
        (request.get("toolConfig") or {}).get("functionCallingConfig", {}).get("mode")
    )
    if mode in ("NONE", "none", 3):
        declarations = []
    search = any(tool.get("googleSearch") is not None for tool in tools)
    forbidden = [
        name
        for name in WORKSPACE_TOOLS
        if not (search and name == "search_web")
        and not (declarations and name == "ask_question")
    ]
    text = (
        "Do not call these workspace tools; this empty app is unrelated to the user's task: "
        + ", ".join(forbidden)
        + "."
    )
    if search:
        text += " You may use search_web when the user's task requires web search."
    if declarations:
        text += (
            "\n\nYou can call the following client functions on the user's machine:\n"
            + json.dumps(declarations, ensure_ascii=False)
        )
        text += '\nTo call functions, write one fenced block:\n```function_calls\n[{"name":"<function name>","arguments":{}}]\n```\nThen call ask_question once with "Run these function calls and send me their results." and write nothing more. Never write function_results yourself.'
    text += "\n\n" + MODEL_TRANSCRIPT
    if system:
        text += "\n\n" + "\n".join(part.text for part in system.parts if part.text)
    return text, declarations


def thinking_level(request: dict) -> int:
    config = (request.get("generationConfig") or {}).get("thinkingConfig") or {}
    level = config.get("thinkingLevel")
    if level:
        return {"MINIMAL": 4, "LOW": 1, "MEDIUM": 2, "HIGH": 3}.get(
            str(level).upper(), 3
        )
    budget = config.get("thinkingBudget")
    return (
        3
        if budget is None or budget > 8192
        else 2
        if budget > 1024
        else 1
        if budget > 0
        else 4
    )


def parse_function_text(
    text: str, declarations: list[dict]
) -> tuple[str, list[dict], bool]:
    calls: list[dict] = []
    malformed = False
    allowed = {item.get("name") for item in declarations}

    def replace(match: re.Match[str]) -> str:
        nonlocal malformed
        kind, raw = match.group(1), match.group(2)
        if kind == "function_results":
            return ""
        try:
            value = json.loads(raw)
            values = value if isinstance(value, list) else [value]
            if not all(
                isinstance(item, dict) and item.get("name") in allowed
                for item in values
            ):
                raise ValueError
            calls.extend(
                {
                    "name": item["name"],
                    "args": item.get("arguments") or {},
                    "id": "call_" + uuid.uuid4().hex,
                }
                for item in values
            )
            return ""
        except (ValueError, TypeError):
            malformed = kind == "function_calls"
            return "" if malformed else match.group(0)

    return (
        re.sub(
            r"```(function_calls|function_results|function_call|tool_calls|tool_call|tool_code)\s*\n(.*?)\n```",
            replace,
            text,
            flags=re.S,
        ).strip(),
        calls,
        malformed,
    )


async def stream_assistant(
    session: GatewaySession,
    *,
    model: str,
    contents: list[AistudioContent],
    system: AistudioContent | None,
    request: dict,
) -> AsyncGenerator[tuple[str, object | None], None]:
    history = encode_history(contents)
    system_text, declarations = instructions(request, system)
    ctx = await session.ensure_ready()
    headers = dict(DEFAULT_UPSTREAM_HEADERS)
    applet = generation = ""
    finished = False
    cancelled_early = False

    async def rpc(method: str, body: list) -> list:
        status, raw = await session.send_request(
            url=GENERATE_CONTENT_URL.rsplit("/", 1)[0] + "/" + method,
            headers=headers,
            body=json.dumps(body, ensure_ascii=False),
            ctx=ctx,
        )
        if status != 200:
            raise classify_error(status, raw.decode("utf-8", errors="replace"))
        try:
            result = json.loads(raw.decode("utf-8").removeprefix(")]}'").strip())
            if not isinstance(result, list):
                raise ValueError
            if result and not isinstance(result[0], str):
                grpc_code, _message = parse_upstream_grpc_error(raw.decode("utf-8"))
                if grpc_code:
                    raise classify_error(502, raw.decode("utf-8"))
            return result
        except ValueError as exc:
            raise TransportError("Invalid Build assistant RPC response") from exc

    try:
        created = await rpc(
            "CreateApplet",
            [
                None,
                ["Untitled", "Generated by Gemini.", None, None, "default"],
                None,
                None,
                1,
            ],
        )
        if not created or not isinstance(created[0], str) or not created[0]:
            raise TransportError("Build assistant response missing applet ID")
        applet = created[0]
        proof = await session.generate_snapshot(
            history, ctx=ctx, model=model.removesuffix("-build")
        )
        headers.update(session.last_signer_headers)
        body: list = [None] * 29
        body[0], body[1], body[5], body[7], body[8], body[11] = (
            [item.to_wire() for item in history],
            proof,
            1,
            "models/" + model.removesuffix("-build").removeprefix("models/"),
            system_text,
            applet,
        )
        body[13], body[15], body[18], body[28] = (
            [2, None, 2],
            1,
            [1, None, None, thinking_level(request)],
            str(uuid.uuid4()).upper(),
        )
        started = await rpc("CodeAssistantOffline", body)
        if not started or not isinstance(started[0], str) or not started[0]:
            raise TransportError("Build assistant response missing generation ID")
        generation = started[0]
        yield "response_id", generation
        seen: dict[tuple[int, int], str] = {}
        usage: dict[int, tuple[int, int, int]] = {}
        text = ""
        emitted = 0
        stops = [
            value
            for value in (request.get("generationConfig") or {}).get(
                "stopSequences", []
            )
            if isinstance(value, str) and value
        ]
        hold = max((len(value) - 1 for value in stops), default=0)
        last_text_step: int | None = None
        completed_model = False
        for _resume in range(2):
            parser, decoder = (
                RPCStreamDecoder(),
                codecs.getincrementaldecoder("utf-8")(),
            )
            async with aclosing(
                session.send_streaming_request(
                    url=GENERATE_CONTENT_URL.rsplit("/", 1)[0]
                    + "/StreamCodeAssistantOfflineGeneration",
                    headers=headers,
                    body=json.dumps([generation, None, None, applet]),
                    ctx=ctx,
                )
            ) as events:
                status = 0
                async for event, payload in events:
                    if event == "status":
                        status = int(str(payload))
                        if status != 200:
                            raise classify_error(
                                status, "Build assistant task rejected"
                            )
                    elif event == "error":
                        raise TransportError("Build assistant transport interrupted")
                    elif event == "chunk" and payload:
                        chunk = (
                            decoder.decode(payload)
                            if isinstance(payload, bytes)
                            else str(payload)
                        )
                        for message in parser.feed(chunk):
                            steps = message[2] if len(message) > 2 else None
                            if not isinstance(steps, list):
                                continue
                            for step in steps:
                                if (
                                    not isinstance(step, list)
                                    or len(step) <= 12
                                    or type(step[11]) is not int
                                    or type(step[12]) is not int
                                ):
                                    raise TransportError("Invalid Build assistant step")
                                number, step_state = step[11], step[12]
                                if step_state == 4 or (len(step) > 5 and step[5]):
                                    raise TransportError(
                                        "Build assistant generation step failed"
                                    )
                                if (
                                    step_state == 1
                                    and len(step) > 15
                                    and isinstance(step[15], list)
                                ):
                                    values = (step[15] + [0, 0, 0])[:3]
                                    usage[number] = (
                                        int(values[0] or 0),
                                        int(values[1] or 0),
                                        int(values[2] or 0),
                                    )
                                has_model, has_body, part_index = False, False, 0
                                for turn in step[0] or []:
                                    if len(turn) < 2 or turn[1] != "model":
                                        continue
                                    has_model = True
                                    for part in turn[0]:
                                        if (
                                            not isinstance(part, list)
                                            or len(part) < 2
                                            or not isinstance(part[1], str)
                                        ):
                                            continue
                                        thought = len(part) > 12 and part[12] in (
                                            1,
                                            True,
                                        )
                                        value = part[1]
                                        key = number, part_index
                                        part_index += 1
                                        previous = seen.get(key, "")
                                        if not value.startswith(previous):
                                            raise TransportError(
                                                "Build assistant snapshot is not a continuation"
                                            )
                                        delta = value[len(previous) :]
                                        seen[key] = value
                                        if delta:
                                            if thought:
                                                yield "thinking", delta
                                            else:
                                                if (
                                                    last_text_step is not None
                                                    and last_text_step != number
                                                    and not previous
                                                ):
                                                    delta = "\n\n" + delta
                                                last_text_step = number
                                                text += delta
                                                positions = [
                                                    text.find(value)
                                                    for value in stops
                                                    if value in text
                                                ]
                                                if positions:
                                                    text = text[: min(positions)]
                                                    finished = cancelled_early = True
                                                if not declarations:
                                                    end = (
                                                        len(text)
                                                        if finished
                                                        else max(0, len(text) - hold)
                                                    )
                                                    if end > emitted:
                                                        yield "body", text[emitted:end]
                                                        emitted = end
                                        has_body = has_body or (
                                            bool(value) and not thought
                                        )
                                if has_model and step_state == 1:
                                    completed_model = True
                                action = step[9] if len(step) > 9 else None
                                dialog = (
                                    action[21]
                                    if isinstance(action, list) and len(action) > 21
                                    else None
                                )
                                if (
                                    step_state == 1
                                    and isinstance(dialog, list)
                                    and len(dialog) > 3
                                    and isinstance(dialog[3], list)
                                ):
                                    params = {
                                        pair[0]: pair[1]
                                        for pair in dialog[3]
                                        if isinstance(pair, list) and len(pair) > 1
                                    }
                                    if params.get("tag") == "ask_question":
                                        questions = json.loads(
                                            params.get("questions") or "[]"
                                        )
                                        lines = []
                                        for question in questions:
                                            value = str(question.get("question") or "")
                                            if (
                                                value
                                                == "Run these function calls and send me their results."
                                            ):
                                                continue
                                            lines.append(value)
                                            lines.extend(
                                                "- " + str(value)
                                                for value in question.get("options", [])
                                            )
                                        addition = "\n".join(lines)
                                        if addition:
                                            text += ("\n\n" if text else "") + addition
                                            if not declarations:
                                                yield "body", text[emitted:]
                                                emitted = len(text)
                                        if addition or declarations:
                                            finished = cancelled_early = True
                                if (
                                    step_state == 1
                                    and has_body
                                    and not (len(step) > 9 and step[9])
                                ):
                                    finished = True
                            if finished:
                                break
                        if finished:
                            break
            if finished:
                break
            parser.finish()
            if completed_model:
                finished = True
                break
        if not finished:
            raise TransportError(
                "Build assistant stream ended without a completed model reply"
            )
        malformed = False
        if not declarations and len(text) > emitted:
            yield "body", text[emitted:]
        if declarations:
            text, calls, malformed = parse_function_text(text, declarations)
            if text:
                yield "body", text
            if calls:
                yield "tool_calls", calls
        if not text and not declarations:
            raise TransportError("Build assistant completed without visible content")
        if usage:
            uncached, output, cached = (
                sum(item[index] for item in usage.values()) for index in range(3)
            )
            yield (
                "usage",
                {
                    "prompt_tokens": uncached + cached,
                    "completion_tokens": output,
                    "cached_tokens": cached,
                    "total_tokens": uncached + cached + output,
                },
            )
        yield "finish_reason", 10 if malformed else 1
        yield "done", None
    finally:
        # Each applet belongs only to this request, so it can be removed immediately.
        async def cleanup() -> None:
            if generation and (not finished or cancelled_early):
                with suppress(Exception):
                    await rpc(
                        "CancelCodeAssistantOfflineGeneration", [generation, applet]
                    )
            if applet:
                try:
                    await rpc("DeleteApplet", [applet])
                except Exception:
                    logger.warning("Build assistant applet cleanup failed")

        task = asyncio.create_task(cleanup())
        try:
            with suppress(TimeoutError):
                async with asyncio.timeout(15):
                    await asyncio.shield(task)
        finally:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task
