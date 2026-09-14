"""v0.26 background process lifecycle and task-boundary acceptance tests."""
from __future__ import annotations

import json
import os
import shlex
import sys
from threading import Thread
import time
from unittest.mock import patch

from mini_agent.agent import agent_loop
from mini_agent.context import ContextManager
from mini_agent.permission import ALLOW, DENY, PermissionGate, PermissionPolicy
from mini_agent.processes import ProcessManager, _ManagedProcess
from mini_agent.state import AgentState
from mini_agent.tools import create_registry
from mini_agent.tools.base import ToolExecutor


def _python(code: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def _runtime(state: AgentState, manager: ProcessManager, rules=None):
    registry = create_registry(state, process_manager=manager)
    policy = PermissionPolicy(rules or {"start_process": {"*": ALLOW}})
    return registry, ToolExecutor(registry, PermissionGate(policy))


def _start(state, executor, command):
    result = executor.execute_result("start_process", {"command": command}, state)
    assert result.outcome == "succeeded", result.output
    state.record_execution_result(result)
    return result


def _finish_manager(state, manager):
    report = manager.cleanup(state.task_id)
    state.record_process_cleanup(report)
    assert report.complete, report.render()


def test_start_process_returns_before_exit_and_next_tool_round_can_continue():
    state = AgentState()
    state.begin_task("start and continue")
    manager = ProcessManager(grace_seconds=0.1)
    registry, executor = _runtime(
        state, manager,
        {"start_process": {"*": ALLOW}, "calculate": ALLOW},
    )
    context = ContextManager(
        state,
        [{"role": "user", "content": "start and continue"}],
        observability=False,
    )
    responses = iter([
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "start", "type": "function",
            "function": {"name": "start_process", "arguments": json.dumps({
                "command": _python("import time; time.sleep(.4)"),
            })},
        }]},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "calc", "type": "function",
            "function": {"name": "calculate", "arguments": '{"expression":"2+2"}'},
        }]},
        {"role": "assistant", "content": "等待服务进程"},
    ])
    started = time.monotonic()
    with patch("mini_agent.agent.call_llm", side_effect=lambda *args, **kwargs: next(responses)):
        assert agent_loop(context, executor) == "等待服务进程"
    assert time.monotonic() - started < 0.35
    assert context.history[4]["content"] == "4"
    assert state.status == "awaiting_process"
    assert state.snapshot()["processes"][0]["status"] == "running"
    start_attempt = state.snapshot()["attempts"][0]
    assert start_attempt["tool"] == "start_process"
    assert json.loads(context.history[2]["content"])["start_attempt_id"] == start_attempt["attempt_id"]
    _finish_manager(state, manager)


def test_start_process_drains_both_streams_with_bounded_memory():
    state = AgentState()
    state.begin_task("drain")
    manager = ProcessManager(max_stream_bytes=64 * 1024, grace_seconds=0.1)
    _, executor = _runtime(state, manager)
    result = _start(
        state, executor,
        _python(
            "import sys,time; "
            "sys.stdout.write('o'*200000); sys.stdout.flush(); "
            "sys.stderr.write('e'*200000); sys.stderr.flush(); time.sleep(.2)"
        ),
    )
    process_id = json.loads(result.output)["process_id"]
    time.sleep(0.35)
    facts = manager.sync_processes(state.task_id)
    state.sync_processes(facts)
    managed = manager._processes[process_id]
    assert managed.stdout_ring.snapshot()[0] == 200000
    assert managed.stderr_ring.snapshot()[0] == 200000
    assert len(managed.stdout_ring.snapshot()[2]) <= 64 * 1024
    assert len(managed.stderr_ring.snapshot()[2]) <= 64 * 1024
    assert state.snapshot()["processes"][0]["status"] == "exited"
    assert [event["kind"] for event in state.snapshot()["process_events"]] == ["started", "exited"]
    _finish_manager(state, manager)


def test_exit_sync_is_idempotent_and_opens_one_successor_generation():
    state = AgentState()
    state.begin_task("lifecycle")
    manager = ProcessManager(grace_seconds=0.1)
    _, executor = _runtime(state, manager)
    result = _start(state, executor, _python("raise SystemExit(3)"))
    time.sleep(0.15)
    state.sync_processes(manager.sync_processes(state.task_id))
    first = state.snapshot()
    state.sync_processes(manager.sync_processes(state.task_id))
    second = state.snapshot()
    assert first["current_generation_id"] == 2
    assert second["current_generation_id"] == 2
    assert [item["kind"] for item in second["process_events"]] == ["started", "failed"]
    assert len(second["process_events"]) == 2
    assert second["process_events"][-1]["start_attempt_id"] == result.reservation.attempt_id
    assert second["processes"][0]["exit_code"] == 3
    assert len(second["failures"]) == 1
    assert second["failures"][0]["caused_by_process_event_id"] == second["process_events"][-1]["event_id"]
    _finish_manager(state, manager)


def test_process_exit_invalidates_verification_from_while_it_was_running():
    state = AgentState()
    state.begin_task("verification boundary")
    manager = ProcessManager(grace_seconds=0.1)
    _, executor = _runtime(
        state, manager,
        {"start_process": {"*": ALLOW}, "run_shell": {"true": ALLOW}},
    )
    _start(state, executor, _python("import time; time.sleep(.2)"))
    verification = executor.execute_result(
        "run_shell", {"command": "true", "purpose": "verification"}, state,
    )
    assert verification.outcome == "succeeded"
    state.record_execution_result(verification)
    assert state.has_verification_evidence()
    generation_before_exit = state.snapshot()["current_generation_id"]

    time.sleep(0.3)
    state.sync_processes(manager.sync_processes(state.task_id))
    snapshot = state.snapshot()
    assert snapshot["current_generation_id"] == generation_before_exit + 1
    assert snapshot["verification_evidence"] == []
    assert snapshot["verification_required"] is True
    _finish_manager(state, manager)


def test_immediate_exit_is_not_lost_before_start_attempt_commit():
    state = AgentState()
    state.begin_task("immediate")
    manager = ProcessManager(grace_seconds=0.1)
    _, executor = _runtime(state, manager)
    result = executor.execute_result("start_process", {"command": _python("raise SystemExit(0)")}, state)
    time.sleep(0.1)
    # An early manager observation must not consume the lifecycle event before
    # the executor commits the start attempt and ProcessRecord.
    manager.sync_processes(state.task_id)
    state.record_execution_result(result)
    state.sync_processes(manager.sync_processes(state.task_id))
    assert [item["kind"] for item in state.snapshot()["process_events"]] == ["started", "exited"]
    _finish_manager(state, manager)


def test_start_process_validation_permission_and_exploring_do_not_spawn():
    state = AgentState()
    state.begin_task("admission")
    manager = ProcessManager(grace_seconds=0.1)
    registry, executor = _runtime(state, manager, {"start_process": {"*": DENY}})
    invalid = executor.execute_result("start_process", {"command": "   "}, state)
    assert invalid.outcome == "invalid"
    assert state.snapshot()["current_generation_id"] == 0
    denied = executor.execute_result("start_process", {"command": "sleep 1"}, state)
    assert denied.outcome == "denied"
    state.record_execution_result(denied)
    assert not manager.active_process_ids(state.task_id)
    assert state.snapshot()["current_generation_id"] == 0

    state.begin_task("exploring")
    state.begin_plan()
    exploring = executor.execute_result("start_process", {"command": "sleep 1"}, state)
    assert exploring.error_kind == "planning_phase_gate"
    assert not manager.active_process_ids(state.task_id)
    assert state.snapshot()["current_generation_id"] == 0
    assert registry.get("start_process").effect_class == "possible"


def test_active_process_limit_is_per_task_and_failed_start_keeps_attempt():
    state = AgentState()
    state.begin_task("quota")
    manager = ProcessManager(max_active_processes=2, grace_seconds=0.1)
    _, executor = _runtime(state, manager)
    _start(state, executor, _python("import time; time.sleep(2)"))
    _start(state, executor, _python("import time; time.sleep(2)"))
    third = executor.execute_result(
        "start_process", {"command": _python("import time; time.sleep(2)")}, state,
    )
    assert third.outcome == "failed"
    assert third.reservation is not None
    state.record_execution_result(third)
    assert len(manager.active_process_ids(state.task_id)) == 2
    assert len(state.snapshot()["processes"]) == 2

    _finish_manager(state, manager)
    state.begin_task("another task")
    _start(state, executor, _python("import time; time.sleep(2)"))
    assert len(manager.active_process_ids(state.task_id)) == 1
    _finish_manager(state, manager)


def test_invalid_cwd_is_a_failed_admitted_start_without_a_process_record(tmp_path):
    state = AgentState()
    state.begin_task("invalid cwd")
    manager = ProcessManager(grace_seconds=0.1)
    _, executor = _runtime(state, manager)
    result = executor.execute_result(
        "start_process", {"command": "echo should-not-run", "cwd": str(tmp_path / "missing")}, state,
    )
    assert result.outcome == "failed"
    assert result.reservation is not None
    state.record_execution_result(result)
    assert state.snapshot()["current_generation_id"] == 1
    assert state.snapshot()["processes"] == []
    assert manager.active_process_ids(state.task_id) == ()


def test_popen_failure_keeps_admitted_failure_without_registering_process():
    state = AgentState()
    state.begin_task("popen failure")
    manager = ProcessManager(grace_seconds=0.1)
    _, executor = _runtime(state, manager)
    with patch("mini_agent.processes.subprocess.Popen", side_effect=OSError("spawn failed")):
        result = executor.execute_result(
            "start_process", {"command": "echo should-not-start"}, state,
        )
    assert result.outcome == "failed"
    assert result.reservation is not None
    state.record_execution_result(result)
    snapshot = state.snapshot()
    assert snapshot["processes"] == []
    assert len(snapshot["failures"]) == 1
    assert manager.active_process_ids(state.task_id) == ()


def test_task_and_process_ids_are_not_reused_after_boundary_cleanup():
    state = AgentState()
    state.begin_task("one")
    manager = ProcessManager(grace_seconds=0.1)
    _, executor = _runtime(state, manager)
    first = _start(state, executor, _python("import time; time.sleep(2)"))
    first_id = json.loads(first.output)["process_id"]
    first_task = state.task_id
    _finish_manager(state, manager)
    state.begin_task("two")
    second = _start(state, executor, _python("import time; time.sleep(2)"))
    second_id = json.loads(second.output)["process_id"]
    assert first_task != state.task_id
    assert first_id != second_id
    assert state.snapshot()["processes"][0]["task_id"] == state.task_id
    _finish_manager(state, manager)


def _eventually(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_short_flushed_output_updates_offsets_while_process_runs():
    state = AgentState()
    state.begin_task("short output")
    manager = ProcessManager(grace_seconds=0.1)
    _, executor = _runtime(state, manager)
    _start(state, executor, _python(
        "import sys,time; sys.stdout.buffer.write(b'hi'); sys.stdout.flush(); "
        "sys.stderr.buffer.write(b'err'); sys.stderr.flush(); time.sleep(2)"
    ))
    assert _eventually(lambda: all(
        (fact.stdout_offset == 2 and fact.stderr_offset == 3)
        for fact in manager.sync_processes(state.task_id)
    ))
    state.sync_processes(manager.sync_processes(state.task_id))
    assert state.snapshot()["processes"][0]["status"] == "running"
    assert state.snapshot()["processes"][0]["stdout_offset"] == 2
    _finish_manager(state, manager)


def test_outer_process_exit_does_not_finish_live_same_group_descendant():
    if os.name != "posix":
        return
    state = AgentState()
    state.begin_task("same group descendant")
    manager = ProcessManager(max_active_processes=1, grace_seconds=0.1)
    _, executor = _runtime(state, manager)
    command = _python(
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2)'])"
    )
    result = _start(state, executor, command)
    managed = manager._processes[json.loads(result.output)["process_id"]]
    assert _eventually(lambda: managed.proc.poll() is not None)
    state.sync_processes(manager.sync_processes(state.task_id))
    assert state.active_process_records()
    assert len(state.snapshot()["process_events"]) == 1
    assert len(manager.active_process_ids(state.task_id)) == 1
    second = executor.execute_result("start_process", {"command": _python("pass")}, state)
    assert second.outcome == "failed"
    assert "活动后台进程已达到上限" in second.output
    _finish_manager(state, manager)


def test_detached_descendant_holding_pipe_delays_exit_and_boundary():
    if os.name != "posix":
        return
    state = AgentState()
    state.begin_task("detached pipe")
    manager = ProcessManager(grace_seconds=0.02)
    _, executor = _runtime(state, manager)
    result = _start(state, executor, _python(
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(.4)'], "
        "start_new_session=True)"
    ))
    managed = manager._processes[json.loads(result.output)["process_id"]]
    assert _eventually(lambda: managed.proc.poll() is not None)
    assert managed._group_gone()
    state.sync_processes(manager.sync_processes(state.task_id))
    assert state.snapshot()["processes"][0]["status"] == "running"
    report = manager.cleanup(state.task_id)
    state.record_process_cleanup(report)
    assert not report.complete
    assert state.task_id == "task-1"
    assert state.snapshot()["processes"][0]["status"] == "running"
    assert _eventually(lambda: manager.sync_processes(state.task_id)[0].status == "exited")
    state.sync_processes(manager.sync_processes(state.task_id))
    state.sync_processes(manager.sync_processes(state.task_id))
    snapshot = state.snapshot()
    assert [item["kind"] for item in snapshot["process_events"]].count("exited") == 1
    assert snapshot["current_generation_id"] == 2
    _finish_manager(state, manager)


def test_cleanup_cannot_succeed_when_wait_does_not_confirm_exit():
    state = AgentState()
    state.begin_task("unconfirmed wait")
    manager = ProcessManager(grace_seconds=0.02)
    _, executor = _runtime(state, manager)
    result = _start(state, executor, _python("import time; time.sleep(2)"))
    process_id = json.loads(result.output)["process_id"]
    with patch.object(_ManagedProcess, "_wait", return_value=False):
        report = manager.cleanup(state.task_id)
    state.record_process_cleanup(report)
    assert not report.complete
    assert process_id in manager._processes
    assert state.task_id == "task-1"
    assert "未确认结束" in report.render()
    _finish_manager(state, manager)


def test_windows_direct_child_and_pipes_do_not_claim_full_boundary_cleanup():
    state = AgentState()
    state.begin_task("windows cleanup")
    manager = ProcessManager(grace_seconds=0.1)
    _, executor = _runtime(state, manager)
    _start(state, executor, _python("raise SystemExit(0)"))
    assert _eventually(lambda: manager.sync_processes(state.task_id)[0].stdout_offset == 0
                       and next(iter(manager._processes.values())).proc.poll() is not None)
    with patch("mini_agent.processes.os.name", "nt"):
        report = manager.cleanup(state.task_id)
    assert not report.complete
    assert "无法确认任意 shell 派生进程树" in report.items[0].reason
    assert "清理不完整" in report.render()
    assert next(iter(manager._processes.values())).closed
    state.record_process_cleanup(report)
    assert state.task_id == "task-1"
    assert manager.cleanup(state.task_id).complete


def test_cleanup_never_signals_a_previously_confirmed_process_group():
    if os.name != "posix":
        return
    state = AgentState()
    state.begin_task("completed process")
    manager = ProcessManager(grace_seconds=0.1)
    _, executor = _runtime(state, manager)
    result = _start(state, executor, _python("raise SystemExit(0)"))
    process_id = json.loads(result.output)["process_id"]
    managed = manager._processes[process_id]
    assert _eventually(lambda: managed.refresh().status == "exited")
    with patch.object(managed, "_signal_group", wraps=managed._signal_group) as signal_group:
        report = manager.cleanup(state.task_id)
    assert report.complete
    signal_group.assert_not_called()


def test_windows_unconfirmed_direct_child_retains_registration():
    state = AgentState()
    state.begin_task("windows incomplete")
    manager = ProcessManager(grace_seconds=0.02)
    _, executor = _runtime(state, manager)
    result = _start(state, executor, _python("import time; time.sleep(2)"))
    process_id = json.loads(result.output)["process_id"]
    with patch("mini_agent.processes.os.name", "nt"), patch.object(
        _ManagedProcess, "_wait", return_value=False,
    ), patch.object(_ManagedProcess, "_signal_group", return_value=(True, "已发送信号")):
        report = manager.cleanup(state.task_id)
    assert not report.complete
    assert process_id in manager._processes
    _finish_manager(state, manager)


def test_second_collector_start_failure_retains_unconfirmed_process():
    state = AgentState()
    state.begin_task("collector failure")
    manager = ProcessManager(grace_seconds=0.02)
    _, executor = _runtime(state, manager)
    original_start = Thread.start

    def fail_stderr(thread):
        if thread.name.endswith("-stderr"):
            raise RuntimeError("thread start failed")
        return original_start(thread)

    with patch.object(Thread, "start", fail_stderr), patch.object(
        _ManagedProcess, "_wait", return_value=False,
    ):
        result = executor.execute_result(
            "start_process", {"command": _python("import time; time.sleep(2)")}, state,
        )
    assert result.outcome == "failed"
    assert "thread start failed" in result.output
    assert "process_id=proc-1" in result.output
    state.record_execution_result(result)
    assert state.snapshot()["processes"] == []
    assert "proc-1" in manager._processes
    assert not manager.cleanup(state.task_id).incomplete
