"""Authenticated management access to the live recent-error window."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Response

from aistudio_api.infrastructure.utils.error_records import get_error_record_store
from aistudio_api.infrastructure.utils.response_diagnostics import (
    get_response_diagnostic_store,
)

router = APIRouter()
_CATEGORIES = {
    "session_expired", "authentication", "rate_limit", "timeout", "browser",
    "network", "validation", "upstream", "internal", "cancelled",
}


@router.get("/errors")
async def list_errors(
    response: Response,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    status: Annotated[int | None, Query(ge=400, le=599)] = None,
    category: Annotated[str | None, Query(max_length=32)] = None,
    cursor: Annotated[str | None, Query(pattern=r"^[a-f0-9]{32}$")] = None,
    include_diagnostics: bool = False,
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = "Authorization"
    if category is not None and category not in _CATEGORIES:
        raise HTTPException(status_code=400, detail="未知的错误分类")
    try:
        result = get_error_record_store().list_records(
            limit=limit, status=status, category=category, cursor=cursor,
        )
        if include_diagnostics:
            result["diagnostics"] = get_response_diagnostic_store().list_records(limit=limit)
        return result
    except OSError as exc:
        raise HTTPException(status_code=503, detail="错误记录暂时不可用") from exc
