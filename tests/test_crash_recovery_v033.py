"""v0.33 crash-recovery handoff tests.

The fixtures stop at a durable boundary.  They never call the old handler,
which makes the no-replay property explicit and deterministic.
"""

from __future__ import annotations

from pathlib import Path
from dataclasses import replace
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mini_agent.context import ContextManager
from mini_agent.resume import prepare_resume
from mini_agent.session import (
    DurableToolBoundary,
    SessionError,
    SessionSizeError,
    SessionStore,
    SessionValidationError,
)
from mini_agent.state import AgentState, PlanRejected
from mini_agent.tools.base import ExecutionResult
from mini_agent.trace import build_trace


def _pending_session(tmp_path: Path, *, admitted: bool, effect: str = "possible",
                     tool: str = "write_file", arguments: dict | None = None):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(tmp_path / "sessions")
    state = AgentState()
    state.begin_task("recover an interrupted call")
    context = ContextManager(state, [{"role": "user", "content": "inspect the task"}], observability=False)
    envelope = store.save(None, state, context, workspace_root=workspace)
    boundary = DurableToolBoundary(store, envelope["session_id"], workspace)
    call_arguments = arguments or {
        "path": "secret.txt", "content": "do not replay",
    }
    assistant = {
        "role": "assistant", "content": None, "tool_calls": [{
            "id": "call-1", "type": "function", "function": {
                "name": tool, "arguments": json.dumps(call_arguments),
            },
        }],
    }
    context.history.append(assistant)
    call = {
        "invocation_id": "inv-1", "tool_call_id": "call-1", "tool": tool,
        "arguments": call_arguments,
        "effect_class": effect,
    }
    boundary.start_round(1, assistant, [call], state, context)
    if admitted:
        reservation = None
        if tool not in {"commit_plan", "recover"}:
            reservation = state.reserve_attempt(effect, tool, call["arguments"])
        admission = SimpleNamespace(effect_class=effect, reservation=reservation)
        boundary.record_admission("inv-1", admission, state, context)
    source_path = store.path_for(envelope["session_id"])
    source_bytes = source_path.read_bytes()
    return workspace, store, envelope, source_path, source_bytes


def test_pending_handler_admission_becomes_uncertain_without_replay(tmp_path: Path):
    workspace, store, envelope, source_path, source_bytes = _pending_session(
        tmp_path, admitted=True, effect="possible",
    )

    candidate = prepare_resume(store, envelope["session_id"], workspace)
    assert candidate.recovery_mode == "crash_recovery"
    runtime = candidate.claim()

    assert runtime.session_id != envelope["session_id"]
    assert source_path.read_bytes() == source_bytes
    assert store.load(envelope["session_id"])["tool_boundary"]["status"] == "pending"
    derived = store.load(runtime.session_id)
    assert derived["tool_boundary"]["status"] == "committed"
    result = derived["tool_boundary"]["calls"][0]["result"]
    assert result["outcome"] == "uncertain"

    issue = runtime.state.unresolved_crash_issues[0]
    assert issue.classification == "uncertain_side_effect"
    assert runtime.state.crash_recovery_gate("write_file", {}, "possible") is not None
    assert runtime.state.crash_recovery_gate("read_file", {}, "none") is not None
    runtime.state.resolve_crash_issue(issue.issue_id, "investigate", "开始只读调查")
    assert runtime.state.crash_recovery_gate("read_file", {}, "none") is None
    report = build_trace(runtime.state.snapshot())
    assert report["integrity"] == {"status": "complete", "issues": []}
    assert report["conclusion"]["evidence"]["crash_recovery_complete"] is False
    assert report["conclusion"]["evidence"]["verification_complete"] is False

    with pytest.raises(SessionValidationError, match="已经派生"):
        prepare_resume(store, envelope["session_id"], workspace).claim()


def test_not_admitted_call_gets_explicit_interrupted_result(tmp_path: Path):
    workspace, store, envelope, source_path, source_bytes = _pending_session(
        tmp_path, admitted=False, effect="possible",
    )
    runtime = prepare_resume(store, envelope["session_id"], workspace).claim()

    issue = runtime.state.crash_issues[0]
    assert issue.classification == "not_executed"
    assert issue.status == "continued"
    assert source_path.read_bytes() == source_bytes
    derived = store.load(runtime.session_id)
    result = derived["tool_boundary"]["calls"][0]["result"]
    assert result["error_kind"] == "interrupted_before_handler"
    assert result["outcome"] == "invalid"
    assert "未执行" in result["content"]
    assert runtime.state.has_unresolved_crash_recovery() is False


def test_crash_handoff_does_not_persist_stdin_body(tmp_path: Path):
    secret = "CRASH-RECOVERY-STDIN-SECRET"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(tmp_path / "sessions")
    state = AgentState()
    state.begin_task("redact interrupted stdin")
    context = ContextManager(state, [{"role": "user", "content": "send input"}], observability=False)
    envelope = store.save(None, state, context, workspace_root=workspace)
    boundary = DurableToolBoundary(store, envelope["session_id"], workspace)
    assistant = {
        "role": "assistant", "content": None, "tool_calls": [{
            "id": "call-stdin", "type": "function", "function": {
                "name": "write_process", "arguments": json.dumps({
                    "process_id": "proc-1", "input": secret, "close_stdin": False,
                }),
            },
        }],
    }
    context.history.append(assistant)
    boundary.start_round(1, assistant, [{
        "invocation_id": "inv-stdin", "tool_call_id": "call-stdin", "tool": "write_process",
        "arguments": {"process_id": "proc-1", "input": secret, "close_stdin": False},
        "effect_class": "possible",
    }], state, context)
    source_path = store.path_for(envelope["session_id"])
    runtime = prepare_resume(store, envelope["session_id"], workspace).claim()
    sidecar = store._crash_claim_path()
    for path in (source_path, store.path_for(runtime.session_id), sidecar):
        assert secret.encode() not in path.read_bytes()


def test_issue_requires_investigation_before_continue_and_then_replans(tmp_path: Path):
    workspace, store, envelope, _, _ = _pending_session(
        tmp_path, admitted=True, effect="none",
    )
    runtime = prepare_resume(store, envelope["session_id"], workspace).claim()
    issue = runtime.state.unresolved_crash_issues[0]

    with pytest.raises(PlanRejected, match="investigate"):
        runtime.state.resolve_crash_issue(issue.issue_id, "continue", "先调查")
    runtime.state.resolve_crash_issue(issue.issue_id, "investigate", "读取相关文件")
    observation = ExecutionResult(
        "read_file", {"path": "README.md"}, "allowed", True, "succeeded", 0,
        "none", "read-only observation", "read-only observation",
    )
    runtime.state.record_execution_result(observation)
    decision = runtime.state.resolve_crash_issue(issue.issue_id, "continue", "调查后确认可以继续")

    assert decision.investigation_attempt_id is not None
    assert runtime.state.has_unresolved_crash_recovery() is False
    assert runtime.state.replan_triggers[-1].kind == "crash_recovery"
    assert runtime.state.planning_state.phase == "exploring"
    report = build_trace(runtime.state.snapshot())
    assert report["integrity"] == {"status": "complete", "issues": []}
    assert any(edge["type"] == "crash_recovery_decision" for edge in report["causal_edges"])


def test_crash_recovery_preserves_terminal_source_and_rejects_continue(tmp_path: Path):
    state = AgentState()
    state.begin_task("terminal source")
    state.status = "blocked"
    state.terminal_reason = "budget_exhausted"
    record = state.begin_crash_recovery(
        "source-session", 1, 1, "a" * 64, 1,
        [{
            "invocation_id": "inv-1", "tool": "write_file", "effect_class": "possible",
            "handler_admitted": True, "attempt_id": "a-1", "generation_id": 0,
            "pre_generation_id": 0, "permission": "allowed", "arguments_hash": "b" * 64,
            "arguments_summary": {"path": "x.txt", "content": "<str:1>"},
        }],
    )
    assert record.status == "resolving"
    assert state.status == "blocked"
    assert state.terminal_reason == "budget_exhausted"
    with pytest.raises(PlanRejected, match="终止"):
        state.resolve_crash_issue("issue-1", "continue", "接受风险")


def test_crash_investigation_bypasses_repair_gate_but_preserves_verification(tmp_path: Path):
    workspace, store, envelope, _, _ = _pending_session(
        tmp_path, admitted=True, effect="none",
    )
    observed = workspace / "observed.txt"
    observed.write_text("read-only fact", encoding="utf-8")
    runtime = prepare_resume(store, envelope["session_id"], workspace).claim()
    issue = runtime.state.unresolved_crash_issues[0]
    runtime.state.resolve_crash_issue(issue.issue_id, "investigate", "检查当前文件")
    runtime.state._repair_phase = "verification_required"
    runtime.state._verification_required = True

    result = runtime.tool_executor.execute_result(
        "read_file", {"path": str(observed)}, state=runtime.state, notify=False,
    )
    assert result.handler_admitted is True
    assert result.error_kind != "repair_phase_gate"
    assert result.outcome == "succeeded"
    runtime.state.record_execution_result(result)
    runtime.state.resolve_crash_issue(issue.issue_id, "continue", "调查结果可供用户决定")
    assert runtime.state.repair_phase == "verification_required"
    assert runtime.state.snapshot()["verification_required"] is True


def test_orphaned_process_and_pending_stdin_become_settleable_issues(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(tmp_path / "sessions")
    state = AgentState()
    state.begin_task("orphan process")
    context = ContextManager(state, [{"role": "user", "content": "start"}], observability=False)
    envelope = store.save(None, state, context, workspace_root=workspace)
    start_args = {"command": "python worker.py", "cwd": ".", "stdin_mode": "pipe"}
    reservation = state.reserve_attempt("possible", "start_process", start_args)
    started = ExecutionResult(
        "start_process", start_args, "allowed", True, "succeeded", 0, "possible",
        "started", "started", process_metadata={
            "process_id": "proc-1", "task_id": state.task_id, "pid": 4321,
            "command": "python worker.py", "cwd": str(workspace),
            "started_at": "2026-01-01T00:00:00Z", "stdin_mode": "pipe",
        }, reservation=reservation,
    )
    state.record_execution_result(started)
    state.process_records[0] = replace(
        state.process_records[0], write_pending=True, stdin_state="write_pending",
    )
    boundary = DurableToolBoundary(store, envelope["session_id"], workspace)
    assistant = {"role": "assistant", "content": None, "tool_calls": [{
        "id": "call-1", "type": "function", "function": {
            "name": "read_file", "arguments": json.dumps({"path": "missing.txt"}),
        },
    }]}
    context.history.append(assistant)
    boundary.start_round(1, assistant, [{
        "invocation_id": "inv-1", "tool_call_id": "call-1", "tool": "read_file",
        "arguments": {"path": "missing.txt"}, "effect_class": "none",
    }], state, context)

    runtime = prepare_resume(store, envelope["session_id"], workspace).claim()
    process = runtime.state.process_records[0]
    assert process.status == "orphaned"
    assert process.write_pending is False
    process_issue = next(item for item in runtime.state.crash_issues if item.tool == "process")
    assert process_issue.status == "unresolved"
    assert runtime.state.session_safety_issues() == []
    assert runtime.state.completion_reminder()["crash_recovery"]["issues"]

    runtime.state.resolve_crash_issue(process_issue.issue_id, "investigate", "读取旧进程观察")
    observation = ExecutionResult(
        "read_process", {"process_id": "proc-1"}, "allowed", True, "succeeded", 0,
        "none", "旧 PID 状态不可控制", "旧 PID 状态不可控制",
    )
    runtime.state.record_execution_result(observation)
    runtime.state.resolve_crash_issue(process_issue.issue_id, "continue", "接受旧进程状态不确定")
    assert runtime.state.active_process_records() == []
    assert runtime.state.completion_reminder() is not None


def test_workspace_drift_is_issue_and_claim_detects_second_change(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    watched = workspace / "watched.txt"
    watched.write_text("v1", encoding="utf-8")
    store = SessionStore(tmp_path / "sessions")
    state = AgentState()
    state.begin_task("workspace drift")
    context = ContextManager(state, [{"role": "user", "content": "inspect"}], observability=False)
    reservation = state.reserve_attempt("none", "read_file", {"path": "watched.txt"})
    state.record_execution_result(ExecutionResult(
        "read_file", {"path": "watched.txt"}, "allowed", True, "succeeded", 0,
        "none", "v1", "v1", reservation=reservation,
    ))
    envelope = store.save(None, state, context, workspace_root=workspace)
    boundary = DurableToolBoundary(store, envelope["session_id"], workspace)
    assistant = {"role": "assistant", "content": None, "tool_calls": [{
        "id": "call-1", "type": "function", "function": {
            "name": "read_file", "arguments": json.dumps({"path": "other.txt"}),
        },
    }]}
    context.history.append(assistant)
    boundary.start_round(1, assistant, [{
        "invocation_id": "inv-1", "tool_call_id": "call-1", "tool": "read_file",
        "arguments": {"path": "other.txt"}, "effect_class": "none",
    }], state, context)
    watched.write_text("v2", encoding="utf-8")
    candidate = prepare_resume(store, envelope["session_id"], workspace)
    watched.write_text("v3", encoding="utf-8")
    with pytest.raises(SessionValidationError, match="结构化观察"):
        candidate.claim()
    runtime = prepare_resume(store, envelope["session_id"], workspace).claim()
    workspace_issue = next(item for item in runtime.state.crash_issues
                           if item.classification == "workspace_drift")
    assert workspace_issue.status == "unresolved"
    assert runtime.state.has_unresolved_crash_recovery()


def test_claim_preparing_record_retries_same_derived_id_after_branch_failure(tmp_path: Path):
    workspace, store, envelope, _, _ = _pending_session(
        tmp_path, admitted=True, effect="possible",
    )
    candidate = prepare_resume(store, envelope["session_id"], workspace)
    original_write = store._write_atomic
    failed = {"value": True}

    def fail_derived(session_id, value):
        if session_id != envelope["session_id"] and failed["value"]:
            failed["value"] = False
            raise OSError("injected derived replace failure")
        return original_write(session_id, value)

    store._write_atomic = fail_derived
    with pytest.raises(OSError, match="injected"):
        candidate.claim()
    claims = store._read_crash_claims()
    assert claims[0]["status"] == "preparing"
    derived_id = claims[0]["derived_session_id"]
    store._write_atomic = original_write
    runtime = prepare_resume(store, envelope["session_id"], workspace).claim()
    assert runtime.session_id == derived_id
    assert store._read_crash_claims()[0]["status"] == "committed"


def test_claim_retry_rewrites_unpublished_branch_from_fresh_workspace_observation(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    watched = workspace / "watched.txt"
    watched.write_text("v1", encoding="utf-8")
    store = SessionStore(tmp_path / "sessions")
    state = AgentState()
    state.begin_task("recover after claim publication failure")
    observed = state.reserve_attempt("none", "read_file", {"path": "watched.txt"})
    state.record_execution_result(ExecutionResult(
        "read_file", {"path": "watched.txt"}, "allowed", True, "succeeded", 0,
        "none", "v1", "v1", reservation=observed,
    ))
    context = ContextManager(state, [{"role": "user", "content": "continue"}], observability=False)
    envelope = store.save(None, state, context, workspace_root=workspace)
    boundary = DurableToolBoundary(store, envelope["session_id"], workspace)
    arguments = {"path": "other.txt", "content": "x"}
    assistant = {
        "role": "assistant", "content": None, "tool_calls": [{
            "id": "call-1", "type": "function", "function": {
                "name": "write_file", "arguments": json.dumps(arguments),
            },
        }],
    }
    context.history.append(assistant)
    boundary.start_round(1, assistant, [{
        "invocation_id": "inv-1", "tool_call_id": "call-1", "tool": "write_file",
        "arguments": arguments, "effect_class": "possible",
    }], state, context)
    reservation = state.reserve_attempt("possible", "write_file", arguments)
    boundary.record_admission(
        "inv-1", SimpleNamespace(effect_class="possible", reservation=reservation),
        state, context,
    )

    candidate = prepare_resume(store, envelope["session_id"], workspace)
    original_write_claims = store._write_crash_claims
    writes = {"count": 0}

    def fail_final_publication(claims, source_session_id):
        writes["count"] += 1
        if writes["count"] == 2:
            raise SessionError("injected final claim publication failure")
        return original_write_claims(claims, source_session_id)

    store._write_crash_claims = fail_final_publication
    with pytest.raises(SessionError, match="final claim publication"):
        candidate.claim()
    store._write_crash_claims = original_write_claims
    preparing = store._read_crash_claims()[0]
    assert preparing["status"] == "preparing"
    derived_id = preparing["derived_session_id"]
    assert store.path_for(derived_id).exists()

    watched.write_text("v2", encoding="utf-8")
    runtime = prepare_resume(store, envelope["session_id"], workspace).claim()
    derived = store.load(runtime.session_id)

    assert runtime.session_id == derived_id
    assert {issue.classification for issue in runtime.state.crash_issues} == {
        "workspace_drift", "uncertain_side_effect",
    }
    assert runtime.state.export_session(allow_pending=True) == derived["state"]
    assert runtime.context.export_session() == derived["context"]
    assert store._read_crash_claims()[0]["status"] == "committed"


def test_crash_claim_size_limit_preserves_previous_readable_sidecar(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    first = {
        "source_session_id": "a" * 32,
        "source_integrity": "b" * 64,
        "derived_session_id": "c" * 32,
        "recovery_id": "cr-1",
        "status": "committed",
    }
    store._write_crash_claims([first], first["source_session_id"])
    before = store._crash_claim_path().read_bytes()
    oversized = [
        {
            "source_session_id": f"{index:032x}",
            "source_integrity": f"{index:064x}",
            "derived_session_id": f"{index + 10000:032x}",
            "recovery_id": f"cr-{index + 1}",
            "status": "committed",
        }
        for index in range(700)
    ]

    with pytest.raises(SessionSizeError, match="超过大小上限"):
        store._write_crash_claims(oversized, first["source_session_id"])

    assert store._crash_claim_path().read_bytes() == before
    assert store._read_crash_claims() == [first]


def test_control_boundary_does_not_create_fake_uncertain_attempt(tmp_path: Path):
    workspace, store, envelope, _, _ = _pending_session(
        tmp_path, admitted=True, effect="none", tool="commit_plan", arguments={
            "goal": "goal", "steps": [{"step_id": "step-a", "content": "observe"}],
        },
    )
    runtime = prepare_resume(store, envelope["session_id"], workspace).claim()
    assert runtime.state.crash_issues[0].attempt_id is None
    assert runtime.state.attempts == []
    derived = store.load(runtime.session_id)
    assert derived["tool_boundary"]["calls"][0]["attempt_id"] is None


def test_uncertain_outcome_is_not_a_normal_handler_result():
    state = AgentState()
    state.begin_task("uncertain protocol")
    with pytest.raises(ValueError, match="只能由 crash recovery"):
        state.record_execution_result(ExecutionResult(
            "read_file", {"path": "x"}, "allowed", True, "uncertain", 0,
            "none", "uncertain", "uncertain",
        ))


def test_crash_state_rejects_cross_recovery_issue_and_non_numeric_generation(tmp_path: Path):
    state = AgentState()
    state.begin_task("validate recovery")
    state.begin_crash_recovery(
        "source-session", 1, 1, "a" * 64, 1,
        [{"invocation_id": "one", "tool": "write_file", "effect_class": "possible",
          "handler_admitted": False, "attempt_id": None, "generation_id": None,
          "pre_generation_id": None, "permission": "not_checked", "arguments_summary": {}}],
    )
    payload = state.export_session()
    payload["crash_recoveries"][0]["issue_ids"] = ["issue-999"]
    with pytest.raises(Exception, match="issue"):
        AgentState.validate_session_export(payload)
    payload = state.export_session()
    payload["crash_recoveries"][0]["source_session_generation"] = "not-an-int"
    with pytest.raises(Exception, match="crash recovery"):
        AgentState.validate_session_export(payload)
