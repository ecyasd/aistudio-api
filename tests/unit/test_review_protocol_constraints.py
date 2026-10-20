"""Offline request regressions for explicit zero schema bounds and their aliases."""

from __future__ import annotations

import pytest

from aistudio_api.api.schemas import GeminiGenerateContentRequest
from aistudio_api.application.chat_service import normalize_gemini_request
from aistudio_api.infrastructure.gateway.wire_spec import SchemaIndex


@pytest.mark.parametrize(
    ("schema_type", "camel_name", "snake_name", "index"),
    [
        ("object", "maxProperties", "max_properties", SchemaIndex.MAX_PROPERTIES),
        ("object", "minProperties", "min_properties", SchemaIndex.MIN_PROPERTIES),
        ("string", "maxLength", "max_length", SchemaIndex.MAX_LENGTH),
        ("string", "minLength", "min_length", SchemaIndex.MIN_LENGTH),
        ("array", "maxItems", "max_items", SchemaIndex.MAX_ITEMS),
        ("array", "minItems", "min_items", SchemaIndex.MIN_ITEMS),
    ],
)
@pytest.mark.parametrize("alias_style", ["camel", "snake", "zero_precedence", "null_fallback"])
@pytest.mark.parametrize("schema_location", ["response", "function"])
def test_zero_schema_bound_reaches_upstream_wire(
    schema_type, camel_name, snake_name, index, alias_style, schema_location,
):
    field_name = snake_name if alias_style in {"snake", "null_fallback"} else camel_name
    constrained = {"type": schema_type, field_name: 0}
    if alias_style == "zero_precedence":
        constrained[snake_name] = 5
    if alias_style == "null_fallback":
        constrained[camel_name] = None
    schema = {"type": "object", "properties": {"empty_value": constrained}}
    payload = {"contents": [{"parts": [{"text": "synthetic schema request"}]}]}
    if schema_location == "response":
        payload["generationConfig"] = {
            "responseMimeType": "application/json", "responseSchema": schema,
        }
    else:
        payload["tools"] = [{"functionDeclarations": [{
            "name": "synthetic_function", "parameters": schema,
        }]}]
    normalized = normalize_gemini_request(
        GeminiGenerateContentRequest.model_validate(payload), "models/gemini-3.5-flash",
    )
    if schema_location == "response":
        wire = normalized.generation_config_overrides["response_schema"]
    else:
        wire = normalized.tools[0][1][0][2]
    property_schema = wire[SchemaIndex.PROPERTIES][0][1]
    assert len(property_schema) > index, (
        "An explicit zero bound must not be omitted or replaced by another alias"
    )
    assert property_schema[index] == 0
