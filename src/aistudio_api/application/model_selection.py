"""Client-visible continuation selectors, independent of upstream model identity."""

from __future__ import annotations

import re
from dataclasses import dataclass

ANTI_TRUNCATION_PREFIX = "抗截断/"
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_NON_TEXT_TOKENS = frozenset(
    {"image", "audio", "tts", "embedding", "embeddings", "video", "live"}
)


@dataclass(frozen=True)
class ModelSelection:
    base_model: str
    auto_continue: bool = False


def supports_continuation_model(model: str) -> bool:
    """Advertise variants only for recognizable text-generation model IDs."""
    clean = model.removeprefix("models/").lower()
    return (
        bool(_MODEL_ID.fullmatch(clean))
        and clean.startswith(("gemini-", "gemma-"))
        and not _NON_TEXT_TOKENS.intersection(clean.split("-"))
    )


def parse_model_selection(raw: str) -> ModelSelection:
    clean = raw.removeprefix("models/")
    enabled = clean.startswith(ANTI_TRUNCATION_PREFIX)
    if enabled:
        clean = clean.removeprefix(ANTI_TRUNCATION_PREFIX)
        if not supports_continuation_model(clean) or "/" in clean:
            raise ValueError("Anti-truncation requires a text Gemini or Gemma model")
    return ModelSelection(base_model=f"models/{clean}", auto_continue=enabled)


def model_quota_key(raw: str) -> str:
    """Keep selectors in the real model's bucket, including persisted old keys."""
    # Do not validate historical keys while loading owner statistics. Request
    # admission validates selectors separately before any account is acquired.
    return raw.removeprefix("models/").removeprefix(ANTI_TRUNCATION_PREFIX).lower()
