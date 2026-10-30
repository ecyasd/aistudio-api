"""Model-specific default behaviors for AI Studio wire requests."""

from __future__ import annotations

import math
import os
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from aistudio_api.config import get_config_file_path, resolve_config_file
from aistudio_api.infrastructure.utils.config_store import api_key_items, mutate_config

from .wire_types import (
    AistudioImageOutputMode,
    AistudioThinkingConfig,
    ImageOutputType,
    MediaResolution,
    ThinkingLevel,
)

_resolve_config_path = resolve_config_file
_SAFETY_CATEGORY_ORDER = (
    ("harassment", 7),
    ("hate", 8),
    ("sexually explicit", 9),
    ("dangerous content", 10),
)


@dataclass(frozen=True)
class ModelDefaults:
    is_image_model: bool = False
    default_tools: tuple[str, ...] = ()
    generation_config_defaults: dict[str, object] = field(default_factory=dict)
    clear_generation_config_indexes: tuple[int, ...] = ()
    safety_settings: tuple[tuple[object, ...], ...] | None = None
    disable_safety_settings: bool = False
    drop_unsupported_params: bool = False
    specified_fields: frozenset[str] = frozenset()

    def generation_config_overrides(self) -> dict[str, object]:
        return dict(self.generation_config_defaults)

    def safety_settings_overrides(self) -> list[list[object]] | None:
        if self.safety_settings is None:
            return None
        return [list(item) for item in self.safety_settings]

    def merged(self, other: ModelDefaults) -> ModelDefaults:
        merged_generation = dict(self.generation_config_defaults)
        merged_generation.update(other.generation_config_defaults)
        merged_tools = self.default_tools + tuple(
            tool for tool in other.default_tools if tool not in self.default_tools
        )
        merged_indexes = self.clear_generation_config_indexes + tuple(
            idx
            for idx in other.clear_generation_config_indexes
            if idx not in self.clear_generation_config_indexes
        )
        return ModelDefaults(
            is_image_model=self.is_image_model or other.is_image_model,
            default_tools=merged_tools,
            generation_config_defaults=merged_generation,
            clear_generation_config_indexes=merged_indexes,
            safety_settings=other.safety_settings
            if other.safety_settings is not None
            else self.safety_settings,
            disable_safety_settings=self.disable_safety_settings
            or other.disable_safety_settings,
            drop_unsupported_params=(
                other.drop_unsupported_params
                if "drop_unsupported_params" in other.specified_fields
                else self.drop_unsupported_params
            ),
            specified_fields=self.specified_fields | other.specified_fields,
        )

    def overridden(self, other: ModelDefaults) -> ModelDefaults:
        merged_generation = dict(self.generation_config_defaults)
        if "generation_config_defaults" in other.specified_fields:
            merged_generation.update(other.generation_config_defaults)
        return ModelDefaults(
            is_image_model=other.is_image_model
            if "is_image_model" in other.specified_fields
            else self.is_image_model,
            default_tools=other.default_tools
            if "default_tools" in other.specified_fields
            else self.default_tools,
            generation_config_defaults=merged_generation,
            clear_generation_config_indexes=(
                other.clear_generation_config_indexes
                if "clear_generation_config_indexes" in other.specified_fields
                else self.clear_generation_config_indexes
            ),
            safety_settings=other.safety_settings
            if "safety_settings" in other.specified_fields
            else self.safety_settings,
            disable_safety_settings=(
                other.disable_safety_settings
                if "disable_safety_settings" in other.specified_fields
                else self.disable_safety_settings
            ),
            drop_unsupported_params=(
                other.drop_unsupported_params
                if "drop_unsupported_params" in other.specified_fields
                else self.drop_unsupported_params
            ),
            specified_fields=self.specified_fields | other.specified_fields,
        )


@dataclass(frozen=True)
class ModelProfile:
    name: str
    exact: tuple[str, ...] = ()
    prefixes: tuple[str, ...] = ()
    contains: tuple[str, ...] = ()
    defaults: ModelDefaults = field(default_factory=ModelDefaults)

    def matches(self, model: str) -> bool:
        return bool(
            (self.exact and model in self.exact)
            or any(model.startswith(prefix) for prefix in self.prefixes)
            or any(token in model for token in self.contains)
        )


def _normalize_model_name(model: str) -> str:
    return model.removeprefix("models/").lower()


def _coerce_thinking_level(value: object) -> ThinkingLevel:
    if isinstance(value, ThinkingLevel):
        return value
    if isinstance(value, int):
        return ThinkingLevel(value)
    return ThinkingLevel[str(value).strip().upper()]


def _coerce_media_resolution(value: object) -> int:
    if isinstance(value, MediaResolution):
        return int(value)
    if isinstance(value, int):
        return value
    return int(MediaResolution[str(value).strip().upper()])


def _coerce_image_output_mode(value: object) -> AistudioImageOutputMode | list[int]:
    if isinstance(value, AistudioImageOutputMode):
        return value
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        output_type = ImageOutputType(
            int(value.get("output_type", ImageOutputType.IMAGE))
        )
        include_text = bool(value.get("include_text"))
        return AistudioImageOutputMode(
            output_type=output_type, include_text=include_text
        )
    label = str(value).strip().lower()
    if label == "text_and_image":
        return AistudioImageOutputMode.text_and_image()
    if label == "image_only":
        return AistudioImageOutputMode.image_only()
    raise ValueError(f"Unsupported image_output_mode: {value!r}")


def _coerce_generation_defaults(raw: dict[str, object] | None) -> dict[str, object]:
    raw = raw or {}
    coerced: dict[str, object] = {}
    for key, value in raw.items():
        if value is None:
            coerced[key] = None
        elif key == "thinking_config" and isinstance(value, dict):
            level = _coerce_thinking_level(value.get("level", ThinkingLevel.HIGH))
            mode = int(value.get("mode", 1))
            coerced[key] = AistudioThinkingConfig(level=level, mode=mode).to_wire()
        elif key == "image_output_mode":
            coerced[key] = _coerce_image_output_mode(value)
        elif key == "media_resolution":
            coerced[key] = _coerce_media_resolution(value)
        else:
            coerced[key] = value
    return coerced


def _normalize_safety_category(name: object) -> str:
    return " ".join(
        str(name).strip().lower().replace("_", " ").replace("-", " ").split()
    )


def _coerce_safety_threshold(value: object) -> int:
    threshold = int(str(value))
    if threshold < 1 or threshold > 5:
        raise ValueError(f"Unsupported safety threshold: {value!r}")
    return threshold


def _coerce_safety_settings(raw: object) -> tuple[tuple[object, ...], ...] | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        normalized = {
            _normalize_safety_category(name): _coerce_safety_threshold(value)
            for name, value in raw.items()
        }
        supported = {name for name, _ in _SAFETY_CATEGORY_ORDER}
        unknown = [name for name in normalized if name not in supported]
        if unknown:
            raise ValueError(f"Unsupported safety categories: {', '.join(unknown)}")
        return tuple(
            (None, None, category, normalized[name])
            for name, category in _SAFETY_CATEGORY_ORDER
            if name in normalized
        )
    if isinstance(raw, (list, tuple)):
        settings = []
        for item in raw:
            if not isinstance(item, (list, tuple)) or len(item) < 4:
                raise ValueError(f"Unsupported safety setting entry: {item!r}")
            settings.append(
                (None, None, int(item[2]), _coerce_safety_threshold(item[3]))
            )
        return tuple(settings)
    raise ValueError(f"Unsupported safety_settings: {raw!r}")


def _defaults_from_mapping(raw: dict[str, object] | None) -> ModelDefaults:
    raw_dict = raw or {}
    raw_default_tools = raw_dict.get("default_tools")
    default_tools_list = (
        raw_default_tools if isinstance(raw_default_tools, (list, tuple)) else ()
    )
    raw_gen_defaults = raw_dict.get("generation_config_defaults")
    gen_defaults = raw_gen_defaults if isinstance(raw_gen_defaults, dict) else None
    raw_clear_indexes = raw_dict.get("clear_generation_config_indexes")
    clear_indexes_list = (
        raw_clear_indexes if isinstance(raw_clear_indexes, (list, tuple)) else ()
    )
    return ModelDefaults(
        is_image_model=bool(raw_dict.get("is_image_model", False)),
        default_tools=tuple(str(x) for x in default_tools_list),
        generation_config_defaults=_coerce_generation_defaults(gen_defaults),
        clear_generation_config_indexes=tuple(int(str(v)) for v in clear_indexes_list),
        safety_settings=_coerce_safety_settings(raw_dict.get("safety_settings")),
        disable_safety_settings=bool(raw_dict.get("disable_safety_settings", False)),
        drop_unsupported_params=bool(raw_dict.get("drop_unsupported_params", False)),
        specified_fields=frozenset(raw_dict.keys()),
    )


def _profile_from_mapping(raw: dict[str, object]) -> ModelProfile:
    raw_match = raw.get("match")
    match: dict[str, object] = raw_match if isinstance(raw_match, dict) else {}
    raw_exact = match.get("exact")
    exact_list = raw_exact if isinstance(raw_exact, (list, tuple)) else ()
    raw_prefixes = match.get("prefixes")
    prefixes_list = raw_prefixes if isinstance(raw_prefixes, (list, tuple)) else ()
    raw_contains = match.get("contains")
    contains_list = raw_contains if isinstance(raw_contains, (list, tuple)) else ()
    return ModelProfile(
        name=str(raw.get("name", "unnamed")),
        exact=tuple(_normalize_model_name(str(v)) for v in exact_list),
        prefixes=tuple(_normalize_model_name(str(v)) for v in prefixes_list),
        contains=tuple(str(v).strip().lower() for v in contains_list),
        defaults=_defaults_from_mapping(raw),
    )


def _default_config() -> dict[str, object]:
    return {
        "logging": {
            "level": "INFO",
            "dump_requests": False,
        },
        "model_defaults": {
            "drop_unsupported_params": False,
            "profiles": [
                {
                    "name": "image_models",
                    "match": {"contains": ["image"]},
                    "is_image_model": True,
                    "generation_config_defaults": {
                        "response_mime_type": None,
                        "image_output_mode": "image_only",
                        "thinking_config": {"level": "MINIMAL", "mode": 1},
                    },
                    "clear_generation_config_indexes": [7, 13, 17],
                    "disable_safety_settings": True,
                },
                {
                    "name": "gemma_models",
                    "match": {"prefixes": ["gemma-"]},
                    "default_tools": ["google_search"],
                    "safety_settings": {
                        "Harassment": 5,
                        "Hate": 5,
                        "Sexually Explicit": 5,
                        "Dangerous Content": 5,
                    },
                },
                {
                    "name": "gemini_models",
                    "match": {"prefixes": ["gemini-"]},
                    "safety_settings": {
                        "Harassment": 5,
                        "Hate": 5,
                        "Sexually Explicit": 5,
                        "Dangerous Content": 5,
                    },
                },
            ],
            "models": {},
        },
    }


def validate_model_config(config: dict[str, object]) -> None:
    """Validate known model fields before saving; retain extension fields.

    Reuse the runtime compiler for enums and supported legacy wire forms so a
    profile that does not match the current model cannot poison every request.
    """
    defaults = config.get("model_defaults")
    if defaults is None:
        return
    if not isinstance(defaults, dict):
        raise ValueError("model_defaults must be a mapping")

    def validate_rule(rule: object) -> None:
        if not isinstance(rule, dict):
            raise ValueError("model rule must be a mapping")
        for key in ("default_tools", "clear_generation_config_indexes"):
            if rule.get(key) is not None and not isinstance(rule[key], (list, tuple)):
                raise ValueError("model rule list field must be a list")
        match = rule.get("match")
        if match is not None:
            if not isinstance(match, dict):
                raise ValueError("match must be a mapping")
            for key in ("exact", "prefixes", "contains"):
                if match.get(key) is not None and not isinstance(match[key], (list, tuple)):
                    raise ValueError("match rule must be a list")
        generation = rule.get("generation_config_defaults")
        if generation is not None:
            if not isinstance(generation, dict):
                raise ValueError("generation defaults must be a mapping")
            for key in ("temperature", "top_p", "top_k", "max_tokens", "max_output_tokens"):
                value = generation.get(key)
                if value is not None and (
                    type(value) not in (int, float) or not math.isfinite(value)
                    or (key in ("top_k", "max_tokens", "max_output_tokens") and type(value) is not int)
                ):
                    raise ValueError("generation numeric field is invalid")
            thinking = generation.get("thinking_config")
            if thinking is not None and not isinstance(thinking, (dict, list)):
                raise ValueError("thinking config must be a mapping or wire list")
            if isinstance(thinking, list):
                if len(thinking) < 4 or type(thinking[0]) is not int:
                    raise ValueError("thinking config wire list is invalid")
                _coerce_thinking_level(thinking[3])
        _defaults_from_mapping(rule)

    profiles = defaults.get("profiles")
    if profiles is not None:
        if not isinstance(profiles, list):
            raise ValueError("profiles must be a list")
        for profile in profiles:
            validate_rule(profile)
    models = defaults.get("models")
    if models is not None:
        if not isinstance(models, dict):
            raise ValueError("models must be a mapping")
        for rule in models.values():
            # Null overrides have always meant an empty rule.
            if rule is not None:
                validate_rule(rule)


ConfigStamp = tuple[int, int, int, int] | None
_CONFIG_CACHE: dict[str, tuple[ConfigStamp, dict[str, object]]] = {}
_API_KEYS_CACHE: dict[str, tuple[ConfigStamp, frozenset[str], list[dict[str, str]]]] = {}
_COMPILED_RULES_CACHE: OrderedDict[
    str, tuple[dict[str, object], tuple[ModelProfile, ...], dict[str, ModelDefaults]]
] = OrderedDict()


def _config_stamp(path: Path) -> ConfigStamp:
    try:
        info = path.stat()
        return info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
    except OSError:
        return None


def invalidate_config_cache() -> None:
    """Clear in-memory configuration and API keys cache."""
    _CONFIG_CACHE.clear()
    _API_KEYS_CACHE.clear()
    _COMPILED_RULES_CACHE.clear()


def _read_yaml_config(config_path: Path) -> dict[str, object]:
    """Pure file read for both runtime snapshots and uncached previews."""
    try:
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return _default_config()
    return _with_builtin_defaults(loaded if isinstance(loaded, dict) else {})


def _load_yaml_config(config_path: Path) -> dict[str, object]:
    if not config_path.exists():
        return _default_config()

    path_key = str(config_path.resolve())
    mtime = _config_stamp(config_path)

    cached = _CONFIG_CACHE.get(path_key)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    merged = _read_yaml_config(config_path)
    _CONFIG_CACHE[path_key] = (mtime, merged)
    return merged


def _with_builtin_defaults(loaded: dict[str, object]) -> dict[str, object]:
    merged = _default_config()
    raw_defaults = merged["model_defaults"]
    raw_loaded = loaded.get("model_defaults")
    merged.update(loaded)
    if isinstance(raw_defaults, dict) and isinstance(raw_loaded, dict):
        merged["model_defaults"] = {**raw_defaults, **raw_loaded}
    return merged


def _profiles(config: dict[str, object]) -> tuple[ModelProfile, ...]:
    raw_md = config.get("model_defaults")
    raw_profiles = raw_md.get("profiles") if isinstance(raw_md, dict) else None
    return tuple(
        _profile_from_mapping(item) for item in (raw_profiles or []) if isinstance(item, dict)
    ) if isinstance(raw_profiles, list) else ()


def _overrides(config: dict[str, object]) -> dict[str, ModelDefaults]:
    raw_md = config.get("model_defaults")
    raw_models = raw_md.get("models") if isinstance(raw_md, dict) else None
    return {
        _normalize_model_name(str(name)): _defaults_from_mapping(
            rule if isinstance(rule, dict) else {}
        ) for name, rule in raw_models.items()
    } if isinstance(raw_models, dict) else {}


def _compiled_rules(
    config_path: str, config: dict[str, object],
) -> tuple[tuple[ModelProfile, ...], dict[str, ModelDefaults]]:
    cached = _COMPILED_RULES_CACHE.get(config_path)
    if cached is not None and cached[0] is config:
        _COMPILED_RULES_CACHE.move_to_end(config_path)
        return cached[1], cached[2]
    # Compile both collections from the exact document already read by the
    # caller. A file replacement between these steps cannot mix generations.
    profiles, overrides = _profiles(config), _overrides(config)
    _COMPILED_RULES_CACHE[config_path] = config, profiles, overrides
    _COMPILED_RULES_CACHE.move_to_end(config_path)
    if len(_COMPILED_RULES_CACHE) > 8:
        _COMPILED_RULES_CACHE.popitem(last=False)
    return profiles, overrides


def resolve_model_defaults(
    model: str, *, config_path: str | os.PathLike[str] | None = None
) -> ModelDefaults:
    resolved_path = str(_resolve_config_path(config_path))
    config = _load_yaml_config(Path(resolved_path))
    profiles, overrides = _compiled_rules(resolved_path, config)
    return _resolve_rules(model, config, profiles, overrides)[0]


def preview_model_defaults(
    model: str, *, config: dict[str, object] | None = None,
) -> tuple[ModelDefaults, list[dict[str, object]]]:
    """Resolve a draft without writing files or changing the runtime cache."""
    document = (_read_yaml_config(get_config_file_path()) if config is None
                else _with_builtin_defaults(config))
    validate_model_config(document)
    return _resolve_rules(model, document, _profiles(document), _overrides(document))


def _resolve_rules(
    model: str, config: dict[str, object], profiles: tuple[ModelProfile, ...],
    overrides: dict[str, ModelDefaults],
) -> tuple[ModelDefaults, list[dict[str, object]]]:
    normalized = _normalize_model_name(model)
    raw_md = config.get("model_defaults")
    global_drop = False
    if isinstance(raw_md, dict) and "drop_unsupported_params" in raw_md:
        global_drop = bool(raw_md["drop_unsupported_params"])
    elif "drop_unsupported_params" in config:
        global_drop = bool(config["drop_unsupported_params"])

    matched: list[dict[str, object]] = []
    resolved = ModelDefaults(drop_unsupported_params=global_drop)
    for index, profile in enumerate(profiles):
        if profile.matches(normalized):
            resolved = resolved.merged(profile.defaults)
            matched.append({"kind": "profile", "name": profile.name, "index": index})
    override = overrides.get(normalized)
    if override is not None:
        resolved = resolved.overridden(override)
        matched.append({"kind": "model", "name": normalized})

    if resolved.is_image_model:
        allowed = {"google_search", "image_search", "google_search_and_image_search"}
        filtered = tuple(t for t in resolved.default_tools if t in allowed)
        if filtered != resolved.default_tools:
            from dataclasses import replace

            resolved = replace(resolved, default_tools=filtered)

    return resolved, matched


def get_configured_api_key_items(
    config_path: str | os.PathLike[str] | None = None,
) -> list[dict[str, str]]:
    """获取 config.yaml 中配置的结构化 API Key 列表（含备注名与创建时间）。"""
    resolved_path = _resolve_config_path(config_path)
    path_key = str(resolved_path.resolve())
    mtime = _config_stamp(resolved_path)

    cached = _API_KEYS_CACHE.get(path_key)
    if cached is not None and cached[0] == mtime:
        return list(cached[2])

    config = _load_yaml_config(resolved_path)
    items = api_key_items(config.get("api_keys"))

    keys_set = frozenset(item["key"] for item in items if item.get("key"))
    _API_KEYS_CACHE[path_key] = (mtime, keys_set, items)
    return items


def get_configured_api_keys(
    config_path: str | os.PathLike[str] | None = None,
) -> frozenset[str]:
    """获取 config.yaml 中配置的所有有效 API Key 集合。"""
    resolved_path = _resolve_config_path(config_path)
    path_key = str(resolved_path.resolve())
    mtime = _config_stamp(resolved_path)

    cached = _API_KEYS_CACHE.get(path_key)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    items = get_configured_api_key_items(config_path)
    return frozenset(item["key"] for item in items if item.get("key"))


def get_configured_logging_settings(
    config_path: str | os.PathLike[str] | None = None,
) -> dict[str, object]:
    """获取 config.yaml 中配置的 logging 设置。"""
    resolved_path = _resolve_config_path(config_path)
    config = _load_yaml_config(resolved_path)
    raw_logging = config.get("logging")
    if isinstance(raw_logging, dict):
        return {
            "level": str(raw_logging.get("level") or "INFO").strip().upper(),
            "dump_requests": raw_logging.get("dump_requests") is True,
            "dump_to_file": raw_logging.get("dump_to_file") is True,
            "dump_dir": str(raw_logging.get("dump_dir") or "").strip(),
        }
    return {
        "level": "INFO",
        "dump_requests": False,
        "dump_to_file": False,
        "dump_dir": "",
    }


def update_configured_logging_settings(
    level: str | None = None,
    dump_requests: bool | None = None,
    dump_to_file: bool | None = None,
    dump_dir: str | None = None,
    config_path: str | os.PathLike[str] | None = None,
) -> dict[str, object]:
    """更新 config.yaml 中的 logging 设置并刷新缓存。"""
    resolved_path = _resolve_config_path(config_path)
    def update_logging(parsed: dict[str, object]) -> dict[str, object]:
        raw_logging = parsed.get("logging")
        logging_dict = dict(raw_logging) if isinstance(raw_logging, dict) else {}
        if level is not None:
            logging_dict["level"] = level.strip().upper()
        if dump_requests is not None:
            logging_dict["dump_requests"] = dump_requests
        if dump_to_file is not None:
            logging_dict["dump_to_file"] = dump_to_file
        if dump_dir is not None:
            logging_dict["dump_dir"] = dump_dir
        parsed["logging"] = logging_dict
        return parsed

    stored = mutate_config(resolved_path, update_logging)
    invalidate_config_cache()
    raw_logging = stored["logging"]
    return dict(raw_logging) if isinstance(raw_logging, dict) else {}
