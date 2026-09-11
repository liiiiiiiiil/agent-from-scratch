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
        elif field in {"output_excerpt", "output", "reason", "cause_hint", "command", "current_goal"}:
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
    if value not in available:
        _add_issue(issues, f"{owner}.{field} 引用不存在: {value}")
        return False
    return True


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
) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []

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
            target = collection.get(identifier)
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
        attempt = attempts.get(attempt_id)
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
        resolved = failure_id in failures
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
                result_attempt = attempts.get(attempt_id)
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
        attempt = attempts.get(attempt_id)
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
    return edges


def build_trace(snapshot: Mapping[str, Any], generation_id: int | None = None) -> dict[str, Any]:
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
    if isinstance(query, Mapping) and query.get("generation_id") is not None:
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

    conclusion = report.get("conclusion", {})
    if isinstance(conclusion, Mapping):
        lines.append(f"\n最终结论: {conclusion.get('status', 'continue')}")
        if conclusion.get("terminal_reason"):
            lines.append(f"terminal_reason: {conclusion['terminal_reason']}")
        if conclusion.get("last_failure") is not None:
            lines.append(f"last_failure: {_value(conclusion['last_failure'])}")
    return "\n".join(lines)


__all__ = ["TraceQueryError", "build_trace", "render_trace"]
