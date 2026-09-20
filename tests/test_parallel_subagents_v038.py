"""v0.38 bounded parallel delegation and ordered parent delivery."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
from threading import Event, Lock, Thread
from types import SimpleNamespace

from mini_agent.agent import ParentRuntimePolicy
from mini_agent.context import ContextManager
from mini_agent.providers.base import ProviderResponse, ProviderUsage, UsageMeter
from mini_agent.providers.catalog import ModelBindingRef
from mini_agent.permission import DENY, PermissionGate, PermissionPolicy
from mini_agent.runtime import AgentRuntime
from mini_agent.session import DurableToolBoundary, SessionStore
from mini_agent.state import AgentState
from mini_agent.tools import create_registry
from mini_agent.tools.base import ToolExecutor


def _arguments(goal: str) -> dict:
    return {
        "goal": goal,
        "scope": ["."],
        "constraints": [],
        "expected_findings": [],
        "requested_tools": ["calculate"],
        "selected_parent_facts": [],
        "purpose": "investigation",
    }


def _report(goal: str) -> dict:
    return {
        "role": "assistant",
        "content": json.dumps({
            "summary": f"finished {goal}",
            "findings": [],
            "evidence": [],
            "limitations": [],
        }),
    }


def _parent_llm(goals: list[str]):
    calls = []

    def parent(messages, **_options):
        calls.append(messages)
        if len(calls) == 1:
            return {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": f"call-{index}", "type": "function",
                    "function": {
                        "name": "delegate_task",
                        "arguments": json.dumps(_arguments(goal)),
                    },
                } for index, goal in enumerate(goals)]
            }
        return {"role": "assistant", "content": "parent finished"}

    return parent, calls


def _run(tmp_path: Path, child_llm, parent_llm, state: AgentState | None = None,
         provider_catalog=None):
    state = state or AgentState()
    state.begin_task("parallel parent")
    registry = create_registry(
        state, workspace_root=tmp_path, subagent_llm=child_llm,
        provider_catalog=provider_catalog,
    )
    context = ContextManager(state, [])
    runtime = AgentRuntime(
        llm_client=parent_llm,
        context=context,
        executor=ToolExecutor(registry),
        policy=ParentRuntimePolicy(),
        max_rounds=4,
    )
    return runtime.run(), context, state


def test_three_children_are_bounded_and_delivered_in_model_order(tmp_path: Path):
    a_started = Event()
    b_started = Event()
    b_finished = Event()
    c_started = Event()
    lock = Lock()
    active = 0
    peak = 0
    starts = []
    finishes = []

    def child(messages, **_options):
        nonlocal active, peak
        contract = next(json.loads(item["content"]) for item in messages
                         if item.get("role") == "user")
        goal = contract["contract"]["goal"]
        with lock:
            active += 1
            peak = max(peak, active)
            starts.append(goal)
        if goal == "A":
            a_started.set()
            assert c_started.wait(2)
        elif goal == "B":
            b_started.set()
        elif goal == "C":
            assert b_finished.is_set()
            c_started.set()
        with lock:
            finishes.append(goal)
            active -= 1
        if goal == "B":
            b_finished.set()
        return _report(goal)

    parent, parent_calls = _parent_llm(["A", "B", "C"])
    result, context, state = _run(tmp_path, child, parent)

    assert result.stop_reason == "text"
    assert peak == 2
    assert set(starts[:2]) == {"A", "B"}
    assert starts[2] == "C"
    assert finishes.index("B") < finishes.index("C") < finishes.index("A")
    assert len(parent_calls) == 2
    assert [item["role"] for item in context.history] == [
        "assistant", "tool", "tool", "tool", "assistant",
    ]
    assert [item["tool_call_id"] for item in context.history[1:4]] == [
        "call-0", "call-1", "call-2",
    ]
    assert len(state.attempts) == 3
    assert all(item.delivery_status == "committed" for item in state.delegation_records)


def test_failure_timeout_and_success_each_deliver_once(tmp_path: Path):
    calls = []

    def child(messages, **_options):
        contract = next(json.loads(item["content"]) for item in messages
                         if item.get("role") == "user")
        goal = contract["contract"]["goal"]
        calls.append(goal)
        if goal == "timeout":
            raise TimeoutError("simulated timeout")
        if goal == "failure":
            raise RuntimeError("simulated failure")
        return _report(goal)

    parent, _ = _parent_llm(["failure", "timeout", "success"])
    result, context, state = _run(tmp_path, child, parent)

    assert result.stop_reason == "text"
    payloads = [json.loads(item["content"]) for item in context.history if item["role"] == "tool"]
    assert len(payloads) == 3
    assert {item["outcome"] for item in payloads} == {"completed", "failed", "timed_out"}
    assert len(calls) == 3
    assert all(item.delivery_status == "committed" for item in state.delegation_records)
    usage = {record.outcome: record.usage.llm_calls for record in state.delegation_records}
    assert usage == {"completed": 1, "failed": 1, "timed_out": 1}


def test_batch_budget_refusal_happens_before_child_llm(tmp_path: Path):
    child_calls = []

    def child(messages, **_options):
        child_calls.append(messages)
        return _report("success")

    parent, _ = _parent_llm(["A", "B", "C"])
    state = AgentState()
    state.configure_delegation_budget(
        max_subagents=3, max_concurrency=2,
        max_total_llm_calls=16, max_total_tool_calls=48,
        max_total_tokens=64_000,
    )
    result, context, state = _run(tmp_path, child, parent, state)

    assert result.stop_reason == "text"
    assert len(child_calls) == 2
    payloads = [json.loads(item["content"]) for item in context.history if item["role"] == "tool"]
    assert len(payloads) == 3
    assert sum(item.get("error_kind") == "aggregate_budget" for item in payloads) == 1
    assert state.delegation_budget.used_llm_calls == 2
    assert state.delegation_budget.reserved_llm_calls == 0


def test_duplicate_contract_is_rejected_before_llm(tmp_path: Path):
    child_calls = []

    def child(messages, **_options):
        child_calls.append(messages)
        return _report("success")

    parent, _ = _parent_llm(["A", "A", "B"])
    state = AgentState()
    state.configure_delegation_budget(max_subagents=3, max_concurrency=2)
    result, context, state = _run(tmp_path, child, parent, state)

    assert result.stop_reason == "text"
    assert len(child_calls) == 2
    payloads = [json.loads(item["content"]) for item in context.history if item["role"] == "tool"]
    assert sum(item.get("error_kind") == "aggregate_budget" for item in payloads) == 1
    assert state.delegation_budget.created_subagents == 2
    assert state.delegation_budget.reserved_llm_calls == 0


def test_max_subagents_is_checked_per_batch_before_llm(tmp_path: Path):
    child_calls = []

    def child(messages, **_options):
        child_calls.append(messages)
        return _report("success")

    parent, _ = _parent_llm(["A", "B", "C"])
    state = AgentState()
    state.configure_delegation_budget(max_subagents=2, max_concurrency=2)
    result, context, state = _run(tmp_path, child, parent, state)

    assert result.stop_reason == "text"
    assert len(child_calls) == 2
    payloads = [json.loads(item["content"]) for item in context.history if item["role"] == "tool"]
    assert sum(item.get("error_kind") == "aggregate_budget" for item in payloads) == 1
    assert state.delegation_budget.created_subagents == 2


def test_mixed_delegate_round_rejects_every_call(tmp_path: Path):
    parent_calls = []

    def parent(messages, **_options):
        parent_calls.append(messages)
        if len(parent_calls) == 1:
            return {
                "role": "assistant", "content": None,
                "tool_calls": [
                    {
                        "id": "delegate", "type": "function",
                        "function": {"name": "delegate_task", "arguments": json.dumps(_arguments("A"))},
                    },
                    {
                        "id": "calc", "type": "function",
                        "function": {"name": "calculate", "arguments": json.dumps({"expression": "1+1"})},
                    },
                ],
            }
        return {"role": "assistant", "content": "done"}

    child_calls = []
    result, context, _ = _run(
        tmp_path,
        lambda *_args, **_kwargs: child_calls.append(True) or _report("A"),
        parent,
    )
    payloads = [json.loads(item["content"]) for item in context.history if item["role"] == "tool"]
    assert result.stop_reason == "text"
    assert child_calls == []
    assert len(payloads) == 2
    assert all(item["error_kind"] == "delegation_batch_gate" for item in payloads)


def test_permission_rejection_never_starts_batch_children(tmp_path: Path):
    child_calls = []
    parent, parent_calls = _parent_llm(["A", "B"])
    state = AgentState()
    state.begin_task("permission batch")
    registry = create_registry(
        state, workspace_root=tmp_path,
        subagent_llm=lambda *_args, **_kwargs: child_calls.append(True) or _report("A"),
    )
    runtime = AgentRuntime(
        llm_client=parent,
        context=ContextManager(state, []),
        executor=ToolExecutor(
            registry, PermissionGate(PermissionPolicy({"delegate_task": DENY})),
        ),
        policy=ParentRuntimePolicy(),
        max_rounds=4,
    )

    result = runtime.run()
    assert result.stop_reason == "failed"
    assert len(parent_calls) == 1
    assert child_calls == []
    assert all(
        item.error_kind == "permission_denied"
        for item in runtime.executions
    )


def test_parent_cancellation_broadcasts_to_all_children(tmp_path: Path):
    entered = Event()
    release = Event()
    entered_count = 0
    lock = Lock()

    def child(messages, **_options):
        nonlocal entered_count
        with lock:
            entered_count += 1
        entered.set()
        release.wait(2)
        contract = next(json.loads(item["content"]) for item in messages
                         if item.get("role") == "user")
        return _report(contract["contract"]["goal"])

    parent, parent_calls = _parent_llm(["A", "B", "C"])
    state = AgentState()
    state.begin_task("cancel parent")
    registry = create_registry(state, workspace_root=tmp_path, subagent_llm=child)
    context = ContextManager(state, [])
    runtime = AgentRuntime(
        llm_client=parent,
        context=context,
        executor=ToolExecutor(registry),
        policy=ParentRuntimePolicy(),
        max_rounds=4,
    )
    holder = []
    thread = Thread(target=lambda: holder.append(_run_runtime(runtime)))
    thread.start()
    manager = registry._delegation_manager
    assert entered.wait(2)
    manager.cancel(state.task_id, "reset requested")
    release.set()
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert holder and isinstance(holder[0], KeyboardInterrupt)
    assert len(parent_calls) == 1
    assert entered_count <= 2
    assert len([item for item in context.history if item["role"] == "tool"]) == 3


def _run_runtime(runtime):
    try:
        return runtime.run()
    except BaseException as error:
        return error


class _FailingBoundary:
    """Small durable boundary double for admission and result failures."""

    def __init__(self, *, fail_admission_at=None, fail_result_at=None):
        self.fail_admission_at = fail_admission_at
        self.fail_result_at = fail_result_at
        self.admission_count = 0
        self.result_count = 0
        self.completed = False

    def start_round(self, *_args):
        return {}

    def record_admission(self, *_args):
        self.admission_count += 1
        if self.admission_count == self.fail_admission_at:
            raise RuntimeError("admission persistence failed")
        return {}

    def record_execution_result(self, *_args):
        self.result_count += 1
        if self.result_count == self.fail_result_at:
            raise RuntimeError("result persistence failed")
        return {}

    def complete_round(self, *_args):
        self.completed = True
        return {}


def test_durable_admission_failure_stops_before_child_llm(tmp_path: Path):
    child_calls = []
    parent, parent_calls = _parent_llm(["A", "B", "C"])
    state = AgentState()
    state.begin_task("admission failure")
    registry = create_registry(
        state, workspace_root=tmp_path,
        subagent_llm=lambda *_args, **_kwargs: child_calls.append(True) or _report("A"),
    )
    boundary = _FailingBoundary(fail_admission_at=2)
    runtime = AgentRuntime(
        llm_client=parent,
        context=ContextManager(state, []),
        executor=ToolExecutor(registry),
        policy=ParentRuntimePolicy(),
        max_rounds=4,
        session_boundary=boundary,
    )

    error = _run_runtime(runtime)
    assert isinstance(error, RuntimeError)
    assert len(parent_calls) == 1
    assert child_calls == []
    assert boundary.completed is False


def test_durable_result_failure_does_not_start_queued_child(tmp_path: Path):
    b_entered = Event()
    release_b = Event()
    release_a = Event()
    child_calls = []

    def child(messages, **_options):
        contract = next(json.loads(item["content"]) for item in messages
                         if item.get("role") == "user")
        goal = contract["contract"]["goal"]
        child_calls.append(goal)
        if goal == "A":
            assert release_a.wait(2)
        if goal == "B":
            b_entered.set()
            release_b.wait(2)
        return _report(goal)

    parent, parent_calls = _parent_llm(["A", "B", "C"])
    state = AgentState()
    state.begin_task("result failure")
    registry = create_registry(state, workspace_root=tmp_path, subagent_llm=child)
    boundary = _FailingBoundary(fail_result_at=1)
    runtime = AgentRuntime(
        llm_client=parent,
        context=ContextManager(state, []),
        executor=ToolExecutor(registry),
        policy=ParentRuntimePolicy(),
        max_rounds=4,
        session_boundary=boundary,
    )
    holder = []
    thread = Thread(target=lambda: holder.append(_run_runtime(runtime)))
    thread.start()
    assert b_entered.wait(2)
    release_a.set()
    try:
        thread.join(timeout=1)
        assert not thread.is_alive(), "父提交失败不应等待仍在运行的子代理"
        assert registry._delegation_manager.active_info()["subagent_ids"]
    finally:
        release_b.set()
        thread.join(timeout=3)

    assert holder and isinstance(holder[0], RuntimeError)
    assert len(parent_calls) == 1
    assert set(child_calls) <= {"A", "B"}
    assert "C" not in child_calls
    assert boundary.completed is False
    assert registry._delegation_manager.wait(2)
    assert state.has_active_delegations()


def test_budget_rejection_enters_parent_state_at_its_tool_position(tmp_path: Path):
    class InspectBoundary(_FailingBoundary):
        def __init__(self):
            super().__init__()
            self.first_result_statuses = None

        def record_execution_result(self, _invocation, _execution, _content,
                                    state, _context, _attempt):
            if self.first_result_statuses is None:
                self.first_result_statuses = [
                    item.delivery_status for item in state.delegation_records
                ]
            return super().record_execution_result()

    parent, _ = _parent_llm(["A", "B", "C"])
    state = AgentState()
    state.begin_task("ordered rejection")
    state.configure_delegation_budget(
        max_subagents=3, max_concurrency=2,
        max_total_llm_calls=16, max_total_tool_calls=48,
        max_total_tokens=64_000,
    )
    registry = create_registry(
        state, workspace_root=tmp_path,
        subagent_llm=lambda messages, **_: _report(
            next(json.loads(item["content"]) for item in messages
                 if item.get("role") == "user")["contract"]["goal"]
        ),
    )
    boundary = InspectBoundary()
    runtime = AgentRuntime(
        llm_client=parent, context=ContextManager(state, []),
        executor=ToolExecutor(registry), policy=ParentRuntimePolicy(),
        max_rounds=4, session_boundary=boundary,
    )

    assert runtime.run().stop_reason == "text"
    assert boundary.first_result_statuses == ["result_ready", "running"]
    assert [item.delivery_status for item in state.delegation_records] == [
        "committed", "committed", "committed",
    ]


class _FakeCatalog:
    subagent_allowed_profiles = ("child-a", "child-b")

    def __init__(self):
        self.calls = []

    def resolve_child_profile(self, requested):
        profile = requested or "child-a"
        if profile not in self.subagent_allowed_profiles:
            raise ValueError("profile 不在白名单")
        return profile

    def bind(self, profile):
        return _FakeBinding(profile, self.calls)


class _FakeBinding:
    def __init__(self, profile, calls):
        self.profile = SimpleNamespace(
            name=profile, context_window=128_000, max_output_tokens=8_192,
        )
        self.reference = ModelBindingRef(
            profile, f"provider-{profile}", "openai_chat",
            hashlib.sha256(profile.encode()).hexdigest(),
        )
        self.usage_meter = UsageMeter()
        self.calls = calls

    def complete(self, messages, **_options):
        contract = next(json.loads(item["content"]) for item in messages
                         if item.get("role") == "user")
        self.calls.append(self.profile.name)
        return ProviderResponse(
            _report(contract["contract"]["goal"]), "stop",
            ProviderUsage(4, 3, "provider"),
        )


def test_parallel_children_keep_distinct_frozen_profiles(tmp_path: Path):
    catalog = _FakeCatalog()
    parent_calls = []
    profiles = ["child-a", "child-b"]

    def parent(messages, **_options):
        parent_calls.append(messages)
        if len(parent_calls) == 1:
            return {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": f"profile-call-{index}", "type": "function",
                    "function": {
                        "name": "delegate_task",
                        "arguments": json.dumps({**_arguments(profile), "model_profile": profile}),
                    },
                } for index, profile in enumerate(profiles)]
            }
        return {"role": "assistant", "content": "done"}

    result, _context, state = _run(
        tmp_path, None, parent, provider_catalog=catalog,
    )
    assert result.stop_reason == "text"
    assert sorted(catalog.calls) == profiles
    assert all(record.usage.llm_calls == 1 for record in state.delegation_records)
    assert {record.contract_summary["goal"] for record in state.delegation_records} == set(profiles)


def test_durable_parallel_results_commit_in_tool_call_order(tmp_path: Path):
    state = AgentState()
    state.begin_task("durable parallel parent")
    context = ContextManager(
        state, [{"role": "user", "content": "investigate"}], observability=False,
    )
    store = SessionStore(tmp_path / "sessions")
    envelope = store.save(None, state, context, workspace_root=tmp_path)
    boundary = DurableToolBoundary(store, envelope["session_id"], tmp_path)
    parent, _ = _parent_llm(["A", "B", "C"])
    registry = create_registry(state, workspace_root=tmp_path, subagent_llm=lambda messages, **_: _report(
        next(json.loads(item["content"]) for item in messages if item.get("role") == "user")["contract"]["goal"]
    ))
    runtime = AgentRuntime(
        llm_client=parent,
        context=context,
        executor=ToolExecutor(registry),
        policy=ParentRuntimePolicy(),
        max_rounds=4,
        session_boundary=boundary,
    )

    assert runtime.run().stop_reason == "text"
    saved = store.load(envelope["session_id"])
    assert saved["tool_boundary"]["status"] == "committed"
    assert [item["tool_call_id"] for item in saved["tool_boundary"]["calls"]] == [
        "call-0", "call-1", "call-2",
    ]
    assert [item["tool_call_id"] for item in saved["context"]["history"] if item["role"] == "tool"] == [
        "call-0", "call-1", "call-2",
    ]
