"""v0.37 lifecycle, cancellation, aggregate budget, and safe-point coverage."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Thread
import time
from types import SimpleNamespace

import pytest

from mini_agent.delegation import (
    DelegationManager,
    SubagentResult,
    SubagentRuntimePolicy,
    UsageRecord,
    build_delegated_task,
)
from mini_agent.agent import ParentRuntimePolicy
from mini_agent.context import ContextBudget, ContextManager
from mini_agent.runtime import AgentRuntime
from mini_agent.session import SessionStore, SessionValidationError
from mini_agent.state import AgentState, DelegationBudget
from mini_agent.tools import create_registry
from mini_agent.tools.base import ToolExecutor


def _arguments(**overrides):
    value = {
        "goal": "inspect the implementation",
        "scope": ["."],
        "constraints": [],
        "expected_findings": [],
        "requested_tools": ["calculate"],
        "selected_parent_facts": [],
        "purpose": "investigation",
    }
    value.update(overrides)
    return value


def _report(summary="ok"):
    return {
        "role": "assistant",
        "content": json.dumps({
            "summary": summary, "findings": [], "evidence": [], "limitations": [],
        }),
    }


def test_reservation_settlement_and_ordered_delivery(tmp_path: Path):
    state = AgentState()
    state.begin_task("parent")
    task = build_delegated_task(_arguments(), state, workspace_root=tmp_path)
    record = state.reserve_delegation(task)
    state.start_delegation(task.delegation_id)
    result = SubagentResult(
        "result-1", task.delegation_id, task.subagent_id, task.parent_task_id,
        "completed", "one bounded result", usage=UsageRecord(
            rounds=1, llm_calls=1, tool_calls=0, tokens=12,
        ), contract_hash=task.contract_hash,
    )
    ready = state.delegation_result_ready(task.delegation_id, result)
    assert record.delivery_status == "created"
    assert ready.delivery_status == "result_ready"
    assert state.delegation_budget_snapshot()["reserved_llm_calls"] == 0
    assert state.delegation_budget_snapshot()["used_llm_calls"] == 1
    with pytest.raises(SessionValidationError):
        SessionStore(tmp_path / "sessions").save(
            None, state, type("Context", (), {"export_session": lambda self: {
                "format": "mini_agent.context", "format_version": 1,
                "history": [], "summary": "", "compacted": False,
                "summarized_rounds": 0, "keep_rounds": 6,
                "runtime_notice": None,
            }})(), workspace_root=tmp_path,
        )
    committed = state.commit_delegation(task.delegation_id, result_id="result-1")
    assert committed.delivery_status == "committed"
    assert state.commit_delegation(task.delegation_id, result_id="result-1") == committed


def test_parent_budget_refusal_happens_before_child_llm(tmp_path: Path):
    calls = []
    state = AgentState()
    state.begin_task("parent")
    state.configure_delegation_budget(
        max_total_llm_calls=1, max_total_tool_calls=1, max_total_tokens=100,
    )
    manager = DelegationManager(
        tmp_path, subagent_llm=lambda *args, **kwargs: calls.append(True) or _report(),
        parent_state=state,
    )
    result = manager.run(_arguments(budget={
        "max_rounds": 1, "max_llm_calls": 2, "max_tool_calls": 1,
        "max_tokens": 100,
    }), state)
    assert result.outcome == "budget_exhausted"
    assert result.error_kind == "aggregate_budget"
    assert calls == []
    assert state.snapshot()["delegations"][0]["delivery_status"] == "result_ready"
    state.commit_delegation(result.delegation_id, result_id=result.result_id)
    assert state.delegation_budget_snapshot()["used_llm_calls"] == 0


def test_manager_cancellation_is_cooperative_and_committable(tmp_path: Path):
    state = AgentState()
    state.begin_task("parent")

    def child_llm(*_args, **_kwargs):
        time.sleep(0.05)
        return _report("late response")

    manager = DelegationManager(tmp_path, subagent_llm=child_llm, parent_state=state)
    holder = []
    thread = Thread(target=lambda: holder.append(manager.run(_arguments(), state)))
    thread.start()
    for _ in range(100):
        if manager.active_info()["active"]:
            break
        time.sleep(0.001)
    assert manager.cancel(state.task_id, "reset requested")
    assert manager.wait(1.0)
    thread.join(timeout=1)
    assert holder and holder[0].outcome == "cancelled"
    assert holder[0].error_kind == "cancelled"
    state.commit_delegation(holder[0].delegation_id, result_id=holder[0].result_id)
    assert state.snapshot()["delegations"][0]["delivery_status"] == "committed"


def test_child_keyboard_interrupt_stops_parent_after_tool_delivery(tmp_path: Path):
    state = AgentState()
    state.begin_task("parent")
    parent_calls = []

    def parent_llm(*_args, **_kwargs):
        parent_calls.append(True)
        if len(parent_calls) == 1:
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "delegate-call", "type": "function",
                "function": {"name": "delegate_task", "arguments": json.dumps(_arguments())},
            }]}
        return {"role": "assistant", "content": "unexpected second request"}

    def interrupted_child(*_args, **_kwargs):
        raise KeyboardInterrupt

    context = ContextManager(state, [])
    runtime = AgentRuntime(
        llm_client=parent_llm, context=context,
        executor=ToolExecutor(create_registry(
            state, workspace_root=tmp_path, subagent_llm=interrupted_child,
        )), policy=ParentRuntimePolicy(), max_rounds=3,
    )
    with pytest.raises(KeyboardInterrupt):
        runtime.run()
    assert len(parent_calls) == 1
    assert [message["role"] for message in context.history] == ["assistant", "tool"]
    assert state.delegation_records[0].delivery_status == "committed"


def test_compaction_summary_checks_child_budget_before_request(tmp_path: Path):
    state = AgentState()
    state.begin_task("parent")
    task = build_delegated_task(
        _arguments(budget={"max_tokens": 1}), state, workspace_root=tmp_path,
    )
    summary_calls = []
    runner = SimpleNamespace(_summarizer=lambda: lambda prompt, **options:
                             summary_calls.append(options) or "summary")
    policy = SubagentRuntimePolicy(
        runner=runner, task=task, scope_gate=None, started_clock=time.monotonic(),
        started_at="", state=AgentState(),
    )
    runtime = SimpleNamespace(
        llm_calls=0, estimated_tokens=0, request_tokens=0, usage_meter=None,
        input_tokens=0, output_tokens=0, _refresh_usage=lambda: None,
    )
    policy.runtime = runtime
    history = [{"role": "system", "content": "system"},
               {"role": "user", "content": "task"}]
    for index in range(8):
        history.append({"role": "user", "content": f"round {index} " + "x" * 100})
        history.append({"role": "assistant", "content": "observed"})
    def context_for(candidate):
        context = ContextManager(
            AgentState(task="task"), history,
            ContextBudget(window=120, output_reserve_ratio=0, history_ratio=0.5),
            summarizer=candidate.summarize, keep_rounds=2,
        )
        context.before_summary = candidate.before_summary
        return context

    context = context_for(policy)
    context.prepare_messages()
    assert summary_calls == []
    assert policy.terminal[0] == "budget_exhausted"
    assert policy.before_llm(runtime).action == "finish"

    allowed_task = build_delegated_task(
        _arguments(), state, workspace_root=tmp_path,
    )
    allowed = SubagentRuntimePolicy(
        runner=runner, task=allowed_task, scope_gate=None,
        started_clock=time.monotonic(), started_at="", state=AgentState(),
    )
    allowed.runtime = runtime
    context_for(allowed).prepare_messages()
    assert len(summary_calls) == 1
    assert 0 < summary_calls[0]["max_output_tokens"] < allowed_task.budget.max_tokens
    assert runtime.llm_calls == 1


def test_parent_runtime_commits_delegate_after_role_tool_result(tmp_path: Path):
    state = AgentState()
    state.begin_task("parent")
    parent_calls = 0
    arguments = _arguments()

    def parent_llm(*_args, **_kwargs):
        nonlocal parent_calls
        parent_calls += 1
        if parent_calls == 1:
            return {
                "role": "assistant", "content": None, "tool_calls": [{
                    "id": "delegate-call-1", "type": "function",
                    "function": {
                        "name": "delegate_task",
                        "arguments": json.dumps(arguments),
                    },
                }],
            }
        return {"role": "assistant", "content": "parent finished"}

    registry = create_registry(
        state, workspace_root=tmp_path,
        subagent_llm=lambda *_args, **_kwargs: _report("child finished"),
    )
    context = ContextManager(state, [])
    result = AgentRuntime(
        llm_client=parent_llm,
        context=context,
        executor=ToolExecutor(registry),
        policy=ParentRuntimePolicy(),
        max_rounds=4,
    ).run()

    assert result.stop_reason == "text"
    assert [item["role"] for item in context.history] == ["assistant", "tool", "assistant"]
    assert state.snapshot()["delegations"][0]["delivery_status"] == "committed"


def test_timeout_and_exception_results_settle_parent_usage(tmp_path: Path):
    state = AgentState()
    state.begin_task("parent")
    manager = DelegationManager(
        tmp_path, subagent_llm=lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError()),
        parent_state=state,
    )
    result = manager.run(_arguments(), state)
    assert result.outcome == "timed_out"
    assert result.usage.llm_calls == 1
    assert state.snapshot()["delegation_budget"]["used_llm_calls"] == 1


def test_repeated_empty_results_hash_as_no_new_observation():
    first = json.dumps({
        "result_id": "r1", "outcome": "completed", "summary": "random one",
        "findings": [], "evidence": [], "limitations": [],
    }, sort_keys=True)
    second = json.dumps({
        "result_id": "r2", "outcome": "failed", "summary": "random two",
        "findings": [], "evidence": [], "limitations": ["different wording"],
    }, sort_keys=True)
    from mini_agent.state import delegation_progress_hash
    assert delegation_progress_hash(first) == delegation_progress_hash(second)


def test_state_round_trip_preserves_settled_aggregate_ledger(tmp_path: Path):
    state = AgentState(
        delegation_budget=DelegationBudget(
            max_subagents=1, max_concurrency=1, max_total_llm_calls=4,
            max_total_tool_calls=5, max_total_tokens=600,
            created_subagents=1, used_llm_calls=2, used_tool_calls=1, used_tokens=240,
        ),
    )
    state.begin_task("parent")
    state.delegation_budget = DelegationBudget(
        max_subagents=1, max_concurrency=1, max_total_llm_calls=4,
        max_total_tool_calls=5, max_total_tokens=600,
        created_subagents=1, used_llm_calls=2, used_tool_calls=1, used_tokens=240,
    )
    exported = state.export_session()
    restored = AgentState.restore_session(exported, tmp_path)
    assert restored.delegation_budget_snapshot()["used_llm_calls"] == 2
    assert restored.delegation_budget_snapshot()["remaining_tokens"] == 360
