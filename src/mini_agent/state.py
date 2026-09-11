"""Thread-safe task state and auditable execution facts."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
import hashlib
import json
import re
from threading import Lock
from typing import Any, Literal

from mini_agent.checkpoint import CheckpointStore
from mini_agent.config import (MAX_ATTEMPT_FINGERPRINTS, MAX_FAILURE_RETRIES,
                               MAX_RECOVERY_ACTIONS, MAX_REPAIR_CYCLES)

EffectClass = Literal["none", "possible"]
AttemptOutcome = Literal["succeeded", "failed", "denied", "timeout", "invalid"]
FailureCategory = Literal["protocol", "permission", "transient", "deterministic", "validation", "unknown"]
RepairPhase = Literal["idle", "diagnosis_required", "verification_required"]


class AttemptBudgetExceeded(ValueError):
    """A tool call reached the per-argument execution budget before its handler."""


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
    caused_by_failure_id: str | None = None
    caused_by_attempt_id: str | None = None
    recovery_id: str | None = None
    fingerprint_reserved: bool = False


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
    recovery_id: str | None = None
    checkpoint_id: str | None = None


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
    """Auditable recovery request; rollback references one task checkpoint."""
    recovery_id: str
    generation_id: int
    action: Literal["retry", "adjust", "ask", "block", "rollback"]
    reason: str
    caused_by_failure_id: str
    status: Literal["proposed", "reserved", "executed", "rejected", "terminal"]
    requested_attempt: str | None = None
    requested_tool: str | None = None
    requested_arguments_hash: str | None = None
    redacted_arguments: dict[str, Any] | None = None
    result_generation_id: int | None = None
    result_attempt: str | None = None
    checkpoint_id: str | None = None


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
    _reserved_repair_cycles: int = field(default=0, init=False, repr=False)
    _repair_phase: RepairPhase = field(default="idle", init=False, repr=False)
    _active_failure_id: str | None = field(default=None, init=False, repr=False)
    _active_recovery_id: str | None = field(default=None, init=False, repr=False)
    _failure_retry_counts: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _original_attempt_arguments: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _next_recovery: int = field(default=1, init=False, repr=False)
    _checkpoint_store: CheckpointStore | None = field(default=None, init=False, repr=False, compare=False)
    _lock: Any = field(default_factory=Lock, init=False, repr=False, compare=False)

    @property
    def current_generation_id(self) -> int:
        with self._lock:
            return self._verification_generation

    @property
    def repair_phase(self) -> RepairPhase:
        with self._lock:
            return self._repair_phase

    @property
    def active_failure_id(self) -> str | None:
        with self._lock:
            return self._active_failure_id

    @property
    def active_recovery_id(self) -> str | None:
        with self._lock:
            return self._active_recovery_id

    def repair_gate(self, name: str, arguments: dict[str, Any],
                    effect_class: EffectClass = "none",
                    reservation: AttemptReservation | None = None) -> str | None:
        """Return a phase-gate error before permission or handler admission.

        A recovery target carries a reservation and is the one controlled
        exception: it runs inside the recovery action even though activation
        has already moved the task into the successor verification phase.
        """
        with self._lock:
            if reservation is not None and reservation.recovery_id:
                return None
            if self._repair_phase == "diagnosis_required":
                if name == "recover" or name == "update_todo":
                    return None
                if name == "run_shell" and arguments.get("purpose", "execution") == "verification":
                    return "工具调用拒绝: diagnosis_required 阶段必须先处理当前 failure，不能直接 verification"
                if effect_class == "possible":
                    return "工具调用拒绝: diagnosis_required 阶段只允许只读调查、update_todo 或独占 recover"
            elif self._repair_phase == "verification_required":
                if name == "run_shell" and arguments.get("purpose", "execution") == "verification":
                    return None
                return "工具调用拒绝: verification_required 阶段下一工具回合只能是单个独立 verification"
            return None

    def _enter_diagnosis(self, failure_id: str) -> None:
        self._repair_phase = "diagnosis_required"
        self._active_failure_id = failure_id
        self._active_recovery_id = None
        self._verification_required = False

    def _enter_verification(self, recovery_id: str | None = None) -> None:
        self._repair_phase = "verification_required"
        self._active_recovery_id = recovery_id
        self._verification_required = True

    def _clear_repair(self) -> None:
        self._repair_phase = "idle"
        self._active_failure_id = None
        self._active_recovery_id = None
        self._verification_required = False
        self.recovery_notice = ""

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

    @property
    def checkpoint_store(self) -> CheckpointStore | None:
        return self._checkpoint_store

    @property
    def checkpoints(self) -> list[Any]:
        """Metadata-only checkpoint records retained by this task."""
        return self._checkpoint_store.list_checkpoints() if self._checkpoint_store is not None else []

    @property
    def rollback_checkpoints(self) -> list[Any]:
        """Checkpoint records that are currently eligible for rollback."""
        return self._checkpoint_store.available() if self._checkpoint_store is not None else []

    def bind_checkpoint_store(self, store: CheckpointStore) -> None:
        """Bind the task-local store shared by the registry, executor, and recovery runtime."""
        with self._lock:
            self._checkpoint_store = store

    def _checkpoint_for_failure(self, checkpoint_id: Any,
                               failure: FailureEvent) -> tuple[Any, str | None]:
        store = self._checkpoint_store
        if store is None:
            return None, "checkpoint store 不可用"
        checkpoint, detail = store.validate_rollback(checkpoint_id)
        if detail:
            return None, detail
        attempt_order = {
            attempt.attempt_id: index for index, attempt in enumerate(self.attempts)
        }
        checkpoint_order = attempt_order.get(checkpoint.attempt_id)
        failure_order = attempt_order.get(failure.caused_by_attempt_id)
        if checkpoint_order is None or failure_order is None:
            return None, "checkpoint 与目标 failure 的因果顺序不可确定"
        if checkpoint_order > failure_order:
            return None, "checkpoint 创建晚于目标 failure"
        return checkpoint, None

    def _ensure_generation(self) -> None:
        if not self.generations:
            self.generations.append(ExecutionGeneration(self._verification_generation))

    def is_terminal(self) -> bool:
        with self._lock:
            return self.status in ("blocked", "failed")

    def reserve_attempt(self, effect_class: EffectClass, tool: str | None = None,
                        arguments: dict[str, Any] | None = None) -> AttemptReservation:
        """Reserve an attempt, its fingerprint quota, and possible-effect generation atomically."""
        with self._lock:
            self._ensure_generation()
            fingerprint_reserved = False
            if tool is not None and arguments is not None:
                fingerprint = (tool, canonical_arguments_hash(arguments))
                count = self._fingerprint_counts.get(fingerprint, 0)
                if count >= MAX_ATTEMPT_FINGERPRINTS:
                    raise AttemptBudgetExceeded("同一参数指纹尝试预算已耗尽")
                self._fingerprint_counts[fingerprint] = count + 1
                fingerprint_reserved = True
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
                # Ordinary mutations invalidate completion evidence, but only
                # an accepted recovery action enters the strict repair
                # verification phase.  Normal multi-step edits may continue
                # before their final verification.
                self._verification_required = True
            return AttemptReservation(
                attempt_id, before, self._verification_generation,
                fingerprint_reserved=fingerprint_reserved,
            )

    def recovery_target(self, action: str, caused_by_failure_id: str,
                        requested_attempt: str | None = None,
                        requested_tool: str | None = None,
                        requested_arguments: dict[str, Any] | None = None,
                        checkpoint_id: str | None = None) -> tuple[tuple[str, dict[str, Any]] | None, str | None]:
        """Return a validated recovery target without mutating recovery state."""
        with self._lock:
            failure = next((f for f in self.failures if f.failure_id == caused_by_failure_id), None)
            if failure is None:
                return None, "未知 failure"
            if self.status in ("failed", "blocked"):
                return None, "任务已终态"
            if caused_by_failure_id != self._active_failure_id:
                return None, "recover 只能针对当前活动 failure"
            if action not in ("retry", "adjust", "ask", "block", "rollback"):
                return None, "不支持的 action"
            if self._reserved_repair_cycles:
                return None, "已有恢复动作正在授权"
            if action in ("retry", "adjust", "rollback") and self._repair_cycles >= MAX_REPAIR_CYCLES:
                self._terminal("failed", "Repair cycle 预算已耗尽", caused_by_failure_id)
                return None, "Repair cycle 预算已耗尽"
            if len(self.recovery_actions) >= MAX_RECOVERY_ACTIONS:
                self._terminal("blocked", "恢复动作预算已耗尽", caused_by_failure_id)
                return None, "恢复动作预算已耗尽"
            if action == "rollback":
                if requested_attempt is not None or requested_tool is not None or requested_arguments is not None:
                    return None, "rollback 不接受 requested_attempt、requested_tool 或 requested_arguments"
                checkpoint, detail = self._checkpoint_for_failure(checkpoint_id, failure)
                if detail:
                    return None, detail
                return ("rollback_checkpoint", {"checkpoint_id": checkpoint.checkpoint_id}), None
            if checkpoint_id is not None:
                return None, "checkpoint_id 仅供 rollback 使用"
            if action == "retry":
                if requested_tool is not None or requested_arguments is not None:
                    return None, "retry 不接受替换工具或参数"
                source_attempt = next((a for a in self.attempts if a.attempt_id == requested_attempt), None)
                if source_attempt is None or source_attempt.failure_id != caused_by_failure_id:
                    return None, "retry 必须引用直接失败 attempt"
                if not failure.retryable:
                    return None, "该 failure 不可重试"
                if self._failure_retry_counts.get(caused_by_failure_id, 0) >= MAX_FAILURE_RETRIES:
                    self._terminal("blocked", "failure retry 预算已耗尽", caused_by_failure_id)
                    return None, "failure retry 预算已耗尽"
                if source_attempt.tool in ("recover", "rollback_checkpoint"):
                    return None, "internal/recover 工具不能作为恢复目标"
                target = (
                    source_attempt.tool,
                    deepcopy(self._original_attempt_arguments.get(source_attempt.attempt_id, {})),
                )
            elif action == "adjust":
                if requested_attempt is not None:
                    return None, "adjust 不接受 requested_attempt"
                if not isinstance(requested_tool, str) or not isinstance(requested_arguments, dict):
                    return None, "adjust 需要目标工具和参数"
                if requested_tool in ("recover", "rollback_checkpoint"):
                    return None, "internal/recover 工具不能作为恢复目标"
                target = (requested_tool, deepcopy(requested_arguments))
            else:
                target = None
            if target is not None:
                fingerprint = (target[0], canonical_arguments_hash(target[1]))
                if self._fingerprint_counts.get(fingerprint, 0) >= MAX_ATTEMPT_FINGERPRINTS:
                    self._terminal("blocked", "同一参数指纹尝试预算已耗尽", caused_by_failure_id)
                    return None, "同一参数指纹尝试预算已耗尽"
                return target, None
            if requested_attempt is not None or requested_tool is not None or requested_arguments is not None:
                return None, "ask/block 不接受目标参数"
            return None, None

    def reserve_recovery(self, action: str, caused_by_failure_id: str, reason: str,
                         requested_attempt: str | None = None,
                         requested_tool: str | None = None,
                         requested_arguments: dict[str, Any] | None = None,
                         defer_generation: bool = False,
                         checkpoint_id: str | None = None) -> tuple[RecoveryAction | None, AttemptReservation | None, dict[str, Any] | None]:
        """Atomically validate and reserve one recovery action and successor generation."""
        with self._lock:
            failure = next((f for f in self.failures if f.failure_id == caused_by_failure_id), None)
            if failure is None:
                return self._reject_recovery(action, caused_by_failure_id, reason, "未知 failure") + (None,)
            if self.status in ("failed", "blocked"):
                return self._reject_recovery(action, caused_by_failure_id, reason, "任务已终态") + (None,)
            if caused_by_failure_id != self._active_failure_id:
                return self._reject_recovery(action, caused_by_failure_id, reason,
                                             "recover 只能针对当前活动 failure") + (None,)
            if action not in ("retry", "adjust", "ask", "block", "rollback"):
                return self._reject_recovery(action, caused_by_failure_id, reason, "不支持的 action") + (None,)
            if self._reserved_repair_cycles:
                return self._reject_recovery(action, caused_by_failure_id, reason,
                                             "已有恢复动作正在授权") + (None,)
            if len(self.recovery_actions) >= MAX_RECOVERY_ACTIONS:
                self._terminal("blocked", "恢复动作预算已耗尽", caused_by_failure_id)
                return self._reject_recovery(action, caused_by_failure_id, reason, "恢复动作预算已耗尽") + (None,)
            if action in ("retry", "adjust", "rollback") and self._repair_cycles >= MAX_REPAIR_CYCLES:
                self._terminal("failed", "Repair cycle 预算已耗尽", caused_by_failure_id)
                return self._reject_recovery(action, caused_by_failure_id, reason,
                                             "Repair cycle 预算已耗尽") + (None,)
            source_attempt = next((a for a in self.attempts if a.attempt_id == requested_attempt), None)
            if action == "rollback":
                if requested_attempt is not None or requested_tool is not None or requested_arguments is not None:
                    return self._reject_recovery(action, caused_by_failure_id, reason,
                                                "rollback 不接受 requested_attempt、requested_tool 或 requested_arguments",
                                                checkpoint_id=checkpoint_id) + (None,)
                checkpoint, detail = self._checkpoint_for_failure(checkpoint_id, failure)
                if detail:
                    return self._reject_recovery(action, caused_by_failure_id, reason,
                                                detail, checkpoint_id=checkpoint_id) + (None,)
                checkpoint_id = checkpoint.checkpoint_id
                requested_tool = None
                requested_arguments = None
            elif checkpoint_id is not None:
                return self._reject_recovery(action, caused_by_failure_id, reason,
                                            "checkpoint_id 仅供 rollback 使用",
                                            checkpoint_id=checkpoint_id) + (None,)
            elif action == "retry":
                if source_attempt is None or source_attempt.failure_id != caused_by_failure_id:
                    return self._reject_recovery(action, caused_by_failure_id, reason, "retry 必须引用直接失败 attempt") + (None,)
                if not failure.retryable:
                    return self._reject_recovery(action, caused_by_failure_id, reason, "该 failure 不可重试") + (None,)
                count = self._failure_retry_counts.get(caused_by_failure_id, 0)
                if count >= MAX_FAILURE_RETRIES:
                    return self._reject_recovery(action, caused_by_failure_id, reason, "failure retry 预算已耗尽") + (None,)
                if source_attempt.tool in ("recover", "rollback_checkpoint"):
                    return self._reject_recovery(action, caused_by_failure_id, reason,
                                                 "internal/recover 工具不能作为恢复目标") + (None,)
                requested_tool = source_attempt.tool
                requested_arguments = deepcopy(self._original_attempt_arguments.get(source_attempt.attempt_id, {}))
            elif action == "adjust":
                if requested_attempt is not None:
                    return self._reject_recovery(action, caused_by_failure_id, reason, "adjust 不接受 requested_attempt") + (None,)
                if not isinstance(requested_tool, str) or not isinstance(requested_arguments, dict):
                    return self._reject_recovery(action, caused_by_failure_id, reason, "adjust 需要目标工具和参数") + (None,)
                if requested_tool in ("recover", "rollback_checkpoint"):
                    return self._reject_recovery(action, caused_by_failure_id, reason,
                                                 "internal/recover 工具不能作为恢复目标") + (None,)
            elif requested_attempt or requested_tool or requested_arguments is not None:
                return self._reject_recovery(action, caused_by_failure_id, reason, "ask/block 不接受目标参数") + (None,)
            if action == "rollback":
                fingerprint = ("rollback_checkpoint", canonical_arguments_hash({"checkpoint_id": checkpoint_id}))
                if self._fingerprint_counts.get(fingerprint, 0) >= MAX_ATTEMPT_FINGERPRINTS:
                    self._terminal("blocked", "同一 rollback 参数指纹尝试预算已耗尽", caused_by_failure_id)
                    return self._reject_recovery(action, caused_by_failure_id, reason,
                                                 "同一 rollback 参数指纹尝试预算已耗尽",
                                                 checkpoint_id=checkpoint_id) + (None,)
                self._fingerprint_counts[fingerprint] = self._fingerprint_counts.get(fingerprint, 0) + 1
            elif action in ("retry", "adjust"):
                fingerprint = (requested_tool, canonical_arguments_hash(requested_arguments or {}))
                if self._fingerprint_counts.get(fingerprint, 0) >= MAX_ATTEMPT_FINGERPRINTS:
                    self._terminal("blocked", "同一参数指纹尝试预算已耗尽", caused_by_failure_id)
                    return self._reject_recovery(action, caused_by_failure_id, reason, "同一参数指纹尝试预算已耗尽") + (None,)
                self._fingerprint_counts[fingerprint] = self._fingerprint_counts.get(fingerprint, 0) + 1
                if action == "retry":
                    self._failure_retry_counts[caused_by_failure_id] = count + 1
            if action in ("retry", "adjust", "rollback"):
                # Reserve the task-wide cycle before authorization. A denied
                # target releases it; activation converts it to used quota.
                self._reserved_repair_cycles += 1
            rid = f"r-{self._next_recovery}"; self._next_recovery += 1
            action_record = RecoveryAction(rid, self._verification_generation, action, str(reason or "")[:500], caused_by_failure_id, "proposed",
                requested_attempt, requested_tool,
                canonical_arguments_hash(requested_arguments) if isinstance(requested_arguments, dict) else None,
                redacted_arguments(requested_arguments) if isinstance(requested_arguments, dict) else None,
                None, None, checkpoint_id)
            self.recovery_actions.append(action_record)
            if defer_generation:
                return action_record, None, requested_arguments
            return self._activate_recovery(action_record, requested_arguments)

    def activate_recovery(self, record: RecoveryAction, arguments: dict[str, Any] | None):
        """Open the generation only after the reserved target is authorized."""
        with self._lock:
            current = next(a for a in self.recovery_actions if a.recovery_id == record.recovery_id)
            if current.status != "proposed":
                raise ValueError("恢复额度已完成或拒绝")
            if self.status in ("blocked", "failed"):
                return self._deny_reserved_recovery(current, "任务已终态"), None, arguments
            return self._activate_recovery(current, arguments)

    def deny_reserved_recovery(self, record: RecoveryAction, detail: str) -> RecoveryAction:
        with self._lock:
            return self._deny_reserved_recovery(record, detail)

    def _deny_reserved_recovery(self, record, detail):
        index = next(i for i, a in enumerate(self.recovery_actions) if a.recovery_id == record.recovery_id)
        current = self.recovery_actions[index]
        if current.status != "proposed":
            raise ValueError("恢复额度已完成或拒绝")
        if current.requested_tool is not None:
            fingerprint = (current.requested_tool, current.requested_arguments_hash)
            self._fingerprint_counts[fingerprint] -= 1
        elif current.action == "rollback" and current.checkpoint_id is not None:
            fingerprint = (
                "rollback_checkpoint",
                canonical_arguments_hash({"checkpoint_id": current.checkpoint_id}),
            )
            self._fingerprint_counts[fingerprint] -= 1
        if current.action == "retry":
            self._failure_retry_counts[current.caused_by_failure_id] -= 1
        if current.action in ("retry", "adjust", "rollback"):
            self._reserved_repair_cycles -= 1
        rejected = RecoveryAction(**{**asdict(current), "status": "rejected"})
        self.recovery_actions[index] = rejected
        self.recovery_notice = f"Recovery {current.recovery_id} rejected: {detail}."
        if len(self.recovery_actions) >= MAX_RECOVERY_ACTIONS:
            self._terminal("blocked", "恢复动作预算已耗尽", current.caused_by_failure_id)
        return rejected

    def _activate_recovery(self, action_record, requested_arguments):
        rid = action_record.recovery_id
        action = action_record.action
        caused_by_failure_id = action_record.caused_by_failure_id
        failure = next(f for f in self.failures if f.failure_id == caused_by_failure_id)
        if action in ("retry", "adjust", "rollback"):
            self._reserved_repair_cycles -= 1
            self._repair_cycles += 1
        gid = self._verification_generation + 1
        self._verification_generation = gid
        self.verification_evidence.clear(); self._last_verified_generation = -1; self._verification_required = True
        action_record = RecoveryAction(**{**asdict(action_record), "status": "reserved",
                                         "generation_id": gid, "result_generation_id": gid})
        index = next(i for i, a in enumerate(self.recovery_actions) if a.recovery_id == rid)
        self.recovery_actions[index] = action_record
        self.generations.append(ExecutionGeneration(gid, opened_by_failure_id=caused_by_failure_id,
            opened_by_recovery_id=rid, open_reason="recovery"))
        self._active_failure_id = caused_by_failure_id
        self._enter_verification(rid)
        self.recovery_notice = f"Recovery {rid} reserved: {action}; verify generation {gid} independently."
        if action in ("ask", "block"):
            self.status = "blocked"
            self.terminal_reason = ("等待外部条件" if action == "ask" else "按恢复策略保守停止") + f"; last_failure={caused_by_failure_id}"
            self.recovery_actions[index] = RecoveryAction(**{**asdict(action_record), "status": "terminal"})
            return self.recovery_actions[index], None, requested_arguments
        ar = AttemptReservation(
            f"a-{self._next_attempt}", gid - 1, gid,
            caused_by_failure_id, failure.caused_by_attempt_id, rid,
            fingerprint_reserved=True,
        )
        self._next_attempt += 1
        return action_record, ar, requested_arguments

    def reject_recovery(self, action: Any, caused_by_failure_id: Any, reason: Any,
                        detail: str, requested_attempt: str | None = None,
                        requested_tool: str | None = None,
                        requested_arguments: dict[str, Any] | None = None,
                        checkpoint_id: str | None = None,
                        block: bool = False) -> RecoveryAction:
        """Record one rejected recovery request without opening a generation."""
        with self._lock:
            record, _ = self._reject_recovery(
                action, caused_by_failure_id, reason, detail,
                requested_attempt, requested_tool, requested_arguments,
                checkpoint_id,
            )
            if block:
                self._terminal("blocked", f"rollback rejected: {detail}", str(caused_by_failure_id))
            return record

    def _reject_recovery(self, action, failure_id, reason, detail,
                         requested_attempt=None, requested_tool=None,
                         requested_arguments=None, checkpoint_id=None):
        rid = f"r-{self._next_recovery}"; self._next_recovery += 1
        rec = RecoveryAction(
            rid, self._verification_generation, action if isinstance(action, str) and action else "<missing>",
            str(reason or "")[:500], failure_id, "rejected", requested_attempt,
            requested_tool,
            canonical_arguments_hash(requested_arguments) if isinstance(requested_arguments, dict) else None,
            redacted_arguments(requested_arguments) if isinstance(requested_arguments, dict) else None,
            None, None, checkpoint_id,
        )
        self.recovery_actions.append(rec)
        self.recovery_notice = f"Recovery {rid} rejected: {str(detail)[:500]}."
        if len(self.recovery_actions) >= MAX_RECOVERY_ACTIONS:
            self._terminal("blocked", "恢复动作预算已耗尽", failure_id)
        return rec, detail

    def record_execution_result(self, result: Any) -> ExecutionAttempt:
        """Commit an ExecutionResult and derive attempt/failure/verification facts."""
        with self._lock:
            self._ensure_generation()
            # A phase-gate rejection is a protocol fact, not a new business
            # failure. Keep an auditable attempt while preserving the active
            # failure that diagnosis/recovery is currently addressing.
            if result.error_kind == "repair_phase_gate":
                attempt_id = f"a-{self._next_attempt}"
                self._next_attempt += 1
                generation_id = self._verification_generation
                args = deepcopy(result.arguments)
                attempt = ExecutionAttempt(
                    attempt_id, generation_id, generation_id, result.tool,
                    canonical_arguments_hash(args), redacted_arguments(args),
                    result.outcome, result.duration_ms, result.effect_class,
                    result.handler_admitted, result.permission,
                    output_excerpt=result.output_excerpt,
                    error_kind=result.error_kind,
                )
                self.attempts.append(attempt)
                self._original_attempt_arguments[attempt_id] = deepcopy(args)
                return attempt
            was_terminal = self.status in ("blocked", "failed")
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
            if not getattr(reservation, "fingerprint_reserved", False):
                self._fingerprint_counts[fingerprint] = self._fingerprint_counts.get(fingerprint, 0) + 1
            is_verify = (
                result.tool == "run_shell"
                and args.get("purpose", "execution") == "verification"
                and not getattr(reservation, "recovery_id", None)
            )
            is_recovery_attempt = bool(getattr(reservation, "recovery_id", None))
            failure_id = None
            category: FailureCategory | None = None
            retryable = False
            phase: Literal["execute", "verify", "recover"] = "recover" if is_recovery_attempt else "execute"
            if result.outcome != "succeeded":
                failure_id = f"f-{self._next_failure}"; self._next_failure += 1
                if result.error_kind == "attempt_fingerprint_budget":
                    category, retryable = "transient", True
                elif result.outcome == "denied": category = "permission"
                elif result.outcome == "timeout": category, retryable = "transient", True
                elif result.outcome == "invalid": category = "protocol"
                elif is_verify:
                    category, retryable, phase = "validation", True, "verify"
                elif result.error_kind in ("edit_no_match", "edit_multiple_matches"):
                    category = "deterministic"
                elif result.error_kind in ("rollback_conflict", "rollback_restore_failed"):
                    category = "unknown"
                elif result.effect_class == "possible" and result.error_kind == "handler_exception":
                    category = "unknown"
                else: category = "deterministic"
            attempt = ExecutionAttempt(
                attempt_id, pre_generation, generation_id, result.tool, arguments_hash,
                redacted_arguments(args), result.outcome, result.duration_ms,
                result.effect_class, result.handler_admitted, result.permission,
                caused_by_failure_id=getattr(reservation, "caused_by_failure_id", None),
                caused_by_attempt_id=getattr(reservation, "caused_by_attempt_id", None),
                exit_code=result.exit_code, error_kind=result.error_kind,
                output_excerpt=result.output_excerpt, failure_id=failure_id,
                recovery_id=getattr(reservation, "recovery_id", None),
                checkpoint_id=getattr(result, "checkpoint_id", None))
            self.attempts.append(attempt)
            self._original_attempt_arguments[attempt_id] = deepcopy(args)
            if getattr(reservation, "recovery_id", None):
                rid = reservation.recovery_id
                for i, action in enumerate(self.recovery_actions):
                    if action.recovery_id == rid:
                        self.recovery_actions[i] = RecoveryAction(**{**asdict(action), "status": "executed", "result_attempt": attempt_id, "result_generation_id": generation_id})
                        break
            if result.tool != "update_todo":
                self.tool_history.append({"tool": result.tool, "arguments_hash": arguments_hash,
                                          "ok": result.outcome == "succeeded", "brief": result.output_excerpt})
            path = args.get("path")
            if result.tool == "rollback_checkpoint" and getattr(reservation, "recovery_id", None):
                recovery = next(
                    (item for item in self.recovery_actions
                     if item.recovery_id == reservation.recovery_id),
                    None,
                )
                checkpoint = (
                    self._checkpoint_store.get(recovery.checkpoint_id)
                    if recovery is not None and recovery.checkpoint_id is not None
                    and self._checkpoint_store is not None else None
                )
                path = checkpoint.path if checkpoint is not None else None
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
                self._enter_diagnosis(failure_id)
                if not was_terminal:
                    self.recovery_notice = (f"Failure {failure_id} ({category}) requires diagnosis; "
                                            f"caused by {attempt_id} in generation {generation_id}.")
                exhausted = self._fingerprint_counts[fingerprint] >= MAX_ATTEMPT_FINGERPRINTS
                if result.error_kind == "rollback_conflict":
                    self._terminal("blocked", "rollback_conflict：目标文件已发生外部变化", failure_id)
                elif result.error_kind == "rollback_restore_failed":
                    self._terminal("blocked", "rollback_restore_failed：恢复操作未完成", failure_id)
                elif category == "permission": self._terminal("failed", "权限被明确拒绝", failure_id)
                elif category == "protocol":
                    if self.status not in ("blocked", "failed"):
                        self.status = "running"
                elif category == "unknown":
                    checkpoint = (
                        self._checkpoint_store.get(result.checkpoint_id)
                        if result.tool in ("write_file", "edit_file")
                        and result.checkpoint_id is not None
                        and self._checkpoint_store is not None else None
                    )
                    if (result.error_kind == "handler_exception" and
                            result.effect_class == "possible" and
                            checkpoint is not None and checkpoint.status == "ready" and
                            not was_terminal):
                        # A handler exception may have happened after a partial
                        # write. The finalized before/after images make the
                        # bounded rollback path deterministic, so leave the
                        # task recoverable instead of treating it as unknown.
                        self.status = "running"
                        self.recovery_notice = (
                            f"Failure {failure_id} caused by {attempt_id} left ready "
                            f"checkpoint {checkpoint.checkpoint_id} for {checkpoint.path}; "
                            "rollback may be requested before independent verification."
                        )
                    else:
                        self._terminal("blocked", "副作用范围未知，需要外部诊断", failure_id)
                elif category == "deterministic":
                    if self.status not in ("blocked", "failed"):
                        self.status = "running"
                elif category == "validation" and self._repair_cycles >= MAX_REPAIR_CYCLES:
                    self._terminal("failed", "Repair cycle 预算已耗尽", failure_id)
                elif result.error_kind == "attempt_fingerprint_budget":
                    self._terminal("blocked", "同一参数指纹尝试预算已耗尽", failure_id)
                elif retryable and exhausted:
                    self._terminal("blocked", "同一参数指纹尝试预算已耗尽", failure_id)
                if (self.status not in ("blocked", "failed")
                        and self._repair_cycles >= MAX_REPAIR_CYCLES):
                    # The current failure would require another executable
                    # recovery action, but all repair cycles have already run.
                    self._terminal("failed", "Repair cycle 预算已耗尽", failure_id)
            elif is_verify and result.handler_admitted:
                passed = result.outcome == "succeeded" and result.exit_code == 0
                if passed:
                    self._clear_repair()
            return attempt

    def _terminal(self, status: str, reason: str, failure_id: str) -> None:
        if self.status in ("blocked", "failed"):
            return
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
                if passed:
                    self._clear_repair()
                else:
                    # Legacy callback integrations do not create FailureEvent
                    # records, so they cannot participate in the structured
                    # Repair Loop. Retain the historical completion signal.
                    self._verification_required = True
            if not ok: self.errors.append(f"{name}: {brief}")

    def begin_task(self, task: str) -> None:
        with self._lock:
            self.task = task; self.current_goal = ""; self.status = "running"; self.terminal_reason = ""
            self.tool_history.clear(); self.files_changed.clear(); self.errors.clear(); self.todos.clear()
            self.verification_evidence.clear(); self.generations.clear(); self.attempts.clear()
            self.failures.clear(); self.recovery_actions.clear(); self.recovery_notice = ""
            self._verification_generation = 0; self._last_verified_generation = -1
            self._verification_required = False; self._repair_phase = "idle"
            self._active_failure_id = None; self._active_recovery_id = None
            self._next_attempt = 1; self._next_failure = 1
            self._fingerprint_counts.clear(); self._repair_cycles = 0; self._reserved_repair_cycles = 0
            self._failure_retry_counts.clear(); self._original_attempt_arguments.clear(); self._next_recovery = 1
            if self._checkpoint_store is not None:
                self._checkpoint_store.clear()
            self.generations.append(ExecutionGeneration(0, open_reason="task_start"))

    def reset_task(self, task: str = "") -> None:
        """Reset all task-local state in place, preserving bound tool references."""
        self.begin_task(task)
        if not task:
            with self._lock:
                self.status = "idle"

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
            needs_verify = self._verification_required
            needs_repair = self._repair_phase != "idle"
            if not missing and not needs_verify and not needs_repair:
                return None
            # Describe observable completion facts instead of counting
            # reminders. The marker changes when Todo state, tool
            # observations, verification evidence, or its generation changes.
            progress_marker = (
                tuple((todo.content, todo.status) for todo in self.todos),
                len(self.tool_history),
                len(self.verification_evidence),
                self._verification_generation,
                needs_verify,
                self._repair_phase,
                self._active_failure_id,
                self._active_recovery_id,
                self._repair_cycles,
                len(self.failures),
                len(self.recovery_actions),
            )
            if self._repair_phase == "diagnosis_required":
                message = (
                    "检测到失败。请在下一条回复中先进行只读调查、更新 Todo，或独占调用 recover "
                    "处理当前活动 failure；不要直接执行副作用或 verification。"
                )
            elif self._repair_phase == "verification_required":
                message = "恢复动作已完成或存在待验证 generation。下一条回复只能独占调用 run_shell(purpose=verification)。"
            else:
                message = (
                    "任务尚未满足完成条件。请在下一条回复中调用能推进任务的工具 "
                    "（更新 Todo、执行调查/操作或运行验证）；确实无法继续时才说明具体阻塞原因。"
                )
            return {
                "unfinished_todos": missing,
                "verification_required": needs_verify,
                "repair_phase": self._repair_phase,
                "active_failure_id": self._active_failure_id,
                "active_recovery_id": self._active_recovery_id,
                "progress_marker": progress_marker,
                "message": message,
            }

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
                "repair_loop": {
                    "phase": self._repair_phase,
                    "active_failure_id": self._active_failure_id,
                    "active_recovery_id": self._active_recovery_id,
                    "cycles_used": self._repair_cycles,
                    "cycles_remaining": max(
                        0, MAX_REPAIR_CYCLES - self._repair_cycles - self._reserved_repair_cycles
                    ),
                    "required_next_action": (
                        "diagnose_or_recover" if self._repair_phase == "diagnosis_required"
                        else "独立 verification" if self._repair_phase == "verification_required"
                        else "continue"
                    ),
                },
                "current_generation_id": self._verification_generation,
                "generations": [asdict(x) for x in self.generations],
                "attempts": [asdict(x) for x in self.attempts],
                "failures": [asdict(x) for x in self.failures],
                "recovery_actions": [asdict(x) for x in self.recovery_actions],
                "checkpoints": self._checkpoint_store.snapshot() if self._checkpoint_store is not None else [],
                "rollback_checkpoints": (
                    [checkpoint.snapshot() for checkpoint in self._checkpoint_store.available()]
                    if self._checkpoint_store is not None else []
                ),
                "latest_failure": asdict(self.failures[-1]) if self.failures else None,
                "recovery_notice": self.recovery_notice,
                "budgets": {"failure_retries_remaining": max(0, MAX_FAILURE_RETRIES - sum(self._failure_retry_counts.values())),
                            "fingerprint_attempts_limit": MAX_ATTEMPT_FINGERPRINTS,
                            "fingerprint_attempts_remaining": [
                                {"tool": tool, "arguments_hash": arguments_hash,
                                 "remaining": max(0, MAX_ATTEMPT_FINGERPRINTS - count)}
                                for (tool, arguments_hash), count in sorted(self._fingerprint_counts.items())
                            ],
                            "recovery_actions_remaining": max(0, MAX_RECOVERY_ACTIONS - len(self.recovery_actions)),
                            "repair_cycles_remaining": max(
                                0, MAX_REPAIR_CYCLES - self._repair_cycles - self._reserved_repair_cycles
                            ),
                            "repair_cycles_used": self._repair_cycles},
            }
