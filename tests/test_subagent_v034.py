"""v0.34 controlled, synchronous, read-only subagent tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
import json
import os
import re
from pathlib import Path

import pytest

from mini_agent.context import ContextManager
from mini_agent.checkpoint import CheckpointStore
from mini_agent.delegation import (
    DelegationError,
    DelegationManager,
    ScopeGate,
    SubagentBudget,
    SubagentRunner,
    build_delegated_task,
    validate_delegation_arguments,
)
from mini_agent.permission import ALLOW, PermissionGate, PermissionPolicy
from mini_agent.state import AgentState
from mini_agent.tools import create_registry
from mini_agent.tools.base import Tool, ToolExecutor, ToolRegistry


def _arguments(**changes):
    result = {
        "goal": "inspect the implementation",
        "scope": ["src"],
        "constraints": [],
        "expected_findings": ["identify the relevant code"],
        "requested_tools": ["read_file", "grep"],
        "selected_parent_facts": [],
        "purpose": "investigation",
    }
    result.update(changes)
    return result


def _report(summary="ok"):
    return json.dumps({
        "summary": summary,
        "findings": [],
        "evidence": [],
        "limitations": [],
    }, ensure_ascii=False)


def test_contract_and_budget_reject_invalid_values(tmp_path: Path):
    with pytest.raises(DelegationError):
        validate_delegation_arguments(_arguments(goal=""))
    with pytest.raises(DelegationError):
        validate_delegation_arguments(_arguments(requested_tools=["read_file", "read_file"]))
    with pytest.raises(DelegationError):
        validate_delegation_arguments(_arguments(requested_tools=["run_shell"]))
    with pytest.raises(DelegationError):
        validate_delegation_arguments(_arguments(purpose="diagnosis"))
    with pytest.raises(DelegationError):
        validate_delegation_arguments(_arguments(selected_parent_facts=["Authorization: Bearer abc"]))
    with pytest.raises(DelegationError):
        SubagentBudget(max_rounds=9)
    with pytest.raises(DelegationError):
        build_delegated_task(_arguments(budget={"max_rounds": 0}), workspace_root=tmp_path)


def test_scope_gate_rejects_escape_symlink_and_secret(tmp_path: Path):
    (tmp_path / "src").mkdir()
    outside = tmp_path.parent / f"outside-{tmp_path.name}"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (tmp_path / "src" / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(DelegationError):
        ScopeGate(tmp_path, ["../outside"])
    with pytest.raises(DelegationError):
        ScopeGate(tmp_path, ["config_local.py"])
    gate = ScopeGate(tmp_path, ["src"])
    with pytest.raises(DelegationError):
        gate.validate_tool_call("read_file", {"path": "src/escape/secret.txt"})
    assert gate.validate_path("src") == str(tmp_path / "src")


def test_filtered_view_freezes_capabilities_and_hides_state_tools():
    parent = ToolRegistry()
    parent.register(Tool("calculate", "math", {"type": "object", "properties": {}}, lambda: "1",
                         delegation_capability="pure_compute"))
    parent.register(Tool("plan_observe", "state", {"type": "object", "properties": {}}, lambda: "state"))
    view = parent.filtered_for_subagent({"calculate", "plan_observe"})
    assert [item.name for item in view.list_tools()] == ["calculate"]
    original = view.schemas()[0]["function"]["description"]
    parent.get("calculate").description = "mutated"
    assert view.schemas()[0]["function"]["description"] == original
    exposed = view.get("calculate")
    exposed.handler = lambda: "mutated-view"
    assert view.get("calculate").handler() == "1"
    with pytest.raises(TypeError):
        view.register(Tool("x", "x", {}, lambda: None))


def test_registry_without_parent_state_does_not_expose_delegation():
    registry = create_registry()
    assert "delegate_task" not in {tool.name for tool in registry.list_tools()}
    with pytest.raises(ValueError):
        create_registry(include_delegation=True)


def test_registry_preserves_restored_checkpoint_store(tmp_path: Path):
    state = AgentState()
    state.begin_task("resume")
    restored = CheckpointStore(str(tmp_path))
    state.bind_checkpoint_store(restored)
    registry = create_registry(state, workspace_root=str(tmp_path))
    assert state.checkpoint_store is restored
    assert registry._checkpoint_store is restored


def test_cross_file_investigation_isolated_and_structured(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
    calls = []

    def child_llm(messages, **_kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "sub-1", "type": "function",
                    "function": {"name": "grep", "arguments": json.dumps({"pattern": "VALUE", "path": "src"})},
                }],
            }
        return {"role": "assistant", "content": _report("found VALUE")}

    parent_state = AgentState()
    parent_state.begin_task("parent")
    registry = create_registry(parent_state, workspace_root=str(tmp_path), subagent_llm=child_llm)
    executor = ToolExecutor(registry, PermissionGate(PermissionPolicy({"delegate_task": ALLOW})))
    result = executor.execute_result("delegate_task", _arguments(), parent_state, notify=False)
    assert result.ok
    payload = json.loads(result.output)
    assert payload["outcome"] == "completed"
    assert payload["delegation_id"] and payload["subagent_id"]
    assert len(calls) == 2
    assert all("delegate_task" not in str(message) for message in calls[0][0:1])
    assert parent_state.snapshot()["plan_revisions"] == []
    assert parent_state.snapshot()["verification_evidence"] == []


def test_forbidden_child_tool_is_a_tool_result_not_a_handler_call(tmp_path: Path):
    calls = []

    def child_llm(messages, **_kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "bad", "type": "function",
                    "function": {"name": "run_shell", "arguments": json.dumps({"command": "touch bad"})},
                }],
            }
        return {"role": "assistant", "content": _report()}

    task = build_delegated_task(_arguments(requested_tools=["read_file"]), workspace_root=tmp_path)
    result = SubagentRunner(tmp_path, llm=child_llm).run(task)
    assert result.outcome == "completed"
    assert "未知或禁止" in str(calls[1]) or "工具调用失败" in str(calls[1])
    assert not (tmp_path / "bad").exists()


def test_invalid_report_gets_one_correction_then_fails(tmp_path: Path):
    calls = []

    def child_llm(messages, **_kwargs):
        calls.append(messages)
        return {"role": "assistant", "content": "not json"}

    task = build_delegated_task(_arguments(requested_tools=["calculate"]), workspace_root=tmp_path)
    result = SubagentRunner(tmp_path, llm=child_llm).run(task)
    assert result.outcome == "failed"
    assert result.error_kind == "invalid_result"
    assert len(calls) == 2
    assert any("格式" in str(message) for message in calls[1])


def test_tool_budget_rejection_closes_every_child_tool_call(tmp_path: Path):
    def child_llm(_messages, **_kwargs):
        return {
            "role": "assistant", "content": None,
            "tool_calls": [
                {"id": "one", "type": "function", "function": {
                    "name": "calculate", "arguments": '{"expression":"1+1"}',
                }},
                {"id": "two", "type": "function", "function": {
                    "name": "calculate", "arguments": '{"expression":"2+2"}',
                }},
            ],
        }

    task = build_delegated_task(
        _arguments(requested_tools=["calculate"], budget={"max_tool_calls": 1}),
        workspace_root=tmp_path,
    )
    runner = SubagentRunner(tmp_path, llm=child_llm)
    result = runner.run(task)
    assert result.outcome == "budget_exhausted"
    assert [message["role"] for message in runner.last_context.history[-3:]] == [
        "assistant", "tool", "tool",
    ]
    assert [message["tool_call_id"] for message in runner.last_context.history[-2:]] == [
        "one", "two",
    ]


def test_malformed_child_calls_receive_protocol_results(tmp_path: Path):
    def child_llm(_messages, **_kwargs):
        return {"role": "assistant", "content": None, "tool_calls": [
            {"id": "dup", "type": "function", "function": {
                "name": "calculate", "arguments": "{}",
            }},
            {"id": "dup", "type": "wrong", "function": {
                "name": "calculate", "arguments": "not-json",
            }},
        ]}

    task = build_delegated_task(
        _arguments(requested_tools=["calculate"]), workspace_root=tmp_path,
    )
    runner = SubagentRunner(tmp_path, llm=child_llm)
    result = runner.run(task)
    assert result.outcome == "failed"
    assistant = runner.last_context.history[-3]
    tools = runner.last_context.history[-2:]
    assert assistant["role"] == "assistant" and len(assistant["tool_calls"]) == 2
    assert all(message["role"] == "tool" for message in tools)
    assert [message["tool_call_id"] for message in tools] == [
        call["id"] for call in assistant["tool_calls"]
    ]


def test_provider_type_error_is_not_retried(tmp_path: Path):
    calls = []

    def child_llm(_messages, **_kwargs):
        calls.append(True)
        raise TypeError("provider rejected request")

    task = build_delegated_task(_arguments(requested_tools=["calculate"]), workspace_root=tmp_path)
    result = SubagentRunner(tmp_path, llm=child_llm).run(task)
    assert result.outcome == "failed"
    assert result.error_kind == "llm_error"
    assert len(calls) == 1


def test_provider_timeout_is_timed_out(tmp_path: Path):
    calls = []

    def child_llm(_messages, **_kwargs):
        calls.append(True)
        raise TimeoutError("deadline")

    task = build_delegated_task(_arguments(requested_tools=["calculate"]), workspace_root=tmp_path)
    result = SubagentRunner(tmp_path, llm=child_llm).run(task)
    assert result.outcome == "timed_out"
    assert result.error_kind == "timeout"
    assert len(calls) == 1


def test_observation_evidence_must_match_actual_tool_result(tmp_path: Path):
    calls = []

    def child_llm(messages, **_kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "calc-1", "type": "function",
                    "function": {"name": "calculate", "arguments": '{"expression":"1+1"}'},
                }],
            }
        tool_content = messages[-1]["content"]
        match = re.search(r"observation_hash=([0-9a-f]{64})", tool_content)
        assert match
        return {"role": "assistant", "content": json.dumps({
            "summary": "calculated", "findings": [{
                "id": "f1", "claim": "calculation completed", "evidence_ids": ["e1"],
                "confidence": "observed",
            }], "evidence": [{
                "id": "e1", "kind": "tool_observation", "claim": "1+1 was evaluated",
                "tool": "calculate", "observation_hash": match.group(1),
            }], "limitations": [],
        })}

    task = build_delegated_task(_arguments(requested_tools=["calculate"]), workspace_root=tmp_path)
    result = SubagentRunner(tmp_path, llm=child_llm).run(task)
    assert result.outcome == "completed"
    assert result.evidence[0].observation_hash
    assert result.findings[0].evidence_ids == ("e1",)


def test_file_evidence_line_must_have_been_observed(tmp_path: Path):
    (tmp_path / "a.py").write_text("only one line\n", encoding="utf-8")
    calls = 0

    def child_llm(_messages, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "read", "type": "function", "function": {
                    "name": "read_file",
                    "arguments": '{"path":"a.py","offset":0,"limit":1}',
                },
            }]}
        return {"role": "assistant", "content": json.dumps({
            "summary": "unsupported line", "findings": [{
                "id": "f1", "claim": "line 999", "evidence_ids": ["e1"],
                "confidence": "observed",
            }], "evidence": [{
                "id": "e1", "kind": "file_location", "claim": "not read",
                "path": "a.py", "line": 999,
            }], "limitations": [],
        })}

    task = build_delegated_task(
        _arguments(scope=["."], requested_tools=["read_file"]),
        workspace_root=tmp_path,
    )
    result = SubagentRunner(tmp_path, llm=child_llm).run(task)
    assert result.outcome == "failed"
    assert result.error_kind == "invalid_result"


def test_contract_and_selected_facts_stay_in_task_message(tmp_path: Path):
    facts = ["Observed marker: safe-test-fact"]

    def child_llm(_messages, **_kwargs):
        return {"role": "assistant", "content": _report()}

    task = build_delegated_task(_arguments(selected_parent_facts=facts), workspace_root=tmp_path)
    runner = SubagentRunner(tmp_path, llm=child_llm)
    result = runner.run(task)
    assert result.outcome == "completed"
    protected = runner.last_context.protected_messages[0]["content"]
    assert "safe-test-fact" not in protected
    assert "<delegation_contract>" not in protected


def test_result_contract_rejects_dangling_and_bad_evidence(tmp_path: Path):
    def child_llm(_messages, **_kwargs):
        return {"role": "assistant", "content": json.dumps({
            "summary": "bad", "findings": [{
                "id": "f1", "statement": "inferred", "evidence": ["missing"],
                "inferred": True, "caveat": "maybe",
            }], "evidence": [], "limitations": [],
        })}

    task = build_delegated_task(_arguments(requested_tools=["calculate"]), workspace_root=tmp_path)
    result = SubagentRunner(tmp_path, llm=child_llm).run(task)
    assert result.outcome == "failed"
    assert result.error_kind == "invalid_result"


def test_phase_gate_requires_current_failure_or_crash_issue():
    state = AgentState()
    state.begin_task("diagnose")
    state._repair_phase = "diagnosis_required"
    state._active_failure_id = "f-1"
    assert state.delegation_gate("delegate_task", _arguments(purpose="diagnosis", source_id="f-1")) is None
    assert state.delegation_gate("delegate_task", _arguments(purpose="diagnosis", source_id="other"))
    assert state.delegation_gate("delegate_task", _arguments())
    state.status = "blocked"
    assert state.delegation_gate("delegate_task", _arguments(purpose="diagnosis", source_id="f-1"))
