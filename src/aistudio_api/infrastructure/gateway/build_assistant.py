"""Text-only Build code assistant, retaining Google's own system instructions.

Protocol adapted from Mag1cFall/AIStudio2API (MIT), assistant.go.
Each request owns its temporary applet; no saved conversation or hidden retry.
"""

from __future__ import annotations

import asyncio
import codecs
import json
import uuid
from collections.abc import AsyncGenerator
from contextlib import aclosing
from copy import deepcopy

from aistudio_api.domain.errors import (
    IncompleteResponseError,
    RequestError,
    classify_error,
)
from aistudio_api.infrastructure.gateway.native_bridge import NativeBridge
from aistudio_api.infrastructure.gateway.stream_parser import (
    IncrementalJSONStreamParser,
)
from aistudio_api.infrastructure.gateway.wire_spec import (
    GenerationConfigIndex,
    PartIndex,
    TopLevelIndex,
)
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart

WORKSPACE_RULE = (
    "Reply in the conversation. Do not call workspace or application tools, including "
    "read_file, write_file, edit_file, delete_file, list_files, run_command, ask_question, or search_web. "
    "Messages enclosed in <assistant_turn> are earlier assistant replies supplied as conversation history.\n\n"
)


def rewrite_history(contents: list[AistudioContent]) -> list[AistudioContent]:
    result = []
    for content in contents:
        parts = []
        for part in content.parts:
            if any((part.inline_data, part.file_id, part.function_call, part.function_response,
                    part.executable_code, part.code_execution_result)):
                raise RequestError(400, "Build assistant currently supports text conversations only")
            if content.role == "model" and part.thought:
                continue
            if part.text is not None:
                parts.append(AistudioPart(text=part.text))
        if content.role == "model" and parts:
            parts = [AistudioPart(text="<assistant_turn>\n" + "".join(p.text or "" for p in parts) + "\n</assistant_turn>")]
        if parts:
            result.append(AistudioContent(parts=parts, role="user"))
    if not result:
        raise RequestError(400, "Build assistant requires text contents")
    return result


def assistant_body(request: list, applet: object) -> list:
    if len(request) > TopLevelIndex.TOOLS and request[TopLevelIndex.TOOLS]:
        raise RequestError(400, "Build assistant text channel does not support client tools")
    body: list = [None] * 29
    body[0], body[1] = request[TopLevelIndex.CONTENTS], request[TopLevelIndex.SNAPSHOT]
    body[5] = body[15] = 1
    body[7] = str(request[TopLevelIndex.MODEL]).removesuffix("-build")
    system = request[TopLevelIndex.SYSTEM_INSTRUCTION] if len(request) > TopLevelIndex.SYSTEM_INSTRUCTION else None
    system_text = ""
    if isinstance(system, list) and system and isinstance(system[0], list):
        system_text = "\n".join(str(p[PartIndex.TEXT]) for p in system[0]
                                if isinstance(p, list) and len(p) > PartIndex.TEXT and isinstance(p[PartIndex.TEXT], str))
    body[8] = WORKSPACE_RULE + system_text
    body[11], body[13] = applet, [2, None, 2]
    level = 3
    config = request[TopLevelIndex.GENERATION_CONFIG] if len(request) > TopLevelIndex.GENERATION_CONFIG else None
    if isinstance(config, list) and len(config) > GenerationConfigIndex.THINKING_CONFIG:
        thinking = config[GenerationConfigIndex.THINKING_CONFIG]
        if isinstance(thinking, list) and len(thinking) > 3 and thinking[3] in (1, 2, 3, 4):
            level = thinking[3]
    body[18], body[28] = [1, None, None, level], str(uuid.uuid4()).upper()
    return body


class SparseJSON:
    """JSPB omits array values; normalize commas without touching string data."""
    def __init__(self) -> None:
        self.string = self.escape = False
        self.previous = ""

    def feed(self, text: str) -> str:
        result = []
        for char in text:
            if self.string:
                result.append(char)
                if self.escape:
                    self.escape = False
                elif char == "\\":
                    self.escape = True
                elif char == '"':
                    self.string = False
                    self.previous = '"'
            else:
                if char == '"':
                    self.string = True
                elif (char == "," and self.previous in {"[", ","}) or (char == "]" and self.previous == ","):
                    result.append("null")
                result.append(char)
                if not char.isspace():
                    self.previous = char
        return "".join(result)


def at(value: list, index: int) -> object:
    return value[index] if len(value) > index else None


class AssistantDecoder:
    def __init__(self) -> None:
        self.parts: dict[tuple[int, int], tuple[str, bool, str]] = {}
        self.usage: dict[int, list[int]] = {}
        self.complete = False
        self.body_seen = False
        self.last_body_step: int | None = None
        self.retained = 0

    def feed(self, message: list) -> list[list]:
        steps = at(message, 2)
        if steps is None:
            return []
        if not isinstance(steps, list):
            raise RequestError(502, "Invalid Build assistant steps")
        chunks: list[list] = []
        for step in steps:
            if not isinstance(step, list) or type(at(step, 11)) is not int or at(step, 12) not in (1, 2, 4):
                raise RequestError(502, "Invalid Build assistant step")
            number = step[11]
            if at(step, 5) is not None or step[12] == 4:
                raise RequestError(502, "Build assistant generation failed")
            if len(self.usage) > 64 or len(self.parts) > 512:
                raise RequestError(502, "Build assistant step limit exceeded")
            usage = at(step, 15)
            if isinstance(usage, list) and len(usage) >= 3:
                if any(type(value) is not int or value < 0 for value in usage[:3]):
                    raise RequestError(502, "Invalid Build assistant usage")
                self.usage[number] = usage[:3]
            contents = at(step, 0)
            if contents is None:
                continue
            if not isinstance(contents, list):
                raise RequestError(502, "Invalid Build assistant content")
            offset = 0
            for content in contents:
                if not isinstance(content, list) or len(content) < 2 or not isinstance(content[0], list):
                    raise RequestError(502, "Invalid Build assistant content")
                if content[1] != "model":
                    continue
                for part in content[0]:
                    if not isinstance(part, list) or (at(part, 1) is not None and not isinstance(part[1], str)):
                        raise RequestError(502, "Invalid Build assistant part")
                    if any(value is not None for index, value in enumerate(part) if index not in {0, 1, 10, 12, 14}):
                        raise RequestError(502, "Build text channel received unsupported content")
                    text = str(at(part, 1) or "")
                    thought = at(part, 12) in (True, 1)
                    signature = at(part, 14) or ""
                    if not isinstance(signature, str):
                        raise RequestError(502, "Invalid Build assistant signature")
                    previous, previous_thought, old_signature = self.parts.get((number, offset), ("", thought, ""))
                    if not text.startswith(previous) or thought != previous_thought:
                        raise RequestError(502, "Build assistant snapshot changed previously emitted content")
                    self.retained += len(text) - len(previous)
                    if self.retained > 2 * 1024 * 1024:
                        raise RequestError(502, "Build assistant response limit exceeded")
                    self.parts[(number, offset)] = (text, thought, signature or old_signature)
                    delta = text[len(previous):]
                    if delta or (signature and signature != old_signature):
                        wire_part = deepcopy(part)
                        wire_part.extend([None] * max(0, 15 - len(wire_part)))
                        wire_part[1] = delta
                        wire_part[10], wire_part[12] = thought, 1 if thought else None
                        wire_part[14] = signature if signature != old_signature else None
                        if delta and not thought:
                            if self.body_seen and not previous and self.last_body_step != number:
                                wire_part[1] = "\n\n" + delta
                            self.body_seen, self.last_body_step = True, number
                        chunks.append([[[[[wire_part], "model"], None]], None, None])
                    offset += 1
            self.complete = step[12] == 1 and at(step, 9) is None and self.body_seen and self.last_body_step == number
        return chunks

    def finish_chunk(self) -> list:
        if not self.complete:
            raise IncompleteResponseError
        usage = None
        if self.usage:
            uncached, output, cached = (sum(row[i] for row in self.usage.values()) for i in range(3))
            usage = [uncached + cached, output, uncached + cached + output, cached]
        return [[[[[], "model"], 1]], None, usage]


async def rpc_json(bridge: NativeBridge, session: str, method: str, body: list, timeout: float) -> list:
    status, raw = await bridge.rpc(session, method, body, timeout)
    if status != 200:
        raise classify_error(status, "Build upstream request failed")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise RequestError(502, "Invalid Build assistant RPC response") from exc
    if not isinstance(value, list) or not value or value[0] is None:
        raise RequestError(502, "Build assistant RPC returned no identifier")
    return value


async def stream_assistant(bridge: NativeBridge, session: str, request: list, timeout: float) -> AsyncGenerator[tuple[str, object], None]:
    from .native_session import owned_cleanup
    applet = generation = None
    completed = False
    # Reject unsupported tools before creating any upstream resource.
    assistant_body(request, "validation-only")
    try:
        async with asyncio.timeout(timeout):
            applet = (await rpc_json(bridge, session, "CreateApplet", [None, ["Untitled", "Generated by Gemini.", None, None, "default"], None, None, 1], timeout))[0]
            generation = (await rpc_json(bridge, session, "CodeAssistantOffline", assistant_body(request, applet), timeout))[0]
            decoder = AssistantDecoder()
            yield "status", 200
            yield "chunk", b"[["
            separator = b""
            for _ in range(2):
                parser, sparse, utf8 = IncrementalJSONStreamParser(), SparseJSON(), codecs.getincrementaldecoder("utf-8")()
                async with aclosing(bridge.stream(session, "StreamCodeAssistantOfflineGeneration", [generation, None, None, applet], timeout)) as events:
                    async for kind, value in events:
                        if kind == "status" and value != 200:
                            raise classify_error(int(str(value)), "Build task stream failed")
                        if kind != "chunk" or not isinstance(value, bytes):
                            continue
                        try:
                            messages = parser.feed(sparse.feed(utf8.decode(value)))
                            for message in messages:
                                for chunk in decoder.feed(message):
                                    yield "chunk", separator + json.dumps(chunk, ensure_ascii=False).encode()
                                    separator = b","
                                if decoder.complete:
                                    break
                        except UnicodeDecodeError as exc:
                            raise RequestError(502, "Invalid Build assistant UTF-8") from exc
                        if decoder.complete:
                            break
                if decoder.complete:
                    break
                list(parser.feed(sparse.feed(utf8.decode(b"", final=True))))
                list(parser.finish())
            terminal = decoder.finish_chunk()
            yield "chunk", separator + json.dumps(terminal).encode() + b"]]"
            completed = True
    finally:
        async def cleanup() -> None:
            async with asyncio.timeout(8):
                try:
                    if generation is not None:
                        await bridge.rpc(session, "CancelCodeAssistantOfflineGeneration", [generation, applet], 3)
                finally:
                    if applet is not None:
                        status, _ = await bridge.rpc(session, "DeleteApplet", [applet], 3)
                        if status not in (200, 404):
                            raise RequestError(503, "Build temporary application cleanup failed")
        if applet is not None:
            try:
                await owned_cleanup(cleanup())
            except Exception:
                if completed:
                    raise RequestError(503, "Build temporary application cleanup failed") from None
