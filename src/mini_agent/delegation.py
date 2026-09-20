"""Contracts and the synchronous, read-only v0.34 subagent runtime.

The parent agent owns the workspace and its mutable State.  This module only
creates a bounded child context and returns one frozen, JSON-serializable
report to the parent tool call.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import fnmatch
import hashlib
import inspect
import json
import ntpath
import os
import re
import socket
from threading import Event, Lock
import time
from typing import Any, Callable, Literal
from uuid import uuid4

from mini_agent.context import ContextBudget, ContextManager, count_tokens
from mini_agent.instructions import InstructionLoader
from mini_agent.permission import ALLOW, PermissionGate, PermissionPolicy
from mini_agent.prompt import build_subagent_prompt
from mini_agent.providers.base import ProviderResponse
from mini_agent.providers.catalog import ModelBinding, ModelBindingRef, ProviderCatalog
from mini_agent.runtime import AgentRuntime, RuntimeDecision, ToolRoundPlan
from mini_agent.state import AgentState
from mini_agent.tools.base import (
    ExecutionResult,
    ToolExecutor,
    ToolRegistry,
)


ALLOWED_SUBAGENT_TOOLS = frozenset({"calculate", "read_file", "list_dir", "grep"})
SUBAGENT_ALLOWED_TOOLS = ALLOWED_SUBAGENT_TOOLS
DELEGATION_PURPOSES = frozenset({"investigation", "diagnosis", "crash_investigation"})
DELEGATION_MAX_SCOPE = 8
DELEGATION_MAX_LIST_ITEMS = 32
DELEGATION_MAX_STRING = 2000
DELEGATION_MAX_GOAL = 4000
DELEGATION_MAX_RESULT_BYTES = 12 * 1024
DELEGATION_MIN_RESULT_BYTES = 1024
DELEGATION_MAX_ROUNDS = 8
DELEGATION_MAX_LLM_CALLS = 8
DELEGATION_MAX_TOOL_CALLS = 24
DELEGATION_MAX_TOKENS = 32_000
DELEGATION_MAX_TIMEOUT = 120
_HASH_RE = re.compile(r"\A[0-9a-fA-F]{64}\Z")
_SENSITIVE_RE = re.compile(
    r"(?i)(api[_ -]?key|authorization|bearer\s+|access[_ -]?token|secret[_ -]?key|password|\btoken\b|\bsecret\b)"
)


class DelegationError(ValueError):
    """A contract, scope, or result contract error."""


class DelegationBusy(DelegationError):
    """The v0.34 manager already has a child running."""


def _require_string(value: Any, field_name: str, *, max_length: int = DELEGATION_MAX_STRING) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DelegationError(f"{field_name} 必须是非空字符串")
    value = value.strip()
    if len(value) > max_length:
        raise DelegationError(f"{field_name} 超过长度上限")
    return value


def _bounded_string_list(value: Any, field_name: str, *, max_items: int = DELEGATION_MAX_LIST_ITEMS,
                         max_length: int = DELEGATION_MAX_STRING,
                         allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not allow_empty and not value):
        adjective = "字符串数组" if allow_empty else "非空字符串数组"
        raise DelegationError(f"{field_name} 必须是{adjective}")
    if len(value) > max_items:
        raise DelegationError(f"{field_name} 超过数量上限")
    result = tuple(_require_string(item, f"{field_name}[{index}]", max_length=max_length)
                   for index, item in enumerate(value))
    if len(set(result)) != len(result):
        raise DelegationError(f"{field_name} 不能含重复项")
    return result


@dataclass(frozen=True)
class SubagentBudget:
    max_rounds: int = DELEGATION_MAX_ROUNDS
    max_llm_calls: int = DELEGATION_MAX_LLM_CALLS
    max_tool_calls: int = DELEGATION_MAX_TOOL_CALLS
    max_tokens: int = DELEGATION_MAX_TOKENS
    max_result_bytes: int = DELEGATION_MAX_RESULT_BYTES
    timeout_seconds: int = DELEGATION_MAX_TIMEOUT
    token_accounting: str = "estimated"

    def __post_init__(self) -> None:
        limits = {
            "max_rounds": DELEGATION_MAX_ROUNDS,
            "max_llm_calls": DELEGATION_MAX_LLM_CALLS,
            "max_tool_calls": DELEGATION_MAX_TOOL_CALLS,
            "max_tokens": DELEGATION_MAX_TOKENS,
            "max_result_bytes": DELEGATION_MAX_RESULT_BYTES,
            "timeout_seconds": DELEGATION_MAX_TIMEOUT,
        }
        for name, limit in limits.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise DelegationError(f"budget.{name} 必须是正整数")
            if value > limit:
                raise DelegationError(f"budget.{name} 不能超过 v0.34 上限 {limit}")
        if self.max_result_bytes < DELEGATION_MIN_RESULT_BYTES:
            raise DelegationError(
                f"budget.max_result_bytes 不能小于 {DELEGATION_MIN_RESULT_BYTES}"
            )
        if self.token_accounting not in {"provider", "estimated", "mixed"}:
            raise DelegationError("token_accounting 必须是 provider、estimated 或 mixed")

    @classmethod
    def from_request(cls, value: Any) -> "SubagentBudget":
        if value is None:
            return cls()
        if not isinstance(value, dict):
            raise DelegationError("budget 必须是对象")
        allowed = {
            "max_rounds", "max_llm_calls", "max_tool_calls", "max_tokens",
            "max_result_bytes", "timeout_seconds",
        }
        unknown = set(value) - allowed
        if unknown:
            raise DelegationError("budget 含未知字段: " + ", ".join(sorted(unknown)))
        values = {name: value.get(name, getattr(cls(), name)) for name in allowed}
        return cls(**values)


class ScopeGate:
    """Validate contract scope and every child workspace-reader path."""

    def __init__(self, workspace_root: str | os.PathLike[str], scope: list[str] | tuple[str, ...]):
        self.workspace_root = os.path.realpath(os.path.abspath(os.fspath(workspace_root)))
        if not os.path.isdir(self.workspace_root):
            raise DelegationError("workspace_root 必须是目录")
        if not isinstance(scope, (list, tuple)) or not 1 <= len(scope) <= DELEGATION_MAX_SCOPE:
            raise DelegationError("scope 必须包含 1–8 个相对路径")
        self.scope = tuple(self._validate_relative(item, "scope") for item in scope)
        self._scope_realpaths = tuple(self._resolve(item) for item in self.scope)
        for path in self._scope_realpaths:
            self._require_inside_workspace(path)

    @staticmethod
    def _validate_relative(value: Any, field_name: str) -> str:
        value = _require_string(value, field_name, max_length=500)
        if os.path.isabs(value) or ntpath.isabs(value):
            raise DelegationError(f"{field_name} 不允许绝对路径")
        parts = value.replace("\\", "/").split("/")
        if any(part == ".." for part in parts):
            raise DelegationError(f"{field_name} 不允许 ..")
        if any(part == "config_local.py" for part in parts):
            raise DelegationError(f"{field_name} 不允许访问 config_local.py")
        return value

    def _resolve(self, relative: str) -> str:
        return os.path.realpath(os.path.abspath(os.path.join(self.workspace_root, relative)))

    def _require_inside_workspace(self, path: str) -> None:
        try:
            inside = os.path.commonpath([self.workspace_root, path]) == self.workspace_root
        except ValueError:
            inside = False
        if not inside:
            raise DelegationError("scope 不能逃逸工作区")

    def _inside_scope(self, path: str) -> bool:
        return any(
            os.path.commonpath([scope, path]) == scope
            for scope in self._scope_realpaths
            if self._safe_commonpath(scope, path)
        )

    @staticmethod
    def _safe_commonpath(left: str, right: str) -> bool:
        try:
            os.path.commonpath([left, right])
            return True
        except ValueError:
            return False

    def validate_path(self, path: Any, *, default: str = ".") -> str:
        value = default if path is None else path
        relative = self._validate_relative(value, "path")
        resolved = self._resolve(relative)
        self._require_inside_workspace(resolved)
        if not self._inside_scope(resolved):
            raise DelegationError("path 不在委派 scope 内")
        return resolved

    def validate_tool_call(self, name: str, arguments: dict[str, Any]) -> None:
        if name not in ALLOWED_SUBAGENT_TOOLS:
            raise DelegationError(f"工具 {name} 不在 Subagent 白名单")
        if name in {"read_file", "list_dir", "grep"}:
            self.validate_path(arguments.get("path"), default=".")

    def normalize_tool_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.validate_tool_call(name, arguments)
        result = deepcopy(arguments)
        if name in {"read_file", "list_dir", "grep"}:
            result["path"] = self.validate_path(result.get("path"), default=".")
        return result

    def validate_evidence_path(self, path: str) -> str:
        self.validate_path(path)
        return self._validate_relative(path, "evidence.path")

    def wrap_handler(self, name: str, handler: Callable) -> Callable:
        """Keep broad directory readers from exposing the local config file."""
        if name == "list_dir":
            def safe_list_dir(path="."):
                rendered = handler(path)
                if not isinstance(rendered, str):
                    return rendered
                return "\n".join(
                    line for line in rendered.splitlines()
                    if line.strip().rstrip("/") != "config_local.py"
                )
            return safe_list_dir
        if name != "grep":
            return handler

        def safe_grep(pattern: str, path=".", include="*"):
            regex = re.compile(pattern)
            results: list[str] = []
            max_results = 100
            for root, dirs, files in os.walk(path):
                dirs[:] = [directory for directory in dirs if (
                    directory != "config_local.py"
                    and self._safe_commonpath(self.workspace_root, os.path.realpath(os.path.join(root, directory)))
                    and self._inside_scope(os.path.realpath(os.path.join(root, directory)))
                )]
                for filename in sorted(files):
                    if filename == "config_local.py" or not fnmatch.fnmatch(filename, include):
                        continue
                    file_path = os.path.realpath(os.path.join(root, filename))
                    if not self._inside_scope(file_path):
                        continue
                    try:
                        with open(file_path, "r", encoding="utf-8", errors="ignore") as stream:
                            for line_number, line in enumerate(stream, 1):
                                if regex.search(line):
                                    display_path = os.path.relpath(file_path, self.workspace_root)
                                    results.append(f"{display_path}:{line_number}: {line.rstrip()}")
                                    if len(results) >= max_results:
                                        return "\n".join(results) + f"\n(结果已达 {max_results} 条上限)"
                    except (PermissionError, OSError):
                        continue
            return "\n".join(results) if results else "无匹配"

        return safe_grep


@dataclass(frozen=True)
class EvidenceRef:
    evidence_id: str
    kind: Literal["file_location", "tool_observation"]
    claim: str
    path: str | None = None
    line: int | None = None
    tool: str | None = None
    observation_hash: str | None = None

    @property
    def id(self) -> str:
        return self.evidence_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.evidence_id,
            "kind": self.kind,
            "claim": self.claim,
            "path": self.path,
            "line": self.line,
            "tool": self.tool,
            "observation_hash": self.observation_hash,
        }


@dataclass(frozen=True)
class Finding:
    finding_id: str
    claim: str
    evidence_ids: tuple[str, ...] = ()
    confidence: Literal["observed", "inferred"] = "observed"
    caveat: str | None = None

    @property
    def id(self) -> str:
        return self.finding_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.finding_id,
            "claim": self.claim,
            "evidence_ids": list(self.evidence_ids),
            "confidence": self.confidence,
            "caveat": self.caveat,
        }


@dataclass(frozen=True, init=False)
class UsageRecord:
    rounds: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    token_accounting: str = "estimated"
    elapsed_ms: int = 0
    result_bytes: int = 0

    def __init__(self, rounds: int = 0, llm_calls: int = 0, tool_calls: int = 0,
                 tokens: int | None = None, token_accounting: str = "estimated",
                 result_bytes: int = 0, elapsed_ms: int = 0, *,
                 input_tokens: int | None = None, output_tokens: int | None = None):
        """Accept the historical aggregate ``tokens`` argument and canonical split counters."""
        if input_tokens is None:
            input_tokens = 0 if tokens is None else tokens
        if output_tokens is None:
            output_tokens = 0
        for name, value in {
            "rounds": rounds, "llm_calls": llm_calls, "tool_calls": tool_calls,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "result_bytes": result_bytes, "elapsed_ms": elapsed_ms,
        }.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"usage.{name} 必须是非负整数")
        object.__setattr__(self, "rounds", rounds)
        object.__setattr__(self, "llm_calls", llm_calls)
        object.__setattr__(self, "tool_calls", tool_calls)
        object.__setattr__(self, "input_tokens", input_tokens)
        object.__setattr__(self, "output_tokens", output_tokens)
        object.__setattr__(self, "token_accounting", token_accounting)
        object.__setattr__(self, "elapsed_ms", elapsed_ms)
        object.__setattr__(self, "result_bytes", result_bytes)

    @property
    def tokens(self) -> int:
        """Backward-compatible aggregate token estimate."""
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "rounds": self.rounds,
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "token_accounting": self.token_accounting,
            "elapsed_ms": self.elapsed_ms,
            "result_bytes": self.result_bytes,
        }


@dataclass(frozen=True)
class DelegatedTask:
    delegation_id: str
    subagent_id: str
    parent_task_id: str
    parent_generation_id: int
    goal: str
    scope: tuple[str, ...]
    constraints: tuple[str, ...]
    expected_findings: tuple[str, ...]
    requested_tools: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    selected_parent_facts: tuple[str, ...]
    purpose: Literal["investigation", "diagnosis", "crash_investigation"]
    source_id: str | None
    budget: SubagentBudget
    depth: int = 1
    contract_hash: str = ""
    created_at: str = ""
    model_profile: str | None = None
    model_binding_ref: ModelBindingRef | None = None

    def __post_init__(self) -> None:
        if self.depth != 1:
            raise DelegationError("v0.34 depth 固定为 1")
        if self.purpose not in DELEGATION_PURPOSES:
            raise DelegationError("未知委派 purpose")
        if (not isinstance(self.requested_tools, tuple)
                or any(not isinstance(tool, str) for tool in self.requested_tools)
                or not self.requested_tools
                or len(set(self.requested_tools)) != len(self.requested_tools)):
            raise DelegationError("requested_tools 必须非空且唯一")
        if any(tool not in ALLOWED_SUBAGENT_TOOLS for tool in self.requested_tools):
            raise DelegationError("requested_tools 含未知或越权工具")
        if (not isinstance(self.allowed_tools, tuple)
                or any(not isinstance(tool, str) for tool in self.allowed_tools)
                or not self.allowed_tools
                or len(set(self.allowed_tools)) != len(self.allowed_tools)
                or not set(self.allowed_tools).issubset(self.requested_tools)
                or not set(self.allowed_tools).issubset(ALLOWED_SUBAGENT_TOOLS)):
            raise DelegationError("allowed_tools 为空或越权")
        if self.source_id is not None and (not isinstance(self.source_id, str) or not self.source_id.strip()):
            raise DelegationError("source_id 无效")
        if self.model_profile is not None and (
                not isinstance(self.model_profile, str) or not self.model_profile.strip()
        ):
            raise DelegationError("model_profile 无效")
        if self.model_binding_ref is not None:
            if not isinstance(self.model_binding_ref, ModelBindingRef):
                raise DelegationError("model_binding_ref 无效")
            if self.model_profile is not None and self.model_binding_ref.profile != self.model_profile:
                raise DelegationError("model_profile 与 model_binding_ref 不一致")
        if not self.created_at:
            object.__setattr__(self, "created_at", _utc_now())
        expected = _contract_hash(self)
        if self.contract_hash and self.contract_hash != expected:
            raise DelegationError("contract_hash 不匹配")
        object.__setattr__(self, "contract_hash", expected)

    def to_dict(self) -> dict[str, Any]:
        return {
            "delegation_id": self.delegation_id,
            "subagent_id": self.subagent_id,
            "parent_task_id": self.parent_task_id,
            "parent_generation_id": self.parent_generation_id,
            "goal": self.goal,
            "scope": list(self.scope),
            "constraints": list(self.constraints),
            "expected_findings": list(self.expected_findings),
            "requested_tools": list(self.requested_tools),
            "allowed_tools": list(self.allowed_tools),
            "selected_parent_facts": list(self.selected_parent_facts),
            "purpose": self.purpose,
            "source_id": self.source_id,
            "budget": asdict(self.budget),
            "depth": self.depth,
            "contract_hash": self.contract_hash,
            "created_at": self.created_at,
            "model_profile": self.model_profile,
            "model_binding_ref": (
                self.model_binding_ref.to_dict() if self.model_binding_ref is not None else None
            ),
        }


def _contract_hash(task: DelegatedTask) -> str:
    payload = {
        "delegation_id": task.delegation_id,
        "subagent_id": task.subagent_id,
        "parent_task_id": task.parent_task_id,
        "parent_generation_id": task.parent_generation_id,
        "goal": task.goal,
        "scope": list(task.scope),
        "constraints": list(task.constraints),
        "expected_findings": list(task.expected_findings),
        "requested_tools": list(task.requested_tools),
        "allowed_tools": list(task.allowed_tools),
        "selected_parent_facts": list(task.selected_parent_facts),
        "purpose": task.purpose,
        "source_id": task.source_id,
        "budget": asdict(task.budget),
        "depth": task.depth,
        "created_at": task.created_at,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class SubagentResult:
    result_id: str
    delegation_id: str
    subagent_id: str
    parent_task_id: str
    outcome: Literal["completed", "failed", "timed_out", "cancelled", "budget_exhausted"]
    summary: str
    findings: tuple[Finding, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()
    limitations: tuple[str, ...] = ()
    usage: UsageRecord = field(default_factory=UsageRecord)
    contract_hash: str = ""
    started_at: str = ""
    finished_at: str = ""
    error_kind: str | None = None
    error_detail: str | None = None
    model_profile: str | None = None
    binding_fingerprint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "result_id": self.result_id,
            "delegation_id": self.delegation_id,
            "subagent_id": self.subagent_id,
            "parent_task_id": self.parent_task_id,
            "outcome": self.outcome,
            "summary": self.summary,
            "findings": [item.to_dict() for item in self.findings],
            "evidence": [item.to_dict() for item in self.evidence],
            "limitations": list(self.limitations),
            "usage": self.usage.to_dict(),
            "contract_hash": self.contract_hash,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            **({"error_kind": self.error_kind} if self.error_kind else {}),
            **({"error_detail": self.error_detail[:500]} if self.error_detail else {}),
            **({"model_profile": self.model_profile} if self.model_profile else {}),
            **({"binding_fingerprint": self.binding_fingerprint} if self.binding_fingerprint else {}),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _contains_sensitive(value: Any) -> bool:
    if isinstance(value, str):
        return bool(_SENSITIVE_RE.search(value)) or bool(re.search(r"(?i)\bsk-[A-Za-z0-9]{12,}\b", value))
    if isinstance(value, dict):
        return any(_contains_sensitive(key) or _contains_sensitive(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_sensitive(item) for item in value)
    return False


def _safe_error_detail(value: Any, limit: int = 500) -> str:
    text = " ".join(str(value).split())
    text = re.sub(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+", r"\1<redacted>", text)
    text = re.sub(r"(?i)\bsk-[A-Za-z0-9_-]+", "<redacted-key>", text)
    return text[:limit]


def validate_delegation_arguments(
    arguments: Any,
    state: AgentState | None = None,
    provider_catalog: ProviderCatalog | None = None,
) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise DelegationError("delegate_task 参数必须是对象")
    allowed = {
        "goal", "scope", "constraints", "expected_findings", "requested_tools",
        "selected_parent_facts", "purpose", "source_id", "budget", "model_profile",
    }
    unknown = set(arguments) - allowed
    if unknown:
        raise DelegationError("delegate_task 含未知参数: " + ", ".join(sorted(unknown)))
    goal = _require_string(arguments.get("goal"), "goal", max_length=DELEGATION_MAX_GOAL)
    scope = arguments.get("scope")
    if not isinstance(scope, list) or not 1 <= len(scope) <= DELEGATION_MAX_SCOPE:
        raise DelegationError("scope 必须包含 1–8 个工作区相对路径")
    scope = tuple(ScopeGate._validate_relative(item, "scope") for item in scope)
    if len(set(scope)) != len(scope):
        raise DelegationError("scope 不能含重复路径")
    constraints = _bounded_string_list(arguments.get("constraints"), "constraints", allow_empty=True)
    expected = _bounded_string_list(arguments.get("expected_findings"), "expected_findings", allow_empty=True)
    requested = _bounded_string_list(
        arguments.get("requested_tools"), "requested_tools", max_items=4,
    )
    if any(tool not in ALLOWED_SUBAGENT_TOOLS for tool in requested):
        raise DelegationError("requested_tools 含未知或越权工具")
    facts = arguments.get("selected_parent_facts")
    if not isinstance(facts, list) or len(facts) > DELEGATION_MAX_LIST_ITEMS:
        raise DelegationError("selected_parent_facts 必须是有界数组")
    facts = tuple(_require_string(item, f"selected_parent_facts[{index}]")
                  for index, item in enumerate(facts))
    if _contains_sensitive(facts):
        raise DelegationError("selected_parent_facts 含疑似敏感凭据")
    purpose = arguments.get("purpose")
    if purpose not in DELEGATION_PURPOSES:
        raise DelegationError("purpose 必须是 investigation、diagnosis 或 crash_investigation")
    source_id = arguments.get("source_id")
    if source_id is not None:
        source_id = _require_string(source_id, "source_id", max_length=200)
    if purpose != "investigation" and source_id is None:
        raise DelegationError(f"purpose={purpose} 必须提供 source_id")
    if purpose != "investigation" and state is None:
        raise DelegationError(f"purpose={purpose} 必须绑定父 State 才能校验 source_id")
    budget = SubagentBudget.from_request(arguments.get("budget"))
    requested_profile = arguments.get("model_profile")
    if requested_profile is not None:
        requested_profile = _require_string(
            requested_profile, "model_profile", max_length=120,
        )
    selected_profile = requested_profile
    if provider_catalog is not None:
        try:
            selected_profile = provider_catalog.resolve_child_profile(requested_profile)
        except ValueError as error:
            raise DelegationError(str(error)) from error
    if state is not None and hasattr(state, "delegation_gate"):
        detail = state.delegation_gate(
            "delegate_task", {**arguments, "purpose": purpose, "source_id": source_id}, "none",
        )
        if detail:
            raise DelegationError(detail)
    return {
        "goal": goal,
        "scope": list(scope),
        "constraints": list(constraints),
        "expected_findings": list(expected),
        "requested_tools": list(requested),
        "selected_parent_facts": list(facts),
        "purpose": purpose,
        "source_id": source_id,
        "model_profile": selected_profile,
        "budget": {
            name: getattr(budget, name) for name in (
                "max_rounds", "max_llm_calls", "max_tool_calls", "max_tokens",
                "max_result_bytes", "timeout_seconds",
            )
        },
    }


def build_delegated_task(arguments: dict[str, Any], state: AgentState | None = None,
                         *, workspace_root: str | os.PathLike[str],
                         provider_catalog: ProviderCatalog | None = None) -> DelegatedTask:
    normalized = validate_delegation_arguments(arguments, state, provider_catalog)
    ScopeGate(workspace_root, normalized["scope"])
    parent_task_id = getattr(state, "task_id", "") if state is not None else ""
    generation = getattr(state, "current_generation_id", 0) if state is not None else 0
    budget = SubagentBudget.from_request(normalized["budget"])
    binding = (
        provider_catalog.bind(normalized["model_profile"])
        if provider_catalog is not None else None
    )
    task = DelegatedTask(
        delegation_id=str(uuid4()),
        subagent_id=str(uuid4()),
        parent_task_id=parent_task_id,
        parent_generation_id=int(generation),
        goal=normalized["goal"],
        scope=tuple(normalized["scope"]),
        constraints=tuple(normalized["constraints"]),
        expected_findings=tuple(normalized["expected_findings"]),
        requested_tools=tuple(normalized["requested_tools"]),
        allowed_tools=tuple(
            tool for tool in normalized["requested_tools"] if tool in ALLOWED_SUBAGENT_TOOLS
        ),
        selected_parent_facts=tuple(normalized["selected_parent_facts"]),
        purpose=normalized["purpose"],
        source_id=normalized["source_id"],
        budget=budget,
        model_profile=normalized["model_profile"],
        model_binding_ref=binding.reference if binding is not None else None,
    )
    return task


def _observation_hash(execution: ExecutionResult) -> str:
    """Hash stable observation facts, excluding timing and attempt metadata."""
    payload = {
        "tool": execution.tool,
        "arguments": execution.arguments,
        "outcome": execution.outcome,
        "output": execution.output,
        "exit_code": execution.exit_code,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), default=str).encode()).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SubagentRunner:
    """Run one child synchronously with isolated state/context/permissions."""

    def __init__(self, workspace_root: str | os.PathLike[str], *, llm: Callable | None = None,
                 llm_callable: Callable | None = None,
                 parent_registry: ToolRegistry | None = None,
                 model_binding: ModelBinding | None = None):
        self.workspace_root = os.path.realpath(os.path.abspath(os.fspath(workspace_root)))
        self.llm = llm if llm is not None else llm_callable
        self.parent_registry = parent_registry
        self.model_binding = model_binding
        self.last_state: AgentState | None = None
        self.last_context: ContextManager | None = None

    def _llm_callable(self) -> Callable:
        if self.llm is not None:
            return self.llm
        if self.model_binding is not None:
            return self.model_binding.complete
        from mini_agent.agent import call_llm
        return call_llm

    def _summarizer(self) -> Callable[..., str]:
        if self.model_binding is not None and self.llm is None:
            def summarize(messages, *, max_output_tokens=None, timeout=None):
                response = self.model_binding.complete(
                    messages, include_tools=False, stream_output=False,
                    max_output_tokens=max_output_tokens, timeout=timeout,
                )
                return response.message.get("content", "") or ""
            return summarize
        llm = self._llm_callable()

        def summarize(messages, *, max_output_tokens=None, timeout=None):
            options = {"include_tools": False, "stream_output": False,
                       "max_output_tokens": max_output_tokens, "timeout": timeout}
            signature = inspect.signature(llm)
            if not any(item.kind == inspect.Parameter.VAR_KEYWORD
                       for item in signature.parameters.values()):
                options = {name: value for name, value in options.items()
                           if name in signature.parameters}
            response = llm(messages, **options)
            if isinstance(response, ProviderResponse):
                return response.message.get("content", "") or ""
            if isinstance(response, dict) and "choices" in response:
                choices = response.get("choices") or []
                if choices and isinstance(choices[0], dict):
                    response = choices[0].get("message", response)
            return response.get("content", "") if isinstance(response, dict) else ""
        return summarize

    def _make_result(self, task: DelegatedTask, outcome: str, summary: str,
                     findings: tuple[Finding, ...] = (), evidence: tuple[EvidenceRef, ...] = (),
                     limitations: tuple[str, ...] = (), usage: UsageRecord | None = None,
                     started_at: str = "", error_kind: str | None = None,
                     error_detail: str | None = None) -> SubagentResult:
        usage = usage or UsageRecord()
        if error_detail is None and error_kind and limitations:
            error_detail = limitations[-1]
        result = SubagentResult(
            result_id=str(uuid4()), delegation_id=task.delegation_id,
            subagent_id=task.subagent_id, parent_task_id=task.parent_task_id,
            outcome=outcome, summary=str(summary)[:4000], findings=findings,
            evidence=evidence, limitations=tuple(str(item)[:500] for item in limitations)[:32],
            usage=usage, contract_hash=task.contract_hash,
            started_at=started_at or _utc_now(), finished_at=_utc_now(), error_kind=error_kind,
            error_detail=_safe_error_detail(error_detail) if error_detail else None,
            model_profile=task.model_profile or (
                self.model_binding.profile.name if self.model_binding is not None else None
            ),
            binding_fingerprint=(task.model_binding_ref.fingerprint if task.model_binding_ref else (
                self.model_binding.reference.fingerprint if self.model_binding is not None else None
            )),
        )
        # A result-size guard is authoritative and is applied to failed reports too.
        encoded = result.to_json().encode("utf-8")
        # ``result_bytes`` is itself serialized, so changing its digit count
        # can change the encoded length.  Iterate to the small fixed point
        # rather than publishing a stale self-referential size.
        for _ in range(4):
            actual_size = len(encoded)
            if result.usage.result_bytes == actual_size:
                break
            result = replace(result, usage=replace(result.usage, result_bytes=actual_size))
            encoded = result.to_json().encode("utf-8")
        if len(encoded) <= task.budget.max_result_bytes:
            return result
        fallback = replace(
            result,
            outcome="failed",
            summary="子代理结果超过大小上限",
            findings=(),
            evidence=(),
            limitations=("result_too_large",),
            usage=UsageRecord(
                result.usage.rounds,
                result.usage.llm_calls,
                result.usage.tool_calls,
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                token_accounting=result.usage.token_accounting,
                result_bytes=0,
                elapsed_ms=result.usage.elapsed_ms,
            ),
            finished_at=_utc_now(),
            error_kind="result_too_large",
            error_detail="result_too_large",
        )
        fallback_bytes = fallback.to_json().encode("utf-8")
        for _ in range(4):
            actual_size = len(fallback_bytes)
            if fallback.usage.result_bytes == actual_size:
                break
            fallback = replace(
                fallback,
                usage=replace(fallback.usage, result_bytes=actual_size),
            )
            fallback_bytes = fallback.to_json().encode("utf-8")
        if len(fallback_bytes) > task.budget.max_result_bytes:
            raise DelegationError("结果预算小于最小错误结果")
        return fallback

    @staticmethod
    def _parse_report(task: DelegatedTask, report: Any, scope_gate: ScopeGate,
                      observations: list[dict[str, Any]] | None = None) -> tuple[str, tuple[Finding, ...], tuple[EvidenceRef, ...], tuple[str, ...]]:
        observations = observations or []
        if not isinstance(report, dict):
            raise DelegationError("最终报告必须是 JSON object")
        required = {"summary", "findings", "evidence", "limitations"}
        if set(report) != required:
            raise DelegationError("最终报告必须恰好包含 summary/findings/evidence/limitations")
        summary = _require_string(report["summary"], "summary", max_length=4000)
        raw_evidence = report["evidence"]
        if not isinstance(raw_evidence, list) or len(raw_evidence) > 64:
            raise DelegationError("evidence 必须是有界数组")
        evidence: list[EvidenceRef] = []
        seen_evidence: set[str] = set()
        for index, item in enumerate(raw_evidence):
            if not isinstance(item, dict):
                raise DelegationError(f"evidence[{index}] 必须是对象")
            allowed = {"id", "kind", "claim", "path", "line", "tool", "observation_hash"}
            required = {"id", "kind", "claim"}
            if not required.issubset(item) or set(item) - allowed:
                raise DelegationError(f"evidence[{index}] 字段不完整或未知")
            evidence_id = _require_string(item["id"], f"evidence[{index}].id", max_length=120)
            if evidence_id in seen_evidence:
                raise DelegationError("evidence ID 重复")
            seen_evidence.add(evidence_id)
            kind = item["kind"]
            if kind not in ("file_location", "tool_observation"):
                raise DelegationError("evidence kind 非法")
            claim = _require_string(item["claim"], f"evidence[{index}].claim", max_length=2000)
            path = item.get("path")
            if path is not None:
                if not isinstance(path, str):
                    raise DelegationError("evidence path 类型非法")
                path = scope_gate.validate_evidence_path(path)
            line = item.get("line")
            if line is not None and (isinstance(line, bool) or not isinstance(line, int) or line < 1):
                raise DelegationError("evidence line 必须是正行号")
            tool = item.get("tool")
            if tool is not None and tool not in task.allowed_tools:
                raise DelegationError("evidence 引用了未实际允许的工具")
            observation_hash = item.get("observation_hash")
            if observation_hash is not None and not _HASH_RE.fullmatch(str(observation_hash)):
                raise DelegationError("evidence observation_hash 必须是 SHA-256")
            if observation_hash is not None:
                observation_hash = str(observation_hash).lower()
            if kind == "file_location" and (path is None or line is None):
                raise DelegationError("file_location evidence 必须包含 path 和 line")
            if kind == "tool_observation" and (tool is None or observation_hash is None):
                raise DelegationError("tool_observation evidence 必须包含 tool 和 observation_hash")

            matching = []
            for observation in observations:
                if kind == "file_location":
                    if observation.get("kind") != "file_location":
                        continue
                    if observation.get("path") != path or observation.get("line") != line:
                        continue
                else:
                    if observation.get("kind") != "tool_observation":
                        continue
                    if observation.get("tool") != tool:
                        continue
                    if observation.get("hash") != observation_hash:
                        continue
                    if path is not None and observation.get("path") != path:
                        continue
                matching.append(observation)
            if not matching:
                raise DelegationError("evidence 没有对应的实际只读观察")
            evidence.append(EvidenceRef(evidence_id, kind, claim, path, line, tool, observation_hash))
        raw_findings = report["findings"]
        if not isinstance(raw_findings, list) or len(raw_findings) > 64:
            raise DelegationError("findings 必须是有界数组")
        findings: list[Finding] = []
        seen_findings: set[str] = set()
        evidence_ids = {item.evidence_id for item in evidence}
        for index, item in enumerate(raw_findings):
            if not isinstance(item, dict):
                raise DelegationError(f"findings[{index}] 必须是对象")
            allowed = {"id", "claim", "evidence_ids", "confidence", "caveat"}
            required = {"id", "claim", "evidence_ids", "confidence"}
            if not required.issubset(item) or set(item) - allowed:
                raise DelegationError(f"findings[{index}] 字段不完整或未知")
            finding_id = _require_string(item["id"], f"findings[{index}].id", max_length=120)
            if finding_id in seen_findings:
                raise DelegationError("finding ID 重复")
            seen_findings.add(finding_id)
            claim = _require_string(item["claim"], f"findings[{index}].claim", max_length=4000)
            refs = item["evidence_ids"]
            if not isinstance(refs, list) or len(refs) > 32 or any(not isinstance(ref, str) for ref in refs):
                raise DelegationError("finding evidence 引用非法")
            if any(ref not in evidence_ids for ref in refs):
                raise DelegationError("finding 引用了悬空 evidence ID")
            confidence = item["confidence"]
            if confidence not in ("observed", "inferred"):
                raise DelegationError("finding confidence 必须是 observed 或 inferred")
            caveat = item.get("caveat")
            if caveat is not None:
                if not isinstance(caveat, str) or len(caveat) > 1000:
                    raise DelegationError(f"findings[{index}].caveat 类型或长度非法")
                caveat = caveat.strip() or None
            if confidence == "inferred" and (not refs or not caveat):
                raise DelegationError("inferred finding 必须有 evidence 和 caveat")
            findings.append(Finding(finding_id, claim, tuple(refs), confidence, caveat))
        limitations = report["limitations"]
        if not isinstance(limitations, list) or len(limitations) > 32:
            raise DelegationError("limitations 必须是有界数组")
        limitations = tuple(_require_string(item, f"limitations[{index}]") for index, item in enumerate(limitations))
        return summary, tuple(findings), tuple(evidence), limitations

    def _observations_for_execution(self, execution: ExecutionResult,
                                    scope_gate: ScopeGate) -> list[dict[str, Any]]:
        """Derive only evidence positions actually present in a tool result."""
        observed_path = execution.arguments.get("path")
        if isinstance(observed_path, str):
            observed_path = os.path.relpath(
                os.path.realpath(observed_path), self.workspace_root,
            )
        observation_hash = _observation_hash(execution)
        observations: list[dict[str, Any]] = [{
            "kind": "tool_observation", "tool": execution.tool,
            "path": observed_path, "hash": observation_hash,
        }]
        if execution.tool == "read_file" and isinstance(execution.output, str):
            for rendered_line in execution.output.splitlines():
                match = re.match(r"^(\d+)\|", rendered_line)
                if match and isinstance(observed_path, str):
                    observations.append({
                        "kind": "file_location", "tool": "read_file",
                        "path": observed_path, "line": int(match.group(1)),
                        "hash": observation_hash,
                    })
        elif execution.tool == "grep" and isinstance(execution.output, str):
            for rendered_line in execution.output.splitlines():
                match = re.match(r"^(.*):(\d+):(?:\s|$)", rendered_line)
                if not match:
                    continue
                candidate = match.group(1)
                try:
                    candidate = scope_gate.validate_evidence_path(candidate)
                except DelegationError:
                    continue
                observations.append({
                    "kind": "file_location", "tool": "grep",
                    "path": candidate, "line": int(match.group(2)),
                    "hash": observation_hash,
                })
        return observations

    def run(self, task: DelegatedTask, *, cancel_event: Event | None = None,
            cancellation_reason: Callable[[], str] | None = None) -> SubagentResult:
        """Assemble an isolated child and delegate control to AgentRuntime."""
        started_clock = time.monotonic()
        started_at = _utc_now()
        budget = task.budget
        scope_gate = ScopeGate(self.workspace_root, list(task.scope))
        child_state = AgentState()
        child_state.begin_task(task.goal)
        self.last_state = child_state
        if self.parent_registry is None:
            from mini_agent.tools.calc import calculate_tool
            from mini_agent.tools.file import read_file_tool, list_dir_tool, grep_tool
            parent = ToolRegistry()
            for tool in (calculate_tool, read_file_tool, list_dir_tool, grep_tool):
                parent.register(tool)
        else:
            parent = self.parent_registry
        view = parent.filtered_for_subagent(set(task.allowed_tools), scope_gate=scope_gate)
        executor = ToolExecutor(
            view, gate=PermissionGate(PermissionPolicy({name: ALLOW for name in task.allowed_tools})),
        )
        system = build_subagent_prompt(
            task, InstructionLoader(self.workspace_root).load(), self.workspace_root,
        )
        history: list[dict[str, Any]] = [{
            "role": "user",
            "content": json.dumps(
                {"contract": task.to_dict(), "selected_parent_facts": list(task.selected_parent_facts)},
                ensure_ascii=False, sort_keys=True,
            ),
        }]
        policy = SubagentRuntimePolicy(
            runner=self, task=task, scope_gate=scope_gate,
            started_clock=started_clock, started_at=started_at,
            state=child_state, cancel_event=cancel_event,
            cancellation_reason=cancellation_reason,
        )
        context = ContextManager(
            child_state, history, observability=False,
            budget=ContextBudget(
                window=(self.model_binding.profile.context_window
                        if self.model_binding is not None else 128_000),
                output_reserve_tokens=(self.model_binding.profile.max_output_tokens
                                       if self.model_binding is not None else None),
            ),
            summarizer=policy.summarize,
            protected_messages=[{"role": "system", "content": system}],
            model_binding=self.model_binding,
            usage_meter=(self.model_binding.usage_meter if self.model_binding is not None else None),
        )
        context.before_summary = policy.before_summary
        self.last_context = context
        runtime = AgentRuntime(
            llm_client=self._llm_callable(),
            context=context,
            executor=executor,
            policy=policy,
            max_rounds=budget.max_rounds,
            output=None,
            model_binding=self.model_binding,
        )
        try:
            runtime_result = runtime.run()
        except KeyboardInterrupt as error:
            policy._set_terminal(
                "cancelled", "子代理收到取消请求", "cancelled",
                (cancellation_reason() if cancellation_reason is not None else "interrupt"),
            )
            return policy.make_result(
                "cancelled", "子代理收到取消请求", error_kind="cancelled",
                detail="user_interrupt",
            )
        except (TimeoutError, socket.timeout) as error:
            if cancel_event is not None and cancel_event.is_set():
                return policy.make_result(
                    "cancelled", "子代理收到取消请求", error_kind="cancelled",
                    detail=policy._cancel_detail(),
                )
            return policy.make_result(
                "timed_out", "子代理 LLM 请求超时", error_kind="timeout",
                detail=_safe_error_detail(error),
            )
        except Exception as error:
            if cancel_event is not None and cancel_event.is_set():
                return policy.make_result(
                    "cancelled", "子代理收到取消请求", error_kind="cancelled",
                    detail=policy._cancel_detail(),
                )
            return policy.make_result(
                "failed", "子代理 LLM 调用失败", error_kind="llm_error",
                detail=_safe_error_detail(error),
            )
        return policy.result_from_runtime(runtime_result)


class SubagentRuntimePolicy:
    """Child-only budget, observation, and Result Contract policy."""

    _FORMAT_NOTICE = (
        "Runtime Notice：格式修正：上一次输出不是合法报告。下一次必须只输出严格 JSON，"
        "字段恰为 summary、findings、evidence、limitations；不得调用工具。"
    )

    def __init__(self, *, runner: SubagentRunner, task: DelegatedTask,
                 scope_gate: ScopeGate, started_clock: float, started_at: str,
                 state: AgentState, cancel_event: Event | None = None,
                 cancellation_reason: Callable[[], str] | None = None) -> None:
        self.runner = runner
        self.task = task
        self.scope_gate = scope_gate
        self.started_clock = started_clock
        self.started_at = started_at
        self.state = state
        self.cancel_event = cancel_event
        self.cancellation_reason = cancellation_reason
        self.correction_used = False
        self.invalid_tool_call = False
        self.observations: list[dict[str, Any]] = []
        self.limitations: list[str] = []
        self.final_report: tuple[str, tuple[Finding, ...], tuple[EvidenceRef, ...], tuple[str, ...]] | None = None
        self.terminal: tuple[str, str, str, str | None] | None = None
        self.runtime: AgentRuntime | None = None

    def _cancel_detail(self) -> str:
        if self.cancellation_reason is not None:
            try:
                value = self.cancellation_reason()
                if value:
                    return _safe_error_detail(value, 240)
            except Exception:
                pass
        return "cooperative_cancel"

    def _cancel_check(self) -> RuntimeDecision | None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            detail = self._cancel_detail()
            self._set_terminal(
                "cancelled", "子代理收到取消请求", "cancelled", detail, "cancelled",
            )
            return RuntimeDecision("finish", "子代理收到取消请求", "cancelled")
        return None

    def _usage(self, runtime: AgentRuntime) -> UsageRecord:
        return UsageRecord(
            runtime.rounds,
            runtime.llm_calls,
            runtime.tool_calls,
            input_tokens=runtime.input_tokens,
            output_tokens=runtime.output_tokens,
            token_accounting=runtime.token_accounting,
            result_bytes=0,
            elapsed_ms=int((time.monotonic() - self.started_clock) * 1000),
        )

    def _set_terminal(self, outcome: str, summary: str, error_kind: str,
                      detail: str | None = None, *limitations: str) -> None:
        self.terminal = (outcome, summary, error_kind, detail)
        for item in limitations:
            if item not in self.limitations:
                self.limitations.append(item)

    def make_result(self, outcome: str, summary: str, *, error_kind: str,
                    detail: str | None = None,
                    findings: tuple[Finding, ...] = (),
                    evidence: tuple[EvidenceRef, ...] = (),
                    limitations: tuple[str, ...] = ()) -> SubagentResult:
        runtime = self.runtime
        usage = self._usage(runtime) if runtime is not None else UsageRecord(
            token_accounting="estimated",
        )
        combined = tuple(self.limitations) + tuple(limitations)
        return self.runner._make_result(
            self.task, outcome, summary, findings, evidence, combined,
            usage, self.started_at, error_kind, detail,
        )

    def result_from_runtime(self, runtime_result) -> SubagentResult:
        self.runtime = self.runtime or getattr(runtime_result, "runtime", None)
        if self.final_report is not None:
            summary, findings, evidence, report_limitations = self.final_report
            return self.make_result(
                "completed", summary, error_kind=None,
                findings=findings, evidence=evidence,
                limitations=report_limitations,
            )
        if self.terminal is not None:
            outcome, summary, error_kind, detail = self.terminal
            return self.make_result(
                outcome, summary, error_kind=error_kind, detail=detail,
            )
        return self.make_result(
            "failed", runtime_result.content or "子代理运行结束",
            error_kind=runtime_result.stop_reason or "runtime_error",
        )

    def before_run(self, runtime):
        self.runtime = runtime
        return self._cancel_check() or self._budget_check(runtime)

    def before_prepare(self, runtime):
        self.runtime = runtime
        return self._cancel_check()

    def before_summary(self, prompt: list[dict[str, Any]]) -> dict[str, object]:
        """Admit an auxiliary model call before ContextManager invokes it."""
        runtime = self.runtime
        if runtime is None:
            raise ValueError("子代理 Runtime 尚未启动")
        runtime._refresh_usage()
        cancelled = self._cancel_check()
        if cancelled is not None:
            raise ValueError("子代理已取消")
        remaining_time = self.task.budget.timeout_seconds - (time.monotonic() - self.started_clock)
        request_tokens = count_tokens(prompt)
        remaining_tokens = self.task.budget.max_tokens - runtime.estimated_tokens - request_tokens
        if remaining_time <= 0:
            self._set_terminal("timed_out", "子代理达到墙钟时间上限", "timeout", "timeout")
            raise ValueError("摘要请求超时")
        if runtime.llm_calls >= self.task.budget.max_llm_calls or remaining_tokens <= 0:
            self._set_terminal("budget_exhausted", "子代理摘要预算耗尽", "budget_exhausted",
                               "summary_budget")
            raise ValueError("摘要请求预算不足")
        return {"max_output_tokens": remaining_tokens, "timeout": remaining_time}

    def summarize(self, prompt: list[dict[str, Any]], *, max_output_tokens: int,
                  timeout: float) -> str:
        runtime = self.runtime
        if runtime is None:
            raise ValueError("子代理 Runtime 尚未启动")
        summarizer = self.runner._summarizer()
        if runtime.usage_meter is not None:
            return summarizer(prompt, max_output_tokens=max_output_tokens, timeout=timeout)
        # Injected test clients have no provider meter. Count their summary
        # request even if it raises, just as the normal Runtime counts calls.
        runtime.llm_calls += 1
        runtime.input_tokens += count_tokens(prompt)
        runtime.estimated_tokens = runtime.input_tokens + runtime.output_tokens
        summary = summarizer(prompt, max_output_tokens=max_output_tokens, timeout=timeout)
        runtime.output_tokens += count_tokens(summary)
        runtime.estimated_tokens = runtime.input_tokens + runtime.output_tokens
        return summary

    def _budget_check(self, runtime):
        elapsed = time.monotonic() - self.started_clock
        if elapsed > self.task.budget.timeout_seconds:
            self._set_terminal("timed_out", "子代理达到墙钟时间上限", "timeout", "timeout")
            return RuntimeDecision("finish", "子代理达到墙钟时间上限", "timeout")
        if runtime.rounds >= self.task.budget.max_rounds or runtime.llm_calls >= self.task.budget.max_llm_calls:
            self._set_terminal(
                "budget_exhausted", "子代理预算耗尽", "budget_exhausted",
                "round_or_llm_budget", "round_or_llm_budget",
            )
            return RuntimeDecision("finish", "子代理预算耗尽", "budget_exhausted")
        if runtime.estimated_tokens + runtime.request_tokens >= self.task.budget.max_tokens:
            self._set_terminal(
                "budget_exhausted", "子代理 token 预算耗尽", "budget_exhausted",
                "token_budget", "token_budget",
            )
            return RuntimeDecision("finish", "子代理 token 预算耗尽", "budget_exhausted")
        return None

    def before_llm(self, runtime):
        self.runtime = runtime
        if self.terminal is not None:
            return RuntimeDecision("finish", self.terminal[1], self.terminal[2])
        return self._cancel_check() or self._budget_check(runtime)

    def after_llm(self, runtime, _message):
        self.runtime = runtime
        cancelled = self._cancel_check()
        if cancelled is not None:
            return cancelled
        elapsed = time.monotonic() - self.started_clock
        if elapsed > self.task.budget.timeout_seconds:
            self._set_terminal("timed_out", "子代理达到墙钟时间上限", "timeout", "timeout")
            return RuntimeDecision("finish", "子代理达到墙钟时间上限", "timeout")
        if runtime.estimated_tokens > self.task.budget.max_tokens:
            self._set_terminal(
                "budget_exhausted", "子代理 token 预算耗尽", "budget_exhausted",
                "token_budget", "token_budget",
            )
            return RuntimeDecision("finish", "子代理 token 预算耗尽", "budget_exhausted")
        return None

    def llm_options(self, runtime):
        remaining = max(
            0.001,
            self.task.budget.timeout_seconds - (time.monotonic() - self.started_clock),
        )
        remaining_tokens = max(
            1, self.task.budget.max_tokens - runtime.estimated_tokens - runtime.request_tokens,
        )
        return {
            "stream_output": False, "timeout": remaining,
            "max_output_tokens": remaining_tokens,
        }

    def on_text(self, runtime, content):
        self.runtime = runtime
        try:
            report = json.loads(content) if isinstance(content, str) else None
            self.final_report = self.runner._parse_report(
                self.task, report, self.scope_gate, self.observations,
            )
        except (TypeError, ValueError, json.JSONDecodeError, DelegationError) as error:
            detail = _safe_error_detail(error)
            if self.correction_used:
                self._set_terminal(
                    "failed", "子代理最终报告非法", "invalid_result", detail,
                    "invalid_result",
                )
                return RuntimeDecision("finish", "子代理最终报告非法", "invalid_result")
            self.correction_used = True
            return RuntimeDecision("continue", notice=self._FORMAT_NOTICE)
        return RuntimeDecision("finish", self.final_report[0], "completed")

    @staticmethod
    def _rejection(runtime, index, error_kind, message):
        name, arguments = runtime.parsed_calls[index]
        content = json.dumps({
            "status": "error", "error_kind": error_kind, "message": message,
        }, ensure_ascii=False)
        return ExecutionResult(
            name, arguments, "not_checked", False, "invalid", 0,
            runtime.effects[index], content, content[:200], error_kind=error_kind,
        )

    def prepare_tool_round(self, runtime, calls):
        self.runtime = runtime
        cancelled = self._cancel_check()
        if cancelled is not None:
            self._set_terminal("cancelled", cancelled.content, "cancelled", self._cancel_detail())
            return ToolRoundPlan(
                True,
                {
                    index: self._rejection(
                        runtime, index, "cancelled", "子代理收到取消请求",
                    ) for index in range(len(calls))
                },
            )
        if runtime.normalized and runtime.normalized.errors_by_call_id:
            self.invalid_tool_call = True
            detail = "子代理工具调用协议非法: " + "; ".join(
                runtime.normalized.errors_by_call_id.values()
            )
            return ToolRoundPlan(
                True,
                {
                    index: self._rejection(
                        runtime, index, "invalid_tool_call", detail,
                    )
                    for index in range(len(calls))
                },
            )
        if self.correction_used:
            self._set_terminal(
                "failed", "格式修正阶段再次发起工具调用", "invalid_result",
                "invalid_result",
            )
            return ToolRoundPlan(
                True,
                {
                    index: self._rejection(
                        runtime, index, "invalid_result", "格式修正阶段不得再次发起工具调用",
                    )
                    for index in range(len(calls))
                },
            )
        if runtime.tool_calls + len(calls) > self.task.budget.max_tool_calls:
            self._set_terminal(
                "budget_exhausted", "子代理工具调用预算耗尽", "budget_exhausted",
                "tool_budget",
            )
            return ToolRoundPlan(
                True,
                {
                    index: self._rejection(
                        runtime, index, "budget_exhausted",
                        "子代理工具调用预算耗尽，当前回合未进入 handler",
                    )
                    for index in range(len(calls))
                },
            )
        return ToolRoundPlan(True)

    def after_tool_result(self, runtime, call, execution):
        if self.cancel_event is not None and self.cancel_event.is_set():
            self._set_terminal(
                "cancelled", "子代理收到取消请求", "cancelled", self._cancel_detail(), "cancelled",
            )
        if execution.outcome == "succeeded" and execution.handler_admitted:
            derived = self.runner._observations_for_execution(execution, self.scope_gate)
            self.observations.extend(derived)
            return execution.tool_content() + "\n[observation_hash=" + derived[0]["hash"] + "]"
        return execution.tool_content()

    def after_tool_round(self, runtime, calls, results):
        self.runtime = runtime
        if self.invalid_tool_call:
            self._set_terminal(
                "failed", "子代理工具调用协议非法", "invalid_tool_call",
                "invalid_tool_call",
            )
            return RuntimeDecision("finish", "子代理工具调用协议非法", "invalid_tool_call")
        if self.terminal is not None:
            return RuntimeDecision("finish", self.terminal[1], self.terminal[2])
        return None

    def on_round_limit(self, runtime):
        self.runtime = runtime
        self._set_terminal(
            "budget_exhausted", "子代理预算耗尽", "budget_exhausted",
            "round_or_llm_budget", "round_or_llm_budget",
        )
        return RuntimeDecision("finish", "子代理预算耗尽", "budget_exhausted")


class DelegationManager:
    """Own the single synchronous child slot for one parent runtime."""

    def __init__(self, workspace_root: str | os.PathLike[str] | None = None, *,
                 subagent_llm: Callable | None = None, llm: Callable | None = None,
                 llm_callable: Callable | None = None,
                 parent_registry: ToolRegistry | None = None,
                 provider_catalog: ProviderCatalog | None = None,
                 parent_state: AgentState | None = None):
        self.workspace_root = os.path.realpath(os.path.abspath(os.fspath(workspace_root or os.getcwd())))
        self.subagent_llm = subagent_llm if subagent_llm is not None else (
            llm if llm is not None else llm_callable
        )
        self.parent_registry = parent_registry
        self.provider_catalog = provider_catalog
        self.parent_state = parent_state
        self._lock = Lock()
        self._active_task_id: str | None = None
        self._active_delegation_id: str | None = None
        self._cancel_event = Event()
        self._cancel_reason = ""
        self._done_event = Event()
        self._done_event.set()
        self._reservation_state: AgentState | None = None
        self._reservation: Any = None
        self._interrupted = False
        self.last_task: DelegatedTask | None = None
        self.last_result: SubagentResult | None = None

    def create_task(self, arguments: dict[str, Any], state: AgentState | None = None) -> DelegatedTask:
        return build_delegated_task(
            arguments, state, workspace_root=self.workspace_root,
            provider_catalog=self.provider_catalog,
        )

    @property
    def active_task_id(self) -> str | None:
        with self._lock:
            return self._active_task_id

    @property
    def active_delegation_id(self) -> str | None:
        with self._lock:
            return self._active_delegation_id

    def active_info(self) -> dict[str, str | bool | None]:
        with self._lock:
            return {
                "active": self._active_task_id is not None,
                "task_id": self._active_task_id,
                "delegation_id": self._active_delegation_id,
                "cancel_requested": self._cancel_event.is_set(),
                "cancel_reason": self._cancel_reason or None,
            }

    def consume_interrupt(self) -> bool:
        with self._lock:
            interrupted = self._interrupted
            self._interrupted = False
            return interrupted

    def cancel(self, task_id: str | None = None, reason: str = "task_boundary") -> bool:
        """Request cooperative cancellation; an in-flight HTTP call is not killed."""
        with self._lock:
            if self._active_task_id is None:
                return False
            if task_id is not None and task_id != self._active_task_id:
                return False
            self._cancel_reason = _safe_error_detail(reason, 240)
            self._cancel_event.set()
            state = self._reservation_state
            delegation_id = self._active_delegation_id
        if state is not None and delegation_id is not None:
            try:
                state.note_delegation_cancellation(delegation_id, self._cancel_reason)
            except ValueError:
                pass
        return True

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for a child call to reach its manager boundary."""
        return self._done_event.wait(timeout)

    @staticmethod
    def _rejected_result(task: DelegatedTask, outcome: str, detail: str) -> SubagentResult:
        now = _utc_now()
        return SubagentResult(
            str(uuid4()), task.delegation_id, task.subagent_id, task.parent_task_id,
            outcome, detail, (), (), (detail,), UsageRecord(token_accounting="estimated"),
            task.contract_hash, now, now, "aggregate_budget" if outcome == "budget_exhausted" else "lifecycle_rejected",
        )

    def run(self, arguments: dict[str, Any] | DelegatedTask,
            state: AgentState | None = None) -> SubagentResult:
        state = state or self.parent_state
        try:
            task = arguments if isinstance(arguments, DelegatedTask) else self.create_task(arguments, state)
        except Exception as error:
            # A direct Manager caller still receives the same bounded result shape.
            now = _utc_now()
            return SubagentResult(str(uuid4()), "", "", getattr(state, "task_id", "") if state else "",
                                  "failed", "委派合同非法", (), (), (_safe_error_detail(error),),
                                  UsageRecord(token_accounting="estimated"), "", now, now, "invalid_contract")
        with self._lock:
            if self._active_task_id is not None:
                result = SubagentResult(
                    str(uuid4()), task.delegation_id, task.subagent_id, task.parent_task_id,
                    "failed", "当前已有子代理运行", (), (), ("delegation_busy",),
                    UsageRecord(token_accounting="estimated"), task.contract_hash,
                    _utc_now(), _utc_now(), "delegation_busy",
                )
                self.last_result = result
                return result
            self._active_task_id = getattr(state, "task_id", None) or task.parent_task_id
            self._active_delegation_id = task.delegation_id
            self._cancel_event = Event()
            self._cancel_reason = ""
            self._done_event.clear()
            self._reservation_state = state
            self._reservation = None
            self._interrupted = False
        reservation = None
        if state is not None:
            try:
                reservation = state.reserve_delegation(task)
                state.start_delegation(task.delegation_id)
                with self._lock:
                    self._reservation = reservation
            except Exception as error:
                result = self._rejected_result(
                    task, "budget_exhausted", _safe_error_detail(error),
                )
                self.last_task = task
                self.last_result = result
                if state is not None:
                    try:
                        state.record_delegation_rejection(task, result, result.error_detail)
                    except Exception:
                        # The deterministic tool result remains authoritative
                        # if the bounded audit list itself is full.
                        pass
                with self._lock:
                    self._active_task_id = None
                    self._active_delegation_id = None
                    self._reservation_state = None
                    self._reservation = None
                    self._done_event.set()
                return result
        self.last_task = task
        try:
            binding = (
                self.provider_catalog.bind(task.model_profile)
                if self.provider_catalog is not None and task.model_profile is not None
                else None
            )
            result = SubagentRunner(
                self.workspace_root, llm=self.subagent_llm,
                parent_registry=self.parent_registry, model_binding=binding,
            ).run(
                task, cancel_event=self._cancel_event,
                cancellation_reason=lambda: self._cancel_reason,
            )
            if result.outcome == "cancelled" and result.error_detail == "user_interrupt":
                with self._lock:
                    self._interrupted = True
            if state is not None:
                state.delegation_result_ready(task.delegation_id, result)
            self.last_result = result
            return result
        except KeyboardInterrupt as error:
            with self._lock:
                self._interrupted = True
            result = SubagentResult(
                str(uuid4()), task.delegation_id, task.subagent_id, task.parent_task_id,
                "cancelled", "子代理收到取消请求", (), (), ("cancelled",),
                UsageRecord(token_accounting="estimated"), task.contract_hash,
                _utc_now(), _utc_now(), "cancelled", _safe_error_detail(error),
            )
            if state is not None:
                state.delegation_result_ready(task.delegation_id, result)
            self.last_result = result
            return result
        except Exception as error:
            result = SubagentResult(
                str(uuid4()), task.delegation_id, task.subagent_id, task.parent_task_id,
                "failed", "子代理运行失败", (), (), (_safe_error_detail(error),),
                UsageRecord(token_accounting="estimated"), task.contract_hash,
                _utc_now(), _utc_now(), "runner_error",
            )
            if state is not None:
                state.delegation_result_ready(task.delegation_id, result)
            self.last_result = result
            return result
        finally:
            with self._lock:
                self._active_task_id = None
                self._active_delegation_id = None
                self._reservation_state = None
                self._reservation = None
                self._done_event.set()

    delegate = run
