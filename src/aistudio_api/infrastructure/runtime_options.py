"""Persisted engine selection, applied when the process starts."""

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

from aistudio_api.config import settings


class RuntimeOptions(BaseModel):
    engine: Literal["browserless", "browser"] = "browserless"


def saved_options() -> RuntimeOptions:
    config = (
        yaml.safe_load(Path(settings.config_file).read_text(encoding="utf-8")) or {}
    )
    return RuntimeOptions.model_validate(
        config.get("runtime") or {"engine": os.getenv("AISTUDIO_ENGINE", "browserless")}
    )


@lru_cache(maxsize=1)
def active_options() -> RuntimeOptions:
    return saved_options()
