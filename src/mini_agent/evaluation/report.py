"""Rebuild aggregate reports from immutable schema 1 TrialResult files."""

from __future__ import annotations

import json
from pathlib import Path
from statistics import median
from typing import Any

from mini_agent.evaluation.schema import validate_trial_result


INFRASTRUCTURE_FAILURES = {"infrastructure_error", "grader_infrastructure_error"}


def _percentile(values: list[int], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) * percentile + 0.999999)) - 1))
    return float(ordered[index])


def _sum_known(rows: list[dict[str, Any]], key: str) -> dict[str, int | None]:
    values = [row.get(key) for row in rows if isinstance(row.get(key), int) and not isinstance(row.get(key), bool)]
    return {
        "observed_trials": len(values),
        "total": sum(values) if values else None,
    }


def _cohort(rows: list[dict[str, Any]]) -> dict[str, Any]:
    graded = [
        row for row in rows
        if isinstance(row.get("grader_passed"), bool)
        and row.get("grader_error_kind") is None
        and row.get("failure_kind") not in INFRASTRUCTURE_FAILURES
    ]
    acceptance_passes = sum(row["grader_passed"] is True for row in graded)
    agent_durations = [row["agent_duration_ms"] for row in rows if isinstance(row.get("agent_duration_ms"), int)]
    grader_durations = [row["grader_duration_ms"] for row in rows if isinstance(row.get("grader_duration_ms"), int)]
    failure_counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get("failure_kind", "unknown"))
        failure_counts[key] = failure_counts.get(key, 0) + 1
    grader_error_counts: dict[str, int] = {}
    for row in rows:
        value = row.get("grader_error_kind")
        if isinstance(value, str):
            grader_error_counts[value] = grader_error_counts.get(value, 0) + 1
    sources: dict[str, int] = {}
    for row in rows:
        key = str(row.get("usage_source", "unavailable"))
        sources[key] = sources.get(key, 0) + 1
    return {
        "trials": len(rows),
        "scored_trials": len(graded),
        "independent_acceptance_passed": acceptance_passes,
        "independent_acceptance_pass_rate": (acceptance_passes / len(graded)) if graded else None,
        "final_task_successes": sum(row.get("success") is True for row in rows),
        "timeouts": sum(row.get("failure_kind") == "agent_timeout" for row in rows),
        "agent_errors": sum(row.get("failure_kind") == "agent_error" for row in rows),
        "task_failures": sum(row.get("failure_kind") == "task_failed" for row in rows),
        "grader_infrastructure_errors": sum(isinstance(row.get("grader_error_kind"), str) for row in rows),
        "runner_infrastructure_errors": sum(row.get("failure_kind") == "infrastructure_error" for row in rows),
        "agent_output_limit_failures": sum(row.get("failure_kind") == "agent_output_limit" for row in rows),
        "grader_error_counts": grader_error_counts,
        "failure_counts": failure_counts,
        "tool_calls": _sum_known(rows, "tool_calls"),
        "permission_denials": _sum_known(rows, "permission_denials"),
        "agent_llm_calls": _sum_known(rows, "agent_llm_calls"),
        "successful_model_responses": _sum_known(rows, "successful_model_responses"),
        "input_tokens": _sum_known(rows, "input_tokens"),
        "output_tokens": _sum_known(rows, "output_tokens"),
        "usage_source_trials": sources,
        "agent_duration_ms": {
            "observed_trials": len(agent_durations),
            "median": median(agent_durations) if agent_durations else None,
            "p95": _percentile(agent_durations, 0.95),
        },
        "grader_duration_ms": {
            "observed_trials": len(grader_durations),
            "median": median(grader_durations) if grader_durations else None,
            "p95": _percentile(grader_durations, 0.95),
        },
        "cost_usd": None,
        "recovery_success_rate": None,
        "invalid_repeat_count": None,
        "uncollected_metrics_note": "本版恢复成功率与无效重复次数不适用/未采集；成本缺少价格快照，保持 null。",
    }


def build_report(output_dir: str | Path) -> dict[str, Any]:
    root = Path(output_dir).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("report 路径必须是目录")
    rows: list[dict[str, Any]] = []
    unreadable: list[str] = []
    for trial_dir in sorted(root.glob("trial-*")):
        result_path = trial_dir / "trial.json"
        if trial_dir.is_symlink() or not result_path.is_file() or result_path.is_symlink():
            unreadable.append(trial_dir.name)
            continue
        try:
            if result_path.stat().st_size > 256 * 1024:
                raise ValueError("result_too_large")
            row = validate_trial_result(json.loads(result_path.read_text(encoding="utf-8")))
        except Exception:
            unreadable.append(trial_dir.name)
            continue
        row = dict(row)
        row["trial_path"] = trial_dir.name
        rows.append(row)
    groups = {kind: [row for row in rows if row.get("run_kind") == kind] for kind in ("live", "fixture")}
    return {
        "schema_version": 1,
        "source": "raw TrialResult JSON; source files are not modified",
        "total_trials": len(rows),
        "unreadable_trial_directories": unreadable,
        "live": _cohort(groups["live"]),
        "fixture": _cohort(groups["fixture"]),
        "trials": [
            {
                "trial_path": row["trial_path"],
                "trial_id": row["trial_id"],
                "case_id": row["case_id"],
                "case_version": row["case_version"],
                "run_kind": row["run_kind"],
                "success": row["success"],
                "grader_passed": row["grader_passed"],
                "failure_kind": row["failure_kind"],
                "agent_stop_reason": row["agent_stop_reason"],
                "successful_model_responses": row.get("successful_model_responses"),
                "usage_source": row.get("usage_source"),
            }
            for row in rows
        ],
    }
