"""账号管理路由。"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from fastapi import APIRouter, Body, Depends, HTTPException, Path as FastApiPath
from pydantic import AliasChoices, BaseModel, Field

from aistudio_api.api.dependencies import (
    get_account_service,
    get_runtime_state,
)
from aistudio_api.infrastructure.account.account_store import AccountMeta
from aistudio_api.infrastructure.account.cookie_formats import (
    MAX_IMPORT_BYTES,
    CookieImportError,
)
from aistudio_api.infrastructure.account.cookie_parser import (
    load_cookies_from_auth_file,
    probe_account_benefit_tier,
    probe_account_email,
)
from aistudio_api.infrastructure.account.credential_import import (
    ImportedAccount,
    bundle_entries,
    normalize_account_import,
    normalize_bundle_import,
)
from aistudio_api.infrastructure.utils.common import mask_email

if TYPE_CHECKING:
    from aistudio_api.api.state import RuntimeState
    from aistudio_api.application.account_service import AccountService
from aistudio_api.infrastructure.utils.logger import get_logger

log = get_logger("accounts")

router = APIRouter(prefix="/accounts")


class AccountResponse(BaseModel):
    id: str
    name: str
    email: str | None
    created_at: str
    last_used: str | None
    auth_user: str = "0"
    cookie_id: str | None = None
    tier: str = "Free"
    enabled: bool = True
    group: str = "default"
    notes: str = ""


def to_account_response(account: AccountMeta) -> AccountResponse:
    cid = getattr(account, "cookie_id", None) or (
        f"cookie_{account.created_at[:16]}"
        if getattr(account, "created_at", None)
        else f"cookie_{account.id}"
    )
    return AccountResponse(
        id=account.id,
        name=account.name,
        email=account.email,
        created_at=account.created_at or "",
        last_used=account.last_used,
        auth_user=getattr(account, "auth_user", "0") or "0",
        cookie_id=cid,
        tier=getattr(account, "tier", "Free") or "Free",
        enabled=getattr(account, "enabled", True),
        group=getattr(account, "group", "default") or "default",
        notes=getattr(account, "notes", "") or "",
    )


class UpdateAccountRequest(BaseModel):
    name: str


class UpdateGroupRequest(BaseModel):
    group: str


class UpdateNotesRequest(BaseModel):
    notes: str


class ToggleAccountRequest(BaseModel):
    enabled: bool | None = None


class PresetResponse(BaseModel):
    id: str
    name: str
    description: str = ""
    action: str = "snapshot"
    account_ids: list[str] = []
    target_tiers: list[str] = []
    target_groups: list[str] = []
    is_system: bool = False
    created_at: str = ""
    updated_at: str = ""


class CreatePresetRequest(BaseModel):
    name: str
    description: str = ""
    action: str = "snapshot"
    account_ids: list[str] = []
    target_tiers: list[str] = []
    target_groups: list[str] = []


class SnapshotPresetRequest(BaseModel):
    name: str
    description: str = ""


class ApplyPresetResponse(BaseModel):
    ok: bool = True
    preset_id: str
    preset_name: str
    enabled_count: int
    disabled_count: int
    enabled_account_ids: list[str]
    disabled_account_ids: list[str]


class ImportCookiesRequest(BaseModel):
    cookies: str = Field(max_length=MAX_IMPORT_BYTES)
    probe_identity: bool = True
    name: str | None = None
    email: str | None = None
    account_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_\-\.@]+$")
    auth_user: str = "0"
    tier: str | None = None


class ImportCookiesResponse(BaseModel):
    account_id: str
    name: str
    cookie_count: int
    auth_user: str = "0"
    tier: str = "Free"
    imported_count: int = 1


class ProbeAndImportRequest(BaseModel):
    cookies: str = Field(max_length=MAX_IMPORT_BYTES)
    name_prefix: str | None = None


class ProbeAndImportResponse(BaseModel):
    imported_count: int
    accounts: list[AccountResponse]


class BundleAccountItem(BaseModel):
    id: str | None = None
    account_id: str | None = None
    name: str | None = Field(
        default=None, validation_alias=AliasChoices("name", "label")
    )
    email: str | None = None
    cookies: object | None = Field(
        default=None, validation_alias=AliasChoices("cookies", "cookie")
    )
    storage_state: dict[str, object] | None = Field(
        default=None, validation_alias=AliasChoices("storage_state", "storageState")
    )
    auth_user: str | int | None = Field(
        default=None, validation_alias=AliasChoices("auth_user", "authUser")
    )
    tier: str | None = Field(
        default=None, validation_alias=AliasChoices("tier", "g1Tier")
    )


class ImportBundleRequest(BaseModel):
    probe_identity: bool = True
    file_path: str | None = None
    content: str | None = Field(default=None, max_length=MAX_IMPORT_BYTES)
    accounts: list[BundleAccountItem] | None = None


class ImportBundleResponse(BaseModel):
    imported_count: int
    accounts: list[AccountResponse]


@router.get("", response_model=list[AccountResponse])
@router.get("/", response_model=list[AccountResponse])
async def list_accounts(
    account_service: AccountService = Depends(get_account_service),
) -> list[AccountResponse]:
    """列出所有账号。"""
    accounts = account_service.list_accounts()
    return [to_account_response(a) for a in accounts]


@router.get("/active", response_model=AccountResponse)
async def get_active_account(
    account_service: AccountService = Depends(get_account_service),
) -> AccountResponse:
    """获取当前活跃账号。"""
    account = account_service.get_active_account()
    if account is None:
        raise HTTPException(status_code=404, detail="没有活跃账号")
    return to_account_response(account)


@router.post("/{account_id}/activate", response_model=AccountResponse)
async def activate_account(
    account_id: str = FastApiPath(..., pattern=r"^[a-zA-Z0-9_\-\.@]+$"),
    account_service: AccountService = Depends(get_account_service),
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> AccountResponse:
    """切换到指定账号。"""
    gateway_session = runtime_state.client.session if runtime_state.client else None

    if gateway_session is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    account = await account_service.activate_account(account_id, gateway_session)
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在或切换失败")
    if runtime_state.rotator:
        runtime_state.rotator.add_account(account.id)
        runtime_state.rotator.clear_cooldown(account.id)
    log.info("激活账号: id=%s, name=%s", account.id, account.name)
    return to_account_response(account)


@router.post("/{account_id}/detect-email", response_model=AccountResponse)
async def detect_account_email(
    account_id: str = FastApiPath(..., pattern=r"^[a-zA-Z0-9_\-\.@]+$"),
    account_service: AccountService = Depends(get_account_service),
) -> AccountResponse:
    """自动获取并更新指定账号的真实邮箱与订阅等级。"""
    acc = account_service.get_account(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")

    target_auth = account_service.get_account_auth_path(account_id)
    email: str | None = None
    detected_tier: str | None = None

    if target_auth:
        try:
            cookie_dict = load_cookies_from_auth_file(target_auth)
            if cookie_dict:
                email = await probe_account_email(
                    cookie_dict, auth_user=acc.auth_user or "0"
                )
                http_tier = await probe_account_benefit_tier(
                    cookie_dict, auth_user=acc.auth_user or "0"
                )
                if http_tier:
                    detected_tier = http_tier
        except Exception as e:
            log.debug(
                "通过 HTTP 探测账号 %s 邮箱/等级失败: %s", account_id, type(e).__name__
            )
    if email:
        account_service.update_account_email(account_id, email)
        log.info("已更新账号邮箱: id=%s, email=%s", account_id, mask_email(email))

    if detected_tier:
        account_service.update_account_tier(account_id, detected_tier)
        log.info("已更新账号订阅等级: id=%s, tier=%s", account_id, detected_tier)

    acc = account_service.get_account(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    return to_account_response(acc)


@router.post("/{account_id}/detect-tier", response_model=AccountResponse)
async def detect_account_tier(
    account_id: str = FastApiPath(..., pattern=r"^[a-zA-Z0-9_\-\.@]+$"),
    account_service: AccountService = Depends(get_account_service),
) -> AccountResponse:
    """探测并更新指定账号的订阅权益等级 (Free / Pro / Ultra / Plus)。"""
    acc = account_service.get_account(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")

    detected_tier: str | None = None
    target_auth = account_service.get_account_auth_path(account_id)
    if target_auth:
        try:
            cookie_dict = load_cookies_from_auth_file(target_auth)
            if cookie_dict:
                detected_tier = await probe_account_benefit_tier(
                    cookie_dict, auth_user=acc.auth_user or "0"
                )
        except Exception as e:
            log.debug(
                "通过 HTTP 探测账号 %s 订阅等级失败: %s", account_id, type(e).__name__
            )

    if detected_tier:
        account_service.update_account_tier(account_id, detected_tier)
        log.info("已更新账号订阅等级: id=%s, tier=%s", account_id, detected_tier)

    acc = account_service.get_account(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    return to_account_response(acc)


@router.delete("/{account_id}")
async def delete_account(
    account_id: str = FastApiPath(..., pattern=r"^[a-zA-Z0-9_\-\.@]+$"),
    account_service: AccountService = Depends(get_account_service),
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> dict[str, bool]:
    """删除账号。"""
    active_account = account_service.get_active_account()
    is_active = active_account is not None and active_account.id == account_id

    success = account_service.delete_account(account_id)
    if not success:
        raise HTTPException(status_code=404, detail="账号不存在")

    if runtime_state.rotator:
        runtime_state.rotator.remove_account(account_id)
    if is_active:
        remaining_accounts = account_service.list_accounts()
        new_active = remaining_accounts[0] if remaining_accounts else None
        gateway_session = runtime_state.client.session if runtime_state.client else None
        if new_active is not None:
            account_service.set_active_account(new_active.id)
            if gateway_session is not None:
                new_auth_path = account_service.get_account_auth_path(new_active.id)
                await gateway_session.switch_auth(
                    str(new_auth_path) if new_auth_path else None
                )
        else:
            if gateway_session is not None:
                await gateway_session.switch_auth(None)

    log.info("已删除账号: id=%s", account_id)
    return {"ok": True}


@router.delete("/group/{cookie_id}")
async def delete_cookie_group(
    cookie_id: str = FastApiPath(..., pattern=r"^[a-zA-Z0-9_\-\.@]+$"),
    account_service: AccountService = Depends(get_account_service),
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> dict[str, int]:
    """按 Cookie 组批量删除该 Cookie 下的所有子账号。"""
    active_account = account_service.get_active_account()
    active_id = active_account.id if active_account is not None else None
    active_deleted = False

    accounts = account_service.list_accounts()
    deleted_count = 0
    rotator = runtime_state.rotator
    for a in accounts:
        acc_cid = getattr(a, "cookie_id", None) or f"cookie_{a.created_at[:16]}"
        if acc_cid == cookie_id:
            if a.id == active_id:
                active_deleted = True
            if account_service.delete_account(a.id):
                deleted_count += 1
                if rotator:
                    rotator.remove_account(a.id)
                log.info("已删除子账号: cookie_id=%s, id=%s", cookie_id, a.id)

    if active_deleted:
        remaining_accounts = account_service.list_accounts()
        new_active = remaining_accounts[0] if remaining_accounts else None
        gateway_session = runtime_state.client.session if runtime_state.client else None
        if new_active is not None:
            account_service.set_active_account(new_active.id)
            if gateway_session is not None:
                new_auth_path = account_service.get_account_auth_path(new_active.id)
                await gateway_session.switch_auth(
                    str(new_auth_path) if new_auth_path else None
                )
        else:
            if gateway_session is not None:
                await gateway_session.switch_auth(None)

    return {"deleted": deleted_count}


@router.put("/{account_id}", response_model=AccountResponse)
async def update_account(
    account_id: str = FastApiPath(..., pattern=r"^[a-zA-Z0-9_\-\.@]+$"),
    req: UpdateAccountRequest = Body(...),
    account_service: AccountService = Depends(get_account_service),
) -> AccountResponse:
    """更新账号名称。"""
    account = account_service.update_account(account_id, req.name)
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    log.info("账号名称已更新: id=%s, name=%s", account.id, req.name)
    return to_account_response(account)


@router.post("/{account_id}/toggle", response_model=AccountResponse)
async def toggle_account(
    account_id: str = FastApiPath(..., pattern=r"^[a-zA-Z0-9_\-\.@]+$"),
    req: ToggleAccountRequest = Body(default_factory=ToggleAccountRequest),
    account_service: AccountService = Depends(get_account_service),
) -> AccountResponse:
    """切换单个账号的使能状态 (Enabled/Disabled)。"""
    account = account_service.toggle_account_enabled(account_id, req.enabled)
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")

    # 如果刚好关闭了当前活跃账号，顺延切到下一个可用账号
    active_acc = account_service.get_active_account()
    if active_acc and active_acc.id == account_id and not account.enabled:
        from aistudio_api.application.account_orchestrator import try_switch_account

        await try_switch_account(failed_account_id=account_id)

    log.info("账号状态已切换: id=%s, enabled=%s", account.id, account.enabled)
    return to_account_response(account)


@router.put("/{account_id}/group", response_model=AccountResponse)
async def update_account_group(
    account_id: str = FastApiPath(..., pattern=r"^[a-zA-Z0-9_\-\.@]+$"),
    req: UpdateGroupRequest = Body(...),
    account_service: AccountService = Depends(get_account_service),
) -> AccountResponse:
    """更新账号所属业务分组。"""
    account = account_service.update_account_group(account_id, req.group)
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    log.info("账号分组已更新: id=%s, group=%s", account.id, account.group)
    return to_account_response(account)


@router.put("/{account_id}/notes", response_model=AccountResponse)
async def update_account_notes(
    account_id: str = FastApiPath(..., pattern=r"^[a-zA-Z0-9_\-\.@]+$"),
    req: UpdateNotesRequest = Body(...),
    account_service: AccountService = Depends(get_account_service),
) -> AccountResponse:
    """更新账号备注。"""
    account = account_service.update_account_notes(account_id, req.notes)
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    return to_account_response(account)


@router.get("/groups/list", response_model=list[str])
async def list_groups(
    account_service: AccountService = Depends(get_account_service),
) -> list[str]:
    """列出当前存在的所有分组名称。"""
    return account_service.list_groups()


@router.post("/groups/{group_name}/toggle")
async def batch_toggle_group(
    group_name: str,
    enabled: bool = Body(..., embed=True),
    account_service: AccountService = Depends(get_account_service),
) -> dict[str, object]:
    """按分组批量启用或停用账号。"""
    updated = account_service.batch_toggle_group(group_name, enabled)
    active_acc = account_service.get_active_account()
    if active_acc and active_acc.group == group_name and not enabled:
        from aistudio_api.application.account_orchestrator import try_switch_account

        await try_switch_account(failed_account_id=active_acc.id)
    return {"ok": True, "count": len(updated), "group": group_name, "enabled": enabled}


# ─── 预设方案 (Presets) REST 路由 ───
@router.get("/presets/list", response_model=list[PresetResponse])
async def list_presets(
    account_service: AccountService = Depends(get_account_service),
) -> list[PresetResponse]:
    """获取所有可用预设方案列表。"""
    presets = account_service.list_presets()
    return [PresetResponse.model_validate(p.to_dict()) for p in presets]


@router.post("/presets", response_model=PresetResponse)
async def create_preset(
    req: CreatePresetRequest,
    account_service: AccountService = Depends(get_account_service),
) -> PresetResponse:
    """创建自定义预设方案。"""
    import secrets

    from aistudio_api.infrastructure.account.account_store import AccountPreset

    pid = f"preset_{secrets.token_hex(4)}"
    preset = AccountPreset(
        id=pid,
        name=req.name.strip() or "自定义方案",
        description=req.description.strip(),
        action=req.action,
        account_ids=req.account_ids,
        target_tiers=req.target_tiers,
        target_groups=req.target_groups,
        is_system=False,
    )
    saved = account_service.save_preset(preset)
    return PresetResponse.model_validate(saved.to_dict())


@router.post("/presets/snapshot", response_model=PresetResponse)
async def create_snapshot_preset(
    req: SnapshotPresetRequest,
    account_service: AccountService = Depends(get_account_service),
) -> PresetResponse:
    """将当前所有启用账号一键快照保存为新预设方案。"""
    saved = account_service.create_snapshot_preset(req.name, req.description)
    log.info("已创建预设快照: name=%s, id=%s", saved.name, saved.id)
    return PresetResponse.model_validate(saved.to_dict())


@router.delete("/presets/{preset_id}")
async def delete_preset(
    preset_id: str,
    account_service: AccountService = Depends(get_account_service),
) -> dict[str, bool]:
    """删除自定义预设方案。"""
    try:
        ok = account_service.delete_preset(preset_id)
        if not ok:
            raise HTTPException(status_code=404, detail="预设方案不存在")
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/presets/{preset_id}/apply", response_model=ApplyPresetResponse)
async def apply_preset(
    preset_id: str,
    account_service: AccountService = Depends(get_account_service),
) -> ApplyPresetResponse:
    """一键应用预设方案，原子切换账号使能状态。"""
    try:
        preset, en_ids, dis_ids = account_service.apply_preset(preset_id)
        # 检查当前活跃账号是否有效，若失效则顺延切换
        active_acc = account_service.get_active_account()
        from aistudio_api.application.account_orchestrator import try_switch_account

        if not active_acc or not active_acc.enabled or active_acc.id in dis_ids:
            await try_switch_account(
                failed_account_id=active_acc.id if active_acc else None
            )
        else:
            await try_switch_account()

        log.info(
            "已应用预设: name=%s, 启用=%d, 停用=%d",
            preset.name,
            len(en_ids),
            len(dis_ids),
        )
        return ApplyPresetResponse(
            ok=True,
            preset_id=preset.id,
            preset_name=preset.name,
            enabled_count=len(en_ids),
            disabled_count=len(dis_ids),
            enabled_account_ids=en_ids,
            disabled_account_ids=dis_ids,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


async def _refresh_imported_accounts(
    accounts: list[AccountMeta],
    account_service: AccountService,
    runtime_state: RuntimeState,
) -> None:
    """New requests receive refreshed credentials; in-flight contexts stay immutable."""
    rotator = getattr(runtime_state, "rotator", None)
    for account in accounts:
        if rotator:
            rotator.add_account(account.id)
            rotator.clear_cooldown(account.id)
    active = account_service.get_active_account()
    if active is None:
        active = next((account for account in accounts if account.enabled), None)
        if active is not None:
            account_service.set_active_account(active.id)
    client = getattr(runtime_state, "client", None)
    if (
        active
        and any(account.id == active.id for account in accounts)
        and client is not None
        and client.session is not None
    ):
        await account_service.activate_account(active.id, client.session)


async def _save_imported_accounts(
    items: list[ImportedAccount],
    *,
    probe_identity: bool,
    account_service: AccountService,
    runtime_state: RuntimeState,
) -> list[AccountMeta]:
    """All entries have been validated before the first network request or write."""
    import secrets

    for index, item in enumerate(items, start=1):
        if item.account_id is not None:
            try:
                account_service.validate_account_id(item.account_id)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400, detail=f"第 {index} 个账号的保存路径无效"
                ) from exc

    imported: list[AccountMeta] = []
    for item in items:
        existing = (
            account_service.get_account(item.account_id) if item.account_id else None
        )
        email = item.email or (existing.email if existing else None)
        auth_user = (
            existing.auth_user
            if existing and not item.auth_user_explicit
            else item.auth_user
        )
        tier = item.tier
        if probe_identity and (not email or not tier):
            cookie_list = item.storage_state["cookies"]
            assert isinstance(cookie_list, list)
            cookies = {
                str(cookie["name"]): str(cookie["value"]) for cookie in cookie_list
            }
            if not email:
                with contextlib.suppress(Exception):
                    email = await probe_account_email(cookies, auth_user=auth_user)
            if not tier:
                with contextlib.suppress(Exception):
                    tier = await probe_account_benefit_tier(
                        cookies, auth_user=auth_user
                    )
        account = account_service.save_account_from_cookies(
            name=item.name
            or (existing.name if existing else None)
            or (
                f"Google Account (u/{item.auth_user})"
                if item.auth_user != "0"
                else "导入的账号"
            ),
            email=email,
            storage_state=item.storage_state,
            account_id=item.account_id,
            auth_user=auth_user,
            cookie_id=(existing.cookie_id if existing else None)
            or f"cookie_{secrets.token_hex(4)}",
            tier=tier or "Free",
        )
        imported.append(account)
        # A later optional probe or disk error must not leave a saved active
        # account using stale in-memory credentials.
        await _refresh_imported_accounts([account], account_service, runtime_state)
    return imported


@router.post("/import-cookies", response_model=ImportCookiesResponse)
async def import_cookies(
    req: ImportCookiesRequest,
    account_service: AccountService = Depends(get_account_service),
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> ImportCookiesResponse:
    """Import Cookie text or a structured export through the shared validation path."""
    try:
        if bundle_entries(req.cookies) is not None:
            if req.account_id:
                raise CookieImportError("更新单个账号时不能导入账号包")
            items = normalize_bundle_import(req.cookies)
        else:
            overrides = req.model_dump(
                exclude={"cookies", "probe_identity"},
                exclude_unset=True,
                exclude_none=True,
            )
            items = [normalize_account_import(req.cookies, overrides=overrides)]
    except CookieImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    accounts = await _save_imported_accounts(
        items,
        probe_identity=req.probe_identity,
        account_service=account_service,
        runtime_state=runtime_state,
    )
    account = accounts[0]
    return ImportCookiesResponse(
        account_id=account.id,
        name=account.name,
        cookie_count=sum(item.cookie_count for item in items),
        auth_user=account.auth_user,
        tier=account.tier,
        imported_count=len(accounts),
    )


@router.post("/probe-import", response_model=ProbeAndImportResponse)
async def probe_and_import(
    req: ProbeAndImportRequest,
    account_service: AccountService = Depends(get_account_service),
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> ProbeAndImportResponse:
    """Explicitly discover a bounded number of Google identities, then save them."""
    import secrets

    from aistudio_api.infrastructure.account.cookie_parser import (
        probe_google_accounts_infinite,
    )

    try:
        item = normalize_account_import(req.cookies)
    except CookieImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    probed = await probe_google_accounts_infinite(item.storage_state)
    if not probed:
        raise HTTPException(status_code=400, detail="未探测到有效已登录 Google 账号")
    accounts = account_service.batch_import_accounts(
        probed_list=probed,
        storage_state=item.storage_state,
        prefix=(req.name_prefix or item.name or "Google Account").strip(),
        cookie_id=f"cookie_{secrets.token_hex(4)}",
    )
    await _refresh_imported_accounts(accounts, account_service, runtime_state)
    log.info("Cookie 身份识别完成: 导入账号数=%d", len(accounts))
    return ProbeAndImportResponse(
        imported_count=len(accounts),
        accounts=[to_account_response(account) for account in accounts],
    )


@router.post("/import-bundle", response_model=ImportBundleResponse)
async def import_bundle(
    req: ImportBundleRequest,
    account_service: AccountService = Depends(get_account_service),
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> ImportBundleResponse:
    """Validate every account before persisting a bundle; never silently skip entries."""
    from pathlib import Path

    try:
        if (
            sum(
                source is not None
                for source in (req.accounts, req.content, req.file_path)
            )
            != 1
        ):
            raise CookieImportError(
                "请且仅提供 accounts、content 或 file_path 中的一项"
            )
        raw: object
        if req.accounts is not None:
            raw = {
                "accounts": [
                    account.model_dump(exclude_none=True) for account in req.accounts
                ]
            }
        elif req.content is not None:
            raw = req.content
        else:
            assert req.file_path is not None
            try:
                path = Path(req.file_path).expanduser().resolve()
                if path.stat().st_size > MAX_IMPORT_BYTES:
                    raise CookieImportError("凭据文件超过 2 MiB，请分批导入")
                raw = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError, ValueError) as exc:
                if isinstance(exc, CookieImportError):
                    raise
                raise CookieImportError(
                    "无法读取凭据文件，请检查路径或直接上传文件"
                ) from exc
        items = normalize_bundle_import(raw)
    except CookieImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    accounts = await _save_imported_accounts(
        items,
        probe_identity=req.probe_identity,
        account_service=account_service,
        runtime_state=runtime_state,
    )
    log.info("批量导入完成: 导入账号数=%d", len(accounts))
    return ImportBundleResponse(
        imported_count=len(accounts),
        accounts=[to_account_response(account) for account in accounts],
    )
