"""v0.21 read-only Trace & Replay coverage."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import pytest

from mini_agent import __main__ as cli
from mini_agent.permission import ALLOW, DENY, PermissionGate, PermissionPolicy
from mini_agent.state import AgentState, ExecutionAttempt
from mini_agent.tools import create_registry
from mini_agent.tools.base import ToolExecutor
from mini_agent.trace import TraceQueryError, build_trace, render_trace


def _executor(state, rules=None, workspace=None):
    registry = create_registry(state, workspace)
    policy = PermissionPolicy(rules or {"run_shell": ALLOW, "recover": ALLOW})
    return ToolExecutor(registry, PermissionGate(policy))


def _record(executor, state, name, arguments):
    result = executor.execute_result(name, arguments, state)
    if name != "recover":
        state.record_execution_result(result)
    return result


def _commit_plan(state, step_id="inspect", status=None):
    state.commit_plan(
        goal="trace task", constraints=[], success_criteria=["check passes"],
        steps=[{
            "step_id": step_id, "content": step_id, "depends_on": [],
            "success_criteria": ["done"], "replaces": [],
        }], reason="trace plan",
    )
    if status in ("in_progress", "completed"):
        state.update_plan_progress(1, step_id, "in_progress", "start")
    if status == "completed":
        state.update_plan_progress(1, step_id, "completed", "done")


def test_todo_revision_is_atomic_generation_bound_and_resettable():
    state = AgentState()
    state.begin_task("todo audit")
    _commit_plan(state, status="in_progress")
    before_invalid = state.snapshot()
    with pytest.raises(ValueError):
        state.update_plan_progress(1, "inspect", "in_progress", "no-op")
    assert state.snapshot() == before_invalid

    state.update_plan_progress(1, "inspect", "completed", "done")
    reservation = state.reserve_attempt("possible", "mutate", {})
    assert reservation.generation_id == 1
    state.attempts.append(ExecutionAttempt(
        "a-1", 0, 1, "observe", "hash", {}, "succeeded", 0,
        "none", True, "allowed",
    ))
    state._revision_attempt_boundaries[1] = 0
    trigger = state.request_replan("observation", "a-1", "the old plan needs a revision")
    state.commit_plan(
        goal="trace task revised", constraints=[], success_criteria=["check passes"],
        steps=[{"step_id": "inspect", "content": "inspect", "depends_on": [],
                "success_criteria": ["done"], "replaces": []}],
        reason="revised", parent_revision_id=1,
        trigger_id=trigger.trigger_id,
    )
    revisions = state.snapshot()["plan_revisions"]
    assert [item["revision_id"] for item in revisions] == [1, 2]
    assert [item["generation_id"] for item in revisions] == [0, 1]
    assert revisions[0]["steps"][0]["status"] == "pending"
    assert state.snapshot()["active_plan"]["steps"][0]["status"] == "completed"

    state.reset_task()
    assert state.snapshot()["plan_revisions"] == []
    state.begin_task("new task")
    _commit_plan(state, "fresh")
    assert state.snapshot()["plan_revisions"][0]["revision_id"] == 1


def test_successful_trace_preserves_todos_and_done_conclusion():
    state = AgentState()
    state.begin_task("finish")
    _commit_plan(state, "finish", "completed")
    state.status = "done"

    report = build_trace(state.snapshot())
    assert report["integrity"] == {"status": "complete", "issues": []}
    assert report["generations"][0]["todo_revisions"] == []
    assert report["conclusion"]["status"] == "done"
    assert "finish" in render_trace(report)


def test_real_registry_failure_retry_and_verification_trace_is_complete():
    state = AgentState()
    state.begin_task("retry a check")
    _commit_plan(state, "check", "completed")
    registry = create_registry(state)
    shell = registry.get("run_shell")
    original = shell.handler
    outputs = iter(("[exit=1] initial", "[exit=0] retry", "[exit=0] final"))
    shell.handler = lambda command, purpose="execution": next(outputs)
    executor = ToolExecutor(
        registry,
        PermissionGate(PermissionPolicy({"run_shell": ALLOW, "recover": ALLOW})),
    )
    try:
        _record(executor, state, "run_shell", {"command": "check", "purpose": "verification"})
        _record(executor, state, "recover", {
            "action": "retry", "caused_by_failure_id": "f-1",
            "reason": "temporary check failure", "requested_attempt": "a-1",
        })
        _record(executor, state, "run_shell", {"command": "check", "purpose": "verification"})
    finally:
        shell.handler = original
    state.status = "done"

    report = build_trace(state.snapshot())
    assert report["integrity"]["status"] == "complete"
    assert [item["generation_id"] for item in report["generations"]] == [0, 1, 2, 3]
    assert report["generations"][1]["failures"][0]["diagnosis_source"] == "recovery.reason"
    assert report["generations"][1]["verification_evidence"][0]["outcome"] == "failed"
    assert report["generations"][2]["recovery_actions"][0]["result_attempt"] == "a-2"
    assert report["generations"][3]["verification_evidence"][0]["caused_by_attempt_id"] == "a-3"
    edge_types = {edge["type"] for edge in report["causal_edges"]}
    assert {"generation_opener", "attempt_failure", "failure_recovery",
            "recovery_successor", "recovery_result", "attempt_verification"} <= edge_types
    assert report["conclusion"]["status"] == "done"


def test_verification_failure_again_is_a_new_failure_in_the_successor_generation():
    state = AgentState()
    state.begin_task("retry then fail again")
    registry = create_registry(state)
    shell = registry.get("run_shell")
    original = shell.handler
    outputs = iter(("[exit=1] initial", "[exit=0] recovery", "[exit=1] still broken"))
    shell.handler = lambda command, purpose="execution": next(outputs)
    executor = ToolExecutor(
        registry,
        PermissionGate(PermissionPolicy({"run_shell": ALLOW, "recover": ALLOW})),
    )
    try:
        _record(executor, state, "run_shell", {"command": "check-again", "purpose": "verification"})
        _record(executor, state, "recover", {
            "action": "retry", "caused_by_failure_id": "f-1",
            "reason": "retry once", "requested_attempt": "a-1",
        })
        _record(executor, state, "run_shell", {"command": "check", "purpose": "verification"})
    finally:
        shell.handler = original

    report = build_trace(state.snapshot())
    assert report["integrity"]["status"] == "complete"
    assert report["generations"][3]["failures"][0]["failure_id"] == "f-2"
    assert report["generations"][3]["failures"][0]["phase"] == "verify"
    assert report["conclusion"]["status"] == "continue"


def test_budget_exhaustion_is_replayed_as_failed_with_rejected_recovery():
    state = AgentState()
    state.begin_task("budget")
    executor = _executor(state)
    _record(executor, state, "run_shell", {"command": "false", "purpose": "verification"})
    with patch("mini_agent.state.MAX_REPAIR_CYCLES", 0):
        _record(executor, state, "recover", {
            "action": "retry", "caused_by_failure_id": "f-1",
            "reason": "no budget", "requested_attempt": "a-1",
        })
    report = build_trace(state.snapshot())
    assert report["conclusion"]["status"] == "failed"
    assert "预算" in report["conclusion"]["terminal_reason"]
    assert report["generations"][1]["recovery_actions"][0]["status"] == "rejected"
    assert report["integrity"]["status"] == "complete"


def test_cause_hint_has_priority_and_ask_maps_to_blocked():
    state = AgentState()
    state.begin_task("ask")
    executor = _executor(state)
    _record(executor, state, "run_shell", {"command": "false", "purpose": "verification"})
    # Existing FailureEvent data is not rewritten by replay; this models a
    # recorded diagnosis supplied by a caller that owns that field.
    state.failures[0] = type(state.failures[0])(
        **{**state.snapshot()["failures"][0], "cause_hint": "explicit diagnosis"}
    )
    _record(executor, state, "recover", {
        "action": "ask", "caused_by_failure_id": "f-1", "reason": "fallback reason",
    })

    report = build_trace(state.snapshot())
    assert report["generations"][1]["failures"][0]["diagnosis"] == "explicit diagnosis"
    assert report["generations"][1]["failures"][0]["diagnosis_source"] == "cause_hint"
    assert report["conclusion"]["status"] == "blocked"
    assert report["conclusion"]["terminal_reason"]
    assert report["conclusion"]["last_failure"]["failure_id"] == "f-1"


def test_permission_denial_is_replayed_without_handler_admission():
    state = AgentState()
    state.begin_task("permission")
    executor = _executor(state, {"run_shell": DENY})
    result = _record(executor, state, "run_shell", {"command": "echo no", "purpose": "execution"})
    assert result.permission == "denied"
    report = build_trace(state.snapshot())
    attempt = report["generations"][0]["attempts"][0]
    assert attempt["handler_admitted"] is False
    assert attempt["permission"] == "denied"
    assert report["conclusion"]["status"] == "failed"
    assert report["integrity"]["status"] == "complete"


def test_checkpoint_rollback_and_new_verification_form_a_complete_chain():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        target = root / "sample.txt"
        target.write_text("before", encoding="utf-8")
        state = AgentState()
        state.begin_task("rollback")
        executor = _executor(state, {
            "write_file": ALLOW, "run_shell": ALLOW, "recover": ALLOW,
            "rollback_checkpoint": ALLOW,
        }, str(root))
        _record(executor, state, "write_file", {"path": str(target), "content": "after"})
        _record(executor, state, "run_shell", {"command": "false", "purpose": "verification"})
        _record(executor, state, "recover", {
            "action": "rollback", "caused_by_failure_id": "f-1",
            "reason": "restore known-good file", "checkpoint_id": "cp-1",
        })
        _record(executor, state, "run_shell", {
            "command": f'test "$(cat {target})" = before', "purpose": "verification",
        })
        state.status = "done"

        report = build_trace(state.snapshot())
        assert target.read_text(encoding="utf-8") == "before"
        assert report["integrity"]["status"] == "complete"
        assert report["conclusion"]["status"] == "done"
        assert report["generations"][-1]["verification_evidence"][0]["outcome"] == "passed"
        assert report["generations"][1]["recovery_actions"] == []
        assert report["generations"][-2]["recovery_actions"][0]["checkpoint_id"] == "cp-1"


def test_generation_filter_and_snapshot_are_read_only():
    state = AgentState()
    state.begin_task("filter")
    _commit_plan(state, "todo")
    snapshot = state.snapshot()
    original = deepcopy(snapshot)
    report = build_trace(snapshot, 0)
    rendered = render_trace(report)
    assert report["query"]["scope"] == "generation"
    assert [item["generation_id"] for item in report["generations"]] == [0]
    assert snapshot == original
    assert state.snapshot() == original
    assert "Generation 0" in rendered


def test_damaged_snapshot_marks_unresolved_edges_without_guessing():
    state = AgentState()
    state.begin_task("damage")
    _commit_plan(state, "done", "completed")
    snapshot = state.snapshot()
    snapshot["attempts"] = [{
        "attempt_id": "a-1", "pre_generation_id": 0, "generation_id": 0,
        "tool": "run_shell", "arguments_hash": "hash", "redacted_arguments": {},
        "outcome": "failed", "duration_ms": 1, "effect_class": "none",
        "handler_admitted": True, "permission": "allowed", "failure_id": "f-1",
    }]
    snapshot["failures"] = [{
        "failure_id": "f-1", "generation_id": 1, "phase": "verify",
        "category": "validation", "retryable": True,
        "caused_by_attempt_id": "a-1",
    }]
    damaged_evidence = [{
        "command": "check", "outcome": "passed", "exit_code": 0, "output": "ok",
        "generation_id": 0, "caused_by_attempt_id": "missing",
    }]
    snapshot["verification_evidence"] = damaged_evidence
    snapshot["verification_history"] = damaged_evidence
    report = build_trace(snapshot)
    assert report["integrity"]["status"] == "incomplete"
    assert any("同一 generation" in issue for issue in report["integrity"]["issues"])
    assert any("来源 attempt" in issue for issue in report["integrity"]["issues"])
    assert any(edge["status"] == "unresolved" for edge in report["causal_edges"])
    assert "UNRESOLVED" in render_trace(report)


def test_old_verification_evidence_is_incomplete_even_when_its_source_exists():
    state = AgentState()
    state.begin_task("stale verification")
    executor = _executor(state)
    _record(executor, state, "run_shell", {"command": "true", "purpose": "verification"})
    snapshot = state.snapshot()
    snapshot["generations"].append({
        "generation_id": 2, "opened_by_attempt_id": "a-2",
        "opened_by_failure_id": None, "opened_by_recovery_id": None,
        "open_reason": "possible_effect",
    })
    snapshot["attempts"].append({
        "attempt_id": "a-2", "pre_generation_id": 1, "generation_id": 2,
        "tool": "mutate", "arguments_hash": "hash", "redacted_arguments": {},
        "outcome": "succeeded", "duration_ms": 1, "effect_class": "possible",
        "handler_admitted": True, "permission": "allowed",
    })
    snapshot["current_generation_id"] = 2
    report = build_trace(snapshot)
    assert report["integrity"]["status"] == "incomplete"
    assert any("复用了旧 generation" in issue for issue in report["integrity"]["issues"])


def test_duplicate_ids_and_invalid_queries_are_explicit():
    state = AgentState()
    state.begin_task("duplicate")
    snapshot = state.snapshot()
    snapshot["generations"].append(deepcopy(snapshot["generations"][0]))
    report = build_trace(snapshot)
    assert report["integrity"]["status"] == "incomplete"
    assert any("generation ID 重复" in issue for issue in report["integrity"]["issues"])
    with pytest.raises(TraceQueryError):
        build_trace(snapshot, "0")
    with pytest.raises(TraceQueryError):
        build_trace(snapshot, 99)


def test_missing_verification_history_for_an_admitted_attempt_is_incomplete():
    state = AgentState()
    state.begin_task("missing evidence")
    executor = _executor(state)
    _record(executor, state, "run_shell", {"command": "true", "purpose": "verification"})
    snapshot = state.snapshot()
    snapshot["verification_history"] = []

    report = build_trace(snapshot)
    assert report["integrity"]["status"] == "incomplete"
    assert any("必须有且只有一条历史证据" in issue for issue in report["integrity"]["issues"])


def test_failure_links_must_be_reciprocal():
    state = AgentState()
    state.begin_task("broken failure backlink")
    executor = _executor(state)
    _record(executor, state, "run_shell", {"command": "false", "purpose": "verification"})
    snapshot = state.snapshot()
    snapshot["attempts"][0]["failure_id"] = None

    report = build_trace(snapshot)
    assert report["integrity"]["status"] == "incomplete"
    assert any("反向引用" in issue for issue in report["integrity"]["issues"])
    assert any(edge["status"] == "unresolved" for edge in report["causal_edges"])


def test_generation_opener_must_match_reason_and_generation():
    state = AgentState()
    state.begin_task("wrong opener")
    executor = _executor(state)
    _record(executor, state, "run_shell", {"command": "true", "purpose": "verification"})
    snapshot = state.snapshot()
    snapshot["generations"].append({
        "generation_id": 2,
        "opened_by_attempt_id": "a-1",
        "opened_by_failure_id": None,
        "opened_by_recovery_id": None,
        "open_reason": "possible_effect",
    })
    snapshot["current_generation_id"] = 2
    snapshot["verification_evidence"] = []

    report = build_trace(snapshot)
    assert report["integrity"]["status"] == "incomplete"
    assert any("opener attempt 未打开" in issue for issue in report["integrity"]["issues"])
    assert any(edge["status"] == "unresolved" for edge in report["causal_edges"])


def test_recovery_generation_and_attempt_have_one_causal_predecessor():
    state = AgentState()
    state.begin_task("single predecessor")
    registry = create_registry(state)
    shell = registry.get("run_shell")
    original = shell.handler
    outputs = iter(("[exit=1] initial", "[exit=0] retry"))
    shell.handler = lambda command, purpose="execution": next(outputs)
    executor = ToolExecutor(
        registry,
        PermissionGate(PermissionPolicy({"run_shell": ALLOW, "recover": ALLOW})),
    )
    try:
        _record(executor, state, "run_shell", {"command": "check", "purpose": "verification"})
        _record(executor, state, "recover", {
            "action": "retry", "caused_by_failure_id": "f-1",
            "reason": "retry", "requested_attempt": "a-1",
        })
    finally:
        shell.handler = original

    snapshot = state.snapshot()
    generation = snapshot["generations"][2]
    attempt = snapshot["attempts"][1]
    assert generation["opened_by_failure_id"] is None
    assert generation["opened_by_recovery_id"] == "r-1"
    assert attempt["caused_by_failure_id"] == "f-1"
    assert attempt["caused_by_attempt_id"] is None
    assert build_trace(snapshot)["integrity"]["status"] == "complete"


def test_report_does_not_expose_private_arguments_checkpoint_bytes_or_absolute_paths():
    snapshot = {
        "task": "safe replay", "current_goal": "", "status": "done",
        "terminal_reason": "", "current_generation_id": 0,
        "generations": [{"generation_id": 0, "open_reason": "task_start"}],
        "attempts": [{
            "attempt_id": "a-1", "pre_generation_id": 0, "generation_id": 0,
            "tool": "write_file", "arguments_hash": "hash",
            "redacted_arguments": {
                "path": "/private/work/app.py", "content": "PRIVATE_FILE_CONTENT",
                "token": "PRIVATE_TOKEN",
            },
            "outcome": "succeeded", "duration_ms": 1, "effect_class": "possible",
            "handler_admitted": True, "permission": "allowed", "output_excerpt": "ok",
            "private_arguments": {"content": "PRIVATE_FILE_CONTENT"},
            "checkpoint_bytes": b"PRIVATE_CHECKPOINT_BYTES",
        }],
        "failures": [], "recovery_actions": [], "verification_evidence": [],
        "todo_revisions": [],
        "checkpoints": [{"path": "app.py", "before_bytes": b"PRIVATE_CHECKPOINT_BYTES"}],
    }
    report = build_trace(snapshot)
    text = render_trace(report)
    assert "PRIVATE_FILE_CONTENT" not in text
    assert "PRIVATE_TOKEN" not in text
    assert "PRIVATE_CHECKPOINT_BYTES" not in text
    assert "/private/work/app.py" not in text
    assert "<absolute-path>" in text


def test_cli_trace_is_visible_in_quiet_mode_and_does_not_run_task():
    class Session:
        def __init__(self):
            self.values = iter(("/trace", "task", "/trace 0", "/trace bad", "exit"))

        def read(self, prompt):
            return next(self.values)

    calls = []
    session = Session()
    output = []

    def fake_loop(context, executor):
        calls.append("llm")
        return "done"

    with patch.object(cli, "InputSession", return_value=session), \
            patch.object(cli, "agent_loop", side_effect=fake_loop), \
            patch.object(cli, "OUTPUT_MODE", "quiet"), \
            patch("sys.argv", ["mini_agent"]):
        from contextlib import redirect_stdout
        from io import StringIO
        stream = StringIO()
        with redirect_stdout(stream):
            cli.main()
        output.append(stream.getvalue())

    text = output[0]
    assert "当前没有活动任务" in text
    assert "Trace & Replay" in text
    assert "用法: /trace" in text
    assert calls == ["llm"]
