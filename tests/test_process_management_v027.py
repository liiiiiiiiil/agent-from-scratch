"""v0.27 process observation, asynchronous failure, and wait handoff."""
from __future__ import annotations

import json
from copy import deepcopy
import shlex
import sys
import time
from unittest.mock import patch

from mini_agent.agent import agent_loop
from mini_agent.context import ContextManager
from mini_agent.permission import ALLOW, PermissionGate, PermissionPolicy
from mini_agent.processes import ProcessManager, _ByteRing
from mini_agent.state import AgentState
from mini_agent.tools import create_registry
from mini_agent.tools.base import ToolExecutor
from mini_agent.trace import build_trace


def _python(code):
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def _setup(max_stream_bytes=64 * 1024):
    state = AgentState()
    state.begin_task("observe")
    manager = ProcessManager(grace_seconds=0.1, max_stream_bytes=max_stream_bytes)
    registry = create_registry(state, process_manager=manager)
    rules = {name: ALLOW for name in (
        "start_process", "get_process", "read_process", "list_processes", "wait_process",
    )}
    executor = ToolExecutor(registry, PermissionGate(PermissionPolicy(rules)))
    return state, manager, registry, executor


def _start(state, executor, command):
    result = executor.execute_result("start_process", {"command": command}, state)
    assert result.outcome == "succeeded", result.output
    state.record_execution_result(result)
    return json.loads(result.output)["process_id"]


def _call(state, executor, name, **arguments):
    result = executor.execute_result(name, arguments, state)
    assert result.outcome == "succeeded", result.output
    state.record_execution_result(result)
    return json.loads(result.output)


def _cleanup(state, manager):
    report = manager.cleanup(state.task_id)
    assert report.complete, report.render()
    state.record_process_cleanup(report)


def test_incremental_utf8_invalid_bytes_and_two_stream_cursors():
    state, manager, _, executor = _setup()
    pid = _start(state, executor, _python(
        "import sys,time; "
        "sys.stdout.buffer.write('界'.encode()[:2]); sys.stdout.flush(); "
        "time.sleep(.2); sys.stdout.buffer.write('界'.encode()[2:]+b'!'); "
        "sys.stdout.flush(); sys.stderr.buffer.write(b'\\xffE'); "
        "sys.stderr.flush(); time.sleep(.3)"
    ))
    try:
        time.sleep(.1)
        first = _call(state, executor, "read_process", process_id=pid)
        assert first["stdout"] == ""
        assert first["next_stdout_offset"] == 0
        assert _call(state, executor, "wait_process", process_id=pid,
                     timeout_ms=0)["reason"] == "still_running"
        time.sleep(.2)
        second = _call(state, executor, "read_process", process_id=pid)
        assert second["stdout"] == "界!"
        assert second["stderr"] == "\ufffdE"
        assert second["next_stdout_offset"] == 4
        third = _call(state, executor, "read_process", process_id=pid)
        assert third["stdout"] == third["stderr"] == ""
        assert third["next_stdout_offset"] == 4
        assert "界!" not in json.dumps(state.snapshot()["processes"], ensure_ascii=False)
    finally:
        _cleanup(state, manager)


def test_byte_ring_gap_and_result_json_remain_bounded():
    ring = _ByteRing(8)
    ring.append(b"0123456789ABCDEF")
    text, offset, gap, lost, _ = ProcessManager._stream_fragment(ring, 0, 4, 100, True)
    assert (text, offset, gap, lost) == ("89AB", 12, True, 8)
    state, manager, _, executor = _setup()
    pid = _start(state, executor, _python(
        "import sys,time; sys.stdout.write('\\n'*20000); sys.stdout.flush(); time.sleep(.2)"
    ))
    try:
        time.sleep(.15)
        result = executor.execute_result("read_process", {"process_id": pid, "max_chars": 4000}, state)
        assert result.outcome == "succeeded"
        assert len(result.tool_content()) <= 8000
        assert json.loads(result.tool_content())["stdout_output_gap"] is False
    finally:
        _cleanup(state, manager)


def test_buffer_eviction_reports_stream_gap_and_tail_after_exit():
    state, manager, _, executor = _setup(max_stream_bytes=8)
    pid = _start(state, executor, _python(
        "import sys; sys.stdout.write('0123456789ABCDEF'); sys.stdout.flush()"
    ))
    try:
        time.sleep(.1)
        result = _call(state, executor, "read_process", process_id=pid)
        assert result["status"] == "exited"
        assert result["stdout"] == "89ABCDEF"
        assert result["stdout_output_gap"] is True
        assert result["stdout_lost_bytes"] == 8
        assert result["next_stdout_offset"] == 16
        again = _call(state, executor, "read_process", process_id=pid)
        assert again["stdout"] == ""
        assert again["stdout_output_gap"] is False
    finally:
        _cleanup(state, manager)


def test_wait_reports_unread_output_without_consuming_it():
    state, manager, _, executor = _setup()
    pid = _start(state, executor, _python(
        "import sys,time; sys.stdout.write('ready'); sys.stdout.flush(); time.sleep(.3)"
    ))
    try:
        time.sleep(.1)
        waited = _call(state, executor, "wait_process", process_id=pid, timeout_ms=0)
        assert waited["reason"] == "output_available"
        assert _call(state, executor, "read_process", process_id=pid)["stdout"] == "ready"
    finally:
        _cleanup(state, manager)


def test_get_list_wait_and_task_ownership():
    state, manager, _, executor = _setup()
    pid = _start(state, executor, _python("import time; time.sleep(.4)"))
    try:
        listed = _call(state, executor, "list_processes")
        assert [item["process_id"] for item in listed["processes"]] == [pid]
        assert "stdout" not in listed["processes"][0]
        current = _call(state, executor, "get_process", process_id=pid)
        assert current["status"] == "running"
        waited = _call(state, executor, "wait_process", process_id=pid, timeout_ms=0)
        assert waited["reason"] == "still_running"
        unknown = executor.execute_result("get_process", {"process_id": "proc-999"}, state)
        assert unknown.outcome == "invalid"
        assert json.loads(unknown.tool_content())["error_kind"] == "unknown_process_id"
        assert manager.get_owned("another-task", pid) is None
        invalid = executor.execute_result("wait_process", {"process_id": pid, "timeout_ms": 30001}, state)
        assert invalid.outcome == "invalid"
    finally:
        _cleanup(state, manager)


def test_nonzero_exit_creates_one_causal_failure_and_trace_is_read_only():
    state, manager, _, executor = _setup()
    pid = _start(state, executor, _python("raise SystemExit(7)"))
    try:
        time.sleep(.15)
        state.sync_processes(manager.sync_processes(state.task_id))
        state.sync_processes(manager.sync_processes(state.task_id))
        snapshot = state.snapshot()
        assert snapshot["processes"][0]["exit_code"] == 7
        assert len(snapshot["failures"]) == 1
        failure = snapshot["failures"][0]
        assert failure["caused_by_process_event_id"] == snapshot["process_events"][-1]["event_id"]
        assert failure["generation_id"] == 1
        assert snapshot["current_generation_id"] == 2
        assert state.active_failure_id == failure["failure_id"]
        before = state.snapshot()
        trace = build_trace(before)
        assert trace["integrity"]["status"] == "complete", trace["integrity"]
        assert state.snapshot() == before
    finally:
        _cleanup(state, manager)


def test_wait_timeout_hands_off_without_another_llm_round():
    state, manager, registry, executor = _setup()
    pid = _start(state, executor, _python("import time; time.sleep(.5)"))
    context = ContextManager(state, [{"role": "user", "content": "observe"}], observability=False)
    call = {"id": "wait", "type": "function", "function": {
        "name": "wait_process", "arguments": json.dumps({"process_id": pid, "timeout_ms": 10}),
    }}
    try:
        with patch("mini_agent.agent.call_llm", return_value={
            "role": "assistant", "content": None, "tool_calls": [call],
        }) as llm:
            agent_loop(context, executor)
        assert llm.call_count == 1
        assert state.status == "awaiting_process"
        assert json.loads(context.history[-1]["content"])["reason"] == "still_running"
    finally:
        _cleanup(state, manager)


def test_same_round_reads_follow_call_order_and_preserve_tool_protocol():
    state, manager, _, executor = _setup()
    pid = _start(state, executor, _python(
        "import sys,time; sys.stdout.write('once'); sys.stdout.flush(); time.sleep(.5)"
    ))
    time.sleep(.1)
    context = ContextManager(state, [{"role": "user", "content": "observe"}], observability=False)
    calls = [{"id": f"read-{i}", "type": "function", "function": {
        "name": "read_process", "arguments": json.dumps({"process_id": pid}),
    }} for i in range(2)]
    replies = iter([
        {"role": "assistant", "content": None, "tool_calls": calls},
        {"role": "assistant", "content": "等待"},
    ])
    try:
        with patch("mini_agent.agent.call_llm", side_effect=lambda *a, **k: next(replies)):
            agent_loop(context, executor)
        results = [item for item in context.history if item["role"] == "tool"]
        assert [item["tool_call_id"] for item in results] == ["read-0", "read-1"]
        assert json.loads(results[0]["content"])["stdout"] == "once"
        assert json.loads(results[1]["content"])["stdout"] == ""
    finally:
        _cleanup(state, manager)


def test_wait_requires_sole_call_and_exit_trace_detects_broken_cause():
    state, manager, _, executor = _setup()
    pid = _start(state, executor, _python("raise SystemExit(2)"))
    context = ContextManager(state, [{"role": "user", "content": "observe"}], observability=False)
    calls = [{"id": str(i), "type": "function", "function": {
        "name": name, "arguments": json.dumps({"process_id": pid}),
    }} for i, name in enumerate(("wait_process", "get_process"))]
    replies = iter([
        {"role": "assistant", "content": None, "tool_calls": calls},
        {"role": "assistant", "content": "等待"},
    ])
    try:
        with patch("mini_agent.agent.call_llm", side_effect=lambda *a, **k: next(replies)):
            agent_loop(context, executor)
        results = [item for item in context.history if item["role"] == "tool"]
        assert len(results) == 2
        assert all(json.loads(item["content"])["error_kind"] == "wait_batch_gate"
                   for item in results)
        time.sleep(.1)
        state.sync_processes(manager.sync_processes(state.task_id))
        snapshot = deepcopy(state.snapshot())
        snapshot["failures"][0]["caused_by_process_event_id"] = "pe-missing"
        assert build_trace(snapshot)["integrity"]["status"] == "incomplete"
    finally:
        _cleanup(state, manager)


def test_exit_conflict_blocks_before_another_llm_call():
    state, manager, _, executor = _setup()
    _start(state, executor, _python("import time; time.sleep(.1); raise SystemExit(2)"))
    invalid = executor.execute_result("get_process", {"process_id": "proc-missing"}, state)
    state.record_execution_result(invalid)
    assert state.active_failure_id is not None
    time.sleep(.2)
    context = ContextManager(state, [{"role": "user", "content": "observe"}], observability=False)
    try:
        with patch("mini_agent.agent.call_llm") as llm:
            agent_loop(context, executor)
        llm.assert_not_called()
        assert state.status == "blocked"
        assert len(state.snapshot()["failures"]) == 2
    finally:
        _cleanup(state, manager)
