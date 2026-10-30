"""System and metadata routes."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import yaml
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, StrictBool, ValidationError, field_validator

from aistudio_api.api.dependencies import get_runtime_state
from aistudio_api.api.response_models import (
    HealthResponse,
    ModelStatsResponse,
    StatsResponse,
    StatsTotalsResponse,
)
from aistudio_api.application import account_orchestrator
from aistudio_api.application.model_selection import parse_model_selection
from aistudio_api.infrastructure.gateway.model_defaults import (
    _resolve_config_path,
    invalidate_config_cache,
    preview_model_defaults,
    validate_model_config,
)
from aistudio_api.infrastructure.gateway.wire_types import (
    SUPPORTED_GENERATION_CONFIG_FIELDS,
    AistudioImageOutputMode,
)
from aistudio_api.infrastructure.utils.config_store import (
    ConfigConflictError,
    api_key_items,
    mutate_config,
    save_model_config,
)
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("routes.system")

if TYPE_CHECKING:
    from aistudio_api.application.runtime import RuntimeState

public_router = APIRouter()
protected_router = APIRouter()


def health_response() -> HealthResponse:
    return HealthResponse(status="ok", busy=False)


def stats_response() -> StatsResponse:
    from aistudio_api.application.runtime import runtime_state

    stats = dict(runtime_state.model_stats)
    totals = StatsTotalsResponse(
        requests=sum(s.requests for s in stats.values()),
        success=sum(s.success for s in stats.values()),
        rate_limited=sum(s.rate_limited for s in stats.values()),
        errors=sum(s.errors for s in stats.values()),
        prompt_tokens=sum(s.prompt_tokens for s in stats.values()),
        completion_tokens=sum(s.completion_tokens for s in stats.values()),
        total_tokens=sum(s.total_tokens for s in stats.values()),
    )
    models = {
        name: ModelStatsResponse(
            requests=s.requests,
            success=s.success,
            rate_limited=s.rate_limited,
            errors=s.errors,
            prompt_tokens=s.prompt_tokens,
            completion_tokens=s.completion_tokens,
            total_tokens=s.total_tokens,
            last_used=s.last_used,
        )
        for name, s in stats.items()
    }
    return StatsResponse(models=models, totals=totals)


@public_router.get("/health", response_model=HealthResponse)
async def health():
    return health_response()


@protected_router.get("/stats", response_model=StatsResponse)
async def stats():
    return stats_response()


@protected_router.get("/browser/status")
async def browser_status(
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> dict[str, object]:
    """Report lifecycle state without starting Chromium or inspecting credentials."""
    client = runtime_state.client
    if client is None:
        return {
            "status": "not_started", "process_running": False,
            "in_flight": 0, "idle_timeout_seconds": 0,
        }
    return client.session.get_status()


# ========== 调度与配额管理 API ==========


class ClearCooldownRequest(BaseModel):
    account_id: str | None = None
    model: str | None = None


@protected_router.get("/rotation")
async def get_rotation_status(
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> dict[str, object]:
    """获取账号黏性调度与配额状态。"""
    rotator = runtime_state.rotator
    if rotator is None:
        return {
            "enabled": False,
            "mode": "sticky",
            "message": "调度器未初始化",
            "accounts": {},
        }

    return {
        "enabled": True,
        "mode": "sticky",
        "accounts": rotator.get_all_stats(),
    }


@protected_router.post("/rotation/clear-cooldown")
async def clear_cooldown(
    req: ClearCooldownRequest,
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> dict[str, object]:
    """清除指定账号或全局的 429 锁定状态。"""
    rotator = runtime_state.rotator
    if rotator is None:
        raise HTTPException(503, detail="调度器未初始化")

    if req.account_id:
        rotator.clear_cooldown(req.account_id, model=req.model)
    else:
        rotator.clear_all_cooldowns()
    logger.info("已清除冷却锁定: 账号=%s, 模型=%s", req.account_id, req.model)

    return {"ok": True, "accounts": rotator.get_all_stats()}


@protected_router.post("/rotation/cleanup-ghosts")
async def cleanup_ghosts(
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> dict[str, object]:
    """主动对齐并彻底清除已删除账号在调度器中残留的幽灵状态。"""
    rotator = runtime_state.rotator
    if rotator is None:
        raise HTTPException(503, detail="调度器未初始化")

    cleaned_ids = rotator.cleanup_ghost_accounts()
    return {
        "ok": True,
        "cleaned_count": len(cleaned_ids),
        "cleaned_ids": cleaned_ids,
        "accounts": rotator.get_all_stats(),
    }


@protected_router.get("/rotation/accounts")
async def get_rotation_accounts(
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> dict[str, dict[str, object]]:
    """获取所有账号的调度与配额统计。"""
    rotator = runtime_state.rotator
    if rotator is None:
        raise HTTPException(503, detail="调度器未初始化")

    return rotator.get_all_stats()


@protected_router.post("/rotation/next")
async def force_next_account(
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> dict[str, object]:
    """强制切换到下一个可用账号。"""
    async with account_orchestrator.account_change_lock():
        rotator = runtime_state.rotator
        if rotator is None:
            raise HTTPException(503, detail="调度器未初始化")

        next_account = await rotator.get_next_account()
        if next_account is None:
            raise HTTPException(404, detail="没有可用的账号")

        account_service = runtime_state.account_service
        client = runtime_state.client

        if account_service is None or client is None or client._session is None:
            raise HTTPException(503, detail="服务未就绪")

        result = await account_service.activate_account(
            next_account.id,
            client._session,
        )

        if result is None:
            raise HTTPException(500, detail="切换失败")

        account_orchestrator.note_account_changed()
        logger.info("已强制切换至下一可用账号: %s (%s)", result.id, result.name)
        return {
            "ok": True,
            "account": {
                "id": result.id,
                "name": result.name,
                "email": result.email,
            },
        }


# ========== 系统与模型配置 API ==========


class LoggingConfigRequest(BaseModel):
    level: str | None = None
    dump_requests: StrictBool | None = None
    dump_to_file: StrictBool | None = None
    dump_dir: str | None = None

    @field_validator("level")
    @classmethod
    def valid_level(cls, level: str | None) -> str | None:
        if level is None:
            return None
        level = level.strip().upper()
        if level not in {"NOTSET", "DEBUG", "INFO", "WARN", "WARNING", "ERROR", "FATAL", "CRITICAL"}:
            raise ValueError("Invalid logging level")
        return level


def _apply_logging_settings(config: LoggingConfigRequest) -> None:
    from aistudio_api.infrastructure.utils.logger import (
        set_dump_dir,
        set_dump_requests,
        set_dump_to_file,
        set_log_level,
    )

    if config.level is not None:
        set_log_level(config.level)
    if config.dump_requests is not None:
        set_dump_requests(config.dump_requests)
    if config.dump_to_file is not None:
        set_dump_to_file(config.dump_to_file)
    if config.dump_dir is not None:
        set_dump_dir(config.dump_dir)


class ConfigYamlUpdateRequest(BaseModel):
    yaml_content: str
    baseline_yaml_content: str | None = None


class ModelPreviewRequest(BaseModel):
    model: str = Field(min_length=1, max_length=160, pattern=r"^(?:models/)?(?:抗截断/)?[A-Za-z0-9][A-Za-z0-9._-]*$")
    yaml_content: str | None = Field(default=None, max_length=1_000_000)


@protected_router.post("/config/model-preview")
async def preview_model_rules(req: ModelPreviewRequest, response: Response) -> dict[str, object]:
    """Read-only rule resolution; no account/session preparation or writes."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = "Authorization"
    draft = (_parse_config_yaml(req.yaml_content, normalize_logging=False)
             if req.yaml_content is not None else None)
    try:
        selection = parse_model_selection(req.model)
        defaults, matched = preview_model_defaults(selection.base_model, config=draft)
    except (ValueError, TypeError, KeyError, OverflowError, yaml.YAMLError) as exc:
        raise HTTPException(400, detail="模型名称或规则无效，请检查模型、结构、枚举及数值") from exc
    except OSError as exc:
        raise HTTPException(503, detail="模型规则暂时不可读取") from exc
    generation = defaults.generation_config_overrides()
    # Unknown extension attributes are ignored by the wire encoder. Make that
    # visible instead of presenting them as effective generation parameters.
    supported = SUPPORTED_GENERATION_CONFIG_FIELDS
    return {
        "model": selection.base_model,
        "auto_continue": selection.auto_continue,
        "source": "draft" if draft is not None else "saved",
        "matched_rules": matched,
        "ignored_generation_keys": sorted(set(generation) - supported),
        "effective": {
            "is_image_model": defaults.is_image_model,
            "default_tools": defaults.default_tools,
            "generation_config_defaults": {
                key: value.to_wire() if isinstance(value, AistudioImageOutputMode) else value
                for key, value in generation.items() if key in supported
            },
            "clear_generation_config_indexes": defaults.clear_generation_config_indexes,
            "safety_settings": defaults.safety_settings_overrides(),
            "disable_safety_settings": defaults.disable_safety_settings,
            "drop_unsupported_params": defaults.drop_unsupported_params,
        },
    }


@protected_router.get("/config")
async def get_system_config() -> dict[str, object]:
    """获取系统运行配置与 config.yaml 内容。"""
    from aistudio_api.config import settings
    from aistudio_api.infrastructure.utils.logger import (
        get_dump_dir,
        get_log_level,
        is_debug_env_active,
        is_dump_requests_enabled,
        is_dump_to_file_enabled,
    )

    config_yaml_path = _resolve_config_path(None)
    yaml_content = ""
    if config_yaml_path.exists():
        with contextlib.suppress(Exception):
            yaml_content = config_yaml_path.read_text(encoding="utf-8")
    return {
        "port": settings.port,
        "browser_port": settings.browser_port,
        "browser_headless": settings.browser_headless,
        "proxy_configured": bool(settings.proxy_url),
        "auth_enabled": settings.auth_enabled,
        "timeout_stream": settings.timeout_stream,
        "yaml_content": yaml_content,
        "log_level": get_log_level(),
        "dump_requests": is_dump_requests_enabled(),
        "debug_env_active": is_debug_env_active(),
        "dump_to_file": is_dump_to_file_enabled(),
        "dump_dir": str(get_dump_dir()),
    }


def _parse_config_yaml(content: str, *, normalize_logging: bool = True) -> dict[str, object]:
    try:
        parsed = yaml.safe_load(content)
        if parsed is not None and not isinstance(parsed, dict):
            raise HTTPException(400, detail="YAML 顶层必须为字典结构")
    except yaml.YAMLError as e:
        raise HTTPException(400, detail="YAML 语法格式错误") from e
    if isinstance(parsed, dict) and parsed.get("logging") is not None:
        raw_logging = parsed["logging"]
        if not isinstance(raw_logging, dict):
            raise HTTPException(400, detail="日志配置必须为字典结构")
        raw_logging = dict(raw_logging)
        for key, default in (("level", "INFO"), ("dump_requests", False), ("dump_to_file", False), ("dump_dir", "")):
            if key in raw_logging and raw_logging[key] is None:
                raw_logging[key] = default
        try:
            logging_config = LoggingConfigRequest.model_validate(raw_logging)
        except ValidationError as e:
            raise HTTPException(400, detail="日志配置格式或级别无效") from e
        if normalize_logging:
            parsed["logging"] = {**raw_logging, **logging_config.model_dump(exclude_unset=True)}
    return parsed or {}


@protected_router.put("/config/yaml")
async def update_config_yaml(req: ConfigYamlUpdateRequest) -> dict[str, object]:
    """更新配置；有基线时仅合并显式编辑字段，并拒绝覆盖并发修改。"""
    # Compare the exact loaded snapshot before normalizing an explicit edit;
    # otherwise hand-written lowercase levels/nulls can create false conflicts.
    parsed = _parse_config_yaml(req.yaml_content, normalize_logging=False)
    baseline = (
        _parse_config_yaml(req.baseline_yaml_content, normalize_logging=False)
        if req.baseline_yaml_content is not None
        else None
    )
    logging_changed = baseline is None or (
        ("logging" in parsed, parsed.get("logging"))
        != ("logging" in baseline, baseline.get("logging"))
    )
    if logging_changed:
        parsed = _parse_config_yaml(req.yaml_content)
    try:
        validate_model_config(parsed)
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise HTTPException(400, detail="模型规则的结构、枚举或数值无效，请检查配置") from exc
    config_yaml_path = _resolve_config_path(None)
    try:
        saved = save_model_config(config_yaml_path, parsed, baseline=baseline)
        invalidate_config_cache()

        if logging_changed and isinstance(saved.get("logging"), dict):
            _apply_logging_settings(LoggingConfigRequest.model_validate(saved["logging"]))
        logger.info("配置文件 config.yaml 已更新并重新加载")
        return {"ok": True, "message": "配置已保存并重载"}
    except ConfigConflictError as e:
        raise HTTPException(409, detail="配置已被其他操作修改，请刷新后合并修改再保存") from e
    except Exception as e:
        raise HTTPException(500, detail="写入配置文件失败") from e


@protected_router.put("/config/logging")
async def update_logging_config(req: LoggingConfigRequest) -> dict[str, object]:
    """更新系统日志级别与请求转储配置，并持久化到 config.yaml。"""
    from aistudio_api.infrastructure.gateway.model_defaults import (
        update_configured_logging_settings,
    )
    from aistudio_api.infrastructure.utils.logger import (
        get_dump_dir,
        get_log_level,
        is_debug_env_active,
        is_dump_requests_enabled,
        is_dump_to_file_enabled,
    )

    try:
        update_configured_logging_settings(
            level=req.level,
            dump_requests=req.dump_requests,
            dump_to_file=req.dump_to_file,
            dump_dir=req.dump_dir,
        )
    except Exception as e:
        raise HTTPException(500, detail="保存日志配置失败") from e
    _apply_logging_settings(req)
    logger.info(
        "日志配置已更新: 级别=%s, 请求转储=%s, 文件转储=%s, 目录=%s",
        req.level,
        req.dump_requests,
        req.dump_to_file,
        req.dump_dir,
    )

    return {
        "ok": True,
        "log_level": get_log_level(),
        "dump_requests": is_dump_requests_enabled(),
        "debug_env_active": is_debug_env_active(),
        "dump_to_file": is_dump_to_file_enabled(),
        "dump_dir": str(get_dump_dir()),
    }


# ========== API Key 备注与密钥管理 ==========


class ApiKeyItemModel(BaseModel):
    name: str = "API Key"
    key: str = ""
    created_at: str | None = None


class CreateApiKeyRequest(BaseModel):
    name: str = "API Key"
    key: str | None = None


class UpdateApiKeyRequest(BaseModel):
    name: str


@protected_router.get("/api-keys", response_model=list[ApiKeyItemModel])
async def list_api_keys() -> list[ApiKeyItemModel]:
    """获取 config.yaml 中配置的 API Keys 列表。"""
    from aistudio_api.infrastructure.gateway.model_defaults import (
        get_configured_api_key_items,
    )

    items = get_configured_api_key_items()
    return [
        ApiKeyItemModel(
            name=item.get("name") or "API Key",
            key=item.get("key") or "",
            created_at=item.get("created_at") or None,
        )
        for item in items
    ]


@protected_router.post("/api-keys", response_model=ApiKeyItemModel)
async def create_api_key(req: CreateApiKeyRequest) -> ApiKeyItemModel:
    """在 config.yaml 中添加新的 API Key。"""
    import secrets
    from datetime import UTC, datetime

    new_key = (
        req.key.strip()
        if req.key and req.key.strip()
        else f"sk-aistudio-{secrets.token_hex(16)}"
    )
    created_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    name = req.name.strip() if req.name.strip() else "API Key"

    config_path = _resolve_config_path(None)

    def append_key(parsed: dict[str, object]) -> dict[str, object]:
        items = api_key_items(parsed.get("api_keys"))
        items.append({"name": name, "key": new_key, "created_at": created_at})
        parsed["api_keys"] = items
        return parsed

    mutate_config(config_path, append_key)
    invalidate_config_cache()

    logger.info("已创建新 API Key: 备注=%s", name)
    return ApiKeyItemModel(name=name, key=new_key, created_at=created_at)


@protected_router.delete("/api-keys/{key_value}")
async def delete_api_key(key_value: str) -> dict[str, bool]:
    """在 config.yaml 中删除指定 API Key。"""

    config_path = _resolve_config_path(None)

    def remove_key(parsed: dict[str, object]) -> dict[str, object]:
        items = api_key_items(parsed.get("api_keys"))
        remaining = [item for item in items if item["key"] != key_value]
        if len(remaining) == len(items):
            raise HTTPException(404, detail="未找到该 API Key")
        parsed["api_keys"] = remaining
        return parsed

    mutate_config(config_path, remove_key)
    invalidate_config_cache()

    logger.info("已删除 API Key")
    return {"ok": True}


@protected_router.put("/api-keys/{key_value}", response_model=ApiKeyItemModel)
async def update_api_key_name(
    key_value: str, req: UpdateApiKeyRequest
) -> ApiKeyItemModel:
    """在 config.yaml 中更新指定 API Key 的备注名。"""

    config_path = _resolve_config_path(None)
    new_name = req.name.strip() if req.name.strip() else "API Key"
    updated_item: ApiKeyItemModel | None = None

    def rename_key(parsed: dict[str, object]) -> dict[str, object]:
        nonlocal updated_item
        items = api_key_items(parsed.get("api_keys"))
        for item in items:
            if item["key"] == key_value:
                item["name"] = new_name
                updated_item = ApiKeyItemModel(
                    name=new_name,
                    key=key_value,
                    created_at=item["created_at"] or None,
                )
        if updated_item is None:
            raise HTTPException(404, detail="未找到该 API Key")
        parsed["api_keys"] = items
        return parsed

    mutate_config(config_path, rename_key)
    invalidate_config_cache()
    logger.info("API Key 备注名已更新: %s", new_name)

    assert updated_item is not None
    return updated_item


class ApiKeyReferenceRequest(BaseModel):
    key: str


class RenameApiKeyRequest(UpdateApiKeyRequest):
    key: str


@protected_router.delete("/api-keys")
async def delete_api_key_by_body(req: ApiKeyReferenceRequest) -> dict[str, bool]:
    return await delete_api_key(req.key)


@protected_router.put("/api-keys", response_model=ApiKeyItemModel)
async def rename_api_key_by_body(req: RenameApiKeyRequest) -> ApiKeyItemModel:
    return await update_api_key_name(req.key, req)
