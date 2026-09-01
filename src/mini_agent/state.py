"""Thread-safe task state and auditable execution facts."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
import hashlib
import json
import re
from threading import Lock
from typing import Any, Literal

from mini_agent.config import (MAX_ATTEMPT_FINGERPRINTS, MAX_FAILURE_RETRIES,
                               MAX_RECOVERY_ACTIONS, MAX_REPAIR_CYCLES)

EffectClass = Literal["none", "possible"]
AttemptOutcome = Literal["succeeded", "failed", "denied", "timeout", "invalid"]
FailureCategory = Literal["protocol", "permission", "transient", "deterministic", "validation", "unknown"]


def canonical_arguments_hash(arguments: dict[str, Any]) -> str:
    data = json.dumps(arguments, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def redacted_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    sensitive = ("key", "token", "secret", "password", "credential", "authorization")
    summary: dict[str, Any] = {}
    for key in sorted(arguments):
        value = arguments[key]
        if any(word in key.lower() for word in sensitive):
            summary[key] = "<redacted>"
        elif key in ("content", "old_string", "new_string", "command"):
            summary[key] = f"<{type(value).__name__}:{len(value) if isinstance(value, str) else '?'}>"
        elif isinstance(value, (str, int, float, bool)) or value is None:
            summary[key] = value[:80] if isinstance(value, str) else value
        else:
            summary[key] = f"<{type(value).__name__}>"
    return summary


@dataclass(frozen=True)
class TodoItem:
    content: str
    status: Literal["pending", "in_progress", "completed"] = "pending"


@dataclass(frozen=True)
class VerificationEvidence:
    command: str
    outcome: str
    exit_code: int | None
    output: str
    generation_id: int = 0
    caused_by_attempt_id: str | None = None

    @property
    def result(self) -> str:
        return self.output


@dataclass(frozen=True)
class ExecutionGeneration:
    generation_id: int
    opened_by_attempt_id: str | None = None
    opened_by_failure_id: str | None = None
    opened_by_recovery_id: str | None = None
    open_reason: Literal["task_start", "possible_effect", "recovery"] = "task_start"


@dataclass(frozen=True)
class AttemptReservation:
    attempt_id: str
    pre_generation_id: int
    generation_id: int


@dataclass(frozen=True)
class ExecutionAttempt:
    attempt_id: str
    pre_generation_id: int
    generation_id: int
    tool: str
    arguments_hash: str
    redacted_arguments: dict[str, Any]
    outcome: AttemptOutcome
    duration_ms: int
    effect_class: EffectClass
    handler_admitted: bool
    permission: str
    caused_by_failure_id: str | None = None
    caused_by_attempt_id: str | None = None
    exit_code: int | None = None
    error_kind: str | None = None
    output_excerpt: str = ""
    failure_id: str | None = None


@dataclass(frozen=True)
class FailureEvent:
    failure_id: str
    generation_id: int
    phase: Literal["execute", "verify", "recover"]
    category: FailureCategory
    retryable: bool
    caused_by_attempt_id: str
    affected_files: tuple[str, ...] = ()
    cause_hint: str | None = None


@dataclass(frozen=True)
class RecoveryAction:
    """Reserved v0.17 schema; recovery execution starts in v0.18."""
    recovery_id: str
    generation_id: int
    action: Literal["retry", "adjust", "ask", "block"]
    reason: str
    caused_by_failure_id: str
    status: Literal["proposed", "reserved", "executed", "rejected", "terminal"]
    requested_attempt: str | None = None
    requested_tool: str | None = None
    requested_arguments_hash: str | None = None
    redacted_arguments: dict[str, Any] | None = None
    result_generation_id: int | None = None
    result_attempt: str | None = None


@dataclass
class AgentState:
    task: str = ""
    current_goal: str = ""
    tool_history: list[dict] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    status: str = "running"
    terminal_reason: str = ""
    todos: list[TodoItem] = field(default_factory=list)
    verification_evidence: list[VerificationEvidence] = field(default_factory=list)
    generations: list[ExecutionGeneration] = field(default_factory=list)
    attempts: list[ExecutionAttempt] = field(default_factory=list)
    failures: list[FailureEvent] = field(default_factory=list)
    recovery_actions: list[RecoveryAction] = field(default_factory=list)
    recovery_notice: str = ""
    _verification_generation: int = field(default=0, init=False, repr=False)
    _last_verified_generation: int = field(default=-1, init=False, repr=False)
    _verification_required: bool = field(default=False, init=False, repr=False)
    _next_attempt: int = field(default=1, init=False, repr=False)
    _next_failure: int = field(default=1, init=False, repr=False)
    _fingerprint_counts: dict[tuple[str, str], int] = field(default_factory=dict, init=False, repr=False)
    _repair_cycles: int = field(default=0, init=False, repr=False)
    _lock: Any = field(default_factory=Lock, init=False, repr=False, compare=False)

    @property
    def current_generation_id(self) -> int:
        with self._lock:
            return self._verification_generation

    @property
    def execution_generations(self) -> list[ExecutionGeneration]:
        with self._lock:
            return list(self.generations)

    @property
    def execution_attempts(self) -> list[ExecutionAttempt]:
        with self._lock:
            return list(self.attempts)

    @property
    def failure_events(self) -> list[FailureEvent]:
        with self._lock:
            return list(self.failures)

    def _ensure_generation(self) -> None:
        if not self.generations:
            self.generations.append(ExecutionGeneration(self._verification_generation))

    def reserve_attempt(self, effect_class: EffectClass) -> AttemptReservation:
        """Reserve a stable attempt id and possible-effect generation atomically."""
        with self._lock:
            self._ensure_generation()
            attempt_id = f"a-{self._next_attempt}"
            self._next_attempt += 1
            before = self._verification_generation
            if effect_class == "possible":
                self._verification_generation += 1
                self.generations.append(ExecutionGeneration(
                    self._verification_generation, opened_by_attempt_id=attempt_id,
                    open_reason="possible_effect"))
                self.verification_evidence.clear()
                self._last_verified_generation = -1
                self._verification_required = True
            return AttemptReservation(attempt_id, before, self._verification_generation)

    def record_execution_result(self, result: Any) -> ExecutionAttempt:
        """Commit an ExecutionResult and derive attempt/failure/verification facts."""
        with self._lock:
            self._ensure_generation()
            reservation = result.reservation
            if reservation is None:
                attempt_id = f"a-{self._next_attempt}"
                self._next_attempt += 1
                pre_generation = generation_id = self._verification_generation
            else:
                attempt_id = reservation.attempt_id
                pre_generation = reservation.pre_generation_id
                generation_id = reservation.generation_id
            args = deepcopy(result.arguments)
            arguments_hash = canonical_arguments_hash(args)
            fingerprint = (result.tool, arguments_hash)
            self._fingerprint_counts[fingerprint] = self._fingerprint_counts.get(fingerprint, 0) + 1
            is_verify = result.tool == "run_shell" and args.get("purpose", "execution") == "verification"
            failure_id = None
            category: FailureCategory | None = None
            retryable = False
            phase: Literal["execute", "verify", "recover"] = "execute"
            if result.outcome != "succeeded":
                failure_id = f"f-{self._next_failure}"; self._next_failure += 1
                if result.outcome == "denied": category = "permission"
                elif result.outcome == "timeout": category, retryable = "transient", True
                elif result.outcome == "invalid": category = "protocol"
                elif is_verify:
                    category, retryable, phase = "validation", True, "verify"
                    self._repair_cycles += 1
                elif result.effect_class == "possible" and result.error_kind == "handler_exception":
                    category = "unknown"
                else: category = "deterministic"
            attempt = ExecutionAttempt(
                attempt_id, pre_generation, generation_id, result.tool, arguments_hash,
                redacted_arguments(args), result.outcome, result.duration_ms,
                result.effect_class, result.handler_admitted, result.permission,
                exit_code=result.exit_code, error_kind=result.error_kind,
                output_excerpt=result.output_excerpt, failure_id=failure_id)
            self.attempts.append(attempt)
            if result.tool != "update_todo":
                self.tool_history.append({"tool": result.tool, "arguments_hash": arguments_hash,
                                          "ok": result.outcome == "succeeded", "brief": result.output_excerpt})
            path = args.get("path")
            if result.outcome == "succeeded" and result.tool in ("write_file", "edit_file"):
                if isinstance(path, str) and path not in self.files_changed:
                    self.files_changed.append(path)
            if is_verify and result.handler_admitted:
                passed = result.outcome == "succeeded" and result.exit_code == 0
                self.verification_evidence.append(VerificationEvidence(
                    str(args.get("command", "")), "passed" if passed else "failed",
                    result.exit_code, result.output_excerpt, generation_id, attempt_id))
                self._last_verified_generation = generation_id if passed else -1
                self._verification_required = not passed
            if failure_id and category:
                affected = (path,) if isinstance(path, str) and result.effect_class == "possible" else ()
                self.failures.append(FailureEvent(failure_id, generation_id, phase, category,
                                                  retryable, attempt_id, affected))
                self.errors.append(f"{result.tool}: {result.output_excerpt}")
                self.recovery_notice = (f"Failure {failure_id} ({category}) requires diagnosis; "
                                        f"caused by {attempt_id} in generation {generation_id}.")
                exhausted = self._fingerprint_counts[fingerprint] >= MAX_ATTEMPT_FINGERPRINTS
                if category == "permission": self._terminal("failed", "权限被明确拒绝", failure_id)
                elif category == "protocol": self._terminal("failed", "工具协议或参数不变量被破坏", failure_id)
                elif category == "unknown": self._terminal("blocked", "副作用范围未知，需要外部诊断", failure_id)
                elif category == "deterministic": self._terminal("failed", "确定性执行错误不可恢复", failure_id)
                elif category == "validation" and self._repair_cycles >= MAX_REPAIR_CYCLES:
                    self._terminal("failed", "Repair cycle 预算已耗尽", failure_id)
                elif retryable and exhausted:
                    self._terminal("blocked", "同一参数指纹尝试预算已耗尽", failure_id)
            return attempt

    def _terminal(self, status: str, reason: str, failure_id: str) -> None:
        self.status = status
        self.terminal_reason = f"{reason}; last_failure={failure_id}"

    def record_tool(self, name: str, args: dict[str, Any], ok: bool, brief: str) -> None:
        """Compatibility API for older callback-based integrations."""
        if name == "update_todo": return
        args_copy = deepcopy(args)
        with self._lock:
            self.tool_history.append({"tool": name, "args": args_copy, "ok": ok, "brief": brief})
            if ok and name in ("write_file", "edit_file"):
                path = args_copy.get("path")
                if isinstance(path, str) and path not in self.files_changed: self.files_changed.append(path)
                self._invalidate_verification()
            if name == "run_shell" and args_copy.get("purpose", "execution") == "execution" and "权限拒绝" not in brief:
                self._invalidate_verification()
            if name == "run_shell" and args_copy.get("purpose", "execution") == "verification":
                timeout = "[timeout]" in str(brief) or "超时" in str(brief)
                match = re.search(r"\[exit=(-?\d+)\]", str(brief))
                code = int(match.group(1)) if match else (0 if ok and not timeout else None)
                passed = bool(ok and not timeout and code == 0)
                self.verification_evidence.append(VerificationEvidence(
                    str(args_copy.get("command", "")), "passed" if passed else "failed",
                    code, str(brief), self._verification_generation))
                self._last_verified_generation = self._verification_generation if passed else -1
                self._verification_required = not passed
            if not ok: self.errors.append(f"{name}: {brief}")

    def begin_task(self, task: str) -> None:
        with self._lock:
            self.task = task; self.current_goal = ""; self.status = "running"; self.terminal_reason = ""
            self.tool_history.clear(); self.files_changed.clear(); self.errors.clear(); self.todos.clear()
            self.verification_evidence.clear(); self.generations.clear(); self.attempts.clear()
            self.failures.clear(); self.recovery_actions.clear(); self.recovery_notice = ""
            self._verification_generation = 0; self._last_verified_generation = -1
            self._verification_required = False; self._next_attempt = 1; self._next_failure = 1
            self._fingerprint_counts.clear(); self._repair_cycles = 0
            self.generations.append(ExecutionGeneration(0, open_reason="task_start"))

    def _invalidate_verification(self) -> None:
        self._verification_generation += 1; self.verification_evidence.clear()
        self._last_verified_generation = -1; self._verification_required = True

    def unfinished_todos(self) -> list[dict[str, str]]:
        with self._lock: return [{"content": t.content, "status": t.status} for t in self.todos if t.status != "completed"]

    def has_verification_evidence(self) -> bool:
        with self._lock:
            return bool(self.verification_evidence) and self._last_verified_generation == self._verification_generation and self.verification_evidence[-1].outcome == "passed"

    def completion_reminder(self) -> dict[str, object] | None:
        with self._lock:
            if self.status in ("blocked", "failed"): return None
            missing = [t.content for t in self.todos if t.status != "completed"]
            if not missing and not self._verification_required: return None
            return {"unfinished_todos": missing, "verification_required": self._verification_required,
                    "message": "任务尚未满足完成条件，请继续执行并验证。"}

    def update_todos(self, todos: list[dict[str, Any]]) -> None:
        if not isinstance(todos, list): raise ValueError("todos 必须是数组")
        if len(todos) > 50: raise ValueError("Todo 数量不能超过 50")
        parsed, in_progress = [], 0
        for item in todos:
            if not isinstance(item, dict): raise ValueError("Todo 项必须是对象")
            content = item.get("content")
            if not isinstance(content, str) or not content.strip(): raise ValueError("Todo content 必须是非空字符串")
            content = content.strip()
            if len(content) > 240: raise ValueError("Todo content 不能超过 240 个字符")
            status = item.get("status", "pending")
            if status not in ("pending", "in_progress", "completed"): raise ValueError("Todo status 非法")
            in_progress += status == "in_progress"; parsed.append(TodoItem(content, status))
        if in_progress > 1: raise ValueError("最多只能有一个 in_progress Todo")
        with self._lock:
            self.todos = parsed
            self.current_goal = next((t.content for t in parsed if t.status == "in_progress"), "")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "task": self.task, "current_goal": self.current_goal,
                "tool_history": deepcopy(self.tool_history), "files_changed": deepcopy(self.files_changed),
                "errors": deepcopy(self.errors), "status": self.status, "terminal_reason": self.terminal_reason,
                "todos": [asdict(x) for x in self.todos],
                "verification_evidence": [asdict(x) for x in self.verification_evidence],
                "verification_required": self._verification_required,
                "current_generation_id": self._verification_generation,
                "generations": [asdict(x) for x in self.generations],
                "attempts": [asdict(x) for x in self.attempts],
                "failures": [asdict(x) for x in self.failures],
                "recovery_actions": [asdict(x) for x in self.recovery_actions],
                "latest_failure": asdict(self.failures[-1]) if self.failures else None,
                "recovery_notice": self.recovery_notice,
                "budgets": {"failure_retries_remaining": MAX_FAILURE_RETRIES,
                            "fingerprint_attempts_limit": MAX_ATTEMPT_FINGERPRINTS,
                            "recovery_actions_remaining": max(0, MAX_RECOVERY_ACTIONS - len(self.recovery_actions)),
                            "repair_cycles_remaining": max(0, MAX_REPAIR_CYCLES - self._repair_cycles)},
            }
