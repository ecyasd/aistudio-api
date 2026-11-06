"""Authenticated metadata-only console endpoints."""

import asyncio
import os
import signal
import time
import uuid
from pathlib import Path

import yaml
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from aistudio_api.api.dependencies import get_runtime_state
from aistudio_api.api.state import RuntimeState
from aistudio_api.config import settings
from aistudio_api.infrastructure.runtime_options import (
    RuntimeOptions,
    active_options,
    saved_options,
)
from aistudio_api.infrastructure.utils import telemetry, telemetry_store
from aistudio_api.infrastructure.utils.common import atomic_write_text
from aistudio_api.infrastructure.utils.usage_report import UsageRow, build_report

router = APIRouter(prefix="/api/console")
_instance_id = uuid.uuid4().hex


@router.get("/runtime")
def runtime_options():
    saved, active = saved_options(), active_options()
    return {
        "saved": saved,
        "active": active,
        "restart_required": saved != active,
        "restart_enabled": os.getenv("AISTUDIO_ALLOW_RESTART") == "1",
        "instance_id": _instance_id,
    }


@router.put("/runtime")
async def update_runtime_options(options: RuntimeOptions):
    active_options()
    path = Path(settings.config_file)
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    config["runtime"] = options.model_dump()
    atomic_write_text(path, yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
    return runtime_options()


async def restart_process() -> None:
    await asyncio.sleep(1)
    os.kill(os.getpid(), signal.SIGTERM)


@router.post("/restart", status_code=202)
async def restart_service(background_tasks: BackgroundTasks):
    if os.getenv("AISTUDIO_ALLOW_RESTART") != "1":
        raise HTTPException(409, "当前部署未启用受控重启")
    if telemetry.active:
        raise HTTPException(409, "仍有进行中的请求，请等待完成或先停止请求")
    telemetry.restarting = True
    background_tasks.add_task(restart_process)
    return {"ok": True, "message": "服务正在重启"}


@router.get("/cooldowns")
async def cooldowns_snapshot(runtime_state: RuntimeState = Depends(get_runtime_state)):
    accounts = runtime_state.rotator.get_all_stats() if runtime_state.rotator else {}
    items: list[dict[str, object]] = []
    for account_id, account in accounts.items():
        cooldowns = account.get("model_cooldowns")
        scopes = dict(cooldowns) if isinstance(cooldowns, dict) else {}
        remaining = account.get("cooldown_remaining")
        if isinstance(remaining, int) and remaining > 0:
            scopes["*"] = remaining
        for model, seconds in scopes.items():
            if isinstance(seconds, int) and seconds > 0:
                items.append(
                    {
                        "id": f"{account_id}:{model}",
                        "account_id": account_id,
                        "account_label": account.get("name") or account_id,
                        "model": model,
                        "remaining": seconds,
                        "reason": "限流冷却",
                    }
                )
    return {
        "items": sorted(
            items, key=lambda item: int(str(item["remaining"])), reverse=True
        )
    }


@router.get("/usage")
async def usage_report(
    start: float = Query(alias="from", ge=0),
    end: float = Query(alias="to", ge=0),
    model: str = Query(default="", max_length=128),
    protocol: str = Query(default="", pattern="^(|gemini|openai)$"),
    state: str = Query(default="", pattern="^(|completed|failed|cancelled)$"),
    channel: str = Query(default="", pattern="^(|playground|assistant)$"),
    page: int = Query(default=1, ge=1),
):
    if not 0 < end - start <= 31 * 86400:
        raise HTTPException(400, "时间范围须大于零且不超过 31 天")
    stored = telemetry_store.recent("request", telemetry_store.MAX_REQUESTS)
    snapshots = {item["id"]: item for item in stored}
    snapshots.update(
        {
            item.id: item.snapshot()
            for item in telemetry.completed
            if item.started_at >= time.time() - telemetry_store.retention_seconds()
        }
    )
    report = build_report(
        [UsageRow.model_validate(item) for item in snapshots.values()],
        start=start,
        end=end,
        model=model,
        protocol=protocol,
        state=state,
        channel=channel,
        page=page,
    )
    return {
        **report,
        "retention_days": telemetry_store.retention_seconds() // 86400,
        "max_records": telemetry_store.MAX_REQUESTS
        if telemetry_store.enabled()
        else telemetry.MAX_COMPLETED,
        "stored_records": len(snapshots),
    }


@router.get("/requests")
async def requests_snapshot():
    telemetry.prune()
    stored = telemetry_store.recent("request", telemetry.MAX_COMPLETED)
    snapshots = {item["id"]: item for item in stored}
    snapshots.update({item.id: item.snapshot() for item in telemetry.completed})
    snapshots.update({item.id: item.snapshot() for item in telemetry.active.values()})
    return {
        "items": sorted(
            snapshots.values(),
            key=lambda item: float(str(item["started_at"])),
            reverse=True,
        ),
        "retention_seconds": telemetry.RETENTION_SECONDS,
        "max_completed": telemetry.MAX_COMPLETED,
    }


@router.post("/requests/{request_id}/cancel")
async def cancel_request(request_id: str):
    item = telemetry.active.get(request_id)
    if item is None or item.task is None or item.task.done():
        raise HTTPException(409, "Request is no longer active")
    item.state = "cancelled"
    item.task.cancel()
    return {"ok": True}


@router.get("/requests/{request_id}")
async def request_detail(request_id: str):
    telemetry.prune()
    if item := telemetry.active.get(request_id):
        return item.snapshot()
    for item in reversed(telemetry.completed):
        if item.id == request_id:
            return item.snapshot()
    for stored in telemetry_store.recent("request", telemetry_store.MAX_REQUESTS):
        if stored["id"] == request_id:
            return stored
    raise HTTPException(404, "请求记录已过期或不存在")


@router.get("/logs")
async def logs_snapshot(after: int = Query(default=0, ge=0)):
    telemetry.prune()
    return {
        "items": list(reversed(telemetry_store.recent("event", telemetry.MAX_LOGS)))
        if telemetry_store.enabled()
        else [item for item in telemetry.logs if int(str(item["id"])) > after],
        "retention_seconds": telemetry.RETENTION_SECONDS,
        "max_records": telemetry.MAX_LOGS,
    }
