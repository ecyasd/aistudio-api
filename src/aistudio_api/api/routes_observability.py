"""Authenticated metadata-only console endpoints."""

from fastapi import APIRouter, HTTPException, Query

from aistudio_api.infrastructure.utils import telemetry, telemetry_store

router = APIRouter(prefix="/api/console")


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
