"""v0.48 process-local background subagent lifecycle coverage."""

from __future__ import annotations

import json
from pathlib import Path
import time
from threading import Event, Lock, Thread

import pytest

from mini_agent.agent import ParentRuntimePolicy
from mini_agent.context import ContextManager
from mini_agent.delegation import DelegationError, DelegationManager, ScopeGate
from mini_agent.permission import ALLOW, PermissionGate, PermissionPolicy
from mini_agent.providers.catalog import ProviderCatalog
from mini_agent.resume import prepare_resume
from mini_agent.runtime import AgentRuntime
from mini_agent.session import DurableToolBoundary, SessionStore, SessionValidationError
from mini_agent.state import AgentState
from mini_agent.trace import build_trace
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
        "agent_profile": "general",
    }


def _report(summary: str = "background result") -> dict:
    return {"role": "assistant", "content": json.dumps({
        "summary": summary, "findings": [], "evidence": [], "limitations": [],
    })}


def _call(name: str, arguments: dict, call_id: str) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _runtime(state: AgentState, context: ContextManager, registry, llm, *, boundary=None):
    gate = PermissionGate(PermissionPolicy({
        "spawn_subagent": ALLOW,
        "get_subagent_status": ALLOW,
        "get_subagent_result": ALLOW,
        "cancel_subagent": ALLOW,
        "delegate_task": ALLOW,
    }))
    return AgentRuntime(
        llm_client=llm,
        context=context,
        executor=ToolExecutor(registry, gate),
        policy=ParentRuntimePolicy(),
        max_rounds=8,
        session_boundary=boundary,
    )


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


def test_parent_keeps_running_and_repeat_claim_returns_same_result(tmp_path: Path):
    state = AgentState()
    state.begin_task("continue while the investigation runs")
    context = ContextManager(state, [{"role": "user", "content": "investigate"}])
    child_started, release_child, child_finished = Event(), Event(), Event()

    def child(_messages, **_options):
        child_started.set()
        assert release_child.wait(2)
        child_finished.set()
        return _report("done once")

    parent_calls = []
    child_id = None

    def parent(_messages, **_options):
        nonlocal child_id
        parent_calls.append(True)
        if len(parent_calls) == 1:
            return {"role": "assistant", "content": None, "tool_calls": [
                _call("spawn_subagent", _arguments(), "spawn-1"),
            ]}
        if len(parent_calls) == 2:
            assert child_started.wait(2)
            assert not child_finished.is_set(), "父 Agent 应在子代理仍运行时进入下一模型轮次"
            spawn_result = next(json.loads(item["content"]) for item in context.history
                                if item.get("role") == "tool")
            child_id = spawn_result["child_session_id"]
            return {"role": "assistant", "content": None, "tool_calls": [
                _call("get_subagent_status", {"child_session_id": child_id}, "status-1"),
            ]}
        status_result = next(json.loads(item["content"]) for item in reversed(context.history)
                             if item.get("role") == "tool")
        assert status_result["status"] == "running"
        assert not child_finished.is_set()
        release_child.set()
        return {"role": "assistant", "content": "等待领取调查结果"}

    registry = create_registry(state, workspace_root=tmp_path, subagent_llm=child)
    runtime = _runtime(state, context, registry, parent)
    result = runtime.run()

    assert result.stop_reason == "awaiting_subagents"
    assert len(parent_calls) == 3
    assert state.status == "running"
    assert _wait_until(lambda: child_finished.is_set())
    notices = runtime.collect_background_events()
    assert len(notices) == 1
    record = state.background_subagent_records()[0]
    assert record.delivery_status == "result_ready"

    result_calls = []

    def claim_twice(_messages, **_options):
        result_calls.append(True)
        if len(result_calls) <= 2:
            return {"role": "assistant", "content": None, "tool_calls": [
                _call("get_subagent_result", {"child_session_id": record.subagent_id},
                      f"claim-{len(result_calls)}"),
            ]}
        return {"role": "assistant", "content": "已收到同一结果"}

    claimed = _runtime(state, context, registry, claim_twice).run()
    tool_results = [json.loads(item["content"]) for item in context.history
                    if item.get("role") == "tool"
                    and json.loads(item["content"]).get("result_id")]
    assert claimed.stop_reason == "text"
    assert len(tool_results) == 2
    assert tool_results[0]["result_id"] == tool_results[1]["result_id"] == record.result_id
    assert state.background_subagent_records()[0].delivery_status == "committed"
    assert state.delegation_budget.used_llm_calls == 1
    assert build_trace(state.snapshot())["integrity"]["status"] == "complete"


def test_two_spawns_start_after_the_ordered_round_and_keep_parent_order(tmp_path: Path):
    state = AgentState()
    state.begin_task("start two background reviews")
    context = ContextManager(state, [{"role": "user", "content": "inspect both"}])
    starts: list[str] = []
    finishes: list[str] = []
    b_finished = Event()
    lock = Lock()
    parent_calls = []

    def child(messages, **_options):
        request = next(json.loads(item["content"]) for item in messages if item.get("role") == "user")
        with lock:
            starts.append(request["contract"]["goal"])
        if request["contract"]["goal"] == "A":
            assert b_finished.wait(2)
        else:
            b_finished.set()
        with lock:
            finishes.append(request["contract"]["goal"])
        return _report(request["contract"]["goal"])

    def parent(_messages, **_options):
        parent_calls.append(True)
        if len(parent_calls) == 1:
            return {"role": "assistant", "content": None, "tool_calls": [
                _call("spawn_subagent", _arguments("A"), "spawn-A"),
                _call("spawn_subagent", _arguments("B"), "spawn-B"),
            ]}
        return {"role": "assistant", "content": "等待领取"}

    registry = create_registry(state, workspace_root=tmp_path, subagent_llm=child)
    runtime = _runtime(state, context, registry, parent)
    assert runtime.run().stop_reason == "awaiting_subagents"
    manager = registry._delegation_manager
    assert manager.wait(2)
    runtime.collect_background_events()

    tool_messages = [item for item in context.history if item.get("role") == "tool"]
    assert [item["tool_call_id"] for item in tool_messages] == ["spawn-A", "spawn-B"]
    confirmations = [json.loads(item["content"]) for item in tool_messages]
    assert all(item["accepted"] and item["status"] == "accepted" for item in confirmations)
    assert set(starts) == {"A", "B"}
    assert finishes == ["B", "A"]
    assert [item.subagent_id for item in state.delegation_records] == [
        item["child_session_id"] for item in confirmations
    ]


def test_background_queue_obeys_shared_concurrency_and_aggregate_budget(tmp_path: Path):
    state = AgentState()
    state.begin_task("bound background work")
    state.configure_delegation_budget(max_subagents=3, max_concurrency=1)
    started: list[str] = []
    active = 0
    peak = 0
    lock = Lock()
    release = Event()

    def child(messages, **_options):
        nonlocal active, peak
        request = next(json.loads(item["content"]) for item in messages if item.get("role") == "user")
        goal = request["contract"]["goal"]
        with lock:
            active += 1
            peak = max(peak, active)
            started.append(goal)
        assert release.wait(2)
        with lock:
            active -= 1
        return _report(goal)

    manager = DelegationManager(tmp_path, subagent_llm=child, parent_state=state)
    confirmations = [json.loads(manager.spawn_background(_arguments(str(i)), state))
                     for i in range(3)]
    assert all(item["accepted"] for item in confirmations)
    assert manager.background_status(confirmations[0]["child_session_id"])["status"] == "queued"
    rejected = json.loads(manager.spawn_background(_arguments("over budget"), state))
    assert rejected["accepted"] is False

    for item in confirmations:
        manager.confirm_background_startup(item["child_session_id"])
    manager.commit_background_spawn_round()
    manager.activate_background_tasks()
    assert _wait_until(lambda: len(started) == 1)
    time.sleep(0.03)
    assert peak == 1
    release.set()
    def collect_and_check():
        manager.collect_background_events()
        return len(started) == 3

    assert _wait_until(collect_and_check, timeout=3)
    assert manager.wait(3)
    manager.collect_background_events()
    assert peak == 1
    assert state.delegation_budget.created_subagents == 3
    assert state.delegation_budget.reserved_llm_calls == 0


def test_cancel_queued_child_yields_one_claimable_cancelled_result(tmp_path: Path):
    state = AgentState()
    state.begin_task("cancel queued investigation")
    manager = DelegationManager(tmp_path, subagent_llm=lambda *_a, **_k: _report(), parent_state=state)
    confirmation = json.loads(manager.spawn_background(_arguments(), state))
    child_id = confirmation["child_session_id"]
    manager.confirm_background_startup(child_id)
    manager.commit_background_spawn_round()

    cancelled = manager.cancel_background(child_id, "user requested")
    result_text = manager.background_result(child_id)
    result = json.loads(result_text)

    assert cancelled["status"] == "cancelled"
    assert result["outcome"] == "cancelled"
    assert manager.background_result(child_id) == result_text
    state.commit_delegation_tool_result(json.dumps(result, ensure_ascii=False))
    manager.mark_background_claimed(child_id, result["result_id"])
    assert manager.background_status(child_id)["status"] == "claimed"


def test_durable_result_claim_checks_identity_and_blocks_safe_points(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(tmp_path / "sessions")
    state = AgentState()
    state.begin_task("claim durable result")
    context = ContextManager(state, [{"role": "user", "content": "investigate"}], observability=False)
    initial = store.save(None, state, context, workspace_root=workspace)
    boundary = DurableToolBoundary(store, initial["session_id"], workspace)
    parent_calls = []

    def parent(_messages, **_options):
        parent_calls.append(True)
        if len(parent_calls) == 1:
            return {"role": "assistant", "content": None, "tool_calls": [
                _call("spawn_subagent", _arguments(), "spawn-1"),
            ]}
        return {"role": "assistant", "content": "等待领取"}

    registry = create_registry(state, workspace_root=workspace,
                               subagent_llm=lambda *_a, **_k: _report("durable result"))
    first_runtime = _runtime(state, context, registry, parent, boundary=boundary)
    assert first_runtime.run().stop_reason == "awaiting_subagents"
    manager = registry._delegation_manager
    assert manager.wait(2)
    first_runtime.collect_background_events()
    child_id = state.background_subagent_records()[0].subagent_id

    with pytest.raises(SessionValidationError, match="待提交委派"):
        store.save(initial["session_id"], state, context, workspace_root=workspace,
                   handoff_status="clean", save_kind="safe_point")

    claim_boundary = DurableToolBoundary(store, initial["session_id"], workspace)
    claim_calls = []

    def claim_parent(_messages, **_options):
        claim_calls.append(True)
        if len(claim_calls) == 1:
            return {"role": "assistant", "content": None, "tool_calls": [
                _call("get_subagent_result", {"child_session_id": child_id}, "claim-1"),
            ]}
        return {"role": "assistant", "content": "结果已领取"}

    assert _runtime(state, context, registry, claim_parent,
                    boundary=claim_boundary).run().stop_reason == "text"
    saved = store.load(initial["session_id"])
    claim_call = next(item for item in saved["tool_boundary"]["calls"]
                      if item["tool"] == "get_subagent_result")
    record = next(item for item in saved["state"]["delegation_records"]
                  if item["subagent_id"] == child_id)
    assert claim_call["child_session_id"] == child_id
    assert claim_call["delegation_id"] == record["delegation_id"]
    assert claim_call["delegation_result_hash"] == record["result_hash"]
    assert record["delivery_status"] == "committed"


def test_clean_boundary_abandons_an_unclaimed_result_after_worker_settles(tmp_path: Path):
    state = AgentState()
    state.begin_task("abandon unclaimed result")
    manager = DelegationManager(tmp_path, subagent_llm=lambda *_a, **_k: _report(), parent_state=state)
    confirmation = json.loads(manager.spawn_background(_arguments(), state))
    child_id = confirmation["child_session_id"]
    manager.confirm_background_startup(child_id)
    manager.commit_background_spawn_round()
    manager.activate_background_tasks()
    assert manager.wait(2)
    manager.collect_background_events()
    assert state.has_unclaimed_background_subagents()

    report = manager.cleanup_background(state.task_id, timeout=1)

    assert report["complete"]
    assert not state.has_active_delegations()
    assert state.background_subagent_records()[0].delivery_status == "abandoned"
    assert state.background_subagent_records()[0].result_id is not None


@pytest.mark.parametrize(
    ("error", "expected"),
    [(TimeoutError("slow"), "timed_out"), (RuntimeError("broken"), "failed")],
)
def test_background_timeout_and_exception_become_bounded_results(tmp_path: Path, error, expected):
    state = AgentState()
    state.begin_task("bound child failure")

    def child(*_args, **_kwargs):
        raise error

    manager = DelegationManager(tmp_path, subagent_llm=child, parent_state=state)
    confirmation = json.loads(manager.spawn_background(_arguments(), state))
    child_id = confirmation["child_session_id"]
    manager.confirm_background_startup(child_id)
    manager.commit_background_spawn_round()
    manager.activate_background_tasks()
    assert manager.wait(2)
    notices = manager.collect_background_events()
    result = json.loads(manager.background_result(child_id))

    assert notices and result["outcome"] == expected
    assert len(result["error_detail"]) <= 500
    assert state.background_subagent_records()[0].delivery_status == "result_ready"


def test_worker_start_failure_and_parent_collection_failure_are_retryable(tmp_path: Path, monkeypatch):
    state = AgentState()
    state.begin_task("settle a failed worker start")
    child_calls = []
    manager = DelegationManager(
        tmp_path,
        subagent_llm=lambda *_a, **_k: child_calls.append(True) or _report(),
        parent_state=state,
    )
    confirmation = json.loads(manager.spawn_background(_arguments(), state))
    child_id = confirmation["child_session_id"]
    manager.confirm_background_startup(child_id)
    manager.commit_background_spawn_round()

    original_start = Thread.start

    def fail_worker_start(thread):
        if thread.name.startswith("mini-agent-subagent-"):
            raise RuntimeError("injected worker start failure")
        return original_start(thread)

    monkeypatch.setattr("mini_agent.delegation.Thread.start", fail_worker_start)
    manager.activate_background_tasks()

    fail_once = [True]
    original_ready = state.delegation_result_ready

    def fail_parent_settlement(*args, **kwargs):
        if fail_once[0]:
            fail_once[0] = False
            raise RuntimeError("injected result collection failure")
        return original_ready(*args, **kwargs)

    monkeypatch.setattr(state, "delegation_result_ready", fail_parent_settlement)
    with pytest.raises(RuntimeError, match="collection failure"):
        manager.collect_background_events()
    monkeypatch.setattr(state, "delegation_result_ready", original_ready)
    notices = manager.collect_background_events()
    result = json.loads(manager.background_result(child_id))

    assert notices and result["outcome"] == "failed"
    assert result["error_kind"] == "runner_error"
    assert child_calls == []
    assert state.background_subagent_records()[0].delivery_status == "result_ready"


def test_durable_spawn_round_failure_never_starts_worker(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(tmp_path / "sessions")
    state = AgentState()
    state.begin_task("persist startup before launching")
    context = ContextManager(state, [{"role": "user", "content": "start"}], observability=False)
    initial = store.save(None, state, context, workspace_root=workspace)

    class FailingRoundBoundary(DurableToolBoundary):
        def complete_round(self, *_args, **_kwargs):
            raise RuntimeError("injected round commit failure")

    boundary = FailingRoundBoundary(store, initial["session_id"], workspace)
    child_calls = []

    def parent(_messages, **_options):
        if not child_calls:
            return {"role": "assistant", "content": None, "tool_calls": [
                _call("spawn_subagent", _arguments(), "spawn-1"),
            ]}
        return {"role": "assistant", "content": "unreachable"}

    registry = create_registry(
        state, workspace_root=workspace,
        subagent_llm=lambda *_a, **_k: child_calls.append(True) or _report(),
    )
    manager = registry._delegation_manager
    with pytest.raises(RuntimeError, match="round commit failure"):
        _runtime(state, context, registry, parent, boundary=boundary).run()
    assert child_calls == []
    source = store.load(initial["session_id"])
    assert source["tool_boundary"]["status"] == "pending"
    assert source["state"]["delegation_records"][0]["startup_confirmed"] is False
    assert manager.cleanup_background(state.task_id, timeout=1)["complete"]
    assert state.delegation_records[0].delivery_status == "interrupted"
    assert state.delegation_records[0].result_id is None


def test_committed_handoff_recovers_as_interrupted_without_restarting_worker(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(tmp_path / "sessions")
    state = AgentState()
    state.begin_task("recover interrupted background work")
    context = ContextManager(state, [{"role": "user", "content": "start"}], observability=False)
    initial = store.save(None, state, context, workspace_root=workspace)
    boundary = DurableToolBoundary(store, initial["session_id"], workspace)
    child_started, release_child = Event(), Event()
    child_calls = []

    def child(*_args, **_kwargs):
        child_calls.append(True)
        child_started.set()
        assert release_child.wait(2)
        return _report()

    parent_calls = []

    def parent(_messages, **_options):
        parent_calls.append(True)
        if len(parent_calls) == 1:
            return {"role": "assistant", "content": None, "tool_calls": [
                _call("spawn_subagent", _arguments(), "spawn-1"),
            ]}
        return {"role": "assistant", "content": "等待后台调查"}

    registry = create_registry(state, workspace_root=workspace, subagent_llm=child)
    result = _runtime(state, context, registry, parent, boundary=boundary).run()
    manager = registry._delegation_manager
    assert result.stop_reason == "awaiting_subagents"
    assert child_started.wait(2)
    saved = store.load(initial["session_id"])
    assert saved["tool_boundary"]["status"] == "committed"
    assert saved["state"]["delegation_records"][0]["delivery_status"] == "created"

    catalog = ProviderCatalog.from_settings(
        {"test": {"protocol": "openai_chat", "endpoint": "https://example.test/v1/chat/completions",
                  "api_key": "test-only"}},
        {"default": {"provider_id": "test", "model_id": "model", "context_window": 4096,
                     "max_output_tokens": 256}},
        parent_profile="default",
    )
    candidate = prepare_resume(store, initial["session_id"], workspace, catalog)
    resumed = candidate.claim()
    record = resumed.state.background_subagent_records()[0]
    assert record.delivery_status == "interrupted"
    assert record.result_id is None
    assert resumed.state.delegation_budget.used_llm_calls == 8
    assert any(issue.classification == "uncertain_state_or_result"
               for issue in resumed.state.crash_issues)
    assert len(child_calls) == 1

    release_child.set()
    assert manager.wait(2)
    manager.collect_background_events()
    assert len(child_calls) == 1


def test_sync_delegate_task_keeps_its_existing_single_result_flow(tmp_path: Path):
    state = AgentState()
    state.begin_task("keep synchronous delegation")
    context = ContextManager(state, [{"role": "user", "content": "inspect"}])
    calls = []

    def parent(_messages, **_options):
        calls.append(True)
        if len(calls) == 1:
            args = _arguments()
            args.pop("agent_profile")
            return {"role": "assistant", "content": None, "tool_calls": [
                _call("delegate_task", args, "sync-1"),
            ]}
        return {"role": "assistant", "content": "synchronous child delivered"}

    registry = create_registry(state, workspace_root=tmp_path,
                               subagent_llm=lambda *_a, **_k: _report("sync"))
    result = _runtime(state, context, registry, parent).run()

    assert result.stop_reason == "text"
    assert len([item for item in context.history if item.get("role") == "tool"]) == 1
    assert state.delegation_records[0].mode == "synchronous"
    assert state.delegation_records[0].delivery_status == "committed"

def test_scope_gate_anchors_reads_to_directory_fds(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    session_root = workspace / "private" / "sessions"
    session_root.mkdir(parents=True)
    (session_root / "session.json").write_text("private", encoding="utf-8")
    gate = ScopeGate(workspace, ["."], session_root=session_root)
    with pytest.raises(DelegationError, match="session 敏感目录"):
        gate.validate_path("private/sessions/session.json")

    source = workspace / "src" / "source.txt"
    outside = tmp_path / "outside.txt"
    source.write_text("safe", encoding="utf-8")
    outside.write_text("private contents", encoding="utf-8")
    canonical = gate.validate_path("src/source.txt")

    original_open = __import__("os").open

    def replace_before_open(path, flags, *args, **kwargs):
        if path == "source.txt" and kwargs.get("dir_fd") is not None:
            source.unlink()
            source.symlink_to(outside)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("mini_agent.delegation.os.open", replace_before_open)
    wrapped = gate.wrap_handler("read_file", lambda path: outside.read_text(encoding="utf-8"))
    with pytest.raises(OSError):
        wrapped(canonical)


def test_scope_gate_fd_readers_preserve_normal_results(tmp_path: Path):
    workspace = tmp_path / "workspace"
    source_dir = workspace / "src"
    source_dir.mkdir(parents=True)
    (source_dir / "notes.txt").write_text("first\nsecond\n", encoding="utf-8")
    (source_dir / "config_local.py").write_text("SECRET", encoding="utf-8")
    gate = ScopeGate(workspace, ["src"])

    read = gate.wrap_handler("read_file", lambda path: "unused")
    listing = gate.wrap_handler("list_dir", lambda path: "unused")
    search = gate.wrap_handler("grep", lambda pattern, path: "unused")
    canonical = gate.validate_path("src/notes.txt")

    assert read(canonical, offset=1, limit=1).startswith("00002|second")
    assert listing(gate.validate_path("src")) == "notes.txt"
    assert "src/notes.txt:2: second" in search("second", gate.validate_path("src"))
    assert "SECRET" not in search("SECRET", gate.validate_path("src"))


def test_failed_clean_handoff_restores_claimable_background_result(tmp_path: Path):
    state = AgentState()
    state.begin_task("keep result after save failure")
    manager = DelegationManager(tmp_path, subagent_llm=lambda *_a, **_k: _report(), parent_state=state)
    confirmation = json.loads(manager.spawn_background(_arguments(), state))
    child_id = confirmation["child_session_id"]
    manager.confirm_background_startup(child_id)
    manager.commit_background_spawn_round()
    manager.activate_background_tasks()
    assert manager.wait(2)
    manager.collect_background_events()
    assert manager.cleanup_background(state.task_id, timeout=1, abandon=False)["complete"]

    record = state.background_subagent_records()[0]
    token = state.stage_background_abandon([record.delegation_id])
    assert state.background_subagent_records()[0].delivery_status == "abandoned"
    state.rollback_background_abandon(token)

    assert state.background_subagent_records()[0].delivery_status == "result_ready"
    assert json.loads(manager.background_result(child_id))["result_id"] == record.result_id
    assert build_trace(state.snapshot())["integrity"]["status"] == "complete"
