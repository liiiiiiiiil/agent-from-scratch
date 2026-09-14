"""v0.29 bounded pipe stdin and redaction boundaries."""
from __future__ import annotations

from io import StringIO
import json
import shlex
import sys
import time
from unittest.mock import patch

import pytest

from mini_agent.output import TerminalOutput
from mini_agent.permission import ALLOW, DENY, PermissionGate, PermissionPolicy
from mini_agent.processes import MAX_STDIN_BYTES, ProcessManager, _ManagedProcess
from mini_agent.state import AgentState
from mini_agent.tools import create_registry
from mini_agent.tools.base import ToolExecutor
from mini_agent.trace import build_trace


def _python(code: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def _runtime(rules=None, manager=None):
    state = AgentState()
    state.begin_task("stdin")
    manager = manager or ProcessManager(grace_seconds=.1)
    registry = create_registry(state, process_manager=manager)
    default_rules = {
        "start_process": {"*": ALLOW},
        "get_process": ALLOW,
        "read_process": ALLOW,
        "wait_process": ALLOW,
        "write_process": ALLOW,
        "run_shell": {"true": ALLOW},
    }
    if rules:
        default_rules.update(rules)
    executor = ToolExecutor(registry, PermissionGate(PermissionPolicy(default_rules)))
    return state, manager, executor


def _start(state, executor, command, stdin_mode="closed"):
    result = executor.execute_result(
        "start_process", {"command": command, "stdin_mode": stdin_mode}, state,
    )
    assert result.outcome == "succeeded", result.output
    state.record_execution_result(result)
    return json.loads(result.output)["process_id"]


def _call(state, executor, name, **arguments):
    result = executor.execute_result(name, arguments, state)
    state.record_execution_result(result)
    return result, json.loads(result.output) if isinstance(result.output, str) else result.output


def _cleanup(state, manager):
    report = manager.cleanup(state.task_id)
    state.record_process_cleanup(report)
    assert report.complete, report.render()


def test_default_stdin_is_eof_and_pipe_accepts_text_then_eof():
    state, manager, executor = _runtime()
    eof_pid = _start(state, executor, _python(
        "import sys; print('eof' if sys.stdin.read() == '' else 'data', flush=True)"
    ))
    pipe_pid = _start(state, executor, _python(
        "import sys; print('reply:' + sys.stdin.readline().strip(), flush=True)"
    ), "pipe")
    try:
        deadline = time.monotonic() + 2
        while manager.get_owned(state.task_id, eof_pid).refresh().status == "running":
            if time.monotonic() >= deadline:
                pytest.fail("进程没有在测试期限内结束")
            time.sleep(.01)
        state.sync_processes(manager.sync_processes(state.task_id))
        _, written = _call(state, executor, "write_process", process_id=pipe_pid,
                           input="hello\n", close_stdin=True)
        assert written["status"] in {"closed", "error"}
        assert written["status"] == "closed"
        deadline = time.monotonic() + 2
        while manager.get_owned(state.task_id, pipe_pid).refresh().status == "running":
            if time.monotonic() >= deadline:
                pytest.fail("pipe 进程没有在 EOF 后结束")
            time.sleep(.01)
        _, eof_output = _call(state, executor, "read_process", process_id=eof_pid)
        _, pipe_output = _call(state, executor, "read_process", process_id=pipe_pid)
        assert eof_output["stdout"] == "eof\n"
        assert pipe_output["stdout"] == "reply:hello\n"
        record = next(item for item in state.snapshot()["processes"] if item["process_id"] == pipe_pid)
        assert record["stdin_mode"] == "pipe"
        assert record["stdin_state"] == "closed"
    finally:
        _cleanup(state, manager)


def test_empty_input_sends_eof_once_and_closed_stdin_is_explicit():
    state, manager, executor = _runtime()
    pid = _start(state, executor, _python(
        "import sys,time; sys.stdin.read(); print('eof', flush=True); time.sleep(.3)"
    ), "pipe")
    try:
        first, payload = _call(state, executor, "write_process", process_id=pid,
                               input="", close_stdin=True)
        assert first.outcome == "succeeded"
        assert payload["status"] == "closed" and payload["written_bytes"] == 0
        second = executor.execute_result("write_process", {
            "process_id": pid, "input": "", "close_stdin": True,
        }, state)
        payload = json.loads(second.output)
        assert second.outcome == "invalid"
        assert payload["error_kind"] in {"stdin_closed", "process_exited"}
        third = executor.execute_result("write_process", {
            "process_id": pid, "input": "again", "close_stdin": False,
        }, state)
        payload = json.loads(third.output)
        assert third.outcome == "invalid"
        assert payload["error_kind"] in {"stdin_closed", "process_exited"}
    finally:
        _cleanup(state, manager)


def test_utf8_byte_limit_and_empty_input_validation_do_not_reserve_generation():
    state, manager, executor = _runtime()
    pid = _start(state, executor, _python("import time; time.sleep(2)"), "pipe")
    try:
        before = state.snapshot()["current_generation_id"]
        too_large = "界" * 1366  # 4098 UTF-8 bytes
        result = executor.execute_result(
            "write_process", {"process_id": pid, "input": too_large}, state,
        )
        assert result.outcome == "invalid"
        assert result.error_kind == "invalid_arguments"
        assert result.reservation is None
        assert state.snapshot()["current_generation_id"] == before

        exact = "界" * 1365 + "a"  # exactly 4096 UTF-8 bytes
        result = executor.execute_result(
            "write_process", {"process_id": pid, "input": exact, "close_stdin": True}, state,
        )
        assert result.outcome == "succeeded"
        assert json.loads(result.output)["written_bytes"] == MAX_STDIN_BYTES
    finally:
        _cleanup(state, manager)


def test_unknown_cross_task_permission_and_exploring_reject_before_write():
    manager = ProcessManager(grace_seconds=.1)
    state, manager, executor = _runtime(manager=manager)
    pid = _start(state, executor, _python("import time; time.sleep(2)"), "pipe")
    try:
        other = AgentState()
        other.begin_task("other")
        other.task_id = "task-other"
        other_registry = create_registry(other, process_manager=manager)
        other_executor = ToolExecutor(
            other_registry, PermissionGate(PermissionPolicy({"write_process": ALLOW})),
        )
        before = other.snapshot()["current_generation_id"]
        cross = other_executor.execute_result(
            "write_process", {"process_id": pid, "input": "SECRET"}, other,
        )
        assert cross.error_kind == "unknown_process_id"
        assert cross.permission == "not_checked" and cross.reservation is None
        assert other.snapshot()["current_generation_id"] == before

        state2, manager2, denied_executor = _runtime(
            rules={"write_process": DENY}, manager=ProcessManager(grace_seconds=.1),
        )
        denied_pid = _start(state2, denied_executor, _python("import time; time.sleep(2)"), "pipe")
        try:
            before = state2.snapshot()["current_generation_id"]
            denied = denied_executor.execute_result(
                "write_process", {"process_id": denied_pid, "input": "SECRET"}, state2,
            )
            assert denied.outcome == "denied" and denied.reservation is None
            assert state2.snapshot()["current_generation_id"] == before
            assert manager2._processes[denied_pid].stdin_state == "open"
        finally:
            _cleanup(state2, manager2)

        state.begin_plan()
        before = state.snapshot()["current_generation_id"]
        exploring = executor.execute_result(
            "write_process", {"process_id": pid, "input": "SECRET"}, state,
        )
        assert exploring.error_kind == "planning_phase_gate"
        assert exploring.reservation is None
        assert state.snapshot()["current_generation_id"] == before
        assert manager._processes[pid].stdin_state == "open"
    finally:
        _cleanup(state, manager)


def test_permission_prompt_and_debug_output_never_show_stdin_body():
    state, manager, executor = _runtime(rules={"write_process": "ask"})
    pid = _start(state, executor, _python("import time; time.sleep(2)"), "pipe")
    marker = "UNIQUE-STDIN-MARKER"
    prompts = []

    def reject(prompt):
        prompts.append(prompt)
        return "reject"

    try:
        with patch("builtins.input", side_effect=reject):
            result = executor.execute_result(
                "write_process", {"process_id": pid, "input": marker,
                                   "close_stdin": True}, state,
            )
        assert result.outcome == "denied"
        assert prompts and marker not in prompts[0]
        assert "bytes=" in prompts[0] and "close_stdin=True" in prompts[0]

        stream = StringIO()
        terminal = TerminalOutput("debug", stream=stream)
        terminal.tools_start([{"function": {"name": "write_process", "arguments": json.dumps({
            "process_id": pid, "input": marker, "close_stdin": True,
        })}}])
        terminal.close()
        assert marker not in stream.getvalue()
        assert "input_bytes" in stream.getvalue()
    finally:
        _cleanup(state, manager)


def test_write_pending_is_bounded_and_cannot_be_repeated():
    state, manager, executor = _runtime()
    pid = _start(state, executor, _python("import time; time.sleep(5)"), "pipe")
    original = _ManagedProcess._write_stdin_worker

    def slow_worker(managed, data, close_stdin, done):
        time.sleep(2.2)
        original(managed, data, close_stdin, done)

    try:
        with patch.object(_ManagedProcess, "_write_stdin_worker", slow_worker):
            started = time.monotonic()
            first, first_payload = _call(
                state, executor, "write_process", process_id=pid, input="one",
            )
            elapsed = time.monotonic() - started
            assert elapsed < 2.5
            assert first.outcome == "succeeded"
            assert first_payload["status"] == "write_pending"
            assert first_payload["written_bytes"] == 0
            second, second_payload = _call(
                state, executor, "write_process", process_id=pid, input="two",
            )
            assert second.outcome == "succeeded"
            assert second_payload["status"] == "write_pending"
            assert second_payload["written_bytes"] == 0
        deadline = time.monotonic() + 4
        observed = {}
        while time.monotonic() < deadline:
            state.sync_processes(manager.sync_processes(state.task_id))
            observed = next(item for item in state.snapshot()["processes"]
                            if item["process_id"] == pid)
            if not observed["write_pending"]:
                break
            time.sleep(.05)
        assert observed["write_pending"] is False
        assert observed["stdin_state"] == "open"
        _, closed = _call(state, executor, "write_process", process_id=pid,
                          input="", close_stdin=True)
        assert closed["status"] == "closed"
        assert not state.completion_reminder() is None
    finally:
        _cleanup(state, manager)


def test_write_attempt_is_redacted_and_exit_requires_fresh_verification():
    state, manager, executor = _runtime()
    pid = _start(state, executor, _python(
        "import sys; sys.stdin.readline(); print('done', flush=True)"
    ), "pipe")
    marker = "UNIQUE-TRACE-STDIN"
    try:
        verification = executor.execute_result(
            "run_shell", {"command": "true", "purpose": "verification"}, state,
        )
        state.record_execution_result(verification)
        assert state.has_verification_evidence()
        write, payload = _call(state, executor, "write_process", process_id=pid,
                               input=marker, close_stdin=True)
        assert write.reservation is not None and payload["status"] == "closed"
        deadline = time.monotonic() + 2
        while manager.get_owned(state.task_id, pid).refresh().status == "running":
            if time.monotonic() >= deadline:
                pytest.fail("进程没有在测试期限内结束")
            time.sleep(.01)
        state.sync_processes(manager.sync_processes(state.task_id))
        snapshot = state.snapshot()
        assert not state.has_verification_evidence()
        assert marker not in json.dumps(snapshot, ensure_ascii=False)
        assert marker not in json.dumps(state._original_attempt_arguments, ensure_ascii=False)
        trace = build_trace(snapshot)
        assert trace["integrity"]["status"] == "complete"
        assert marker not in json.dumps(trace, ensure_ascii=False)
        write_attempt = next(item for item in snapshot["attempts"] if item["tool"] == "write_process")
        assert write_attempt["redacted_arguments"]["input"].startswith("<str:")

        fresh = executor.execute_result(
            "run_shell", {"command": "true", "purpose": "verification"}, state,
        )
        state.record_execution_result(fresh)
        assert state.has_verification_evidence()
        assert build_trace(state.snapshot())["integrity"]["status"] == "complete"
    finally:
        _cleanup(state, manager)


def test_write_process_cannot_be_a_recovery_retry_or_adjust_target():
    state = AgentState()
    state.begin_task("recovery")
    state._enter_diagnosis("f-1")
    # The recovery validator checks the target tool before any handler call;
    # use a directly constructed failure because this test targets that rule.
    from mini_agent.state import FailureEvent
    state.failures.append(FailureEvent("f-1", 0, "execute", "deterministic", True, "a-1"))
    state.attempts.append(type("Attempt", (), {
        "attempt_id": "a-1", "failure_id": "f-1", "tool": "write_process",
    })())
    state._active_failure_id = "f-1"
    target, detail = state.recovery_target("retry", "f-1", requested_attempt="a-1")
    assert target is None and "不能作为恢复目标" in detail


def test_exit_between_write_preflight_and_handler_closes_stdin_on_cleanup():
    state, manager, executor = _runtime()
    pid = _start(state, executor, _python("import time; time.sleep(.2)"), "pipe")
    managed = manager.get_owned(state.task_id, pid)
    original_preflight = manager.write_preflight

    def exit_after_preflight(task_id, process_id):
        result = original_preflight(task_id, process_id)
        assert result is None
        managed.proc.wait(timeout=2)
        return result

    try:
        with patch.object(manager, "write_preflight", side_effect=exit_after_preflight):
            result = executor.execute_result(
                "write_process", {"process_id": pid, "input": "late"}, state,
            )
        assert result.error_kind == "process_exited"
        assert managed.proc.stdin.closed
    finally:
        report = manager.cleanup(state.task_id)
        assert report.complete, report.render()
        assert managed.proc.stdin.closed


def test_rejected_unknown_write_id_keeps_trace_complete():
    state, manager, executor = _runtime()
    result = executor.execute_result(
        "write_process", {"process_id": "proc-404", "input": "unused"}, state,
    )
    assert result.error_kind == "unknown_process_id"
    state.record_execution_result(result)
    assert build_trace(state.snapshot())["integrity"]["status"] == "complete"
    assert manager.cleanup(state.task_id).complete


def test_broken_pipe_is_execution_failure_without_confirmed_written_bytes():
    state, manager, executor = _runtime()
    pid = _start(state, executor, _python(
        "import os,time; os.close(0); print('ready', flush=True); time.sleep(2)"
    ), "pipe")
    try:
        deadline = time.monotonic() + 2
        while True:
            observed = manager.read_process(state.task_id, pid)
            if "ready" in observed["stdout"]:
                break
            if time.monotonic() >= deadline:
                pytest.fail("子进程没有关闭 stdin 并报告 ready")
            time.sleep(.01)
        result, payload = _call(
            state, executor, "write_process", process_id=pid, input="x" * MAX_STDIN_BYTES,
        )
        assert result.outcome == "failed" and result.error_kind == "broken_pipe"
        assert payload["written_bytes"] == 0
        assert payload["delivery_uncertain"] is True
        assert state.snapshot()["failures"][-1]["category"] == "deterministic"
        assert build_trace(state.snapshot())["integrity"]["status"] == "complete"
    finally:
        _cleanup(state, manager)
