"""v0.28 task-owned process control and lifecycle causality."""
from __future__ import annotations

from copy import deepcopy
import json
import os
import time
from unittest.mock import patch

import pytest

from mini_agent.permission import ALLOW, DENY, PermissionGate, PermissionPolicy
from mini_agent.agent import agent_loop
from mini_agent.context import ContextManager
from mini_agent.processes import ProcessManager
from mini_agent.state import AgentState
from mini_agent.tools import create_registry
from mini_agent.tools.base import ToolExecutor
from mini_agent.trace import build_trace
from test_process_management_v027 import _python, _start


def _runtime(rules=None):
    state = AgentState()
    state.begin_task("control")
    manager = ProcessManager(grace_seconds=.12)
    registry = create_registry(state, process_manager=manager)
    policy = {"start_process": ALLOW, "terminate_process": ALLOW, "kill_process": ALLOW,
              "get_process": ALLOW}
    policy.update(rules or {})
    executor = ToolExecutor(registry, PermissionGate(PermissionPolicy(policy)))
    return state, manager, registry, executor


def _control(state, manager, executor, name, process_id):
    result = executor.execute_result(name, {"process_id": process_id}, state)
    state.record_execution_result(result)
    state.sync_processes(manager.sync_processes(state.task_id))
    return result, json.loads(result.output)


def test_terminate_records_one_controlled_exit_and_invalidates_old_verification():
    state, manager, registry, executor = _runtime()
    assert registry.get("terminate_process").effect_class == "possible"
    pid = _start(state, executor, _python("import time; time.sleep(10)"))
    try:
        result, payload = _control(state, manager, executor, "terminate_process", pid)
        assert result.outcome == "succeeded"
        assert payload["status"] == "terminated"
        state.sync_processes(manager.sync_processes(state.task_id))
        snap = state.snapshot()
        assert [event["kind"] for event in snap["process_events"]] == ["started", "terminated"]
        assert snap["process_events"][-1]["caused_by_control_attempt_id"] == result.reservation.attempt_id
        assert snap["process_events"][-1]["start_attempt_id"] == snap["processes"][0]["start_attempt_id"]
        assert snap["processes"][0]["status"] == "terminated"
        assert snap["verification_required"]
        assert snap["generations"][-1]["opened_by_process_event_id"] == snap["process_events"][-1]["event_id"]
        assert build_trace(snap)["integrity"]["status"] == "complete"
        broken = deepcopy(snap)
        broken["process_events"][-1]["caused_by_control_attempt_id"] = "a-missing"
        assert build_trace(broken)["integrity"]["status"] == "incomplete"
    finally:
        assert manager.cleanup(state.task_id).complete


def test_term_timeout_then_kill_and_already_exited():
    state, manager, _, executor = _runtime()
    pid = _start(state, executor, _python(
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('ready',flush=True); time.sleep(10)"
    ))
    try:
        time.sleep(.08)
        first, payload = _control(state, manager, executor, "terminate_process", pid)
        assert first.outcome == "succeeded" and payload["status"] == "still_running"
        assert [event["kind"] for event in state.snapshot()["process_events"]] == ["started"]
        second, payload = _control(state, manager, executor, "kill_process", pid)
        assert second.outcome == "succeeded" and payload["status"] == "killed"
        assert [event["kind"] for event in state.snapshot()["process_events"]] == ["started", "killed"]
        third, payload = _control(state, manager, executor, "kill_process", pid)
        assert third.outcome == "succeeded" and payload["status"] == "already_exited"
        assert len(state.snapshot()["process_events"]) == 2
    finally:
        assert manager.cleanup(state.task_id).complete


def test_natural_exit_before_control_keeps_natural_event():
    state, manager, _, executor = _runtime()
    pid = _start(state, executor, _python("pass"))
    try:
        deadline = time.monotonic() + 1
        while manager.get_owned(state.task_id, pid).refresh().status == "running":
            assert time.monotonic() < deadline
            time.sleep(.01)
        result, payload = _control(state, manager, executor, "terminate_process", pid)
        assert result.outcome == "succeeded" and payload["status"] == "already_exited"
        assert [event["kind"] for event in state.snapshot()["process_events"]] == ["started", "exited"]
        assert state.snapshot()["process_events"][-1]["caused_by_control_attempt_id"] is None
        assert result.reservation.attempt_id in [item["attempt_id"] for item in state.snapshot()["attempts"]]
    finally:
        assert manager.cleanup(state.task_id).complete


def test_invalid_cross_task_and_denied_do_not_signal_or_reserve():
    state, manager, _, executor = _runtime({"terminate_process": DENY})
    pid = _start(state, executor, _python("import time; time.sleep(10)"))
    try:
        before = state.snapshot()
        invalid = executor.execute_result("terminate_process", {"process_id": "proc-other"}, state)
        assert invalid.error_kind == "unknown_process_id" and invalid.permission == "not_checked"
        denied = executor.execute_result("terminate_process", {"process_id": pid}, state)
        assert denied.error_kind == "permission_denied" and denied.reservation is None
        assert state.snapshot()["generations"] == before["generations"]
        other = AgentState()
        other.begin_task("other")
        other.task_id = "task-other"
        create_registry(other, process_manager=manager)
        cross = executor.execute_result("kill_process", {"process_id": pid}, other)
        assert cross.error_kind == "unknown_process_id" and cross.permission == "not_checked"
    finally:
        assert manager.cleanup(state.task_id).complete


def test_signal_failure_has_no_terminal_event():
    state, manager, _, executor = _runtime()
    pid = _start(state, executor, _python("import time; time.sleep(10)"))
    try:
        with patch.object(manager._processes[pid], "_signal_group", return_value=(False, "denied by OS")):
            result, payload = _control(state, manager, executor, "terminate_process", pid)
        assert result.error_kind == "control_failed"
        assert payload["status"] == "error"
        assert [event["kind"] for event in state.snapshot()["process_events"]] == ["started"]
    finally:
        assert manager.cleanup(state.task_id).complete


def test_exploring_rejects_control_before_permission_or_generation():
    state, manager, _, executor = _runtime()
    pid = _start(state, executor, _python("import time; time.sleep(10)"))
    try:
        state.begin_plan()
        before = state.snapshot()["current_generation_id"]
        result = executor.execute_result("kill_process", {"process_id": pid}, state)
        assert result.error_kind == "planning_phase_gate"
        assert result.permission == "not_checked" and result.reservation is None
        assert state.snapshot()["current_generation_id"] == before
        assert manager.get_owned(state.task_id, pid).refresh().status == "running"
    finally:
        assert manager.cleanup(state.task_id).complete


def test_diagnosis_and_strict_verification_reject_direct_control():
    state, manager, _, executor = _runtime({"run_shell": {"false": ALLOW}})
    pid = _start(state, executor, _python("import time; time.sleep(10)"))
    try:
        failed = executor.execute_result("run_shell", {"command": "false"}, state)
        state.record_execution_result(failed)
        assert state.repair_phase == "diagnosis_required"
        before = state.snapshot()["current_generation_id"]
        rejected = executor.execute_result("terminate_process", {"process_id": pid}, state)
        assert rejected.error_kind == "repair_phase_gate"
        assert rejected.permission == "not_checked" and rejected.reservation is None
        assert state.snapshot()["current_generation_id"] == before
        state._repair_phase = "verification_required"
        rejected = executor.execute_result("kill_process", {"process_id": pid}, state)
        assert rejected.error_kind == "repair_phase_gate"
        assert rejected.permission == "not_checked" and rejected.reservation is None
        assert manager.get_owned(state.task_id, pid).refresh().status == "running"
    finally:
        assert manager.cleanup(state.task_id).complete


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
def test_control_cleans_shell_descendant_group():
    state, manager, _, executor = _runtime()
    pid = _start(state, executor, "sleep 10 & wait")
    try:
        result, payload = _control(state, manager, executor, "terminate_process", pid)
        assert result.outcome == "succeeded" and payload["status"] == "terminated"
        assert manager._processes[pid]._group_gone()
        assert state.snapshot()["processes"][0]["status"] == "terminated"
    finally:
        assert manager.cleanup(state.task_id).complete


def test_control_round_returns_exactly_one_tool_message_per_call():
    state, manager, _, executor = _runtime()
    pid = _start(state, executor, _python("import time; time.sleep(10)"))
    context = ContextManager(state, [{"role": "user", "content": "stop process"}], observability=False)
    first = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "term", "type": "function", "function": {
            "name": "terminate_process", "arguments": json.dumps({"process_id": pid})}},
        {"id": "get", "type": "function", "function": {
            "name": "get_process", "arguments": json.dumps({"process_id": pid})}},
    ]}
    calls = 0

    class ObservedRound(Exception):
        pass

    def reply(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return first
        tool_messages = [item for item in context.history if item.get("role") == "tool"]
        assert [item["tool_call_id"] for item in tool_messages] == ["term", "get"]
        assert json.loads(tool_messages[0]["content"])["status"] == "terminated"
        assert json.loads(tool_messages[1]["content"])["status"] == "terminated"
        raise ObservedRound

    try:
        with patch("mini_agent.agent.call_llm", side_effect=reply):
            with pytest.raises(ObservedRound):
                agent_loop(context, executor)
    finally:
        assert manager.cleanup(state.task_id).complete


def test_control_exit_requires_new_independent_verification():
    state, manager, _, executor = _runtime({"run_shell": {"true": ALLOW}})
    pid = _start(state, executor, _python("import time; time.sleep(10)"))
    try:
        old = executor.execute_result("run_shell", {"command": "true", "purpose": "verification"}, state)
        state.record_execution_result(old)
        assert state.has_verification_evidence()
        _control(state, manager, executor, "terminate_process", pid)
        assert not state.has_verification_evidence()
        assert state.snapshot()["verification_required"]
        fresh = executor.execute_result("run_shell", {"command": "true", "purpose": "verification"}, state)
        state.record_execution_result(fresh)
        assert state.has_verification_evidence()
        assert state.snapshot()["verification_evidence"][-1]["generation_id"] == state.snapshot()["current_generation_id"]
        assert build_trace(state.snapshot())["integrity"]["status"] == "complete"
    finally:
        assert manager.cleanup(state.task_id).complete
