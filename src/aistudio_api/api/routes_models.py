"""Models and metadata routes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException

from aistudio_api.api.dependencies import get_runtime_state
from aistudio_api.api.response_models import (
    GeminiModelListResponse,
    GeminiModelResponse,
)
from aistudio_api.application.model_selection import (
    ANTI_TRUNCATION_PREFIX,
    parse_model_selection,
    supports_continuation_model,
)
from aistudio_api.infrastructure.gateway.model_defaults import resolve_model_defaults
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("routes.models")

if TYPE_CHECKING:
    from aistudio_api.application.runtime import RuntimeState
router = APIRouter()


def _continuation_variant(
    model_data: Mapping[str, object],
) -> dict[str, object] | None:
    raw_id = str(model_data.get("id") or model_data.get("name") or "")
    clean = raw_id.removeprefix("models/")
    if not supports_continuation_model(clean):
        return None
    if any(
        model_data.get(key) is True
        for key in ("is_image_model", "is_video_model", "is_audio_model")
    ) or model_data.get("category") in ("image", "video", "audio"):
        return None
    if resolve_model_defaults(clean).is_image_model:
        return None
    variant = dict(model_data)
    variant["id"] = f"{ANTI_TRUNCATION_PREFIX}{clean}"
    variant["displayName"] = f"抗截断 · {model_data.get('displayName') or clean}"
    variant["description"] = (
        "本代理的可选有界续写：文本以 MAX_TOKENS 结束时，最多追加两轮，"
        "保留思考内容和每个 Part 的签名。额外轮次消耗额外请求额度和 Token；"
        "实际工具、图片或结构化结果不强行续写，不保证任意回答完整。"
    )
    return variant


def _to_gemini_model(model_data: Mapping[str, object]) -> GeminiModelResponse:
    raw_id = str(model_data.get("id") or model_data.get("name") or "")
    clean_id = raw_id.removeprefix("models/")
    name = f"models/{clean_id}"
    display_name = str(model_data.get("displayName") or clean_id)
    description = str(model_data.get("description") or f"Google {display_name} model")
    input_tokens = int(str(model_data.get("inputTokenLimit") or 1048576))
    output_tokens = int(str(model_data.get("outputTokenLimit") or 8192))
    # Advertise the capability implemented by this gateway, including its
    # streaming transport, rather than unsupported upstream API methods.
    methods = ["generateContent"]
    return GeminiModelResponse(
        name=name,
        version="001",
        displayName=display_name,
        description=description,
        inputTokenLimit=input_tokens,
        outputTokenLimit=output_tokens,
        supportedGenerationMethods=methods,
        temperature=1.0,
        topP=0.95,
        topK=64,
    )


@router.get("/v1beta/models", response_model=GeminiModelListResponse)
async def list_models(
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> GeminiModelListResponse:
    session = runtime_state.client._session if runtime_state.client else None
    from aistudio_api.infrastructure.gateway.model_discovery import model_discovery

    discovered = await model_discovery.get_models(session=session)
    models: list[GeminiModelResponse] = []
    seen: set[str] = set()
    for item in discovered:
        candidates = [item]
        variant = _continuation_variant(item)
        if variant is not None:
            candidates.append(variant)
        for candidate in candidates:
            model = _to_gemini_model(candidate)
            if model.name not in seen:
                seen.add(model.name)
                models.append(model)
    logger.info("获取模型列表完成 (共发现 %d 个)", len(models))
    return GeminiModelListResponse(models=models)


@router.get("/v1beta/models/{model_id:path}", response_model=GeminiModelResponse)
async def get_model(
    model_id: str,
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> GeminiModelResponse:
    session = runtime_state.client._session if runtime_state.client else None
    from aistudio_api.infrastructure.gateway.model_discovery import model_discovery

    try:
        selection = parse_model_selection(model_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Model not found") from exc
    model_info = await model_discovery.get_model_by_id(
        selection.base_model, session=session
    )
    if model_info is not None:
        if selection.auto_continue:
            model_info = _continuation_variant(model_info)
            if model_info is None:
                raise HTTPException(status_code=404, detail="Model not found")
        logger.info("成功获取模型元数据: %s", model_id)
        return _to_gemini_model(model_info)
    raise HTTPException(status_code=404, detail="Model not found")
