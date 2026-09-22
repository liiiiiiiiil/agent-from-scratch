"""The deliberately small JSON Schema subset accepted by the MCP adapter.

MCP servers are external processes, so this module validates both the schema
that enters the parent tool registry and every argument object before it is
sent back to a server.  It intentionally does not reuse the project's more
permissive generic tool validator.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from typing import Any, Mapping


MAX_MCP_SCHEMA_BYTES = 64 * 1024
MAX_MCP_DIRECTORY_BYTES = 512 * 1024
MAX_MCP_AGENT_TOOLS = 128
MAX_MCP_SCHEMA_TEXT_CHARS = 4096

_SCALAR_TYPES = {"string", "integer", "number", "boolean", "null"}
_SCHEMA_METADATA = {"title", "description"}
_ROOT_KEYS = {"type", "properties", "required", "additionalProperties", *_SCHEMA_METADATA}
_PROPERTY_KEYS = {
    "type", "description", "title", "enum", "minLength", "maxLength",
    "minimum", "maximum", "minItems", "maxItems", "items",
}


def _schema_bytes(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise ValueError("MCP inputSchema 必须是有限、可编码的 JSON object") from error
    if len(encoded) > MAX_MCP_SCHEMA_BYTES:
        raise ValueError(f"MCP inputSchema 超过 {MAX_MCP_SCHEMA_BYTES} bytes 上限")
    return encoded


def _text_field(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value or len(value) > MAX_MCP_SCHEMA_TEXT_CHARS:
        raise ValueError(f"MCP schema 的 {field} 必须是有界非空字符串")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"MCP schema 的 {field} 含控制字符")


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, bool, int, float)) and not (
        isinstance(value, float) and not math.isfinite(value)
    )


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    return False


def _validate_enum(enum: Any, expected: str, field: str) -> None:
    if not isinstance(enum, list) or not enum:
        raise ValueError(f"MCP schema 的 {field}.enum 必须是非空数组")
    for item in enum:
        if not _is_scalar(item) or not _matches_type(item, expected):
            raise ValueError(f"MCP schema 的 {field}.enum 必须包含同一标量类型的值")


def _bound_number(value: Any, field: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"MCP schema 的 {field} 必须是有限数字")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"MCP schema 的 {field} 必须是有限数字")
    return value


def _bound_length(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"MCP schema 的 {field} 必须是非负整数")
    return value


def _validate_scalar_schema(schema: Mapping[str, Any], field: str) -> dict[str, Any]:
    unknown = set(schema) - _PROPERTY_KEYS
    if unknown:
        raise ValueError(f"MCP schema 的 {field} 含不支持的关键字: {sorted(unknown)}")
    expected = schema.get("type")
    if expected not in _SCALAR_TYPES:
        raise ValueError(f"MCP schema 的 {field}.type 必须是标量类型")
    for metadata in _SCHEMA_METADATA:
        if metadata in schema:
            _text_field(schema[metadata], f"{field}.{metadata}")
    result = deepcopy(dict(schema))
    if "enum" in schema:
        _validate_enum(schema["enum"], expected, field)
    if "items" in schema:
        raise ValueError(f"MCP schema 的标量属性 {field} 不能有 items")
    for bound in ("minLength", "maxLength"):
        if bound in schema:
            if expected != "string":
                raise ValueError(f"MCP schema 的 {field}.{bound} 只适用于 string")
            _bound_length(schema[bound], f"{field}.{bound}")
    for bound in ("minimum", "maximum"):
        if bound in schema:
            if expected not in {"integer", "number"}:
                raise ValueError(f"MCP schema 的 {field}.{bound} 只适用于 number/integer")
            _bound_number(schema[bound], f"{field}.{bound}")
    if "minLength" in schema and "maxLength" in schema and schema["minLength"] > schema["maxLength"]:
        raise ValueError(f"MCP schema 的 {field} 长度下限超过上限")
    if "minimum" in schema and "maximum" in schema and schema["minimum"] > schema["maximum"]:
        raise ValueError(f"MCP schema 的 {field} 数值下限超过上限")
    return result


def _validate_property(schema: Any, field: str) -> dict[str, Any]:
    if not isinstance(schema, Mapping):
        raise ValueError(f"MCP schema 的 {field} 必须是 object")
    expected = schema.get("type")
    if expected == "array":
        unknown = set(schema) - _PROPERTY_KEYS
        if unknown:
            raise ValueError(f"MCP schema 的 {field} 含不支持的关键字: {sorted(unknown)}")
        if "items" not in schema:
            raise ValueError(f"MCP schema 的数组属性 {field} 缺少 items")
        if not isinstance(schema["items"], Mapping):
            raise ValueError(f"MCP schema 的数组属性 {field}.items 必须是 object")
        item_schema = _validate_scalar_schema(schema["items"], f"{field}.items")
        for metadata in _SCHEMA_METADATA:
            if metadata in schema:
                _text_field(schema[metadata], f"{field}.{metadata}")
        result = deepcopy(dict(schema))
        result["items"] = item_schema
        if "enum" in schema:
            raise ValueError(f"MCP schema 的数组属性 {field} 不支持 enum")
        for bound in ("minItems", "maxItems"):
            if bound in schema:
                _bound_length(schema[bound], f"{field}.{bound}")
        if "minItems" in schema and "maxItems" in schema and schema["minItems"] > schema["maxItems"]:
            raise ValueError(f"MCP schema 的 {field} 数组长度下限超过上限")
        for bound in ("minLength", "maxLength", "minimum", "maximum"):
            if bound in schema:
                raise ValueError(f"MCP schema 的数组属性 {field} 不支持 {bound}")
        return result
    if expected == "object":
        raise ValueError(f"MCP schema 拒绝嵌套 object: {field}")
    return _validate_scalar_schema(schema, field)


def validate_mcp_schema(schema: Any) -> dict[str, Any]:
    """Validate and return a detached MCP input schema."""
    if not isinstance(schema, Mapping):
        raise ValueError("MCP inputSchema 必须是 object")
    _schema_bytes(schema)
    unknown = set(schema) - _ROOT_KEYS
    if unknown:
        raise ValueError(f"MCP inputSchema 含不支持的关键字: {sorted(unknown)}")
    if schema.get("type") != "object":
        raise ValueError("MCP inputSchema 根 type 必须是 object")
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        raise ValueError("MCP inputSchema.properties 必须是 object")
    for key in properties:
        if not isinstance(key, str) or not key or len(key) > 128 or "\x00" in key:
            raise ValueError("MCP inputSchema 属性名无效")
    required = schema.get("required", [])
    if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
        raise ValueError("MCP inputSchema.required 必须是字符串数组")
    if len(set(required)) != len(required):
        raise ValueError("MCP inputSchema.required 不能有重复项")
    unknown_required = set(required) - set(properties)
    if unknown_required:
        raise ValueError("MCP inputSchema.required 引用了未知属性")
    additional = schema.get("additionalProperties", False)
    if not isinstance(additional, bool):
        raise ValueError("MCP inputSchema.additionalProperties 必须是 bool")
    for metadata in _SCHEMA_METADATA:
        if metadata in schema:
            _text_field(schema[metadata], f"inputSchema.{metadata}")
    normalized = deepcopy(dict(schema))
    normalized["properties"] = {
        key: _validate_property(value, f"properties.{key}")
        for key, value in properties.items()
    }
    normalized["required"] = list(required)
    normalized["additionalProperties"] = additional
    encoded = _schema_bytes(normalized)
    if len(encoded) > MAX_MCP_SCHEMA_BYTES:
        raise ValueError(f"MCP inputSchema 超过 {MAX_MCP_SCHEMA_BYTES} bytes 上限")
    return normalized


def validate_mcp_arguments(schema: Mapping[str, Any], arguments: Any) -> dict[str, Any]:
    """Validate arguments against a previously validated MCP schema."""
    if not isinstance(arguments, dict) or any(not isinstance(key, str) for key in arguments):
        raise ValueError("MCP tool arguments 必须是 JSON object")
    try:
        json.dumps(arguments, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, UnicodeError) as error:
        raise ValueError("MCP tool arguments 必须是有限、可编码的 JSON object") from error
    properties = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        unknown = [key for key in arguments if key not in properties]
        if unknown:
            raise ValueError("MCP tool 未知参数: " + ", ".join(unknown))
    missing = [key for key in schema.get("required", []) if key not in arguments]
    if missing:
        raise ValueError("MCP tool 缺少必需参数: " + ", ".join(missing))
    normalized = deepcopy(arguments)
    for key, value in arguments.items():
        prop = properties.get(key)
        if prop is None:
            continue
        expected = prop["type"]
        if expected == "array":
            if not isinstance(value, list):
                raise ValueError(f"MCP 参数 {key} 类型应为 array")
            if "minItems" in prop and len(value) < prop["minItems"]:
                raise ValueError(f"MCP 参数 {key} 数组长度不足")
            if "maxItems" in prop and len(value) > prop["maxItems"]:
                raise ValueError(f"MCP 参数 {key} 数组长度超过上限")
            item_schema = prop["items"]
            for index, item in enumerate(value):
                _validate_value(item, item_schema, f"{key}[{index}]")
            continue
        _validate_value(value, prop, key)
    return normalized


def _validate_value(value: Any, schema: Mapping[str, Any], field: str) -> None:
    expected = schema["type"]
    if not _matches_type(value, expected):
        raise ValueError(f"MCP 参数 {field} 类型应为 {expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"MCP 参数 {field} 不在允许值中")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise ValueError(f"MCP 参数 {field} 长度不足")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ValueError(f"MCP 参数 {field} 超过长度上限")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"MCP 参数 {field} 小于允许下限")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"MCP 参数 {field} 超过允许上限")


__all__ = [
    "MAX_MCP_AGENT_TOOLS", "MAX_MCP_DIRECTORY_BYTES", "MAX_MCP_SCHEMA_BYTES",
    "validate_mcp_arguments", "validate_mcp_schema",
]
