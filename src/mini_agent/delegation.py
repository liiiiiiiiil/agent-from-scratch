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
import stat
from collections import deque
from queue import Empty, Queue
from threading import Condition, Event, Lock, Thread
import time
from typing import Any, Callable, Literal
from uuid import uuid4

from mini_agent.agent_profiles import AgentProfile, AgentProfileCatalog
from mini_agent.context import ContextBudget, ContextManager, count_tokens
from mini_agent.instructions import InstructionLoader
from mini_agent.permission import ALLOW, DENY, PermissionGate, PermissionPolicy
from mini_agent.prompt import build_subagent_prompt
from mini_agent.providers.base import ProviderResponse
from mini_agent.providers.catalog import ModelBinding, ModelBindingRef, ProviderCatalog
from mini_agent.runtime import AgentRuntime, RuntimeDecision, ToolRoundPlan
from mini_agent.state import (
    AgentState, MAX_CONCURRENCY, delegation_result_hash,
    CHILD_SESSION_MAX_ELAPSED_MS, CHILD_SESSION_MAX_LLM_CALLS,
    CHILD_SESSION_MAX_ROUNDS, CHILD_SESSION_MAX_TOKENS,
    CHILD_SESSION_MAX_TOOL_CALLS,
)
from mini_agent.tools.base import (
    ExecutionResult,
    ToolExecutor,
    ToolRegistry,
)
from mini_agent.skills import SkillCatalog, SkillAccessError


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
CHILD_SESSION_MAX_SNAPSHOT_BYTES = 256 * 1024
CHILD_SESSION_MAX_TOTAL_SNAPSHOT_BYTES = 720 * 1024
CHILD_SESSION_MAX_OBSERVATIONS = 256
_HASH_RE = re.compile(r"\A[0-9a-fA-F]{64}\Z")
_UUID_RE = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)
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

    def __init__(self, workspace_root: str | os.PathLike[str], scope: list[str] | tuple[str, ...],
                 *, session_root: str | os.PathLike[str] | None = None):
        self.workspace_root = os.path.realpath(os.path.abspath(os.fspath(workspace_root)))
        if not os.path.isdir(self.workspace_root):
            raise DelegationError("workspace_root 必须是目录")
        root_info = os.stat(self.workspace_root, follow_symlinks=False)
        self._root_identity = (root_info.st_dev, root_info.st_ino)
        home_sessions = os.path.realpath(os.path.expanduser("~/.mini_agent/sessions"))
        workspace_sensitive = (
            os.path.join(self.workspace_root, ".mini_agent", "sessions"),
            os.path.join(self.workspace_root, "sessions"),
            os.path.join(self.workspace_root, ".sessions"),
        )
        sensitive_roots = (*workspace_sensitive, home_sessions)
        if session_root is not None:
            sensitive_roots += (os.fspath(session_root),)
        self._sensitive_roots = tuple(dict.fromkeys(
            os.path.realpath(path) for path in sensitive_roots
        ))
        self._sensitive_config = os.path.realpath(
            os.path.join(self.workspace_root, "config_local.py")
        )
        if not isinstance(scope, (list, tuple)) or not 1 <= len(scope) <= DELEGATION_MAX_SCOPE:
            raise DelegationError("scope 必须包含 1–8 个相对路径")
        self.scope = tuple(self._validate_relative(item, "scope") for item in scope)
        self._scope_realpaths = tuple(self._resolve(item) for item in self.scope)
        for path in self._scope_realpaths:
            self._require_inside_workspace(path)
            self._reject_sensitive(path, "scope")

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

    def _reject_sensitive(self, path: str, field_name: str) -> None:
        normalized = os.path.realpath(os.path.abspath(path))
        if os.path.basename(normalized).casefold() == "config_local.py" or normalized == self._sensitive_config:
            raise DelegationError(f"{field_name} 不允许访问 config_local.py")
        for root in self._sensitive_roots:
            try:
                common = os.path.commonpath([root, normalized])
            except ValueError:
                continue
            if common == root:
                raise DelegationError(f"{field_name} 不允许访问 session 敏感目录")

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
        self._reject_sensitive(resolved, "path")
        if not self._inside_scope(resolved):
            raise DelegationError("path 不在委派 scope 内")
        return resolved

    def validate_resolved_path(self, path: Any) -> str:
        """Recheck a canonical path handed from registry admission to a reader."""
        if not isinstance(path, str) or not os.path.isabs(path):
            raise DelegationError("内部读取路径必须是绝对 canonical path")
        absolute = os.path.abspath(path)
        resolved = os.path.realpath(absolute)
        if absolute != resolved:
            raise DelegationError("读取期间路径解析发生变化")
        self._require_inside_workspace(resolved)
        self._reject_sensitive(resolved, "path")
        if not self._inside_scope(resolved):
            raise DelegationError("path 不在委派 scope 内")
        return resolved

    def validate_tool_call(self, name: str, arguments: dict[str, Any]) -> None:
        # The filtered registry admits ``skill`` only when the role runner has
        # supplied its parent-authorized restricted catalog. It is not a path
        # reader, so it needs no workspace scope normalization here.
        if name == "skill":
            return
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

    def _open_child_path(self, path: str, *, directory: bool) -> int:
        """Open from the frozen workspace directory without following symlinks."""
        canonical = self.validate_resolved_path(path)
        if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
            raise DelegationError("当前平台缺少安全目录 fd 读取能力")
        relative = os.path.relpath(canonical, self.workspace_root)
        root_parts = [part for part in self.workspace_root.split(os.sep) if part]
        parts = [] if relative == "." else relative.split(os.sep)
        flags = os.O_RDONLY | os.O_NOFOLLOW
        fd = os.open(os.path.sep, flags | os.O_DIRECTORY)
        try:
            for part in root_parts:
                next_fd = os.open(part, flags | os.O_DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            root_info = os.fstat(fd)
            if (root_info.st_dev, root_info.st_ino) != self._root_identity:
                raise DelegationError("工作区根目录身份发生变化")
            for index, part in enumerate(parts):
                last = index == len(parts) - 1
                next_fd = os.open(
                    part, flags | (os.O_DIRECTORY if not last or directory else 0),
                    dir_fd=fd,
                )
                os.close(fd)
                fd = next_fd
            mode = os.fstat(fd).st_mode
            if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
                raise DelegationError("子代理只能读取普通文件或目录")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def wrap_handler(self, name: str, handler: Callable) -> Callable:
        """Keep broad directory readers from exposing the local config file."""
        if name == "read_file":
            def safe_read_file(path, offset=0, limit=2000):
                fd = self._open_child_path(path, directory=False)
                with os.fdopen(fd, "r", encoding="utf-8") as stream:
                    lines = stream.readlines()
                total = len(lines)
                start = max(0, offset)
                end = min(start + limit, total)
                numbered = [f"{index + 1:05d}|{lines[index]}" for index in range(start, end)]
                if end < total:
                    suffix = f"\n(共 {total} 行，已读 {end - start} 行，还有 {total - end} 行未读)"
                else:
                    suffix = f"\n(End of file - 共 {total} 行)" if total > 0 else ""
                return "".join(numbered) + suffix
            return safe_read_file
        if name == "list_dir":
            def safe_list_dir(path="."):
                canonical = self.validate_resolved_path(path)
                fd = self._open_child_path(canonical, directory=True)
                try:
                    entries = []
                    for entry in sorted(os.listdir(fd)):
                        candidate = os.path.join(canonical, entry)
                        try:
                            self._reject_sensitive(candidate, "path")
                            info = os.stat(entry, dir_fd=fd, follow_symlinks=False)
                        except (DelegationError, OSError):
                            continue
                        if stat.S_ISLNK(info.st_mode):
                            continue
                        entries.append(entry + ("/" if stat.S_ISDIR(info.st_mode) else ""))
                    if not entries:
                        return f"目录为空: {canonical}"
                    rendered = "\n".join(entries[:200])
                    if len(entries) > 200:
                        rendered += f"\n(共 {len(entries)} 条，仅显示前 200 条)"
                    return rendered
                finally:
                    os.close(fd)
            return safe_list_dir
        if name != "grep":
            return handler

        def safe_grep(pattern: str, path=".", include="*"):
            canonical_root = self.validate_resolved_path(path)
            regex = re.compile(pattern)
            results: list[str] = []
            max_results = 100
            root_fd = self._open_child_path(canonical_root, directory=True)

            def walk(directory_fd: int, directory_path: str) -> None:
                for entry in sorted(os.listdir(directory_fd)):
                    if len(results) >= max_results:
                        return
                    candidate = os.path.join(directory_path, entry)
                    try:
                        self._reject_sensitive(candidate, "path")
                        if not self._inside_scope(candidate):
                            continue
                        info = os.stat(entry, dir_fd=directory_fd, follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode):
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            child_fd = os.open(
                                entry, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=directory_fd,
                            )
                            try:
                                walk(child_fd, candidate)
                            finally:
                                os.close(child_fd)
                        elif stat.S_ISREG(info.st_mode) and fnmatch.fnmatch(entry, include):
                            file_fd = os.open(
                                entry, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd,
                            )
                            with os.fdopen(file_fd, "r", encoding="utf-8", errors="ignore") as stream:
                                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                                    continue
                                display_path = os.path.relpath(candidate, self.workspace_root)
                                for line_number, line in enumerate(stream, 1):
                                    if regex.search(line):
                                        results.append(f"{display_path}:{line_number}: {line.rstrip()}")
                                        if len(results) >= max_results:
                                            return
                    except (DelegationError, PermissionError, OSError):
                        continue

            try:
                walk(root_fd, canonical_root)
            finally:
                os.close(root_fd)
            if len(results) >= max_results:
                return "\n".join(results) + f"\n(结果已达 {max_results} 条上限)"
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
    agent_profile: str | None = None
    agent_profile_fingerprint: str | None = None
    # Parent-approved skill IDs are ephemeral Runtime grants. They are never
    # part of the durable contract or session.
    authorized_skills: tuple[str, ...] = field(default=(), repr=False, compare=False)

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
        if self.agent_profile is None:
            if self.agent_profile_fingerprint is not None:
                raise DelegationError("未指定 agent_profile 时不能有角色指纹")
        elif (not isinstance(self.agent_profile, str)
              or not isinstance(self.agent_profile_fingerprint, str)
              or not _HASH_RE.fullmatch(self.agent_profile_fingerprint)):
            raise DelegationError("agent_profile 身份无效")
        if not self.created_at:
            object.__setattr__(self, "created_at", _utc_now())
        expected = _contract_hash(self)
        if self.contract_hash and self.contract_hash != expected:
            raise DelegationError("contract_hash 不匹配")
        object.__setattr__(self, "contract_hash", expected)

    def to_dict(self) -> dict[str, Any]:
        result = {
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
        if self.agent_profile is not None:
            result["agent_profile"] = self.agent_profile
            result["agent_profile_fingerprint"] = self.agent_profile_fingerprint
        return result


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
    if task.agent_profile is not None:
        payload["agent_profile"] = task.agent_profile
        payload["agent_profile_fingerprint"] = task.agent_profile_fingerprint
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
    agent_profile: str | None = None
    agent_profile_fingerprint: str | None = None
    round_index: int | None = None

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
            **({"agent_profile": self.agent_profile} if self.agent_profile else {}),
            **({"agent_profile_fingerprint": self.agent_profile_fingerprint}
               if self.agent_profile_fingerprint else {}),
            **({"round_index": self.round_index} if self.round_index is not None else {}),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class ChildSessionSnapshot:
    """Private, bounded child Context checkpoint attached to a parent safe point."""

    child_session_id: str
    parent_task_id: str
    workspace_fingerprint: str
    agent_profile: str
    agent_profile_fingerprint: str
    model_binding_ref: dict[str, str] | None
    skill_identities: tuple[dict[str, str], ...]
    round_index: int
    cumulative_usage: UsageRecord
    last_claimed_result_id: str
    last_claimed_result_hash: str
    last_result_json: str
    observations: tuple[dict[str, Any], ...]
    context: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "mini_agent.child_session", "format_version": 1,
            "child_session_id": self.child_session_id,
            "parent_task_id": self.parent_task_id,
            "workspace_fingerprint": self.workspace_fingerprint,
            "agent_profile": self.agent_profile,
            "agent_profile_fingerprint": self.agent_profile_fingerprint,
            "model_binding_ref": deepcopy(self.model_binding_ref),
            "skill_identities": [deepcopy(item) for item in self.skill_identities],
            "round_index": self.round_index,
            "cumulative_usage": self.cumulative_usage.to_dict(),
            "last_claimed_result_id": self.last_claimed_result_id,
            "last_claimed_result_hash": self.last_claimed_result_hash,
            "last_result_json": self.last_result_json,
            "observations": [deepcopy(item) for item in self.observations],
            "context": deepcopy(self.context),
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ChildSessionSnapshot":
        if not isinstance(value, dict) or value.get("format") != "mini_agent.child_session" or value.get("format_version") != 1:
            raise DelegationError("child session 快照格式无效")
        try:
            snapshot = cls(
                value["child_session_id"], value["parent_task_id"],
                value["workspace_fingerprint"], value["agent_profile"],
                value["agent_profile_fingerprint"], deepcopy(value.get("model_binding_ref")),
                tuple(deepcopy(value.get("skill_identities", []))),
                value["round_index"], UsageRecord(**value["cumulative_usage"]),
                value["last_claimed_result_id"], value["last_claimed_result_hash"],
                value["last_result_json"], tuple(deepcopy(value.get("observations", []))),
                deepcopy(value["context"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise DelegationError(f"child session 快照字段无效: {error}") from error
        if (not isinstance(snapshot.child_session_id, str)
                or not _UUID_RE.fullmatch(snapshot.child_session_id)
                or isinstance(snapshot.round_index, bool)
                or not isinstance(snapshot.round_index, int)
                or not 1 <= snapshot.round_index <= CHILD_SESSION_MAX_ROUNDS
                or not _HASH_RE.fullmatch(snapshot.workspace_fingerprint)
                or not _HASH_RE.fullmatch(snapshot.agent_profile_fingerprint)
                or not _HASH_RE.fullmatch(snapshot.last_claimed_result_hash)):
            raise DelegationError("child session 快照身份或轮次无效")
        if delegation_result_hash(json.loads(snapshot.last_result_json)) != snapshot.last_claimed_result_hash:
            raise DelegationError("child session 最近结果 hash 不匹配")
        if len(snapshot.observations) > CHILD_SESSION_MAX_OBSERVATIONS:
            raise DelegationError("child session 观察事实超过上限")
        encoded = json.dumps(snapshot.to_dict(), ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        if len(encoded) > CHILD_SESSION_MAX_SNAPSHOT_BYTES:
            raise DelegationError(f"child session {snapshot.child_session_id} 快照超过大小上限")
        return snapshot


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
    agent_profile_catalog: AgentProfileCatalog | None = None,
) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise DelegationError("delegate_task 参数必须是对象")
    allowed = {
        "goal", "scope", "constraints", "expected_findings", "requested_tools",
        "selected_parent_facts", "purpose", "source_id", "budget", "model_profile",
        "agent_profile",
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
    role: AgentProfile | None = None
    agent_profile = arguments.get("agent_profile")
    if "agent_profile" in arguments and agent_profile is None:
        raise DelegationError("agent_profile 必须省略或提供有效角色 ID")
    if agent_profile is not None:
        agent_profile = _require_string(agent_profile, "agent_profile", max_length=64)
        if agent_profile_catalog is None:
            raise DelegationError("agent_profile Catalog 未装配")
        try:
            role = agent_profile_catalog.resolve(agent_profile)
        except ValueError as error:
            raise DelegationError(str(error)) from error
        if provider_catalog is not None:
            role_model = provider_catalog.resolve_child_profile(role.model_profile)
            if requested_profile is not None and selected_profile != role_model:
                raise DelegationError(
                    "同时指定 agent_profile 与 model_profile 时，两者必须解析为同一子模型"
                )
            selected_profile = role_model
        elif requested_profile is not None:
            raise DelegationError("显式 model_profile 需要 ProviderCatalog")
        effective_tools = tuple(
            tool for tool in requested
            if tool in role.tools and role.permission_for(tool) != "deny"
        )
        if not effective_tools:
            raise DelegationError("requested_tools 与角色工具及权限没有交集")
    if state is not None and hasattr(state, "delegation_gate"):
        detail = state.delegation_gate(
            "delegate_task", {**arguments, "purpose": purpose, "source_id": source_id}, "none",
        )
        if detail:
            raise DelegationError(detail)
    normalized = {
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
    if role is not None:
        normalized["agent_profile"] = role.profile_id
        normalized["agent_profile_fingerprint"] = role.fingerprint
        normalized["allowed_tools"] = list(effective_tools)
    return normalized


def build_delegated_task(arguments: dict[str, Any], state: AgentState | None = None,
                         *, workspace_root: str | os.PathLike[str],
                         provider_catalog: ProviderCatalog | None = None,
                         agent_profile_catalog: AgentProfileCatalog | None = None,
                         session_root: str | os.PathLike[str] | None = None) -> DelegatedTask:
    normalized = validate_delegation_arguments(
        arguments, state, provider_catalog, agent_profile_catalog,
    )
    ScopeGate(workspace_root, normalized["scope"], session_root=session_root)
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
        allowed_tools=tuple(normalized.get(
            "allowed_tools",
            [tool for tool in normalized["requested_tools"] if tool in ALLOWED_SUBAGENT_TOOLS],
        )),
        selected_parent_facts=tuple(normalized["selected_parent_facts"]),
        purpose=normalized["purpose"],
        source_id=normalized["source_id"],
        budget=budget,
        model_profile=normalized["model_profile"],
        model_binding_ref=binding.reference if binding is not None else None,
        agent_profile=normalized.get("agent_profile"),
        agent_profile_fingerprint=normalized.get("agent_profile_fingerprint"),
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


def _workspace_fingerprint(workspace_root: str | os.PathLike[str]) -> str:
    normalized = os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(workspace_root))))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _skill_identity(catalog: SkillCatalog | None, skill_id: str) -> dict[str, str]:
    if catalog is None:
        raise DelegationError(f"恢复所需 Skill Catalog 不可用: {skill_id}")
    try:
        definition = catalog.get(skill_id)
    except SkillAccessError as error:
        raise DelegationError(f"恢复所需 Skill 不存在或不可用: {skill_id}") from error
    snapshot = definition.file_snapshot
    identity = {
        "skill_id": definition.name,
        "source": definition.source,
        "root_identity": list(definition.root_identity),
        "directory_identity": list(definition.directory_identity),
        "file": {
            "dev": snapshot.dev, "ino": snapshot.ino, "size": snapshot.size,
            "mtime_ns": snapshot.mtime_ns, "ctime_ns": snapshot.ctime_ns,
        },
    }
    fingerprint = hashlib.sha256(json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return {"skill_id": definition.name, "source": definition.source,
            "fingerprint": fingerprint}


class SubagentRunner:
    """Run one child synchronously with isolated state/context/permissions."""

    def __init__(self, workspace_root: str | os.PathLike[str], *, llm: Callable | None = None,
                 llm_callable: Callable | None = None,
                 parent_registry: ToolRegistry | None = None,
                 model_binding: ModelBinding | None = None,
                 agent_profile: AgentProfile | None = None,
                 skill_catalog: SkillCatalog | None = None,
                 session_root: str | os.PathLike[str] | None = None,
                 resume_snapshot: ChildSessionSnapshot | None = None,
                 round_index: int | None = None):
        self.workspace_root = os.path.realpath(os.path.abspath(os.fspath(workspace_root)))
        self.llm = llm if llm is not None else llm_callable
        self.parent_registry = parent_registry
        self.model_binding = model_binding
        self.agent_profile = agent_profile
        self.skill_catalog = skill_catalog
        self.session_root = session_root
        self.resume_snapshot = resume_snapshot
        self.round_index = round_index
        self.last_snapshot: ChildSessionSnapshot | None = None
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
                     error_detail: str | None = None,
                     round_index: int | None = None) -> SubagentResult:
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
            agent_profile=task.agent_profile,
            agent_profile_fingerprint=task.agent_profile_fingerprint,
            round_index=round_index,
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
        if task.agent_profile == "tester":
            prose = " ".join((
                summary,
                *(item.claim for item in findings),
                *(item.caveat or "" for item in findings),
                *(item.claim for item in evidence),
                *limitations,
            ))
            if re.search(
                    r"(?i)(?:\btests?\b.{0,48}\b(?:passed|succeeded|successful|are green)\b"
                    r"|\b(?:passed|succeeded|successful|green)\b.{0,48}\btests?\b"
                    r"|测试[^。！？\n]{0,32}(?:通过|成功|全绿)"
                    r"|(?:通过|成功|全绿)[^。！？\n]{0,32}测试)",
                    prose):
                raise DelegationError("tester 角色不能报告测试已执行或通过")
            if not any(re.search(r"(?i)(未执行|未运行|not run|not executed)", item)
                       for item in limitations):
                raise DelegationError("tester 报告必须明确说明本次未执行测试")
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
        scope_gate = ScopeGate(self.workspace_root, list(task.scope),
                               session_root=self.session_root)
        child_state = AgentState()
        child_state.begin_task(task.goal)
        self.last_state = child_state
        if self.parent_registry is None:
            from mini_agent.tools.calc import calculate_tool
            from mini_agent.tools.file import read_file_tool, list_dir_tool, grep_tool
            parent = ToolRegistry()
            for tool in (calculate_tool, read_file_tool, list_dir_tool, grep_tool):
                parent.register(tool)
            if self.skill_catalog is not None:
                from mini_agent.tools.skill import make_skill_tool
                parent.register(make_skill_tool(self.skill_catalog))
        else:
            parent = self.parent_registry
        child_skill_catalog = (
            self.skill_catalog.restricted_to(task.authorized_skills)
            if self.skill_catalog is not None and task.authorized_skills else None
        )
        child_tools = set(task.allowed_tools)
        if child_skill_catalog is not None and child_skill_catalog.definitions:
            child_tools.add("skill")
        view = parent.filtered_for_subagent(
            child_tools, scope_gate=scope_gate, skill_catalog=child_skill_catalog,
        )
        child_permissions: dict[str, Any] = {name: ALLOW for name in task.allowed_tools}
        if child_skill_catalog is not None:
            child_permissions["skill"] = {
                "*": DENY,
                **{item.name: ALLOW for item in child_skill_catalog.definitions},
            }
        executor = ToolExecutor(view, gate=PermissionGate(PermissionPolicy(child_permissions)))
        system = build_subagent_prompt(
            task, InstructionLoader(self.workspace_root).load(), self.workspace_root,
            role_profile=self.agent_profile,
        )
        contract_message: dict[str, Any] = {
            "role": "user",
            "content": json.dumps(
                {"contract": task.to_dict(), "selected_parent_facts": list(task.selected_parent_facts)},
                ensure_ascii=False, sort_keys=True,
            ),
        }
        policy = SubagentRuntimePolicy(
            runner=self, task=task, scope_gate=scope_gate,
            started_clock=started_clock, started_at=started_at,
            state=child_state, cancel_event=cancel_event,
            cancellation_reason=cancellation_reason,
        )
        context_budget = ContextBudget(
            window=(self.model_binding.profile.context_window
                    if self.model_binding is not None else 128_000),
            output_reserve_tokens=(self.model_binding.profile.max_output_tokens
                                   if self.model_binding is not None else None),
        )
        protected = [{"role": "system", "content": system}]
        skill_policy = (PermissionPolicy({"skill": child_permissions["skill"]})
                        if child_skill_catalog is not None else None)
        if self.resume_snapshot is None:
            context = ContextManager(
                child_state, [contract_message], observability=False,
                budget=context_budget, summarizer=policy.summarize,
                protected_messages=protected, model_binding=self.model_binding,
                usage_meter=(self.model_binding.usage_meter if self.model_binding is not None else None),
                skill_catalog=child_skill_catalog, permission_policy=skill_policy,
            )
        else:
            context = ContextManager.restore_session(
                child_state, self.resume_snapshot.context,
                budget=context_budget, summarizer=policy.summarize,
                observability=False, protected_messages=protected,
                model_binding=self.model_binding,
                usage_meter=(self.model_binding.usage_meter if self.model_binding is not None else None),
                skill_catalog=child_skill_catalog, permission_policy=skill_policy,
            )
            context.history.append(contract_message)
            for fact in self.resume_snapshot.observations:
                path = fact.get("path") if isinstance(fact, dict) else None
                if path is not None:
                    try:
                        scope_gate.validate_evidence_path(path)
                    except DelegationError:
                        continue
                policy.observations.append(deepcopy(fact))
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
        result = policy.result_from_runtime(runtime_result)
        if self.round_index is not None and result.round_index != self.round_index:
            result = replace(result, round_index=self.round_index)
        if self.round_index is not None and result.outcome == "completed":
            try:
                prior = self.resume_snapshot.cumulative_usage if self.resume_snapshot is not None else UsageRecord()
                usage = UsageRecord(
                    rounds=prior.rounds + result.usage.rounds,
                    llm_calls=prior.llm_calls + result.usage.llm_calls,
                    tool_calls=prior.tool_calls + result.usage.tool_calls,
                    input_tokens=prior.input_tokens + result.usage.input_tokens,
                    output_tokens=prior.output_tokens + result.usage.output_tokens,
                    token_accounting=(result.usage.token_accounting if prior.rounds == 0 else
                                      prior.token_accounting if prior.token_accounting == result.usage.token_accounting
                                      else "mixed"),
                    result_bytes=prior.result_bytes + result.usage.result_bytes,
                    elapsed_ms=prior.elapsed_ms + result.usage.elapsed_ms,
                )
                old_skills = {
                    item["skill_id"]: deepcopy(item)
                    for item in (self.resume_snapshot.skill_identities if self.resume_snapshot else ())
                }
                for skill_id in (self.agent_profile.skills if self.agent_profile is not None
                                 else task.authorized_skills):
                    old_skills[skill_id] = _skill_identity(self.skill_catalog, skill_id)
                observations = deepcopy(policy.observations)
                unique_observations: list[dict[str, Any]] = []
                seen_observations: set[str] = set()
                for fact in observations:
                    key = json.dumps(fact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    if key not in seen_observations:
                        seen_observations.add(key)
                        unique_observations.append(fact)
                result_json = result.to_json()
                candidate = ChildSessionSnapshot(
                    task.subagent_id, task.parent_task_id,
                    _workspace_fingerprint(self.workspace_root),
                    task.agent_profile or "", task.agent_profile_fingerprint or "",
                    task.model_binding_ref.to_dict() if task.model_binding_ref is not None else None,
                    tuple(old_skills[name] for name in sorted(old_skills)),
                    self.round_index, usage, result.result_id,
                    delegation_result_hash(result), result_json,
                    tuple(unique_observations), context.export_session(),
                )
                self.last_snapshot = ChildSessionSnapshot.from_dict(candidate.to_dict())
            except Exception as error:
                # The report and its actual usage remain valid even when its
                # private continuation snapshot cannot be retained.
                self.last_snapshot = None
                result = self._make_result(
                    task, "completed", result.summary, result.findings,
                    result.evidence,
                    result.limitations + ("子会话快照不可用，当前结果不能续接",),
                    result.usage, result.started_at,
                    "snapshot_unavailable", _safe_error_detail(error),
                    round_index=self.round_index,
                )
        return result


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
            round_index=self.runner.round_index,
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


@dataclass(frozen=True)
class _PreparedDelegation:
    index: int
    task: DelegatedTask
    cancel_event: Event


@dataclass
class _BackgroundDelegation:
    task: DelegatedTask
    state: AgentState | None
    round_index: int = 1
    resume_snapshot: ChildSessionSnapshot | None = None
    cancel_event: Event = field(default_factory=Event)
    status: str = "queued"
    startup_confirmed: bool = False
    round_committed: bool = False
    cancel_requested: bool = False
    result: SubagentResult | None = None
    candidate_snapshot: ChildSessionSnapshot | None = None
    thread: Thread | None = None


class DelegationScheduler:
    """Run a reserved delegation batch with bounded, index-addressed workers."""

    def __init__(self, manager: "DelegationManager", prepared: list[_PreparedDelegation],
                 max_concurrency: int, *, ready: dict[int, SubagentResult] | None = None,
                 on_result_ready: Callable[[int, Any], None] | None = None,
                 on_result: Callable[[int, Any], None] | None = None):
        self.manager = manager
        self.prepared = tuple(prepared)
        self.max_concurrency = max(1, min(max_concurrency, len(self.prepared)))
        self.ready = dict(ready or {})
        self.on_result_ready = on_result_ready
        self.on_result = on_result
        self._cancelled = False

    def cancel(self, reason: str = "task_boundary") -> None:
        self._cancelled = True
        for item in self.prepared:
            item.cancel_event.set()

    def _drain_ordered(self, results: dict[int, SubagentResult], next_index: list[int]) -> None:
        while next_index and next_index[0] in results:
            index = next_index.pop(0)
            result = results.pop(index)
            if self.on_result is not None:
                self.on_result(index, result)

    def run(self) -> dict[int, SubagentResult]:
        if not self.prepared:
            for index in sorted(self.ready):
                if self.on_result is not None:
                    self.on_result(index, self.ready[index])
            return {}
        pending = deque(self.prepared)
        active: dict[int, _PreparedDelegation] = {}
        results = dict(self.ready)
        next_index = sorted(set(results) | {item.index for item in self.prepared})
        completed: Queue[tuple[int, SubagentResult]] = Queue()

        def worker(item: _PreparedDelegation) -> None:
            acquired = False
            try:
                while not item.cancel_event.is_set():
                    if self.manager.acquire_concurrency_slot(timeout=0.05):
                        acquired = True
                        break
                if acquired:
                    result = self.manager._run_child(item.task, item.cancel_event)
                else:
                    result = self.manager._cancelled_result(item.task, "cancelled_before_slot")
            except BaseException as error:
                result = self.manager._runner_error_result(item.task, error)
            finally:
                if acquired:
                    self.manager.release_concurrency_slot()
            self.manager._child_finished(item.task, result)
            completed.put((item.index, result))

        def launch() -> None:
            if self._cancelled or self.manager.cancel_requested:
                return
            while pending and len(active) < self.max_concurrency:
                item = pending.popleft()
                active[item.index] = item
                try:
                    Thread(target=worker, args=(item,), daemon=True).start()
                except Exception as error:
                    active.pop(item.index)
                    result = self.manager._runner_error_result(item.task, error)
                    self.manager._child_finished(item.task, result)
                    results[item.index] = result

        try:
            self._drain_ordered(results, next_index)
            launch()
            self._drain_ordered(results, next_index)
            while active:
                index, result = completed.get()
                active.pop(index)
                results[index] = result
                if self.on_result_ready is not None:
                    self.on_result_ready(index, result)
                self._drain_ordered(results, next_index)
                launch()
                self._drain_ordered(results, next_index)
            # Cancellation leaves queued work without a worker. It still
            # needs a bounded result in the current parent tool round.
            while pending:
                item = pending.popleft()
                result = self.manager._cancelled_result(item.task, "scheduler_cancelled")
                results[item.index] = result
                self.manager._child_finished(item.task, result)
                self._drain_ordered(results, next_index)
        except BaseException:
            # A failed parent commit must return promptly. Running workers
            # keep their manager registration until they actually stop; daemon
            # threads cannot hold the CLI open after its bounded cleanup.
            self.manager.cancel(reason="parent_commit_failed", interrupt=False)
            while pending:
                item = pending.popleft()
                self.manager._child_finished(
                    item.task, self.manager._cancelled_result(item.task, "scheduler_cancelled"),
                )
            raise
        return results


class DelegationManager:
    """Own synchronous and process-local background Subagent lifecycles."""

    def __init__(self, workspace_root: str | os.PathLike[str] | None = None, *,
                 subagent_llm: Callable | None = None, llm: Callable | None = None,
                 llm_callable: Callable | None = None,
                 parent_registry: ToolRegistry | None = None,
                 provider_catalog: ProviderCatalog | None = None,
                 parent_state: AgentState | None = None,
                 agent_profile_catalog: AgentProfileCatalog | None = None,
                 skill_catalog: SkillCatalog | None = None,
                 session_root: str | os.PathLike[str] | None = None):
        self.workspace_root = os.path.realpath(os.path.abspath(os.fspath(workspace_root or os.getcwd())))
        self.subagent_llm = subagent_llm if subagent_llm is not None else (
            llm if llm is not None else llm_callable
        )
        self.parent_registry = parent_registry
        self.provider_catalog = provider_catalog
        self.parent_state = parent_state
        if agent_profile_catalog is None:
            from mini_agent import config as runtime_config
            agent_profile_catalog = AgentProfileCatalog(
                runtime_config.AGENT_PROFILES, provider_catalog=provider_catalog,
            )
        self.agent_profile_catalog = agent_profile_catalog
        self.skill_catalog = skill_catalog
        self.session_root = session_root
        self.parent_permission_gate: PermissionGate | None = None
        self._lock = Lock()
        self._active: dict[str, tuple[DelegatedTask, Event, AgentState | None]] = {}
        self._cancel_reason = ""
        self._done_event = Event()
        self._done_event.set()
        self._interrupted = False
        self._background: dict[str, _BackgroundDelegation] = {}
        self._background_events: Queue[tuple[str, SubagentResult, ChildSessionSnapshot | None]] = Queue()
        self._child_snapshots: dict[str, ChildSessionSnapshot] = {}
        self.resume_issues: list[dict[str, str]] = []
        self._concurrency_condition = Condition()
        self._running_workers = 0
        self.last_task: DelegatedTask | None = None
        self.last_result: SubagentResult | None = None

    def create_task(self, arguments: dict[str, Any], state: AgentState | None = None) -> DelegatedTask:
        if self.parent_state is None and state is not None:
            self.parent_state = state
        return build_delegated_task(
            arguments, state, workspace_root=self.workspace_root,
            provider_catalog=self.provider_catalog,
            agent_profile_catalog=self.agent_profile_catalog,
            session_root=self.session_root,
        )

    def bind_session_root(self, root: str | os.PathLike[str]) -> None:
        """Protect the actual durable store, including non-default locations."""
        self.session_root = os.path.realpath(os.path.abspath(os.fspath(root)))

    def bind_parent_permission_gate(self, gate: PermissionGate) -> None:
        """Bind the current parent executor's permission policy for Skill grants."""
        self.parent_permission_gate = gate

    def _concurrency_limit(self) -> int:
        configured = getattr(
            getattr(self.parent_state, "delegation_budget", None),
            "max_concurrency", MAX_CONCURRENCY,
        )
        if isinstance(configured, bool) or not isinstance(configured, int):
            configured = MAX_CONCURRENCY
        return max(1, min(MAX_CONCURRENCY, configured))

    def acquire_concurrency_slot(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._concurrency_condition:
            while self._running_workers >= self._concurrency_limit():
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._concurrency_condition.wait(
                    0.05 if remaining is None else min(0.05, remaining),
                )
            self._running_workers += 1
            return True

    def release_concurrency_slot(self) -> None:
        with self._concurrency_condition:
            if self._running_workers <= 0:
                raise RuntimeError("子代理并发槽位重复释放")
            self._running_workers -= 1
            self._concurrency_condition.notify_all()

    @staticmethod
    def _background_confirmation(task: DelegatedTask, accepted: bool,
                                 budget: dict[str, Any], reason: str | None = None,
                                 *, round_index: int = 1) -> str:
        payload: dict[str, Any] = {
            "child_session_id": task.subagent_id,
            "delegation_id": task.delegation_id,
            "round_index": round_index,
            "agent_profile": task.agent_profile,
            "status": "accepted" if accepted else "rejected",
            "accepted": accepted,
            "budget": {
                "max_rounds": task.budget.max_rounds,
                "max_llm_calls": task.budget.max_llm_calls,
                "max_tool_calls": task.budget.max_tool_calls,
                "max_tokens": task.budget.max_tokens,
                "timeout_seconds": task.budget.timeout_seconds,
                "parent_remaining": {
                    key: budget.get(key) for key in (
                        "remaining_subagents", "remaining_llm_calls",
                        "remaining_tool_calls", "remaining_tokens",
                    )
                },
            },
        }
        if reason:
            payload["reason"] = _safe_error_detail(reason, 300)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def spawn_background(self, arguments: dict[str, Any],
                         state: AgentState | None = None) -> str:
        """Reserve one named investigation and defer its worker until Runtime commit."""
        state = state or self.parent_state
        task: DelegatedTask | None = None
        try:
            task = self.create_task(arguments, state)
            if task.purpose != "investigation" or task.agent_profile is None:
                raise DelegationError("后台子代理必须指定角色且 purpose 固定为 investigation")
            task = self._authorize_role_skills(task)
            if state is None:
                raise DelegationError("后台子代理需要绑定父 State")
            state.reserve_delegation(task, mode="background")
            state.register_child_session(task, _workspace_fingerprint(self.workspace_root))
            with self._lock:
                if task.subagent_id in self._background:
                    raise DelegationError("child_session_id 已存在")
                self._background[task.subagent_id] = _BackgroundDelegation(task, state)
                self._done_event.clear()
            return self._background_confirmation(
                task, True, state.delegation_budget_snapshot(), round_index=1,
            )
        except Exception as error:
            if task is None:
                try:
                    # Keep a unique request identity even for a rejected contract.
                    task = self.create_task(arguments, state)
                except Exception:
                    return json.dumps({
                        "child_session_id": None, "delegation_id": None,
                        "agent_profile": arguments.get("agent_profile")
                        if isinstance(arguments, dict) else None,
                        "status": "rejected", "accepted": False,
                        "budget": {}, "reason": _safe_error_detail(error, 300),
                    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            snapshot = state.delegation_budget_snapshot() if state is not None else {}
            return self._background_confirmation(task, False, snapshot, str(error), round_index=1)

    def _prepare_followup_task(
        self, arguments: dict[str, Any], state: AgentState | None,
    ) -> tuple[DelegatedTask, ChildSessionSnapshot, int]:
        if not isinstance(arguments, dict):
            raise DelegationError("followup_subagent 参数必须是 object")
        child_id = arguments.get("child_session_id")
        if not isinstance(child_id, str) or _UUID_RE.fullmatch(child_id) is None:
            raise DelegationError("child_session_id 必须是 UUID")
        if arguments.get("purpose") != "investigation":
            raise DelegationError("followup_subagent 的 purpose 固定为 investigation")
        if "model_profile" in arguments or "agent_profile" in arguments:
            raise DelegationError("followup_subagent 不允许更换角色或模型")
        snapshot = self._child_snapshots.get(child_id)
        if snapshot is None:
            raise DelegationError("child_session_id 没有可续接的已领取成功快照")
        state = state or self.parent_state
        if state is None:
            raise DelegationError("followup_subagent 需要父 State")
        lifecycle = next((item for item in state.child_session_records
                          if item.child_session_id == child_id), None)
        if lifecycle is None or lifecycle.status != "idle":
            raise DelegationError("child_session_id 当前不可续接")
        if (snapshot.round_index != lifecycle.round_index
                or snapshot.last_claimed_result_id != lifecycle.last_claimed_result_id
                or snapshot.last_claimed_result_hash != lifecycle.last_claimed_result_hash
                or snapshot.cumulative_usage.to_dict() != asdict(lifecycle.cumulative_usage)):
            raise DelegationError("child_session 快照与最近已领取结果不一致")
        task_arguments = dict(arguments)
        task_arguments.pop("child_session_id", None)
        task_arguments["agent_profile"] = snapshot.agent_profile
        task = self.create_task(task_arguments, state)
        task = replace(task, subagent_id=child_id, contract_hash="")
        if (task.agent_profile_fingerprint != snapshot.agent_profile_fingerprint
                or (task.model_binding_ref.to_dict() if task.model_binding_ref else None)
                != snapshot.model_binding_ref):
            raise DelegationError("当前角色或模型绑定与冻结子会话不一致")
        for identity in snapshot.skill_identities:
            skill_id = identity["skill_id"]
            if _skill_identity(self.skill_catalog, skill_id) != identity:
                raise DelegationError(f"Skill 文件身份变化: {skill_id}")
            try:
                assert self.skill_catalog is not None
                self.skill_catalog.verify_identity(skill_id)
            except SkillAccessError as error:
                raise DelegationError(f"Skill 文件身份变化: {skill_id}") from error
        state.validate_background_followup(
            task, child_id, _workspace_fingerprint(self.workspace_root),
        )
        return task, snapshot, lifecycle.round_index + 1

    def validate_followup_arguments(
        self, arguments: dict[str, Any], state: AgentState | None = None,
    ) -> None:
        """Run full read-only contract, identity, scope, and budget checks before handler entry."""
        self._prepare_followup_task(arguments, state)

    def followup_background(self, arguments: dict[str, Any],
                            state: AgentState | None = None) -> str:
        state = state or self.parent_state
        task: DelegatedTask | None = None
        try:
            task, snapshot, round_index = self._prepare_followup_task(arguments, state)
            task = self._authorize_role_skills(task)
            assert state is not None
            state.reserve_background_followup(
                task, task.subagent_id, _workspace_fingerprint(self.workspace_root),
            )
            item = _BackgroundDelegation(
                task, state, round_index=round_index, resume_snapshot=snapshot,
            )
            with self._lock:
                current = self._background.get(task.subagent_id)
                if current is not None and current.status not in {"claimed", "abandoned", "interrupted"}:
                    raise DelegationError("child_session_id 已有活动回合")
                self._background[task.subagent_id] = item
                self._done_event.clear()
            return self._background_confirmation(
                task, True, state.delegation_budget_snapshot(), round_index=round_index,
            )
        except Exception as error:
            child_id = (arguments.get("child_session_id")
                        if isinstance(arguments, dict) else None)
            return json.dumps({
                "child_session_id": child_id,
                "delegation_id": task.delegation_id if task is not None else None,
                "round_index": None,
                "status": "rejected", "accepted": False,
                "budget": state.delegation_budget_snapshot() if state is not None else {},
                "reason": _safe_error_detail(error, 300),
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def export_child_sessions(self, state: AgentState | None = None) -> list[dict[str, Any]]:
        state = state or self.parent_state
        if state is None:
            return []
        snapshots: list[dict[str, Any]] = []
        for lifecycle in state.child_session_records:
            if lifecycle.status != "idle":
                continue
            snapshot = self._child_snapshots.get(lifecycle.child_session_id)
            if snapshot is None:
                raise DelegationError(
                    f"child_session_id={lifecycle.child_session_id} 缺少可保存的 idle 快照"
                )
            snapshots.append(snapshot.to_dict())
        total = sum(len(json.dumps(
            item, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")) for item in snapshots)
        if total > CHILD_SESSION_MAX_TOTAL_SNAPSHOT_BYTES:
            raise DelegationError(
                "child_sessions 总大小超过 720 KiB；child_session_id="
                + ",".join(item["child_session_id"] for item in snapshots)
            )
        return snapshots

    def restore_child_sessions(
        self, snapshots: list[dict[str, Any]], state: AgentState,
    ) -> list[dict[str, str]]:
        """Rebind idle snapshots to current local role/model/Skill catalogs."""
        issues: list[dict[str, str]] = []
        self._child_snapshots.clear()
        for raw in snapshots:
            child_id = str(raw.get("child_session_id", "<unknown>")) if isinstance(raw, dict) else "<unknown>"
            try:
                snapshot = ChildSessionSnapshot.from_dict(raw)
                lifecycle = next((item for item in state.child_session_records
                                  if item.child_session_id == snapshot.child_session_id), None)
                if lifecycle is None or lifecycle.status != "idle":
                    raise DelegationError("State lifecycle 不是 idle")
                if snapshot.parent_task_id != state.task_id or lifecycle.parent_task_id != state.task_id:
                    raise DelegationError("父 task_id 不匹配")
                if snapshot.workspace_fingerprint != _workspace_fingerprint(self.workspace_root):
                    raise DelegationError("workspace 身份变化")
                profile = self.agent_profile_catalog.resolve(snapshot.agent_profile)
                if profile.fingerprint != snapshot.agent_profile_fingerprint:
                    raise DelegationError("agent_profile 指纹变化")
                selected = (self.provider_catalog.resolve_child_profile(profile.model_profile)
                            if self.provider_catalog is not None else None)
                binding = (self.provider_catalog.bind(selected)
                           if self.provider_catalog is not None and selected is not None else None)
                current_ref = binding.reference.to_dict() if binding is not None else None
                if current_ref != snapshot.model_binding_ref:
                    raise DelegationError("model binding 指纹变化")
                if current_ref != lifecycle.model_binding_ref:
                    raise DelegationError("State model binding 来源摘要不匹配")
                current_skills = {
                    item["skill_id"]: item
                    for item in (_skill_identity(self.skill_catalog, skill_id)
                                 for skill_id in profile.skills)
                }
                saved_skills = {item["skill_id"]: item for item in snapshot.skill_identities}
                if current_skills != saved_skills:
                    raise DelegationError("Skill Catalog 或文件身份变化")
                if (snapshot.round_index != lifecycle.round_index
                        or snapshot.last_claimed_result_id != lifecycle.last_claimed_result_id
                        or snapshot.last_claimed_result_hash != lifecycle.last_claimed_result_hash):
                    raise DelegationError("最近领取结果与 State lifecycle 不匹配")
                self._child_snapshots[snapshot.child_session_id] = snapshot
            except Exception as error:
                reason = _safe_error_detail(error, 300)
                try:
                    state.mark_child_session_incompatible(child_id, reason)
                except Exception:
                    pass
                issue = {"child_session_id": child_id, "reason": reason}
                issues.append(issue)
        loaded = set(self._child_snapshots)
        for lifecycle in state.child_session_records:
            if lifecycle.status == "idle" and lifecycle.child_session_id not in loaded:
                reason = "没有可用的已持久化 idle 子会话快照"
                state.mark_child_session_incompatible(lifecycle.child_session_id, reason)
                issues.append({"child_session_id": lifecycle.child_session_id, "reason": reason})
        self.resume_issues = issues
        return issues

    def clear_child_sessions(self) -> None:
        with self._lock:
            self._child_snapshots.clear()
            self._background.clear()

    def confirm_background_startup(self, child_session_id: str) -> None:
        with self._lock:
            item = self._background.get(child_session_id)
        if item is None:
            raise DelegationError("未知 child_session_id")
        if item.state is not None:
            item.state.confirm_background_startup(item.task.delegation_id)
        with self._lock:
            item.startup_confirmed = True

    def commit_background_spawn_round(self) -> None:
        """Release accepted startup confirmations after the whole round commits."""
        with self._lock:
            for item in self._background.values():
                if item.status == "queued" and item.startup_confirmed:
                    item.round_committed = True

    def activate_background_tasks(self) -> None:
        """Start only confirmations whose full parent tool round has committed."""
        launches: list[tuple[str, _BackgroundDelegation, Thread]] = []
        with self._lock:
            waiting = [item for item in self._background.values()
                       if item.status == "queued" and item.startup_confirmed
                       and item.round_committed]
        for item in waiting:
            if not self.acquire_concurrency_slot(timeout=0):
                break
            try:
                if item.state is not None:
                    item.state.start_delegation(item.task.delegation_id)
                with self._lock:
                    current = self._background.get(item.task.subagent_id)
                    if current is not item or item.status != "queued" or item.cancel_event.is_set():
                        self.release_concurrency_slot()
                        continue
                    item.status = "running"

                    def worker(selected=item):
                        try:
                            result, snapshot = self._run_child(
                                selected.task, selected.cancel_event,
                                resume_snapshot=selected.resume_snapshot,
                                round_index=selected.round_index,
                                with_snapshot=True,
                            )
                        except BaseException as error:
                            result = replace(
                                self._runner_error_result(selected.task, error),
                                round_index=selected.round_index,
                            )
                            snapshot = None
                        finally:
                            # A worker publishes only the bounded completion value.
                            pass
                        self._background_events.put((selected.task.subagent_id, result, snapshot))
                        self.release_concurrency_slot()

                    thread = Thread(
                        target=worker,
                        name=f"mini-agent-subagent-{item.task.subagent_id[:8]}",
                        daemon=True,
                    )
                    item.thread = thread
                    launches.append((item.task.subagent_id, item, thread))
            except Exception as error:
                self.release_concurrency_slot()
                result = self._runner_error_result(item.task, error)
                self._background_events.put((item.task.subagent_id, replace(result, round_index=item.round_index), None))
        for child_session_id, item, thread in launches:
            try:
                thread.start()
            except BaseException as error:
                self.release_concurrency_slot()
                self._background_events.put((
                    child_session_id,
                    replace(self._runner_error_result(item.task, error), round_index=item.round_index),
                    None,
                ))

    def collect_background_events(self, *, dispatch: bool = True) -> list[dict[str, str]]:
        """Settle completed worker results on the parent Runtime thread."""
        notices: list[dict[str, str]] = []
        while True:
            try:
                child_session_id, result, snapshot = self._background_events.get_nowait()
            except Empty:
                break
            with self._lock:
                item = self._background.get(child_session_id)
                if item is None or item.result is not None:
                    continue
            if item.state is not None:
                try:
                    item.state.delegation_result_ready(item.task.delegation_id, result)
                except BaseException:
                    self._background_events.put((child_session_id, result, snapshot))
                    raise
            with self._lock:
                if item.result is not None:
                    continue
                item.result = result
                item.candidate_snapshot = snapshot
                item.status = "cancelled" if result.outcome == "cancelled" else "result_ready"
                self.last_task, self.last_result = item.task, result
            notices.append({
                "child_session_id": child_session_id,
                "status": item.status,
                "result_id": result.result_id,
                "round_index": str(item.round_index),
            })
        if dispatch:
            self.activate_background_tasks()
        with self._lock:
            live = bool(self._active) or any(
                item.status == "running" for item in self._background.values()
            )
            if not live:
                self._done_event.set()
        return notices

    def background_status(self, child_session_id: str,
                          state: AgentState | None = None) -> dict[str, Any]:
        with self._lock:
            item = self._background.get(child_session_id)
        if item is not None:
            return {
                "child_session_id": child_session_id,
                "delegation_id": item.task.delegation_id,
                "agent_profile": item.task.agent_profile,
                "status": item.status,
                "cancel_requested": item.cancel_requested,
                "result_id": item.result.result_id if item.result is not None else None,
                "round_index": item.round_index,
            }
        state = state or self.parent_state
        lifecycle = next((entry for entry in getattr(state, "child_session_records", [])
                          if entry.child_session_id == child_session_id), None)
        record = next((record for record in getattr(state, "delegation_records", [])
                       if record.mode == "background" and record.subagent_id == child_session_id
                       and (lifecycle is None or record.delegation_id == lifecycle.latest_delegation_id)), None)
        if record is None and lifecycle is None:
            record = next((record for record in getattr(state, "delegation_records", [])
                           if record.mode == "background" and record.subagent_id == child_session_id), None)
        if record is None:
            return {"child_session_id": child_session_id, "status": "not_found", "result_id": None}
        status = {
            "created": "queued", "running": "running", "result_ready": "result_ready",
            "committed": "claimed", "interrupted": "interrupted", "abandoned": "abandoned",
        }.get(record.delivery_status, "interrupted")
        if record.outcome == "cancelled" and status == "result_ready":
            status = "cancelled"
        return {
            "child_session_id": child_session_id,
            "delegation_id": record.delegation_id,
            "agent_profile": record.agent_profile,
            "status": status,
            "cancel_requested": bool(record.cancellation_reason),
            "result_id": record.result_id,
            "round_index": lifecycle.round_index if lifecycle is not None else 1,
        }

    def background_result(self, child_session_id: str,
                          state: AgentState | None = None) -> str:
        self.collect_background_events()
        with self._lock:
            item = self._background.get(child_session_id)
            result = item.result if item is not None else None
        if result is not None:
            return result.to_json()
        if item is not None:
            status = self.background_status(child_session_id, state)
            return json.dumps(status, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        snapshot = self._child_snapshots.get(child_session_id)
        if snapshot is not None:
            return snapshot.last_result_json
        status = self.background_status(child_session_id, state)
        return json.dumps(status, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def cancel_background(self, child_session_id: str,
                          reason: str = "requested") -> dict[str, Any]:
        safe_reason = _safe_error_detail(reason, 240)
        with self._lock:
            item = self._background.get(child_session_id)
            if item is None:
                return {"child_session_id": child_session_id, "status": "not_found",
                        "cancel_requested": False, "result_id": None}
            status = item.status
            if status == "queued":
                item.cancel_requested = True
                item.cancel_event.set()
            elif status == "running":
                item.cancel_requested = True
                item.cancel_event.set()
            result = item.result
        if status in {"queued", "running"} and item.state is not None:
            item.state.note_delegation_cancellation(item.task.delegation_id, safe_reason)
        if status == "queued" and result is None:
            self._finish_queued_cancellation(item, safe_reason)
        return self.background_status(child_session_id)

    def _finish_queued_cancellation(self, item: _BackgroundDelegation, reason: str) -> None:
        with self._lock:
            if item.status != "queued" or item.result is not None:
                return
            confirmed = item.startup_confirmed and item.round_committed
            result = (replace(self._cancelled_result(item.task, reason), round_index=item.round_index)
                      if confirmed else None)
        if not confirmed:
            if item.state is not None:
                item.state.cancel_unstarted_background_delegation(
                    item.task.delegation_id, reason,
                )
            with self._lock:
                if item.status == "queued":
                    item.status = "interrupted"
            return
        if item.state is not None:
            item.state.delegation_result_ready(item.task.delegation_id, result)
        with self._lock:
            if item.status != "queued" or item.result is not None:
                return
            item.result = result
            item.status = "cancelled"
            self.last_task, self.last_result = item.task, result

    def mark_background_claimed(self, child_session_id: str, result_id: str) -> None:
        with self._lock:
            item = self._background.get(child_session_id)
            if item is None:
                return
            if item.result is None or item.result.result_id != result_id:
                raise DelegationError("领取结果与内存结果 ID 不一致")
            if item.status == "claimed":
                return
            if item.result.outcome == "completed" and item.result.error_kind != "snapshot_unavailable":
                if item.candidate_snapshot is None:
                    raise DelegationError(
                        f"child_session_id={child_session_id} completed 结果缺少可续接快照"
                    )
                if (item.candidate_snapshot.last_claimed_result_id != result_id
                        or item.candidate_snapshot.round_index != item.round_index):
                    raise DelegationError("child session 候选快照与领取结果不匹配")
                self._child_snapshots[child_session_id] = item.candidate_snapshot
            item.status = "claimed"
            self._done_event.set()

    def cleanup_background(self, task_id: str | None, timeout: float = 2.0,
                           *, abandon: bool = True) -> dict[str, Any]:
        """Cancel, collect and mark unclaimed background results as abandoned."""
        self.cancel(task_id, "task_boundary", interrupt=False)
        if not self.wait(timeout):
            return {"complete": False, **self.active_info()}
        self.collect_background_events(dispatch=False)
        with self._lock:
            entries = [item for item in self._background.values()
                       if item.task.parent_task_id == task_id
                       and item.status in {"result_ready", "cancelled"}]
        if abandon:
            for item in entries:
                if item.state is not None:
                    item.state.abandon_background_delegation(
                        item.task.delegation_id, "父任务边界关闭时结果未领取",
                    )
                with self._lock:
                    item.status = "abandoned"
                    item.result = None
        with self._lock:
            complete = not any(item.task.parent_task_id == task_id
                               and item.status in ({"queued", "running"} if not abandon else
                                                   {"queued", "running", "result_ready", "cancelled"})
                               for item in self._background.values())
            if complete and not self._active:
                self._done_event.set()
        return {"complete": complete, **self.active_info()}

    def discard_background_results(self, delegation_ids: list[str]) -> None:
        """Drop result bodies only after the clean handoff has committed."""
        selected = set(delegation_ids)
        with self._lock:
            for item in self._background.values():
                if item.task.delegation_id in selected:
                    if item.state is None or next(
                        (record.delivery_status for record in item.state.delegation_records
                         if record.delegation_id == item.task.delegation_id), None
                    ) != "abandoned":
                        raise DelegationError("后台结果尚未提交放弃事实")
                    item.status = "abandoned"
                    item.result = None

    def _authorize_role_skills(self, task: DelegatedTask) -> DelegatedTask:
        if task.agent_profile is None:
            return task
        profile = self.agent_profile_catalog.resolve(task.agent_profile)
        if profile.fingerprint != task.agent_profile_fingerprint:
            raise DelegationError("agent_profile 在合同冻结后发生变化")
        granted: list[str] = []
        if profile.skills and (self.skill_catalog is None or self.parent_permission_gate is None):
            raise DelegationError("agent_profile 所需 Skill Catalog 或父权限闸门不可用")
        if self.skill_catalog is not None and self.parent_permission_gate is not None:
            for skill_id in profile.skills:
                try:
                    definition = self.skill_catalog.get(skill_id)
                except SkillAccessError as error:
                    raise DelegationError(
                        f"agent_profile 引用不存在或不可用的 Skill: {skill_id}"
                    ) from error
                denial = self.parent_permission_gate.guard(
                    "skill", {"name": skill_id},
                    display_context={"skill_id": skill_id, "source": definition.source},
                )
                if denial is None:
                    granted.append(skill_id)
        return replace(task, authorized_skills=tuple(granted))

    @staticmethod
    def _contract_identity(task: DelegatedTask) -> str:
        """Identify repeated work while ignoring per-call IDs and timestamps."""
        payload = {
            "goal": task.goal,
            "scope": list(task.scope),
            "constraints": list(task.constraints),
            "expected_findings": list(task.expected_findings),
            "requested_tools": list(task.requested_tools),
            "allowed_tools": list(task.allowed_tools),
            "selected_parent_facts": list(task.selected_parent_facts),
            "purpose": task.purpose,
            "source_id": task.source_id,
            "budget": task.budget.__dict__,
            "model_profile": task.model_profile,
        }
        if task.agent_profile is not None:
            payload["agent_profile"] = task.agent_profile
            payload["agent_profile_fingerprint"] = task.agent_profile_fingerprint
        return hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()

    @property
    def active_task_id(self) -> str | None:
        with self._lock:
            return next(iter(self._active.values()))[0].parent_task_id if self._active else None

    @property
    def active_delegation_id(self) -> str | None:
        with self._lock:
            return next(iter(self._active)) if self._active else None

    def active_info(self) -> dict[str, Any]:
        with self._lock:
            entries = list(self._active.items())
            first = entries[0][1][0] if entries else None
            backgrounds = [item for item in self._background.values()
                           if item.status not in {"claimed", "abandoned", "interrupted"}]
            return {
                "active": bool(entries or backgrounds),
                "active_count": len(entries) + len(backgrounds),
                "task_ids": ([item[1][0].parent_task_id for item in entries]
                              + [item.task.parent_task_id for item in backgrounds]),
                "delegation_ids": ([item[0] for item in entries]
                                    + [item.task.delegation_id for item in backgrounds]),
                "subagent_ids": ([item[1][0].subagent_id for item in entries]
                                 + [item.task.subagent_id for item in backgrounds]),
                "child_session_ids": [item.task.subagent_id for item in backgrounds],
                "background_statuses": [
                    {"child_session_id": item.task.subagent_id, "status": item.status,
                     "result_id": item.result.result_id if item.result else None}
                    for item in backgrounds[:8]
                ],
                "task_id": first.parent_task_id if first else None,
                "delegation_id": entries[0][0] if entries else None,
                "cancel_requested": bool(self._cancel_reason),
                "cancel_reason": self._cancel_reason or None,
            }

    def consume_interrupt(self) -> bool:
        with self._lock:
            interrupted = self._interrupted
            self._interrupted = False
            return interrupted

    @property
    def cancel_requested(self) -> bool:
        with self._lock:
            return bool(self._cancel_reason)

    def cancel(self, task_id: str | None = None, reason: str = "task_boundary",
               *, interrupt: bool = True) -> bool:
        """Broadcast cooperative cancellation to all children of a parent task."""
        with self._lock:
            if not self._active and not any(
                    item.status in {"queued", "running"} for item in self._background.values()):
                return False
            owned = ({item[0].parent_task_id for item in self._active.values()}
                     | {item.task.parent_task_id for item in self._background.values()
                        if item.status in {"queued", "running"}})
            if task_id is not None and task_id not in owned:
                return False
            self._cancel_reason = _safe_error_detail(reason, 240)
            if interrupt:
                self._interrupted = True
            entries = list(self._active.values())
            backgrounds = [item for item in self._background.values()
                           if item.status in {"queued", "running"}]
        for task, cancel_event, state in entries:
            cancel_event.set()
            if state is not None:
                try:
                    state.note_delegation_cancellation(task.delegation_id, self._cancel_reason)
                except ValueError:
                    pass
        for item in backgrounds:
            item.cancel_requested = True
            item.cancel_event.set()
            if item.state is not None:
                try:
                    item.state.note_delegation_cancellation(
                        item.task.delegation_id, self._cancel_reason,
                    )
                except ValueError:
                    pass
            if item.status == "queued":
                self._finish_queued_cancellation(item, self._cancel_reason)
        return True

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for all children of the current parent task to settle."""
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while True:
            with self._lock:
                sync_active = bool(self._active)
                threads = [item.thread for item in self._background.values()
                           if item.status == "running" and item.thread is not None]
            if not sync_active and not any(thread.is_alive() for thread in threads):
                return True
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            self._done_event.wait(min(0.02, remaining) if remaining is not None else 0.02)

    @staticmethod
    def _rejected_result(task: DelegatedTask, outcome: str, detail: str) -> SubagentResult:
        now = _utc_now()
        return SubagentResult(
            str(uuid4()), task.delegation_id, task.subagent_id, task.parent_task_id,
            outcome, detail, (), (), (detail,), UsageRecord(token_accounting="estimated"),
            task.contract_hash, now, now,
            "aggregate_budget" if outcome == "budget_exhausted" else "lifecycle_rejected",
            agent_profile=task.agent_profile,
            agent_profile_fingerprint=task.agent_profile_fingerprint,
        )

    @staticmethod
    def _cancelled_result(task: DelegatedTask, detail: str) -> SubagentResult:
        now = _utc_now()
        return SubagentResult(
            str(uuid4()), task.delegation_id, task.subagent_id, task.parent_task_id,
            "cancelled", "子代理收到取消请求", (), (), (detail,),
            UsageRecord(token_accounting="estimated"), task.contract_hash,
            now, now, "cancelled", detail, task.model_profile,
            task.model_binding_ref.fingerprint if task.model_binding_ref else None,
            task.agent_profile, task.agent_profile_fingerprint,
        )

    @staticmethod
    def _runner_error_result(task: DelegatedTask, error: BaseException) -> SubagentResult:
        now = _utc_now()
        kind = "cancelled" if isinstance(error, KeyboardInterrupt) else "runner_error"
        outcome = "cancelled" if kind == "cancelled" else "failed"
        return SubagentResult(
            str(uuid4()), task.delegation_id, task.subagent_id, task.parent_task_id,
            outcome, "子代理运行失败", (), (), (_safe_error_detail(error),),
            UsageRecord(token_accounting="estimated"), task.contract_hash,
            now, now, kind, _safe_error_detail(error), task.model_profile,
            task.model_binding_ref.fingerprint if task.model_binding_ref else None,
            task.agent_profile, task.agent_profile_fingerprint,
        )

    def _register(self, tasks: list[tuple[DelegatedTask, Event, AgentState | None]]) -> None:
        with self._lock:
            if not self._active:
                self._cancel_reason = ""
                self._interrupted = False
            for task, event, state in tasks:
                self._active[task.delegation_id] = (task, event, state)
            if tasks:
                self._done_event.clear()

    def _child_finished(self, task: DelegatedTask, result: SubagentResult) -> None:
        with self._lock:
            self._active.pop(task.delegation_id, None)
            self.last_task = task
            self.last_result = result
            if result.outcome == "cancelled" and result.error_detail == "user_interrupt":
                self._interrupted = True
            if not self._active:
                self._done_event.set()

    def _run_child(
        self, task: DelegatedTask, cancel_event: Event, *,
        resume_snapshot: ChildSessionSnapshot | None = None,
        round_index: int | None = None,
        with_snapshot: bool = False,
    ) -> SubagentResult | tuple[SubagentResult, ChildSessionSnapshot | None]:
        try:
            binding = (
                self.provider_catalog.bind(task.model_profile)
                if self.provider_catalog is not None and task.model_profile is not None
                else None
            )
            profile = (
                self.agent_profile_catalog.resolve(task.agent_profile)
                if task.agent_profile is not None else None
            )
            if profile is not None and profile.fingerprint != task.agent_profile_fingerprint:
                raise DelegationError("当前 Runtime 的 agent_profile 指纹与合同不一致")
            runner = SubagentRunner(
                self.workspace_root, llm=self.subagent_llm,
                parent_registry=self.parent_registry, model_binding=binding,
                agent_profile=profile, skill_catalog=self.skill_catalog,
                session_root=self.session_root,
                resume_snapshot=resume_snapshot, round_index=round_index,
            )
            result = runner.run(
                task, cancel_event=cancel_event,
                cancellation_reason=lambda: self._cancel_reason,
            )
            return (result, runner.last_snapshot) if with_snapshot else result
        except BaseException as error:
            result = self._runner_error_result(task, error)
            if round_index is not None:
                result = replace(result, round_index=round_index)
            return (result, None) if with_snapshot else result

    def prepare_batch(self, admissions: dict[int, Any], state: AgentState | None) -> tuple[
            dict[int, DelegatedTask], dict[int, SubagentResult], dict[int, DelegatedTask]]:
        """Freeze contracts and reserve all accepted calls in model order."""
        tasks: list[tuple[int, DelegatedTask]] = []
        ready: dict[int, SubagentResult] = {}
        rejected_tasks: dict[int, DelegatedTask] = {}
        seen_identities: set[str] = set()
        for index, admission in sorted(admissions.items()):
            try:
                task = self._authorize_role_skills(self.create_task(admission.arguments, state))
                identity = self._contract_identity(task)
                if identity in seen_identities:
                    ready[index] = self._rejected_result(
                        task, "budget_exhausted", "重复的委派合同",
                    )
                    rejected_tasks[index] = task
                    continue
                seen_identities.add(identity)
                tasks.append((index, task))
            except Exception as error:
                # ToolExecutor already performed schema and permission checks;
                # this is the remaining contract/binding boundary.
                ready[index] = SubagentResult(
                    str(uuid4()), "", "", getattr(state, "task_id", "") if state else "",
                    "failed", "委派合同非法", (), (), (_safe_error_detail(error),),
                    UsageRecord(token_accounting="estimated"), "", _utc_now(), _utc_now(),
                    "invalid_contract", _safe_error_detail(error),
                )
        if not tasks:
            return {}, ready, rejected_tasks
        task_values = [task for _, task in tasks]
        accepted: dict[int, DelegatedTask] = {}
        if state is None:
            accepted = dict(tasks)
        else:
            outcomes = state.reserve_delegation_batch(task_values)
            for (index, task), outcome in zip(tasks, outcomes):
                if isinstance(outcome, Exception):
                    result = self._rejected_result(
                        task, "budget_exhausted", _safe_error_detail(outcome),
                    )
                    ready[index] = result
                    rejected_tasks[index] = task
                else:
                    state.start_delegation(task.delegation_id)
                    accepted[index] = task
        return accepted, ready, rejected_tasks

    def run_prepared_batch(
        self, tasks: dict[int, DelegatedTask], state: AgentState | None = None,
        *, ready: dict[int, Any] | None = None,
        rejected_tasks: dict[int, DelegatedTask] | None = None,
        on_result_ready: Callable[[int, Any], None] | None = None,
        on_result: Callable[[int, Any], None] | None = None,
    ) -> dict[int, SubagentResult]:
        """Run already reserved tasks; only the parent thread settles State."""
        if not tasks and not ready:
            return {}
        entries = [
            _PreparedDelegation(index, task, Event())
            for index, task in sorted(tasks.items())
        ]
        self._register([(item.task, item.cancel_event, state) for item in entries])

        def mark_ready(index: int, result: Any) -> None:
            if on_result_ready is not None and state is not None and index in tasks:
                state.delegation_result_ready(tasks[index].delegation_id, result)
            if on_result_ready is not None:
                on_result_ready(index, result)

        scheduler = DelegationScheduler(
            self, entries,
            getattr(getattr(state, "delegation_budget", None), "max_concurrency", 1),
            ready=ready,
            on_result_ready=mark_ready,
            on_result=(lambda index, result: self._deliver_prepared_result(
                index, result, tasks, rejected_tasks or {}, state, on_result_ready, on_result,
            ))
        )
        return scheduler.run()

    def _deliver_prepared_result(
        self, index: int, result: Any, tasks: dict[int, DelegatedTask],
        rejected_tasks: dict[int, DelegatedTask],
        state: AgentState | None,
        on_result_ready: Callable[[int, Any], None] | None,
        on_result: Callable[[int, Any], None] | None,
    ) -> None:
        if state is not None and index in tasks:
            record = next(
                (item for item in state.delegation_records
                 if item.delegation_id == tasks[index].delegation_id), None,
            )
            if record is None or record.delivery_status != "result_ready":
                state.delegation_result_ready(tasks[index].delegation_id, result)
                if on_result_ready is not None:
                    on_result_ready(index, result)
        elif state is not None and index in rejected_tasks:
            try:
                state.record_delegation_rejection(
                    rejected_tasks[index],
                    result, result.error_detail,
                )
            except Exception:
                if on_result_ready is not None:
                    raise
            if on_result_ready is not None:
                # Budget and duplicate-contract rejections also produce a
                # structured SubagentResult.  Persist that result before
                # the ordered parent delivery just like a worker result.
                on_result_ready(index, result)
        if on_result is not None:
            on_result(index, result)

    def run(self, arguments: dict[str, Any] | DelegatedTask,
            state: AgentState | None = None) -> SubagentResult:
        """Compatibility single-task entry point retained for v0.34–v0.37."""
        state = state or self.parent_state
        try:
            task = arguments if isinstance(arguments, DelegatedTask) else self.create_task(arguments, state)
            task = self._authorize_role_skills(task)
        except Exception as error:
            now = _utc_now()
            return SubagentResult(
                str(uuid4()), "", "", getattr(state, "task_id", "") if state else "",
                "failed", "委派合同非法", (), (), (_safe_error_detail(error),),
                UsageRecord(token_accounting="estimated"), "", now, now, "invalid_contract",
            )
        with self._lock:
            if self._active:
                now = _utc_now()
                result = SubagentResult(
                    str(uuid4()), task.delegation_id, task.subagent_id, task.parent_task_id,
                    "failed", "当前已有子代理运行", (), (), ("delegation_busy",),
                    UsageRecord(token_accounting="estimated"), task.contract_hash,
                    now, now, "delegation_busy",
                )
                self.last_result = result
                return result
        if state is not None:
            try:
                record = state.reserve_delegation(task)
                state.start_delegation(task.delegation_id)
            except Exception as error:
                result = self._rejected_result(task, "budget_exhausted", _safe_error_detail(error))
                try:
                    state.record_delegation_rejection(task, result, result.error_detail)
                except Exception:
                    pass
                self.last_result = result
                return result
        event = Event()
        self._register([(task, event, state)])
        acquired = False
        try:
            while not event.is_set():
                if self.acquire_concurrency_slot(timeout=0.05):
                    acquired = True
                    break
            result = (self._run_child(task, event) if acquired else
                      self._cancelled_result(task, "cancelled_before_slot"))
        finally:
            if acquired:
                self.release_concurrency_slot()
        self._child_finished(task, result)
        if state is not None:
            state.delegation_result_ready(task.delegation_id, result)
        return result

    delegate = run
