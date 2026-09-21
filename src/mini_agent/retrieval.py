"""Small, deterministic lexical retrieval over workspace Memory records."""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Any

from mini_agent.memory import MemoryStore, MemoryValidationError


DEFAULT_SEARCH_LIMIT = 5
MAX_SEARCH_LIMIT = 10
MAX_QUERY_CHARS = 1200
MAX_SNIPPET_CHARS = 240
SOURCE_STATUS_UNVERIFIED = "unverified"

_FIELD_ORDER = ("title", "tags", "source", "body")
_FIELD_WEIGHTS = {
    "title": 16,
    "tags": 10,
    "source": 6,
    "body": 2,
}
_PHRASE_BONUS = 8
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")


def normalize_text(value: str) -> str:
    """Apply the retrieval-only Unicode and case normalization contract."""
    return unicodedata.normalize("NFKC", value).casefold()


def _tokens(value: str, *, include_cjk_chars: bool = False) -> list[str]:
    """Return continuous Latin/digit/code tokens and useful CJK fragments."""
    normalized = normalize_text(value)
    result: list[str] = []
    for match in re.finditer(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+|[a-z0-9]+(?:[_-][a-z0-9]+)*", normalized):
        chunk = match.group(0)
        if _CJK_RE.fullmatch(chunk):
            result.append(chunk)
            if len(chunk) > 1:
                result.extend(chunk[index:index + 2] for index in range(len(chunk) - 1))
            if include_cjk_chars:
                result.extend(chunk)
        else:
            result.append(chunk)
    return result


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _validate_query(query: Any) -> str:
    if not isinstance(query, str):
        raise MemoryValidationError("query 必须是字符串")
    if len(query) > MAX_QUERY_CHARS:
        raise MemoryValidationError(f"query 超过 {MAX_QUERY_CHARS} 字符上限")
    return query


def _validate_limit(limit: Any) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise MemoryValidationError("limit 必须是整数")
    if not 1 <= limit <= MAX_SEARCH_LIMIT:
        raise MemoryValidationError(f"limit 必须是 1 到 {MAX_SEARCH_LIMIT} 的整数")
    return limit


@dataclass(frozen=True)
class MemorySearchResult:
    """A bounded search response before it is rendered by a tool or Context."""

    query: str
    memories: tuple[dict[str, Any], ...]
    total_matches: int

    @property
    def returned(self) -> int:
        return len(self.memories)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "memories": [dict(item) for item in self.memories],
            "total_matches": self.total_matches,
            "returned": self.returned,
        }


def _field_values(record: dict[str, Any]) -> dict[str, list[str]]:
    return {
        "title": [record.get("title", "")],
        "tags": list(record.get("tags", [])),
        "source": [record.get("source", "")],
        "body": [record.get("body", "")],
    }


def _snippet(body: str, query_terms: list[str]) -> str:
    if not body:
        return ""
    normalized = normalize_text(body)
    positions = [position for term in query_terms if term
                 for position in (normalized.find(term),) if position >= 0]
    if not positions:
        return body[:MAX_SNIPPET_CHARS]
    center = _normalized_offset_to_original(body, min(positions))
    half_before = min(100, center)
    start = max(0, center - half_before)
    end = min(len(body), start + MAX_SNIPPET_CHARS)
    start = max(0, end - MAX_SNIPPET_CHARS)
    return body[start:end]


def _normalized_offset_to_original(value: str, offset: int) -> int:
    """Map a normalized-string offset to a safe raw-string boundary.

    NFKC and casefold can expand one source character into several normalized
    characters.  Prefix normalization is bounded by Memory's 2000-character
    body limit and keeps snippets anchored to the original text.
    """
    if offset <= 0:
        return 0
    for index in range(1, len(value) + 1):
        if len(normalize_text(value[:index])) > offset:
            return index - 1
    return len(value)


class MemoryRetriever:
    """Read and rank the current MemoryStore snapshot on every search."""

    def __init__(self, store: MemoryStore):
        if not isinstance(store, MemoryStore):
            raise TypeError("MemoryRetriever 需要 MemoryStore")
        self.store = store

    def search(self, query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> list[dict[str, Any]]:
        """Return only ranked memories, retaining a simple list API."""
        return list(self.search_with_total(query, limit=limit).memories)

    def search_with_total(
        self, query: str, limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> MemorySearchResult:
        query = _validate_query(query)
        limit = _validate_limit(limit)
        normalized_query = normalize_text(query).strip()
        if not normalized_query:
            return MemorySearchResult(query=query, memories=(), total_matches=0)

        query_terms = _unique(_tokens(normalized_query))
        if not query_terms:
            return MemorySearchResult(query=query, memories=(), total_matches=0)
        records = self.store.snapshot()
        ranked: list[tuple[int, str, str, dict[str, Any], list[str]]] = []
        for record in records:
            values = _field_values(record)
            matched_fields: list[str] = []
            score = 0
            for field in _FIELD_ORDER:
                field_text = " ".join(str(item) for item in values[field])
                field_normalized = normalize_text(field_text)
                field_tokens = set(_tokens(field_text, include_cjk_chars=True))
                hits = [term for term in query_terms if term in field_tokens]
                if hits:
                    matched_fields.append(field)
                    score += _FIELD_WEIGHTS[field] * len(hits)
                if normalized_query in field_normalized:
                    score += _PHRASE_BONUS
            if not matched_fields:
                continue
            item = {
                "memory_id": record["memory_id"],
                "title": record["title"],
                "snippet": _snippet(record["body"], query_terms),
                "source": record["source"],
                "source_status": SOURCE_STATUS_UNVERIFIED,
                "updated_at": record["updated_at"],
                "score": score,
                "matched_fields": matched_fields,
            }
            ranked.append((score, record["updated_at"], record["memory_id"], item, matched_fields))

        # Stable passes make the three-level ordering explicit: ID ascending,
        # then timestamp descending, then score descending.
        ranked.sort(key=lambda entry: entry[2])
        ranked.sort(key=lambda entry: entry[1], reverse=True)
        ranked.sort(key=lambda entry: entry[0], reverse=True)
        memories = tuple(entry[3] for entry in ranked[:limit])
        return MemorySearchResult(
            query=query, memories=memories, total_matches=len(ranked),
        )


__all__ = [
    "DEFAULT_SEARCH_LIMIT", "MAX_SEARCH_LIMIT", "MAX_QUERY_CHARS",
    "MAX_SNIPPET_CHARS", "SOURCE_STATUS_UNVERIFIED", "MemorySearchResult",
    "MemoryRetriever", "normalize_text",
]
