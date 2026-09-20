"""v0.39 durable child-result delivery and recovery coverage."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Event

import pytest

from mini_agent.context import ContextManager
from mini_agent.delegation import DelegationManager
from mini_agent.runtime import AgentRuntime
from mini_agent.agent import ParentRuntimePolicy
from mini_agent.session import (
    DurableToolBoundary,
    SessionStore,
    SessionValidationError,
)
from mini_agent.resume import prepare_resume
from mini_agent.state import AgentState
from mini_agent.trace import build_trace, render_trace
from mini_agent.tools import create_registry
from mini_agent.tools.base import ToolExecutor


def _arguments(goal: str = "inspect") -> dict:
    return {
        "goal": goal,
        "scope": ["."],
        "constraints": [],
        "expected_findings": [],
        "requested_tools": ["calculate"],
        "selected_parent_facts": [],
        "purpose": "investigation",
    }


def _report(summary: str = "child result") -> dict:
    return {
        "role": "assistant",
        "content": json.dumps({
            "summary": summary, "findings": [], "evidence": [], "limitations": [],
        }),
    }


class _CrashOnCommitBoundary(DurableToolBoundary):
    def commit_delegation_result(self, *args, **kwargs):
        raise RuntimeError("injected parent delivery crash")


class _CrashAfterBatchBoundary(DurableToolBoundary):
    def persist_delegation_batch(self, *args, **kwargs):
        super().persist_delegation_batch(*args, **kwargs)
        raise RuntimeError("injected crash after delegation start")


class _CrashAfterReadyBoundary(DurableToolBoundary):
    def record_delegation_result_ready(self, *args, **kwargs):
        super().record_delegation_result_ready(*args, **kwargs)
        raise RuntimeError("injected crash after result_ready")


class _CrashOnSecondCommitBoundary(DurableToolBoundary):
    def __init__(self, store, session_id, workspace_root, last_ready: Event):
        super().__init__(store, session_id, workspace_root)
        self.last_ready = last_ready
        self.commit_count = 0

    def record_delegation_result_ready(self, invocation_id, *args, **kwargs):
        saved = super().record_delegation_result_ready(invocation_id, *args, **kwargs)
        if invocation_id == "r-1-c-2":
            self.last_ready.set()
        return saved

    def commit_delegation_result(self, *args, **kwargs):
        self.commit_count += 1
        if self.commit_count == 2:
            raise RuntimeError("injected half-round commit crash")
        return super().commit_delegation_result(*args, **kwargs)


def _parent_llm(arguments: dict):
    calls = [0]

    def parent(_messages, **_options):
        calls[0] += 1
        if calls[0] == 1:
            return {
                "role": "assistant", "content": None, "tool_calls": [{
                    "id": "delegate-call", "type": "function",
                    "function": {
                        "name": "delegate_task",
                        "arguments": json.dumps(arguments),
                    },
                }],
            }
        return {"role": "assistant", "content": "done"}

    return parent, calls


def _batch_parent_llm(goals: list[str]):
    calls = [0]

    def parent(_messages, **_options):
        calls[0] += 1
        if calls[0] == 1:
            return {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": f"delegate-call-{index}", "type": "function",
                    "function": {
                        "name": "delegate_task",
                        "arguments": json.dumps(_arguments(goal)),
                    },
                } for index, goal in enumerate(goals)],
            }
        return {"role": "assistant", "content": "done"}

    return parent, calls


def test_result_ready_is_durable_before_parent_delivery(tmp_path: Path):
    state = AgentState()
    state.begin_task("durable delegation")
    context = ContextManager(state, [])
    store = SessionStore(tmp_path / "sessions")
    initial = store.save(None, state, context, workspace_root=tmp_path)
    boundary = _CrashOnCommitBoundary(store, initial["session_id"], tmp_path)
    registry = create_registry(
        state, workspace_root=tmp_path,
        subagent_llm=lambda *_args, **_kwargs: _report("saved child result"),
    )
    parent, parent_calls = _parent_llm(_arguments())
    runtime = AgentRuntime(
        llm_client=parent, context=context, executor=ToolExecutor(registry),
        policy=ParentRuntimePolicy(), max_rounds=3, session_boundary=boundary,
    )

    with pytest.raises(RuntimeError, match="injected"):
        runtime.run()
    assert parent_calls[0] == 1
    source = store.load(initial["session_id"])
    durable = source["tool_boundary"]
    assert durable["status"] == "pending"
    assert durable["pending_delegation_results"]
    pending = durable["pending_delegation_results"][0]
    assert pending["result_json"]
    assert source["state"]["delegation_records"][0]["delivery_status"] == "result_ready"

    candidate = prepare_resume(store, initial["session_id"], tmp_path)
    resumed = candidate.claim()
    assert resumed.session_id != initial["session_id"]
    assert len(resumed.context.history) == 2  # assistant tool-call, role=tool
    assert resumed.context.history[-1]["content"] == pending["result_json"]
    assert resumed.state.delegation_records[0].delivery_status == "committed"
    assert resumed.state.delegation_budget_snapshot()["used_llm_calls"] == 1
    derived = store.load(resumed.session_id)
    assert derived["tool_boundary"]["status"] == "committed"
    assert "pending_delegation_results" not in derived["tool_boundary"]
    assert derived["tool_boundary"]["calls"][0]["result"]["content"] == pending["result_json"]
    trace = build_trace(resumed.state.snapshot())
    assert trace["integrity"]["status"] == "complete"
    assert any(edge["type"] == "parent_delegation" and edge["resolved"]
               for edge in trace["causal_edges"])
    assert any(edge["type"] == "delegation_lifecycle" for edge in trace["causal_edges"])
    assert "Delegations:" in render_trace(trace)


def test_crash_after_ready_save_reuses_exact_result(tmp_path: Path):
    state = AgentState()
    state.begin_task("ready-save crash")
    context = ContextManager(state, [])
    store = SessionStore(tmp_path / "sessions")
    initial = store.save(None, state, context, workspace_root=tmp_path)
    boundary = _CrashAfterReadyBoundary(store, initial["session_id"], tmp_path)
    child_calls = []
    registry = create_registry(
        state, workspace_root=tmp_path,
        subagent_llm=lambda *_args, **_kwargs: child_calls.append(1) or _report(),
    )
    parent, parent_calls = _parent_llm(_arguments())
    runtime = AgentRuntime(
        llm_client=parent, context=context, executor=ToolExecutor(registry),
        policy=ParentRuntimePolicy(), max_rounds=3, session_boundary=boundary,
    )
    with pytest.raises(RuntimeError, match="after result_ready"):
        runtime.run()
    source = store.load(initial["session_id"])
    ready = source["tool_boundary"]["pending_delegation_results"][0]
    resumed = prepare_resume(store, initial["session_id"], tmp_path).claim()
    assert resumed.context.history[-1]["content"] == ready["result_json"]
    assert len(child_calls) == 1 and parent_calls[0] == 1
    assert store.load(initial["session_id"])["integrity"] == source["integrity"]


def test_three_ready_results_survive_half_round_commit_and_session_claim(tmp_path: Path):
    last_ready = Event()
    state = AgentState()
    state.begin_task("half-round delegation")
    context = ContextManager(state, [])
    store = SessionStore(tmp_path / "sessions")
    initial = store.save(None, state, context, workspace_root=tmp_path)
    boundary = _CrashOnSecondCommitBoundary(
        store, initial["session_id"], tmp_path, last_ready,
    )
    child_calls = []

    def child(messages, **_options):
        request = next(json.loads(item["content"]) for item in messages
                       if item.get("role") == "user")
        goal = request["contract"]["goal"]
        child_calls.append(goal)
        if goal == "A":
            assert last_ready.wait(5), "later results were not durably ready"
        return _report(goal)

    registry = create_registry(state, workspace_root=tmp_path, subagent_llm=child)
    parent, parent_calls = _batch_parent_llm(["A", "B", "C"])
    runtime = AgentRuntime(
        llm_client=parent, context=context, executor=ToolExecutor(registry),
        policy=ParentRuntimePolicy(), max_rounds=3, session_boundary=boundary,
    )
    with pytest.raises(RuntimeError, match="half-round"):
        runtime.run()
    source = store.load(initial["session_id"])
    calls = source["tool_boundary"]["calls"]
    pending = source["tool_boundary"]["pending_delegation_results"]
    assert [call["status"] for call in calls] == ["committed", "pending", "pending"]
    assert [item["invocation_id"] for item in pending] == ["r-1-c-1", "r-1-c-2"]
    first_content = calls[0]["result"]["content"]
    ready_contents = [item["result_json"] for item in pending]

    resumed = prepare_resume(store, initial["session_id"], tmp_path).claim()
    derived = store.load(resumed.session_id)
    assert resumed.session_id != initial["session_id"]
    assert [item["status"] for item in derived["tool_boundary"]["calls"]] == ["committed"] * 3
    assert "pending_delegation_results" not in derived["tool_boundary"]
    assert [item["result"]["content"] for item in derived["tool_boundary"]["calls"]] == [
        first_content, *ready_contents,
    ]
    assert [item["tool_call_id"] for item in derived["tool_boundary"]["calls"]] == [
        "delegate-call-0", "delegate-call-1", "delegate-call-2",
    ]
    assert [item["content"] for item in resumed.context.history if item["role"] == "tool"] == [
        first_content, *ready_contents,
    ]
    assert all(record.delivery_status == "committed" for record in resumed.state.delegation_records)
    assert len(child_calls) == 3 and parent_calls[0] == 1
    assert store.load(initial["session_id"])["integrity"] == source["integrity"]
    with pytest.raises(SessionValidationError, match="已经派生"):
        prepare_resume(store, initial["session_id"], tmp_path).claim()


def test_failed_derived_session_write_retries_same_claim_without_child_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    state = AgentState()
    state.begin_task("claim write interruption")
    context = ContextManager(state, [])
    store = SessionStore(tmp_path / "sessions")
    initial = store.save(None, state, context, workspace_root=tmp_path)
    boundary = _CrashOnCommitBoundary(store, initial["session_id"], tmp_path)
    child_calls = []
    registry = create_registry(
        state, workspace_root=tmp_path,
        subagent_llm=lambda *_args, **_kwargs: child_calls.append(1) or _report(),
    )
    parent, parent_calls = _parent_llm(_arguments())
    runtime = AgentRuntime(
        llm_client=parent, context=context, executor=ToolExecutor(registry),
        policy=ParentRuntimePolicy(), max_rounds=3, session_boundary=boundary,
    )
    with pytest.raises(RuntimeError, match="parent delivery crash"):
        runtime.run()
    source = store.load(initial["session_id"])
    original_write = store._write_atomic

    def fail_derived_write(session_id, envelope):
        if session_id != initial["session_id"]:
            raise OSError("injected derived write failure")
        return original_write(session_id, envelope)

    monkeypatch.setattr(store, "_write_atomic", fail_derived_write)
    with pytest.raises(OSError, match="derived write failure"):
        prepare_resume(store, initial["session_id"], tmp_path).claim()
    assert store.load(initial["session_id"])["integrity"] == source["integrity"]
    preparing = store._read_crash_claims()
    assert len(preparing) == 1 and preparing[0]["status"] == "preparing"
    derived_id = preparing[0]["derived_session_id"]

    monkeypatch.setattr(store, "_write_atomic", original_write)
    resumed = prepare_resume(store, initial["session_id"], tmp_path).claim()
    assert resumed.session_id == derived_id
    assert resumed.context.history[-1]["content"] == source["tool_boundary"][
        "pending_delegation_results"
    ][0]["result_json"]
    assert len(child_calls) == 1 and parent_calls[0] == 1
    assert store._read_crash_claims()[0]["status"] == "committed"


def test_running_crash_preserves_uncertainty_without_synthetic_child_result(tmp_path: Path):
    state = AgentState()
    state.begin_task("lost investigation")
    context = ContextManager(state, [])
    store = SessionStore(tmp_path / "sessions")
    initial = store.save(None, state, context, workspace_root=tmp_path)
    boundary = _CrashAfterBatchBoundary(store, initial["session_id"], tmp_path)
    child_calls = []
    registry = create_registry(
        state, workspace_root=tmp_path,
        subagent_llm=lambda *_args, **_kwargs: child_calls.append(1) or _report(),
    )
    parent, _ = _parent_llm(_arguments())
    runtime = AgentRuntime(
        llm_client=parent, context=context, executor=ToolExecutor(registry),
        policy=ParentRuntimePolicy(), max_rounds=3, session_boundary=boundary,
    )
    with pytest.raises(RuntimeError, match="after delegation start"):
        runtime.run()
    assert not child_calls

    resumed = prepare_resume(store, initial["session_id"], tmp_path).claim()
    record = resumed.state.delegation_records[0]
    assert record.delivery_status == "interrupted"
    assert record.result_id is None and record.result_hash is None
    assert record.usage.llm_calls == 0
    assert resumed.context.history[-1]["role"] == "tool"
    assert json.loads(resumed.context.history[-1]["content"])["status"] == "uncertain"
    assert resumed.state.delegation_budget.used_llm_calls == record.reserved_usage.llm_calls
    derived = store.load(resumed.session_id)
    assert derived["tool_boundary"]["calls"][0]["result"]["outcome"] == "uncertain"
    trace = build_trace(resumed.state.snapshot())
    assert any(edge["type"] == "parent_delegation" and edge["resolved"]
               for edge in trace["causal_edges"])
    assert "interrupted" in render_trace(trace)


def test_pending_result_hash_and_call_identity_are_validated(tmp_path: Path):
    state = AgentState()
    state.begin_task("tamper durable delegation")
    context = ContextManager(state, [])
    store = SessionStore(tmp_path / "sessions")
    envelope = store.save(None, state, context, workspace_root=tmp_path)
    boundary = DurableToolBoundary(store, envelope["session_id"], tmp_path)
    assistant = {
        "role": "assistant", "content": None, "tool_calls": [{
            "id": "call-1", "type": "function", "function": {
                "name": "delegate_task", "arguments": json.dumps(_arguments()),
            },
        }],
    }
    context.history.append(assistant)
    call = {
        "invocation_id": "r-1-c-0", "tool_call_id": "call-1",
        "tool": "delegate_task", "arguments": _arguments(), "effect_class": "none",
    }
    boundary.start_round(1, assistant, [call], state, context)
    manager = DelegationManager(tmp_path, subagent_llm=lambda *_a, **_k: _report(), parent_state=state)
    task = manager.create_task(_arguments(), state)
    state.reserve_delegation(task)
    state.start_delegation(task.delegation_id)
    boundary.persist_delegation_batch({0: task}, state, context)
    result = manager._rejected_result(task, "failed", "test")
    state.delegation_result_ready(task.delegation_id, result)
    boundary.record_delegation_result_ready(call["invocation_id"], result, state, context)
    raw = store.load(envelope["session_id"])
    raw["tool_boundary"]["pending_delegation_results"][0]["result_hash"] = "0" * 64
    with pytest.raises(SessionValidationError, match="完整性|SHA-256|hash"):
        # The public loader must reject tampering before a resume candidate is built.
        path = store.path_for(envelope["session_id"])
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        store.load(envelope["session_id"])


def test_running_delegation_becomes_an_interruption_fact_without_result():
    state = AgentState()
    state.begin_task("interrupted delegation")
    manager = DelegationManager(".", parent_state=state)
    task = manager.create_task(_arguments(), state)
    state.reserve_delegation(task)
    state.start_delegation(task.delegation_id)

    state.reconcile_pending_delegation_boundary([{
        "invocation_id": "r-1-c-0",
        "tool": "delegate_task",
        "status": "pending",
        "delegation_id": task.delegation_id,
    }])

    record = state.delegation_records[0]
    assert record.delivery_status == "interrupted"
    assert record.outcome == "failed"
    assert record.result_id is None
    assert record.result_hash is None
    assert record.usage.llm_calls == 0
    assert "调查运行中丢失" in (record.diagnostic_reason or "")
    assert state.delegation_budget_snapshot()["reserved_subagents"] == 0
