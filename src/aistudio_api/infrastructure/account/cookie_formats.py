"""Offline Cookie decoding. Errors never include imported credential values."""

from __future__ import annotations

import json
import math
import re
import time

MAX_IMPORT_BYTES = 2 * 1024 * 1024
MAX_COOKIES = 1000
_COOKIE_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_COOKIE_VALUE = re.compile(r"^[\x21\x23-\x2B\x2D-\x3A\x3C-\x5B\x5D-\x7E]+$")
_TARGET_HOSTS = {"aistudio.google.com", "alkalimakersuite-pa.clients6.google.com"}


class CookieImportError(ValueError):
    """A safe, actionable import error, without source text or credentials."""


def decode_cookie_document(raw: object) -> object:
    try:
        encoded = (
            raw
            if isinstance(raw, str)
            else json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
        )
        size = len(encoded.encode("utf-8"))
    except (UnicodeEncodeError, TypeError, ValueError, RecursionError) as exc:
        raise CookieImportError(
            "凭据包含无效 JSON 或 Unicode 字符，请重新导出"
        ) from exc
    if size > MAX_IMPORT_BYTES:
        raise CookieImportError("凭据文件超过 2 MiB，请分批导入")
    if not isinstance(raw, str):
        return raw
    text = raw.lstrip("\ufeff").strip(" \r\n")
    if text.lstrip().startswith(("{", "[")):
        try:
            return json.loads(text)
        except (ValueError, RecursionError) as exc:
            raise CookieImportError("JSON 格式不正确，请重新导出凭据") from exc
    return text


def _in_scope(item: dict[str, object]) -> bool:
    domain = item.get("domain")
    if domain is not None:
        if not isinstance(domain, str) or not domain.strip():
            raise CookieImportError("Cookie 的 domain 必须是非空字符串")
        host = domain.strip().lower().lstrip(".")
        if host != "google.com" and host not in _TARGET_HOSTS:
            return False
        if host == "google.com" and item.get("hostOnly") is True:
            return False
    path = item.get("path", "/")
    if not isinstance(path, str):
        raise CookieImportError("Cookie 的 path 必须是字符串")
    # The shared HTTP credential dictionary has no per-URL cookie jar. Never
    # broaden a cookie limited to an unrelated path to every upstream request.
    if path not in ("", "/"):
        return False
    if item.get("partitionKey") is not None or item.get("partitioned") is True:
        return False
    if item.get("session") is not True:
        expires = item.get("expires", item.get("expirationDate"))
        if expires is not None:
            if isinstance(expires, bool) or not isinstance(expires, (int, float)):
                raise CookieImportError("Cookie 的过期时间必须是 Unix 秒时间戳")
            try:
                finite = math.isfinite(expires)
            except OverflowError:
                finite = False
            if not finite:
                raise CookieImportError("Cookie 的过期时间无效")
            if expires != -1 and expires <= time.time():
                return False
    return True


def _records(document: object, depth: int = 0) -> list[dict[str, object]]:
    if depth > 8:
        raise CookieImportError("凭据嵌套层级过深")
    document = decode_cookie_document(document)
    if isinstance(document, dict):
        for key in ("storage_state", "storageState", "cookies", "cookie"):
            if key in document:
                return _records(document[key], depth + 1)
        if document and all(isinstance(value, str) for value in document.values()):
            return [{"name": name, "value": value} for name, value in document.items()]
        raise CookieImportError("未找到 Cookie 数组、键值对象或 storage state")
    if isinstance(document, list):
        if not all(
            isinstance(item, dict) and "name" in item and "value" in item
            for item in document
        ):
            raise CookieImportError("Cookie 数组的每一项都必须包含 name 和 value")
        return document
    if not isinstance(document, str):
        raise CookieImportError("Cookie 必须为文本、JSON 对象或数组")
    if not document:
        return []
    records: list[dict[str, object]] = []
    if any(
        line.removeprefix("#HttpOnly_").count("\t") >= 6
        for line in document.splitlines()
    ):
        for number, line in enumerate(document.splitlines(), start=1):
            # Preserve trailing tabs: an empty cookie value is a valid seventh field.
            line = line.strip(" \r")
            if line.startswith("#HttpOnly_"):
                line = line.removeprefix("#HttpOnly_")
            elif not line.strip() or line.startswith("#"):
                continue
            columns = line.split("\t")
            if len(columns) != 7:
                raise CookieImportError(
                    f"cookies.txt 第 {number} 行必须包含 7 个制表符分隔的字段"
                )
            try:
                expires = float(columns[4])
            except ValueError as exc:
                raise CookieImportError(
                    f"cookies.txt 第 {number} 行的过期时间无效"
                ) from exc
            if columns[1].upper() not in {"TRUE", "FALSE"} or columns[
                3
            ].upper() not in {"TRUE", "FALSE"}:
                raise CookieImportError(f"cookies.txt 第 {number} 行的布尔字段无效")
            records.append(
                {
                    "domain": columns[0],
                    "path": columns[2],
                    "hostOnly": columns[1].upper() == "FALSE",
                    "expires": -1 if expires == 0 else expires,
                    "name": columns[5],
                    "value": columns[6],
                }
            )
        return records
    ignored_attributes = {
        "domain",
        "path",
        "expires",
        "max-age",
        "samesite",
        "secure",
        "httponly",
        "priority",
        "hostonly",
    }
    for line in document.splitlines():
        line = re.sub(r"^cookie:\s*", "", line.strip(), flags=re.IGNORECASE)
        if not line or line.startswith("#"):
            continue
        for entry in line.split(";"):
            if not entry.strip():
                continue
            name, separator, value = entry.strip().partition("=")
            name = name.strip()
            if name.lower() in ignored_attributes:
                continue
            if not separator:
                raise CookieImportError("Cookie 请求头必须使用 name=value 格式")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            records.append({"name": name, "value": value})
    return records


def parse_cookie_values(raw: object) -> dict[str, str]:
    """Normalize supported exports; ignore unrelated/expired cookies, reject conflicts."""
    records = _records(raw)
    if len(records) > MAX_COOKIES:
        raise CookieImportError("Cookie 条目过多，请仅导出 Google AI Studio 的 Cookie")
    parsed: dict[str, str] = {}
    for item in records:
        if not _in_scope(item):
            continue
        name, value = item.get("name"), item.get("value")
        if not isinstance(name, str) or not _COOKIE_NAME.fullmatch(name):
            raise CookieImportError("Cookie 名称包含无效字符")
        if value in (None, ""):
            continue
        if not isinstance(value, str) or not _COOKIE_VALUE.fullmatch(value):
            raise CookieImportError("Cookie 值包含无效字符，请重新导出")
        if name in parsed and parsed[name] != value:
            raise CookieImportError(
                "同名 Cookie 存在冲突，请仅导出当前 Google 登录会话"
            )
        parsed[name] = value
    return parsed
