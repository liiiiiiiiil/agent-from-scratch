"""Thread-safe task state and auditable execution facts."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import re
from threading import Lock
from typing import Any, Literal

from mini_agent.checkpoint import CheckpointStore
from mini_agent.config import (MAX_ATTEMPT_FINGERPRINTS, MAX_FAILURE_RETRIES,
                               MAX_NO_PROGRESS_REPLANS, MAX_REPLAN_REVISIONS,
                               MAX_RECOVERY_ACTIONS, MAX_REPAIR_CYCLES,
                               MAX_STAGNANT_ROUNDS, MAX_SUBAGENTS,
                               MAX_CONCURRENCY, MAX_TOTAL_LLM_CALLS,
                               MAX_TOTAL_TOOL_CALLS, MAX_TOTAL_TOKENS)

EffectClass = Literal["none", "possible"]
AttemptOutcome = Literal["succeeded", "failed", "denied", "timeout", "invalid", "uncertain"]
FailureCategory = Literal["protocol", "permission", "transient", "deterministic", "validation", "unknown"]
RepairPhase = Literal["idle", "diagnosis_required", "verification_required"]
PlanStepStatus = Literal["pending", "in_progress", "completed"]
PlanningPhase = Literal["direct", "exploring", "awaiting_approval", "executing"]
ProcessStatus = Literal["running", "exited", "failed", "terminated", "orphaned"]
ProcessEventKind = Literal["started", "exited", "failed", "terminated", "killed", "cleanup_failed"]
StdinMode = Literal["closed", "pipe"]
StdinState = Literal["disabled", "open", "write_pending", "closed", "error"]

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
_DELEGATION_RECORD_LIMIT = 64
_DELEGATION_SUMMARY_MAX = 1200
_MEMORY_WRITE_TOOLS = {"remember", "revise_memory", "forget_memory"}
_MEMORY_COMMIT_UNCERTAIN = "memory_commit_uncertain"


def _delegation_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class DelegationUsage:
    """Serializable usage counters kept in the parent task ledger."""

    rounds: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    token_accounting: str = "estimated"
    result_bytes: int = 0
    elapsed_ms: int = 0

    def __post_init__(self) -> None:
        for name in (
            "rounds", "llm_calls", "tool_calls", "input_tokens",
            "output_tokens", "result_bytes", "elapsed_ms",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"delegation usage.{name} 必须是非负整数")
        if self.token_accounting not in {"provider", "estimated", "mixed"}:
            raise ValueError("delegation usage.token_accounting 无效")

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @classmethod
    def from_value(cls, value: Any) -> "DelegationUsage":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise ValueError("delegation usage 必须是 object")
        return cls(
            rounds=int(value.get("rounds", 0)),
            llm_calls=int(value.get("llm_calls", 0)),
            tool_calls=int(value.get("tool_calls", 0)),
            input_tokens=int(value.get("input_tokens", value.get("tokens", 0))),
            output_tokens=int(value.get("output_tokens", 0)),
            token_accounting=value.get("token_accounting", "estimated"),
            result_bytes=int(value.get("result_bytes", 0)),
            elapsed_ms=int(value.get("elapsed_ms", 0)),
        )


@dataclass(frozen=True)
class DelegationBudget:
    """Parent aggregate budget plus reserved and settled usage."""

    max_subagents: int = MAX_SUBAGENTS
    max_concurrency: int = MAX_CONCURRENCY
    max_total_llm_calls: int = MAX_TOTAL_LLM_CALLS
    max_total_tool_calls: int = MAX_TOTAL_TOOL_CALLS
    max_total_tokens: int = MAX_TOTAL_TOKENS
    created_subagents: int = 0
    reserved_subagents: int = 0
    reserved_llm_calls: int = 0
    reserved_tool_calls: int = 0
    reserved_tokens: int = 0
    used_llm_calls: int = 0
    used_tool_calls: int = 0
    used_tokens: int = 0

    def __post_init__(self) -> None:
        for name in (
            "max_subagents", "max_concurrency", "max_total_llm_calls",
            "max_total_tool_calls", "max_total_tokens", "created_subagents",
            "reserved_subagents", "reserved_llm_calls", "reserved_tool_calls",
            "reserved_tokens", "used_llm_calls", "used_tool_calls", "used_tokens",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"delegation budget.{name} 必须是非负整数")
        if self.max_subagents <= 0 or self.max_concurrency <= 0:
            raise ValueError("delegation budget 必须允许正数子代理和并发数")
        if self.max_concurrency > self.max_subagents:
            raise ValueError("max_concurrency 不能超过 max_subagents")

    @property
    def remaining_llm_calls(self) -> int:
        return self.max_total_llm_calls - self.used_llm_calls - self.reserved_llm_calls

    @property
    def remaining_tool_calls(self) -> int:
        return self.max_total_tool_calls - self.used_tool_calls - self.reserved_tool_calls

    @property
    def remaining_tokens(self) -> int:
        return self.max_total_tokens - self.used_tokens - self.reserved_tokens

    @property
    def remaining_subagents(self) -> int:
        return self.max_subagents - self.created_subagents


DelegationDeliveryStatus = Literal[
    "created", "running", "result_ready", "committed", "interrupted", "abandoned",
]
DelegationOutcome = Literal[
    "pending", "completed", "failed", "timed_out", "cancelled", "budget_exhausted",
]


@dataclass(frozen=True)
class DelegationRecord:
    """Bounded parent-side lifecycle fact; child history never enters it."""

    delegation_id: str
    subagent_id: str
    parent_task_id: str
    parent_generation_id: int
    task_contract_hash: str
    delivery_status: DelegationDeliveryStatus = "created"
    outcome: DelegationOutcome = "pending"
    result_id: str | None = None
    result_hash: str | None = None
    usage: DelegationUsage = field(default_factory=DelegationUsage)
    created_at: str = ""
    started_at: str | None = None
    result_ready_at: str | None = None
    committed_at: str | None = None
    cancellation_reason: str | None = None
    diagnostic_reason: str | None = None
    result_summary: str = ""
    contract_summary: dict[str, Any] = field(default_factory=dict)
    reserved_usage: DelegationUsage = field(default_factory=DelegationUsage)
    progress_hash: str | None = None
    parent_attempt_id: str | None = None
    agent_profile: str | None = None
    agent_profile_fingerprint: str | None = None
    # New fields are omitted from synchronous legacy records to preserve their
    # exported shape. Background task records are process-local capabilities;
    # only their bounded lifecycle and immutable identity are serialized.
    mode: Literal["synchronous", "background"] = "synchronous"
    startup_confirmed: bool = False
    claimed_at: str | None = None
    abandoned_at: str | None = None


def _delegation_record_payload(record: DelegationRecord) -> dict[str, Any]:
    """Keep legacy role-free State/session record shapes byte-compatible."""
    payload = asdict(record)
    if payload.get("agent_profile") is None:
        payload.pop("agent_profile", None)
        payload.pop("agent_profile_fingerprint", None)
    if payload.get("mode") == "synchronous":
        payload.pop("mode", None)
        payload.pop("startup_confirmed", None)
        payload.pop("claimed_at", None)
        payload.pop("abandoned_at", None)
    return payload


def delegation_progress_hash(result: Any) -> str:
    """Hash normalized findings/evidence, excluding IDs, usage and prose."""
    raw = result.to_dict() if hasattr(result, "to_dict") else result
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    evidence = []
    evidence_by_id: dict[str, dict[str, Any]] = {}
    for item in raw.get("evidence", []) if isinstance(raw.get("evidence"), list) else []:
        if not isinstance(item, dict):
            continue
        normalized = {
            "kind": item.get("kind"), "claim": item.get("claim"),
            "path": item.get("path"), "line": item.get("line"),
            "tool": item.get("tool"), "observation_hash": item.get("observation_hash"),
        }
        evidence.append(normalized)
        if isinstance(item.get("id"), str):
            evidence_by_id[item["id"]] = normalized
    evidence.sort(key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))
    findings = []
    for item in raw.get("findings", []) if isinstance(raw.get("findings"), list) else []:
        if not isinstance(item, dict):
            continue
        finding_evidence = []
        for evidence_id in item.get("evidence_ids", []) if isinstance(item.get("evidence_ids"), list) else []:
            finding_evidence.append(evidence_by_id.get(evidence_id, {"missing": True}))
        findings.append({
            "claim": item.get("claim"), "confidence": item.get("confidence"),
            "caveat": item.get("caveat"), "evidence": sorted(
                finding_evidence,
                key=lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True),
            ),
        })
    findings.sort(key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))
    payload = {"findings": findings, "evidence": evidence}
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def delegation_result_hash(result: Any) -> str:
    """Return the durable identity of the complete child result document.

    The progress hash intentionally ignores prose and IDs.  Durable delivery
    needs the opposite property: the raw result must be byte-for-byte
    reconstructable, so the session boundary and State use this canonical
    document hash for idempotency and tamper detection.
    """
    raw = result.to_dict() if hasattr(result, "to_dict") else result
    if not isinstance(raw, dict):
        raise ValueError("SubagentResult 必须是 object")
    return hashlib.sha256(json.dumps(
        raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


class AttemptBudgetExceeded(ValueError):
    """A tool call reached the per-argument execution budget before its handler."""


class PlanRejected(ValueError):
    """A model plan request failed validation without becoming an execution failure."""


class SessionExportError(ValueError):
    """The task is not at a complete, serializable session safety point."""


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
        elif key in ("content", "body", "old_string", "new_string", "command", "input"):
            summary[key] = f"<{type(value).__name__}:{len(value) if isinstance(value, str) else '?'}>"
        elif isinstance(value, (str, int, float, bool)) or value is None:
            summary[key] = value[:80] if isinstance(value, str) else value
        else:
            summary[key] = f"<{type(value).__name__}>"
    return summary


def stored_attempt_arguments(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Keep replay-ineligible stdin bodies out of the original-argument store."""
    result = deepcopy(arguments)
    if tool == "write_process":
        result.pop("input", None)
    if tool in {"remember", "revise_memory"} and isinstance(result.get("body"), str):
        result["body"] = f"<str:{len(result['body'])}>"
    return result


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
    kind: Literal["failure", "observation", "user_feedback", "blocked_resume", "crash_recovery"]
    reason: str
    caused_by_failure_id: str | None = None
    caused_by_attempt_id: str | None = None
    caused_by_decision_id: int | None = None
    status: Literal["active", "resolved", "rejected"] = "active"
    result_revision_id: int | None = None
    caused_by_crash_recovery_id: str | None = None


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
    open_reason: Literal[
        "task_start", "possible_effect", "recovery", "process_exit", "resume", "crash_recovery"
    ] = "task_start"
    opened_by_crash_recovery_id: str | None = None


@dataclass(frozen=True)
class CrashRecoveryRecord:
    """One immutable hand-off from an incomplete schema-3 tool boundary."""

    recovery_id: str
    source_session_id: str
    source_session_generation: int
    source_commit_sequence: int
    source_integrity: str
    source_round: int
    recovery_generation_id: int
    workspace_report: tuple[str, ...] = ()
    issue_ids: tuple[str, ...] = ()
    status: Literal["resolving", "replanned", "blocked", "complete"] = "resolving"
    derived_session_id: str | None = None
    # Digest of the structured workspace observation captured while preparing
    # the candidate.  The complete observation stays in the candidate/runtime;
    # this digest makes the hand-off auditable without copying file contents.
    workspace_observation_digest: str | None = None


@dataclass(frozen=True)
class CrashRecoveryIssue:
    """A pending invocation whose external outcome cannot be asserted."""

    issue_id: str
    recovery_id: str
    invocation_id: str
    tool: str
    effect_class: EffectClass
    handler_admitted: bool
    attempt_id: str | None
    generation_id: int | None
    recovery_generation_id: int
    classification: Literal[
        "not_executed", "uncertain_state_or_result", "uncertain_side_effect",
        "workspace_drift",
    ]
    reason: str
    status: Literal["unresolved", "investigating", "continued", "blocked"] = "unresolved"


@dataclass(frozen=True)
class CrashRecoveryDecision:
    """The CLI-only decision attached to one crash-recovery issue."""

    decision_id: int
    recovery_id: str
    issue_id: str
    decision: Literal["investigate", "continue", "block"]
    feedback: str
    generation_id: int
    investigation_attempt_id: str | None = None


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
    stdin_mode: StdinMode = "closed"
    stdin_state: StdinState = "disabled"
    write_pending: bool = False
    stdin_error: str | None = None


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
    caused_by_process_event_id: str | None = None


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
    crash_recoveries: list[CrashRecoveryRecord] = field(default_factory=list)
    crash_issues: list[CrashRecoveryIssue] = field(default_factory=list)
    crash_decisions: list[CrashRecoveryDecision] = field(default_factory=list)
    delegation_records: list[DelegationRecord] = field(default_factory=list)
    delegation_budget: DelegationBudget = field(default_factory=DelegationBudget)
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
    _next_crash_recovery: int = field(default=1, init=False, repr=False)
    _next_crash_issue: int = field(default=1, init=False, repr=False)
    _next_crash_decision: int = field(default=1, init=False, repr=False)
    _pending_process_controls: dict[str, tuple[str, str]] = field(default_factory=dict, init=False, repr=False)
    _pending_attempts: set[str] = field(default_factory=set, init=False, repr=False)
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
            "crash_recovery": tuple(
                (item.recovery_id, item.status, item.recovery_generation_id,
                 tuple(item.issue_ids)) for item in self.crash_recoveries
            ),
            "crash_issues": tuple(
                (item.issue_id, item.status, item.classification,
                 item.investigation_attempt_id if hasattr(item, "investigation_attempt_id") else None)
                for item in self.crash_issues
            ),
            # Failure IDs are task-local sequence numbers.  They must not
            # turn the same underlying failure into apparent progress.
            "active_failure": failure_summary,
            "trigger": (
                trigger.kind, trigger.caused_by_failure_id,
                trigger.caused_by_attempt_id, trigger.caused_by_decision_id,
                trigger.caused_by_crash_recovery_id,
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
                (item.process_id, item.status, item.stdout_offset, item.stderr_offset,
                 item.stdin_mode, item.stdin_state, item.write_pending, item.stdin_error)
                for item in self.process_records
            ),
            # IDs, usage and free-form summaries are deliberately excluded.
            # A repeated delegation only becomes progress when it contributes
            # a new normalized finding/evidence fact.
            "delegation_findings": tuple(sorted(
                item.progress_hash for item in self.delegation_records
                if item.delivery_status == "committed" and item.progress_hash
            )),
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
        unresolved = [item for item in self.crash_issues if item.status in ("unresolved", "investigating")]
        if unresolved:
            return f"逐项使用 /resolve {unresolved[0].issue_id} investigate|continue|block"
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

    @property
    def unresolved_crash_issues(self) -> list[CrashRecoveryIssue]:
        with self._lock:
            return [item for item in self.crash_issues
                    if item.status in ("unresolved", "investigating")]

    def has_unresolved_crash_recovery(self) -> bool:
        with self._lock:
            return any(item.status in ("unresolved", "investigating")
                       for item in self.crash_issues)

    def crash_recovery_gate(self, name: str, arguments: dict[str, Any] | None = None,
                            effect_class: EffectClass = "none") -> str | None:
        """Keep an unresolved recovery hand-off on the read-only investigation path."""
        with self._lock:
            if not any(item.status in ("unresolved", "investigating")
                       for item in self.crash_issues):
                return None
            if not any(item.status == "investigating" for item in self.crash_issues):
                return "工具调用拒绝: 请先由用户使用 /resolve <issue_id> investigate 开启调查"
            allowed = {
                "read_file", "list_dir", "grep", "calculate",
                "get_process", "read_process", "list_processes", "wait_process",
                "list_memories", "read_memory", "search_memories",
                "list_references", "search_reference", "read_reference",
                "get_subagent_status", "get_subagent_result", "cancel_subagent",
            }
            if name == "delegate_task" and isinstance(arguments, dict):
                if arguments.get("purpose") == "crash_investigation":
                    return None
            if name not in allowed or effect_class != "none":
                return "工具调用拒绝: crash recovery 仍有未结算 issue；当前只允许只读调查"
            return None

    def delegation_gate(self, name: str, arguments: dict[str, Any] | None = None,
                        effect_class: EffectClass = "none") -> str | None:
        """Gate parent-side delegation lifecycle tools before their handlers."""
        if name not in {
                "delegate_task", "spawn_subagent", "get_subagent_status",
                "get_subagent_result", "cancel_subagent"}:
            return None
        arguments = arguments if isinstance(arguments, dict) else {}
        with self._lock:
            if self.status in ("blocked", "failed"):
                return "工具调用拒绝: blocked/failed 状态不能委派"
            if self.status != "running":
                return "工具调用拒绝: 当前任务不在 idle/running 委派状态"
            if effect_class != "none":
                return "工具调用拒绝: 子代理工具必须是无副作用调用"
            phase = self.planning_state.phase
            repair = self._repair_phase
            purpose = arguments.get("purpose")
            source_id = arguments.get("source_id")
            if name in {"get_subagent_status", "get_subagent_result", "cancel_subagent"}:
                return None
            if phase == "awaiting_approval" or repair == "verification_required":
                return "工具调用拒绝: 当前阶段不能委派调查"
            unresolved = [item for item in self.crash_issues
                          if item.status in ("unresolved", "investigating")]
            if unresolved:
                investigating = [item for item in unresolved if item.status == "investigating"]
                if purpose != "crash_investigation" or not investigating:
                    return "工具调用拒绝: crash recovery 只允许调查当前 investigating issue"
                if source_id not in {
                        item.issue_id for item in investigating
                    } | {item.recovery_id for item in investigating}:
                    return "工具调用拒绝: crash_investigation 必须引用当前 issue"
                return None
            if repair == "diagnosis_required":
                if name == "spawn_subagent":
                    return "工具调用拒绝: diagnosis_required 只能使用同步 delegate_task"
                if purpose != "diagnosis" or source_id != self._active_failure_id:
                    return "工具调用拒绝: diagnosis 必须引用当前 active_failure_id"
                return None
            if name == "spawn_subagent":
                if purpose != "investigation":
                    return "工具调用拒绝: spawn_subagent 只允许 investigation"
                if phase not in ("direct", "exploring", "executing"):
                    return "工具调用拒绝: 当前计划阶段不能启动后台调查"
                return None
            if purpose != "investigation":
                return "工具调用拒绝: 当前空闲阶段只允许 investigation"
            if phase not in ("direct", "exploring", "executing"):
                return "工具调用拒绝: 当前计划阶段不能委派调查"
            return None

    @property
    def active_delegation_records(self) -> list[DelegationRecord]:
        with self._lock:
            return [item for item in self.delegation_records
                    if item.delivery_status not in {"committed", "interrupted", "abandoned"}]

    def has_active_delegations(self) -> bool:
        with self._lock:
            return any(item.delivery_status not in {"committed", "interrupted", "abandoned"}
                       for item in self.delegation_records)

    def has_active_synchronous_delegations(self) -> bool:
        with self._lock:
            return any(
                item.mode == "synchronous"
                and item.delivery_status not in {"committed", "interrupted", "abandoned"}
                for item in self.delegation_records
            )

    def has_unclaimed_background_subagents(self) -> bool:
        with self._lock:
            return any(
                item.mode == "background"
                and item.delivery_status not in {"committed", "interrupted", "abandoned"}
                for item in self.delegation_records
            )

    def background_subagent_records(self) -> list[DelegationRecord]:
        with self._lock:
            return [item for item in self.delegation_records if item.mode == "background"]

    def bind_delegation_parent_attempt(self, delegation_id: str, attempt_id: str) -> DelegationRecord:
        """Keep the durable parent invocation reference for read-only Trace."""
        if not isinstance(attempt_id, str) or not attempt_id.startswith("a-"):
            raise ValueError("委派 parent attempt_id 无效")
        with self._lock:
            index, record = self._find_delegation_locked(delegation_id)
            if record.parent_attempt_id not in (None, attempt_id):
                raise ValueError("委派 parent attempt_id 不能替换")
            updated = replace(record, parent_attempt_id=attempt_id)
            self.delegation_records[index] = updated
            return updated

    def delegation_budget_snapshot(self) -> dict[str, Any]:
        with self._lock:
            budget = self.delegation_budget
            return {
                **asdict(budget),
                "remaining_subagents": max(0, budget.remaining_subagents),
                "remaining_llm_calls": max(0, budget.remaining_llm_calls),
                "remaining_tool_calls": max(0, budget.remaining_tool_calls),
                "remaining_tokens": max(0, budget.remaining_tokens),
            }

    def configure_delegation_budget(self, **limits: int) -> DelegationBudget:
        """Set parent limits while retaining already consumed task facts."""
        allowed = {
            "max_subagents", "max_concurrency", "max_total_llm_calls",
            "max_total_tool_calls", "max_total_tokens",
        }
        unknown = set(limits) - allowed
        if unknown:
            raise ValueError("未知 delegation budget 字段: " + ", ".join(sorted(unknown)))
        with self._lock:
            candidate = replace(self.delegation_budget, **limits)
            if (
                candidate.created_subagents > candidate.max_subagents
                or candidate.used_llm_calls + candidate.reserved_llm_calls > candidate.max_total_llm_calls
                or candidate.used_tool_calls + candidate.reserved_tool_calls > candidate.max_total_tool_calls
                or candidate.used_tokens + candidate.reserved_tokens > candidate.max_total_tokens
            ):
                raise ValueError("新的 delegation budget 小于已经消耗或预留的额度")
            self.delegation_budget = candidate
            return candidate

    @staticmethod
    def _delegation_summary(task: Any) -> dict[str, Any]:
        def bounded(value: Any, limit: int) -> str:
            return str(value or "")[:limit]
        return {
            "goal": bounded(getattr(task, "goal", ""), 800),
            "scope": [bounded(item, 240) for item in tuple(getattr(task, "scope", ()))[:8]],
            "purpose": bounded(getattr(task, "purpose", ""), 80),
            "requested_tools": list(tuple(getattr(task, "requested_tools", ()))[:4]),
            **({"agent_profile": getattr(task, "agent_profile")}
               if getattr(task, "agent_profile", None) else {}),
            **({"agent_profile_fingerprint": getattr(task, "agent_profile_fingerprint")}
               if getattr(task, "agent_profile_fingerprint", None) else {}),
        }

    def reserve_delegation(self, task: Any, *, mode: str = "synchronous") -> DelegationRecord:
        """Atomically create a record and reserve its requested child budget."""
        records = self.reserve_delegation_batch([task], mode=mode)
        record = records[0]
        if isinstance(record, Exception):
            raise record
        return record

    def reserve_delegation_batch(self, tasks: list[Any], *,
                                 mode: str = "synchronous") -> list[DelegationRecord | Exception]:
        """Reserve a model-ordered batch while holding the State ledger lock.

        A later refusal never reallocates an earlier reservation.  Each item
        therefore returns either its created record or the exact refusal that
        belongs to that tool-call index.
        """
        if not isinstance(tasks, list):
            raise TypeError("tasks 必须是 list")
        if mode not in {"synchronous", "background"}:
            raise ValueError("委派 mode 无效")
        if not tasks:
            return []
        prepared: list[tuple[Any, DelegationUsage]] = []
        for task in tasks:
            requested = getattr(task, "budget", None)
            if requested is None:
                prepared.append((task, DelegationUsage()))
                continue
            prepared.append((task, DelegationUsage(
                rounds=int(getattr(requested, "max_rounds", 0)),
                llm_calls=int(getattr(requested, "max_llm_calls", 0)),
                tool_calls=int(getattr(requested, "max_tool_calls", 0)),
                input_tokens=0,
                output_tokens=int(getattr(requested, "max_tokens", 0)),
            )))
        with self._lock:
            budget = self.delegation_budget
            seen_batch: set[str] = set()
            results: list[DelegationRecord | Exception] = []
            for task, request_usage in prepared:
                contract_hash = str(getattr(task, "contract_hash", ""))
                delegation_id = str(getattr(task, "delegation_id", ""))
                if getattr(task, "budget", None) is None:
                    results.append(ValueError("委派合同缺少 budget"))
                    continue
                if not delegation_id or not getattr(task, "subagent_id", "") or not contract_hash:
                    results.append(ValueError("委派合同 ID 或 hash 无效"))
                    continue
                if delegation_id in {item.delegation_id for item in self.delegation_records}:
                    results.append(ValueError("delegation_id 已存在"))
                    continue
                if contract_hash in seen_batch or any(
                        item.task_contract_hash == contract_hash
                        for item in self.delegation_records):
                    results.append(ValueError("重复的委派合同"))
                    continue
                if len(self.delegation_records) >= _DELEGATION_RECORD_LIMIT:
                    results.append(ValueError("委派记录达到有界上限"))
                    continue
                if budget.created_subagents >= budget.max_subagents:
                    results.append(ValueError("父任务 max_subagents 预算已耗尽"))
                    continue
                if request_usage.llm_calls > budget.remaining_llm_calls:
                    results.append(ValueError("父任务 max_total_llm_calls 预算不足"))
                    continue
                if request_usage.tool_calls > budget.remaining_tool_calls:
                    results.append(ValueError("父任务 max_total_tool_calls 预算不足"))
                    continue
                if request_usage.tokens > budget.remaining_tokens:
                    results.append(ValueError("父任务 max_total_tokens 预算不足"))
                    continue
                record = DelegationRecord(
                    delegation_id=delegation_id,
                    subagent_id=str(getattr(task, "subagent_id", "")),
                    parent_task_id=self.task_id,
                    parent_generation_id=int(getattr(task, "parent_generation_id", self._verification_generation)),
                    task_contract_hash=contract_hash,
                    delivery_status="created", outcome="pending",
                    created_at=_delegation_now(),
                    contract_summary=self._delegation_summary(task),
                    reserved_usage=request_usage,
                    agent_profile=getattr(task, "agent_profile", None),
                    agent_profile_fingerprint=getattr(task, "agent_profile_fingerprint", None),
                    mode=mode,
                )
                self.delegation_records.append(record)
                budget = replace(
                    budget,
                    created_subagents=budget.created_subagents + 1,
                    reserved_subagents=budget.reserved_subagents + 1,
                    reserved_llm_calls=budget.reserved_llm_calls + request_usage.llm_calls,
                    reserved_tool_calls=budget.reserved_tool_calls + request_usage.tool_calls,
                    reserved_tokens=budget.reserved_tokens + request_usage.tokens,
                )
                seen_batch.add(contract_hash)
                self._append_trace_event_locked(
                    "delegation_created", record_type="delegation", record_id=record.delegation_id,
                )
                results.append(record)
            self.delegation_budget = budget
            return results

    def record_delegation_rejection(self, task: Any, result: Any,
                                    reason: str | None = None) -> DelegationRecord:
        """Keep a bounded audit record for a pre-child budget/lifecycle refusal."""
        with self._lock:
            if len(self.delegation_records) >= _DELEGATION_RECORD_LIMIT:
                raise ValueError("委派记录达到有界上限")
            if any(item.delegation_id == getattr(task, "delegation_id", None)
                   for item in self.delegation_records):
                raise ValueError("delegation_id 已存在")
            self.delegation_records.append(DelegationRecord(
                delegation_id=str(getattr(task, "delegation_id", "")),
                subagent_id=str(getattr(task, "subagent_id", "")),
                parent_task_id=self.task_id,
                parent_generation_id=int(getattr(task, "parent_generation_id", self._verification_generation)),
                task_contract_hash=str(getattr(task, "contract_hash", "")),
                delivery_status="created", outcome="pending", created_at=_delegation_now(),
                contract_summary=self._delegation_summary(task),
                agent_profile=getattr(task, "agent_profile", None),
                agent_profile_fingerprint=getattr(task, "agent_profile_fingerprint", None),
            ))
        return self.delegation_result_ready(
            task.delegation_id, result, diagnostic_reason=reason,
        )

    def start_delegation(self, delegation_id: str) -> DelegationRecord:
        with self._lock:
            index, record = self._find_delegation_locked(delegation_id)
            if record.delivery_status == "running":
                return record
            if record.delivery_status != "created":
                raise ValueError("委派只能从 created 转为 running")
            updated = replace(record, delivery_status="running", started_at=_delegation_now())
            self.delegation_records[index] = updated
            self._append_trace_event_locked(
                "background_subagent_started" if record.mode == "background"
                else "delegation_started",
                record_type="delegation", record_id=delegation_id,
            )
            return updated

    def confirm_background_startup(self, delegation_id: str) -> DelegationRecord:
        with self._lock:
            index, record = self._find_delegation_locked(delegation_id)
            if record.mode != "background" or record.delivery_status != "created":
                raise ValueError("只有已预留的后台委派可以确认启动")
            if record.startup_confirmed:
                return record
            updated = replace(record, startup_confirmed=True)
            self.delegation_records[index] = updated
            self._append_trace_event_locked(
                "background_subagent_accepted", record_type="delegation",
                record_id=delegation_id,
            )
            return updated

    def _find_delegation_locked(self, delegation_id: str) -> tuple[int, DelegationRecord]:
        for index, record in enumerate(self.delegation_records):
            if record.delegation_id == delegation_id:
                return index, record
        raise ValueError("未知 delegation_id")

    def delegation_result_ready(self, delegation_id: str, result: Any,
                                *, diagnostic_reason: str | None = None) -> DelegationRecord:
        raw = result.to_dict() if hasattr(result, "to_dict") else result
        if not isinstance(raw, dict):
            raise ValueError("SubagentResult 必须是 object")
        if raw.get("delegation_id") != delegation_id:
            raise ValueError("SubagentResult delegation_id 不一致")
        usage = DelegationUsage.from_value(raw.get("usage", {}))
        result_id = raw.get("result_id")
        result_hash = delegation_result_hash(raw)
        progress_hash = delegation_progress_hash(raw)
        outcome = raw.get("outcome")
        if outcome not in {"completed", "failed", "timed_out", "cancelled", "budget_exhausted"}:
            raise ValueError("SubagentResult outcome 无效")
        with self._lock:
            index, record = self._find_delegation_locked(delegation_id)
            if (raw.get("subagent_id") != record.subagent_id
                    or raw.get("parent_task_id") != record.parent_task_id
                    or raw.get("contract_hash") != record.task_contract_hash):
                raise ValueError("SubagentResult 身份或合同不一致")
            if (raw.get("agent_profile") != record.agent_profile
                    or raw.get("agent_profile_fingerprint") != record.agent_profile_fingerprint):
                raise ValueError("SubagentResult agent_profile 身份不一致")
            if record.mode == "background" and not record.startup_confirmed:
                raise ValueError("后台子代理启动确认尚未提交")
            if record.delivery_status == "committed":
                if record.result_id == result_id and record.result_hash == result_hash:
                    return record
                raise ValueError("已 committed 的委派不能替换结果")
            if record.delivery_status == "interrupted":
                raise ValueError("已中断的委派不能接受迟到结果")
            if record.delivery_status == "result_ready":
                if record.result_id == result_id and record.result_hash == result_hash:
                    return record
                raise ValueError("result_ready 委派不能替换结果")
            reserved = record.reserved_usage
            budget = self.delegation_budget
            self.delegation_budget = replace(
                budget,
                reserved_subagents=max(0, budget.reserved_subagents - 1),
                reserved_llm_calls=max(0, budget.reserved_llm_calls - reserved.llm_calls),
                reserved_tool_calls=max(0, budget.reserved_tool_calls - reserved.tool_calls),
                reserved_tokens=max(0, budget.reserved_tokens - reserved.tokens),
                used_llm_calls=budget.used_llm_calls + usage.llm_calls,
                used_tool_calls=budget.used_tool_calls + usage.tool_calls,
                used_tokens=budget.used_tokens + usage.tokens,
            )
            updated = replace(
                record, delivery_status="result_ready", outcome=outcome,
                result_id=str(result_id) if isinstance(result_id, str) else None,
                result_hash=result_hash, usage=usage,
                result_ready_at=_delegation_now(),
                diagnostic_reason=(str(diagnostic_reason)[:500] if diagnostic_reason else raw.get("error_kind")),
                result_summary=str(raw.get("summary") or "")[:_DELEGATION_SUMMARY_MAX],
                cancellation_reason=(raw.get("error_detail")[:500]
                                     if outcome == "cancelled" and isinstance(raw.get("error_detail"), str)
                                     else record.cancellation_reason),
                progress_hash=progress_hash,
            )
            self.delegation_records[index] = updated
            self._append_trace_event_locked(
                "background_subagent_result_ready" if record.mode == "background"
                else "delegation_result_ready",
                record_type="delegation", record_id=delegation_id,
            )
            return updated

    def commit_delegation(self, delegation_id: str, *, result_id: str | None = None) -> DelegationRecord:
        with self._lock:
            index, record = self._find_delegation_locked(delegation_id)
            if record.delivery_status == "committed":
                if result_id is None or result_id == record.result_id:
                    return record
                raise ValueError("重复 commit 引用了不同 result_id")
            if record.delivery_status == "abandoned":
                raise ValueError("已放弃的后台委派结果不能领取")
            if record.delivery_status != "result_ready" or not record.result_id:
                raise ValueError("委派必须先进入 result_ready 才能 committed")
            if result_id is not None and result_id != record.result_id:
                raise ValueError("result_id 与 result_ready 结果不一致")
            updated = replace(
                record, delivery_status="committed", committed_at=_delegation_now(),
                claimed_at=_delegation_now() if record.mode == "background" else None,
            )
            self.delegation_records[index] = updated
            self._append_trace_event_locked(
                "background_subagent_claimed" if record.mode == "background"
                else "delegation_committed",
                record_type="delegation", record_id=delegation_id,
            )
            self._stagnation_progress_marker = self._progress_marker_locked()
            return updated

    def commit_delegation_tool_result(self, content: Any) -> DelegationRecord | None:
        try:
            raw = json.loads(content) if isinstance(content, str) else content
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict) or not raw.get("delegation_id"):
            return None
        if not isinstance(raw.get("result_id"), str) or not raw.get("result_id"):
            return None
        result_hash = delegation_result_hash(raw)
        with self._lock:
            _, record = self._find_delegation_locked(raw["delegation_id"])
            if (record.result_id != raw["result_id"] or record.result_hash != result_hash
                    or raw.get("subagent_id") != record.subagent_id
                    or raw.get("parent_task_id") != record.parent_task_id
                    or raw.get("contract_hash") != record.task_contract_hash
                    or raw.get("agent_profile") != record.agent_profile
                    or raw.get("agent_profile_fingerprint") != record.agent_profile_fingerprint):
                raise ValueError("tool result ID/hash 与 State result_ready 不一致")
        return self.commit_delegation(raw["delegation_id"], result_id=raw["result_id"])

    def mark_delegation_interrupted(self, delegation_id: str, reason: str) -> DelegationRecord:
        """Close a created/running child as an audit fact during recovery.

        A new process must never retain an old worker as active.  No child
        result exists, so the audit record has no result identity.  The
        reserved maximum remains a conservative budget charge, while the
        record's actual usage stays unknown.
        """
        with self._lock:
            index, record = self._find_delegation_locked(delegation_id)
            if record.delivery_status == "interrupted":
                return record
            if record.delivery_status in {"committed", "abandoned"}:
                raise ValueError("已交付委派不能标记为调查中断")
            if record.delivery_status == "result_ready" and record.mode != "background":
                raise ValueError("result_ready 委派不能标记为调查中断")
            reserved = record.reserved_usage
            budget = self.delegation_budget
            was_reserved = record.delivery_status in {"created", "running"}
            prior_usage = record.usage if record.delivery_status == "result_ready" else DelegationUsage()
            self.delegation_budget = replace(
                budget,
                reserved_subagents=max(0, budget.reserved_subagents - (1 if was_reserved else 0)),
                reserved_llm_calls=max(0, budget.reserved_llm_calls - (reserved.llm_calls if was_reserved else 0)),
                reserved_tool_calls=max(0, budget.reserved_tool_calls - (reserved.tool_calls if was_reserved else 0)),
                reserved_tokens=max(0, budget.reserved_tokens - (reserved.tokens if was_reserved else 0)),
                used_llm_calls=max(0, budget.used_llm_calls - prior_usage.llm_calls) + reserved.llm_calls,
                used_tool_calls=max(0, budget.used_tool_calls - prior_usage.tool_calls) + reserved.tool_calls,
                used_tokens=max(0, budget.used_tokens - prior_usage.tokens) + reserved.tokens,
            )
            updated = replace(
                record,
                delivery_status="interrupted",
                outcome="failed",
                result_id=None, result_hash=None, progress_hash=None,
                usage=DelegationUsage(), committed_at=None, claimed_at=None,
                diagnostic_reason=(str(reason)[:400] + "；实际用量未知，预算按预留上限保守占用"),
                result_summary="调查中断；未恢复旧子代理",
            )
            self.delegation_records[index] = updated
            self._append_trace_event_locked(
                "background_subagent_interrupted" if record.mode == "background"
                else "delegation_interrupted",
                record_type="delegation", record_id=delegation_id,
            )
            self._stagnation_progress_marker = self._progress_marker_locked()
            return updated

    def abandon_background_delegation(self, delegation_id: str, reason: str) -> DelegationRecord:
        """Record deliberate result abandonment at a CLI task boundary."""
        with self._lock:
            index, record = self._find_delegation_locked(delegation_id)
            if record.mode != "background":
                raise ValueError("只有后台委派可以放弃")
            if record.delivery_status == "abandoned":
                return record
            if record.delivery_status in {"committed", "interrupted"}:
                return record
            if record.delivery_status != "result_ready":
                raise ValueError("后台委派必须先收束才能放弃")
            updated = replace(
                record, delivery_status="abandoned", abandoned_at=_delegation_now(),
                diagnostic_reason=str(reason)[:400],
            )
            self.delegation_records[index] = updated
            self._append_trace_event_locked(
                "background_subagent_abandoned", record_type="delegation",
                record_id=delegation_id,
            )
            self._stagnation_progress_marker = self._progress_marker_locked()
            return updated

    def stage_background_abandon(self, delegation_ids: list[str]) -> tuple[Any, ...]:
        """Prepare a clean handoff while retaining enough state to undo a failed save."""
        with self._lock:
            prior = []
            for delegation_id in delegation_ids:
                index, record = self._find_delegation_locked(delegation_id)
                if record.mode != "background" or record.delivery_status != "result_ready":
                    raise ValueError("只能暂存已收束且未领取的后台结果")
                prior.append((index, record))
            trace_length = len(self.trace_events)
            next_sequence = self._next_trace_sequence
            marker = self._stagnation_progress_marker
            for index, record in prior:
                self.delegation_records[index] = replace(
                    record, delivery_status="abandoned", abandoned_at=_delegation_now(),
                    diagnostic_reason="父任务边界关闭时结果未领取",
                )
                self._append_trace_event_locked(
                    "background_subagent_abandoned", record_type="delegation",
                    record_id=record.delegation_id,
                )
            self._stagnation_progress_marker = self._progress_marker_locked()
            return (tuple(prior), trace_length, next_sequence, marker)

    def rollback_background_abandon(self, token: tuple[Any, ...]) -> None:
        """Restore unclaimed results when the clean handoff was not committed."""
        prior, trace_length, next_sequence, marker = token
        with self._lock:
            if (len(self.trace_events) != trace_length + len(prior)
                    or self._next_trace_sequence != next_sequence + len(prior)):
                raise RuntimeError("后台结果放弃期间 State 发生其他变化，不能回滚")
            for index, record in prior:
                current = self.delegation_records[index]
                if (current.delegation_id != record.delegation_id
                        or current.delivery_status != "abandoned"):
                    raise RuntimeError("后台结果放弃期间委派记录发生变化，不能回滚")
                self.delegation_records[index] = record
            del self.trace_events[trace_length:]
            self._next_trace_sequence = next_sequence
            self._stagnation_progress_marker = marker

    def cancel_unstarted_background_delegation(
        self, delegation_id: str, reason: str,
    ) -> DelegationRecord:
        """Close a queued request whose startup-confirmation round never committed."""
        with self._lock:
            index, record = self._find_delegation_locked(delegation_id)
            if record.mode != "background" or record.delivery_status != "created":
                raise ValueError("只有未启动的 queued 后台委派可以直接取消")
            reserved = record.reserved_usage
            budget = self.delegation_budget
            self.delegation_budget = replace(
                budget,
                created_subagents=max(0, budget.created_subagents - 1),
                reserved_subagents=max(0, budget.reserved_subagents - 1),
                reserved_llm_calls=max(0, budget.reserved_llm_calls - reserved.llm_calls),
                reserved_tool_calls=max(0, budget.reserved_tool_calls - reserved.tool_calls),
                reserved_tokens=max(0, budget.reserved_tokens - reserved.tokens),
            )
            updated = replace(
                record, delivery_status="interrupted", outcome="cancelled",
                reserved_usage=DelegationUsage(),
                diagnostic_reason=str(reason)[:400],
                result_summary="启动确认回合未提交；worker 未启动",
            )
            self.delegation_records[index] = updated
            self._append_trace_event_locked(
                "background_subagent_never_started", record_type="delegation",
                record_id=delegation_id,
            )
            self._stagnation_progress_marker = self._progress_marker_locked()
            return updated

    def commit_recovered_delegation_result(
        self, delegation_id: str, call: dict[str, Any], result: Any,
    ) -> DelegationRecord:
        """Settle a durable child result while deriving a fresh parent attempt.

        The source session may have persisted a child result before the parent
        execution attempt existed.  Recovery has the original arguments in
        the private reservation ledger, so it can create the same successful
        parent attempt without invoking the child or inventing arguments.
        """
        raw = result.to_dict() if hasattr(result, "to_dict") else result
        if not isinstance(raw, dict):
            raise ValueError("恢复委派结果必须是 object")
        result_id = raw.get("result_id")
        result_hash = delegation_result_hash(raw)
        with self._lock:
            index, record = self._find_delegation_locked(delegation_id)
            if record.delivery_status == "committed":
                if record.result_id == result_id and record.result_hash == result_hash:
                    return record
                raise ValueError("已 committed 的委派不能替换结果")
            if record.delivery_status != "result_ready":
                raise ValueError("恢复委派结果必须来自 result_ready")
            if record.result_id != result_id or record.result_hash != result_hash:
                raise ValueError("恢复委派结果与 State 摘要不一致")

            attempt_id = call.get("attempt_id")
            if not isinstance(attempt_id, str) or attempt_id not in self._pending_attempts:
                raise ValueError("恢复委派结果缺少 pending attempt")
            arguments_hash = call.get("arguments_hash")
            if not isinstance(arguments_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", arguments_hash):
                raise ValueError("恢复委派结果缺少调用参数身份")
            summary = call.get("arguments_summary", {})
            if not isinstance(summary, dict):
                raise ValueError("恢复委派结果缺少调用参数摘要")
            pre_generation = call.get("pre_generation_id")
            generation_id = call.get("generation_id")
            if not isinstance(pre_generation, int) or not isinstance(generation_id, int):
                raise ValueError("恢复委派结果缺少 generation 引用")
            self._pending_attempts.discard(attempt_id)
            attempt = ExecutionAttempt(
                attempt_id, pre_generation, generation_id, "delegate_task", arguments_hash,
                deepcopy(summary), "succeeded", 0, "none", True,
                "allowed", output_excerpt="recovered durable delegation result",
            )
            self.attempts.append(attempt)
            self._append_trace_event_locked(
                "execution_result", generation_id=generation_id,
                revision_id=self.planning_state.active_revision_id,
                record_type="attempt", record_id=attempt_id,
            )
            updated = replace(
                record,
                delivery_status="committed",
                committed_at=_delegation_now(),
                diagnostic_reason=(
                    record.diagnostic_reason
                    or "已从持久化 result_ready 原文恢复并按父调用顺序交付"
                ),
            )
            self.delegation_records[index] = updated
            self._append_trace_event_locked(
                "delegation_result_recovered", generation_id=generation_id,
                record_type="delegation", record_id=delegation_id,
            )
            self._stagnation_progress_marker = self._progress_marker_locked()
            return updated

    def note_delegation_cancellation(self, delegation_id: str, reason: str) -> None:
        with self._lock:
            index, record = self._find_delegation_locked(delegation_id)
            self.delegation_records[index] = replace(
                record, cancellation_reason=str(reason)[:500],
            )
            if record.mode == "background" and record.cancellation_reason != str(reason)[:500]:
                self._append_trace_event_locked(
                    "background_subagent_cancel_requested", record_type="delegation",
                    record_id=delegation_id,
                )

    def reconcile_pending_delegation_boundary(
        self, calls: list[dict[str, Any]],
        pending_results: list[dict[str, Any]] | None = None,
    ) -> None:
        """Conservatively retain aggregate reservations across v0.33 recovery.

        A schema-3 boundary can be persisted before a child has returned.  A
        v0.39 result-ready entry already has a State record and keeps its
        settled usage.  Older boundaries have no such entry; their old
        created/running records are closed as interruption facts and their
        pending budget is charged conservatively.
        """
        pending = [item for item in calls if item.get("tool") in {"delegate_task", "spawn_subagent"}
                   and item.get("status") == "pending"]
        if not pending:
            return
        ready_ids = {
            item.get("invocation_id") for item in (pending_results or [])
            if isinstance(item, dict)
        }
        with self._lock:
            records_by_id = {item.delegation_id: item for item in self.delegation_records}
            interrupted = [
                call.get("delegation_id") for call in pending
                if call.get("invocation_id") not in ready_ids
                and isinstance(call.get("delegation_id"), str)
                and records_by_id.get(call.get("delegation_id")) is not None
                and records_by_id[call.get("delegation_id")].delivery_status in {"created", "running"}
            ]
        for delegation_id in interrupted:
            self.mark_delegation_interrupted(
                delegation_id, "调查运行中丢失；恢复分支不重启旧子代理。",
            )

    def interrupt_unclaimed_background_subagents(self, reason: str) -> list[DelegationRecord]:
        """Close every unclaimed in-memory result during crash recovery."""
        with self._lock:
            ids = [item.delegation_id for item in self.delegation_records
                   if item.mode == "background"
                   and item.delivery_status in {"created", "running", "result_ready"}]
        return [self.mark_delegation_interrupted(item, reason) for item in ids]

    def begin_crash_recovery(
        self,
        source_session_id: str,
        source_session_generation: int,
        source_commit_sequence: int,
        source_integrity: str,
        source_round: int,
        pending_calls: list[dict[str, Any]],
        workspace_report: list[str] | tuple[str, ...] = (),
        workspace_observation_digest: str | None = None,
        interrupted_background: list[DelegationRecord] | tuple[DelegationRecord, ...] = (),
    ) -> CrashRecoveryRecord:
        """Convert one durable pending suffix into a new, non-replayable generation."""
        with self._lock:
            if not isinstance(source_session_id, str) or not source_session_id:
                raise ValueError("source_session_id 无效")
            if any(item.status in ("resolving",) for item in self.crash_recoveries):
                raise ValueError("当前已有活动 crash recovery")
            if not isinstance(pending_calls, list):
                raise ValueError("crash recovery pending_calls 必须是 list")
            self._ensure_generation()
            recovery_id = f"cr-{self._next_crash_recovery}"
            self._next_crash_recovery += 1
            generation_id = self._verification_generation + 1
            self._verification_generation = generation_id
            previous_status = self.status
            previous_terminal_reason = self.terminal_reason
            record = CrashRecoveryRecord(
                recovery_id, source_session_id, int(source_session_generation),
                int(source_commit_sequence), str(source_integrity), int(source_round),
                generation_id, tuple(str(item)[:500] for item in workspace_report)[:64],
                (), "resolving", None, workspace_observation_digest,
            )
            self.generations.append(ExecutionGeneration(
                generation_id, opened_by_crash_recovery_id=recovery_id,
                open_reason="crash_recovery",
            ))
            self._append_trace_event_locked(
                "crash_recovery_started", generation_id=generation_id,
                record_type="crash_recovery", record_id=recovery_id,
            )
            self.verification_evidence.clear()
            self._last_verified_generation = -1
            self._verification_required = True
            self._pending_attempts.clear()
            self._pending_process_controls.clear()
            # A process handle cannot cross the session boundary.  Preserve the
            # fact that it may still exist without claiming an exit or input
            # delivery, and make it permanently non-controllable in this task.
            orphaned_processes = [
                item for item in self.process_records
                if item.status == "running" or item.write_pending
            ]
            self.process_records = [
                replace(
                    item,
                    status="orphaned",
                    # No writer thread or process handle crosses the derived
                    # session boundary.  Possible partial delivery is an
                    # issue fact, never a live write_pending capability.
                    write_pending=False,
                    stdin_state=("error" if item.write_pending else item.stdin_state),
                ) if item in orphaned_processes else item
                for item in self.process_records
            ]
            self.awaiting_process = None
            issue_ids: list[str] = []

            def add_issue(issue: CrashRecoveryIssue) -> None:
                issue_ids.append(issue.issue_id)
                self.crash_issues.append(issue)
                self._append_trace_event_locked(
                    "crash_recovery_issue", generation_id=generation_id,
                    record_type="crash_issue", record_id=issue.issue_id,
                )

            # Every old process or in-flight stdin writer receives its own
            # user-settled fact.  The new runtime cannot control the old PID,
            # and it must not claim that stdin was delivered or that the
            # process exited.
            for process in orphaned_processes:
                issue_id = f"issue-{self._next_crash_issue}"
                self._next_crash_issue += 1
                stdin_note = (
                    "；stdin 可能部分投递，不能确认送达"
                    if process.write_pending else ""
                )
                issue = CrashRecoveryIssue(
                    issue_id, recovery_id, f"process:{process.process_id}",
                    "process", "possible", True, process.start_attempt_id,
                    process.start_generation_id, generation_id,
                    "uncertain_side_effect",
                    f"旧进程 {process.process_id} (pid={process.pid}) 不能跨恢复分支控制；"
                    f"不能断言退出或外部状态{stdin_note}。",
                    "unresolved",
                )
                add_issue(issue)

            if workspace_report:
                issue_id = f"issue-{self._next_crash_issue}"
                self._next_crash_issue += 1
                add_issue(CrashRecoveryIssue(
                    issue_id, recovery_id, "workspace-drift", "workspace", "none",
                    False, None, None, generation_id, "workspace_drift",
                    "恢复准备阶段观察到工作区与 durable manifest 不一致；变化只能作为调查证据，"
                    "不能推断任何 pending handler 是否执行。",
                    "unresolved",
                ))

            for delegation in interrupted_background:
                if (not isinstance(delegation, DelegationRecord)
                        or delegation.mode != "background"
                        or delegation.delivery_status != "interrupted"):
                    raise ValueError("crash recovery 后台委派记录无效")
                issue_id = f"issue-{self._next_crash_issue}"
                self._next_crash_issue += 1
                add_issue(CrashRecoveryIssue(
                    issue_id, recovery_id,
                    f"background:{delegation.delegation_id}", "spawn_subagent",
                    "none", True, None, None, generation_id,
                    "uncertain_state_or_result",
                    f"后台子代理 {delegation.subagent_id} 未领取且未安全保存结果；"
                    "旧 worker 未恢复，用量按预留上限保守结算。",
                    "unresolved",
                ))

            for call in pending_calls:
                effect = call.get("effect_class", "none")
                admitted = bool(call.get("handler_admitted", False))
                tool_name = str(call.get("tool", "<unknown>"))
                if not admitted:
                    classification = "not_executed"
                    reason = "持久边界显示 handler_admitted=false；调用未进入 handler，恢复时补入未执行结果。"
                elif effect == "none":
                    classification = "uncertain_state_or_result"
                    reason = "调用已准入无外部副作用工具，但结果未提交；不能推断 State 或观察结果是否产生。"
                else:
                    classification = "uncertain_side_effect"
                    reason = "调用已准入可能产生副作用的 handler，但结果未提交；不得重放或声称成功。"
                if admitted and effect == "possible" and tool_name in _MEMORY_WRITE_TOOLS:
                    reason = (
                        "记忆写入调用已进入 handler，但父工具结果未提交；记忆文件可能已经替换，"
                        "提交状态未确认。不得重放；请先只读查看当前记忆，再逐项处理恢复 issue。"
                    )
                issue_id = f"issue-{self._next_crash_issue}"
                self._next_crash_issue += 1
                attempt_id = call.get("attempt_id") if isinstance(call.get("attempt_id"), str) else None
                if admitted and attempt_id is not None:
                    if any(item.attempt_id == attempt_id for item in self.attempts):
                        raise ValueError("crash recovery attempt_id 与既有 attempt 冲突")
                    summary = deepcopy(call.get("arguments_summary", {}))
                    arguments_hash = call.get("arguments_hash")
                    if (not isinstance(arguments_hash, str)
                            or not re.fullmatch(r"[0-9a-f]{64}", arguments_hash)):
                        raise ValueError("pending admitted call 缺少 durable arguments_hash")
                    attempt_generation = int(
                        call.get("generation_id")
                        if isinstance(call.get("generation_id"), int)
                        else generation_id
                    )
                    attempt_excerpt = (
                        reason[:200]
                        if admitted and effect == "possible" and tool_name in _MEMORY_WRITE_TOOLS
                        else "crash recovery: result not committed"
                    )
                    attempt = ExecutionAttempt(
                        attempt_id,
                        int(call.get("pre_generation_id") if call.get("pre_generation_id") is not None else attempt_generation),
                        attempt_generation, str(call.get("tool", "<unknown>")), arguments_hash,
                        summary, "uncertain", 0, effect, True,
                        str(call.get("permission", "allowed")),
                        output_excerpt=attempt_excerpt,
                        error_kind="crash_recovery_uncertain",
                    )
                    self.attempts.append(attempt)
                    try:
                        self._next_attempt = max(self._next_attempt, int(attempt_id[2:]) + 1)
                    except (TypeError, ValueError):
                        pass
                    self._append_trace_event_locked(
                        "crash_recovery_uncertain",
                        generation_id=attempt_generation, record_type="attempt", record_id=attempt.attempt_id,
                    )
                issue = CrashRecoveryIssue(
                    issue_id, recovery_id, str(call.get("invocation_id", "")),
                    str(call.get("tool", "<unknown>")), effect, admitted, attempt_id,
                    call.get("generation_id"), generation_id, classification, reason,
                    "unresolved" if admitted else "continued",
                )
                add_issue(issue)
                self.tool_history.append({
                    "tool": issue.tool,
                    "arguments_hash": str(call.get("arguments_hash") or canonical_arguments_hash(call.get("arguments_summary", {}))),
                    "ok": False, "brief": reason[:240], "crash_issue_id": issue_id,
                })
            record = replace(record, issue_ids=tuple(issue_ids))
            if not any(item.status == "unresolved" for item in self.crash_issues
                       if item.recovery_id == recovery_id):
                record = replace(record, status="complete")
            self.crash_recoveries.append(record)
            if any(item.status == "unresolved" for item in self.crash_issues
                   if item.recovery_id == recovery_id):
                # An existing trigger belongs to the source audit trail.  Do
                # not silently erase it when crash recovery adds another
                # obligation; only create the crash trigger after all issues
                # have been explicitly continued.
                self.planning_state = replace(self.planning_state, phase="exploring")
            if previous_status in ("blocked", "failed", "done"):
                self.status = previous_status
                self.terminal_reason = previous_terminal_reason
            else:
                self.status = "running"
                self.terminal_reason = previous_terminal_reason
            self._stagnation_progress_marker = self._progress_marker_locked()
            return record

    def resolve_crash_issue(
        self, issue_id: str, decision: str, feedback: str,
        investigation_attempt_id: str | None = None,
    ) -> CrashRecoveryDecision:
        """Apply one explicit CLI decision; the model cannot manufacture it."""
        with self._lock:
            if self.status in ("blocked", "failed", "done"):
                raise PlanRejected("当前任务已终止，不能继续结算 crash issue")
            issue = next((item for item in self.crash_issues if item.issue_id == issue_id), None)
            if issue is None:
                raise PlanRejected("未知 crash recovery issue")
            if issue.status in ("continued", "blocked"):
                raise PlanRejected("该 issue 已结算")
            if decision not in ("investigate", "continue", "block"):
                raise PlanRejected("decision 必须是 investigate、continue 或 block")
            clean_feedback = self._plan_text(feedback, "feedback", _PLAN_REASON_MAX)
            selected_attempt = investigation_attempt_id
            if decision == "continue":
                if issue.status != "investigating":
                    raise PlanRejected("continue 前必须先对该 issue 使用 investigate")
                if issue.classification == "workspace_drift" and issue.tool != "workspace":
                    raise PlanRejected("workspace drift issue 引用无效")
                investigate_decisions = [
                    item for item in self.crash_decisions
                    if item.recovery_id == issue.recovery_id
                    and item.issue_id == issue.issue_id
                    and item.decision == "investigate"
                ]
                if not investigate_decisions:
                    raise PlanRejected("continue 前必须存在该 issue 的 investigate 决定")
                candidates = [
                    item for item in self.attempts
                    if item.generation_id == issue.recovery_generation_id
                    and item.outcome == "succeeded" and item.handler_admitted
                    and item.permission == "allowed" and item.effect_class == "none"
                    and item.tool not in {
                        "begin_plan", "cancel_planning", "commit_plan",
                        "update_plan_progress", "request_replan", "recover",
                        "rollback_checkpoint", "run_shell",
                    }
                ]
                if selected_attempt is not None:
                    candidates = [item for item in candidates if item.attempt_id == selected_attempt]
                if not candidates:
                    raise PlanRejected("continue 需要本次恢复 generation 中成功、获准且无副作用的调查 attempt")
                if issue.classification == "workspace_drift":
                    candidates = [item for item in candidates if item.tool in {
                        "read_file", "list_dir", "grep", "calculate",
                        "get_process", "read_process", "list_processes", "wait_process",
                    }]
                    if not candidates:
                        raise PlanRejected("workspace drift continue 需要只读调查 attempt")
                selected_attempt = candidates[-1].attempt_id
                new_status = "continued"
            elif decision == "investigate":
                new_status = "investigating"
            else:
                new_status = "blocked"
            decision_record = CrashRecoveryDecision(
                self._next_crash_decision, issue.recovery_id, issue_id,
                decision, clean_feedback, self._verification_generation, selected_attempt,
            )
            self._next_crash_decision += 1
            self.crash_decisions.append(decision_record)
            index = self.crash_issues.index(issue)
            self.crash_issues[index] = replace(issue, status=new_status)
            recovery_index = next(i for i, item in enumerate(self.crash_recoveries)
                                  if item.recovery_id == issue.recovery_id)
            recovery = self.crash_recoveries[recovery_index]
            if decision == "block":
                self.status = "blocked"
                self.terminal_reason = f"crash_recovery_blocked; issue={issue_id}"
                self.crash_recoveries[recovery_index] = replace(recovery, status="blocked")
            elif all(item.status == "continued" for item in self.crash_issues
                     if item.recovery_id == issue.recovery_id):
                previous_trigger_id = self.planning_state.active_trigger_id
                if previous_trigger_id is not None:
                    for trigger_index, previous_trigger in enumerate(self.replan_triggers):
                        if (previous_trigger.trigger_id == previous_trigger_id
                                and previous_trigger.status == "active"):
                            self.replan_triggers[trigger_index] = replace(
                                previous_trigger, status="rejected",
                            )
                            self._append_trace_event_locked(
                                "replan_trigger_superseded",
                                generation_id=self._verification_generation,
                            )
                            break
                trigger = ReplanTrigger(
                    self._next_plan_trigger, self._verification_generation,
                    "crash_recovery", "所有不确定调用已由用户逐项结算，必须重新规划。",
                    caused_by_crash_recovery_id=issue.recovery_id,
                )
                self._next_plan_trigger += 1
                self.replan_triggers.append(trigger)
                self.planning_state = replace(
                    self.planning_state, phase="exploring", active_trigger_id=trigger.trigger_id,
                    trigger_no_progress_commits=0,
                )
                self.crash_recoveries[recovery_index] = replace(recovery, status="replanned")
                self._append_trace_event_locked(
                    "crash_recovery_trigger", generation_id=self._verification_generation,
                    record_type="replan_trigger", record_id=trigger.trigger_id,
                )
            self._append_trace_event_locked(
                "crash_recovery_decision", generation_id=self._verification_generation,
                record_type="crash_decision", record_id=decision_record.decision_id,
            )
            self._stagnation_progress_marker = self._progress_marker_locked()
            return decision_record

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
                if active is None and resolved_trigger.kind not in ("failure", "blocked_resume", "crash_recovery"):
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
                if resolved_trigger.kind == "crash_recovery":
                    for recovery_index, recovery in enumerate(self.crash_recoveries):
                        if recovery.recovery_id == resolved_trigger.caused_by_crash_recovery_id:
                            self.crash_recoveries[recovery_index] = replace(
                                recovery, status="complete",
                            )
                            break
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
        stdin_mode = metadata.get("stdin_mode", "closed")
        if stdin_mode not in ("closed", "pipe"):
            stdin_mode = "closed"
        record = ProcessRecord(
            process_id, task_id, attempt.attempt_id, attempt.generation_id,
            str(metadata.get("command") or "")[:240],
            str(metadata.get("cwd") or "")[:400], pid, "running", started_at,
            stdin_mode=stdin_mode,
            stdin_state="open" if stdin_mode == "pipe" else "disabled",
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
                stdin_mode = getattr(fact, "stdin_mode", record.stdin_mode)
                if stdin_mode not in ("closed", "pipe"):
                    stdin_mode = record.stdin_mode
                stdin_state = getattr(fact, "stdin_state", record.stdin_state)
                if stdin_state not in ("disabled", "open", "write_pending", "closed", "error"):
                    stdin_state = record.stdin_state
                write_pending = bool(getattr(fact, "write_pending", record.write_pending))
                stdin_error = getattr(fact, "stdin_error", record.stdin_error)
                if record.terminal_event_id is not None:
                    if (
                        stdout_offset != record.stdout_offset
                        or stderr_offset != record.stderr_offset
                        or stdin_mode != record.stdin_mode
                        or stdin_state != record.stdin_state
                        or write_pending != record.write_pending
                        or stdin_error != record.stdin_error
                    ):
                        self.process_records[index] = replace(
                            record, stdout_offset=stdout_offset, stderr_offset=stderr_offset,
                            stdin_mode=stdin_mode, stdin_state=stdin_state,
                            write_pending=write_pending, stdin_error=stdin_error,
                        )
                    continue
                status = str(getattr(fact, "status", "running"))
                if status not in ("running", "exited", "failed"):
                    status = "running"
                if not bool(getattr(fact, "newly_exited", False)):
                    self.process_records[index] = replace(
                        record, stdout_offset=stdout_offset, stderr_offset=stderr_offset,
                        stdin_mode=stdin_mode, stdin_state=stdin_state,
                        write_pending=write_pending, stdin_error=stdin_error,
                    )
                    continue
                event_id = f"pe-{self._next_process_event}"
                self._next_process_event += 1
                control = self._pending_process_controls.pop(process_id, None)
                event_kind: ProcessEventKind = (control[0] if control is not None else
                                                "exited" if status == "exited" else "failed")
                generation_id = self._verification_generation
                event = ProcessEvent(
                    event_id, process_id, self.task_id, event_kind, generation_id,
                    record.start_attempt_id, stdout_offset, stderr_offset,
                    getattr(fact, "exit_code", None),
                    caused_by_control_attempt_id=control[1] if control else None,
                    reason="controlled_exit" if control else "natural_exit",
                )
                self.process_events.append(event)
                self.process_records[index] = replace(
                    record, status="terminated" if control else status,
                    ended_at=getattr(fact, "ended_at", None),
                    exit_code=getattr(fact, "exit_code", None),
                    stdout_offset=stdout_offset, stderr_offset=stderr_offset,
                    stdin_mode=stdin_mode, stdin_state=stdin_state,
                    write_pending=write_pending, stdin_error=stdin_error,
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
                if event_kind == "failed":
                    failure_id = f"f-{self._next_failure}"
                    self._next_failure += 1
                    failure = FailureEvent(
                        failure_id, generation_id, "execute", "deterministic",
                        False, record.start_attempt_id,
                        cause_hint=f"process_id={process_id}; exit_code={event.exit_code}",
                        caused_by_process_event_id=event_id,
                    )
                    self.failures.append(failure)
                    previous_repair_phase = self._repair_phase
                    if not (active_failure or strict_verification):
                        self._enter_diagnosis(failure_id)
                        self.recovery_notice = (
                            f"Failure {failure_id} requires diagnosis; "
                            f"process {process_id} exited with code {event.exit_code}."
                        )
                    self._append_trace_event_locked(
                        "failure_recorded", generation_id=generation_id,
                        revision_id=self.planning_state.active_revision_id,
                        record_type="failure", record_id=failure_id,
                        repair_phase_before=previous_repair_phase,
                        repair_phase_after=self._repair_phase,
                    )
                if self._process_manager is not None and hasattr(self._process_manager, "acknowledge_exit"):
                    self._process_manager.acknowledge_exit(process_id)
                committed.append(event)
        return committed

    def active_process_records(self) -> list[ProcessRecord]:
        with self._lock:
            return [item for item in self.process_records
                    if item.status == "running" or item.write_pending]

    def enter_awaiting_process(self, reason: str = "still_running") -> ProcessWaitState | None:
        with self._lock:
            active = [item for item in self.process_records
                      if item.status == "running" or item.write_pending]
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
                stdin_mode = getattr(item, "stdin_mode", record.stdin_mode)
                if stdin_mode not in ("closed", "pipe"):
                    stdin_mode = record.stdin_mode
                stdin_state = getattr(item, "stdin_state", record.stdin_state)
                if stdin_state not in ("disabled", "open", "write_pending", "closed", "error"):
                    stdin_state = record.stdin_state
                write_pending = bool(getattr(item, "write_pending", record.write_pending))
                stdin_error = getattr(item, "stdin_error", record.stdin_error)
                record = replace(
                    record, stdin_mode=stdin_mode, stdin_state=stdin_state,
                    write_pending=write_pending, stdin_error=stdin_error,
                )
                self.process_records[index] = record
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
            reservation = AttemptReservation(
                attempt_id, before, self._verification_generation,
                fingerprint_reserved=fingerprint_reserved,
            )
            self._pending_attempts.add(attempt_id)
            return reservation

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
                    "request_replan", "write_process",
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
                    "request_replan", "write_process",
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
                                           "update_plan_progress", "request_replan", "write_process"):
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
                                      "update_plan_progress", "request_replan", "write_process"):
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
        self._pending_attempts.add(ar.attempt_id)
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
        if getattr(result, "outcome", None) == "uncertain":
            raise ValueError("uncertain outcome 只能由 crash recovery 结算旧 attempt")
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
                self._original_attempt_arguments[attempt_id] = stored_attempt_arguments(result.tool, args)
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
                self._pending_attempts.discard(attempt_id)
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
                elif result.error_kind == _MEMORY_COMMIT_UNCERTAIN:
                    category = "unknown"
                elif result.tool == "write_process" and result.error_kind in (
                    "stdin_pipe_error", "stdin_write_error", "stdin_write_thread_error",
                ):
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
            self._original_attempt_arguments[attempt_id] = stored_attempt_arguments(result.tool, args)
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
            if result.tool in ("terminate_process", "kill_process") and result.outcome == "succeeded":
                try:
                    control_output = json.loads(result.output)
                except (TypeError, ValueError):
                    control_output = {}
                expected = "killed" if result.tool == "kill_process" else "terminated"
                process_id = args.get("process_id")
                if (control_output.get("status") == expected and isinstance(process_id, str)
                        and any(item.process_id == process_id and item.task_id == self.task_id
                                and item.terminal_event_id is None for item in self.process_records)):
                    self._pending_process_controls[process_id] = (expected, attempt_id)
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
                    if result.error_kind == _MEMORY_COMMIT_UNCERTAIN:
                        self.recovery_notice = (
                            f"Failure {failure_id} ({_MEMORY_COMMIT_UNCERTAIN}) caused by {attempt_id}: "
                            "记忆文件可能已经替换，提交状态未确认；请先只读核查，不能直接重试记忆写入。"
                        )
                    else:
                        self.recovery_notice = (f"Failure {failure_id} ({category}) requires diagnosis; "
                                                f"caused by {attempt_id} in generation {generation_id}.")
                exhausted = self._fingerprint_counts[fingerprint] >= MAX_ATTEMPT_FINGERPRINTS
                if result.error_kind == _MEMORY_COMMIT_UNCERTAIN:
                    self._terminal(
                        "blocked",
                        "memory_commit_uncertain：记忆文件可能已经替换，提交状态未确认；"
                        "请先只读核查，不得直接重试",
                        failure_id,
                    )
                elif result.error_kind == "rollback_conflict":
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
        args_copy = stored_attempt_arguments(name, args)
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
            if ok and name == "write_process":
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
            self.crash_recoveries.clear(); self.crash_issues.clear(); self.crash_decisions.clear()
            self.delegation_records.clear()
            self.delegation_budget = replace(
                self.delegation_budget,
                created_subagents=0, reserved_subagents=0,
                reserved_llm_calls=0, reserved_tool_calls=0, reserved_tokens=0,
                used_llm_calls=0, used_tool_calls=0, used_tokens=0,
            )
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
            self._next_crash_recovery = 1; self._next_crash_issue = 1; self._next_crash_decision = 1
            self._pending_process_controls.clear()
            self._pending_attempts.clear()
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
            unresolved = [item for item in self.crash_issues
                          if item.status in ("unresolved", "investigating")]
            if unresolved:
                return {
                    "unfinished_todos": [],
                    "unfinished_plan_steps": [],
                    "verification_required": True,
                    "repair_phase": self._repair_phase,
                    "active_failure_id": self._active_failure_id,
                    "active_recovery_id": self._active_recovery_id,
                    "crash_recovery": {
                        "issues": [
                            {"issue_id": item.issue_id, "tool": item.tool,
                             "classification": item.classification,
                             "handler_admitted": item.handler_admitted,
                             "reason": item.reason}
                            for item in unresolved[:32]
                        ],
                    },
                    "progress_marker": (
                        "crash_recovery",
                        tuple((item.issue_id, item.status) for item in unresolved),
                        self._verification_generation,
                    ),
                    "message": (
                        "崩溃恢复仍有未结算调用。只能先进行只读调查；"
                        f"然后逐项使用 /resolve {unresolved[0].issue_id} continue 或 block。"
                    ),
                }
            active_processes = [
                item for item in self.process_records
                if item.status == "running" or item.write_pending
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
                        tuple(
                            (item.process_id, item.status, item.stdout_offset,
                             item.stderr_offset, item.stdin_mode,
                             item.stdin_state, item.write_pending,
                             item.stdin_error)
                            for item in active_processes
                        ),
                        self._verification_generation,
                    ),
                    "message": (
                        "后台进程或 stdin 写入仍未收束。请交回 CLI，显示 process_id 后等待用户继续输入；"
                        "进程退出后必须重新独立 verification。"
                    ),
                }
            active_delegations = [item for item in self.delegation_records
                                  if item.delivery_status not in {"committed", "interrupted", "abandoned"}]
            if active_delegations:
                return {
                    "unfinished_todos": [],
                    "unfinished_plan_steps": [],
                    "verification_required": self._verification_required,
                    "repair_phase": self._repair_phase,
                    "delegations": [
                        {"delegation_id": item.delegation_id,
                         "goal": item.contract_summary.get("goal", ""),
                         "delivery_status": item.delivery_status,
                         "outcome": item.outcome}
                        for item in active_delegations[:8]
                    ],
                    "progress_marker": (
                        "delegations",
                        tuple((item.delegation_id, item.delivery_status, item.outcome)
                              for item in active_delegations),
                    ),
                    "message": (
                        "存在尚未提交的子代理结果。请等待委派收束；若已取消，"
                        "应先完成结果提交或说明具体清理阻塞原因。"
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

    def session_safety_issues(self) -> list[str]:
        """Return reasons why this State cannot be saved at a safe point."""
        with self._lock:
            return self._session_safety_issues_locked()

    def _session_safety_issues_locked(self, *, allow_pending: bool = False) -> list[str]:
        issues: list[str] = []
        if self._pending_attempts and not allow_pending:
            issues.append("存在未结算的 tool attempt: " + ", ".join(sorted(self._pending_attempts)))
        if self._reserved_repair_cycles and not allow_pending:
            issues.append("存在未结算的 recovery 预算预留")
        proposed_recoveries = [item.recovery_id for item in self.recovery_actions if item.status == "proposed"]
        if proposed_recoveries:
            issues.append("存在未结算的 recovery action: " + ", ".join(proposed_recoveries))
        if self._pending_process_controls and not allow_pending:
            issues.append("存在未提交的进程控制结果")
        active_delegations = [item.delegation_id for item in self.delegation_records
                              if item.delivery_status not in {"committed", "interrupted", "abandoned"}]
        if active_delegations and not allow_pending:
            issues.append("存在活动或待提交委派: " + ", ".join(active_delegations))
        active = [item for item in self.process_records
                  if item.status == "running" or item.write_pending]
        if active and not allow_pending:
            issues.append(
                "存在活动后台进程或在途 stdin: "
                + ", ".join(f"{item.process_id}(pid={item.pid})" for item in active)
            )
        uncommitted_events = [item.process_id for item in self.process_records
                              if item.status not in ("running", "orphaned")
                              and item.terminal_event_id is None]
        if uncommitted_events and not allow_pending:
            issues.append(
                "存在尚未提交终止事件的进程: " + ", ".join(uncommitted_events)
            )
        return issues

    def export_session(self, *, allow_pending: bool = False) -> dict[str, Any]:
        """Export authoritative, JSON-safe task facts for v0.30 sessions.

        Runtime locks, the ProcessManager, checkpoint image bytes and all
        other live handles are deliberately absent.  Derived projections such
        as ``todos`` and ``current_goal`` are recomputed by the runtime and
        therefore are not stored as authoritative facts.
        """
        with self._lock:
            issues = self._session_safety_issues_locked(allow_pending=allow_pending)
            if issues:
                raise SessionExportError("；".join(issues))

            generations = [asdict(item) for item in self.generations]
            if not generations:
                # Legacy callback users can have execution facts without an
                # explicit structured reservation.  Represent the implicit
                # task-start generation in the export without mutating State.
                generations = [asdict(ExecutionGeneration(0, open_reason="task_start"))]

            payload = {
                "format": "mini_agent.state",
                "format_version": 2,
                "task": self.task,
                "task_id": self.task_id,
                "tool_history": deepcopy(self.tool_history),
                "files_changed": deepcopy(self.files_changed),
                "errors": deepcopy(self.errors),
                "status": self.status,
                "terminal_reason": self.terminal_reason,
                "plan_revisions": [asdict(item) for item in self.plan_revisions],
                "plan_progress_history": [asdict(item) for item in self.plan_progress_history],
                "user_plan_decisions": [asdict(item) for item in self.user_plan_decisions],
                "replan_triggers": [asdict(item) for item in self.replan_triggers],
                "planning_state": asdict(self.planning_state),
                "stagnation_state": asdict(self.stagnation_state),
                "verification_evidence": [asdict(item) for item in self.verification_evidence],
                "verification_history": [asdict(item) for item in self.verification_history],
                "generations": generations,
                "attempts": [asdict(item) for item in self.attempts],
                "failures": [asdict(item) for item in self.failures],
                "recovery_actions": [asdict(item) for item in self.recovery_actions],
                "crash_recoveries": [asdict(item) for item in self.crash_recoveries],
                "crash_issues": [asdict(item) for item in self.crash_issues],
                "crash_decisions": [asdict(item) for item in self.crash_decisions],
                "delegation_records": [_delegation_record_payload(item) for item in self.delegation_records],
                "delegation_budget": asdict(self.delegation_budget),
                "trace_events": [asdict(item) for item in self.trace_events],
                "process_records": [asdict(item) for item in self.process_records],
                "process_events": [asdict(item) for item in self.process_events],
                "awaiting_process": asdict(self.awaiting_process) if self.awaiting_process else None,
                "recovery_notice": self.recovery_notice,
                "private": {
                    "verification_generation": self._verification_generation,
                    "last_verified_generation": self._last_verified_generation,
                    "verification_required": self._verification_required,
                    "next_attempt": self._next_attempt,
                    "next_failure": self._next_failure,
                    "next_plan_revision": self._next_plan_revision,
                    "next_plan_progress": self._next_plan_progress,
                    "next_plan_decision": self._next_plan_decision,
                    "next_plan_trigger": self._next_plan_trigger,
                    "fingerprint_counts": [
                        {"tool": tool, "arguments_hash": arguments_hash, "count": count}
                        for (tool, arguments_hash), count in sorted(self._fingerprint_counts.items())
                    ],
                    "repair_cycles": self._repair_cycles,
                    "reserved_repair_cycles": self._reserved_repair_cycles,
                    "repair_phase": self._repair_phase,
                    "active_failure_id": self._active_failure_id,
                    "active_recovery_id": self._active_recovery_id,
                    "failure_retry_counts": [
                        {"failure_id": failure_id, "count": count}
                        for failure_id, count in sorted(self._failure_retry_counts.items())
                    ],
                    "original_attempt_arguments": [
                        {"attempt_id": attempt_id, "arguments": deepcopy(arguments)}
                        for attempt_id, arguments in sorted(self._original_attempt_arguments.items())
                    ],
                    "next_recovery": self._next_recovery,
                    "next_trace_sequence": self._next_trace_sequence,
                    "next_task_id": self._next_task_id,
                    "next_process_event": self._next_process_event,
                    "next_crash_recovery": self._next_crash_recovery,
                    "next_crash_issue": self._next_crash_issue,
                    "next_crash_decision": self._next_crash_decision,
                    "pending_process_controls": [
                        {"process_id": process_id, "expected": expected, "attempt_id": attempt_id}
                        for process_id, (expected, attempt_id)
                        in sorted(self._pending_process_controls.items())
                    ],
                    "pending_attempts": sorted(self._pending_attempts),
                    "revision_attempt_boundaries": [
                        {"revision_id": revision_id, "attempt_count": attempt_count}
                        for revision_id, attempt_count in sorted(self._revision_attempt_boundaries.items())
                    ],
                    "stagnation_progress_marker": self._stagnation_progress_marker,
                },
                "checkpoint_metadata": (
                    self._checkpoint_store.snapshot()
                    if self._checkpoint_store is not None else []
                ),
            }

        # Dataclass tuples are intentional in the runtime, but session export
        # is a plain JSON value so that canonical hashing is independent of the
        # encoder used by callers.
        normalized = json.loads(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        self.validate_session_export(normalized, allow_pending=allow_pending)
        return normalized

    def export_tool_boundary(self) -> dict[str, Any]:
        """Export State while a durable tool boundary owns pending attempts."""
        return self.export_session(allow_pending=True)

    @staticmethod
    def validate_session_export(payload: Any, *, allow_pending: bool = False) -> None:
        """Validate State references and private counters without restoring it."""
        if not isinstance(payload, dict):
            raise SessionExportError("State 导出必须是 JSON object")
        format_version = payload.get("format_version")
        if (payload.get("format") != "mini_agent.state"
                or isinstance(format_version, bool)
                or not isinstance(format_version, int)
                or format_version not in (1, 2)):
            raise SessionExportError("未知或不支持的 State 导出版本")

        def require_type(name: str, expected: type | tuple[type, ...]) -> Any:
            value = payload.get(name)
            if not isinstance(value, expected):
                raise SessionExportError(f"State 字段 {name} 类型无效")
            return value

        task = require_type("task", str)
        task_id = require_type("task_id", str)
        status = require_type("status", str)
        if status not in {"idle", "running", "awaiting_process", "done", "blocked", "failed"}:
            raise SessionExportError(f"未知 State status: {status}")

        list_names = (
            "tool_history", "files_changed", "errors", "plan_revisions",
            "plan_progress_history", "user_plan_decisions", "replan_triggers",
            "verification_evidence", "verification_history", "generations",
            "attempts", "failures", "recovery_actions", "trace_events",
            "process_records", "process_events", "checkpoint_metadata",
            "crash_recoveries", "crash_issues", "crash_decisions",
            "delegation_records",
        )
        optional_v1_lists = {"crash_recoveries", "crash_issues", "crash_decisions", "delegation_records"}
        for name in list_names:
            if name in optional_v1_lists and format_version == 1 and name not in payload:
                continue
            require_type(name, list)
        record_lists = (
            "plan_revisions", "plan_progress_history", "user_plan_decisions",
            "replan_triggers", "verification_evidence", "verification_history",
            "generations", "attempts", "failures", "recovery_actions",
            "trace_events", "process_records", "process_events", "checkpoint_metadata",
            "crash_recoveries", "crash_issues", "crash_decisions",
        )
        for name in record_lists:
            if name not in payload:
                continue
            if any(not isinstance(item, dict) for item in payload[name]):
                raise SessionExportError(f"State {name} 含非 object 记录")
        planning = require_type("planning_state", dict)
        stagnation = require_type("stagnation_state", dict)
        private = require_type("private", dict)

        delegation_budget = payload.get("delegation_budget")
        if delegation_budget is not None:
            if not isinstance(delegation_budget, dict):
                raise SessionExportError("delegation_budget 类型无效")
            try:
                DelegationBudget(**{
                    key: delegation_budget[key]
                    for key in (
                        "max_subagents", "max_concurrency", "max_total_llm_calls",
                        "max_total_tool_calls", "max_total_tokens", "created_subagents",
                        "reserved_subagents", "reserved_llm_calls", "reserved_tool_calls",
                        "reserved_tokens", "used_llm_calls", "used_tool_calls", "used_tokens",
                    ) if key in delegation_budget
                })
            except (TypeError, ValueError, KeyError) as error:
                raise SessionExportError(f"delegation_budget 无效: {error}") from error
        delegation_records = payload.get("delegation_records", [])
        if not isinstance(delegation_records, list):
            raise SessionExportError("delegation_records 类型无效")
        seen_delegations: set[str] = set()
        delegation_ids_by_status: dict[str, set[str]] = {name: set() for name in (
            "created", "running", "result_ready", "committed", "interrupted", "abandoned",
        )}
        usage_totals = {"llm_calls": 0, "tool_calls": 0, "tokens": 0}
        reserved_totals = {"llm_calls": 0, "tool_calls": 0, "tokens": 0}
        for raw in delegation_records:
            if not isinstance(raw, dict):
                raise SessionExportError("delegation_records 含非 object 记录")
            delegation_id = raw.get("delegation_id")
            if not isinstance(delegation_id, str) or not delegation_id or delegation_id in seen_delegations:
                raise SessionExportError("delegation_id 无效或重复")
            seen_delegations.add(delegation_id)
            delivery_status = raw.get("delivery_status")
            if delivery_status not in {"created", "running", "result_ready", "committed", "interrupted", "abandoned"}:
                raise SessionExportError("delegation delivery_status 无效")
            delegation_ids_by_status[delivery_status].add(delegation_id)
            if raw.get("outcome") not in {"pending", "completed", "failed", "timed_out", "cancelled", "budget_exhausted"}:
                raise SessionExportError("delegation outcome 无效")
            if not isinstance(raw.get("task_contract_hash"), str) or not re.fullmatch(r"[0-9a-f]{64}", raw.get("task_contract_hash", "")):
                raise SessionExportError("delegation contract hash 无效")
            role_id = raw.get("agent_profile")
            role_fingerprint = raw.get("agent_profile_fingerprint")
            if (role_id is None) != (role_fingerprint is None):
                raise SessionExportError("delegation agent_profile 身份字段不完整")
            if role_id is not None and (
                    not isinstance(role_id, str)
                    or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", role_id)
                    or not isinstance(role_fingerprint, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", role_fingerprint)):
                raise SessionExportError("delegation agent_profile 身份无效")
            mode = raw.get("mode", "synchronous")
            if mode not in {"synchronous", "background"}:
                raise SessionExportError("delegation mode 无效")
            if mode == "background":
                if not isinstance(raw.get("startup_confirmed"), bool):
                    raise SessionExportError("background startup_confirmed 无效")
                for field_name in ("claimed_at", "abandoned_at"):
                    value = raw.get(field_name)
                    if value is not None and not isinstance(value, str):
                        raise SessionExportError(f"background {field_name} 无效")
                if delivery_status in {"running", "result_ready", "committed", "abandoned"} and not raw.get("startup_confirmed"):
                    raise SessionExportError("后台委派状态与启动确认不一致")
            elif any(field_name in raw for field_name in ("startup_confirmed", "claimed_at", "abandoned_at")):
                raise SessionExportError("同步委派包含后台生命周期字段")
            if delivery_status in {"result_ready", "committed", "abandoned"}:
                if not isinstance(raw.get("result_id"), str) or not raw["result_id"]:
                    raise SessionExportError(f"{delivery_status} delegation 缺少 result_id")
                if not isinstance(raw.get("result_hash"), str) or not re.fullmatch(r"[0-9a-f]{64}", raw["result_hash"]):
                    raise SessionExportError(f"{delivery_status} delegation result_hash 无效")
            if delivery_status == "interrupted" and (raw.get("result_id") is not None
                    or raw.get("result_hash") is not None or raw.get("committed_at") is not None):
                raise SessionExportError("interrupted delegation 不能伪造已交付结果")
            if delivery_status == "committed" and mode == "background" and not raw.get("claimed_at"):
                raise SessionExportError("已领取后台委派缺少 claimed_at")
            if delivery_status == "abandoned" and (mode != "background" or not raw.get("abandoned_at")):
                raise SessionExportError("abandoned delegation 生命周期无效")
            parent_attempt_id = raw.get("parent_attempt_id")
            if parent_attempt_id is not None and (not isinstance(parent_attempt_id, str)
                    or not re.fullmatch(r"a-[1-9][0-9]*", parent_attempt_id)):
                raise SessionExportError("delegation parent_attempt_id 无效")
            try:
                usage = DelegationUsage.from_value(raw.get("usage", {}))
                reserved = DelegationUsage.from_value(raw.get("reserved_usage", {}))
            except (TypeError, ValueError) as error:
                raise SessionExportError(f"delegation usage 无效: {error}") from error
            usage_totals["llm_calls"] += usage.llm_calls
            usage_totals["tool_calls"] += usage.tool_calls
            usage_totals["tokens"] += usage.tokens
            if delivery_status in {"created", "running"}:
                reserved_totals["llm_calls"] += reserved.llm_calls
                reserved_totals["tool_calls"] += reserved.tool_calls
                reserved_totals["tokens"] += reserved.tokens

        if delegation_budget is not None:
            if delegation_budget.get("used_llm_calls", 0) < usage_totals["llm_calls"]:
                raise SessionExportError("delegation budget.used_llm_calls 小于记录 usage")
            if delegation_budget.get("used_tool_calls", 0) < usage_totals["tool_calls"]:
                raise SessionExportError("delegation budget.used_tool_calls 小于记录 usage")
            if delegation_budget.get("used_tokens", 0) < usage_totals["tokens"]:
                raise SessionExportError("delegation budget.used_tokens 小于记录 usage")
            if delegation_budget.get("reserved_llm_calls", 0) < reserved_totals["llm_calls"]:
                raise SessionExportError("delegation budget.reserved_llm_calls 小于活动委派预留")
            if delegation_budget.get("reserved_tool_calls", 0) < reserved_totals["tool_calls"]:
                raise SessionExportError("delegation budget.reserved_tool_calls 小于活动委派预留")
            if delegation_budget.get("reserved_tokens", 0) < reserved_totals["tokens"]:
                raise SessionExportError("delegation budget.reserved_tokens 小于活动委派预留")

        required_private = {
            "verification_generation", "last_verified_generation", "verification_required",
            "next_attempt", "next_failure", "next_plan_revision", "next_plan_progress",
            "next_plan_decision", "next_plan_trigger", "fingerprint_counts", "repair_cycles",
            "reserved_repair_cycles", "repair_phase", "active_failure_id", "active_recovery_id",
            "failure_retry_counts", "original_attempt_arguments", "next_recovery",
            "next_trace_sequence", "next_task_id", "next_process_event",
            "pending_process_controls", "pending_attempts", "revision_attempt_boundaries",
        }
        optional_v1_private = {"next_crash_recovery", "next_crash_issue", "next_crash_decision"}
        if format_version == 2:
            required_private.update(optional_v1_private)
        missing_private = sorted(required_private - set(private))
        if missing_private:
            raise SessionExportError("State private 字段缺失: " + ", ".join(missing_private))

        def unique(items: list[Any], label: str) -> None:
            if len(items) != len(set(items)):
                raise SessionExportError(f"State {label} 含重复 ID")

        generations = [item.get("generation_id") for item in payload["generations"]]
        if any(not isinstance(item, int) or item < 0 for item in generations):
            raise SessionExportError("generation_id 必须是非负整数")
        unique(generations, "generation")
        if generations != sorted(generations):
            raise SessionExportError("generation 记录必须按顺序排列")
        if not generations or generations[0] != 0:
            raise SessionExportError("generation 必须从 0 开始")
        if generations != list(range(generations[-1] + 1)):
            raise SessionExportError("generation 记录必须连续")
        generation_ids = set(generations)
        for generation in payload["generations"]:
            if generation.get("open_reason") not in {
                "task_start", "possible_effect", "recovery", "process_exit", "resume", "crash_recovery",
            }:
                raise SessionExportError("generation open_reason 无效")
            if generation.get("generation_id") == 0 and generation.get("open_reason") != "task_start":
                raise SessionExportError("初始 generation 必须是 task_start")
            if generation.get("open_reason") == "crash_recovery":
                if (not isinstance(generation.get("opened_by_crash_recovery_id"), str)
                        or not re.fullmatch(r"cr-[1-9][0-9]*", generation["opened_by_crash_recovery_id"])):
                    raise SessionExportError("crash recovery generation opener 无效")
        current_generation = private.get("verification_generation")
        if not isinstance(current_generation, int) or current_generation < 0:
            raise SessionExportError("verification_generation 无效")
        if generation_ids and current_generation not in generation_ids:
            raise SessionExportError("当前 generation 不存在于 generation 记录")
        if generations and current_generation != generations[-1]:
            raise SessionExportError("当前 generation 必须是最新记录")
        if private.get("last_verified_generation") not in {-1, *generation_ids}:
            raise SessionExportError("last_verified_generation 引用不存在")
        if not isinstance(private.get("verification_required"), bool):
            raise SessionExportError("verification_required 类型无效")
        for name in (
            "fingerprint_counts", "failure_retry_counts", "original_attempt_arguments",
            "pending_process_controls", "pending_attempts", "revision_attempt_boundaries",
        ):
            if not isinstance(private.get(name), list):
                raise SessionExportError(f"State private 字段 {name} 类型无效")
        if any(not isinstance(item, dict) for item in private["fingerprint_counts"]):
            raise SessionExportError("fingerprint_counts 含非 object 记录")
        if any(not isinstance(item, dict) for item in private["failure_retry_counts"]):
            raise SessionExportError("failure_retry_counts 含非 object 记录")
        if any(not isinstance(item, dict) for item in private["original_attempt_arguments"]):
            raise SessionExportError("original_attempt_arguments 含非 object 记录")
        if any(not isinstance(item, dict) for item in private["revision_attempt_boundaries"]):
            raise SessionExportError("revision_attempt_boundaries 含非 object 记录")

        plan_revisions = payload["plan_revisions"]
        revision_ids = [item.get("revision_id") for item in plan_revisions]
        if any(not isinstance(item, int) or item <= 0 for item in revision_ids):
            raise SessionExportError("revision_id 必须是正整数")
        unique(revision_ids, "revision")
        revision_set = set(revision_ids)
        step_sets: dict[int, set[str]] = {}
        for revision in plan_revisions:
            if revision.get("generation_id") not in generation_ids:
                raise SessionExportError("plan revision 引用了不存在的 generation")
            parent = revision.get("parent_revision_id")
            if parent is not None and (parent not in revision_set or parent >= revision["revision_id"]):
                raise SessionExportError("plan revision parent 引用无效")
            steps = revision.get("steps")
            if not isinstance(steps, list):
                raise SessionExportError("plan revision steps 类型无效")
            step_ids = [step.get("step_id") for step in steps if isinstance(step, dict)]
            if len(step_ids) != len(steps) or any(not isinstance(step_id, str) or not step_id for step_id in step_ids):
                raise SessionExportError("plan step ID 无效")
            unique(step_ids, "plan step")
            step_sets[revision["revision_id"]] = set(step_ids)
            for step in steps:
                if any(dependency not in step_sets[revision["revision_id"]]
                       for dependency in step.get("depends_on", [])):
                    raise SessionExportError("plan step dependency 引用无效")
        for event in payload["plan_progress_history"]:
            if not isinstance(event, dict) or event.get("revision_id") not in revision_set:
                raise SessionExportError("plan progress 引用了不存在的 revision")
            if event.get("step_id") not in step_sets[event["revision_id"]]:
                raise SessionExportError("plan progress 引用了不存在的 step")
            if event.get("generation_id") not in generation_ids:
                raise SessionExportError("plan progress 引用了不存在的 generation")
        progress_ids = [item.get("progress_id") for item in payload["plan_progress_history"]]
        if any(not isinstance(item, int) or item <= 0 for item in progress_ids):
            raise SessionExportError("progress_id 无效")
        unique(progress_ids, "progress")
        if planning.get("active_revision_id") is not None and planning.get("active_revision_id") not in revision_set:
            raise SessionExportError("active revision 引用不存在")
        trigger_ids = [item.get("trigger_id") for item in payload["replan_triggers"]]
        unique(trigger_ids, "trigger")
        trigger_set = set(trigger_ids)
        if planning.get("active_trigger_id") is not None and planning.get("active_trigger_id") not in trigger_set:
            raise SessionExportError("active trigger 引用不存在")
        if planning.get("phase") not in {"direct", "exploring", "awaiting_approval", "executing"}:
            raise SessionExportError("planning phase 无效")
        if planning.get("mode") not in {"auto", "plan_only"}:
            raise SessionExportError("planning mode 无效")

        attempts = payload["attempts"]
        attempt_ids = [item.get("attempt_id") for item in attempts]
        if any(not isinstance(item, str) or not item.startswith("a-") for item in attempt_ids):
            raise SessionExportError("attempt_id 无效")
        unique(attempt_ids, "attempt")
        attempt_set = set(attempt_ids)
        for attempt in attempts:
            if attempt.get("outcome") not in {
                "succeeded", "failed", "denied", "timeout", "invalid", "uncertain",
            }:
                raise SessionExportError("attempt outcome 无效")
            if attempt.get("pre_generation_id") not in generation_ids or attempt.get("generation_id") not in generation_ids:
                raise SessionExportError("attempt 引用了不存在的 generation")
            if attempt.get("caused_by_attempt_id") is not None and attempt["caused_by_attempt_id"] not in attempt_set:
                raise SessionExportError("attempt caused_by_attempt_id 引用无效")
        failure_ids = [item.get("failure_id") for item in payload["failures"]]
        if any(not isinstance(item, str) or not item.startswith("f-") for item in failure_ids):
            raise SessionExportError("failure_id 无效")
        unique(failure_ids, "failure")
        failure_set = set(failure_ids)
        for failure in payload["failures"]:
            if failure.get("generation_id") not in generation_ids or failure.get("caused_by_attempt_id") not in attempt_set:
                raise SessionExportError("failure 引用无效")
        recovery_ids = [item.get("recovery_id") for item in payload["recovery_actions"]]
        if any(not isinstance(item, str) or not item.startswith("r-") for item in recovery_ids):
            raise SessionExportError("recovery_id 无效")
        unique(recovery_ids, "recovery")
        recovery_set = set(recovery_ids)
        for action in payload["recovery_actions"]:
            if action.get("generation_id") not in generation_ids or action.get("caused_by_failure_id") not in failure_set:
                raise SessionExportError("recovery action 引用无效")
            if action.get("result_generation_id") is not None and action["result_generation_id"] not in generation_ids:
                raise SessionExportError("recovery result generation 引用无效")
            if action.get("result_attempt") is not None and action["result_attempt"] not in attempt_set:
                raise SessionExportError("recovery result attempt 引用无效")
        for decision in payload["user_plan_decisions"]:
            if decision.get("revision_id") is not None and decision["revision_id"] not in revision_set:
                raise SessionExportError("user plan decision 引用了不存在的 revision")
            if decision.get("generation_id") not in generation_ids:
                raise SessionExportError("user plan decision 引用了不存在的 generation")
        decision_ids = [item.get("decision_id") for item in payload["user_plan_decisions"]]
        if any(not isinstance(item, int) or item <= 0 for item in decision_ids):
            raise SessionExportError("decision_id 无效")
        unique(decision_ids, "decision")
        for trigger in payload["replan_triggers"]:
            if trigger.get("kind") not in {
                "failure", "observation", "user_feedback", "blocked_resume", "crash_recovery",
            }:
                raise SessionExportError("replan trigger kind 无效")
            if trigger.get("generation_id") not in generation_ids:
                raise SessionExportError("replan trigger 引用了不存在的 generation")
            for key, values in (("caused_by_failure_id", failure_set), ("caused_by_attempt_id", attempt_set), ("caused_by_decision_id", {item.get("decision_id") for item in payload["user_plan_decisions"]})):
                if trigger.get(key) is not None and trigger.get(key) not in values:
                    raise SessionExportError(f"replan trigger {key} 引用无效")
            if trigger.get("result_revision_id") is not None and trigger["result_revision_id"] not in revision_set:
                raise SessionExportError("replan trigger result revision 引用无效")

        for evidence_name in ("verification_evidence", "verification_history"):
            for evidence in payload[evidence_name]:
                if evidence.get("generation_id") not in generation_ids:
                    raise SessionExportError(f"{evidence_name} 引用了不存在的 generation")
                if evidence.get("caused_by_attempt_id") is not None and evidence["caused_by_attempt_id"] not in attempt_set:
                    raise SessionExportError(f"{evidence_name} 引用了不存在的 attempt")

        process_ids = [item.get("process_id") for item in payload["process_records"]]
        unique(process_ids, "process")
        process_set = set(process_ids)
        for process in payload["process_records"]:
            if process.get("task_id") != task_id or process.get("start_attempt_id") not in attempt_set:
                raise SessionExportError("process record 引用无效")
            if process.get("start_generation_id") not in generation_ids:
                raise SessionExportError("process record generation 引用无效")
            if process.get("status") not in {"running", "exited", "failed", "terminated", "orphaned"}:
                raise SessionExportError("process status 无效")
            if not isinstance(process.get("write_pending"), bool):
                raise SessionExportError("process write_pending 类型无效")
            if process.get("status") == "orphaned" and process.get("write_pending"):
                raise SessionExportError("orphaned process 不得保留 write_pending")
        event_ids = [item.get("event_id") for item in payload["process_events"]]
        unique(event_ids, "process event")
        for event in payload["process_events"]:
            if event.get("process_id") not in process_set or event.get("task_id") != task_id:
                raise SessionExportError("process event 引用无效")
            if event.get("generation_id") not in generation_ids or event.get("start_attempt_id") not in attempt_set:
                raise SessionExportError("process event 的 generation/attempt 引用无效")
        awaiting = payload.get("awaiting_process")
        if awaiting is not None:
            if not isinstance(awaiting, dict) or any(item not in process_set for item in awaiting.get("process_ids", [])):
                raise SessionExportError("awaiting_process 引用无效")

        counters = (
            "next_attempt", "next_failure", "next_plan_revision", "next_plan_progress",
            "next_plan_decision", "next_plan_trigger", "next_recovery", "next_trace_sequence",
            "next_task_id", "next_process_event", "repair_cycles", "reserved_repair_cycles",
        )
        if format_version == 2:
            counters = counters + ("next_crash_recovery", "next_crash_issue", "next_crash_decision")
        for name in counters:
            if (not isinstance(private.get(name), int) or isinstance(private[name], bool)
                    or private[name] < 0):
                raise SessionExportError(f"State 私有计数器 {name} 无效")
        for name in (
            "next_attempt", "next_failure", "next_plan_revision", "next_plan_progress",
            "next_plan_decision", "next_plan_trigger", "next_recovery", "next_trace_sequence",
            "next_task_id", "next_process_event",
        ):
            if private[name] < 1:
                raise SessionExportError(f"State 私有计数器 {name} 必须为正整数")
        if private["repair_cycles"] > MAX_REPAIR_CYCLES or private["reserved_repair_cycles"] > MAX_REPAIR_CYCLES:
            raise SessionExportError("repair cycle 计数超过预算")
        if private["reserved_repair_cycles"] and not allow_pending:
            raise SessionExportError("安全点不得包含未结算的 recovery 预算")
        if len(payload["recovery_actions"]) > MAX_RECOVERY_ACTIONS:
            raise SessionExportError("recovery action 计数超过预算")
        if planning.get("replans_used", 0) > MAX_REPLAN_REVISIONS or planning.get("replans_remaining", 0) < 0:
            raise SessionExportError("replan 预算无效")
        if private["next_attempt"] <= max([int(str(item)[2:]) for item in attempt_ids] or [0]):
            raise SessionExportError("next_attempt 未超过已有 attempt")
        if private["next_failure"] <= max([int(str(item)[2:]) for item in failure_ids] or [0]):
            raise SessionExportError("next_failure 未超过已有 failure")
        if private["next_recovery"] <= max([int(str(item)[2:]) for item in recovery_ids] or [0]):
            raise SessionExportError("next_recovery 未超过已有 recovery")
        for item in private["fingerprint_counts"]:
            if not isinstance(item, dict) or not isinstance(item.get("tool"), str) or not isinstance(item.get("arguments_hash"), str) or not isinstance(item.get("count"), int) or item["count"] <= 0:
                raise SessionExportError("fingerprint_counts 无效")
        fingerprint_keys = [
            (item.get("tool"), item.get("arguments_hash"))
            for item in private["fingerprint_counts"]
        ]
        unique(fingerprint_keys, "fingerprint")
        original = private["original_attempt_arguments"]
        original_ids = [item.get("attempt_id") for item in original]
        unique(original_ids, "original attempt arguments")
        if any(item not in attempt_set for item in original_ids):
            raise SessionExportError("原始恢复参数引用不存在的 attempt")
        attempt_by_id = {item["attempt_id"]: item for item in attempts}
        for item in original:
            if attempt_by_id[item["attempt_id"]].get("tool") == "write_process" and "input" in item.get("arguments", {}):
                raise SessionExportError("write_process.input 不得进入 State session 导出")
            if not isinstance(item.get("arguments"), dict):
                raise SessionExportError("原始恢复参数必须是 JSON object")
        for item in private["failure_retry_counts"]:
            if (not isinstance(item, dict) or item.get("failure_id") not in failure_set
                    or not isinstance(item.get("count"), int) or item["count"] < 0):
                raise SessionExportError("failure_retry_counts 无效")
        if len({item.get("failure_id") for item in private["failure_retry_counts"]}) != len(private["failure_retry_counts"]):
            raise SessionExportError("failure_retry_counts 含重复 failure")
        pending_attempts = private.get("pending_attempts")
        if not isinstance(pending_attempts, list) or any(
                not isinstance(item, str) or not item.startswith("a-")
                or not item[2:].isdigit()
                for item in pending_attempts):
            raise SessionExportError("pending_attempts 无效")
        unique(pending_attempts, "pending attempt")
        if any(item in attempt_set for item in pending_attempts):
            raise SessionExportError("pending attempt 不能同时出现在 attempts")
        if any(int(item[2:]) >= private["next_attempt"] for item in pending_attempts):
            raise SessionExportError("pending attempt 引用了尚未分配的 attempt")
        if pending_attempts and not allow_pending:
            raise SessionExportError("安全点不得包含 pending attempt")
        if private.get("pending_process_controls") and not allow_pending:
            raise SessionExportError("安全点不得包含 pending process control")
        boundaries = private["revision_attempt_boundaries"]
        if not isinstance(boundaries, list):
            raise SessionExportError("revision_attempt_boundaries 类型无效")
        for boundary in boundaries:
            if boundary.get("revision_id") not in revision_set or not isinstance(boundary.get("attempt_count"), int) or boundary["attempt_count"] < 0 or boundary["attempt_count"] > len(attempts):
                raise SessionExportError("revision attempt boundary 无效")
        max_ids = {
            "next_plan_revision": revision_ids,
            "next_plan_progress": [item.get("progress_id") for item in payload["plan_progress_history"]],
            "next_plan_decision": [item.get("decision_id") for item in payload["user_plan_decisions"]],
            "next_plan_trigger": trigger_ids,
            "next_trace_sequence": [item.get("sequence_id") for item in payload["trace_events"]],
            "next_process_event": [int(str(item)[3:]) for item in event_ids if isinstance(item, str) and item.startswith("pe-")],
        }
        for counter_name, values in max_ids.items():
            numeric_values = [value for value in values if isinstance(value, int)]
            if numeric_values and private[counter_name] <= max(numeric_values):
                raise SessionExportError(f"{counter_name} 未超过已有记录")
        if isinstance(task_id, str) and task_id.startswith("task-"):
            try:
                task_number = int(task_id[5:])
            except ValueError:
                task_number = 0
            if task_number and private["next_task_id"] <= task_number:
                raise SessionExportError("next_task_id 未超过当前 task_id")
        if not isinstance(stagnation, dict) or not isinstance(private.get("repair_phase"), str):
            raise SessionExportError("修复/停滞状态无效")
        if private["repair_phase"] not in {"idle", "diagnosis_required", "verification_required"}:
            raise SessionExportError("repair_phase 无效")
        if private.get("active_failure_id") is not None and private["active_failure_id"] not in failure_set:
            raise SessionExportError("active failure 引用无效")
        if private.get("active_recovery_id") is not None and private["active_recovery_id"] not in recovery_set:
            raise SessionExportError("active recovery 引用无效")
        crash_recoveries = payload.get("crash_recoveries", [])
        crash_issues = payload.get("crash_issues", [])
        crash_decisions = payload.get("crash_decisions", [])
        if not isinstance(crash_recoveries, list) or not isinstance(crash_issues, list) or not isinstance(crash_decisions, list):
            raise SessionExportError("crash recovery 列表类型无效")
        crash_recovery_ids = [item.get("recovery_id") for item in crash_recoveries]
        crash_issue_ids = [item.get("issue_id") for item in crash_issues]
        if (any(not isinstance(value, str) or not value for value in crash_recovery_ids + crash_issue_ids)
                or any(not re.fullmatch(r"cr-[1-9][0-9]*", value) for value in crash_recovery_ids)
                or any(not re.fullmatch(r"issue-[1-9][0-9]*", value) for value in crash_issue_ids)):
            raise SessionExportError("crash recovery/issue ID 无效")
        unique(crash_recovery_ids, "crash recovery")
        unique(crash_issue_ids, "crash issue")
        crash_recovery_set = set(crash_recovery_ids)
        crash_issue_set = set(crash_issue_ids)
        crash_decision_ids = [item.get("decision_id") for item in crash_decisions]
        if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in crash_decision_ids):
            raise SessionExportError("crash decision ID 无效")
        unique(crash_decision_ids, "crash decision")
        for trigger in payload["replan_triggers"]:
            if trigger.get("kind") == "crash_recovery":
                if trigger.get("caused_by_crash_recovery_id") not in crash_recovery_set:
                    raise SessionExportError("crash recovery trigger 引用无效")
        for item in crash_recoveries:
            if (not isinstance(item.get("source_session_id"), str)
                    or not item.get("source_session_id")
                    or not isinstance(item.get("source_integrity"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", item.get("source_integrity", ""))
                    or (not isinstance(item.get("source_session_generation"), int)
                        or isinstance(item.get("source_session_generation"), bool)
                        or item.get("source_session_generation") < 1)
                    or (not isinstance(item.get("source_commit_sequence"), int)
                        or isinstance(item.get("source_commit_sequence"), bool)
                        or item.get("source_commit_sequence") < 0)
                    or (not isinstance(item.get("source_round"), int)
                        or isinstance(item.get("source_round"), bool)
                        or item.get("source_round") < 0)
                    or (not isinstance(item.get("recovery_generation_id"), int)
                        or isinstance(item.get("recovery_generation_id"), bool)
                        or item.get("recovery_generation_id") not in generation_ids)
                    or not isinstance(item.get("issue_ids"), list)
                    or item.get("status") not in {"resolving", "replanned", "blocked", "complete"}):
                raise SessionExportError("crash recovery 记录无效")
            if (not all(isinstance(issue_id, str) for issue_id in item.get("issue_ids", []))
                    or len(item.get("issue_ids", [])) != len(set(item.get("issue_ids", [])))):
                raise SessionExportError("crash recovery issue_ids 无效")
            if not isinstance(item.get("workspace_report", []), list):
                raise SessionExportError("workspace_report 必须是数组")
            digest = item.get("workspace_observation_digest")
            if digest is not None and (not isinstance(digest, str)
                                       or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                raise SessionExportError("workspace observation digest 无效")
            if any(not isinstance(report, str) or len(report) > 500
                   for report in item.get("workspace_report", [])):
                raise SessionExportError("workspace_report 无效")
            if not isinstance(item.get("derived_session_id"), (str, type(None))) or (
                    item.get("derived_session_id") is not None
                    and not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", item["derived_session_id"])):
                raise SessionExportError("derived session ID 无效")
            recovery_generation = next(
                (generation for generation in payload["generations"]
                 if generation.get("generation_id") == item.get("recovery_generation_id")),
                None,
            )
            if (not isinstance(recovery_generation, dict)
                    or recovery_generation.get("open_reason") != "crash_recovery"
                    or recovery_generation.get("opened_by_crash_recovery_id") != item.get("recovery_id")):
                raise SessionExportError("crash recovery generation opener 无效")
            expected_issue_ids = [issue.get("issue_id") for issue in crash_issues
                                  if issue.get("recovery_id") == item.get("recovery_id")]
            if item.get("issue_ids") != expected_issue_ids:
                raise SessionExportError("crash recovery issue_ids 必须精确匹配同 recovery 的有序 issues")
            owned_issues = [issue for issue in crash_issues
                            if issue.get("recovery_id") == item.get("recovery_id")]
            statuses = {issue.get("status") for issue in owned_issues}
            if item.get("status") == "complete" and statuses - {"continued"}:
                raise SessionExportError("complete crash recovery 仍有未结算 issue")
            if item.get("status") == "replanned" and statuses - {"continued"}:
                raise SessionExportError("replanned crash recovery 仍有未结算 issue")
            if item.get("status") == "replanned" and not any(
                    trigger.get("kind") == "crash_recovery"
                    and trigger.get("caused_by_crash_recovery_id") == item.get("recovery_id")
                    for trigger in payload["replan_triggers"]):
                raise SessionExportError("replanned crash recovery 缺少聚合 replan trigger")
            if item.get("status") == "resolving" and not (statuses - {"continued"}):
                raise SessionExportError("没有未结算 issue 的 crash recovery 不能保持 resolving")
            if item.get("status") == "blocked" and "blocked" not in statuses:
                raise SessionExportError("blocked crash recovery 缺少 blocked issue")
        for item in crash_issues:
            if (item.get("recovery_id") not in crash_recovery_set
                    or not isinstance(item.get("issue_id"), str)
                    or not isinstance(item.get("invocation_id"), str)
                    or not isinstance(item.get("tool"), str)
                    or item.get("effect_class") not in {"none", "possible"}
                    or not isinstance(item.get("handler_admitted"), bool)
                    or item.get("classification") == "not_executed" and item.get("handler_admitted")
                    or item.get("recovery_generation_id") not in generation_ids
                    or item.get("classification") not in {
                        "not_executed", "uncertain_state_or_result", "uncertain_side_effect",
                        "workspace_drift",
                    }
                    or item.get("status") not in {"unresolved", "investigating", "continued", "blocked"}):
                raise SessionExportError("crash issue 记录无效")
            owner = next(item2 for item2 in crash_recoveries
                         if item2.get("recovery_id") == item.get("recovery_id"))
            if item.get("recovery_generation_id") != owner.get("recovery_generation_id"):
                raise SessionExportError("crash issue recovery generation 归属不一致")
            if not item.get("invocation_id") or not item.get("tool") or not isinstance(item.get("reason"), str):
                raise SessionExportError("crash issue identity/reason 无效")
            classification = item.get("classification")
            if classification == "workspace_drift":
                if (item.get("invocation_id") != "workspace-drift"
                        or item.get("tool") != "workspace"
                        or item.get("handler_admitted")
                        or item.get("effect_class") != "none"
                        or item.get("attempt_id") is not None
                        or item.get("generation_id") is not None):
                    raise SessionExportError("workspace drift issue 形状无效")
            elif classification == "not_executed":
                if item.get("handler_admitted") or item.get("attempt_id") is not None:
                    raise SessionExportError("not_executed issue 不得引用 handler attempt")
                if item.get("status") != "continued":
                    raise SessionExportError("not_executed issue 必须自动结算")
            elif classification == "uncertain_state_or_result" and (
                    not item.get("handler_admitted") or item.get("effect_class") != "none"):
                raise SessionExportError("uncertain_state_or_result issue 形状无效")
            elif classification == "uncertain_side_effect" and (
                    not item.get("handler_admitted") or item.get("effect_class") != "possible"):
                raise SessionExportError("uncertain_side_effect issue 形状无效")
            if (item.get("generation_id") is not None
                    and (not isinstance(item.get("generation_id"), int)
                         or isinstance(item.get("generation_id"), bool)
                         or item.get("generation_id") not in generation_ids)):
                raise SessionExportError("crash issue generation 引用无效")
            if item.get("attempt_id") is not None and item.get("attempt_id") not in attempt_set:
                raise SessionExportError("crash issue attempt 引用无效")
            if item.get("attempt_id") is not None:
                attempt = next(attempt for attempt in attempts
                               if attempt.get("attempt_id") == item.get("attempt_id"))
                if (attempt.get("tool") != item.get("tool")
                        and item.get("tool") != "process"):
                    raise SessionExportError("crash issue attempt/tool 引用不一致")
                if (attempt.get("generation_id") != item.get("generation_id")
                        or attempt.get("effect_class") != item.get("effect_class")
                        or attempt.get("handler_admitted") is not True):
                    raise SessionExportError("crash issue attempt 引用不一致")
        for item in crash_decisions:
            issue = next((candidate for candidate in crash_issues
                          if candidate.get("issue_id") == item.get("issue_id")), None)
            if (item.get("recovery_id") not in crash_recovery_set
                    or item.get("issue_id") not in crash_issue_set
                    or issue is None
                    or issue.get("recovery_id") != item.get("recovery_id")
                    or not isinstance(item.get("decision_id"), int)
                    or isinstance(item.get("decision_id"), bool)
                    or item.get("decision") not in {"investigate", "continue", "block"}
                    or item.get("generation_id") not in generation_ids):
                raise SessionExportError("crash decision 记录无效")
            investigation_attempt_id = item.get("investigation_attempt_id")
            if investigation_attempt_id is not None and (
                    not isinstance(investigation_attempt_id, str)
                    or not investigation_attempt_id.startswith("a-")
                    or investigation_attempt_id not in attempt_set):
                raise SessionExportError("crash decision investigation attempt 引用无效")
            if item.get("generation_id") != issue.get("recovery_generation_id"):
                raise SessionExportError("crash decision generation 与 issue recovery 不一致")
            if item.get("decision") == "investigate" and item.get("investigation_attempt_id") is not None:
                raise SessionExportError("investigate 决定不能预先声称调查 attempt")
            if item.get("decision") == "continue":
                selected = item.get("investigation_attempt_id")
                if selected is None:
                    raise SessionExportError("continue 决定缺少调查 attempt")
                attempt = next(attempt for attempt in attempts if attempt.get("attempt_id") == selected)
                if (attempt.get("generation_id") != issue.get("recovery_generation_id")
                        or attempt.get("outcome") != "succeeded"
                        or attempt.get("permission") != "allowed"
                        or attempt.get("effect_class") != "none"
                        or attempt.get("handler_admitted") is not True):
                    raise SessionExportError("continue 调查 attempt 不属于恢复 generation")
                investigate_events = [
                    event.get("sequence_id") for event in payload["trace_events"]
                    if event.get("record_type") == "crash_decision"
                    and event.get("record_id") in {
                        decision.get("decision_id") for decision in crash_decisions
                        if decision.get("issue_id") == issue.get("issue_id")
                        and decision.get("decision") == "investigate"
                    }
                ]
                attempt_events = [event.get("sequence_id") for event in payload["trace_events"]
                                  if event.get("record_type") == "attempt"
                                  and event.get("record_id") == selected]
                if not investigate_events or not attempt_events or max(investigate_events) >= max(attempt_events):
                    raise SessionExportError("调查 attempt 必须发生在 investigate 决定之后")
        for checkpoint in payload["checkpoint_metadata"]:
            if not isinstance(checkpoint, dict) or not isinstance(checkpoint.get("checkpoint_id"), str):
                raise SessionExportError("checkpoint metadata 无效")
            if checkpoint.get("attempt_id") not in attempt_set or checkpoint.get("generation_id") not in generation_ids:
                raise SessionExportError("checkpoint metadata 引用无效")

    @classmethod
    def restore_session(cls, payload: Any, workspace_root: str | None = None,
                        *, allow_pending: bool = False) -> "AgentState":
        """Rebuild authoritative State facts from a validated session export.

        This conversion never restores operating-system handles.  Checkpoint
        metadata and historical process records are imported as audit facts;
        their live capabilities are intentionally supplied by the new runtime.
        """
        cls.validate_session_export(payload, allow_pending=allow_pending)
        if not isinstance(payload, dict):  # keeps type checkers and callers honest
            raise SessionExportError("State 导出必须是 JSON object")

        def diff_from(raw: Any) -> PlanDifference | None:
            if raw is None:
                return None
            if not isinstance(raw, dict):
                raise SessionExportError("plan diff 无效")
            retained = tuple(
                PlanStepDifference(item["step_id"], bool(item.get("dependencies_changed", False)))
                for item in raw.get("retained", [])
            )
            return PlanDifference(
                retained=retained,
                added=tuple(raw.get("added", [])),
                cancelled=tuple(raw.get("cancelled", [])),
                replaced=tuple(raw.get("replaced", [])),
                goal_changed=bool(raw.get("goal_changed", False)),
                constraints_changed=bool(raw.get("constraints_changed", False)),
                success_criteria_changed=bool(raw.get("success_criteria_changed", False)),
            )

        def step_from(raw: dict[str, Any]) -> PlanStep:
            return PlanStep(
                raw["step_id"], raw["content"], raw.get("status", "pending"),
                tuple(raw.get("depends_on", [])), tuple(raw.get("success_criteria", [])),
                tuple(raw.get("replaces", [])),
            )

        revisions = [
            PlanRevision(
                raw["revision_id"], raw["generation_id"], raw.get("parent_revision_id"),
                raw.get("trigger_id"), raw["goal"], tuple(raw.get("constraints", [])),
                tuple(raw.get("success_criteria", [])),
                tuple(step_from(step) for step in raw.get("steps", [])),
                raw["reason"], diff_from(raw.get("diff")),
            ) for raw in payload["plan_revisions"]
        ]
        progress = [PlanProgressEvent(
            raw["progress_id"], raw["revision_id"], raw["generation_id"], raw["step_id"],
            raw["from_status"], raw["to_status"], raw["reason"],
        ) for raw in payload["plan_progress_history"]]
        decisions = [UserPlanDecision(
            raw["decision_id"], raw.get("revision_id"), raw["decision"], raw.get("feedback"),
            raw["generation_id"], raw.get("previous_terminal_reason"),
            raw.get("caused_by_failure_id"),
        ) for raw in payload["user_plan_decisions"]]
        triggers = [ReplanTrigger(
            raw["trigger_id"], raw["generation_id"], raw["kind"], raw["reason"],
            raw.get("caused_by_failure_id"), raw.get("caused_by_attempt_id"),
            raw.get("caused_by_decision_id"), raw.get("status", "active"),
            raw.get("result_revision_id"), raw.get("caused_by_crash_recovery_id"),
        ) for raw in payload["replan_triggers"]]
        planning = PlanningState(**{
            key: payload["planning_state"][key]
            for key in ("mode", "phase", "active_revision_id", "active_trigger_id",
                        "replans_used", "replans_remaining", "trigger_no_progress_commits")
        })
        stagnation = LoopStagnationState(
            payload["stagnation_state"].get("progress_epoch", 0),
            payload["stagnation_state"].get("consecutive_no_progress_rounds", 0),
            payload["stagnation_state"].get("last_round_fingerprint"),
            tuple(payload["stagnation_state"].get("seen_observation_hashes", [])),
            tuple(payload["stagnation_state"].get("seen_effect_action_hashes", [])),
            payload["stagnation_state"].get("warning_kind"),
            payload["stagnation_state"].get("last_reason"),
        )
        evidence = [VerificationEvidence(
            raw["command"], raw["outcome"], raw.get("exit_code"), raw["output"],
            raw.get("generation_id", 0), raw.get("caused_by_attempt_id"),
        ) for raw in payload["verification_evidence"]]
        verification_history = [VerificationEvidence(
            raw["command"], raw["outcome"], raw.get("exit_code"), raw["output"],
            raw.get("generation_id", 0), raw.get("caused_by_attempt_id"),
        ) for raw in payload["verification_history"]]
        generations = [ExecutionGeneration(
            raw["generation_id"], raw.get("opened_by_attempt_id"),
            raw.get("opened_by_failure_id"), raw.get("opened_by_recovery_id"),
            raw.get("opened_by_process_event_id"), raw.get("open_reason", "task_start"),
            raw.get("opened_by_crash_recovery_id"),
        ) for raw in payload["generations"]]
        attempts = [ExecutionAttempt(
            raw["attempt_id"], raw["pre_generation_id"], raw["generation_id"], raw["tool"],
            raw["arguments_hash"], deepcopy(raw["redacted_arguments"]), raw["outcome"],
            raw["duration_ms"], raw["effect_class"], raw["handler_admitted"], raw["permission"],
            raw.get("caused_by_failure_id"), raw.get("caused_by_attempt_id"), raw.get("exit_code"),
            raw.get("error_kind"), raw.get("output_excerpt", ""), raw.get("failure_id"),
            raw.get("recovery_id"), raw.get("checkpoint_id"),
        ) for raw in payload["attempts"]]
        failures = [FailureEvent(
            raw["failure_id"], raw["generation_id"], raw["phase"], raw["category"],
            raw["retryable"], raw["caused_by_attempt_id"], tuple(raw.get("affected_files", [])),
            raw.get("cause_hint"), raw.get("caused_by_process_event_id"),
        ) for raw in payload["failures"]]
        recoveries = [RecoveryAction(
            raw["recovery_id"], raw["generation_id"], raw["action"], raw["reason"],
            raw["caused_by_failure_id"], raw["status"], raw.get("requested_attempt"),
            raw.get("requested_tool"), raw.get("requested_arguments_hash"),
            deepcopy(raw.get("redacted_arguments")), raw.get("result_generation_id"),
            raw.get("result_attempt"), raw.get("checkpoint_id"),
        ) for raw in payload["recovery_actions"]]
        trace_events = [TraceEvent(
            raw["sequence_id"], raw["kind"], raw["generation_id"], raw.get("revision_id"),
            raw.get("record_type"), raw.get("record_id"), raw.get("planning_phase_before"),
            raw.get("planning_phase_after"), raw.get("repair_phase_before"),
            raw.get("repair_phase_after"), raw.get("stagnation_kind"), raw.get("stagnation_count"),
            raw.get("stagnation_fingerprint"),
        ) for raw in payload["trace_events"]]
        process_records = [ProcessRecord(
            raw["process_id"], raw["task_id"], raw["start_attempt_id"], raw["start_generation_id"],
            raw["command_summary"], raw["cwd_summary"], raw["pid"], raw["status"], raw["started_at"],
            raw.get("ended_at"), raw.get("exit_code"), raw.get("stdout_offset", 0),
            raw.get("stderr_offset", 0), raw.get("terminal_event_id"), raw.get("stdin_mode", "closed"),
            raw.get("stdin_state", "disabled"), raw.get("write_pending", False), raw.get("stdin_error"),
        ) for raw in payload["process_records"]]
        process_events = [ProcessEvent(
            raw["event_id"], raw["process_id"], raw["task_id"], raw["kind"], raw["generation_id"],
            raw["start_attempt_id"], raw["stdout_offset"], raw["stderr_offset"], raw.get("exit_code"),
            raw.get("caused_by_control_attempt_id"), raw.get("reason"),
        ) for raw in payload["process_events"]]
        awaiting_raw = payload.get("awaiting_process")
        awaiting = None if awaiting_raw is None else ProcessWaitState(
            tuple(awaiting_raw["process_ids"]), awaiting_raw["reason"],
            tuple(awaiting_raw.get("last_observed_event_ids", [])),
            tuple(awaiting_raw.get("last_stdout_offsets", [])),
            tuple(awaiting_raw.get("last_stderr_offsets", [])),
        )

        crash_recoveries = [CrashRecoveryRecord(
            raw["recovery_id"], raw["source_session_id"], raw["source_session_generation"],
            raw["source_commit_sequence"], raw["source_integrity"], raw["source_round"],
            raw["recovery_generation_id"], tuple(raw.get("workspace_report", [])),
            tuple(raw.get("issue_ids", [])), raw.get("status", "resolving"),
            raw.get("derived_session_id"),
            raw.get("workspace_observation_digest"),
        ) for raw in payload.get("crash_recoveries", [])]
        crash_issues = [CrashRecoveryIssue(
            raw["issue_id"], raw["recovery_id"], raw["invocation_id"], raw["tool"],
            raw["effect_class"], raw["handler_admitted"], raw.get("attempt_id"),
            raw.get("generation_id"), raw["recovery_generation_id"], raw["classification"],
            raw["reason"], raw.get("status", "unresolved"),
        ) for raw in payload.get("crash_issues", [])]
        crash_decisions = [CrashRecoveryDecision(
            raw["decision_id"], raw["recovery_id"], raw["issue_id"], raw["decision"],
            raw["feedback"], raw["generation_id"], raw.get("investigation_attempt_id"),
        ) for raw in payload.get("crash_decisions", [])]
        delegation_records = []
        for raw in payload.get("delegation_records", []):
            delegation_records.append(DelegationRecord(
                raw["delegation_id"], raw["subagent_id"], raw["parent_task_id"],
                raw["parent_generation_id"], raw["task_contract_hash"],
                raw.get("delivery_status", "created"), raw.get("outcome", "pending"),
                raw.get("result_id"), raw.get("result_hash"),
                DelegationUsage.from_value(raw.get("usage", {})),
                raw.get("created_at", ""), raw.get("started_at"), raw.get("result_ready_at"),
                raw.get("committed_at"), raw.get("cancellation_reason"),
                raw.get("diagnostic_reason"), raw.get("result_summary", ""),
                deepcopy(raw.get("contract_summary", {})),
                DelegationUsage.from_value(raw.get("reserved_usage", {})),
                raw.get("progress_hash"), raw.get("parent_attempt_id"),
                raw.get("agent_profile"), raw.get("agent_profile_fingerprint"),
                raw.get("mode", "synchronous"), raw.get("startup_confirmed", False),
                raw.get("claimed_at"), raw.get("abandoned_at"),
            ))
        raw_budget = payload.get("delegation_budget") or {}
        delegation_budget = DelegationBudget(**{
            key: raw_budget[key] for key in (
                "max_subagents", "max_concurrency", "max_total_llm_calls",
                "max_total_tool_calls", "max_total_tokens", "created_subagents",
                "reserved_subagents", "reserved_llm_calls", "reserved_tool_calls",
                "reserved_tokens", "used_llm_calls", "used_tool_calls", "used_tokens",
            ) if key in raw_budget
        })

        state = cls(
            task=payload["task"], task_id=payload["task_id"], tool_history=deepcopy(payload["tool_history"]),
            files_changed=deepcopy(payload["files_changed"]), errors=deepcopy(payload["errors"]),
            status=payload["status"], terminal_reason=payload["terminal_reason"],
            plan_revisions=revisions, plan_progress_history=progress,
            user_plan_decisions=decisions, replan_triggers=triggers, planning_state=planning,
            stagnation_state=stagnation, verification_evidence=evidence,
            verification_history=verification_history, generations=generations, attempts=attempts,
            failures=failures, recovery_actions=recoveries, trace_events=trace_events,
            crash_recoveries=crash_recoveries, crash_issues=crash_issues,
            crash_decisions=crash_decisions,
            delegation_records=delegation_records,
            delegation_budget=delegation_budget,
            process_records=process_records, process_events=process_events,
            awaiting_process=awaiting, recovery_notice=payload["recovery_notice"],
        )
        private = payload["private"]
        for name in (
            "verification_generation", "last_verified_generation", "verification_required",
            "next_attempt", "next_failure", "next_plan_revision", "next_plan_progress",
            "next_plan_decision", "next_plan_trigger", "repair_cycles", "reserved_repair_cycles",
            "repair_phase", "active_failure_id", "active_recovery_id", "next_recovery",
            "next_trace_sequence", "next_task_id", "next_process_event",
        ):
            setattr(state, f"_{name}", deepcopy(private[name]))
        state._next_crash_recovery = int(private.get("next_crash_recovery", 1))
        state._next_crash_issue = int(private.get("next_crash_issue", 1))
        state._next_crash_decision = int(private.get("next_crash_decision", 1))
        state._fingerprint_counts = {
            (item["tool"], item["arguments_hash"]): item["count"]
            for item in private["fingerprint_counts"]
        }
        state._failure_retry_counts = {
            item["failure_id"]: item["count"] for item in private["failure_retry_counts"]
        }
        state._original_attempt_arguments = {
            item["attempt_id"]: deepcopy(item["arguments"])
            for item in private["original_attempt_arguments"]
        }
        state._pending_process_controls = {
            item["process_id"]: (item["expected"], item["attempt_id"])
            for item in private["pending_process_controls"]
        }
        state._pending_attempts = set(private["pending_attempts"])
        state._revision_attempt_boundaries = {
            item["revision_id"]: item["attempt_count"]
            for item in private["revision_attempt_boundaries"]
        }
        checkpoint_store = CheckpointStore(workspace_root)
        checkpoint_store.import_snapshot(payload["checkpoint_metadata"])
        state.bind_checkpoint_store(checkpoint_store)
        with state._lock:
            state._sync_current_goal_locked()
            state._stagnation_progress_marker = state._progress_marker_locked()
        return state

    def begin_resume(self) -> int:
        """Open an independent resume generation and invalidate old proof."""
        with self._lock:
            self._ensure_generation()
            generation_id = self._verification_generation + 1
            self._verification_generation = generation_id
            self.generations.append(ExecutionGeneration(generation_id, open_reason="resume"))
            self.verification_evidence.clear()
            self._last_verified_generation = -1
            self._verification_required = True
            self._append_trace_event_locked("session_resumed", generation_id=generation_id)
            self._stagnation_progress_marker = self._progress_marker_locked()
            return generation_id

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
                "crash_recoveries": [asdict(x) for x in self.crash_recoveries],
                "crash_issues": [asdict(x) for x in self.crash_issues],
                "crash_decisions": [asdict(x) for x in self.crash_decisions],
                "delegations": [_delegation_record_payload(x) for x in self.delegation_records],
                "delegation_budget": {
                    **asdict(self.delegation_budget),
                    "remaining_subagents": max(0, self.delegation_budget.remaining_subagents),
                    "remaining_llm_calls": max(0, self.delegation_budget.remaining_llm_calls),
                    "remaining_tool_calls": max(0, self.delegation_budget.remaining_tool_calls),
                    "remaining_tokens": max(0, self.delegation_budget.remaining_tokens),
                },
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
