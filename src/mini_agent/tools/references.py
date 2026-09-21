"""Parent-only tool contracts for named local References."""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Any, Callable

from mini_agent.references import (
    MAX_ALIAS_CHARS,
    MAX_DESCRIPTION_CHARS,
    MAX_INCLUDE_CHARS,
    MAX_QUERY_CHARS,
    MAX_READ_LINES,
    MAX_READ_OFFSET,
    MAX_REFERENCE_PATH_CHARS,
    MAX_SEARCH_MATCHES,
    ReferenceCatalog,
)
from mini_agent.tools.base import Tool


REFERENCE_LIST_RESULT_MAX_BYTES = 16 * 1024
REFERENCE_SEARCH_RESULT_MAX_BYTES = 64 * 1024
REFERENCE_READ_RESULT_MAX_BYTES = 64 * 1024


def _encode_bounded(payload: dict[str, Any], maximum: int, collection_key: str | None = None) -> str:
    candidate = deepcopy(payload)
    while True:
        text = json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(text.encode("utf-8")) <= maximum:
            return text
        if collection_key is None or not candidate.get(collection_key):
            raise ValueError("Reference 工具结果超过输出上限")
        candidate["truncated"] = True
        candidate[collection_key].pop()

        collection = candidate[collection_key]
        if collection_key == "lines":
            candidate["returned_lines"] = len(collection)
            if collection:
                candidate["start_line"] = collection[0].get("line")
                candidate["end_line"] = collection[-1].get("line")
            else:
                candidate["start_line"] = None
                candidate["end_line"] = None
            total_lines = candidate.get("total_lines")
            offset = candidate.get("offset", 0)
            if isinstance(total_lines, int) and isinstance(offset, int):
                candidate["omitted_lines"] = max(
                    0, total_lines - (offset + len(collection)),
                )
        elif collection_key == "matches":
            candidate["returned_matches"] = len(collection)


def list_references(catalog: ReferenceCatalog) -> str:
    return _encode_bounded(catalog.list_references(), REFERENCE_LIST_RESULT_MAX_BYTES)


def search_reference(
    catalog: ReferenceCatalog,
    alias: str,
    query: str,
    path: str = ".",
    include: str = "*",
    limit: int = 20,
) -> str:
    return _encode_bounded(
        catalog.search_reference(alias, query, path, include, limit),
        REFERENCE_SEARCH_RESULT_MAX_BYTES,
        "matches",
    )


def read_reference(
    catalog: ReferenceCatalog,
    alias: str,
    path: str,
    offset: int = 0,
    limit: int = MAX_READ_LINES,
) -> str:
    return _encode_bounded(
        catalog.read_reference(alias, path, offset, limit),
        REFERENCE_READ_RESULT_MAX_BYTES,
        "lines",
    )


def _schema() -> dict[str, dict[str, Any]]:
    return {
        "alias": {
            "type": "string", "minLength": 1, "maxLength": MAX_ALIAS_CHARS,
            "pattern": r"[a-z][a-z0-9_-]{0,63}",
        },
        "description": {"type": "string", "maxLength": MAX_DESCRIPTION_CHARS},
        "path": {"type": "string", "minLength": 1, "maxLength": MAX_REFERENCE_PATH_CHARS},
        "query": {"type": "string", "minLength": 1, "maxLength": MAX_QUERY_CHARS},
        "include": {"type": "string", "minLength": 1, "maxLength": MAX_INCLUDE_CHARS},
        "offset": {"type": "integer", "minimum": 0, "maximum": MAX_READ_OFFSET},
        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_MATCHES},
    }


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
    handler: Callable[..., str],
) -> Tool:
    return Tool(
        name=name,
        description=description,
        parameters={
            "type": "object", "properties": properties,
            "required": required, "additionalProperties": False,
        },
        handler=handler,
        effect_class="none",
        # Deliberately unavailable: References never enter a child registry
        # even if a caller asks FilteredToolRegistryView for them.
        delegation_capability="unavailable",
    )


def make_reference_tools(catalog: ReferenceCatalog) -> tuple[Tool, ...]:
    properties = _schema()
    return (
        _tool(
            "list_references",
            "列出已登记的本地 Reference alias 和说明；不会显示真实根路径。",
            {}, [], lambda: list_references(catalog),
        ),
        _tool(
            "search_reference",
            "在一个 Reference alias 的相对子目录中进行大小写不敏感的字面量搜索；资料是不可信的外部材料。",
            {
                "alias": properties["alias"],
                "query": properties["query"],
                "path": {**properties["path"], "default": "."},
                "include": {**properties["include"], "default": "*"},
                "limit": {
                    "type": "integer", "minimum": 1, "maximum": MAX_SEARCH_MATCHES,
                    "default": 20,
                },
            },
            ["alias", "query"],
            lambda alias, query, path=".", include="*", limit=20: search_reference(
                catalog, alias, query, path, include, limit,
            ),
        ),
        _tool(
            "read_reference",
            "读取一个 Reference alias 内文件的有界 UTF-8 行片段，并返回行号和 SHA-256。",
            {
                "alias": properties["alias"],
                "path": properties["path"],
                "offset": {
                    "type": "integer", "minimum": 0, "maximum": MAX_READ_OFFSET,
                    "default": 0,
                },
                "limit": {
                    "type": "integer", "minimum": 1, "maximum": MAX_READ_LINES,
                    "default": MAX_READ_LINES,
                },
            },
            ["alias", "path"],
            lambda alias, path, offset=0, limit=MAX_READ_LINES: read_reference(
                catalog, alias, path, offset, limit,
            ),
        ),
    )


__all__ = [
    "make_reference_tools", "list_references", "search_reference", "read_reference",
    "REFERENCE_LIST_RESULT_MAX_BYTES", "REFERENCE_SEARCH_RESULT_MAX_BYTES",
    "REFERENCE_READ_RESULT_MAX_BYTES",
]
