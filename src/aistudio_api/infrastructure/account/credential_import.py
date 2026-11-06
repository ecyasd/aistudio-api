"""Normalize account exports before any probe, persistence, or session change."""

from __future__ import annotations

import re
from dataclasses import dataclass

from aistudio_api.infrastructure.account.cookie_formats import (
    CookieImportError,
    decode_cookie_document,
)
from aistudio_api.infrastructure.account.cookie_parser import (
    filter_minimal_cookies,
    normalize_benefit_tier,
    parse_raw_cookies,
)

MAX_BUNDLE_ACCOUNTS = 100
_ACCOUNT_ID = re.compile(r"^[A-Za-z0-9_.@-]{1,128}$")
_TIERS = {"free", "plus", "pro", "ultra", "ultra_20x", "tier0", "tier1", "tier2"}


@dataclass(frozen=True)
class ImportedAccount:
    storage_state: dict[str, object]
    name: str | None
    email: str | None
    account_id: str | None
    auth_user: str
    auth_user_explicit: bool
    tier: str | None
    cookie_count: int


def bundle_entries(raw: object) -> list[object] | None:
    document = decode_cookie_document(raw)
    entries: object = None
    if isinstance(document, dict):
        if "accounts" in document and "profiles" in document:
            raise CookieImportError("账号包不能同时包含 accounts 和 profiles")
        if "accounts" in document:
            entries = document["accounts"]
        elif "profiles" in document:
            entries = document["profiles"]
        else:
            return None
    elif isinstance(document, list) and any(
        isinstance(item, dict)
        and any(
            key in item
            for key in ("cookies", "cookie", "storage_state", "storageState")
        )
        for item in document
    ):
        entries = document
    else:
        return None
    if not isinstance(entries, list) or not entries:
        raise CookieImportError("账号包必须包含非空的账号列表")
    if len(entries) > MAX_BUNDLE_ACCOUNTS:
        raise CookieImportError("每个账号包最多包含 100 个账号，请分批导入")
    return entries


def _optional_text(value: object, field: str, limit: int) -> str | None:
    if value is None or value == "":
        return None
    if (
        not isinstance(value, str)
        or len(value) > limit
        or any(ord(char) < 32 for char in value)
    ):
        raise CookieImportError(f"账号的 {field} 字段无效")
    return value.strip() or None


def normalize_account_import(
    raw: object, *, overrides: dict[str, object] | None = None
) -> ImportedAccount:
    document = decode_cookie_document(raw)
    if bundle_entries(document) is not None:
        raise CookieImportError("账号条目中不能再嵌套账号包")
    metadata = dict(document) if isinstance(document, dict) else {}
    for key, value in (overrides or {}).items():
        if value is not None:
            metadata[key] = value

    cookie_values = filter_minimal_cookies(parse_raw_cookies(document))
    if not cookie_values:
        raise CookieImportError("未找到未过期且作用域适用的 Google 登录 Cookie")
    if not any(
        cookie_values.get(key) for key in ("SAPISID", "__Secure-3PAPISID", "APISID")
    ):
        raise CookieImportError(
            "缺少请求签名所需的 SAPISID、__Secure-3PAPISID 或 APISID，请重新导出完整 Cookie"
        )

    account_id = _optional_text(
        metadata.get("account_id", metadata.get("id")), "id", 128
    )
    if account_id is not None and (
        not _ACCOUNT_ID.fullmatch(account_id) or account_id == "." or ".." in account_id
    ):
        raise CookieImportError("账号 id 只能使用字母、数字、下划线、连字符、点或 @")
    auth_user = metadata.get("auth_user", metadata.get("authUser", "0"))
    if (
        isinstance(auth_user, bool)
        or not isinstance(auth_user, (str, int))
        or not re.fullmatch(r"[0-9]{1,3}", str(auth_user))
    ):
        raise CookieImportError("登录序号 auth_user 必须是 0 到 999 的整数")
    tier_text = _optional_text(metadata.get("tier", metadata.get("g1Tier")), "tier", 32)
    if tier_text is not None and tier_text.lower() not in _TIERS:
        raise CookieImportError("账号等级必须是 Free、Plus、Pro、Ultra 或 Ultra_20X")
    email = _optional_text(metadata.get("email"), "email", 320)
    if email is None:
        state = metadata.get("storage_state", metadata.get("storageState", metadata))
        extension = state.get("aistudio2api") if isinstance(state, dict) else None
        source = extension.get("source") if isinstance(extension, dict) else None
        if isinstance(source, dict):
            email = _optional_text(source.get("email"), "email", 320)
    if email is None and any(
        key in metadata for key in ("storage_state", "storageState")
    ):
        label = metadata.get("label")
        if isinstance(label, str) and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", label):
            email = _optional_text(label, "email", 320)
    return ImportedAccount(
        storage_state={
            "cookies": [
                {"name": key, "value": value} for key, value in cookie_values.items()
            ]
        },
        name=_optional_text(metadata.get("name", metadata.get("label")), "name", 256),
        email=email.lower() if email else None,
        account_id=account_id,
        auth_user=str(int(auth_user)),
        auth_user_explicit="auth_user" in metadata or "authUser" in metadata,
        tier=normalize_benefit_tier(tier_text) if tier_text else None,
        cookie_count=len(cookie_values),
    )


def normalize_bundle_import(raw: object) -> list[ImportedAccount]:
    entries = bundle_entries(raw)
    if entries is None:
        raise CookieImportError("未找到 accounts、profiles 或账号条目数组")
    normalized: list[ImportedAccount] = []
    account_ids: set[str] = set()
    identities: set[tuple[str, str]] = set()
    for index, entry in enumerate(entries, start=1):
        try:
            if not isinstance(entry, dict):
                raise CookieImportError("账号条目必须是 JSON 对象")
            item = normalize_account_import(entry)
            if item.account_id is not None:
                if item.account_id in account_ids:
                    raise CookieImportError("账号包内包含重复 id")
                account_ids.add(item.account_id)
            if item.email:
                identity = (item.email, item.auth_user)
                if identity in identities:
                    raise CookieImportError("账号包内包含重复的邮箱和登录序号")
                identities.add(identity)
            normalized.append(item)
        except CookieImportError as exc:
            raise CookieImportError(f"第 {index} 个账号：{exc}") from exc
    return normalized
