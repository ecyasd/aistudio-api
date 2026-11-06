"""AIS2-style usage reports from the bounded, content-free request ledger."""

from __future__ import annotations

import math
from collections import defaultdict

from pydantic import BaseModel, Field


class UsageRow(BaseModel):
    id: str
    started_at: float
    model: str | None = None
    protocol: str = "gemini"
    channel: str = "playground"
    engine: str = "browserless"
    state: str
    status: int = 0
    error_status: int | None = None
    duration_ms: int = 0
    stream: bool = False
    timings: dict[str, int] = Field(default_factory=dict)
    usage: dict[str, int] = Field(default_factory=dict)


def percentile(values: list[int], fraction: float) -> int | None:
    return (
        sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]
        if values
        else None
    )


def summarize(rows: list[UsageRow]) -> dict[str, int | float | None]:
    first = [
        row.timings["first_body"]
        for row in rows
        if row.stream and "first_body" in row.timings
    ]
    return {
        "requests": len(rows),
        "success": sum(row.state == "completed" for row in rows),
        "failed": sum(row.state == "failed" for row in rows),
        "cancelled": sum(row.state == "cancelled" for row in rows),
        "rate_limited": sum((row.error_status or row.status) == 429 for row in rows),
        "reported_usage": sum("total_tokens" in row.usage for row in rows),
        "input_tokens": sum(row.usage.get("prompt_tokens", 0) for row in rows),
        "output_tokens": sum(row.usage.get("completion_tokens", 0) for row in rows),
        "cached_tokens": sum(row.usage.get("cached_tokens", 0) for row in rows),
        "total_tokens": sum(row.usage.get("total_tokens", 0) for row in rows),
        "first_body_p50": percentile(first, 0.5),
        "first_body_p95": percentile(first, 0.95),
        "duration_p50": percentile([row.duration_ms for row in rows], 0.5),
        "duration_p95": percentile([row.duration_ms for row in rows], 0.95),
    }


def build_report(
    rows: list[UsageRow],
    *,
    start: float,
    end: float,
    model: str = "",
    protocol: str = "",
    state: str = "",
    channel: str = "",
    page: int = 1,
) -> dict:
    rows = [row for row in rows if row.state in {"completed", "failed", "cancelled"}]
    options = {
        "models": sorted({row.model for row in rows if row.model}),
        "channels": sorted({row.channel for row in rows}),
    }
    selected = [
        row
        for row in rows
        if (not model or row.model == model)
        and (not protocol or row.protocol == protocol)
        and (not state or row.state == state)
        and (not channel or row.channel == channel)
    ]
    current = [row for row in selected if start <= row.started_at < end]
    previous = [
        row for row in selected if start - (end - start) <= row.started_at < start
    ]
    seconds = 300 if end - start <= 3600 else 3600 if end - start <= 86400 else 21600
    grouped: dict[int, list[UsageRow]] = defaultdict(list)
    by_model: dict[str, list[UsageRow]] = defaultdict(list)
    for row in current:
        grouped[int((row.started_at - start) // seconds)].append(row)
        by_model[row.model or "未识别模型"].append(row)
    buckets = [
        {"at": start + index * seconds, **summarize(grouped[index])}
        for index in range(math.ceil((end - start) / seconds))
    ]
    ranking = sorted(
        ({"model": key, **summarize(value)} for key, value in by_model.items()),
        key=lambda item: int(item["requests"] or 0),
        reverse=True,
    )
    page_size = 50
    pages = max(1, math.ceil(len(current) / page_size))
    page = min(page, pages)
    records = sorted(current, key=lambda row: row.started_at, reverse=True)
    return {
        "from": start,
        "to": end,
        "bucket_seconds": seconds,
        "totals": summarize(current),
        "previous": summarize(previous),
        "buckets": buckets,
        "ranking": ranking,
        "options": options,
        "records": [
            row.model_dump()
            for row in records[(page - 1) * page_size : page * page_size]
        ],
        "page": page,
        "pages": pages,
    }
