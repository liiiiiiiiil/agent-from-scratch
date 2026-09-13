"""Read-only Trace & Replay views for one in-memory task.

This module deliberately consumes only the public ``AgentState.snapshot`` shape.
It does not import the executor, call a handler, ask the permission gate, or
look up any of the private retry/checkpoint stores.  A malformed snapshot is
still useful: records that can be located are returned and uncertain causal
edges are marked unresolved.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, is_dataclass
import json
import os
import re
from collections.abc import Mapping, Sequence
from typing import Any


class TraceQueryError(ValueError):
    """Raised for an invalid trace query, rather than a damaged trace."""


_GENERATION_FIELDS = (
    "generation_id", "opened_by_attempt_id", "opened_by_failure_id",
    "opened_by_recovery_id", "open_reason",
)
_ATTEMPT_FIELDS = (
    "attempt_id", "pre_generation_id", "generation_id", "tool",
    "arguments_hash", "redacted_arguments", "outcome", "duration_ms",
    "effect_class", "handler_admitted", "permission", "caused_by_failure_id",
    "caused_by_attempt_id", "exit_code", "error_kind", "output_excerpt",
    "failure_id", "recovery_id", "checkpoint_id",
)
_FAILURE_FIELDS = (
    "failure_id", "generation_id", "phase", "category", "retryable",
    "caused_by_attempt_id", "affected_files", "cause_hint",
)
_RECOVERY_FIELDS = (
    "recovery_id", "generation_id", "action", "reason",
    "caused_by_failure_id", "status", "requested_attempt", "requested_tool",
    "requested_arguments_hash", "redacted_arguments", "result_generation_id",
    "result_attempt", "checkpoint_id",
)
_VERIFICATION_FIELDS = (
    "command", "outcome", "exit_code", "output", "generation_id",
    "caused_by_attempt_id",
)
_PLAN_REVISION_FIELDS = (
    "revision_id", "generation_id", "parent_revision_id", "trigger_id",
    "goal", "constraints", "success_criteria", "steps", "reason", "diff",
)
_PLAN_PROGRESS_FIELDS = (
    "progress_id", "revision_id", "generation_id", "step_id",
    "from_status", "to_status", "reason",
)
_PLAN_DECISION_FIELDS = (
    "decision_id", "revision_id", "decision", "feedback", "generation_id",
    "previous_terminal_reason", "caused_by_failure_id",
)
_TRIGGER_FIELDS = (
    "trigger_id", "generation_id", "kind", "reason",
    "caused_by_failure_id", "caused_by_attempt_id", "caused_by_decision_id",
    "status", "result_revision_id",
)
_TRACE_EVENT_FIELDS = (
    "sequence_id", "kind", "generation_id", "revision_id", "record_type",
    "record_id", "planning_phase_before", "planning_phase_after",
    "repair_phase_before", "repair_phase_after", "stagnation_kind",
    "stagnation_count", "stagnation_fingerprint",
)
_TODO_REVISION_FIELDS = ("revision_id", "generation_id", "todos", "current_goal")
_TODO_FIELDS = ("content", "status")
_CHECKPOINT_FIELDS = (
    "checkpoint_id", "attempt_id", "generation_id", "path", "before_type",
    "before_sha256", "after_type", "after_sha256", "mode", "status",
    "unavailable_reason", "created_at",
)

_ACCEPTED_RECOVERY_STATUSES = {"reserved", "executed", "terminal"}
_STATE_STATUSES = {"running", "done", "blocked", "failed", "idle"}


def _jsonish(value: Any) -> Any:
    """Copy values into a JSON-like shape without consulting private state."""
    if is_dataclass(value):
        return _jsonish(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonish(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonish(item) for item in value]
    if isinstance(value, list):
        return [_jsonish(item) for item in value]
    return deepcopy(value)


def _bounded_text(value: Any, limit: int = 4000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _safe_text(value: Any) -> str:
    """Hide absolute locations while retaining the bounded saved excerpt."""
    text = _bounded_text(value)
    # State normally stores workspace-relative checkpoint paths, but ordinary
    # tool output can still echo an absolute path.  Replay has no workspace
    # root and must not expose that private location.
    text = re.sub(
        r"(?<![A-Za-z0-9_])/(?:[^\s,:;()\[\]{}]+(?:/[^\s,:;()\[\]{}]+)*)",
        "<absolute-path>",
        text,
    )
    text = re.sub(r"\b[A-Za-z]:\\(?:[^\s,:;()\[\]{}]+(?:\\[^\s,:;()\[\]{}]+)*)",
                  "<absolute-path>", text)
    return text


def _safe_path(value: Any) -> Any:
    if not isinstance(value, str):
        return _jsonish(value)
    if os.path.isabs(value) or re.match(r"^[A-Za-z]:[\\/]", value):
        return "<absolute-path>"
    return _safe_text(value)


def _safe_redacted_arguments(value: Any) -> dict[str, Any] | Any:
    """Re-sanitize the already-redacted State summary.

    Trace never receives the original arguments.  This second pass prevents a
    hand-built/corrupt snapshot from turning a field named ``content`` or
    ``token`` into a raw value in the report.
    """
    if not isinstance(value, Mapping):
        return _jsonish(value)
    sensitive = ("key", "token", "secret", "password", "credential", "authorization")
    result: dict[str, Any] = {}
    for key, raw in value.items():
        name = str(key)
        lower = name.lower()
        if any(word in lower for word in sensitive):
            result[name] = "<redacted>"
        elif name in {"content", "old_string", "new_string", "command"}:
            # Keep the normal State marker (for example ``<str:18>``), but do
            # not trust arbitrary values supplied by a damaged snapshot.
            marker = str(raw)
            result[name] = marker if re.fullmatch(r"<[A-Za-z_][A-Za-z0-9_]*:\d+>", marker) else "<value omitted>"
        elif name in {"path", "file", "filename"}:
            result[name] = _safe_path(raw)
        elif isinstance(raw, str):
            result[name] = _safe_text(raw[:120])
        elif isinstance(raw, (int, float, bool)) or raw is None:
            result[name] = raw
        else:
            result[name] = f"<{type(raw).__name__}>"
    return result


def _safe_record(record: Any, fields: Sequence[str], issues: list[str], label: str) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        issues.append(f"{label} 不是对象")
        return {}
    result: dict[str, Any] = {}
    for field in fields:
        if field not in record:
            continue
        value = record[field]
        if field in {"redacted_arguments"}:
            result[field] = _safe_redacted_arguments(value)
        elif field in {"affected_files"}:
            result[field] = [_safe_path(item) for item in value] if isinstance(value, (list, tuple)) else _jsonish(value)
        elif field in {"output_excerpt", "output", "reason", "cause_hint", "command", "current_goal",
                       "feedback", "previous_terminal_reason", "goal", "step_id", "from_status",
                       "to_status", "kind", "stagnation_kind", "stagnation_fingerprint"}:
            result[field] = _safe_text(value)
        elif field in {"path", "file", "filename"}:
            result[field] = _safe_path(value)
        elif field == "todos" and isinstance(value, (list, tuple)):
            result[field] = [
                {key: _safe_text(item.get(key)) if key == "content" else _jsonish(item.get(key))
                 for key in _TODO_FIELDS if key in item}
                if isinstance(item, Mapping) else _jsonish(item)
                for item in value
            ]
        else:
            result[field] = _jsonish(value)
    return result


def _safe_plan_revision(record: Any, issues: list[str], label: str) -> dict[str, Any]:
    """Copy plan structure while bounding every model supplied text field."""
    if not isinstance(record, Mapping):
        issues.append(f"{label} 不是对象")
        return {}
    result = _safe_record(record, _PLAN_REVISION_FIELDS, issues, label)
    for field in ("constraints", "success_criteria"):
        value = record.get(field)
        if isinstance(value, (list, tuple)):
            result[field] = [_safe_text(item) for item in value]
        elif field in record:
            result[field] = _jsonish(value)
    steps = record.get("steps")
    if isinstance(steps, (list, tuple)):
        safe_steps = []
        for index, step in enumerate(steps):
            if not isinstance(step, Mapping):
                issues.append(f"{label}.steps[{index}] 不是对象")
                safe_steps.append(_jsonish(step))
                continue
            safe_step = {}
            for field in ("step_id", "content", "status"):
                if field in step:
                    safe_step[field] = _safe_text(step[field]) if field != "status" else _jsonish(step[field])
            for field in ("depends_on", "success_criteria", "replaces"):
                value = step.get(field)
                if isinstance(value, (list, tuple)):
                    safe_step[field] = [
                        _safe_text(item) if field != "success_criteria" else _safe_text(item)
                        for item in value
                    ]
                elif field in step:
                    safe_step[field] = _jsonish(value)
            safe_steps.append(safe_step)
        result["steps"] = safe_steps
    elif "steps" in record:
        result["steps"] = _jsonish(steps)
    diff = record.get("diff")
    if isinstance(diff, Mapping):
        safe_diff = {}
        for key, value in diff.items():
            if key == "retained" and isinstance(value, (list, tuple)):
                safe_diff[key] = [
                    {
                        "step_id": _safe_text(item.get("step_id")),
                        "dependencies_changed": bool(item.get("dependencies_changed")),
                    }
                    if isinstance(item, Mapping) else _jsonish(item)
                    for item in value
                ]
            elif key in {"added", "cancelled", "replaced"} and isinstance(value, (list, tuple)):
                safe_diff[key] = [_safe_text(item) for item in value]
            else:
                safe_diff[key] = _jsonish(value)
        result["diff"] = safe_diff
    return result


def _safe_trace_event(record: Any, issues: list[str], label: str) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        issues.append(f"{label} 不是对象")
        return {}
    result = _safe_record(record, _TRACE_EVENT_FIELDS, issues, label)
    # The fingerprint is already a short State summary.  Do not preserve a
    # hand-built long value in a replay report.
    if "stagnation_fingerprint" in record:
        value = record.get("stagnation_fingerprint")
        result["stagnation_fingerprint"] = _safe_text(value)[:12]
    return result


def _safe_checkpoint(record: Any, issues: list[str], label: str) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        issues.append(f"{label} 不是对象")
        return {}
    result: dict[str, Any] = {}
    for field in _CHECKPOINT_FIELDS:
        if field not in record:
            continue
        value = record[field]
        if field == "path":
            result[field] = _safe_path(value)
        elif field == "unavailable_reason":
            result[field] = _safe_text(value)
        else:
            result[field] = _jsonish(value)
    return result


def _records(snapshot: Mapping[str, Any], key: str, issues: list[str]) -> list[Any]:
    value = snapshot.get(key, [])
    if not isinstance(value, (list, tuple)):
        issues.append(f"{key} 不是数组")
        return []
    return list(value)


def _id_map(records: list[dict[str, Any]], field: str, kind: str, issues: list[str]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(records):
        identifier = record.get(field)
        if not isinstance(identifier, str) or not identifier:
            issues.append(f"{kind}[{index}] 缺少合法 {field}")
            continue
        if identifier in result:
            issues.append(f"{kind} ID 重复: {identifier}")
            continue
        result[identifier] = record
    return result


def _valid_generation(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _add_issue(issues: list[str], message: str) -> None:
    if message not in issues:
        issues.append(message)


def _ref_issue(issues: list[str], owner: str, field: str, value: Any,
               available: Mapping[Any, Any]) -> bool:
    if value is None:
        return False
    try:
        found = value in available
    except TypeError:
        found = False
    if not found:
        _add_issue(issues, f"{owner}.{field} 引用不存在: {value}")
        return False
    return True


def _mapping_get(mapping: Mapping[Any, Any], key: Any, default: Any = None) -> Any:
    """Read a possibly damaged map without letting an unhashable ID escape."""
    try:
        return mapping.get(key, default)
    except (AttributeError, TypeError):
        return default


def _mapping_contains(mapping: Mapping[Any, Any], key: Any) -> bool:
    try:
        return key in mapping
    except (AttributeError, TypeError):
        return False


def _hashable(value: Any) -> bool:
    try:
        hash(value)
    except TypeError:
        return False
    return True


def _valid_positive_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _safe_plan_steps(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    value = record.get("steps", [])
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, (list, tuple)) else []


def _step_structure(step: Mapping[str, Any]) -> tuple[Any, ...]:
    def values(key: str) -> tuple[Any, ...]:
        value = step.get(key, [])
        return tuple(value) if isinstance(value, (list, tuple)) else ("<invalid>",)
    return (
        step.get("step_id"), step.get("content"), values("depends_on"),
        values("success_criteria"), values("replaces"),
    )


def _computed_plan_diff(parent: Mapping[str, Any], revision: Mapping[str, Any]) -> dict[str, Any]:
    parent_steps = {
        item.get("step_id"): item for item in _safe_plan_steps(parent)
        if item.get("step_id") is not None
    }
    new_steps = {
        item.get("step_id"): item for item in _safe_plan_steps(revision)
        if item.get("step_id") is not None
    }
    replaced = tuple(
        item for step in _safe_plan_steps(revision)
        for item in (step.get("replaces", []) if isinstance(step.get("replaces", []), (list, tuple)) else [])
    )
    return {
        "retained": [
            {
                "step_id": step_id,
                "dependencies_changed": (
                    parent_steps[step_id].get("depends_on", [])
                    != new_steps[step_id].get("depends_on", [])
                ),
            }
            for step_id in parent_steps if step_id in new_steps
        ],
        "added": [step_id for step_id in new_steps if step_id not in parent_steps],
        "cancelled": [
            step_id for step_id in parent_steps
            if step_id not in new_steps and step_id not in replaced
        ],
        "replaced": list(replaced),
        "goal_changed": parent.get("goal") != revision.get("goal"),
        "constraints_changed": parent.get("constraints") != revision.get("constraints"),
        "success_criteria_changed": parent.get("success_criteria") != revision.get("success_criteria"),
    }


def _normalise_diff(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return value
    result = {}
    for key in ("retained", "added", "cancelled", "replaced",
                "goal_changed", "constraints_changed", "success_criteria_changed"):
        raw = value.get(key)
        if key == "retained" and isinstance(raw, (list, tuple)):
            result[key] = [
                {
                    "step_id": item.get("step_id"),
                    "dependencies_changed": bool(item.get("dependencies_changed")),
                }
                if isinstance(item, Mapping) else item
                for item in raw
            ]
        elif key in {"added", "cancelled", "replaced"} and isinstance(raw, (list, tuple)):
            result[key] = list(raw)
        else:
            result[key] = raw
    return result


def _event_index(events: list[dict[str, Any]], record_type: str, record_id: Any) -> list[dict[str, Any]]:
    return [
        event for event in events
        if event.get("record_type") == record_type and event.get("record_id") == record_id
    ]


def _event_record_view(event: Mapping[str, Any], records: Mapping[str, Any]) -> dict[str, Any] | None:
    record_type = event.get("record_type")
    record_id = event.get("record_id")
    if record_type == "verification_history":
        values = _mapping_get(records, record_type, [])
        return deepcopy(values[record_id]) if isinstance(record_id, int) and 0 <= record_id < len(values) else None
    if record_type == "tool_history":
        values = _mapping_get(records, record_type, [])
        return deepcopy(values[record_id]) if isinstance(record_id, int) and 0 <= record_id < len(values) else None
    values = _mapping_get(records, record_type, {})
    return deepcopy(_mapping_get(values, record_id)) if isinstance(values, Mapping) else None


def _node(kind: str, identifier: Any) -> str:
    return f"{kind}:{identifier}"


def _edge(edge_type: str, source: Any, target: Any, generation_id: Any,
          resolved: bool, detail: str | None = None) -> dict[str, Any]:
    result = {
        "type": edge_type,
        "kind": edge_type,
        "from": source,
        "to": target,
        "generation_id": generation_id,
        "resolved": resolved,
        "status": "resolved" if resolved else "unresolved",
    }
    if detail:
        result["detail"] = detail
    return result


def _conclusion(snapshot: Mapping[str, Any], generation_id: int,
                selected_ids: list[int], all_ids: list[int],
                failures: list[dict[str, Any]]) -> dict[str, Any]:
    later = [item for item in all_ids if item > generation_id]
    if later:
        status = "continue"
    else:
        state_status = snapshot.get("status", "running")
        status = state_status if state_status in {"done", "blocked", "failed"} else "continue"
    result: dict[str, Any] = {"status": status}
    if status in {"blocked", "failed"}:
        result["terminal_reason"] = _safe_text(snapshot.get("terminal_reason", ""))
        latest = failures[-1] if failures else None
        result["last_failure"] = deepcopy(latest)
    return result


def _build_edges(
    generation_records: dict[int, dict[str, Any]],
    attempts: dict[str, dict[str, Any]],
    failures: dict[str, dict[str, Any]],
    recoveries: dict[str, dict[str, Any]],
    evidence: list[dict[str, Any]],
    issues: list[str],
    plan_revisions: dict[int, dict[str, Any]] | None = None,
    plan_progress: dict[int, dict[str, Any]] | None = None,
    plan_decisions: dict[int, dict[str, Any]] | None = None,
    triggers: dict[int, dict[str, Any]] | None = None,
    trace_events: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    plan_revisions = plan_revisions or {}
    plan_progress = plan_progress or {}
    plan_decisions = plan_decisions or {}
    triggers = triggers or {}
    trace_events = trace_events or []

    for gid, generation in generation_records.items():
        opener_fields = [
            ("attempt", "opened_by_attempt_id"),
            ("failure", "opened_by_failure_id"),
            ("recovery", "opened_by_recovery_id"),
        ]
        present = [(kind, field, generation.get(field)) for kind, field in opener_fields
                   if generation.get(field) is not None]
        if gid == 0 and not present:
            edges.append(_edge("generation_opener", "task_start", _node("generation", gid), gid, True))
        elif gid == 0:
            _add_issue(issues, "初始 generation 0 不应有 opener")
        elif len(present) != 1:
            _add_issue(issues, f"generation {gid} 的 opener 不唯一或缺失")
            if not present:
                edges.append(_edge("generation_opener", "unknown", _node("generation", gid), gid, False,
                                   "缺少 generation opener"))
        for kind, field, identifier in present:
            collection = {"attempt": attempts, "failure": failures, "recovery": recoveries}[kind]
            target = _mapping_get(collection, identifier)
            resolved = target is not None and gid != 0 and len(present) == 1
            detail = None
            if target is None:
                _add_issue(issues, f"generation {gid}.{field} 引用不存在: {identifier}")
                detail = "opener 引用不存在"
            elif gid == 0:
                detail = "初始 generation 不应有 opener"
            elif generation.get("open_reason") == "possible_effect":
                if kind != "attempt":
                    _add_issue(issues, f"generation {gid} 的 possible_effect opener 必须是 attempt")
                    resolved = False
                    detail = "open_reason 与 opener 类型不一致"
                elif (target.get("generation_id") != gid
                      or target.get("pre_generation_id") != gid - 1
                      or target.get("effect_class") != "possible"
                      or target.get("handler_admitted") is not True):
                    _add_issue(issues, f"generation {gid} 的 opener attempt 未打开该 generation")
                    resolved = False
                    detail = "opener attempt 的 generation/effect 不一致"
            elif generation.get("open_reason") == "recovery":
                if kind != "recovery":
                    _add_issue(issues, f"generation {gid} 的 recovery opener 必须是 recovery")
                    resolved = False
                    detail = "open_reason 与 opener 类型不一致"
                elif (target.get("result_generation_id") != gid
                      or target.get("generation_id") != gid
                      or target.get("status") not in _ACCEPTED_RECOVERY_STATUSES):
                    _add_issue(issues, f"generation {gid} 的 opener recovery 未打开该 generation")
                    resolved = False
                    detail = "opener recovery 的结果 generation/status 不一致"
            else:
                _add_issue(issues, f"generation {gid} 的 open_reason 与 opener 不一致")
                resolved = False
                detail = "open_reason 与 opener 不一致"
            edges.append(_edge("generation_opener", _node(kind, identifier), _node("generation", gid), gid,
                               resolved, detail))

    for failure_id, failure in failures.items():
        attempt_id = failure.get("caused_by_attempt_id")
        attempt = _mapping_get(attempts, attempt_id)
        resolved = attempt is not None
        detail = None
        if attempt is None:
            _add_issue(issues, f"failure {failure_id}.caused_by_attempt_id 引用不存在: {attempt_id}")
            detail = "触发 attempt 不存在"
        elif attempt.get("generation_id") != failure.get("generation_id"):
            _add_issue(issues, f"failure {failure_id} 与触发 attempt 不属于同一 generation")
            resolved = False
            detail = "failure 与 attempt generation 不一致"
        elif attempt.get("failure_id") != failure_id:
            _add_issue(issues, f"failure {failure_id} 与触发 attempt 的反向引用不一致")
            resolved = False
            detail = "attempt.failure_id 未回链该 failure"
        edges.append(_edge("attempt_failure", _node("attempt", attempt_id), _node("failure", failure_id),
                           failure.get("generation_id"), resolved, detail))

    for recovery_id, recovery in recoveries.items():
        failure_id = recovery.get("caused_by_failure_id")
        resolved = _mapping_contains(failures, failure_id)
        if not resolved:
            _add_issue(issues, f"recovery {recovery_id}.caused_by_failure_id 引用不存在: {failure_id}")
        edges.append(_edge("failure_recovery", _node("failure", failure_id), _node("recovery", recovery_id),
                           recovery.get("generation_id"), resolved,
                           None if resolved else "recovery 的 failure 不存在"))

        if recovery.get("status") in _ACCEPTED_RECOVERY_STATUSES:
            result_generation = recovery.get("result_generation_id")
            generation_ok = _valid_generation(result_generation) and result_generation in generation_records
            if result_generation is None:
                _add_issue(issues, f"accepted recovery {recovery_id} 缺少后继 generation")
            elif not generation_ok:
                _add_issue(issues, f"accepted recovery {recovery_id} 的后继 generation 不存在: {result_generation}")
            if recovery.get("generation_id") != result_generation:
                _add_issue(issues, f"recovery {recovery_id} 的 generation 与 result_generation 不一致")
                generation_ok = False
            edges.append(_edge("recovery_successor", _node("recovery", recovery_id),
                               _node("generation", result_generation), result_generation,
                               generation_ok, None if generation_ok else "后继 generation 不可确认"))

            if recovery.get("action") not in {"ask", "block"}:
                attempt_id = recovery.get("result_attempt")
                result_attempt = _mapping_get(attempts, attempt_id)
                attempt_ok = result_attempt is not None
                if attempt_id is None:
                    _add_issue(issues, f"accepted recovery {recovery_id} 缺少 result attempt")
                elif result_attempt is None:
                    _add_issue(issues, f"recovery {recovery_id}.result_attempt 引用不存在: {attempt_id}")
                else:
                    if result_attempt.get("recovery_id") != recovery_id:
                        _add_issue(issues, f"recovery {recovery_id} 的 result attempt 未回链该 recovery")
                        attempt_ok = False
                    if result_attempt.get("generation_id") != result_generation:
                        _add_issue(issues, f"recovery {recovery_id} 与 result attempt 不属于同一 generation")
                        attempt_ok = False
                    if result_attempt.get("caused_by_failure_id") != failure_id:
                        _add_issue(issues, f"recovery {recovery_id} 的 result attempt 未回链触发 failure")
                        attempt_ok = False
                    if result_attempt.get("caused_by_attempt_id") is not None:
                        _add_issue(issues, f"recovery {recovery_id} 的 result attempt 存在多个因果前驱")
                        attempt_ok = False
                edges.append(_edge("recovery_result", _node("recovery", recovery_id),
                                   _node("attempt", attempt_id), result_generation, attempt_ok,
                                   None if attempt_ok else "result attempt 不可确认"))
            elif recovery.get("result_attempt") is not None:
                _add_issue(issues, f"ask/block recovery {recovery_id} 不应带 result attempt")

        elif any(recovery.get(field) is not None for field in ("result_generation_id", "result_attempt")):
            _add_issue(issues, f"rejected recovery {recovery_id} 不应带执行结果引用")

    for index, item in enumerate(evidence):
        attempt_id = item.get("caused_by_attempt_id")
        attempt = _mapping_get(attempts, attempt_id)
        resolved = attempt is not None
        detail = None
        if attempt is None:
            _add_issue(issues, f"verification[{index}] 缺少来源 attempt: {attempt_id}")
            detail = "verification 来源 attempt 不存在"
        elif attempt.get("generation_id") != item.get("generation_id"):
            _add_issue(issues, f"verification[{index}] 跨 generation 引用来源 attempt")
            resolved = False
            detail = "verification 与来源 attempt generation 不一致"
        else:
            arguments = attempt.get("redacted_arguments", {})
            if not (
                attempt.get("tool") == "run_shell"
                and isinstance(arguments, Mapping)
                and arguments.get("purpose") == "verification"
                and attempt.get("recovery_id") is None
                and attempt.get("handler_admitted") is True
            ):
                _add_issue(issues, f"verification[{index}] 的来源 attempt 不是获准的独立 verification")
                resolved = False
                detail = "来源 attempt 不是独立 verification"
        edges.append(_edge("attempt_verification", _node("attempt", attempt_id),
                           _node("verification", index), item.get("generation_id"), resolved, detail))

    # Plan edges are deliberately built from explicit IDs and ordered events.
    # Generation equality alone cannot establish which revision owned a fact.
    for revision_id, revision in plan_revisions.items():
        parent_id = revision.get("parent_revision_id")
        if parent_id is None:
            diff_ok = revision.get("diff") is None
            edges.append(_edge(
                "revision_structure", _node("revision", revision_id),
                _node("revision_structure", revision_id), revision.get("generation_id"),
                diff_ok, None if diff_ok else "初始 revision 的 diff 不可确认",
            ))
            continue
        resolved = _mapping_contains(plan_revisions, parent_id) and parent_id != revision_id
        detail = None if resolved else "parent revision 不存在"
        if not resolved:
            _add_issue(issues, f"plan revision {revision_id}.parent_revision_id 引用不存在: {parent_id}")
        edges.append(_edge(
            "revision_parent", _node("revision", parent_id), _node("revision", revision_id),
            revision.get("generation_id"), resolved, detail,
        ))
        parent = _mapping_get(plan_revisions, parent_id)
        diff_ok = parent is not None and _normalise_diff(revision.get("diff")) == _normalise_diff(
            _computed_plan_diff(parent, revision)
        )
        edges.append(_edge(
            "revision_structure", _node("revision", revision_id),
            _node("revision_structure", revision_id), revision.get("generation_id"),
            diff_ok, None if diff_ok else "保存的结构 diff 与快照不一致",
        ))
        steps = _safe_plan_steps(revision)
        step_ids = {step.get("step_id") for step in steps}
        dependency_graph = {
            step.get("step_id"): [dependency for dependency in step.get("depends_on", [])
                                   if isinstance(dependency, str)]
            for step in steps
        }
        cycle_nodes: set[Any] = set()
        visiting: list[Any] = []
        visited: set[Any] = set()
        def mark_cycle(step_id: Any) -> None:
            if step_id in visiting:
                cycle_nodes.update(visiting[visiting.index(step_id):])
                return
            if step_id in visited:
                return
            visiting.append(step_id)
            for dependency in dependency_graph.get(step_id, []):
                if dependency in dependency_graph:
                    mark_cycle(dependency)
            visiting.pop()
            visited.add(step_id)
        for step_id in dependency_graph:
            mark_cycle(step_id)
        for step in steps:
            step_id = step.get("step_id")
            dependencies = step.get("depends_on", [])
            if not isinstance(dependencies, list):
                dependencies = []
            for dependency in dependencies:
                dependency_ok = dependency in step_ids and step_id not in cycle_nodes and dependency not in cycle_nodes
                edges.append(_edge(
                    "step_dependency", _node("step", f"{revision_id}:{step_id}"),
                    _node("step", f"{revision_id}:{dependency}"), revision.get("generation_id"),
                    dependency_ok, None if dependency_ok else "步骤依赖缺失或存在环",
                ))

    event_targets = {
        (event.get("record_type"), event.get("record_id")): event
        for event in trace_events
        if isinstance(event, Mapping)
        and _hashable(event.get("record_type"))
        and _hashable(event.get("record_id"))
    }
    for trigger_id, trigger in triggers.items():
        source_type = None
        source_id = None
        collection = None
        if trigger.get("caused_by_failure_id") is not None:
            source_type, source_id, collection = "failure", trigger.get("caused_by_failure_id"), failures
        elif trigger.get("caused_by_attempt_id") is not None:
            source_type, source_id, collection = "attempt", trigger.get("caused_by_attempt_id"), attempts
        elif trigger.get("caused_by_decision_id") is not None:
            source_type, source_id, collection = "decision", trigger.get("caused_by_decision_id"), plan_decisions
        else:
            _add_issue(issues, f"trigger {trigger_id} 缺少来源")
        source = _mapping_get(collection, source_id) if collection is not None else None
        source_ok = source is not None
        if source_ok and source_type == "failure":
            source_ok = source.get("generation_id") == trigger.get("generation_id")
        elif source_ok and source_type == "attempt":
            source_ok = source.get("generation_id") == trigger.get("generation_id")
            source_ok = source_ok and source.get("outcome") == "succeeded"
            arguments = source.get("redacted_arguments", {})
            source_ok = source_ok and source.get("handler_admitted") is True
            source_ok = source_ok and source.get("permission") == "allowed"
            source_ok = source_ok and source.get("effect_class") == "none"
            source_ok = source_ok and source.get("tool") not in {
                "begin_plan", "cancel_planning", "commit_plan", "update_plan_progress",
                "request_replan", "recover", "rollback_checkpoint",
            }
            source_ok = source_ok and not (
                source.get("tool") == "run_shell"
                and isinstance(arguments, Mapping)
                and arguments.get("purpose") == "verification"
            )
        elif source_ok and source_type == "decision":
            source_ok = source.get("generation_id") == trigger.get("generation_id")
            source_ok = source_ok and source.get("decision") in {
                "rejected", "continue_exploring", "resume_blocked",
            }
        if source_type is not None:
            if not source_ok:
                _add_issue(issues, f"trigger {trigger_id} 的 {source_type} 来源不存在或类型不匹配")
            edges.append(_edge(
                "trigger_source",
                _node(source_type, source_id), _node("trigger", trigger_id),
                trigger.get("generation_id"), source_ok,
                None if source_ok else "trigger 来源不可确认",
            ))
        result_revision_id = trigger.get("result_revision_id")
        if result_revision_id is not None and not _mapping_contains(plan_revisions, result_revision_id):
            edges.append(_edge(
                "trigger_revision", _node("trigger", trigger_id),
                _node("revision", result_revision_id), trigger.get("generation_id"), False,
                "trigger 的 result revision 不存在",
            ))

    consumed_by: dict[int, list[int]] = {}
    for revision_id, revision in plan_revisions.items():
        trigger_id = revision.get("trigger_id")
        if trigger_id is None:
            continue
        if not _hashable(trigger_id):
            _add_issue(issues, f"revision {revision_id}.trigger_id 不是可定位的 ID")
            edges.append(_edge(
                "trigger_revision", _node("trigger", trigger_id), _node("revision", revision_id),
                revision.get("generation_id"), False, "revision 的 trigger ID 不可定位",
            ))
            continue
        consumed_by.setdefault(trigger_id, []).append(revision_id)
        trigger = _mapping_get(triggers, trigger_id)
        resolved = (
            trigger is not None
            and trigger.get("status") == "resolved"
            and trigger.get("result_revision_id") == revision_id
        )
        if not resolved:
            _add_issue(issues, f"revision {revision_id}.trigger_id 消费不可确认: {trigger_id}")
        edges.append(_edge(
            "trigger_revision", _node("trigger", trigger_id), _node("revision", revision_id),
            revision.get("generation_id"), resolved,
            None if resolved else "revision 的 trigger 反向引用不可确认",
        ))
    for trigger_id, revision_ids in consumed_by.items():
        if len(revision_ids) > 1:
            _add_issue(issues, f"trigger {trigger_id} 被多个 revision 消费: {revision_ids}")
            for revision_id in revision_ids:
                for edge in edges:
                    if edge.get("type") == "trigger_revision" and edge.get("to") == _node("revision", revision_id):
                        edge["resolved"] = False
                        edge["status"] = "unresolved"
                        edge["detail"] = "一个 trigger 被多个 revision 消费"

    for progress_id, progress in plan_progress.items():
        revision_id = progress.get("revision_id")
        event = event_targets.get(("plan_progress", progress_id))
        resolved = (
            _mapping_contains(plan_revisions, revision_id)
            and event is not None
            and event.get("revision_id") == revision_id
            and event.get("generation_id") == progress.get("generation_id")
        )
        if event is None:
            _add_issue(issues, f"progress {progress_id} 缺少顺序事件，revision 归属不可确定")
        if not resolved:
            _add_issue(issues, f"progress {progress_id} 的 revision 引用或顺序不可确认")
        edges.append(_edge(
            "revision_progress", _node("revision", revision_id), _node("progress", progress_id),
            progress.get("generation_id"), resolved,
            None if resolved else "progress 的 revision 归属不可确认",
        ))
    for decision_id, decision in plan_decisions.items():
        revision_id = decision.get("revision_id")
        if revision_id is None:
            continue
        event = event_targets.get(("user_plan_decision", decision_id))
        resolved = (
            _mapping_contains(plan_revisions, revision_id)
            and event is not None
            and event.get("revision_id") == revision_id
            and event.get("generation_id") == decision.get("generation_id")
        )
        if not resolved:
            _add_issue(issues, f"decision {decision_id} 的 revision 归属不可确认")
        edges.append(_edge(
            "revision_decision", _node("revision", revision_id), _node("decision", decision_id),
            decision.get("generation_id"), resolved,
            None if resolved else "decision 的 revision 归属不可确认",
        ))

    fact_events = {
        "attempt": {"execution_result", "tool_history"},
        "recovery_action": {
            "recovery_proposed", "recovery_activated", "recovery_result", "recovery_rejected",
        },
        "verification_history": {"verification_recorded"},
    }
    for event in trace_events:
        revision_id = event.get("revision_id")
        record_type = event.get("record_type")
        if not _mapping_contains(plan_revisions, revision_id) or not _mapping_contains(fact_events, record_type):
            continue
        if event.get("kind") not in _mapping_get(fact_events, record_type, set()):
            continue
        record_id = event.get("record_id")
        if record_type == "attempt":
            exists = _mapping_contains(attempts, record_id)
        elif record_type == "recovery_action":
            exists = _mapping_contains(recoveries, record_id)
        else:
            exists = isinstance(record_id, int) and 0 <= record_id < len(evidence)
        if not exists:
            _add_issue(issues, f"trace event {event.get('sequence_id')} 的事实引用不存在")
        edges.append(_edge(
            f"revision_{'verification' if record_type == 'verification_history' else record_type.replace('_action', '')}",
            _node("revision", revision_id), _node(
                "verification" if record_type == "verification_history" else record_type.split("_")[0], record_id,
            ), event.get("generation_id"), exists,
            None if exists else "事件事实引用不存在",
        ))
    target_kind = {
        "plan_revision": "revision", "plan_progress": "progress",
        "user_plan_decision": "decision", "replan_trigger": "trigger",
        "attempt": "attempt", "failure": "failure",
        "recovery_action": "recovery", "verification_history": "verification",
        "tool_history": "tool_history",
    }
    for event in trace_events:
        record_type = event.get("record_type")
        if not _mapping_contains(target_kind, record_type):
            continue
        record_id = event.get("record_id")
        if record_type == "plan_revision":
            exists = _mapping_contains(plan_revisions, record_id)
        elif record_type == "plan_progress":
            exists = _mapping_contains(plan_progress, record_id)
        elif record_type == "user_plan_decision":
            exists = _mapping_contains(plan_decisions, record_id)
        elif record_type == "replan_trigger":
            exists = _mapping_contains(triggers, record_id)
        elif record_type == "attempt":
            exists = _mapping_contains(attempts, record_id)
        elif record_type == "failure":
            exists = _mapping_contains(failures, record_id)
        elif record_type == "recovery_action":
            exists = _mapping_contains(recoveries, record_id)
        elif record_type == "tool_history":
            exists = isinstance(record_id, int) and not isinstance(record_id, bool) and 0 <= record_id < len(tool_history)
        else:
            exists = isinstance(record_id, int) and 0 <= record_id < len(evidence)
        if not exists:
            edges.append(_edge(
                "trace_event_reference", _node("trace_event", event.get("sequence_id")),
                _node(target_kind[record_type], record_id), event.get("generation_id"), False,
                "事件目标记录不存在",
            ))
    ordered_sequences = sorted(
        event.get("sequence_id") for event in trace_events
        if _valid_positive_id(event.get("sequence_id"))
    )
    for previous, current in zip(ordered_sequences, ordered_sequences[1:]):
        if current != previous + 1:
            _add_issue(issues, "trace event sequence_id 不连续")
            edges.append(_edge(
                "trace_sequence", _node("trace_event", previous),
                _node("trace_event", current), None, False,
                "事件序号存在缺口",
            ))
    return edges


def _build_generation_trace(snapshot: Mapping[str, Any], generation_id: int | None = None) -> dict[str, Any]:
    """Build a read-only replay report from one ``AgentState.snapshot``.

    Only an invalid query raises ``TraceQueryError``.  Damaged records become
    integrity issues and remain visible in the returned report.
    """
    if not isinstance(snapshot, Mapping):
        raise TraceQueryError("snapshot 必须是 mapping")
    if generation_id is not None and not _valid_generation(generation_id):
        raise TraceQueryError("generation_id 必须是非负整数或 None")

    issues: list[str] = []
    raw_generations = _records(snapshot, "generations", issues)
    raw_attempts = _records(snapshot, "attempts", issues)
    raw_failures = _records(snapshot, "failures", issues)
    raw_recoveries = _records(snapshot, "recovery_actions", issues)
    raw_current_evidence = _records(snapshot, "verification_evidence", issues)
    raw_evidence = (
        _records(snapshot, "verification_history", issues)
        if "verification_history" in snapshot else raw_current_evidence
    )
    raw_revisions = _records(snapshot, "todo_revisions", issues)
    raw_checkpoints = _records(snapshot, "checkpoints", issues)

    generations = [_safe_record(item, _GENERATION_FIELDS, issues, f"generations[{i}]")
                   for i, item in enumerate(raw_generations)]
    attempts_list = [_safe_record(item, _ATTEMPT_FIELDS, issues, f"attempts[{i}]")
                     for i, item in enumerate(raw_attempts)]
    failures_list = [_safe_record(item, _FAILURE_FIELDS, issues, f"failures[{i}]")
                     for i, item in enumerate(raw_failures)]
    recoveries_list = [_safe_record(item, _RECOVERY_FIELDS, issues, f"recovery_actions[{i}]")
                       for i, item in enumerate(raw_recoveries)]
    evidence_list = [_safe_record(item, _VERIFICATION_FIELDS, issues, f"verification_evidence[{i}]")
                     for i, item in enumerate(raw_evidence)]
    current_evidence_list = [
        _safe_record(item, _VERIFICATION_FIELDS, issues, f"current_verification_evidence[{i}]")
        for i, item in enumerate(raw_current_evidence)
    ]
    revisions_list = [_safe_record(item, _TODO_REVISION_FIELDS, issues, f"todo_revisions[{i}]")
                      for i, item in enumerate(raw_revisions)]
    checkpoints_list = [_safe_checkpoint(item, issues, f"checkpoints[{i}]")
                        for i, item in enumerate(raw_checkpoints)]

    generation_map: dict[int, dict[str, Any]] = {}
    for index, record in enumerate(generations):
        gid = record.get("generation_id")
        if not _valid_generation(gid):
            _add_issue(issues, f"generation[{index}] 的 generation_id 非法: {gid}")
            continue
        if gid in generation_map:
            _add_issue(issues, f"generation ID 重复: {gid}")
            continue
        if record.get("open_reason") not in {"task_start", "possible_effect", "recovery"}:
            _add_issue(issues, f"generation {gid}.open_reason 非法: {record.get('open_reason')}")
        if gid == 0 and record.get("open_reason") != "task_start":
            _add_issue(issues, "初始 generation 0 的 open_reason 必须是 task_start")
        if gid != 0 and record.get("open_reason") == "task_start":
            _add_issue(issues, f"非初始 generation {gid} 不能使用 task_start")
        generation_map[gid] = record
    generation_ids = sorted(generation_map)
    if generation_ids:
        if generation_ids[0] != 0:
            _add_issue(issues, "generation 序列缺少初始 generation 0")
        expected = list(range(generation_ids[0], generation_ids[-1] + 1))
        if generation_ids != expected:
            _add_issue(issues, "generation 序列不连续")

    attempt_map = _id_map(attempts_list, "attempt_id", "attempt", issues)
    failure_map = _id_map(failures_list, "failure_id", "failure", issues)
    recovery_map = _id_map(recoveries_list, "recovery_id", "recovery", issues)
    checkpoint_map = _id_map(checkpoints_list, "checkpoint_id", "checkpoint", issues)
    revision_ids: set[int] = set()
    for index, revision in enumerate(revisions_list):
        revision_id = revision.get("revision_id")
        if isinstance(revision_id, int) and not isinstance(revision_id, bool):
            if revision_id in revision_ids:
                _add_issue(issues, f"Todo revision ID 重复: {revision_id}")
            revision_ids.add(revision_id)

    for index, record in enumerate(attempts_list):
        owner = f"attempt {record.get('attempt_id', index)}"
        if record.get("outcome") not in {"succeeded", "failed", "denied", "timeout", "invalid"}:
            _add_issue(issues, f"{owner}.outcome 非法: {record.get('outcome')}")
        for field in ("pre_generation_id", "generation_id"):
            value = record.get(field)
            if not _valid_generation(value):
                _add_issue(issues, f"{owner}.{field} 非法: {value}")
            elif value not in generation_map:
                _add_issue(issues, f"{owner}.{field} 引用不存在: {value}")
        for field, collection in (("caused_by_attempt_id", attempt_map),
                                  ("caused_by_failure_id", failure_map),
                                  ("failure_id", failure_map),
                                  ("recovery_id", recovery_map)):
            _ref_issue(issues, owner, field, record.get(field), collection)
        _ref_issue(issues, owner, "checkpoint_id", record.get("checkpoint_id"), checkpoint_map)
        causal_fields = [
            field for field in ("caused_by_attempt_id", "caused_by_failure_id")
            if record.get(field) is not None
        ]
        if causal_fields and len(causal_fields) != 1:
            _add_issue(issues, f"{owner} 的 caused_by_* 因果前驱不唯一")
        failure_id = record.get("failure_id")
        if failure_id in failure_map:
            failure = failure_map[failure_id]
            if (failure.get("caused_by_attempt_id") != record.get("attempt_id")
                    or failure.get("generation_id") != record.get("generation_id")):
                _add_issue(issues, f"{owner}.failure_id 与 FailureEvent 反向引用不一致")
        recovery_id = record.get("recovery_id")
        if recovery_id in recovery_map:
            recovery = recovery_map[recovery_id]
            if (recovery.get("result_attempt") != record.get("attempt_id")
                    or recovery.get("result_generation_id") != record.get("generation_id")
                    or record.get("caused_by_failure_id") != recovery.get("caused_by_failure_id")):
                _add_issue(issues, f"{owner} 与 RecoveryAction 结果引用不一致")
    for index, record in enumerate(failures_list):
        owner = f"failure {record.get('failure_id', index)}"
        if record.get("phase") not in {"execute", "verify", "recover"}:
            _add_issue(issues, f"{owner}.phase 非法: {record.get('phase')}")
        if record.get("category") not in {"protocol", "permission", "transient", "deterministic", "validation", "unknown"}:
            _add_issue(issues, f"{owner}.category 非法: {record.get('category')}")
        value = record.get("generation_id")
        if not _valid_generation(value) or value not in generation_map:
            _add_issue(issues, f"{owner}.generation_id 引用不存在或非法: {value}")
        _ref_issue(issues, owner, "caused_by_attempt_id", record.get("caused_by_attempt_id"), attempt_map)
        attempt = attempt_map.get(record.get("caused_by_attempt_id"))
        if attempt is not None and attempt.get("failure_id") != record.get("failure_id"):
            _add_issue(issues, f"{owner} 与触发 attempt 的 failure_id 反向引用不一致")
    for index, record in enumerate(recoveries_list):
        owner = f"recovery {record.get('recovery_id', index)}"
        if record.get("status") not in {"proposed", "reserved", "executed", "rejected", "terminal"}:
            _add_issue(issues, f"{owner}.status 非法: {record.get('status')}")
        value = record.get("generation_id")
        if not _valid_generation(value) or value not in generation_map:
            _add_issue(issues, f"{owner}.generation_id 引用不存在或非法: {value}")
        _ref_issue(issues, owner, "caused_by_failure_id", record.get("caused_by_failure_id"), failure_map)
        _ref_issue(issues, owner, "result_attempt", record.get("result_attempt"), attempt_map)
        _ref_issue(issues, owner, "checkpoint_id", record.get("checkpoint_id"), checkpoint_map)
        result_generation = record.get("result_generation_id")
        if result_generation is not None and (not _valid_generation(result_generation) or result_generation not in generation_map):
            _add_issue(issues, f"{owner}.result_generation_id 引用不存在或非法: {result_generation}")
    for index, item in enumerate(evidence_list):
        owner = f"verification[{index}]"
        value = item.get("generation_id")
        if not _valid_generation(value) or value not in generation_map:
            _add_issue(issues, f"{owner}.generation_id 引用不存在或非法: {value}")
        _ref_issue(issues, owner, "caused_by_attempt_id", item.get("caused_by_attempt_id"), attempt_map)
        attempt = attempt_map.get(item.get("caused_by_attempt_id"))
        if attempt is not None:
            arguments = attempt.get("redacted_arguments", {})
            is_verification = (
                attempt.get("tool") == "run_shell"
                and isinstance(arguments, Mapping)
                and arguments.get("purpose") == "verification"
                and attempt.get("recovery_id") is None
                and attempt.get("handler_admitted") is True
            )
            if not is_verification:
                _add_issue(issues, f"{owner} 的来源 attempt 不是获准的独立 verification")
            expected_outcome = (
                "passed"
                if attempt.get("outcome") == "succeeded" and attempt.get("exit_code") == 0
                else "failed"
            )
            if item.get("outcome") != expected_outcome or item.get("exit_code") != attempt.get("exit_code"):
                _add_issue(issues, f"{owner} 与来源 attempt 的结果不一致")
    for index, revision in enumerate(revisions_list):
        value = revision.get("generation_id")
        if not _valid_generation(value) or value not in generation_map:
            _add_issue(issues, f"todo_revisions[{index}].generation_id 引用不存在或非法: {value}")
        if not isinstance(revision.get("revision_id"), int) or isinstance(revision.get("revision_id"), bool):
            _add_issue(issues, f"todo_revisions[{index}].revision_id 非法: {revision.get('revision_id')}")
    for index, checkpoint in enumerate(checkpoints_list):
        value = checkpoint.get("generation_id")
        if value is not None and (not _valid_generation(value) or value not in generation_map):
            _add_issue(issues, f"checkpoint[{index}].generation_id 引用不存在或非法: {value}")

    evidence_by_attempt: dict[str, list[dict[str, Any]]] = {}
    for item in evidence_list:
        attempt_id = item.get("caused_by_attempt_id")
        if isinstance(attempt_id, str):
            evidence_by_attempt.setdefault(attempt_id, []).append(item)
    for attempt_id, attempt in attempt_map.items():
        arguments = attempt.get("redacted_arguments", {})
        is_verification = (
            attempt.get("tool") == "run_shell"
            and isinstance(arguments, Mapping)
            and arguments.get("purpose") == "verification"
            and attempt.get("recovery_id") is None
            and attempt.get("handler_admitted") is True
        )
        if is_verification:
            count = len(evidence_by_attempt.get(attempt_id, []))
            if count != 1:
                _add_issue(issues, f"verification attempt {attempt_id} 必须有且只有一条历史证据，实际为 {count}")

    for index, record in enumerate(generations):
        gid = record.get("generation_id", index)
        if not _valid_generation(gid):
            continue
        for field, collection in (("opened_by_attempt_id", attempt_map),
                                  ("opened_by_failure_id", failure_map),
                                  ("opened_by_recovery_id", recovery_map)):
            _ref_issue(issues, f"generation {gid}", field, record.get(field), collection)

    current_generation = snapshot.get("current_generation_id")
    if current_generation is not None and (
        not _valid_generation(current_generation) or current_generation not in generation_map
    ):
        _add_issue(issues, f"current_generation_id 非法或不存在: {current_generation}")
    if snapshot.get("status") not in _STATE_STATUSES:
        _add_issue(issues, f"status 非法: {snapshot.get('status')}")

    all_edges = _build_edges(generation_map, attempt_map, failure_map, recovery_map, evidence_list, issues)

    # Completion evidence intentionally retains only the current generation;
    # the append-only history above is allowed to span every generation.
    if _valid_generation(current_generation):
        for index, item in enumerate(current_evidence_list):
            if item.get("generation_id") != current_generation:
                _add_issue(issues, f"current verification[{index}] 复用了旧 generation 证据")
            if item not in evidence_list:
                _add_issue(issues, f"current verification[{index}] 不存在于 verification history")

    referenced_generation_ids: set[int] = set(generation_ids)
    for collection in (attempts_list, failures_list, recoveries_list, evidence_list, revisions_list):
        for record in collection:
            for field in ("generation_id", "pre_generation_id", "result_generation_id"):
                value = record.get(field)
                if _valid_generation(value):
                    referenced_generation_ids.add(value)
    display_ids = sorted(referenced_generation_ids)
    if generation_id is not None:
        if generation_id not in generation_map:
            raise TraceQueryError(f"generation 不存在: {generation_id}")
        display_ids = [generation_id]

    failure_diagnosis: dict[str, dict[str, Any]] = {}
    for failure in failures_list:
        failure_id = failure.get("failure_id")
        cause_hint = failure.get("cause_hint")
        if isinstance(cause_hint, str) and cause_hint.strip():
            diagnosis, source = cause_hint, "cause_hint"
        else:
            related = next((item for item in recoveries_list
                            if item.get("caused_by_failure_id") == failure_id and item.get("reason")), None)
            if related is not None:
                diagnosis, source = related.get("reason", ""), "recovery.reason"
            else:
                diagnosis, source = "未记录诊断", "none"
        failure_diagnosis[failure_id] = {
            "diagnosis": _safe_text(diagnosis),
            "diagnosis_source": source,
        }

    generations_report: list[dict[str, Any]] = []
    for gid in display_ids:
        generation = deepcopy(generation_map.get(gid)) if gid in generation_map else None
        grouped_attempts = [deepcopy(item) for item in attempts_list if item.get("generation_id") == gid]
        grouped_failures = []
        for item in failures_list:
            if item.get("generation_id") == gid:
                enriched = deepcopy(item)
                enriched.update(failure_diagnosis.get(item.get("failure_id"), {}))
                grouped_failures.append(enriched)
        grouped_recoveries = [deepcopy(item) for item in recoveries_list if item.get("generation_id") == gid]
        grouped_evidence = [deepcopy(item) for item in evidence_list if item.get("generation_id") == gid]
        grouped_revisions = [deepcopy(item) for item in revisions_list if item.get("generation_id") == gid]
        local_edges = [deepcopy(edge) for edge in all_edges
                       if edge.get("generation_id") == gid
                       or edge.get("from") == _node("generation", gid)
                       or edge.get("to") == _node("generation", gid)]
        failures_until_generation = [
            item for item in failures_list
            if _valid_generation(item.get("generation_id")) and item.get("generation_id") <= gid
        ]
        conclusion = _conclusion(
            snapshot, gid, display_ids, generation_ids,
            failures_until_generation or grouped_failures,
        )
        generations_report.append({
            "generation_id": gid,
            "recorded": generation is not None,
            "generation": generation,
            "todo_revisions": grouped_revisions,
            "attempts": grouped_attempts,
            "failures": grouped_failures,
            "recovery_actions": grouped_recoveries,
            "verification_evidence": grouped_evidence,
            "causal_edges": local_edges,
            "edges": local_edges,
            "conclusion": conclusion,
        })

    if not generations_report and generation_id is None and not generation_map:
        _add_issue(issues, "缺少 generation 记录")

    final_conclusion = generations_report[-1]["conclusion"] if generations_report else {
        "status": "continue" if snapshot.get("status") not in {"blocked", "failed", "done"} else snapshot.get("status"),
    }
    if final_conclusion.get("status") in {"blocked", "failed"} and "terminal_reason" not in final_conclusion:
        final_conclusion["terminal_reason"] = _safe_text(snapshot.get("terminal_reason", ""))
        final_conclusion["last_failure"] = deepcopy(failures_list[-1]) if failures_list else None

    task_info = {
        "task": _safe_text(snapshot.get("task", "")),
        "current_goal": _safe_text(snapshot.get("current_goal", "")),
        "status": snapshot.get("status", "running"),
        "terminal_reason": _safe_text(snapshot.get("terminal_reason", "")),
    }
    query = {
        "generation_id": generation_id,
        "scope": "all" if generation_id is None else "generation",
        "generation_ids": display_ids,
    }
    report = {
        "task": task_info["task"],
        "current_goal": task_info["current_goal"],
        "task_info": task_info,
        "query": query,
        "generations": generations_report,
        "causal_edges": all_edges if generation_id is None else [
            deepcopy(edge) for edge in all_edges
            if edge.get("generation_id") == generation_id
            or edge.get("from") == _node("generation", generation_id)
            or edge.get("to") == _node("generation", generation_id)
        ],
        "conclusion": final_conclusion,
        "integrity": {
            "status": "complete" if not issues else "incomplete",
            "issues": issues,
        },
        "checkpoints": checkpoints_list,
        "rollback_checkpoints": [
            deepcopy(item) for item in checkpoints_list if item.get("status") == "ready"
        ],
        "budgets": _jsonish(snapshot.get("budgets", {})),
    }
    return report


def build_trace(
    snapshot: Mapping[str, Any],
    generation_id: int | None = None,
    *,
    revision_id: int | None = None,
) -> dict[str, Any]:
    """Build a read-only generation and plan replay report.

    The generation implementation above remains the compatibility layer for
    v0.21 snapshots.  This wrapper adds plan facts only when the snapshot has
    the v0.22+ public plan records, and uses ``trace_events`` for every
    cross-list ownership decision.
    """
    if not isinstance(snapshot, Mapping):
        raise TraceQueryError("snapshot 必须是 mapping")
    if generation_id is not None and not _valid_generation(generation_id):
        raise TraceQueryError("generation_id 必须是非负整数或 None")
    if revision_id is not None and not _valid_positive_id(revision_id):
        raise TraceQueryError("revision_id 必须是正整数或 None")
    if generation_id is not None and revision_id is not None:
        raise TraceQueryError("generation_id 与 revision_id 不能同时指定")

    issues: list[str] = []
    # Always validate the complete snapshot first.  A generation query narrows
    # the view after validation so cross-generation plan links remain visible
    # as unresolved/resolved causal edges instead of looking like missing data.
    base = _build_generation_trace(snapshot, None)
    issues.extend(base.get("integrity", {}).get("issues", []))
    if generation_id is not None and generation_id not in {
        item.get("generation_id") for item in base.get("generations", [])
        if isinstance(item, Mapping)
    }:
        raise TraceQueryError(f"generation 不存在: {generation_id}")

    def raw_records(key: str) -> list[Any]:
        value = snapshot.get(key, [])
        if not isinstance(value, (list, tuple)):
            _add_issue(issues, f"{key} 不是数组")
            return []
        return list(value)

    has_plan_key = "plan_revisions" in snapshot
    raw_plan = raw_records("plan_revisions") if has_plan_key else []
    raw_progress = raw_records("plan_progress_history") if "plan_progress_history" in snapshot else []
    raw_decisions = raw_records("user_plan_decisions") if "user_plan_decisions" in snapshot else []
    raw_triggers = raw_records("replan_triggers") if "replan_triggers" in snapshot else []
    raw_events = raw_records("trace_events") if "trace_events" in snapshot else []

    plan_records = [
        _safe_plan_revision(item, issues, f"plan_revisions[{index}]")
        for index, item in enumerate(raw_plan)
    ]
    progress_records = [
        _safe_record(item, _PLAN_PROGRESS_FIELDS, issues, f"plan_progress_history[{index}]")
        for index, item in enumerate(raw_progress)
    ]
    decision_records = [
        _safe_record(item, _PLAN_DECISION_FIELDS, issues, f"user_plan_decisions[{index}]")
        for index, item in enumerate(raw_decisions)
    ]
    trigger_records = [
        _safe_record(item, _TRIGGER_FIELDS, issues, f"replan_triggers[{index}]")
        for index, item in enumerate(raw_triggers)
    ]
    event_records = [
        _safe_trace_event(item, issues, f"trace_events[{index}]")
        for index, item in enumerate(raw_events)
    ]

    plan_map: dict[int, dict[str, Any]] = {}
    for index, record in enumerate(plan_records):
        identifier = record.get("revision_id")
        if not _valid_positive_id(identifier):
            _add_issue(issues, f"plan_revisions[{index}].revision_id 非法: {identifier}")
            continue
        if identifier in plan_map:
            _add_issue(issues, f"plan revision ID 重复: {identifier}")
            continue
        plan_map[identifier] = record

    progress_map: dict[int, dict[str, Any]] = {}
    for index, record in enumerate(progress_records):
        identifier = record.get("progress_id")
        if not _valid_positive_id(identifier):
            _add_issue(issues, f"plan_progress_history[{index}].progress_id 非法: {identifier}")
            continue
        if identifier in progress_map:
            _add_issue(issues, f"plan progress ID 重复: {identifier}")
            continue
        progress_map[identifier] = record

    decision_map: dict[int, dict[str, Any]] = {}
    for index, record in enumerate(decision_records):
        identifier = record.get("decision_id")
        if not _valid_positive_id(identifier):
            _add_issue(issues, f"user_plan_decisions[{index}].decision_id 非法: {identifier}")
            continue
        if identifier in decision_map:
            _add_issue(issues, f"plan decision ID 重复: {identifier}")
            continue
        decision_map[identifier] = record

    trigger_map: dict[int, dict[str, Any]] = {}
    for index, record in enumerate(trigger_records):
        identifier = record.get("trigger_id")
        if not _valid_positive_id(identifier):
            _add_issue(issues, f"replan_triggers[{index}].trigger_id 非法: {identifier}")
            continue
        if identifier in trigger_map:
            _add_issue(issues, f"trigger ID 重复: {identifier}")
            continue
        trigger_map[identifier] = record

    # A plan-bearing v0.22/v0.24 snapshot without v0.25 events can still
    # expose its immutable structure.  Cross-record ordering and ownership
    # remain explicitly incomplete instead of being reconstructed from list
    # positions or generation numbers.
    has_plan_data = bool(plan_records or progress_records or decision_records or trigger_records)
    if has_plan_data and "trace_events" not in snapshot:
        _add_issue(issues, "计划记录缺少 trace_events，跨记录先后与执行归属不可确定")

    generation_map = {
        item.get("generation_id"): item
        for item in (generation.get("generation") for generation in base.get("generations", []))
        if isinstance(item, Mapping) and _valid_generation(item.get("generation_id"))
    }
    attempts: dict[str, dict[str, Any]] = {}
    failures: dict[str, dict[str, Any]] = {}
    recoveries: dict[str, dict[str, Any]] = {}
    evidence: list[dict[str, Any]] = []
    raw_tool_history = snapshot.get("tool_history", [])
    tool_history: list[dict[str, Any]] = []
    if isinstance(raw_tool_history, (list, tuple)):
        for item in raw_tool_history:
            if not isinstance(item, Mapping):
                tool_history.append({})
                continue
            tool_history.append({
                "tool": _safe_text(item.get("tool", "")),
                "ok": bool(item.get("ok", False)),
                "brief": _safe_text(item.get("brief", "")),
            })
    for generation in base.get("generations", []):
        if not isinstance(generation, Mapping):
            continue
        for item in generation.get("attempts", []) or []:
            if isinstance(item, Mapping) and isinstance(item.get("attempt_id"), str):
                attempts.setdefault(item["attempt_id"], item)
        for item in generation.get("failures", []) or []:
            if isinstance(item, Mapping) and isinstance(item.get("failure_id"), str):
                failures.setdefault(item["failure_id"], item)
        for item in generation.get("recovery_actions", []) or []:
            if isinstance(item, Mapping) and isinstance(item.get("recovery_id"), str):
                recoveries.setdefault(item["recovery_id"], item)
        for item in generation.get("verification_evidence", []) or []:
            if isinstance(item, Mapping):
                evidence.append(item)
    # The base report is grouped by generation; use the public history again
    # so event record_id keeps its documented verification_history index.
    raw_history = snapshot.get("verification_history", snapshot.get("verification_evidence", []))
    if isinstance(raw_history, (list, tuple)):
        evidence = [
            _safe_record(item, _VERIFICATION_FIELDS, issues, f"verification_history[{index}]")
            for index, item in enumerate(raw_history)
        ]

    # Validate event order and direct pointers before using events for plan
    # attribution.  A target's generation is directly checkable even when its
    # surrounding ordering is damaged.
    event_by_target: dict[tuple[str, Any], list[dict[str, Any]]] = {}
    sequence_ids: list[int] = []
    target_maps: dict[str, Mapping[Any, Any] | Sequence[Any]] = {
        "plan_revision": plan_map,
        "plan_progress": progress_map,
        "user_plan_decision": decision_map,
        "replan_trigger": trigger_map,
        "attempt": attempts,
        "failure": failures,
        "recovery_action": recoveries,
        "verification_history": evidence,
        "tool_history": tool_history,
    }
    for index, event in enumerate(event_records):
        sequence_id = event.get("sequence_id")
        if not _valid_positive_id(sequence_id):
            _add_issue(issues, f"trace event[{index}] sequence_id 非法: {sequence_id}")
        else:
            sequence_ids.append(sequence_id)
        event_generation = event.get("generation_id")
        if not _valid_generation(event_generation) or not _mapping_contains(generation_map, event_generation):
            _add_issue(issues, f"trace event[{index}] generation_id 引用不存在或非法: {event_generation}")
        event_revision = event.get("revision_id")
        if event_revision is not None and not _mapping_contains(plan_map, event_revision):
            _add_issue(issues, f"trace event[{index}] revision_id 引用不存在: {event_revision}")
        record_type = event.get("record_type")
        record_id = event.get("record_id")
        if record_type is not None:
            available = _mapping_get(target_maps, record_type)
            target = None
            if available is not None:
                if isinstance(available, Mapping):
                    target = _mapping_get(available, record_id)
                elif isinstance(record_id, int) and not isinstance(record_id, bool) and 0 <= record_id < len(available):
                    target = available[record_id]
            if target is None:
                _add_issue(issues, f"trace event[{index}] {record_type} 引用不存在: {record_id}")
            else:
                if _hashable(record_type) and _hashable(record_id):
                    event_by_target.setdefault((record_type, record_id), []).append(event)
                target_generation = target.get("generation_id") if isinstance(target, Mapping) else None
                # Recovery events point at the final RecoveryAction record;
                # all recorded recovery state transitions after activation use
                # that successor generation.
                proposal_transition = (
                    record_type == "recovery_action"
                    and event.get("kind") == "recovery_proposed"
                    and isinstance(event_generation, int)
                    and not isinstance(event_generation, bool)
                    and target_generation == event_generation + 1
                )
                if target_generation is not None and event_generation != target_generation and not proposal_transition:
                    _add_issue(issues, f"trace event[{index}] generation 与目标 {record_type} 不一致")
            if available is None:
                _add_issue(issues, f"trace event[{index}] record_type 非法: {record_type}")
        elif record_id is not None:
            _add_issue(issues, f"trace event[{index}] 无 record_type 却带 record_id")
    if sequence_ids:
        if len(set(sequence_ids)) != len(sequence_ids):
            _add_issue(issues, "trace event sequence_id 重复")
        if sorted(sequence_ids) != list(range(1, max(sequence_ids) + 1)):
            _add_issue(issues, "trace event sequence_id 不连续")

    for event in event_records:
        for field in ("planning_phase_before", "planning_phase_after"):
            if (event.get(field) is not None
                    and (not isinstance(event.get(field), str) or event.get(field) not in {
                "direct", "exploring", "awaiting_approval", "executing",
            })):
                _add_issue(issues, f"trace event {event.get('sequence_id')}.{field} 非法")
        for field in ("repair_phase_before", "repair_phase_after"):
            if (event.get(field) is not None
                    and (not isinstance(event.get(field), str) or event.get(field) not in {
                "idle", "diagnosis_required", "verification_required",
            })):
                _add_issue(issues, f"trace event {event.get('sequence_id')}.{field} 非法")

    commit_events: dict[int, dict[str, Any]] = {}
    for event in event_records:
        if (event.get("kind") == "plan_committed"
                and event.get("record_type") == "plan_revision"
                and _mapping_contains(plan_map, event.get("record_id"))):
            revision_id_value = event["record_id"]
            if revision_id_value in commit_events:
                _add_issue(issues, f"revision {revision_id_value} 有多个提交事件")
            else:
                commit_events[revision_id_value] = event
    if has_plan_data and "trace_events" in snapshot:
        for revision_id_value in plan_map:
            if revision_id_value not in commit_events:
                _add_issue(issues, f"revision {revision_id_value} 缺少 plan_committed 顺序事件")

    # Plan Contract structure and parent graph.
    for revision_id_value, revision in plan_map.items():
        parent_id = revision.get("parent_revision_id")
        if parent_id is not None:
            if not _valid_positive_id(parent_id) or not _mapping_contains(plan_map, parent_id):
                _add_issue(issues, f"revision {revision_id_value}.parent_revision_id 引用不存在: {parent_id}")
            elif parent_id >= revision_id_value:
                _add_issue(issues, f"revision {revision_id_value}.parent_revision_id 必须早于子 revision")
        for field in ("steps",):
            steps = revision.get(field)
            if not isinstance(steps, list):
                _add_issue(issues, f"revision {revision_id_value}.{field} 不是数组")
        step_ids: set[Any] = set()
        steps = _safe_plan_steps(revision)
        for index, step in enumerate(steps):
            step_id = step.get("step_id")
            if step_id in step_ids:
                _add_issue(issues, f"revision {revision_id_value} step ID 重复: {step_id}")
            step_ids.add(step_id)
            dependencies = step.get("depends_on", [])
            if not isinstance(dependencies, list):
                _add_issue(issues, f"revision {revision_id_value} step {step_id}.depends_on 不是数组")
                dependencies = []
            for dependency in dependencies:
                if dependency not in step_ids and dependency not in {
                    item.get("step_id") for item in steps
                }:
                    _add_issue(issues, f"revision {revision_id_value} step {step_id} 依赖不存在: {dependency}")
        dependency_graph = {
            step.get("step_id"): [item for item in step.get("depends_on", []) if isinstance(item, str)]
            for step in steps
        }
        visiting: set[Any] = set()
        visited: set[Any] = set()
        def visit(step_id: Any) -> None:
            if step_id in visiting:
                _add_issue(issues, f"revision {revision_id_value} 依赖存在环")
                return
            if step_id in visited:
                return
            visiting.add(step_id)
            for dependency in dependency_graph.get(step_id, []):
                if dependency in dependency_graph:
                    visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)
        for step_id in dependency_graph:
            visit(step_id)

        parent = _mapping_get(plan_map, parent_id) if parent_id is not None else None
        if parent is None:
            if revision.get("diff") is not None:
                _add_issue(issues, f"revision {revision_id_value} 的初始结构不应带 diff")
        else:
            parent_ids = {item.get("step_id") for item in _safe_plan_steps(parent)}
            current_ids = {item.get("step_id") for item in steps}
            for step in steps:
                replacements = step.get("replaces", [])
                if not isinstance(replacements, list):
                    _add_issue(issues, f"revision {revision_id_value} step {step.get('step_id')}.replaces 不是数组")
                    continue
                for replaced in replacements:
                    if replaced not in parent_ids or replaced in current_ids:
                        _add_issue(issues, f"revision {revision_id_value} replaces 引用的步骤不是 parent 中被移除步骤: {replaced}")
            expected_diff = _computed_plan_diff(parent, revision)
            if _normalise_diff(revision.get("diff")) != _normalise_diff(expected_diff):
                _add_issue(issues, f"revision {revision_id_value} 保存的结构 diff 与快照重算结果不一致")

    def has_cycle_parent() -> None:
        visiting: set[int] = set()
        visited: set[int] = set()
        def visit(revision_id_value: int) -> None:
            if revision_id_value in visiting:
                _add_issue(issues, "plan revision parent 图存在环")
                return
            if revision_id_value in visited:
                return
            visiting.add(revision_id_value)
            parent_id = plan_map[revision_id_value].get("parent_revision_id")
            if _mapping_contains(plan_map, parent_id):
                visit(parent_id)
            visiting.remove(revision_id_value)
            visited.add(revision_id_value)
        for revision_id_value in plan_map:
            visit(revision_id_value)
    has_cycle_parent()

    def observation_attempt(attempt: Mapping[str, Any]) -> bool:
        arguments = attempt.get("redacted_arguments", {})
        if not (
            attempt.get("outcome") == "succeeded"
            and attempt.get("handler_admitted") is True
            and attempt.get("permission") == "allowed"
            and attempt.get("effect_class") == "none"
            and attempt.get("tool") not in {
                "begin_plan", "cancel_planning", "commit_plan", "update_plan_progress",
                "request_replan", "recover", "rollback_checkpoint",
            }
        ):
            return False
        return not (
            attempt.get("tool") == "run_shell"
            and isinstance(arguments, Mapping)
            and arguments.get("purpose") == "verification"
        )

    # Source and consumer checks for triggers, including the strict reverse
    # link from a resolved trigger to exactly one revision.
    consumed: dict[int, list[int]] = {}
    for trigger_id, trigger in trigger_map.items():
        if trigger.get("kind") not in {"failure", "observation", "user_feedback", "blocked_resume"}:
            _add_issue(issues, f"trigger {trigger_id}.kind 非法")
        source_fields = [
            field for field in ("caused_by_failure_id", "caused_by_attempt_id", "caused_by_decision_id")
            if trigger.get(field) is not None
        ]
        if len(source_fields) != 1:
            _add_issue(issues, f"trigger {trigger_id} 来源不唯一或缺失")
            continue
        source_field = source_fields[0]
        if trigger.get("kind") == "failure":
            source = _mapping_get(failures, trigger.get(source_field)) if source_field == "caused_by_failure_id" else None
            source_ok = source is not None and source.get("generation_id") == trigger.get("generation_id")
            source_type = "failure"
            source_id = trigger.get(source_field)
        elif trigger.get("kind") == "observation":
            source = _mapping_get(attempts, trigger.get(source_field)) if source_field == "caused_by_attempt_id" else None
            source_ok = source is not None and source.get("generation_id") == trigger.get("generation_id") and observation_attempt(source)
            source_type = "attempt"
            source_id = trigger.get(source_field)
        else:
            source = _mapping_get(decision_map, trigger.get(source_field)) if source_field == "caused_by_decision_id" else None
            expected_decision = "resume_blocked" if trigger.get("kind") == "blocked_resume" else "rejected"
            source_ok = (
                source is not None
                and source.get("generation_id") == trigger.get("generation_id")
                and source.get("decision") in ({"resume_blocked"} if expected_decision == "resume_blocked" else {"rejected", "continue_exploring"})
            )
            source_type = "decision"
            source_id = trigger.get(source_field)
        if not source_ok:
            _add_issue(issues, f"trigger {trigger_id} 的来源不存在或类型不匹配")
        result_revision_id = trigger.get("result_revision_id")
        if result_revision_id is not None:
            consumed.setdefault(trigger_id, []).append(result_revision_id)
            revision = _mapping_get(plan_map, result_revision_id)
            if revision is None or revision.get("trigger_id") != trigger_id or trigger.get("status") != "resolved":
                _add_issue(issues, f"trigger {trigger_id} 与 result_revision_id 反向引用不一致")
        elif trigger.get("status") == "resolved":
            _add_issue(issues, f"resolved trigger {trigger_id} 缺少 result_revision_id")
    for trigger_id, revision_ids in consumed.items():
        if len(revision_ids) > 1:
            _add_issue(issues, f"trigger {trigger_id} 被多个 revision 消费: {revision_ids}")
    for revision_id_value, revision in plan_map.items():
        trigger_id = revision.get("trigger_id")
        if trigger_id is None:
            if revision.get("parent_revision_id") is not None:
                _add_issue(issues, f"后续 revision {revision_id_value} 缺少 trigger_id")
            continue
        trigger = _mapping_get(trigger_map, trigger_id)
        if trigger is None:
            _add_issue(issues, f"revision {revision_id_value}.trigger_id 引用不存在: {trigger_id}")
        elif trigger.get("result_revision_id") != revision_id_value or trigger.get("status") != "resolved":
            _add_issue(issues, f"revision {revision_id_value}.trigger_id 未被唯一有效消费: {trigger_id}")

    # Progress continuity and its effective revision interval are based only
    # on sequence events.  Without those events the report does not guess.
    ordered_events = sorted(
        [item for item in event_records if _valid_positive_id(item.get("sequence_id"))],
        key=lambda item: item["sequence_id"],
    )
    start_by_revision = {
        revision_id_value: commit_events[revision_id_value].get("sequence_id")
        for revision_id_value in commit_events
    }
    next_start_by_revision: dict[int, int | None] = {}
    starts = sorted((value, key) for key, value in start_by_revision.items() if _valid_positive_id(value))
    for index, (start, revision_id_value) in enumerate(starts):
        next_start_by_revision[revision_id_value] = starts[index + 1][0] if index + 1 < len(starts) else None
    for revision_id_value, revision in plan_map.items():
        parent_id = revision.get("parent_revision_id")
        if _mapping_contains(start_by_revision, parent_id) and _mapping_contains(start_by_revision, revision_id_value):
            if start_by_revision[parent_id] >= start_by_revision[revision_id_value]:
                _add_issue(issues, f"revision {revision_id_value} 的 parent 提交顺序不可确认")
            between = [
                other_id for other_id, start in start_by_revision.items()
                if start_by_revision[parent_id] < start < start_by_revision[revision_id_value]
                and other_id not in {parent_id, revision_id_value}
            ]
            if between:
                _add_issue(issues, f"revision {revision_id_value} 提交时 parent {parent_id} 已不是活动 revision")
        elif parent_id is not None:
            _add_issue(issues, f"revision {revision_id_value} 的 parent 先后顺序不可确定")
        trigger = _mapping_get(trigger_map, revision.get("trigger_id"))
        if trigger is not None and revision_id_value in start_by_revision:
            source_id = next((trigger.get(field) for field in (
                "caused_by_failure_id", "caused_by_attempt_id", "caused_by_decision_id"
            ) if trigger.get(field) is not None), None)
            source_type = (
                "failure" if trigger.get("caused_by_failure_id") is not None else
                "attempt" if trigger.get("caused_by_attempt_id") is not None else
                "user_plan_decision" if trigger.get("caused_by_decision_id") is not None else None
            )
            if source_type is not None:
                source_events = _event_index(event_records, source_type, source_id)
                if not source_events or not any(
                    _valid_positive_id(item.get("sequence_id"))
                    and item["sequence_id"] < start_by_revision[revision_id_value]
                    for item in source_events
                ):
                    _add_issue(issues, f"revision {revision_id_value} 的 trigger 前因顺序不可确认")

    def active_revision_at(sequence_id: Any) -> int | None:
        candidates = [
            (start, revision_id_value)
            for revision_id_value, start in start_by_revision.items()
            if _valid_positive_id(start) and _valid_positive_id(sequence_id) and start <= sequence_id
        ]
        return max(candidates)[1] if candidates else None

    # The revision pointer is the ownership decision for an event.  Check it
    # against the commit timeline instead of trusting the list position or the
    # event's target generation alone.
    for event in ordered_events:
        event_revision = event.get("revision_id")
        if event_revision is None:
            continue
        expected_revision = active_revision_at(event.get("sequence_id"))
        if expected_revision != event_revision:
            _add_issue(
                issues,
                f"trace event {event.get('sequence_id')} 的 revision 归属与活动 revision 不一致",
            )
        record_type = event.get("record_type")
        record_id = event.get("record_id")
        target = _mapping_get(target_maps, record_type)
        if isinstance(target, Mapping):
            target_record = _mapping_get(target, record_id)
            if isinstance(target_record, Mapping) and record_type in {
                "plan_progress", "user_plan_decision",
            } and target_record.get("revision_id") != event_revision:
                _add_issue(
                    issues,
                    f"trace event {event.get('sequence_id')} 的 revision 与目标 {record_type} 不一致",
                )
            if record_type == "plan_revision" and record_id != event_revision:
                _add_issue(
                    issues,
                    f"trace event {event.get('sequence_id')} 未指向自身 revision",
                )

    for progress_id, progress in progress_map.items():
        revision_id_value = progress.get("revision_id")
        revision = _mapping_get(plan_map, revision_id_value)
        step_ids = {step.get("step_id") for step in _safe_plan_steps(revision or {})}
        if revision is None or progress.get("step_id") not in step_ids:
            _add_issue(issues, f"progress {progress_id} 的 revision 或 step 引用不存在")
        event_values = event_by_target.get(("plan_progress", progress_id), [])
        if not event_values:
            _add_issue(issues, f"progress {progress_id} 缺少顺序事件，无法确认生效期间")
            continue
        sequence_id = event_values[0].get("sequence_id")
        start = _mapping_get(start_by_revision, revision_id_value)
        end = _mapping_get(next_start_by_revision, revision_id_value)
        if start is None or not _valid_positive_id(sequence_id) or sequence_id <= start or (end is not None and sequence_id >= end):
            _add_issue(issues, f"progress {progress_id} 发生在 revision 生效期间之外")

    for revision_id_value in plan_map:
        status_by_step = {
            step.get("step_id"): step.get("status", "pending")
            for step in _safe_plan_steps(plan_map[revision_id_value])
        }
        relevant = []
        for progress_id, progress in progress_map.items():
            if progress.get("revision_id") != revision_id_value:
                continue
            events_for_progress = event_by_target.get(("plan_progress", progress_id), [])
            if events_for_progress and _valid_positive_id(events_for_progress[0].get("sequence_id")):
                relevant.append((events_for_progress[0]["sequence_id"], progress_id, progress))
            else:
                relevant.append((10**18, progress_id, progress))
        for _, progress_id, progress in sorted(relevant):
            step_id = progress.get("step_id")
            if status_by_step.get(step_id) != progress.get("from_status"):
                _add_issue(issues, f"progress {progress_id} 状态转换与先前事件不连续")
            if (progress.get("from_status"), progress.get("to_status")) not in {
                ("pending", "in_progress"), ("in_progress", "completed"),
            }:
                _add_issue(issues, f"progress {progress_id} 状态转换非法")
            status_by_step[step_id] = progress.get("to_status")

    for decision_id, decision in decision_map.items():
        revision_id_value = decision.get("revision_id")
        if revision_id_value is not None and not _mapping_contains(plan_map, revision_id_value):
            _add_issue(issues, f"decision {decision_id}.revision_id 引用不存在: {revision_id_value}")
        event_values = event_by_target.get(("user_plan_decision", decision_id), [])
        if not event_values:
            _add_issue(issues, f"decision {decision_id} 缺少顺序事件，待审批状态不可确认")
        elif decision.get("decision") in {"approved", "rejected", "continue_exploring"}:
            if event_values[0].get("planning_phase_before") != "awaiting_approval":
                _add_issue(issues, f"decision {decision_id} 未指向当时待审批的当前 revision")
            elif active_revision_at(event_values[0].get("sequence_id")) != revision_id_value:
                _add_issue(issues, f"decision {decision_id} 未指向当时待审批的当前 revision")
    for event in event_records:
        if event.get("kind") == "plan_review":
            if not _mapping_contains(plan_map, event.get("revision_id")):
                _add_issue(issues, f"plan_review {event.get('sequence_id')} 的 revision 引用不存在")

    # A contiguous event sequence proves only that the events which were
    # written are ordered.  Check the reverse direction as well: otherwise a
    # fact omitted by the recorder silently disappears from revision replay.
    missing_fact_edges: list[dict[str, Any]] = []
    if "trace_events" in snapshot:
        required_events = (
            ("attempt", attempts, "execution_result"),
            ("failure", failures, "failure_recorded"),
            ("recovery_action", recoveries, None),
            ("verification_history", dict(enumerate(evidence)), "verification_recorded"),
        )
        recovery_kinds = {
            "recovery_proposed", "recovery_activated", "recovery_result", "recovery_rejected",
        }
        for record_type, records, expected_kind in required_events:
            for record_id, fact in records.items():
                matches = event_by_target.get((record_type, record_id), [])
                recorded = any(
                    event.get("kind") == expected_kind
                    if expected_kind is not None else event.get("kind") in recovery_kinds
                    for event in matches
                )
                if recorded:
                    continue
                _add_issue(issues, f"{record_type} {record_id} 缺少对应 trace event")
                missing_fact_edges.append(_edge(
                    "trace_event_missing", _node(record_type, record_id),
                    "trace_event:missing", fact.get("generation_id"), False,
                    "事实缺少顺序事件，revision 归属不可确认",
                ))

    # The plan edges also make absent sequence links visible as unresolved.
    all_edges = _build_edges(
        generation_map, attempts, failures, recoveries, evidence, issues,
        plan_map, progress_map, decision_map, trigger_map, event_records,
    )
    all_edges.extend(missing_fact_edges)

    # A plan edge can cross generations (for example parent revision 1 in
    # generation 0 -> revision 2 in generation 1). Generation views keep
    # their own facts grouped locally, while retaining such an edge whenever
    # either endpoint belongs to the requested generation.
    endpoint_generation: dict[str, int] = {
        **{
            _node("revision", identifier): record.get("generation_id")
            for identifier, record in plan_map.items()
            if _valid_generation(record.get("generation_id"))
        },
        **{
            _node("trigger", identifier): record.get("generation_id")
            for identifier, record in trigger_map.items()
            if _valid_generation(record.get("generation_id"))
        },
        **{
            _node("progress", identifier): _mapping_get(plan_map, record.get("revision_id"), {}).get("generation_id")
            for identifier, record in progress_map.items()
            if _valid_generation(_mapping_get(plan_map, record.get("revision_id"), {}).get("generation_id"))
        },
        **{
            _node("decision", identifier): record.get("generation_id")
            for identifier, record in decision_map.items()
            if _valid_generation(record.get("generation_id"))
        },
        **{
            _node("attempt", identifier): record.get("generation_id")
            for identifier, record in attempts.items()
            if _valid_generation(record.get("generation_id"))
        },
        **{
            _node("failure", identifier): record.get("generation_id")
            for identifier, record in failures.items()
            if _valid_generation(record.get("generation_id"))
        },
        **{
            _node("recovery", identifier): record.get("generation_id")
            for identifier, record in recoveries.items()
            if _valid_generation(record.get("generation_id"))
        },
        **{
            _node("verification", index): record.get("generation_id")
            for index, record in enumerate(evidence)
            if _valid_generation(record.get("generation_id"))
        },
    }
    endpoint_generation.update({
        _node("generation", identifier): identifier
        for identifier in generation_map
    })

    def edge_in_generation(edge: Mapping[str, Any], generation: int) -> bool:
        if edge.get("generation_id") == generation:
            return True
        return any(
            endpoint_generation.get(edge.get(endpoint)) == generation
            for endpoint in ("from", "to")
        )

    record_store: dict[str, Any] = {
        "plan_revision": plan_map,
        "plan_progress": progress_map,
        "user_plan_decision": decision_map,
        "replan_trigger": trigger_map,
        "attempt": attempts,
        "failure": failures,
        "recovery_action": recoveries,
        "verification_history": evidence,
    }

    def event_record(event: Mapping[str, Any]) -> dict[str, Any] | None:
        return _event_record_view(event, record_store)

    def source_for_trigger(trigger: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(trigger, Mapping):
            return None
        if trigger.get("caused_by_failure_id") is not None:
            return deepcopy(_mapping_get(failures, trigger.get("caused_by_failure_id")))
        if trigger.get("caused_by_attempt_id") is not None:
            return deepcopy(_mapping_get(attempts, trigger.get("caused_by_attempt_id")))
        if trigger.get("caused_by_decision_id") is not None:
            return deepcopy(_mapping_get(decision_map, trigger.get("caused_by_decision_id")))
        return None

    def in_period(event: Mapping[str, Any], revision_id_value: int) -> bool:
        sequence_id = event.get("sequence_id")
        start = _mapping_get(start_by_revision, revision_id_value)
        end = _mapping_get(next_start_by_revision, revision_id_value)
        if start is None or not _valid_positive_id(sequence_id):
            return False
        return start <= sequence_id and (end is None or sequence_id < end)

    fact_event_kinds = {
        "attempt": {"execution_result"},
        "failure": {"failure_recorded"},
        "recovery_action": {
            "recovery_proposed", "recovery_activated", "recovery_result", "recovery_rejected",
        },
        "verification_history": {"verification_recorded"},
    }
    plan_views: list[dict[str, Any]] = []
    for revision_id_value in sorted(plan_map):
        revision = deepcopy(plan_map[revision_id_value])
        trigger = _mapping_get(trigger_map, revision.get("trigger_id"))
        commit_event = commit_events.get(revision_id_value)
        facts: dict[str, dict[Any, dict[str, Any]]] = {
            "attempt": {}, "failure": {}, "recovery_action": {}, "verification_history": {},
        }
        related_events = []
        for event in ordered_events:
            if event.get("revision_id") != revision_id_value:
                continue
            if in_period(event, revision_id_value) or event.get("kind") in {"plan_committed", "plan_review"}:
                related_events.append(event)
            record_type = event.get("record_type")
            if (_mapping_contains(fact_event_kinds, record_type)
                    and event.get("kind") in _mapping_get(fact_event_kinds, record_type, set())
                    and in_period(event, revision_id_value)):
                value = event_record(event)
                if value is not None:
                    facts[record_type][event.get("record_id")] = value
        progress_view = []
        for progress_id, progress in progress_map.items():
            if progress.get("revision_id") == revision_id_value:
                progress_view.append((
                    event_by_target.get(("plan_progress", progress_id), [{}])[0].get("sequence_id", 10**18),
                    deepcopy(progress),
                ))
        progress_view = [item for _, item in sorted(progress_view, key=lambda value: value[0])]
        decision_view = []
        for decision_id, decision in decision_map.items():
            if decision.get("revision_id") == revision_id_value:
                decision_view.append((
                    event_by_target.get(("user_plan_decision", decision_id), [{}])[0].get("sequence_id", 10**18),
                    deepcopy(decision),
                ))
        decision_view = [item for _, item in sorted(decision_view, key=lambda value: value[0])]
        attempts_view = list(facts["attempt"].values())
        failures_view = list(facts["failure"].values())
        recovery_view = list(facts["recovery_action"].values())
        verification_view = [facts["verification_history"][key] for key in sorted(facts["verification_history"])]
        generation_ids_for_revision = {revision.get("generation_id")}
        for collection in (attempts_view, failures_view, recovery_view, verification_view):
            for item in collection:
                if _valid_generation(item.get("generation_id")):
                    generation_ids_for_revision.add(item["generation_id"])
        source = source_for_trigger(trigger)
        if source is not None and _valid_generation(source.get("generation_id")):
            generation_ids_for_revision.add(source["generation_id"])
        start = _mapping_get(start_by_revision, revision_id_value)
        end = _mapping_get(next_start_by_revision, revision_id_value)
        start_event = commit_event if _valid_positive_id(start) else None
        end_event = next(
            (item for item in commit_events.values() if item.get("sequence_id") == end), None,
        ) if end is not None else None
        start_generation = start_event.get("generation_id") if start_event else None
        end_generation = end_event.get("generation_id") if end_event else None
        active_generation_ids = [
            gid for gid in sorted(generation_map)
            if _valid_generation(start_generation) and start_generation <= gid
            and (end_generation is None or gid <= end_generation)
        ]
        view = {
            **revision,
            "parent": deepcopy(_mapping_get(plan_map, revision.get("parent_revision_id"))),
            "trigger": deepcopy(trigger),
            "trigger_source": source,
            "predecessor": {
                "parent_revision_id": revision.get("parent_revision_id"),
                "trigger": deepcopy(trigger),
                "source": source,
            },
            "submission_event": deepcopy(commit_event),
            "active_from_sequence_id": commit_event.get("sequence_id") if commit_event else None,
            "active_until_sequence_id": _mapping_get(next_start_by_revision, revision_id_value),
            "progress_events": progress_view,
            "decisions": decision_view,
            "attempts": attempts_view,
            "investigation_attempts": [item for item in attempts_view if observation_attempt(item)],
            "failures": failures_view,
            "recovery_actions": recovery_view,
            "verification_evidence": verification_view,
            "generation_ids": sorted(item for item in generation_ids_for_revision if _valid_generation(item)),
            "active_generation_ids": active_generation_ids,
            "timeline": [
                {**deepcopy(event), "record": event_record(event)}
                for event in sorted(related_events, key=lambda item: item.get("sequence_id", 10**18))
            ],
            "attribution": "complete" if commit_event is not None else "unresolved",
        }
        view["structure_diff"] = deepcopy(view.get("diff"))
        plan_views.append(view)

    selected_plan_views = [
        item for item in plan_views
        if (
            revision_id is None
            and (generation_id is None or generation_id in item.get("active_generation_ids", []))
        ) or item.get("revision_id") == revision_id
    ]
    if generation_id is not None:
        selected_plan_views = [
            {**item, "generation_role": (
                "submitted" if item.get("generation_id") == generation_id else "active"
            )}
            for item in selected_plan_views
        ]
    if revision_id is not None and not _mapping_contains(plan_map, revision_id):
        raise TraceQueryError(f"revision 不存在: {revision_id}")

    # Ordered top-level timeline.  A revision query includes the selected
    # revision's trigger source event as a predecessor, even when it belongs
    # to the old revision/generation.
    selected_timeline_events = ordered_events
    if revision_id is not None:
        selected_revision = plan_map[revision_id]
        selected_trigger = _mapping_get(trigger_map, selected_revision.get("trigger_id"))
        selected_keys = {(event.get("record_type"), event.get("record_id")) for event in ordered_events
                         if event.get("revision_id") == revision_id}
        if selected_trigger is not None:
            selected_keys.add(("replan_trigger", selected_trigger.get("trigger_id")))
            source_id = source_for_trigger(selected_trigger)
            if source_id is not None:
                source_key = (
                    "failure" if selected_trigger.get("caused_by_failure_id") is not None else
                    "attempt" if selected_trigger.get("caused_by_attempt_id") is not None else
                    "user_plan_decision"
                )
                source_identifier = next((selected_trigger.get(field) for field in (
                    "caused_by_failure_id", "caused_by_attempt_id", "caused_by_decision_id"
                ) if selected_trigger.get(field) is not None), None)
                selected_keys.add((source_key, source_identifier))
        selected_timeline_events = [
            event for event in ordered_events
            if event.get("revision_id") == revision_id
            or (event.get("record_type"), event.get("record_id")) in selected_keys
        ]
    elif generation_id is not None:
        selected_timeline_events = [
            event for event in ordered_events
            if event.get("generation_id") == generation_id
        ]
    plan_timeline = [
        {**deepcopy(event), "record": event_record(event)}
        for event in selected_timeline_events
        if event.get("kind") not in {"execution_result"} or event.get("revision_id") is not None
    ]

    if revision_id is not None:
        relevant_generation_ids = set(selected_plan_views[0].get("generation_ids", [])) if selected_plan_views else set()
        generation_reports = [
            item for item in base.get("generations", [])
            if isinstance(item, Mapping) and item.get("generation_id") in relevant_generation_ids
        ]
    else:
        generation_reports = base.get("generations", [])
        if generation_id is not None:
            generation_reports = [item for item in generation_reports if item.get("generation_id") == generation_id]

    for generation in generation_reports:
        if not isinstance(generation, dict):
            continue
        gid = generation.get("generation_id")
        generation["plan_revisions"] = [
            {**deepcopy(item), "generation_role": (
                "submitted" if item.get("generation_id") == gid else "active"
            )}
            for item in selected_plan_views
            if gid in item.get("active_generation_ids", [])
        ]
        generation["trace_events"] = [
            deepcopy(event) for event in event_records if event.get("generation_id") == gid
        ]
        generation_edges = [
            deepcopy(edge) for edge in all_edges
            if edge_in_generation(edge, gid)
        ]
        generation["causal_edges"] = generation_edges
        generation["edges"] = generation_edges

    def latest_stagnation(up_to_generation: int | None = None) -> dict[str, Any] | None:
        values = [
            event for event in event_records
            if event.get("kind") in {"stagnation_warning", "stagnation_blocked"}
            and (up_to_generation is None or event.get("generation_id") <= up_to_generation)
        ]
        if values:
            return deepcopy(values[-1])
        raw_stagnation = snapshot.get("loop_stagnation", snapshot.get("stagnation", {}))
        if isinstance(raw_stagnation, Mapping) and (
            raw_stagnation.get("warning_kind") or raw_stagnation.get("last_reason")
        ):
            return {
                "kind": "stagnation_snapshot",
                "stagnation_kind": _safe_text(raw_stagnation.get("warning_kind"))[:120],
                "stagnation_count": raw_stagnation.get("consecutive_no_progress_rounds"),
                "stagnation_fingerprint": _safe_text(raw_stagnation.get("last_round_fingerprint"))[:12],
            }
        return None

    def conclusion_basis(for_generation: int | None) -> dict[str, Any]:
        planning_state = snapshot.get("planning_state", {})
        active_revision_id = planning_state.get("active_revision_id") if isinstance(planning_state, Mapping) else None
        current_generation = snapshot.get("current_generation_id")
        target_generation = current_generation if for_generation is None else for_generation
        historical = for_generation is not None and for_generation != current_generation
        if historical:
            eligible_commits = [
                event for event in commit_events.values()
                if _valid_positive_id(event.get("sequence_id"))
                and _valid_generation(event.get("generation_id"))
                and event["generation_id"] <= target_generation
            ]
            latest_commit = max(eligible_commits, key=lambda item: item["sequence_id"]) if eligible_commits else None
            active_revision_id = latest_commit.get("record_id") if latest_commit else None
            active_revision = _mapping_get(plan_map, active_revision_id)
            active_steps = deepcopy(_safe_plan_steps(active_revision or {}))
            step_by_id = {step.get("step_id"): step for step in active_steps}
            for event in ordered_events:
                if (event.get("record_type") != "plan_progress"
                        or event.get("kind") != "plan_progress"
                        or event.get("revision_id") != active_revision_id
                        or not _valid_generation(event.get("generation_id"))
                        or event["generation_id"] > target_generation):
                    continue
                progress = _mapping_get(progress_map, event.get("record_id"))
                step = _mapping_get(step_by_id, progress.get("step_id")) if progress else None
                if step is not None:
                    step["status"] = progress.get("to_status")
        else:
            active_revision = _mapping_get(plan_map, active_revision_id)
            active_plan = snapshot.get("active_plan")
            active_steps = deepcopy(
                active_plan.get("steps", [])
                if isinstance(active_plan, Mapping)
                and active_plan.get("revision_id") == active_revision_id
                else (active_revision or {}).get("steps", [])
            )
        current_evidence = [
            deepcopy(item) for item in evidence
            if item.get("generation_id") == target_generation
        ]
        failures_until = [
            deepcopy(item) for item in failures.values()
            if for_generation is None or item.get("generation_id") <= for_generation
        ]
        return {
            "active_revision_id": active_revision_id,
            "active_revision_steps": active_steps,
            "active_revision_statuses": {
                item.get("step_id"): item.get("status")
                for item in active_steps
                if isinstance(item, Mapping)
            },
            "current_generation_id": target_generation,
            "verification_generation": target_generation,
            "current_generation_verification": current_evidence,
            "verification_required": bool(snapshot.get("verification_required", False)) if target_generation == current_generation else None,
            "verification_complete": (
                bool(current_evidence)
                and current_evidence[-1].get("outcome") == "passed"
                and not (
                    target_generation == current_generation
                    and snapshot.get("verification_required", False)
                )
            ),
            "last_failure": failures_until[-1] if failures_until else None,
            "stagnation": latest_stagnation(for_generation),
            "terminal_summary": (
                {"status": "not_recorded", "terminal_reason": ""}
                if historical else {
                    "status": snapshot.get("status", "running"),
                    "terminal_reason": _safe_text(snapshot.get("terminal_reason", "")),
                }
            ),
        }

    for generation in generation_reports:
        if isinstance(generation, dict) and isinstance(generation.get("conclusion"), dict):
            generation["conclusion"]["evidence"] = conclusion_basis(generation.get("generation_id"))
            generation["conclusion"]["basis"] = generation["conclusion"]["evidence"]

    final_conclusion = deepcopy(base.get("conclusion", {}))
    final_conclusion["evidence"] = conclusion_basis(None)
    final_conclusion["basis"] = final_conclusion["evidence"]
    if revision_id is not None:
        query_generation_ids = sorted({
            item for view in selected_plan_views for item in view.get("generation_ids", [])
            if _valid_generation(item)
        })
    elif generation_id is not None:
        query_generation_ids = [generation_id]
    else:
        query_generation_ids = base.get("query", {}).get("generation_ids", [])
    report = base
    report["generations"] = generation_reports
    report["query"] = {
        "generation_id": generation_id,
        "revision_id": revision_id,
        "scope": "revision" if revision_id is not None else "all" if generation_id is None else "generation",
        "generation_ids": query_generation_ids,
    }
    report["plan_revisions"] = deepcopy(selected_plan_views)
    report["plan_timeline"] = plan_timeline
    report["trace_events"] = deepcopy(selected_timeline_events)
    report["causal_edges"] = [
        deepcopy(edge) for edge in all_edges
        if generation_id is None
        or edge_in_generation(edge, generation_id)
    ] if revision_id is None else []
    # A revision query must retain the old generation's source edge as a
    # causal link, even though its main display is the selected revision.
    if revision_id is not None:
        selected_edge_nodes = {
            _node("revision", revision_id),
            _node("trigger", plan_map[revision_id].get("trigger_id")),
        }
        selected_trigger = _mapping_get(trigger_map, plan_map[revision_id].get("trigger_id"))
        if selected_trigger:
            for field, kind in (("caused_by_failure_id", "failure"), ("caused_by_attempt_id", "attempt"), ("caused_by_decision_id", "decision")):
                if selected_trigger.get(field) is not None:
                    selected_edge_nodes.add(_node(kind, selected_trigger[field]))
        report["causal_edges"] = [
            deepcopy(edge) for edge in all_edges
            if edge.get("from") in selected_edge_nodes or edge.get("to") in selected_edge_nodes
        ]
    report["conclusion"] = (
        deepcopy(generation_reports[0]["conclusion"])
        if generation_id is not None and generation_reports else final_conclusion
    )
    report["integrity"] = {
        "status": "complete" if not issues else "incomplete",
        "issues": issues,
    }
    return report


def _value(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return str(value)


def _render_record(prefix: str, record: Mapping[str, Any], fields: Sequence[str]) -> list[str]:
    lines = [prefix]
    for field in fields:
        if field in record:
            lines.append(f"    {field}: {_value(record[field])}")
    return lines


def render_trace(report: Mapping[str, Any]) -> str:
    """Render every safe, bounded record in a trace report as text."""
    if not isinstance(report, Mapping):
        raise TraceQueryError("report 必须是 mapping")
    lines = ["Trace & Replay"]
    lines.append(f"任务: {report.get('task', '') or '(无)'}")
    query = report.get("query", {})
    if isinstance(query, Mapping) and query.get("revision_id") is not None:
        lines.append(f"查询范围: revision {query.get('revision_id')}")
    elif isinstance(query, Mapping) and query.get("generation_id") is not None:
        lines.append(f"查询范围: generation {query.get('generation_id')}")
    else:
        lines.append("查询范围: 全部 generation")
    integrity = report.get("integrity", {})
    lines.append(f"完整性: {integrity.get('status', 'incomplete') if isinstance(integrity, Mapping) else 'incomplete'}")
    if isinstance(integrity, Mapping):
        for issue in integrity.get("issues", []) or []:
            lines.append(f"  问题: {issue}")

    checkpoints = report.get("checkpoints", []) or []
    lines.append("Checkpoints:")
    if not checkpoints:
        lines.append("  (none)")
    for checkpoint in checkpoints:
        lines.extend(_render_record(
            f"  [{checkpoint.get('checkpoint_id', '?')}]",
            checkpoint,
            _CHECKPOINT_FIELDS,
        ))

    for generation in report.get("generations", []) or []:
        gid = generation.get("generation_id", "?") if isinstance(generation, Mapping) else "?"
        lines.append(f"\nGeneration {gid}")
        if not isinstance(generation, Mapping):
            lines.append("  <invalid generation record>")
            continue
        if not generation.get("recorded", True):
            lines.append("  generation record: <missing>")
        else:
            raw_generation = generation.get("generation") or {}
            for line in _render_record("  generation:", raw_generation, _GENERATION_FIELDS):
                lines.append(line)
        sections = (
            ("Todo revisions", "todo_revisions", "revision_id", _TODO_REVISION_FIELDS),
            ("Attempts", "attempts", "attempt_id", _ATTEMPT_FIELDS),
            ("Failures", "failures", "failure_id", _FAILURE_FIELDS + ("diagnosis", "diagnosis_source")),
            ("Recovery actions", "recovery_actions", "recovery_id", _RECOVERY_FIELDS),
            ("Verification evidence", "verification_evidence", None, _VERIFICATION_FIELDS),
        )
        for title, key, identifier_field, fields in sections:
            lines.append(f"  {title}:")
            records = generation.get(key, []) or []
            if not records:
                lines.append("    (none)")
            for index, record in enumerate(records):
                identifier = record.get(identifier_field) if identifier_field else index
                lines.extend(_render_record(f"    [{identifier}]", record, fields))
        lines.append("  Causal edges:")
        edges = generation.get("causal_edges", generation.get("edges", [])) or []
        if not edges:
            lines.append("    (none)")
        for edge in edges:
            if not isinstance(edge, Mapping):
                lines.append(f"    {_value(edge)}")
                continue
            state = "resolved" if edge.get("resolved") else "UNRESOLVED"
            lines.append(f"    {edge.get('type', '?')}: {edge.get('from', '?')} -> {edge.get('to', '?')} [{state}]")
            if edge.get("detail"):
                lines.append(f"      detail: {edge['detail']}")
        conclusion = generation.get("conclusion", {})
        if isinstance(conclusion, Mapping):
            lines.append(f"  结论: {conclusion.get('status', 'continue')}")
            if conclusion.get("terminal_reason"):
                lines.append(f"    terminal_reason: {conclusion['terminal_reason']}")
            if conclusion.get("last_failure") is not None:
                lines.append(f"    last_failure: {_value(conclusion['last_failure'])}")

    plan_revisions = report.get("plan_revisions", []) or []
    lines.append("\nPlan chain:")
    if not plan_revisions:
        lines.append("  (none)")
    for revision in plan_revisions:
        if not isinstance(revision, Mapping):
            lines.append(f"  {_value(revision)}")
            continue
        identifier = revision.get("revision_id", "?")
        lines.append(f"  Revision {identifier} (generation {revision.get('generation_id', '?')})")
        for field, label in (
            ("goal", "goal"), ("reason", "reason"),
            ("parent_revision_id", "parent"), ("trigger_id", "trigger"),
            ("generation_role", "generation_role"),
            ("attribution", "attribution"),
        ):
            if field in revision:
                value = revision[field]
                lines.append(f"    {label}: {_safe_text(value) if isinstance(value, str) else _value(value)}")
        if revision.get("trigger_source") is not None:
            lines.append(f"    trigger_source: {_value(revision['trigger_source'])}")
        if revision.get("diff") is not None:
            lines.append(f"    structure_diff: {_value(revision['diff'])}")
        lines.append("    steps:")
        for step in revision.get("steps", []) or []:
            if isinstance(step, Mapping):
                lines.append(
                    f"      [{step.get('status', '?')}] {step.get('step_id', '?')}: "
                    f"{_safe_text(step.get('content', ''))}"
                )
        for title, key in (
            ("progress", "progress_events"), ("decisions", "decisions"),
            ("attempts", "attempts"), ("failures", "failures"),
            ("recovery", "recovery_actions"), ("verification", "verification_evidence"),
        ):
            records = revision.get(key, []) or []
            lines.append(f"    {title}: {len(records)}")

    lines.append("  Plan timeline:")
    timeline = report.get("plan_timeline", []) or []
    if not timeline:
        lines.append("    (none)")
    for event in timeline:
        if not isinstance(event, Mapping):
            lines.append(f"    {_value(event)}")
            continue
        lines.append(
            f"    [{event.get('sequence_id', '?')}] {event.get('kind', '?')} "
            f"generation={event.get('generation_id', '?')} revision={event.get('revision_id', '-') }"
        )
        if event.get("record_type") is not None:
            lines.append(
                f"      record: {event.get('record_type')}#{event.get('record_id', '?')}"
            )
        for before, after, label in (
            ("planning_phase_before", "planning_phase_after", "planning_phase"),
            ("repair_phase_before", "repair_phase_after", "repair_phase"),
        ):
            if event.get(before) is not None or event.get(after) is not None:
                lines.append(f"      {label}: {event.get(before)} -> {event.get(after)}")
        if event.get("record") is not None:
            lines.append(f"      fact: {_value(event['record'])}")
        if event.get("stagnation_kind") is not None:
            lines.append(
                f"      stagnation: {event.get('stagnation_kind')} "
                f"count={event.get('stagnation_count')} "
                f"fingerprint={event.get('stagnation_fingerprint')}"
            )

    conclusion = report.get("conclusion", {})
    if isinstance(conclusion, Mapping):
        lines.append(f"\n最终结论: {conclusion.get('status', 'continue')}")
        if conclusion.get("terminal_reason"):
            lines.append(f"terminal_reason: {conclusion['terminal_reason']}")
        if conclusion.get("last_failure") is not None:
            lines.append(f"last_failure: {_value(conclusion['last_failure'])}")
        evidence = conclusion.get("evidence", conclusion.get("basis"))
        if isinstance(evidence, Mapping):
            lines.append("结论依据:")
            for field in (
                "active_revision_id", "active_revision_statuses", "current_generation_id",
                "verification_generation", "verification_required", "verification_complete",
                "last_failure", "stagnation", "terminal_summary",
            ):
                if field in evidence:
                    lines.append(f"  {field}: {_value(evidence[field])}")
    return "\n".join(lines)


__all__ = ["TraceQueryError", "build_trace", "render_trace"]
