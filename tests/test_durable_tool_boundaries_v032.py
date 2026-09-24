"""v0.32 durable tool-boundary protocol tests."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import sys
import threading
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mini_agent.agent import agent_loop
from mini_agent.context import ContextManager
from mini_agent.session import (
    DurableToolBoundary, SessionError, SessionStore, SessionValidationError,
)
from mini_agent.permission import PermissionGate, PermissionPolicy
from mini_agent.state import AgentState, PlanRejected
from mini_agent.tools.base import Tool, ToolExecutor, ToolRegistry


def _runtime(tmp_path: Path, *, handler=None, gate=None):
    state = AgentState()
    state.begin_task("durable boundary")
    context = ContextManager(state, [{"role": "user", "content": "use the tool"}], observability=False)
    store = SessionStore(tmp_path / "sessions")
    envelope = store.save(None, state, context, workspace_root=tmp_path)
    registry = ToolRegistry()
    registry.register(Tool(
        "read_value", "read a value", {
            "type": "object", "properties": {"value": {"type": "integer"}},
            "required": ["value"], "additionalProperties": False,
        }, handler or (lambda value: f"value={value}"), effect_class="none",
    ))
    executor = ToolExecutor(
        registry, gate=gate or PermissionGate(PermissionPolicy({"read_value": "allow"})),
    )
    executor.session_boundary = DurableToolBoundary(
        store, envelope["session_id"], tmp_path,
    )
    return state, context, store, envelope, executor


def _tool_message(call_id="call-1", value=7):
    return {
        "role": "assistant", "content": None, "tool_calls": [{
            "id": call_id, "type": "function", "function": {
                "name": "read_value", "arguments": json.dumps({"value": value}),
            },
        }],
    }


def test_round_writes_pending_admission_result_and_complete_commits(tmp_path: Path):
    state, context, store, envelope, executor = _runtime(tmp_path)
    responses = [_tool_message(), {"role": "assistant", "content": "done"}]
    with patch("mini_agent.agent.call_llm", side_effect=lambda *args, **kwargs: responses.pop(0)):
        assert agent_loop(context, executor) == "done"

    saved = store.load(envelope["session_id"])
    assert saved["schema_version"] == 4
    boundary = saved["tool_boundary"]
    assert boundary["status"] == "committed"
    assert boundary["calls"][0]["status"] == "committed"
    assert boundary["calls"][0]["handler_admitted"] is True
    assert boundary["calls"][0]["attempt_id"] == "a-1"
    assert [item["role"] for item in saved["context"]["history"]][-2:] == ["assistant", "tool"]


def test_multiple_calls_commit_in_model_order_with_pending_suffix(tmp_path: Path):
    seen = []
    state, context, store, envelope, executor = _runtime(
        tmp_path, handler=lambda value: seen.append(value) or f"value={value}",
    )
    assistant = {
        "role": "assistant", "content": None, "tool_calls": [
            {"id": "call-1", "type": "function", "function": {
                "name": "read_value", "arguments": json.dumps({"value": 1}),
            }},
            {"id": "call-2", "type": "function", "function": {
                "name": "read_value", "arguments": json.dumps({"value": 2}),
            }},
        ],
    }
    responses = [assistant, {"role": "assistant", "content": "done"}]
    with patch("mini_agent.agent.call_llm", side_effect=lambda *args, **kwargs: responses.pop(0)):
        assert agent_loop(context, executor) == "done"

    saved = store.load(envelope["session_id"])
    calls = saved["tool_boundary"]["calls"]
    assert seen == [1, 2]
    assert [call["tool_call_id"] for call in calls] == ["call-1", "call-2"]
    assert all(call["status"] == "committed" for call in calls)
    assert [item["tool_call_id"] for item in saved["context"]["history"][-2:]] == [
        "call-1", "call-2",
    ]


def test_admission_commit_failure_does_not_enter_handler(tmp_path: Path):
    entered = []

    def handler(value):
        entered.append(value)
        return "should not run"

    state, context, store, envelope, executor = _runtime(tmp_path, handler=handler)
    responses = [_tool_message()]
    real = executor.session_boundary.record_admission

    def fail(*args, **kwargs):
        raise SessionError("injected admission commit failure")

    executor.session_boundary.record_admission = fail
    with patch("mini_agent.agent.call_llm", side_effect=lambda *args, **kwargs: responses.pop(0)):
        with pytest.raises(SessionError, match="injected admission"):
            agent_loop(context, executor)
    assert entered == []
    assert store.load(envelope["session_id"])["tool_boundary"]["calls"][0]["status"] == "pending"
    executor.session_boundary.record_admission = real


def test_boundary_rejects_missing_result_and_damaged_attempt_reference(tmp_path: Path):
    state, context, store, envelope, executor = _runtime(tmp_path)
    responses = [_tool_message(), {"role": "assistant", "content": "done"}]
    with patch("mini_agent.agent.call_llm", side_effect=lambda *args, **kwargs: responses.pop(0)):
        agent_loop(context, executor)
    path = store.path_for(envelope["session_id"])
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["tool_boundary"]["calls"][0]["attempt_id"] = "a-999"
    without_integrity = {key: value for key, value in raw.items() if key != "integrity"}
    raw["integrity"] = {"algorithm": "sha256", "sha256": hashlib.sha256(
        json.dumps(without_integrity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()}
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SessionValidationError, match="attempt"):
        store.load(envelope["session_id"])


def test_boundary_rejects_context_result_without_committed_boundary_call(tmp_path: Path):
    state, context, store, envelope, executor = _runtime(tmp_path)
    responses = [_tool_message(), {"role": "assistant", "content": "done"}]
    with patch("mini_agent.agent.call_llm", side_effect=lambda *args, **kwargs: responses.pop(0)):
        agent_loop(context, executor)
    path = store.path_for(envelope["session_id"])
    raw = json.loads(path.read_text(encoding="utf-8"))
    call = raw["tool_boundary"]["calls"][0]
    call.update({
        "permission": "not_checked",
        "handler_admitted": False,
        "attempt_id": None,
        "pre_generation_id": None,
        "generation_id": None,
        "status": "pending",
        "result": None,
    })
    without_integrity = {key: value for key, value in raw.items() if key != "integrity"}
    raw["integrity"] = {"algorithm": "sha256", "sha256": hashlib.sha256(
        json.dumps(without_integrity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()}
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SessionValidationError, match="pending"):
        store.load(envelope["session_id"])


def test_permission_and_argument_rejections_are_committed_without_handler_entry(tmp_path: Path):
    entered = []

    def handler(value):
        entered.append(value)
        return "unexpected"

    state, context, store, envelope, executor = _runtime(
        tmp_path, handler=handler,
        gate=PermissionGate(PermissionPolicy({"read_value": "deny"})),
    )
    responses = [_tool_message(), {"role": "assistant", "content": "done"}]
    with patch("mini_agent.agent.call_llm", side_effect=lambda *args, **kwargs: responses.pop(0)):
        agent_loop(context, executor)
    saved = store.load(envelope["session_id"])
    call = saved["tool_boundary"]["calls"][0]
    assert entered == []
    assert call["permission"] == "denied"
    assert call["handler_admitted"] is False
    assert saved["state"]["attempts"][0]["outcome"] == "denied"


def test_parameter_error_is_a_durable_result_without_handler_admission(tmp_path: Path):
    entered = []
    state, context, store, envelope, executor = _runtime(
        tmp_path, handler=lambda value: entered.append(value),
    )
    responses = [{
        "role": "assistant", "content": None, "tool_calls": [{
            "id": "call-invalid", "type": "function", "function": {
                "name": "read_value", "arguments": json.dumps({"value": "wrong"}),
            },
        }],
    }, {"role": "assistant", "content": "done"}, {"role": "assistant", "content": "done"}]
    with patch("mini_agent.agent.call_llm", side_effect=lambda *args, **kwargs: responses.pop(0)):
        agent_loop(context, executor)
    call = store.load(envelope["session_id"])["tool_boundary"]["calls"][0]
    assert entered == []
    assert call["handler_admitted"] is False
    assert call["status"] == "committed"
    assert call["result"]["outcome"] == "invalid"


def test_plan_handler_rejection_keeps_admission_without_attempt(tmp_path: Path):
    entered = []
    state = AgentState()
    state.begin_task("plan rejection")
    context = ContextManager(state, [{"role": "user", "content": "plan"}], observability=False)
    store = SessionStore(tmp_path / "sessions")
    envelope = store.save(None, state, context, workspace_root=tmp_path)
    registry = ToolRegistry()

    def reject_plan():
        entered.append(True)
        raise PlanRejected("invalid plan")

    registry.register(Tool(
        "commit_plan", "plan", {"type": "object", "properties": {}},
        reject_plan,
    ))
    executor = ToolExecutor(
        registry, gate=PermissionGate(PermissionPolicy({"commit_plan": "allow"})),
    )
    executor.session_boundary = DurableToolBoundary(store, envelope["session_id"], tmp_path)
    responses = [{
        "role": "assistant", "content": None, "tool_calls": [{
            "id": "plan-1", "type": "function", "function": {
                "name": "commit_plan", "arguments": "{}",
            },
        }],
    }, {"role": "assistant", "content": "done"}]
    with patch("mini_agent.agent.call_llm", side_effect=lambda *args, **kwargs: responses.pop(0)):
        assert agent_loop(context, executor) == "done"

    call = store.load(envelope["session_id"])["tool_boundary"]["calls"][0]
    assert entered == [True]
    assert call["permission"] == "allowed"
    assert call["handler_admitted"] is True
    assert call["attempt_id"] is None
    assert call["result"]["error_kind"] == "plan_rejected"


def test_recover_boundary_is_possible_and_needs_no_outer_attempt(tmp_path: Path):
    state = AgentState()
    state.begin_task("durable recover")
    context = ContextManager(
        state, [{"role": "user", "content": "recover"}], observability=False,
    )
    store = SessionStore(tmp_path / "sessions")
    envelope = store.save(None, state, context, workspace_root=tmp_path)
    registry = ToolRegistry()
    entered = []
    registry.register(Tool(
        "recover", "recover", {"type": "object", "properties": {}},
        lambda: entered.append(True) or "recovered",
    ))
    executor = ToolExecutor(
        registry, gate=PermissionGate(PermissionPolicy({"recover": "allow"})),
    )
    executor.session_boundary = DurableToolBoundary(
        store, envelope["session_id"], tmp_path,
    )
    responses = [{
        "role": "assistant", "content": None, "tool_calls": [{
            "id": "recover-1", "type": "function", "function": {
                "name": "recover", "arguments": "{}",
            },
        }],
    }, {"role": "assistant", "content": "done"}]

    with patch("mini_agent.agent.call_llm", side_effect=lambda *args, **kwargs: responses.pop(0)):
        assert agent_loop(context, executor) == "done"

    call = store.load(envelope["session_id"])["tool_boundary"]["calls"][0]
    assert entered == [True]
    assert call["effect_class"] == "possible"
    assert call["handler_admitted"] is True
    assert call["attempt_id"] is None
    assert call["status"] == "committed"


def test_execute_admitted_does_not_repeat_terminal_gate_and_settles_reservation():
    entered = []
    state = AgentState()
    state.begin_task("fixed admission")
    registry = ToolRegistry()
    registry.register(Tool(
        "read_value", "read", {"type": "object", "properties": {}},
        lambda: entered.append(True) or "ok",
    ))
    executor = ToolExecutor(
        registry, gate=PermissionGate(PermissionPolicy({"read_value": "allow"})),
    )

    admission = executor.admit("read_value", {}, state)
    assert admission.reservation.attempt_id in state._pending_attempts
    state.status = "blocked"
    state.terminal_reason = "changed after durable admission"
    result = executor.execute_admitted(admission, notify=False)
    state.record_execution_result(result)

    assert entered == [True]
    assert result.handler_admitted is True
    assert result.reservation == admission.reservation
    assert state._pending_attempts == set()


def test_concurrent_results_commit_each_available_ordered_prefix(tmp_path: Path):
    second_entered = threading.Event()
    release_second = threading.Event()
    first_committed = threading.Event()

    def handler(value):
        if value == 2:
            second_entered.set()
            assert release_second.wait(2)
        return f"value={value}"

    state, context, store, envelope, executor = _runtime(tmp_path, handler=handler)
    record_result = executor.session_boundary.record_execution_result

    def observe_result(invocation_id, *args, **kwargs):
        saved = record_result(invocation_id, *args, **kwargs)
        if invocation_id.endswith("c-0"):
            first_committed.set()
        return saved

    executor.session_boundary.record_execution_result = observe_result
    assistant = {
        "role": "assistant", "content": None, "tool_calls": [
            {"id": "call-1", "type": "function", "function": {
                "name": "read_value", "arguments": json.dumps({"value": 1}),
            }},
            {"id": "call-2", "type": "function", "function": {
                "name": "read_value", "arguments": json.dumps({"value": 2}),
            }},
        ],
    }
    responses = [assistant, {"role": "assistant", "content": "done"}]
    outcome = []

    def run():
        outcome.append(agent_loop(context, executor))

    with patch("mini_agent.agent.call_llm", side_effect=lambda *args, **kwargs: responses.pop(0)):
        thread = threading.Thread(target=run)
        thread.start()
        try:
            assert second_entered.wait(2)
            assert first_committed.wait(2)
            saved = store.load(envelope["session_id"])
            assert [call["status"] for call in saved["tool_boundary"]["calls"]] == [
                "committed", "pending",
            ]
        finally:
            release_second.set()
            thread.join(2)

    assert not thread.is_alive()
    assert outcome == ["done"]


def test_concurrent_result_cannot_overtake_model_order(tmp_path: Path):
    first_entered = threading.Event()
    release_first = threading.Event()
    second_finished = threading.Event()

    def handler(value):
        if value == 1:
            first_entered.set()
            assert release_first.wait(2)
        else:
            second_finished.set()
        return f"value={value}"

    state, context, store, envelope, executor = _runtime(tmp_path, handler=handler)
    assistant = {
        "role": "assistant", "content": None, "tool_calls": [
            {"id": "call-1", "type": "function", "function": {
                "name": "read_value", "arguments": json.dumps({"value": 1}),
            }},
            {"id": "call-2", "type": "function", "function": {
                "name": "read_value", "arguments": json.dumps({"value": 2}),
            }},
        ],
    }
    responses = [assistant, {"role": "assistant", "content": "done"}]
    outcome = []

    with patch("mini_agent.agent.call_llm", side_effect=lambda *args, **kwargs: responses.pop(0)):
        thread = threading.Thread(target=lambda: outcome.append(agent_loop(context, executor)))
        thread.start()
        try:
            assert first_entered.wait(2)
            assert second_finished.wait(2)
            saved = store.load(envelope["session_id"])
            assert [call["status"] for call in saved["tool_boundary"]["calls"]] == [
                "pending", "pending",
            ]
        finally:
            release_first.set()
            thread.join(2)

    assert not thread.is_alive()
    assert outcome == ["done"]


def test_write_process_input_is_absent_from_schema3_bytes(tmp_path: Path):
    state = AgentState()
    state.begin_task("redact durable input")
    secret = "DURABLE-STDIN-SECRET"
    context = ContextManager(state, [
        {"role": "user", "content": "send input"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call-input", "type": "function", "function": {
                "name": "write_process", "arguments": json.dumps({
                    "process_id": "proc-1", "input": secret, "close_stdin": False,
                }),
            },
        }]},
        {"role": "tool", "tool_call_id": "call-input", "content": "written_bytes=1"},
    ], observability=False)
    store = SessionStore(tmp_path / "sessions")
    envelope = store.save(None, state, context, workspace_root=tmp_path)
    raw = store.path_for(envelope["session_id"]).read_bytes()
    assert secret.encode() not in raw
    assert b"redacted:write_process.input" in raw
