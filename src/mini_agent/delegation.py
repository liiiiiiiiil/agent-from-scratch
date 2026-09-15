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
import json
import ntpath
import os
import re
import socket
from threading import Lock
import time
from typing import Any, Callable, Literal
from uuid import uuid4

from mini_agent.context import ContextManager, count_tokens
from mini_agent.instructions import InstructionLoader
from mini_agent.permission import ALLOW, PermissionGate, PermissionPolicy
from mini_agent.prompt import build_subagent_prompt
from mini_agent.runtime import AgentRuntime
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
        if self.token_accounting != "estimated":
            raise DelegationError("v0.34 只支持 token_accounting=estimated")

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


def validate_delegation_arguments(arguments: Any, state: AgentState | None = None) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise DelegationError("delegate_task 参数必须是对象")
    allowed = {
        "goal", "scope", "constraints", "expected_findings", "requested_tools",
        "selected_parent_facts", "purpose", "source_id", "budget",
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
        "budget": {
            name: getattr(budget, name) for name in (
                "max_rounds", "max_llm_calls", "max_tool_calls", "max_tokens",
                "max_result_bytes", "timeout_seconds",
            )
        },
    }


def build_delegated_task(arguments: dict[str, Any], state: AgentState | None = None,
                         *, workspace_root: str | os.PathLike[str]) -> DelegatedTask:
    normalized = validate_delegation_arguments(arguments, state)
    ScopeGate(workspace_root, normalized["scope"])
    parent_task_id = getattr(state, "task_id", "") if state is not None else ""
    generation = getattr(state, "current_generation_id", 0) if state is not None else 0
    budget = SubagentBudget.from_request(normalized["budget"])
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
                 parent_registry: ToolRegistry | None = None):
        self.workspace_root = os.path.realpath(os.path.abspath(os.fspath(workspace_root)))
        self.llm = llm if llm is not None else llm_callable
        self.parent_registry = parent_registry
        self.last_state: AgentState | None = None
        self.last_context: ContextManager | None = None

    def _llm_callable(self) -> Callable:
        if self.llm is not None:
            return self.llm
        from mini_agent.agent import call_llm
        return call_llm

    @staticmethod
    def _normalize_calls(message: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Normalize every advertised call and retain one error per bad call.

        Invalid calls receive synthetic unique IDs, matching the parent loop's
        protocol behavior, so the child can always append a corresponding
        ``role=tool`` result before it terminates.
        """
        calls = message.get("tool_calls") or []
        if not isinstance(calls, list):
            raise DelegationError("子 LLM tool_calls 格式非法")
        normalized = []
        errors: dict[str, str] = {}
        seen_ids: set[str] = set()
        original_ids = {
            call.get("id") for call in calls
            if isinstance(call, dict) and isinstance(call.get("id"), str)
            and call.get("id").strip()
        }
        next_local_id = 0
        for index, call in enumerate(calls):
            call_errors: list[str] = []
            function = call.get("function") if isinstance(call, dict) else None
            raw_id = call.get("id") if isinstance(call, dict) else None
            if not isinstance(call, dict):
                call_errors.append("tool_call 格式非法")
            elif call.get("type") != "function":
                call_errors.append("tool_call.type 非法")
            if not isinstance(function, dict):
                call_errors.append("tool_call 缺少 function")
                function = {}
            name = function.get("name")
            raw = function.get("arguments", "{}")
            if not isinstance(name, str) or not name.strip():
                call_errors.append("tool_call 名称非法")
                name = "invalid_tool_call"
            if isinstance(raw, str):
                try:
                    args = json.loads(raw)
                except json.JSONDecodeError:
                    call_errors.append("tool_call 参数不是 JSON")
                    args = {}
            else:
                args = raw
            if not isinstance(args, dict):
                call_errors.append("tool_call 参数必须是对象")
                args = {}
            if not isinstance(raw_id, str) or not raw_id.strip():
                call_errors.append("tool_call_id 非法")
            elif raw_id in seen_ids:
                call_errors.append("tool_call_id 重复")
            if call_errors:
                while True:
                    call_id = f"local-error-{next_local_id}"
                    next_local_id += 1
                    if call_id not in seen_ids and call_id not in original_ids:
                        break
                errors[call_id] = "; ".join(call_errors)
            else:
                call_id = raw_id
            seen_ids.add(call_id)
            normalized.append({
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            })
        return normalized, errors

    @staticmethod
    def _append_rejected_tool_results(context: ContextManager, child_state: AgentState,
                                      calls: list[dict[str, Any]], error_kind: str,
                                      detail: str) -> None:
        """Close a child tool round without entering any advertised handler."""
        for call in calls:
            function = call["function"]
            arguments = json.loads(function["arguments"])
            content = json.dumps({
                "status": "error", "error_kind": error_kind,
                "message": detail,
            }, ensure_ascii=False)
            execution = ExecutionResult(
                function["name"], arguments, "not_checked", False, "invalid",
                0, "none", content, content[:200], error_kind=error_kind,
            )
            child_state.record_execution_result(execution)
            context.history.append({
                "role": "tool", "tool_call_id": call["id"],
                "content": execution.tool_content(),
            })

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
        fallback = SubagentResult(
            result.result_id, result.delegation_id, result.subagent_id, result.parent_task_id,
            "failed", "子代理结果超过大小上限", (), (), ("result_too_large",),
            UsageRecord(result.usage.rounds, result.usage.llm_calls, result.usage.tool_calls,
                        result.usage.tokens, result.usage.token_accounting, 0, result.usage.elapsed_ms),
            result.contract_hash, result.started_at, _utc_now(), "result_too_large",
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

    def run(self, task: DelegatedTask) -> SubagentResult:
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
        policy = PermissionPolicy({name: ALLOW for name in task.allowed_tools})
        executor = ToolExecutor(view, gate=PermissionGate(policy))
        instructions = InstructionLoader(self.workspace_root).load()
        system = build_subagent_prompt(task, instructions, self.workspace_root)
        history: list[dict[str, Any]] = [{
            "role": "user",
            "content": json.dumps({"contract": task.to_dict(), "selected_parent_facts": list(task.selected_parent_facts)},
                                   ensure_ascii=False, sort_keys=True),
        }]
        context = ContextManager(
            child_state, history, observability=False,
            protected_messages=[{"role": "system", "content": system}],
        )
        self.last_context = context
        llm = self._llm_callable()
        runtime = AgentRuntime(
            llm_client=llm, context=context, executor=executor,
            max_rounds=budget.max_rounds,
        )
        llm_calls = tool_calls = rounds = tokens = 0
        correction_used = False
        limitations: list[str] = []
        observations: list[dict[str, Any]] = []
        while True:
            if time.monotonic() - started_clock > budget.timeout_seconds:
                usage = UsageRecord(rounds, llm_calls, tool_calls, tokens, "estimated", 0,
                                    int((time.monotonic() - started_clock) * 1000))
                return self._make_result(task, "timed_out", "子代理达到墙钟时间上限", limitations=limitations + ["timeout"], usage=usage, started_at=started_at, error_kind="timeout")
            if rounds >= budget.max_rounds or llm_calls >= budget.max_llm_calls:
                usage = UsageRecord(rounds, llm_calls, tool_calls, tokens, "estimated", 0,
                                    int((time.monotonic() - started_clock) * 1000))
                return self._make_result(task, "budget_exhausted", "子代理预算耗尽", limitations=limitations + ["round_or_llm_budget"], usage=usage, started_at=started_at, error_kind="budget_exhausted")
            prepared = context.prepare_messages()
            request_tokens = count_tokens(prepared)
            if tokens + request_tokens >= budget.max_tokens:
                usage = UsageRecord(rounds, llm_calls, tool_calls, tokens, "estimated", 0,
                                    int((time.monotonic() - started_clock) * 1000))
                return self._make_result(task, "budget_exhausted", "子代理 token 预算耗尽", limitations=limitations + ["token_budget"], usage=usage, started_at=started_at, error_kind="budget_exhausted")
            remaining_timeout = max(
                0.001, budget.timeout_seconds - (time.monotonic() - started_clock),
            )
            llm_calls += 1
            rounds += 1
            try:
                message = runtime.invoke(prepared, remaining_timeout)
            except (TimeoutError, socket.timeout) as error:
                usage = UsageRecord(rounds, llm_calls, tool_calls, tokens + request_tokens,
                                    "estimated", 0, int((time.monotonic() - started_clock) * 1000))
                return self._make_result(task, "timed_out", "子代理 LLM 请求超时", limitations=limitations + [_safe_error_detail(error)], usage=usage, started_at=started_at, error_kind="timeout")
            except Exception as error:
                usage = UsageRecord(rounds, llm_calls, tool_calls, tokens + request_tokens,
                                    "estimated", 0, int((time.monotonic() - started_clock) * 1000))
                return self._make_result(task, "failed", "子代理 LLM 调用失败", limitations=limitations + [_safe_error_detail(error)], usage=usage, started_at=started_at, error_kind="llm_error")
            tokens += request_tokens + count_tokens(message)
            if time.monotonic() - started_clock > budget.timeout_seconds:
                usage = UsageRecord(rounds, llm_calls, tool_calls, tokens, "estimated", 0,
                                    int((time.monotonic() - started_clock) * 1000))
                return self._make_result(task, "timed_out", "子代理达到墙钟时间上限", limitations=limitations + ["timeout"], usage=usage, started_at=started_at, error_kind="timeout")
            if tokens > budget.max_tokens:
                usage = UsageRecord(rounds, llm_calls, tool_calls, tokens, "estimated", 0,
                                    int((time.monotonic() - started_clock) * 1000))
                return self._make_result(task, "budget_exhausted", "子代理 token 预算耗尽", limitations=limitations + ["token_budget"], usage=usage, started_at=started_at, error_kind="budget_exhausted")
            try:
                calls, call_errors = self._normalize_calls(message)
            except DelegationError as error:
                return self._make_result(task, "failed", "子代理工具调用协议非法", limitations=limitations + [_safe_error_detail(error)], usage=UsageRecord(rounds, llm_calls, tool_calls, tokens, "estimated", 0, int((time.monotonic() - started_clock) * 1000)), started_at=started_at, error_kind="invalid_tool_call")
            assistant = {"role": "assistant", "content": message.get("content"), **({"tool_calls": calls} if calls else {})}
            context.history.append(assistant)
            if call_errors:
                self._append_rejected_tool_results(
                    context, child_state, calls, "invalid_tool_call",
                    "子代理工具调用协议非法: " + "; ".join(call_errors.values()),
                )
                return self._make_result(task, "failed", "子代理工具调用协议非法", limitations=limitations + list(call_errors.values()), usage=UsageRecord(rounds, llm_calls, tool_calls, tokens, "estimated", 0, int((time.monotonic() - started_clock) * 1000)), started_at=started_at, error_kind="invalid_tool_call")
            if not calls:
                content = message.get("content")
                try:
                    report = json.loads(content) if isinstance(content, str) else None
                    summary, findings, evidence, report_limitations = self._parse_report(
                        task, report, scope_gate, observations,
                    )
                except (TypeError, ValueError, json.JSONDecodeError, DelegationError) as error:
                    if calls or correction_used:
                        return self._make_result(task, "failed", "子代理最终报告非法", limitations=limitations + ["invalid_result", _safe_error_detail(error)], usage=UsageRecord(rounds, llm_calls, tool_calls, tokens, "estimated", 0, int((time.monotonic() - started_clock) * 1000)), started_at=started_at, error_kind="invalid_result")
                    correction_used = True
                    context.set_runtime_notice(
                        "Runtime Notice：格式修正：上一次输出不是合法报告。下一次必须只输出严格 JSON，字段恰为 summary、findings、evidence、limitations；不得调用工具。"
                    )
                    continue
                usage = UsageRecord(rounds, llm_calls, tool_calls, tokens, "estimated", 0,
                                    int((time.monotonic() - started_clock) * 1000))
                return self._make_result(task, "completed", summary, findings, evidence,
                                         limitations + list(report_limitations), usage, started_at)
            if correction_used:
                self._append_rejected_tool_results(
                    context, child_state, calls, "invalid_result",
                    "格式修正阶段不得再次发起工具调用",
                )
                return self._make_result(task, "failed", "格式修正阶段再次发起工具调用", limitations=limitations + ["invalid_result"], usage=UsageRecord(rounds, llm_calls, tool_calls, tokens, "estimated", 0, int((time.monotonic() - started_clock) * 1000)), started_at=started_at, error_kind="invalid_result")
            if tool_calls + len(calls) > budget.max_tool_calls:
                self._append_rejected_tool_results(
                    context, child_state, calls, "budget_exhausted",
                    "子代理工具调用预算耗尽，当前回合未进入 handler",
                )
                return self._make_result(task, "budget_exhausted", "子代理工具调用预算耗尽", limitations=limitations + ["tool_budget"], usage=UsageRecord(rounds, llm_calls, tool_calls, tokens, "estimated", 0, int((time.monotonic() - started_clock) * 1000)), started_at=started_at, error_kind="budget_exhausted")
            tool_calls += len(calls)
            for call in calls:
                function = call["function"]
                name = function["name"]
                try:
                    args = json.loads(function["arguments"])
                except json.JSONDecodeError:
                    args = {}
                result = executor.execute_result(name, args, state=child_state, notify=False)
                if isinstance(result, ExecutionResult):
                    child_state.record_execution_result(result)
                    content = result.tool_content()
                    if result.ok and result.handler_admitted:
                        derived = self._observations_for_execution(result, scope_gate)
                        observations.extend(derived)
                        content += "\n[observation_hash=" + derived[0]["hash"] + "]"
                else:
                    content = str(result)
                context.history.append({"role": "tool", "tool_call_id": call["id"], "content": content})


class DelegationManager:
    """Own the single synchronous child slot for one parent runtime."""

    def __init__(self, workspace_root: str | os.PathLike[str] | None = None, *,
                 subagent_llm: Callable | None = None, llm: Callable | None = None,
                 llm_callable: Callable | None = None,
                 parent_registry: ToolRegistry | None = None):
        self.workspace_root = os.path.realpath(os.path.abspath(os.fspath(workspace_root or os.getcwd())))
        self.subagent_llm = subagent_llm if subagent_llm is not None else (
            llm if llm is not None else llm_callable
        )
        self.parent_registry = parent_registry
        self._lock = Lock()
        self._active = False
        self.last_task: DelegatedTask | None = None
        self.last_result: SubagentResult | None = None

    def create_task(self, arguments: dict[str, Any], state: AgentState | None = None) -> DelegatedTask:
        return build_delegated_task(arguments, state, workspace_root=self.workspace_root)

    def run(self, arguments: dict[str, Any] | DelegatedTask,
            state: AgentState | None = None) -> SubagentResult:
        try:
            task = arguments if isinstance(arguments, DelegatedTask) else self.create_task(arguments, state)
        except Exception as error:
            # A direct Manager caller still receives the same bounded result shape.
            now = _utc_now()
            return SubagentResult(str(uuid4()), "", "", getattr(state, "task_id", "") if state else "",
                                  "failed", "委派合同非法", (), (), (_safe_error_detail(error),),
                                  UsageRecord(token_accounting="estimated"), "", now, now, "invalid_contract")
        with self._lock:
            if self._active:
                result = SubagentResult(
                    str(uuid4()), task.delegation_id, task.subagent_id, task.parent_task_id,
                    "failed", "当前已有子代理运行", (), (), ("delegation_busy",),
                    UsageRecord(token_accounting="estimated"), task.contract_hash,
                    _utc_now(), _utc_now(), "delegation_busy",
                )
                self.last_result = result
                return result
            self._active = True
        self.last_task = task
        try:
            result = SubagentRunner(
                self.workspace_root, llm=self.subagent_llm,
                parent_registry=self.parent_registry,
            ).run(task)
            self.last_result = result
            return result
        except Exception as error:
            result = SubagentResult(
                str(uuid4()), task.delegation_id, task.subagent_id, task.parent_task_id,
                "failed", "子代理运行失败", (), (), (_safe_error_detail(error),),
                UsageRecord(token_accounting="estimated"), task.contract_hash,
                _utc_now(), _utc_now(), "runner_error",
            )
            self.last_result = result
            return result
        finally:
            with self._lock:
                self._active = False

    delegate = run
