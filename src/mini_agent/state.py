"""Thread-safe task state and auditable execution facts."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
import hashlib
import json
import re
from threading import Lock
from typing import Any, Literal

from mini_agent.checkpoint import CheckpointStore
from mini_agent.config import (MAX_ATTEMPT_FINGERPRINTS, MAX_FAILURE_RETRIES,
                               MAX_NO_PROGRESS_REPLANS, MAX_REPLAN_REVISIONS,
                               MAX_RECOVERY_ACTIONS, MAX_REPAIR_CYCLES,
                               MAX_STAGNANT_ROUNDS)

EffectClass = Literal["none", "possible"]
AttemptOutcome = Literal["succeeded", "failed", "denied", "timeout", "invalid"]
FailureCategory = Literal["protocol", "permission", "transient", "deterministic", "validation", "unknown"]
RepairPhase = Literal["idle", "diagnosis_required", "verification_required"]
PlanStepStatus = Literal["pending", "in_progress", "completed"]
PlanningPhase = Literal["direct", "exploring", "awaiting_approval", "executing"]
ProcessStatus = Literal["running", "exited", "failed", "terminated"]
ProcessEventKind = Literal["started", "exited", "failed", "terminated", "killed", "cleanup_failed"]

_STEP_ID_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_PLAN_STEP_LIMIT = 50
_PLAN_GOAL_MAX = 1200
_PLAN_REASON_MAX = 600
_PLAN_TEXT_MAX = 240
_PLAN_CONSTRAINT_LIMIT = 20
_PLAN_TASK_CRITERIA_LIMIT = 20
_PLAN_STEP_CRITERIA_LIMIT = 10
_PLAN_REFERENCE_LIMIT = 50
_MISSING = object()


class AttemptBudgetExceeded(ValueError):
    """A tool call reached the per-argument execution budget before its handler."""


class PlanRejected(ValueError):
    """A model plan request failed validation without becoming an execution failure."""


@dataclass(frozen=True)
class PlanStepDifference:
    step_id: str
    dependencies_changed: bool = False


@dataclass(frozen=True)
class PlanDifference:
    retained: tuple[PlanStepDifference, ...] = ()
    added: tuple[str, ...] = ()
    cancelled: tuple[str, ...] = ()
    replaced: tuple[str, ...] = ()
    goal_changed: bool = False
    constraints_changed: bool = False
    success_criteria_changed: bool = False


@dataclass(frozen=True)
class LoopStagnationState:
    progress_epoch: int = 0
    consecutive_no_progress_rounds: int = 0
    last_round_fingerprint: str | None = None
    seen_observation_hashes: tuple[str, ...] = ()
    seen_effect_action_hashes: tuple[str, ...] = ()
    warning_kind: str | None = None
    last_reason: str | None = None


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
class PlanStep:
    step_id: str
    content: str
    status: PlanStepStatus = "pending"
    depends_on: tuple[str, ...] = ()
    success_criteria: tuple[str, ...] = ()
    replaces: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanRevision:
    revision_id: int
    generation_id: int
    parent_revision_id: int | None
    trigger_id: int | None
    goal: str
    constraints: tuple[str, ...]
    success_criteria: tuple[str, ...]
    steps: tuple[PlanStep, ...]
    reason: str
    diff: PlanDifference | None = None


@dataclass(frozen=True)
class PlanProgressEvent:
    progress_id: int
    revision_id: int
    generation_id: int
    step_id: str
    from_status: str
    to_status: str
    reason: str


@dataclass(frozen=True)
class PlanningState:
    mode: Literal["auto", "plan_only"] = "auto"
    phase: PlanningPhase = "direct"
    active_revision_id: int | None = None
    active_trigger_id: int | None = None
    replans_used: int = 0
    replans_remaining: int = MAX_REPLAN_REVISIONS
    trigger_no_progress_commits: int = 0


@dataclass(frozen=True)
class UserPlanDecision:
    decision_id: int
    revision_id: int | None
    decision: Literal["approved", "rejected", "continue_exploring", "resume_blocked"]
    feedback: str | None
    generation_id: int
    previous_terminal_reason: str | None = None
    caused_by_failure_id: str | None = None


@dataclass(frozen=True)
class ReplanTrigger:
    trigger_id: int
    generation_id: int
    kind: Literal["failure", "observation", "user_feedback", "blocked_resume"]
    reason: str
    caused_by_failure_id: str | None = None
    caused_by_attempt_id: str | None = None
    caused_by_decision_id: int | None = None
    status: Literal["active", "resolved", "rejected"] = "active"
    result_revision_id: int | None = None


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
    opened_by_process_event_id: str | None = None
    open_reason: Literal["task_start", "possible_effect", "recovery", "process_exit"] = "task_start"


@dataclass(frozen=True)
class ProcessRecord:
    """Serializable current projection for one task-owned process."""

    process_id: str
    task_id: str
    start_attempt_id: str
    start_generation_id: int
    command_summary: str
    cwd_summary: str
    pid: int
    status: ProcessStatus
    started_at: str
    ended_at: str | None = None
    exit_code: int | None = None
    stdout_offset: int = 0
    stderr_offset: int = 0
    terminal_event_id: str | None = None


@dataclass(frozen=True)
class ProcessEvent:
    """Append-only lifecycle fact; log bodies never enter State."""

    event_id: str
    process_id: str
    task_id: str
    kind: ProcessEventKind
    generation_id: int
    start_attempt_id: str
    stdout_offset: int
    stderr_offset: int
    exit_code: int | None = None
    caused_by_control_attempt_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ProcessWaitState:
    process_ids: tuple[str, ...]
    reason: Literal["no_new_output", "still_running"]
    last_observed_event_ids: tuple[str, ...] = ()
    last_stdout_offsets: tuple[int, ...] = ()
    last_stderr_offsets: tuple[int, ...] = ()


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


@dataclass(frozen=True)
class TraceEvent:
    """One task-local ordered pointer into a saved execution fact."""

    sequence_id: int
    kind: str
    generation_id: int
    revision_id: int | None = None
    record_type: str | None = None
    record_id: int | str | None = None
    planning_phase_before: PlanningPhase | None = None
    planning_phase_after: PlanningPhase | None = None
    repair_phase_before: RepairPhase | None = None
    repair_phase_after: RepairPhase | None = None
    stagnation_kind: str | None = None
    stagnation_count: int | None = None
    stagnation_fingerprint: str | None = None


@dataclass
class AgentState:
    task: str = ""
    task_id: str = ""
    current_goal: str = ""
    tool_history: list[dict] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    status: str = "running"
    terminal_reason: str = ""
    plan_revisions: list[PlanRevision] = field(default_factory=list)
    plan_progress_history: list[PlanProgressEvent] = field(default_factory=list)
    user_plan_decisions: list[UserPlanDecision] = field(default_factory=list)
    replan_triggers: list[ReplanTrigger] = field(default_factory=list)
    planning_state: PlanningState = field(default_factory=PlanningState)
    stagnation_state: LoopStagnationState = field(default_factory=LoopStagnationState)
    verification_evidence: list[VerificationEvidence] = field(default_factory=list)
    verification_history: list[VerificationEvidence] = field(default_factory=list)
    generations: list[ExecutionGeneration] = field(default_factory=list)
    attempts: list[ExecutionAttempt] = field(default_factory=list)
    failures: list[FailureEvent] = field(default_factory=list)
    recovery_actions: list[RecoveryAction] = field(default_factory=list)
    trace_events: list[TraceEvent] = field(default_factory=list)
    process_records: list[ProcessRecord] = field(default_factory=list)
    process_events: list[ProcessEvent] = field(default_factory=list)
    awaiting_process: ProcessWaitState | None = None
    recovery_notice: str = ""
    _verification_generation: int = field(default=0, init=False, repr=False)
    _last_verified_generation: int = field(default=-1, init=False, repr=False)
    _verification_required: bool = field(default=False, init=False, repr=False)
    _next_attempt: int = field(default=1, init=False, repr=False)
    _next_failure: int = field(default=1, init=False, repr=False)
    _next_plan_revision: int = field(default=1, init=False, repr=False)
    _next_plan_progress: int = field(default=1, init=False, repr=False)
    _next_plan_decision: int = field(default=1, init=False, repr=False)
    _next_plan_trigger: int = field(default=1, init=False, repr=False)
    _fingerprint_counts: dict[tuple[str, str], int] = field(default_factory=dict, init=False, repr=False)
    _repair_cycles: int = field(default=0, init=False, repr=False)
    _reserved_repair_cycles: int = field(default=0, init=False, repr=False)
    _repair_phase: RepairPhase = field(default="idle", init=False, repr=False)
    _active_failure_id: str | None = field(default=None, init=False, repr=False)
    _active_recovery_id: str | None = field(default=None, init=False, repr=False)
    _failure_retry_counts: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _original_attempt_arguments: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _next_recovery: int = field(default=1, init=False, repr=False)
    _next_trace_sequence: int = field(default=1, init=False, repr=False)
    _next_task_id: int = field(default=1, init=False, repr=False)
    _next_process_event: int = field(default=1, init=False, repr=False)
    _revision_attempt_boundaries: dict[int, int] = field(default_factory=dict, init=False, repr=False)
    _stagnation_progress_marker: str | None = field(default=None, init=False, repr=False)
    _checkpoint_store: CheckpointStore | None = field(default=None, init=False, repr=False, compare=False)
    _process_manager: Any = field(default=None, init=False, repr=False, compare=False)
    _lock: Any = field(default_factory=Lock, init=False, repr=False, compare=False)
    _projection_ready: bool = field(default=False, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # ``current_goal`` remains accepted by the dataclass constructor for
        # old callers, but the runtime projection is always derived from the
        # active Plan Contract and never from that input value.
        object.__setattr__(self, "current_goal", "")
        object.__setattr__(self, "_projection_ready", True)
        object.__setattr__(self, "_stagnation_progress_marker", self._progress_marker_locked())

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "current_goal" and getattr(self, "_projection_ready", False):
            raise AttributeError("current_goal 是只读的 active plan 投影")
        object.__setattr__(self, name, value)

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

    def _progress_marker_locked(self) -> str:
        """Return the narrow, durable task marker used by stagnation detection."""
        active = self._plan_view_locked()
        trigger = next(
            (item for item in self.replan_triggers
             if item.trigger_id == self.planning_state.active_trigger_id),
            None,
        )
        latest_verification = next(
            (
                (item.outcome, item.exit_code)
                for item in reversed(self.verification_evidence)
                if item.generation_id == self._verification_generation
            ),
            None,
        )
        active_failure = next(
            (item for item in self.failures
             if item.failure_id == self._active_failure_id),
            None,
        )
        source_attempt = next(
            (item for item in self.attempts
             if active_failure is not None
             and item.attempt_id == active_failure.caused_by_attempt_id),
            None,
        )
        failure_summary = (
            active_failure.category,
            active_failure.phase,
            active_failure.retryable,
            active_failure.affected_files,
            source_attempt.tool if source_attempt is not None else None,
            source_attempt.arguments_hash if source_attempt is not None else None,
            source_attempt.outcome if source_attempt is not None else None,
            source_attempt.error_kind if source_attempt is not None else None,
            source_attempt.exit_code if source_attempt is not None else None,
        ) if active_failure is not None else None
        marker = {
            "plan": (
                active.get("goal"), active.get("constraints"),
                active.get("success_criteria"),
                tuple((step["step_id"], step["content"], step["status"],
                       tuple(step["depends_on"]), tuple(step["success_criteria"]),
                       tuple(step["replaces"])) for step in active.get("steps", [])),
            ) if active else None,
            "planning_phase": self.planning_state.phase,
            "repair_phase": self._repair_phase,
            # Failure IDs are task-local sequence numbers.  They must not
            # turn the same underlying failure into apparent progress.
            "active_failure": failure_summary,
            "trigger": (
                trigger.kind, trigger.caused_by_failure_id,
                trigger.caused_by_attempt_id, trigger.caused_by_decision_id,
            ) if trigger is not None else None,
            "decisions": tuple(
                (decision.revision_id, decision.decision, decision.feedback,
                 decision.previous_terminal_reason, decision.caused_by_failure_id)
                for decision in self.user_plan_decisions
            ),
            "files_changed": tuple(sorted(self.files_changed)),
            "verification_required": self._verification_required,
            "verification_conclusion": latest_verification,
            "processes": tuple(
                (item.process_id, item.status, item.stdout_offset, item.stderr_offset)
                for item in self.process_records
            ),
        }
        return json.dumps(marker, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), default=str)

    def _allowed_next_action_locked(self) -> str:
        if self.status == "failed":
            return "使用 /new <任务>"
        if self.status == "blocked":
            resumable = (
                self.planning_state.active_trigger_id is None
                and self.planning_state.replans_remaining > 0
                and "预算已耗尽" not in (self.terminal_reason or "")
                and "budget_exhausted" not in (self.terminal_reason or "")
            )
            return "使用 /resume <反馈>，或 /new <任务>" if resumable else "使用 /new <任务>"
        if self.planning_state.phase == "awaiting_approval":
            return "等待 CLI 用户批准、驳回或继续调查"
        if self.status == "awaiting_process":
            return "继续输入以观察后台进程"
        if self._repair_phase == "verification_required":
            return "独占调用 run_shell(purpose=verification)"
        if self._repair_phase == "diagnosis_required":
            if self.planning_state.phase == "exploring":
                return "只读调查，或独占调用 commit_plan"
            return (
                "只读诊断、独占 recover，或独占调用 "
                "request_replan(kind=failure, source_id=active_failure_id)"
            )
        if self.planning_state.phase == "exploring":
            return "只读调查，或独占调用 commit_plan"
        return "执行能推进任务的工具；有合格观察时可独占 request_replan"

    @staticmethod
    def _short_hash(value: str | None) -> str:
        return (value or "")[:12] or "-"

    def _append_trace_event_locked(
        self,
        kind: str,
        *,
        generation_id: int | None = None,
        revision_id: int | None = None,
        record_type: str | None = None,
        record_id: int | str | None = None,
        planning_phase_before: Any = _MISSING,
        planning_phase_after: Any = _MISSING,
        repair_phase_before: Any = _MISSING,
        repair_phase_after: Any = _MISSING,
        stagnation_kind: str | None = None,
        stagnation_count: int | None = None,
        stagnation_fingerprint: str | None = None,
    ) -> TraceEvent:
        """Append a bounded pointer event while the State lock is held."""
        current_planning = self.planning_state.phase
        current_repair = self._repair_phase

        def phase_change(before: Any, after: Any, current: Any) -> tuple[Any, Any]:
            if before is _MISSING and after is _MISSING:
                return None, None
            before_value = None if before is _MISSING else before
            after_value = current if after is _MISSING else after
            if before_value == after_value:
                return None, None
            return before_value, after_value

        planning_before, planning_after = phase_change(
            planning_phase_before, planning_phase_after, current_planning,
        )
        repair_before, repair_after = phase_change(
            repair_phase_before, repair_phase_after, current_repair,
        )
        event = TraceEvent(
            self._next_trace_sequence,
            str(kind),
            self._verification_generation if generation_id is None else generation_id,
            revision_id,
            record_type,
            record_id,
            planning_before,
            planning_after,
            repair_before,
            repair_after,
            stagnation_kind,
            stagnation_count,
            self._short_hash(stagnation_fingerprint) if stagnation_fingerprint is not None else None,
        )
        self.trace_events.append(event)
        self._next_trace_sequence += 1
        return event

    def _stagnation_kind_locked(self, repeated_round: bool,
                                tool_names: tuple[str, ...],
                                has_observation: bool = False) -> str:
        phase = self.planning_state.phase
        if phase == "exploring":
            if any(name not in {
                "commit_plan", "update_plan_progress", "request_replan",
                "begin_plan", "cancel_planning", "recover",
            } for name in tool_names):
                return "no_new_observation"
            return "explore_without_commit"
        if phase == "direct" and repeated_round:
            return "repeated_action"
        if phase == "direct":
            if has_observation:
                return "no_new_observation"
            return "execute_without_progress"
        return "execute_without_progress"

    def observe_tool_round(self, action_fingerprint: str | None,
                           observation_hashes: list[str] | tuple[str, ...] = (),
                           effect_action_hashes: list[str] | tuple[str, ...] = (),
                           tool_names: list[str] | tuple[str, ...] = ()) -> dict[str, Any]:
        """Observe one complete tool round after all facts and results are committed."""
        with self._lock:
            if self.status in ("blocked", "failed") or self.planning_state.phase == "awaiting_approval":
                return {"blocked": False, "warning": None,
                        "count": self.stagnation_state.consecutive_no_progress_rounds,
                        "allowed_next_action": self._allowed_next_action_locked()}
            marker = self._progress_marker_locked()
            current = self.stagnation_state
            current_fingerprint = action_fingerprint
            if self._stagnation_progress_marker != marker:
                observation_seen = tuple(dict.fromkeys(observation_hashes))[:256]
                effect_seen = tuple(dict.fromkeys(effect_action_hashes))[:256]
                self.stagnation_state = LoopStagnationState(
                    current.progress_epoch + 1, 0, current_fingerprint,
                    observation_seen, effect_seen, None, None,
                )
                self._stagnation_progress_marker = marker
                return {"blocked": False, "warning": None, "progress": True,
                        "count": 0, "allowed_next_action": self._allowed_next_action_locked()}

            observation_seen = list(current.seen_observation_hashes)
            effect_seen = list(current.seen_effect_action_hashes)
            first_fact = False
            for value in observation_hashes:
                if value in observation_seen:
                    continue
                if len(observation_seen) >= 256:
                    break
                observation_seen.append(value)
                first_fact = True
            for value in effect_action_hashes:
                if value in effect_seen:
                    continue
                if len(effect_seen) >= 256:
                    break
                effect_seen.append(value)
                first_fact = True
            repeated_round = bool(
                current_fingerprint and current_fingerprint == current.last_round_fingerprint
            )
            if first_fact:
                self.stagnation_state = replace(
                    current, consecutive_no_progress_rounds=0,
                    last_round_fingerprint=current_fingerprint,
                    seen_observation_hashes=tuple(observation_seen),
                    seen_effect_action_hashes=tuple(effect_seen),
                    warning_kind=None, last_reason=None,
                )
                return {"blocked": False, "warning": None, "progress": True,
                        "count": 0, "allowed_next_action": self._allowed_next_action_locked()}

            count = current.consecutive_no_progress_rounds + 1
            kind = self._stagnation_kind_locked(
                repeated_round, tuple(tool_names), bool(observation_hashes),
            )
            short_fingerprint = self._short_hash(current_fingerprint)
            reason = f"{kind}; fingerprint={short_fingerprint}; count={count}"
            if count >= MAX_STAGNANT_ROUNDS:
                self.status = "blocked"
                self.terminal_reason = reason
            warning = count == MAX_STAGNANT_ROUNDS - 1
            self.stagnation_state = replace(
                current, consecutive_no_progress_rounds=count,
                last_round_fingerprint=current_fingerprint,
                seen_observation_hashes=tuple(observation_seen),
                seen_effect_action_hashes=tuple(effect_seen),
                warning_kind=kind if warning else current.warning_kind,
                last_reason=reason,
            )
            if warning:
                self._append_trace_event_locked(
                    "stagnation_warning",
                    revision_id=self.planning_state.active_revision_id,
                    stagnation_kind=kind,
                    stagnation_count=count,
                    stagnation_fingerprint=current_fingerprint,
                )
            if count >= MAX_STAGNANT_ROUNDS:
                self._append_trace_event_locked(
                    "stagnation_blocked",
                    revision_id=self.planning_state.active_revision_id,
                    stagnation_kind=kind,
                    stagnation_count=count,
                    stagnation_fingerprint=current_fingerprint,
                )
            return {
                "blocked": count >= MAX_STAGNANT_ROUNDS,
                "warning": (
                    f"Runtime Notice：连续 {count} 个完整工具回合没有任务进展，"
                    f"类别={kind}。下一步必须是：{self._allowed_next_action_locked()}。"
                ) if warning else None,
                "progress": False, "count": count, "kind": kind,
                "terminal_reason": self.terminal_reason if count >= MAX_STAGNANT_ROUNDS else None,
                "allowed_next_action": self._allowed_next_action_locked(),
            }

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
                if name in ("recover", "request_replan"):
                    return None
                if name in ("begin_plan", "cancel_planning"):
                    return "工具调用拒绝: diagnosis_required 阶段只能只读诊断、recover 或 request_replan"
                if name == "commit_plan":
                    if self.planning_state.phase == "exploring":
                        return None
                    return "工具调用拒绝: diagnosis_required 阶段必须先通过 request_replan 转入 exploring"
                if name == "update_plan_progress":
                    return "工具调用拒绝: diagnosis_required 阶段不能推进旧计划步骤"
                if name == "run_shell" and arguments.get("purpose", "execution") == "verification":
                    return "工具调用拒绝: diagnosis_required 阶段必须先处理当前 failure，不能直接 verification"
                if effect_class == "possible":
                    return "工具调用拒绝: diagnosis_required 阶段只允许只读调查、计划推进或独占 recover"
            elif self._repair_phase == "verification_required":
                if name == "run_shell" and arguments.get("purpose", "execution") == "verification":
                    return None
                return "工具调用拒绝: verification_required 阶段下一工具回合只能是单个独立 verification"
            return None

    def _enter_diagnosis(self, failure_id: str) -> None:
        self._repair_phase = "diagnosis_required"
        self._active_failure_id = failure_id
        self._active_recovery_id = None

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
    def process_manager(self) -> Any:
        return self._process_manager

    @property
    def processes(self) -> list[ProcessRecord]:
        """Return a detached view of task-local process metadata."""
        with self._lock:
            return list(self.process_records)

    @property
    def process_lifecycle_events(self) -> list[ProcessEvent]:
        with self._lock:
            return list(self.process_events)

    def bind_process_manager(self, manager: Any) -> None:
        """Bind the runtime owner without placing operating-system handles in State."""
        with self._lock:
            self._process_manager = manager

    def ensure_task_id(self) -> str:
        """Allocate a task id for compatibility callers that skipped begin_task."""
        with self._lock:
            if not self.task_id:
                self.task_id = f"task-{self._next_task_id}"
                self._next_task_id += 1
            return self.task_id

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

    @staticmethod
    def _plan_text(value: Any, field_name: str, maximum: int) -> str:
        if not isinstance(value, str):
            raise PlanRejected(f"{field_name} 必须是字符串")
        value = value.strip()
        if not value:
            raise PlanRejected(f"{field_name} 不能为空")
        if len(value) > maximum:
            raise PlanRejected(f"{field_name} 不能超过 {maximum} 个字符")
        return value

    @classmethod
    def _plan_texts(cls, value: Any, field_name: str, maximum_items: int,
                    maximum_text: int) -> tuple[str, ...]:
        if not isinstance(value, list):
            raise PlanRejected(f"{field_name} 必须是数组")
        if len(value) > maximum_items:
            raise PlanRejected(f"{field_name} 不能超过 {maximum_items} 项")
        return tuple(cls._plan_text(item, f"{field_name}[{index}]", maximum_text)
                     for index, item in enumerate(value))

    @classmethod
    def _plan_ids(cls, value: Any, field_name: str) -> tuple[str, ...]:
        if not isinstance(value, list):
            raise PlanRejected(f"{field_name} 必须是数组")
        if len(value) > _PLAN_REFERENCE_LIMIT:
            raise PlanRejected(f"{field_name} 不能超过 {_PLAN_REFERENCE_LIMIT} 项")
        result: list[str] = []
        for index, item in enumerate(value):
            if not isinstance(item, str):
                raise PlanRejected(f"{field_name}[{index}] 必须是字符串")
            item = item.strip()
            if not _STEP_ID_PATTERN.fullmatch(item):
                raise PlanRejected(f"{field_name}[{index}] 不是合法 step_id")
            if item in result:
                raise PlanRejected(f"{field_name} 不允许重复 ID: {item}")
            result.append(item)
        return tuple(result)

    @classmethod
    def _parse_plan_steps(cls, value: Any) -> tuple[PlanStep, ...]:
        if not isinstance(value, list):
            raise PlanRejected("steps 必须是数组")
        if not 1 <= len(value) <= _PLAN_STEP_LIMIT:
            raise PlanRejected(f"steps 数量必须在 1-{_PLAN_STEP_LIMIT} 项之间")
        parsed: list[PlanStep] = []
        seen: set[str] = set()
        allowed = {"step_id", "content", "depends_on", "success_criteria", "replaces"}
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                raise PlanRejected(f"steps[{index}] 必须是对象")
            unknown = sorted(set(item) - allowed)
            if unknown:
                raise PlanRejected(f"steps[{index}] 包含未知字段: {', '.join(unknown)}")
            missing = sorted(allowed - set(item))
            if missing:
                raise PlanRejected(f"steps[{index}] 缺少字段: {', '.join(missing)}")
            step_id = item["step_id"]
            if not isinstance(step_id, str):
                raise PlanRejected(f"steps[{index}].step_id 必须是字符串")
            step_id = step_id.strip()
            if not _STEP_ID_PATTERN.fullmatch(step_id):
                raise PlanRejected(f"steps[{index}].step_id 不匹配 [A-Za-z][A-Za-z0-9_-]{{0,63}}")
            if step_id in seen:
                raise PlanRejected(f"step_id 重复: {step_id}")
            seen.add(step_id)
            step_success_criteria = cls._plan_texts(
                item["success_criteria"], f"steps[{index}].success_criteria",
                _PLAN_STEP_CRITERIA_LIMIT, _PLAN_TEXT_MAX,
            )
            if not step_success_criteria:
                raise PlanRejected(f"steps[{index}].success_criteria 至少需要 1 项")
            parsed.append(PlanStep(
                step_id=step_id,
                content=cls._plan_text(item["content"], f"steps[{index}].content", _PLAN_TEXT_MAX),
                status="pending",
                depends_on=cls._plan_ids(item["depends_on"], f"steps[{index}].depends_on"),
                success_criteria=step_success_criteria,
                replaces=cls._plan_ids(item["replaces"], f"steps[{index}].replaces"),
            ))
        ids = {step.step_id for step in parsed}
        for step in parsed:
            if step.step_id in step.depends_on:
                raise PlanRejected(f"步骤 {step.step_id} 不能依赖自身")
            unknown = [item for item in step.depends_on if item not in ids]
            if unknown:
                raise PlanRejected(f"步骤 {step.step_id} 依赖未知步骤: {', '.join(unknown)}")
        graph = {step.step_id: step.depends_on for step in parsed}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step_id: str) -> None:
            if step_id in visiting:
                raise PlanRejected("steps.depends_on 不能形成环")
            if step_id in visited:
                return
            visiting.add(step_id)
            for dependency in graph[step_id]:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)

        for step in parsed:
            visit(step.step_id)
        return tuple(parsed)

    @staticmethod
    def _plan_step_structure(step: PlanStep) -> tuple[Any, ...]:
        return (step.step_id, step.content, step.depends_on,
                step.success_criteria, step.replaces)

    @staticmethod
    def _plan_difference_view(difference: PlanDifference | None) -> dict[str, Any] | None:
        if difference is None:
            return None
        return {
            "retained": [asdict(item) for item in difference.retained],
            "added": list(difference.added),
            "cancelled": list(difference.cancelled),
            "replaced": list(difference.replaced),
            "goal_changed": difference.goal_changed,
            "constraints_changed": difference.constraints_changed,
            "success_criteria_changed": difference.success_criteria_changed,
        }

    def _active_revision_locked(self) -> PlanRevision | None:
        active_id = self.planning_state.active_revision_id
        if active_id is None:
            return None
        return next((revision for revision in self.plan_revisions
                      if revision.revision_id == active_id), None)

    def _plan_view_locked(self, revision_id: int | None = None) -> dict[str, Any] | None:
        revision = (
            next((item for item in self.plan_revisions if item.revision_id == revision_id), None)
            if revision_id is not None else self._active_revision_locked()
        )
        if revision is None:
            return None
        statuses = {step.step_id: step.status for step in revision.steps}
        for event in self.plan_progress_history:
            if event.revision_id == revision.revision_id and event.step_id in statuses:
                statuses[event.step_id] = event.to_status
        return {
            "revision_id": revision.revision_id,
            "generation_id": revision.generation_id,
            "parent_revision_id": revision.parent_revision_id,
            "trigger_id": revision.trigger_id,
            "goal": revision.goal,
            "constraints": list(revision.constraints),
            "success_criteria": list(revision.success_criteria),
            "steps": [
                {
                    "step_id": step.step_id,
                    "content": step.content,
                    "status": statuses[step.step_id],
                    "depends_on": list(step.depends_on),
                    "success_criteria": list(step.success_criteria),
                    "replaces": list(step.replaces),
                }
                for step in revision.steps
            ],
            "reason": revision.reason,
            "diff": self._plan_difference_view(revision.diff),
        }

    def _sync_current_goal_locked(self) -> None:
        active = self._plan_view_locked()
        object.__setattr__(self, "current_goal", next(
            (step["content"] for step in (active or {}).get("steps", [])
            if step["status"] == "in_progress"),
            "",
        ))

    def _plan_projection_locked(self) -> list[dict[str, str]]:
        active = self._plan_view_locked()
        return [
            {"content": step["content"], "status": step["status"]}
            for step in (active or {}).get("steps", [])
        ]

    @property
    def todos(self) -> list[dict[str, str]]:
        """Read-only Todo-shaped projection of the active Plan Contract."""
        with self._lock:
            return deepcopy(self._plan_projection_locked())

    def begin_plan(self) -> PlanningState:
        with self._lock:
            if (self.status != "running" or self.planning_state.phase != "direct"
                    or self.planning_state.active_revision_id is not None
                    or self._repair_phase != "idle"):
                raise PlanRejected("只有尚未提交计划的普通任务可以开始规划")
            previous_phase = self.planning_state.phase
            self.planning_state = replace(self.planning_state, phase="exploring")
            self._append_trace_event_locked(
                "begin_plan",
                planning_phase_before=previous_phase,
            )
            return self.planning_state

    def cancel_planning(self) -> PlanningState:
        with self._lock:
            if (self.planning_state.mode != "auto" or
                    self.status != "running" or
                    self.planning_state.phase != "exploring" or
                    self.planning_state.active_revision_id is not None or
                    self.planning_state.active_trigger_id is not None or
                    self._repair_phase != "idle"):
                raise PlanRejected("当前规划不能取消")
            previous_phase = self.planning_state.phase
            self.planning_state = replace(self.planning_state, phase="direct")
            self._append_trace_event_locked(
                "cancel_planning",
                planning_phase_before=previous_phase,
            )
            return self.planning_state

    def request_replan(self, kind: Any, source_id: Any, reason: Any) -> ReplanTrigger:
        """Create one active, source-backed replan trigger atomically."""
        with self._lock:
            if self.status != "running":
                raise PlanRejected("只有 running 任务可以请求重规划")
            if self.planning_state.phase not in ("direct", "executing"):
                raise PlanRejected("只有 direct 或 executing 阶段可以请求重规划")
            if self.planning_state.active_trigger_id is not None:
                raise PlanRejected("当前已有活动 replan trigger")
            if self._repair_phase == "verification_required":
                raise PlanRejected("verification_required 时不能请求重规划")
            if kind not in ("failure", "observation"):
                raise PlanRejected("模型只能请求 failure 或 observation 类型的重规划")
            if not isinstance(source_id, str) or not source_id.strip():
                raise PlanRejected("source_id 必须是非空字符串")
            source_id = source_id.strip()
            if len(source_id) > 120:
                raise PlanRejected("source_id 不能超过 120 个字符")
            clean_reason = self._plan_text(reason, "reason", _PLAN_REASON_MAX)
            failure = None
            attempt = None
            if kind == "failure":
                if self._repair_phase != "diagnosis_required":
                    raise PlanRejected("failure trigger 只能在 diagnosis_required 阶段请求")
                if source_id != self._active_failure_id:
                    raise PlanRejected("failure source_id 必须精确引用当前 active_failure_id")
                failure = next((item for item in self.failures if item.failure_id == source_id), None)
                if failure is None:
                    raise PlanRejected("当前 active failure 记录不存在")
            else:
                if self._repair_phase != "idle":
                    raise PlanRejected("observation trigger 只能在 idle repair 阶段请求")
                active_revision = self._active_revision_locked()
                if active_revision is None:
                    raise PlanRejected("observation trigger 需要已有 active revision")
                boundary = self._revision_attempt_boundaries.get(active_revision.revision_id)
                for candidate in self.attempts:
                    if candidate.attempt_id != source_id:
                        continue
                    attempt = candidate
                    break
                if attempt is None:
                    raise PlanRejected("observation source_id 必须引用当前任务中存在的 attempt")
                attempt_index = next(
                    (index for index, item in enumerate(self.attempts)
                     if item.attempt_id == attempt.attempt_id),
                    -1,
                )
                if boundary is None or attempt_index < boundary:
                    raise PlanRejected("observation 必须发生在当前 active revision 提交之后")
                if (attempt.outcome != "succeeded" or not attempt.handler_admitted
                        or attempt.permission != "allowed" or attempt.effect_class != "none"
                        or attempt.tool in {
                            "begin_plan", "cancel_planning", "commit_plan",
                            "update_plan_progress", "request_replan", "recover",
                            "rollback_checkpoint",
                        }
                        or (attempt.tool == "run_shell"
                            and attempt.redacted_arguments.get("purpose") == "verification")):
                    raise PlanRejected("observation 必须引用成功且获准的只读调查 attempt")

            if self.planning_state.replans_remaining <= 0:
                self.status = "blocked"
                self.terminal_reason = (
                    "replan_budget_exhausted; MAX_REPLAN_REVISIONS="
                    f"{MAX_REPLAN_REVISIONS}"
                )
                raise PlanRejected("重规划预算已耗尽，任务已阻塞")

            trigger = ReplanTrigger(
                trigger_id=self._next_plan_trigger,
                generation_id=self._verification_generation,
                kind=kind,
                reason=clean_reason,
                caused_by_failure_id=source_id if kind == "failure" else None,
                caused_by_attempt_id=source_id if kind == "observation" else None,
            )
            self.replan_triggers.append(trigger)
            self._next_plan_trigger += 1
            previous_phase = self.planning_state.phase
            previous_revision_id = self.planning_state.active_revision_id
            self.planning_state = replace(
                self.planning_state, phase="exploring",
                active_trigger_id=trigger.trigger_id,
                trigger_no_progress_commits=0,
            )
            self._append_trace_event_locked(
                "trigger_created",
                revision_id=previous_revision_id,
                record_type="replan_trigger",
                record_id=trigger.trigger_id,
                planning_phase_before=previous_phase,
            )
            return trigger

    def resume_blocked(self, feedback: Any) -> UserPlanDecision:
        """Resume a blocked task only through an explicit CLI decision."""
        with self._lock:
            if self.status != "blocked":
                if self.status == "failed":
                    raise PlanRejected("failed 任务不能恢复，请使用 /new <任务>")
                raise PlanRejected("只有 blocked 任务可以使用 /resume")
            if self.planning_state.active_trigger_id is not None:
                raise PlanRejected("当前 blocked 任务已有活动恢复 trigger，不能重复恢复，请使用 /new <任务>")
            if (self.planning_state.replans_remaining <= 0
                    or "预算已耗尽" in (self.terminal_reason or "")
                    or "budget_exhausted" in (self.terminal_reason or "")):
                raise PlanRejected("重规划预算已耗尽，请使用 /new <任务>")
            clean_feedback = self._plan_text(feedback, "feedback", _PLAN_REASON_MAX)
            previous_reason = self.terminal_reason or None
            active_failure = self._active_failure_id
            revision_id = self.planning_state.active_revision_id
            previous_planning_phase = self.planning_state.phase
            previous_repair_phase = self._repair_phase
            decision = UserPlanDecision(
                decision_id=self._next_plan_decision,
                revision_id=revision_id,
                decision="resume_blocked",
                feedback=clean_feedback,
                generation_id=self._verification_generation,
                previous_terminal_reason=previous_reason,
                caused_by_failure_id=active_failure,
            )
            self.user_plan_decisions.append(decision)
            self._next_plan_decision += 1
            trigger = ReplanTrigger(
                trigger_id=self._next_plan_trigger,
                generation_id=self._verification_generation,
                kind="blocked_resume",
                reason=clean_feedback,
                caused_by_decision_id=decision.decision_id,
            )
            self.replan_triggers.append(trigger)
            self._next_plan_trigger += 1
            self.status = "running"
            self.terminal_reason = ""
            self.planning_state = replace(
                self.planning_state, phase="exploring",
                active_trigger_id=trigger.trigger_id,
                trigger_no_progress_commits=0,
            )
            terminal_recovery = next(
                (item for item in self.recovery_actions
                 if item.recovery_id == self._active_recovery_id
                 and item.status == "terminal"
                 and item.action in ("ask", "block")),
                None,
            )
            if self._repair_phase == "verification_required" and terminal_recovery is not None:
                # ask/block already opened a successor generation and left a
                # verification obligation.  An explicit user resume permits
                # read-only diagnosis and a new revision, but must retain the
                # original failure, generation, repair budget, and obligation
                # to verify the revised path independently.
                self._repair_phase = "diagnosis_required"
                self._active_recovery_id = None
            current = self.stagnation_state
            self.stagnation_state = LoopStagnationState(
                current.progress_epoch + 1, 0, None, (), (), None, None,
            )
            self._stagnation_progress_marker = self._progress_marker_locked()
            self._append_trace_event_locked(
                "plan_decision",
                revision_id=revision_id,
                record_type="user_plan_decision",
                record_id=decision.decision_id,
                planning_phase_before=previous_planning_phase,
                repair_phase_before=previous_repair_phase,
            )
            self._append_trace_event_locked(
                "trigger_created",
                revision_id=revision_id,
                record_type="replan_trigger",
                record_id=trigger.trigger_id,
            )
            return decision

    def planning_gate(self, name: str, arguments: dict[str, Any],
                      effect_class: EffectClass) -> str | None:
        """Check the planning boundary before permission and handler admission."""
        with self._lock:
            phase = self.planning_state.phase
            if phase == "awaiting_approval":
                return "工具调用拒绝: 当前计划等待用户决定"
            if phase == "exploring":
                if name == "commit_plan":
                    return None
                if name == "request_replan":
                    return "工具调用拒绝: exploring 阶段不能重复请求 replan"
                if name == "cancel_planning" and self.planning_state.mode == "auto" and self.planning_state.active_revision_id is None and self.planning_state.active_trigger_id is None:
                    return None
                if name in ("begin_plan", "update_plan_progress", "cancel_planning", "recover"):
                    return "工具调用拒绝: exploring 阶段不能推进计划状态"
                if name == "run_shell" and arguments.get("purpose", "execution") == "verification":
                    return "工具调用拒绝: exploring 阶段不能进行 verification"
                if effect_class != "none":
                    return "工具调用拒绝: exploring 阶段只允许只读调查"
            elif name in ("begin_plan", "cancel_planning"):
                if name == "begin_plan" and phase == "direct":
                    return None
                return "工具调用拒绝: 当前阶段不能切换规划状态"
            return None

    def decide_plan(self, decision: str, revision_id: int,
                    feedback: str | None = None) -> UserPlanDecision:
        """Accept a CLI-only decision about the current plan revision."""
        with self._lock:
            if self.status != "running":
                raise PlanRejected("终态任务不能接受计划决定")
            if self.planning_state.mode != "plan_only":
                raise PlanRejected("只有 --plan 任务需要用户计划决定")
            if (isinstance(revision_id, bool) or not isinstance(revision_id, int)
                    or revision_id != self.planning_state.active_revision_id):
                raise PlanRejected("只能决定当前 revision")
            if decision not in ("approved", "rejected", "continue_exploring"):
                raise PlanRejected("未知计划决定")
            if self.planning_state.phase != "awaiting_approval":
                raise PlanRejected("当前没有待批准的计划")
            clean_feedback = None
            if decision != "approved":
                clean_feedback = self._plan_text(feedback, "feedback", _PLAN_REASON_MAX)
                if decision == "rejected" and self.planning_state.replans_remaining <= 0:
                    self.status = "blocked"
                    self.terminal_reason = (
                        "replan_budget_exhausted; MAX_REPLAN_REVISIONS="
                        f"{MAX_REPLAN_REVISIONS}"
                    )
                    raise PlanRejected("重规划预算已耗尽，请使用 /new <任务>")
            elif feedback is not None:
                raise PlanRejected("批准计划不接受反馈参数")
            record = UserPlanDecision(
                self._next_plan_decision, revision_id, decision,
                clean_feedback, self._verification_generation,
            )
            self.user_plan_decisions.append(record)
            self._next_plan_decision += 1
            previous_phase = self.planning_state.phase
            if decision == "approved":
                self.planning_state = replace(self.planning_state, phase="executing")
                self._append_trace_event_locked(
                    "plan_decision",
                    revision_id=revision_id,
                    record_type="user_plan_decision",
                    record_id=record.decision_id,
                    planning_phase_before=previous_phase,
                )
            else:
                trigger = ReplanTrigger(
                    trigger_id=self._next_plan_trigger,
                    generation_id=self._verification_generation,
                    kind="user_feedback",
                    reason=clean_feedback,
                    caused_by_decision_id=record.decision_id,
                )
                self.replan_triggers.append(trigger)
                self._next_plan_trigger += 1
                self.planning_state = replace(
                    self.planning_state, phase="exploring",
                    active_trigger_id=trigger.trigger_id,
                    trigger_no_progress_commits=0,
                )
                self._append_trace_event_locked(
                    "plan_decision",
                    revision_id=revision_id,
                    record_type="user_plan_decision",
                    record_id=record.decision_id,
                    planning_phase_before=previous_phase,
                )
                self._append_trace_event_locked(
                    "trigger_created",
                    revision_id=revision_id,
                    record_type="replan_trigger",
                    record_id=trigger.trigger_id,
                )
            return record

    def review_current_plan(self, revision_id: int) -> PlanningState:
        """Return an unchanged plan to review after continue_exploring."""
        with self._lock:
            if (self.planning_state.mode != "plan_only" or
                    self.status != "running" or self._repair_phase != "idle" or
                    self.planning_state.phase != "exploring" or
                    isinstance(revision_id, bool) or
                    revision_id != self.planning_state.active_revision_id or
                    not self.user_plan_decisions or
                    self.user_plan_decisions[-1].decision != "continue_exploring" or
                    self.user_plan_decisions[-1].revision_id != revision_id):
                raise PlanRejected("只有继续调查的当前 revision 可以重新交付审批")
            previous_phase = self.planning_state.phase
            trigger_id = self.planning_state.active_trigger_id
            for index, trigger in enumerate(self.replan_triggers):
                if trigger.trigger_id == trigger_id and trigger.status == "active":
                    self.replan_triggers[index] = replace(trigger, status="rejected")
                    break
            else:
                raise PlanRejected("当前继续调查的触发记录无效")
            self.planning_state = replace(
                self.planning_state, phase="awaiting_approval", active_trigger_id=None,
            )
            self._append_trace_event_locked(
                "trigger_resolved",
                revision_id=revision_id,
                record_type="replan_trigger",
                record_id=trigger_id,
            )
            self._append_trace_event_locked(
                "plan_review",
                revision_id=revision_id,
                planning_phase_before=previous_phase,
            )
            return self.planning_state

    def commit_plan(self, goal: Any, constraints: Any, success_criteria: Any,
                    steps: Any, reason: Any,
                    parent_revision_id: Any = _MISSING,
                    trigger_id: Any = _MISSING) -> PlanRevision:
        """Validate and atomically append one immutable Plan Contract revision."""
        with self._lock:
            previous_planning_phase = self.planning_state.phase
            previous_repair_phase = self._repair_phase
            if self.status != "running":
                raise PlanRejected("终态任务不能提交计划")
            if self.planning_state.phase == "awaiting_approval":
                raise PlanRejected("计划等待用户决定，不能再次提交")
            if self._repair_phase == "verification_required":
                raise PlanRejected("当前必须先完成独立 verification")
            if (self.planning_state.mode == "plan_only" and
                    self.planning_state.phase == "executing" and
                    self.planning_state.active_revision_id is not None):
                raise PlanRejected("plan_only 执行阶段不能直接改写已批准计划")
            clean_goal = self._plan_text(goal, "goal", _PLAN_GOAL_MAX)
            clean_constraints = self._plan_texts(
                constraints, "constraints", _PLAN_CONSTRAINT_LIMIT, _PLAN_TEXT_MAX,
            )
            clean_success = self._plan_texts(
                success_criteria, "success_criteria", _PLAN_TASK_CRITERIA_LIMIT, _PLAN_TEXT_MAX,
            )
            if not clean_success:
                raise PlanRejected("success_criteria 至少需要 1 项")
            clean_reason = self._plan_text(reason, "reason", _PLAN_REASON_MAX)
            parsed_steps = self._parse_plan_steps(steps)
            active = self._active_revision_locked()
            if self.planning_state.mode == "plan_only" and active is None and not any(
                attempt.outcome == "succeeded"
                and attempt.handler_admitted
                and attempt.permission == "allowed"
                and attempt.effect_class == "none"
                and attempt.tool not in {
                    "begin_plan", "cancel_planning", "commit_plan",
                    "update_plan_progress", "request_replan", "recover",
                    "rollback_checkpoint", "run_shell",
                }
                for attempt in self.attempts
            ):
                raise PlanRejected("--plan 首次提交前必须完成一次获准的只读调查")
            active_trigger = self.planning_state.active_trigger_id
            if active_trigger is None:
                if trigger_id is not _MISSING:
                    raise PlanRejected("当前没有需要引用的 replan trigger")
                resolved_trigger = None
            else:
                if (isinstance(trigger_id, bool) or not isinstance(trigger_id, int)
                        or trigger_id != active_trigger):
                    raise PlanRejected("新计划必须引用当前活动 trigger_id")
                resolved_trigger = next((item for item in self.replan_triggers
                                         if item.trigger_id == trigger_id and item.status == "active"), None)
                if resolved_trigger is None:
                    raise PlanRejected("当前 trigger 已失效")
                if active is None and resolved_trigger.kind not in ("failure", "blocked_resume"):
                    raise PlanRejected("该 trigger 必须引用当前 active revision")
            if active is not None and resolved_trigger is None:
                raise PlanRejected("后续 revision 必须引用当前活动 trigger")
            parent_provided = parent_revision_id is not _MISSING
            if active is None:
                if parent_provided:
                    raise PlanRejected("初次提交不得提供 parent_revision_id")
                parent = None
            else:
                if not parent_provided:
                    raise PlanRejected("后续提交必须提供当前 active_revision_id")
                if isinstance(parent_revision_id, bool) or not isinstance(parent_revision_id, int):
                    raise PlanRejected("parent_revision_id 必须是整数")
                if parent_revision_id != active.revision_id:
                    raise PlanRejected("parent_revision_id 不是当前 active revision，不能分叉或引用旧 revision")
                parent = active

            current_ids = {step.step_id for step in parsed_steps}
            historical: dict[str, tuple[str, tuple[str, ...]]] = {}
            for revision in self.plan_revisions:
                for step in revision.steps:
                    definition = (step.content, step.success_criteria)
                    old = historical.get(step.step_id)
                    if old is not None and old != definition:
                        raise PlanRejected(f"step_id {step.step_id} 的 content 或 success_criteria 不能改变")
                    historical[step.step_id] = definition
            if parent is None:
                if any(step.replaces for step in parsed_steps):
                    raise PlanRejected("初始 revision 不允许使用 replaces")
                inherited = {}
            else:
                parent_view = self._plan_view_locked(parent.revision_id) or {}
                inherited = {
                    step["step_id"]: step["status"]
                    for step in parent_view.get("steps", [])
                }
                for step in parsed_steps:
                    old_definition = historical.get(step.step_id)
                    if old_definition is not None:
                        if step.step_id not in inherited:
                            raise PlanRejected(f"已经移除的 step_id 不得重新启用: {step.step_id}")
                        if old_definition != (step.content, step.success_criteria):
                            raise PlanRejected(f"step_id {step.step_id} 的 content 或 success_criteria 不能改变")
                parent_ids = set(inherited)
                replaced_ids: set[str] = set()
                for step in parsed_steps:
                    for replaced in step.replaces:
                        if replaced not in parent_ids:
                            raise PlanRejected(f"replaces 只能引用 parent revision 中存在的步骤: {replaced}")
                        if replaced in current_ids:
                            raise PlanRejected(f"replaces 的步骤必须从新 revision 移除: {replaced}")
                        if replaced in replaced_ids:
                            raise PlanRejected(f"replaces 不允许重复引用: {replaced}")
                        replaced_ids.add(replaced)
                inherited = {step_id: status for step_id, status in inherited.items()
                             if step_id in current_ids}
            if parent is not None:
                old_structure = (
                    parent.goal, parent.constraints, parent.success_criteria,
                    tuple(self._plan_step_structure(step) for step in parent.steps),
                )
                new_structure = (
                    clean_goal, clean_constraints, clean_success,
                    tuple(self._plan_step_structure(step) for step in parsed_steps),
                )
                if old_structure == new_structure:
                    if resolved_trigger is not None:
                        count = self.planning_state.trigger_no_progress_commits + 1
                        self.planning_state = replace(
                            self.planning_state,
                            trigger_no_progress_commits=count,
                        )
                        if count >= MAX_NO_PROGRESS_REPLANS:
                            self.status = "blocked"
                            self.terminal_reason = (
                                "replan_no_progress; "
                                f"trigger={resolved_trigger.trigger_id}; commits={count}"
                            )
                            raise PlanRejected(
                                "同一 trigger 的计划结构连续无变化提交达到上限，任务已阻塞"
                            )
                        raise PlanRejected(
                            "计划结构没有变化；纯状态变化请使用 update_plan_progress；"
                            f"同一 trigger 无进展提交 {count}/{MAX_NO_PROGRESS_REPLANS}"
                        )
                    raise PlanRejected("计划结构没有变化；纯状态变化请使用 update_plan_progress")

            if resolved_trigger is not None and self.planning_state.replans_remaining <= 0:
                self.status = "blocked"
                self.terminal_reason = (
                    "replan_budget_exhausted; MAX_REPLAN_REVISIONS="
                    f"{MAX_REPLAN_REVISIONS}"
                )
                raise PlanRejected("重规划预算已耗尽，任务已阻塞")

            final_steps = tuple(
                PlanStep(
                    step.step_id, step.content, inherited.get(step.step_id, "pending"),
                    step.depends_on, step.success_criteria, step.replaces,
                )
                for step in parsed_steps
            )
            final_status = {step.step_id: step.status for step in final_steps}
            for step in final_steps:
                if step.status in ("in_progress", "completed"):
                    not_completed = [dependency for dependency in step.depends_on
                                     if final_status.get(dependency) != "completed"]
                    if not_completed:
                        raise PlanRejected(
                            f"已继承为 {step.status} 的步骤 {step.step_id} 依赖未完成: {', '.join(not_completed)}"
                        )
            difference = None
            if parent is not None:
                parent_steps = {step.step_id: step for step in parent.steps}
                new_steps = {step.step_id: step for step in final_steps}
                replaced = tuple(
                    replaced_id for step in final_steps for replaced_id in step.replaces
                )
                difference = PlanDifference(
                    retained=tuple(
                        PlanStepDifference(
                            step_id,
                            parent_steps[step_id].depends_on != new_steps[step_id].depends_on,
                        )
                        for step_id in parent_steps
                        if step_id in new_steps
                    ),
                    added=tuple(step_id for step_id in new_steps if step_id not in parent_steps),
                    cancelled=tuple(
                        step_id for step_id in parent_steps
                        if step_id not in new_steps and step_id not in replaced
                    ),
                    replaced=replaced,
                    goal_changed=parent.goal != clean_goal,
                    constraints_changed=parent.constraints != clean_constraints,
                    success_criteria_changed=parent.success_criteria != clean_success,
                )
            revision = PlanRevision(
                self._next_plan_revision, self._verification_generation,
                parent.revision_id if parent is not None else None, active_trigger,
                clean_goal, clean_constraints, clean_success, final_steps, clean_reason,
                difference,
            )
            self.plan_revisions.append(revision)
            self._next_plan_revision += 1
            self._revision_attempt_boundaries[revision.revision_id] = len(self.attempts)
            self._append_trace_event_locked(
                "plan_committed",
                generation_id=revision.generation_id,
                revision_id=revision.revision_id,
                record_type="plan_revision",
                record_id=revision.revision_id,
                planning_phase_before=previous_planning_phase,
                planning_phase_after=(
                    "awaiting_approval"
                    if self.planning_state.mode == "plan_only" else "executing"
                ),
                repair_phase_before=previous_repair_phase,
                repair_phase_after="idle" if resolved_trigger is not None else previous_repair_phase,
            )
            if resolved_trigger is not None:
                index = self.replan_triggers.index(resolved_trigger)
                self.replan_triggers[index] = replace(
                    resolved_trigger, status="resolved", result_revision_id=revision.revision_id,
                )
                self._append_trace_event_locked(
                    "trigger_resolved",
                    generation_id=revision.generation_id,
                    revision_id=revision.revision_id,
                    record_type="replan_trigger",
                    record_id=resolved_trigger.trigger_id,
                )
            replans_used = self.planning_state.replans_used + (1 if resolved_trigger is not None else 0)
            self.planning_state = PlanningState(
                mode=self.planning_state.mode,
                phase="awaiting_approval" if self.planning_state.mode == "plan_only" else "executing",
                active_revision_id=revision.revision_id,
                active_trigger_id=None, replans_used=replans_used,
                replans_remaining=max(0, MAX_REPLAN_REVISIONS - replans_used),
                trigger_no_progress_commits=0,
            )
            if resolved_trigger is not None and self._repair_phase == "diagnosis_required":
                # Replanning resolves diagnosis into a new executable proposal;
                # it does not claim the original failure was fixed and therefore
                # deliberately preserves verification_required.
                self._repair_phase = "idle"
                self._active_failure_id = None
                self._active_recovery_id = None
                self.recovery_notice = ""
            self._sync_current_goal_locked()
            return revision

    def update_plan_progress(self, revision_id: Any, step_id: Any, status: Any,
                             reason: Any) -> PlanProgressEvent:
        """Append one legal status transition for the active revision."""
        with self._lock:
            if self.planning_state.phase != "executing":
                raise PlanRejected("只有 executing 阶段可以推进计划步骤")
            if isinstance(revision_id, bool) or not isinstance(revision_id, int):
                raise PlanRejected("revision_id 必须是整数")
            if not isinstance(step_id, str) or not _STEP_ID_PATTERN.fullmatch(step_id.strip()):
                raise PlanRejected("step_id 不是合法 ID")
            step_id = step_id.strip()
            if status not in ("pending", "in_progress", "completed") or not isinstance(status, str):
                raise PlanRejected("status 必须是 pending、in_progress 或 completed")
            clean_reason = self._plan_text(reason, "reason", _PLAN_REASON_MAX)
            active = self._active_revision_locked()
            if active is None or revision_id != active.revision_id:
                raise PlanRejected("只能更新当前 active revision")
            view = self._plan_view_locked(active.revision_id) or {}
            steps = {step["step_id"]: step for step in view.get("steps", [])}
            step = steps.get(step_id)
            if step is None:
                raise PlanRejected(f"active revision 中不存在步骤: {step_id}")
            from_status = step["status"]
            if from_status == "completed":
                raise PlanRejected("completed 步骤不能再次更新")
            if (from_status, status) not in (("pending", "in_progress"), ("in_progress", "completed")):
                raise PlanRejected(f"不允许的状态转换: {from_status} -> {status}")
            if from_status == "pending":
                dependencies = [dependency for dependency in step["depends_on"]
                                if steps[dependency]["status"] != "completed"]
                if dependencies:
                    raise PlanRejected(
                        f"步骤 {step_id} 的依赖尚未完成: {', '.join(dependencies)}"
                    )
                if any(item["status"] == "in_progress" for item in steps.values()):
                    raise PlanRejected("同一时间只能有一个 in_progress 步骤")
            event = PlanProgressEvent(
                self._next_plan_progress, active.revision_id, self._verification_generation,
                step_id, from_status, status, clean_reason,
            )
            self.plan_progress_history.append(event)
            self._next_plan_progress += 1
            self._append_trace_event_locked(
                "plan_progress",
                generation_id=event.generation_id,
                revision_id=event.revision_id,
                record_type="plan_progress",
                record_id=event.progress_id,
            )
            self._sync_current_goal_locked()
            return event

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

    def _register_process_start_locked(self, result: Any, attempt: ExecutionAttempt) -> None:
        """Register a successful start beside its already committed attempt."""
        metadata = getattr(result, "process_metadata", None)
        if not isinstance(metadata, dict):
            return
        process_id = metadata.get("process_id")
        task_id = metadata.get("task_id") or self.task_id
        pid = metadata.get("pid")
        if (not isinstance(process_id, str) or not process_id
                or not isinstance(task_id, str) or task_id != self.task_id
                or isinstance(pid, bool) or not isinstance(pid, int)):
            return
        if any(item.process_id == process_id for item in self.process_records):
            return
        started_at = str(metadata.get("started_at") or "")
        record = ProcessRecord(
            process_id, task_id, attempt.attempt_id, attempt.generation_id,
            str(metadata.get("command") or "")[:240],
            str(metadata.get("cwd") or "")[:400], pid, "running", started_at,
        )
        event_id = f"pe-{self._next_process_event}"
        self._next_process_event += 1
        event = ProcessEvent(
            event_id, process_id, task_id, "started", attempt.generation_id,
            attempt.attempt_id, 0, 0,
        )
        self.process_records.append(record)
        self.process_events.append(event)
        self._append_trace_event_locked(
            "process_event",
            generation_id=attempt.generation_id,
            revision_id=self.planning_state.active_revision_id,
            record_type="process_event",
            record_id=event_id,
        )

    def sync_processes(self, facts: Any) -> list[ProcessEvent]:
        """Commit manager observations at one foreground synchronization point."""
        committed: list[ProcessEvent] = []
        with self._lock:
            if not isinstance(facts, (list, tuple)):
                return committed
            for fact in facts:
                process_id = getattr(fact, "process_id", None)
                if not isinstance(process_id, str):
                    continue
                index = next(
                    (i for i, item in enumerate(self.process_records)
                     if item.process_id == process_id and item.task_id == self.task_id),
                    None,
                )
                if index is None:
                    # A start can finish before its attempt is committed.  It
                    # becomes visible only after record_execution_result adds
                    # the ProcessRecord and started event atomically.
                    continue
                record = self.process_records[index]
                stdout_offset = max(record.stdout_offset, int(getattr(fact, "stdout_offset", 0) or 0))
                stderr_offset = max(record.stderr_offset, int(getattr(fact, "stderr_offset", 0) or 0))
                if record.terminal_event_id is not None:
                    if (stdout_offset != record.stdout_offset or stderr_offset != record.stderr_offset):
                        self.process_records[index] = replace(
                            record, stdout_offset=stdout_offset, stderr_offset=stderr_offset,
                        )
                    continue
                status = str(getattr(fact, "status", "running"))
                if status not in ("running", "exited", "failed"):
                    status = "running"
                if not bool(getattr(fact, "newly_exited", False)):
                    self.process_records[index] = replace(
                        record, stdout_offset=stdout_offset, stderr_offset=stderr_offset,
                    )
                    continue
                event_id = f"pe-{self._next_process_event}"
                self._next_process_event += 1
                event_kind: ProcessEventKind = "exited" if status == "exited" else "failed"
                generation_id = self._verification_generation
                event = ProcessEvent(
                    event_id, process_id, self.task_id, event_kind, generation_id,
                    record.start_attempt_id, stdout_offset, stderr_offset,
                    getattr(fact, "exit_code", None), reason="natural_exit",
                )
                self.process_events.append(event)
                self.process_records[index] = replace(
                    record, status=status, ended_at=getattr(fact, "ended_at", None),
                    exit_code=getattr(fact, "exit_code", None),
                    stdout_offset=stdout_offset, stderr_offset=stderr_offset,
                    terminal_event_id=event_id,
                )
                self._append_trace_event_locked(
                    "process_event", generation_id=generation_id,
                    revision_id=self.planning_state.active_revision_id,
                    record_type="process_event", record_id=event_id,
                )
                active_failure = self._active_failure_id
                strict_verification = self._repair_phase == "verification_required"
                self._verification_generation += 1
                self.generations.append(ExecutionGeneration(
                    self._verification_generation,
                    opened_by_process_event_id=event_id,
                    open_reason="process_exit",
                ))
                self.verification_evidence.clear()
                self._last_verified_generation = -1
                self._verification_required = True
                if active_failure or strict_verification:
                    # Preserve existing repair facts; the process event is a
                    # second cause and must not overwrite the active failure.
                    if self.status not in ("blocked", "failed"):
                        self.status = "blocked"
                        self.terminal_reason = (
                            "process_exit_conflict: "
                            f"process_id={process_id}; event={event_id}; "
                            f"active_failure={active_failure or '-'}; "
                            f"strict_verification={str(strict_verification).lower()}"
                        )
                if self._process_manager is not None and hasattr(self._process_manager, "acknowledge_exit"):
                    self._process_manager.acknowledge_exit(process_id)
                committed.append(event)
        return committed

    def active_process_records(self) -> list[ProcessRecord]:
        with self._lock:
            return [item for item in self.process_records if item.status == "running"]

    def enter_awaiting_process(self, reason: str = "still_running") -> ProcessWaitState | None:
        with self._lock:
            active = [item for item in self.process_records if item.status == "running"]
            if not active or self.status in ("blocked", "failed"):
                return None
            wait = ProcessWaitState(
                tuple(item.process_id for item in active),
                reason if reason in ("no_new_output", "still_running") else "still_running",
                tuple(item.terminal_event_id or "" for item in active),
                tuple(item.stdout_offset for item in active),
                tuple(item.stderr_offset for item in active),
            )
            self.awaiting_process = wait
            self.status = "awaiting_process"
            return wait

    def resume_process_wait(self) -> None:
        with self._lock:
            if self.status == "awaiting_process":
                self.status = "running"
            self.awaiting_process = None

    def record_process_cleanup(self, report: Any) -> None:
        """Commit cleanup confirmations while the old task is still retained."""
        with self._lock:
            for item in getattr(report, "items", ()):
                process_id = getattr(item, "process_id", None)
                index = next((i for i, record in enumerate(self.process_records)
                              if record.process_id == process_id), None)
                if index is None:
                    continue
                record = self.process_records[index]
                if getattr(item, "complete", False):
                    if record.terminal_event_id is not None:
                        continue
                    event_id = f"pe-{self._next_process_event}"
                    self._next_process_event += 1
                    kind: ProcessEventKind = "killed" if getattr(item, "killed", False) else "terminated"
                    event = ProcessEvent(
                        event_id, record.process_id, record.task_id, kind,
                        self._verification_generation, record.start_attempt_id,
                        max(record.stdout_offset, int(getattr(item, "stdout_offset", 0) or 0)),
                        max(record.stderr_offset, int(getattr(item, "stderr_offset", 0) or 0)),
                        getattr(item, "exit_code", None),
                        reason=str(getattr(item, "reason", "cleanup")),
                    )
                    self.process_events.append(event)
                    self._append_trace_event_locked(
                        "process_event",
                        generation_id=self._verification_generation,
                        revision_id=self.planning_state.active_revision_id,
                        record_type="process_event",
                        record_id=event_id,
                    )
                    self.process_records[index] = replace(
                        record, status="terminated",
                        ended_at=getattr(item, "ended_at", None) or record.ended_at or "cleanup",
                        exit_code=getattr(item, "exit_code", None),
                        stdout_offset=event.stdout_offset,
                        stderr_offset=event.stderr_offset,
                        terminal_event_id=event_id,
                    )
                else:
                    event_id = f"pe-{self._next_process_event}"
                    self._next_process_event += 1
                    cleanup_event = ProcessEvent(
                        event_id, record.process_id, record.task_id, "cleanup_failed",
                        self._verification_generation, record.start_attempt_id,
                        max(record.stdout_offset, int(getattr(item, "stdout_offset", 0) or 0)),
                        max(record.stderr_offset, int(getattr(item, "stderr_offset", 0) or 0)),
                        getattr(item, "exit_code", None),
                        reason=str(getattr(item, "reason", "cleanup incomplete")),
                    )
                    self.process_events.append(cleanup_event)
                    self._append_trace_event_locked(
                        "process_event",
                        generation_id=self._verification_generation,
                        revision_id=self.planning_state.active_revision_id,
                        record_type="process_event",
                        record_id=event_id,
                    )
            incomplete = getattr(report, "incomplete", ())
            if incomplete:
                self.terminal_reason = (
                    "process_cleanup_incomplete: "
                    + "; ".join(
                        f"pid={getattr(item, 'pid', '?')}, process_id={getattr(item, 'process_id', '?')}: "
                        f"{getattr(item, 'reason', 'unknown')}"
                        for item in incomplete
                    )
                )

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
                if source_attempt.tool in (
                    "begin_plan", "cancel_planning", "recover", "rollback_checkpoint",
                    "commit_plan", "update_plan_progress",
                    "request_replan",
                ):
                    return None, "control/plan 工具不能作为恢复目标"
                target = (
                    source_attempt.tool,
                    deepcopy(self._original_attempt_arguments.get(source_attempt.attempt_id, {})),
                )
            elif action == "adjust":
                if requested_attempt is not None:
                    return None, "adjust 不接受 requested_attempt"
                if not isinstance(requested_tool, str) or not isinstance(requested_arguments, dict):
                    return None, "adjust 需要目标工具和参数"
                if requested_tool in (
                    "begin_plan", "cancel_planning", "recover", "rollback_checkpoint",
                    "commit_plan", "update_plan_progress",
                    "request_replan",
                ):
                    return None, "control/plan 工具不能作为恢复目标"
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
                if source_attempt.tool in ("begin_plan", "cancel_planning", "recover",
                                           "rollback_checkpoint", "commit_plan",
                                           "update_plan_progress", "request_replan"):
                    return self._reject_recovery(action, caused_by_failure_id, reason,
                                                 "internal/recover 工具不能作为恢复目标") + (None,)
                requested_tool = source_attempt.tool
                requested_arguments = deepcopy(self._original_attempt_arguments.get(source_attempt.attempt_id, {}))
            elif action == "adjust":
                if requested_attempt is not None:
                    return self._reject_recovery(action, caused_by_failure_id, reason, "adjust 不接受 requested_attempt") + (None,)
                if not isinstance(requested_tool, str) or not isinstance(requested_arguments, dict):
                    return self._reject_recovery(action, caused_by_failure_id, reason, "adjust 需要目标工具和参数") + (None,)
                if requested_tool in ("begin_plan", "cancel_planning", "recover",
                                      "rollback_checkpoint", "commit_plan",
                                      "update_plan_progress", "request_replan"):
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
            self._append_trace_event_locked(
                "recovery_proposed",
                generation_id=action_record.generation_id,
                revision_id=self.planning_state.active_revision_id,
                record_type="recovery_action",
                record_id=action_record.recovery_id,
            )
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
        self._append_trace_event_locked(
            "recovery_rejected",
            generation_id=rejected.generation_id,
            revision_id=self.planning_state.active_revision_id,
            record_type="recovery_action",
            record_id=rejected.recovery_id,
        )
        self.recovery_notice = f"Recovery {current.recovery_id} rejected: {detail}."
        if len(self.recovery_actions) >= MAX_RECOVERY_ACTIONS:
            self._terminal("blocked", "恢复动作预算已耗尽", current.caused_by_failure_id)
        return rejected

    def _activate_recovery(self, action_record, requested_arguments):
        rid = action_record.recovery_id
        action = action_record.action
        caused_by_failure_id = action_record.caused_by_failure_id
        previous_repair_phase = self._repair_phase
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
        self.generations.append(ExecutionGeneration(
            gid, opened_by_recovery_id=rid, open_reason="recovery"
        ))
        self._active_failure_id = caused_by_failure_id
        self._enter_verification(rid)
        self._append_trace_event_locked(
            "recovery_activated",
            generation_id=gid,
            revision_id=self.planning_state.active_revision_id,
            record_type="recovery_action",
            record_id=rid,
            repair_phase_before=previous_repair_phase,
            repair_phase_after=self._repair_phase,
        )
        self.recovery_notice = f"Recovery {rid} reserved: {action}; verify generation {gid} independently."
        if action in ("ask", "block"):
            self.status = "blocked"
            self.terminal_reason = ("等待外部条件" if action == "ask" else "按恢复策略保守停止") + f"; last_failure={caused_by_failure_id}"
            self.recovery_actions[index] = RecoveryAction(**{**asdict(action_record), "status": "terminal"})
            return self.recovery_actions[index], None, requested_arguments
        ar = AttemptReservation(
            f"a-{self._next_attempt}", gid - 1, gid,
            caused_by_failure_id, None, rid,
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
        self._append_trace_event_locked(
            "recovery_rejected",
            record_type="recovery_action",
            record_id=rid,
            revision_id=self.planning_state.active_revision_id,
        )
        self.recovery_notice = f"Recovery {rid} rejected: {str(detail)[:500]}."
        if len(self.recovery_actions) >= MAX_RECOVERY_ACTIONS:
            self._terminal("blocked", "恢复动作预算已耗尽", failure_id)
        return rec, detail

    def record_execution_result(self, result: Any) -> ExecutionAttempt | None:
        """Commit an ExecutionResult and derive attempt/failure/verification facts."""
        if (getattr(result, "error_kind", None) in (
                "plan_rejected", "planning_phase_gate", "task_terminal",
            )
                or getattr(result, "tool", None) in (
                    "begin_plan", "cancel_planning", "commit_plan",
                    "update_plan_progress", "request_replan",
                )):
            return None
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
                self._append_trace_event_locked(
                    "execution_result",
                    generation_id=generation_id,
                    revision_id=self.planning_state.active_revision_id,
                    record_type="attempt",
                    record_id=attempt_id,
                )
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
            attempt_revision_id = self.planning_state.active_revision_id
            self._append_trace_event_locked(
                "execution_result",
                generation_id=generation_id,
                revision_id=attempt_revision_id,
                record_type="attempt",
                record_id=attempt_id,
            )
            if result.tool == "start_process" and result.outcome == "succeeded":
                self._register_process_start_locked(result, attempt)
            if getattr(reservation, "recovery_id", None):
                rid = reservation.recovery_id
                for i, action in enumerate(self.recovery_actions):
                    if action.recovery_id == rid:
                        self.recovery_actions[i] = RecoveryAction(**{**asdict(action), "status": "executed", "result_attempt": attempt_id, "result_generation_id": generation_id})
                        self._append_trace_event_locked(
                            "recovery_result",
                            generation_id=generation_id,
                            revision_id=attempt_revision_id,
                            record_type="recovery_action",
                            record_id=rid,
                        )
                        break
            if result.tool not in ("commit_plan", "update_plan_progress"):
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
                evidence = VerificationEvidence(
                    str(args.get("command", "")), "passed" if passed else "failed",
                    result.exit_code, result.output_excerpt, generation_id, attempt_id)
                self.verification_evidence.append(evidence)
                self.verification_history.append(evidence)
                verification_history_index = len(self.verification_history) - 1
                verification_repair_phase = self._repair_phase
                self._last_verified_generation = generation_id if passed else -1
                self._verification_required = not passed
            else:
                verification_history_index = None
                verification_repair_phase = None
            if failure_id and category:
                affected = (path,) if isinstance(path, str) and result.effect_class == "possible" else ()
                self.failures.append(FailureEvent(failure_id, generation_id, phase, category,
                                                  retryable, attempt_id, affected))
                if verification_history_index is not None:
                    self._append_trace_event_locked(
                        "verification_recorded",
                        generation_id=generation_id,
                        revision_id=attempt_revision_id,
                        record_type="verification_history",
                        record_id=verification_history_index,
                    )
                previous_repair_phase = self._repair_phase
                self._enter_diagnosis(failure_id)
                self._append_trace_event_locked(
                    "failure_recorded",
                    generation_id=generation_id,
                    revision_id=attempt_revision_id,
                    record_type="failure",
                    record_id=failure_id,
                    repair_phase_before=previous_repair_phase,
                    repair_phase_after=self._repair_phase,
                )
                self.errors.append(f"{result.tool}: {result.output_excerpt}")
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
                    self._append_trace_event_locked(
                        "verification_recorded",
                        generation_id=generation_id,
                        revision_id=attempt_revision_id,
                        record_type="verification_history",
                        record_id=verification_history_index,
                        repair_phase_before=verification_repair_phase,
                        repair_phase_after=self._repair_phase,
                    )
            return attempt

    def _terminal(self, status: str, reason: str, failure_id: str) -> None:
        if self.status in ("blocked", "failed"):
            return
        self.status = status
        self.terminal_reason = f"{reason}; last_failure={failure_id}"

    def record_tool(self, name: str, args: dict[str, Any], ok: bool, brief: str) -> None:
        """Compatibility API for older callback-based integrations."""
        if name in ("begin_plan", "cancel_planning", "commit_plan", "update_plan_progress",
                    "request_replan"): return
        args_copy = deepcopy(args)
        with self._lock:
            self.tool_history.append({"tool": name, "args": args_copy, "ok": ok, "brief": brief})
            self._append_trace_event_locked(
                "execution_result",
                revision_id=self.planning_state.active_revision_id,
                record_type="tool_history",
                record_id=len(self.tool_history) - 1,
            )
            if ok and name in ("write_file", "edit_file"):
                path = args_copy.get("path")
                if isinstance(path, str) and path not in self.files_changed: self.files_changed.append(path)
                self._invalidate_verification()
            if name == "run_shell" and "权限拒绝" not in brief:
                self._invalidate_verification()
            if name == "run_shell" and args_copy.get("purpose", "execution") == "verification":
                timeout = "[timeout]" in str(brief) or "超时" in str(brief)
                match = re.search(r"\[exit=(-?\d+)\]", str(brief))
                code = int(match.group(1)) if match else (0 if ok and not timeout else None)
                passed = bool(ok and not timeout and code == 0)
                evidence = VerificationEvidence(
                    str(args_copy.get("command", "")), "passed" if passed else "failed",
                    code, str(brief), self._verification_generation)
                self.verification_evidence.append(evidence)
                self.verification_history.append(evidence)
                verification_history_index = len(self.verification_history) - 1
                previous_repair_phase = self._repair_phase
                self._last_verified_generation = self._verification_generation if passed else -1
                if passed:
                    self._clear_repair()
                    self._append_trace_event_locked(
                        "verification_recorded",
                        revision_id=self.planning_state.active_revision_id,
                        record_type="verification_history",
                        record_id=verification_history_index,
                        repair_phase_before=previous_repair_phase,
                        repair_phase_after=self._repair_phase,
                    )
                else:
                    self._append_trace_event_locked(
                        "verification_recorded",
                        revision_id=self.planning_state.active_revision_id,
                        record_type="verification_history",
                        record_id=verification_history_index,
                    )
                    # Legacy callback integrations do not create FailureEvent
                    # records, so they cannot participate in the structured
                    # Repair Loop. Retain the historical completion signal.
                    self._verification_required = True
            if not ok: self.errors.append(f"{name}: {brief}")

    def begin_task(self, task: str, mode: Literal["auto", "plan_only"] = "auto") -> None:
        if mode not in ("auto", "plan_only"):
            raise ValueError("未知规划模式")
        with self._lock:
            self.task_id = f"task-{self._next_task_id}"
            self._next_task_id += 1
            self.task = task; object.__setattr__(self, "current_goal", ""); self.status = "running"; self.terminal_reason = ""
            self.tool_history.clear(); self.files_changed.clear(); self.errors.clear()
            self.plan_revisions.clear(); self.plan_progress_history.clear()
            self.user_plan_decisions.clear(); self.replan_triggers.clear()
            self.planning_state = PlanningState(mode=mode, phase="exploring" if mode == "plan_only" else "direct")
            self.stagnation_state = LoopStagnationState()
            self.verification_evidence.clear(); self.verification_history.clear()
            self.generations.clear(); self.attempts.clear()
            self.failures.clear(); self.recovery_actions.clear(); self.recovery_notice = ""
            self.trace_events.clear()
            self.process_records.clear(); self.process_events.clear(); self.awaiting_process = None
            self._verification_generation = 0; self._last_verified_generation = -1
            self._verification_required = False; self._repair_phase = "idle"
            self._active_failure_id = None; self._active_recovery_id = None
            self._next_attempt = 1; self._next_failure = 1
            self._next_plan_revision = 1; self._next_plan_progress = 1
            self._next_plan_decision = 1; self._next_plan_trigger = 1
            self._fingerprint_counts.clear(); self._repair_cycles = 0; self._reserved_repair_cycles = 0
            self._failure_retry_counts.clear(); self._original_attempt_arguments.clear(); self._next_recovery = 1
            self._next_trace_sequence = 1
            self._next_process_event = 1
            self._revision_attempt_boundaries.clear()
            if self._checkpoint_store is not None:
                self._checkpoint_store.clear()
            self.generations.append(ExecutionGeneration(0, open_reason="task_start"))
            self._append_trace_event_locked(
                "task_started",
                generation_id=0,
                planning_phase_after=self.planning_state.phase,
                repair_phase_after=self._repair_phase,
            )
            self._stagnation_progress_marker = self._progress_marker_locked()

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
        with self._lock:
            return [item for item in self._plan_projection_locked()
                    if item["status"] != "completed"]

    def has_verification_evidence(self) -> bool:
        with self._lock:
            return bool(self.verification_evidence) and self._last_verified_generation == self._verification_generation and self.verification_evidence[-1].outcome == "passed"

    def completion_reminder(self) -> dict[str, object] | None:
        with self._lock:
            if self.status in ("blocked", "failed"): return None
            if self.status == "awaiting_process": return None
            if self.planning_state.phase == "awaiting_approval": return None
            active_processes = [
                item for item in self.process_records if item.status == "running"
            ]
            if active_processes:
                return {
                    "unfinished_todos": [
                        step["content"] for step in (self._plan_view_locked() or {}).get("steps", [])
                        if step["status"] != "completed"
                    ],
                    "unfinished_plan_steps": [
                        step["content"] for step in (self._plan_view_locked() or {}).get("steps", [])
                        if step["status"] != "completed"
                    ],
                    "verification_required": self._verification_required,
                    "repair_phase": self._repair_phase,
                    "active_failure_id": self._active_failure_id,
                    "active_recovery_id": self._active_recovery_id,
                    "awaiting_process": True,
                    "process_ids": [item.process_id for item in active_processes],
                    "progress_marker": (
                        "awaiting_process",
                        tuple(item.process_id for item in active_processes),
                        self._verification_generation,
                    ),
                    "message": (
                        "后台进程仍在运行。请交回 CLI，显示 process_id 后等待用户继续输入；"
                        "进程退出后必须重新独立 verification。"
                    ),
                }
            active = self._plan_view_locked()
            missing = [step["content"] for step in (active or {}).get("steps", [])
                       if step["status"] != "completed"]
            needs_verify = self._verification_required
            needs_repair = self._repair_phase != "idle"
            needs_plan = self.planning_state.phase == "exploring"
            if not missing and not needs_verify and not needs_repair and not needs_plan:
                return None
            # Describe observable completion facts instead of counting
            # reminders. The marker changes when plan state, tool
            # observations, verification evidence, or its generation changes.
            progress_marker = (
                (
                    (active or {}).get("revision_id"),
                    tuple((step["step_id"], step["status"])
                          for step in (active or {}).get("steps", [])),
                ),
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
                self.planning_state.phase,
                self.planning_state.active_trigger_id,
            )
            if self._repair_phase == "diagnosis_required":
                if self.planning_state.phase == "exploring":
                    message = (
                        "检测到失败。请继续只读调查，或独占调用 commit_plan 提交引用当前 trigger 的修订；"
                        "不要直接执行副作用或 verification。"
                    )
                else:
                    message = (
                        "检测到失败。请先进行只读诊断，独占调用 recover 处理当前活动 failure，或独占调用 "
                        "request_replan 引用该 failure；不要直接执行副作用或 verification。"
                    )
            elif self._repair_phase == "verification_required":
                message = "恢复动作已完成或存在待验证 generation。下一条回复只能独占调用 run_shell(purpose=verification)。"
            elif needs_plan:
                message = "当前处于只读调查阶段。请继续调查并独占调用 commit_plan；普通模式未提交计划时也可调用 cancel_planning。"
            else:
                message = (
                    "任务尚未满足完成条件。请在下一条回复中调用能推进任务的工具 "
                    "（提交或推进计划、执行调查/操作或运行验证）；确实无法继续时才说明具体阻塞原因。"
                )
            return {
                "unfinished_todos": missing,
                "unfinished_plan_steps": missing,
                "verification_required": needs_verify,
                "repair_phase": self._repair_phase,
                "active_failure_id": self._active_failure_id,
                "active_recovery_id": self._active_recovery_id,
                "progress_marker": progress_marker,
                "message": message,
            }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            active_plan = self._plan_view_locked()
            projected_current_goal = next(
                (step["content"] for step in (active_plan or {}).get("steps", [])
                 if step["status"] == "in_progress"),
                "",
            )
            return {
                "task": self.task, "task_id": self.task_id, "current_goal": projected_current_goal,
                "tool_history": deepcopy(self.tool_history), "files_changed": deepcopy(self.files_changed),
                "errors": deepcopy(self.errors), "status": self.status, "terminal_reason": self.terminal_reason,
                "allowed_next_action": self._allowed_next_action_locked(),
                "todos": self._plan_projection_locked(),
                "plan_revisions": [
                    {
                        "revision_id": revision.revision_id,
                        "generation_id": revision.generation_id,
                        "parent_revision_id": revision.parent_revision_id,
                        "trigger_id": revision.trigger_id,
                        "goal": revision.goal,
                        "constraints": list(revision.constraints),
                        "success_criteria": list(revision.success_criteria),
                        "steps": [
                            {
                                "step_id": step.step_id,
                                "content": step.content,
                                "status": step.status,
                                "depends_on": list(step.depends_on),
                                "success_criteria": list(step.success_criteria),
                                "replaces": list(step.replaces),
                            }
                            for step in revision.steps
                        ],
                        "reason": revision.reason,
                        "diff": self._plan_difference_view(revision.diff),
                    }
                    for revision in self.plan_revisions
                ],
                "plan_progress_history": [
                    {
                        "progress_id": event.progress_id,
                        "revision_id": event.revision_id,
                        "generation_id": event.generation_id,
                        "step_id": event.step_id,
                        "from_status": event.from_status,
                        "to_status": event.to_status,
                        "reason": event.reason,
                    }
                    for event in self.plan_progress_history
                ],
                "planning_state": asdict(self.planning_state),
                "loop_stagnation": asdict(self.stagnation_state),
                # Short alias kept for callers that render State sections by
                # capability name rather than the concrete class name.
                "stagnation": asdict(self.stagnation_state),
                "user_plan_decisions": [asdict(x) for x in self.user_plan_decisions],
                "replan_triggers": [asdict(x) for x in self.replan_triggers],
                "active_plan": active_plan,
                "verification_evidence": [asdict(x) for x in self.verification_evidence],
                "verification_history": [asdict(x) for x in self.verification_history],
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
                        "explore_or_commit_revision" if (
                            self._repair_phase == "diagnosis_required"
                            and self.planning_state.phase == "exploring"
                        ) else "diagnose_recover_or_replan" if self._repair_phase == "diagnosis_required"
                        else "独立 verification" if self._repair_phase == "verification_required"
                        else "continue"
                    ),
                },
                "current_generation_id": self._verification_generation,
                "generations": [asdict(x) for x in self.generations],
                "attempts": [asdict(x) for x in self.attempts],
                "failures": [asdict(x) for x in self.failures],
                "recovery_actions": [asdict(x) for x in self.recovery_actions],
                "trace_events": [asdict(x) for x in self.trace_events],
                "processes": [asdict(x) for x in self.process_records],
                "process_events": [asdict(x) for x in self.process_events],
                "awaiting_process": asdict(self.awaiting_process) if self.awaiting_process is not None else None,
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
                            "repair_cycles_used": self._repair_cycles,
                            "replan_revisions_limit": MAX_REPLAN_REVISIONS,
                            "replan_revisions_used": self.planning_state.replans_used,
                            "replan_revisions_remaining": self.planning_state.replans_remaining,
                            "no_progress_replans_limit": MAX_NO_PROGRESS_REPLANS,
                            "stagnant_rounds_limit": MAX_STAGNANT_ROUNDS},
            }
