"""Provider-neutral response and usage contracts.

Adapters stop at this boundary.  They never execute tools, mutate State, or
decide whether an agent is complete.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Any, Literal, Mapping, Protocol
import math
import re


MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_ERROR_BYTES = 16 * 1024
MAX_SSE_LINE_BYTES = 2 * 1024 * 1024
MAX_CONTENT_CHARS = 4 * 1024 * 1024
MAX_TOOL_ARGUMENT_CHARS = 512 * 1024


class ProviderProtocolError(RuntimeError):
    """The provider returned a structurally unusable response."""


class ProviderHTTPError(RuntimeError):
    """A bounded, credential-free provider HTTP failure."""

    def __init__(self, status: int, reason: str = "", detail: str = "") -> None:
        self.status = status
        self.reason = _safe_text(reason, 160)
        self.detail = _safe_text(detail, 1000)
        message = f"服务商 HTTP {status}"
        if self.reason:
            message += f" {self.reason}"
        if self.detail:
            message += f": {self.detail}"
        super().__init__(message)


class ProviderConnectionError(ProviderHTTPError):
    """A connection/timeout failure without the endpoint in its message."""

    def __init__(self, detail: str = "服务商连接失败") -> None:
        self.status = 0
        self.reason = "连接失败"
        self.detail = _safe_text(detail, 200)
        RuntimeError.__init__(self, f"服务商连接失败: {self.detail}")


class ProviderTimeoutError(ProviderConnectionError, TimeoutError):
    """A provider request timed out without exposing its endpoint."""


class ProviderStreamError(ProviderProtocolError):
    """The provider stream ended or changed shape before a complete response."""


def _safe_text(value: Any, limit: int = 1000, redactions: tuple[str, ...] = ()) -> str:
    text = " ".join(str(value or "").split())
    for secret in redactions:
        if secret:
            text = text.replace(secret, "<redacted>")
    text = re.sub(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+", r"\1<redacted>", text)
    text = re.sub(r"(?i)(x-api-key\s*[:=]\s*)[^\s,;]+", r"\1<redacted>", text)
    text = re.sub(r"(?i)\bsk-[A-Za-z0-9_./+=-]{8,}", "<redacted-key>", text)
    return text[:limit]


@dataclass(frozen=True)
class ProviderUsage:
    input_tokens: int
    output_tokens: int
    source: Literal["provider", "estimated", "mixed"]

    def __post_init__(self) -> None:
        for name in ("input_tokens", "output_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"usage.{name} 必须是非负整数")
        if self.source not in {"provider", "estimated", "mixed"}:
            raise ValueError("usage.source 必须是 provider、estimated 或 mixed")


@dataclass(frozen=True)
class ProviderResponse:
    """The only response shape that crosses from an adapter into Runtime."""

    message: dict[str, Any]
    finish_reason: str | None
    usage: ProviderUsage

    def __post_init__(self) -> None:
        if not isinstance(self.message, dict):
            raise TypeError("ProviderResponse.message 必须是 dict")
        if self.message.get("role", "assistant") != "assistant":
            raise ProviderProtocolError("provider 响应 role 必须是 assistant")
        if self.finish_reason is not None and not isinstance(self.finish_reason, str):
            raise ProviderProtocolError("provider finish_reason 类型非法")


class ProviderAdapter(Protocol):
    """Protocol adapter surface used by the canonical Runtime."""

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        include_tools: bool = True,
        tool_registry: Any = None,
        stream_output: bool = False,
        on_content: Any = None,
        timeout: float | None = None,
        strict_tool_calls: bool = True,
    ) -> ProviderResponse: ...


def merge_provider_usage(*items: ProviderUsage | None) -> ProviderUsage:
    """Combine usage fragments while preserving conservative source semantics."""
    present = [item for item in items if item is not None]
    if not present:
        return ProviderUsage(0, 0, "estimated")
    source = "provider" if all(item.source == "provider" for item in present) else (
        "estimated" if all(item.source == "estimated" for item in present) else "mixed"
    )
    return ProviderUsage(
        sum(item.input_tokens for item in present),
        sum(item.output_tokens for item in present),
        source,
    )


class UsageMeter:
    """Thread-safe usage ledger shared by a binding and its summarizer."""

    def __init__(self) -> None:
        self._lock = Lock()
        self.llm_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self._sources: list[str] = []

    def record(
        self,
        usage: ProviderUsage | None,
        *,
        estimated_input_tokens: int = 0,
        estimated_output_tokens: int = 0,
    ) -> ProviderUsage:
        if usage is None or usage.source == "estimated":
            actual = ProviderUsage(
                max(0, int(estimated_input_tokens)),
                max(0, int(estimated_output_tokens)),
                "estimated" if usage is None else usage.source,
            )
        elif usage.source == "mixed":
            # A mixed response carries provider values where available.  A
            # zero field is the adapter's bounded marker for missing usage;
            # fill only that side from the conservative local estimate.
            actual = ProviderUsage(
                usage.input_tokens or max(0, int(estimated_input_tokens)),
                usage.output_tokens or max(0, int(estimated_output_tokens)),
                "mixed",
            )
        else:
            actual = usage
        with self._lock:
            self.llm_calls += 1
            self.input_tokens += actual.input_tokens
            self.output_tokens += actual.output_tokens
            self._sources.append(actual.source)
        return actual

    def snapshot(self) -> dict[str, int | str]:
        with self._lock:
            sources = tuple(self._sources)
            source = "estimated" if not sources else (
                sources[0] if all(item == sources[0] for item in sources) else "mixed"
            )
            return {
                "llm_calls": self.llm_calls,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "token_accounting": source,
                "source_count": len(self._sources),
            }

    def delta(self, snapshot: Mapping[str, int | str]) -> dict[str, int | str]:
        current = self.snapshot()
        with self._lock:
            recent = self._sources[int(snapshot.get("source_count", 0)):]
        source = "estimated" if not recent else (
            recent[0] if all(item == recent[0] for item in recent) else "mixed"
        )
        return {
            "llm_calls": int(current["llm_calls"]) - int(snapshot.get("llm_calls", 0)),
            "input_tokens": int(current["input_tokens"]) - int(snapshot.get("input_tokens", 0)),
            "output_tokens": int(current["output_tokens"]) - int(snapshot.get("output_tokens", 0)),
            "token_accounting": source,
        }


def validate_limit(value: Any, name: str, *, integer: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是正数")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{name} 必须是有限正数")
    if value <= 0:
        raise ValueError(f"{name} 必须是正数")
    if integer and not isinstance(value, int):
        raise ValueError(f"{name} 必须是正整数")
