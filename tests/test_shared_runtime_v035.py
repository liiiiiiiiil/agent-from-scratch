"""v0.35 tests for the canonical parent/child AgentRuntime loop."""

from __future__ import annotations

import inspect
import json
import time
from pathlib import Path
from unittest.mock import patch

from mini_agent.agent import agent_loop
from mini_agent.context import ContextManager
from mini_agent.delegation import SubagentRunner, build_delegated_task
from mini_agent.permission import ALLOW, PermissionGate, PermissionPolicy
from mini_agent.runtime import AgentRuntime, RuntimeDecision, ToolRoundPlan
from mini_agent.state import AgentState
from mini_agent.tools.base import Tool, ToolExecutor, ToolRegistry


class _SharedPolicy:
    def before_run(self, runtime):
        return None

    def before_prepare(self, runtime):
        return None

    def before_llm(self, runtime):
        return None

    def llm_options(self, runtime):
        return {}

    def on_text(self, runtime, content):
        return RuntimeDecision("finish", content, "text")

    def prepare_tool_round(self, runtime, calls):
        return ToolRoundPlan(serial=False)

    def after_tool_result(self, runtime, call, execution):
        return execution.tool_content()

    def after_tool_round(self, runtime, calls, results):
        return None

    def on_round_limit(self, runtime):
        return RuntimeDecision("finish", "limit", "round_limit")


def _shared_runtime(llm, handler):
    state = AgentState()
    state.begin_task("shared runtime")
    context = ContextManager(state, [{"role": "user", "content": "use tools"}], observability=False)
    registry = ToolRegistry()
    registry.register(Tool(
        "first", "first", {"type": "object", "properties": {}},
        handler, delegation_capability="pure_compute",
    ))
    registry.register(Tool(
        "second", "second", {"type": "object", "properties": {}},
        handler, delegation_capability="pure_compute",
    ))
    executor = ToolExecutor(
        registry, PermissionGate(PermissionPolicy({"first": ALLOW, "second": ALLOW})),
    )
    return AgentRuntime(
        llm_client=llm,
        context=context,
        executor=executor,
        policy=_SharedPolicy(),
        max_rounds=4,
    ), context


def test_parent_and_subagent_use_canonical_run_and_no_legacy_loop():
    runtime_source = inspect.getsource(AgentRuntime)
    parent_source = inspect.getsource(agent_loop)
    child_source = inspect.getsource(SubagentRunner.run)
    assert "loop_impl" not in inspect.signature(AgentRuntime).parameters
    assert "_legacy_agent_loop" not in parent_source
    assert "while True" not in child_source

    parent_calls = []
    child_calls = []

    def parent_llm(_messages, **_kwargs):
        parent_calls.append(True)
        return {"role": "assistant", "content": "parent done"}

    def child_llm(_messages, **_kwargs):
        child_calls.append(True)
        return {"role": "assistant", "content": '{"summary":"child","findings":[],"evidence":[],"limitations":[]}'}

    state = AgentState(task="parent")
    context = ContextManager(state, [{"role": "user", "content": "parent"}], observability=False)
    with patch("mini_agent.agent.call_llm", side_effect=parent_llm):
        assert agent_loop(context, ToolExecutor(ToolRegistry())) == "parent done"

    task = build_delegated_task({
        "goal": "inspect", "scope": ["."], "constraints": [],
        "expected_findings": [], "requested_tools": ["calculate"],
        "selected_parent_facts": [], "purpose": "investigation",
    }, workspace_root=Path.cwd())
    real_run = AgentRuntime.run
    entered = []

    def spy(instance):
        entered.append(instance)
        return real_run(instance)

    with patch.object(AgentRuntime, "run", spy):
        result = SubagentRunner(Path.cwd(), llm=child_llm).run(task)
    assert result.outcome == "completed"
    assert parent_calls == [True]
    assert child_calls == [True]
    assert len(entered) == 1
    assert "while self.rounds <" in runtime_source


def test_same_protocol_skeleton_commits_parallel_results_in_model_order():
    completion_order = []

    def handler(name=None):
        if name == "first":
            time.sleep(0.02)
        completion_order.append(name)
        return name

    responses = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "one", "type": "function", "function": {"name": "first", "arguments": "{}"}},
            {"id": "two", "type": "function", "function": {"name": "second", "arguments": "{}"}},
        ]},
        {"role": "assistant", "content": "done"},
    ]

    def llm(messages, **_kwargs):
        if len(messages) > 2:
            assert [item["role"] for item in messages[-2:]] == ["tool", "tool"]
            assert [item["tool_call_id"] for item in messages[-2:]] == ["one", "two"]
        return responses.pop(0)

    runtime, context = _shared_runtime(llm, lambda: None)
    # The handler needs the tool name; use the immutable definition's closure
    # only to make completion order observable.
    runtime.executor.registry.get("first").handler = lambda: time.sleep(0.02) or completion_order.append("first") or "first"
    runtime.executor.registry.get("second").handler = lambda: completion_order.append("second") or "second"
    result = runtime.run()
    assert result.content == "done"
    assert completion_order == ["second", "first"]
    assert [item["tool_call_id"] for item in context.history[-3:-1]] == ["one", "two"]


def test_normalization_assigns_unique_ids_and_closes_every_invalid_call():
    responses = [{
        "role": "assistant", "content": None, "tool_calls": [
            {"id": "duplicate", "type": "function", "function": {"name": "first", "arguments": "{}"}},
            {"id": "duplicate", "type": "function", "function": {"name": "first", "arguments": "bad"}},
            {"type": "wrong", "function": {"name": "", "arguments": None}},
        ],
    }, {"role": "assistant", "content": "done"}]

    def llm(_messages, **_kwargs):
        return responses.pop(0)

    runtime, context = _shared_runtime(llm, lambda: "ok")
    result = runtime.run()
    assistant = context.history[1]
    tools = context.history[2:5]
    assert result.content == "done"
    assert len(assistant["tool_calls"]) == len(tools) == 3
    assert len({call["id"] for call in assistant["tool_calls"]}) == 3
    assert [item["tool_call_id"] for item in tools] == [call["id"] for call in assistant["tool_calls"]]
    assert all("工具调用失败" in item["content"] for item in tools[1:])


def test_before_prepare_updates_state_before_context_snapshot():
    state = AgentState(task="stale")

    class SnapshotContext:
        def __init__(self):
            self.state = state
            self.history = [{"role": "user", "content": "inspect"}]

        def prepare_messages(self):
            assert self.state.task == "fresh"
            return list(self.history)

    class Policy(_SharedPolicy):
        def before_prepare(self, runtime):
            runtime.context.state.task = "fresh"
            return None

    runtime = AgentRuntime(
        llm_client=lambda _messages, **_kwargs: {"role": "assistant", "content": "done"},
        context=SnapshotContext(), executor=ToolExecutor(ToolRegistry()),
        policy=Policy(), max_rounds=1,
    )
    assert runtime.run().content == "done"


def test_compat_executor_uses_a_legal_execution_outcome():
    responses = iter([
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "compat", "type": "function", "function": {
                "name": "legacy", "arguments": "{}",
            },
        }]},
        {"role": "assistant", "content": "done"},
    ])
    seen = []

    class CompatExecutor:
        def execute(self, _name, _arguments):
            return "ok"

    class Policy(_SharedPolicy):
        def after_tool_result(self, runtime, call, execution):
            seen.append(execution.outcome)
            return execution.tool_content()

    state = AgentState(task="compat")
    context = ContextManager(
        state, [{"role": "user", "content": "compat"}], observability=False,
    )
    runtime = AgentRuntime(
        llm_client=lambda _messages, **_kwargs: next(responses),
        context=context, executor=CompatExecutor(), policy=Policy(), max_rounds=2,
    )
    assert runtime.run().content == "done"
    assert seen == ["succeeded"]


def test_malformed_child_round_does_not_enter_any_handler(tmp_path):
    entered = []
    parent = ToolRegistry()
    parent.register(Tool(
        "calculate", "calculate", {
            "type": "object", "properties": {}, "additionalProperties": False,
        }, lambda: entered.append(True) or "unexpected",
        delegation_capability="pure_compute",
    ))

    def child_llm(_messages, **_kwargs):
        return {"role": "assistant", "content": None, "tool_calls": [
            {"id": "good", "type": "function", "function": {
                "name": "calculate", "arguments": "{}",
            }},
            {"id": "bad", "type": "wrong", "function": {
                "name": "calculate", "arguments": "{}",
            }},
        ]}

    task = build_delegated_task({
        "goal": "calculate", "scope": ["."], "constraints": [],
        "expected_findings": [], "requested_tools": ["calculate"],
        "selected_parent_facts": [], "purpose": "investigation",
    }, workspace_root=tmp_path)
    runner = SubagentRunner(tmp_path, llm=child_llm, parent_registry=parent)
    result = runner.run(task)
    assert result.outcome == "failed"
    assert result.error_kind == "invalid_tool_call"
    assert entered == []
    assert len([item for item in runner.last_context.history if item["role"] == "tool"]) == 2


def test_child_budget_rejection_is_a_complete_runtime_round(tmp_path):
    calls = []

    def child_llm(_messages, **_kwargs):
        return {"role": "assistant", "content": None, "tool_calls": [
            {"id": "one", "type": "function", "function": {
                "name": "calculate", "arguments": '{"expression":"1+1"}',
            }},
            {"id": "two", "type": "function", "function": {
                "name": "calculate", "arguments": '{"expression":"2+2"}',
            }},
        ]}

    task = build_delegated_task({
        "goal": "calculate", "scope": ["."], "constraints": [],
        "expected_findings": [], "requested_tools": ["calculate"],
        "selected_parent_facts": [], "purpose": "investigation",
        "budget": {"max_tool_calls": 1},
    }, workspace_root=tmp_path)
    runner = SubagentRunner(tmp_path, llm=child_llm)
    result = runner.run(task)
    assert result.outcome == "budget_exhausted"
    assert len([item for item in runner.last_context.history if item["role"] == "tool"]) == 2
    assert calls == []
