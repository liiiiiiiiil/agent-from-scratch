"""v0.49 followup and safe-point child-session coverage."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from mini_agent.agent import ParentRuntimePolicy
from mini_agent.agent_profiles import AgentProfileCatalog
from mini_agent.context import ContextManager
from mini_agent.delegation import DelegationError, DelegationManager, UsageRecord
import mini_agent.delegation as delegation_module
import mini_agent.config as runtime_config
from mini_agent.permission import ALLOW, PermissionGate, PermissionPolicy
from mini_agent.providers.catalog import ProviderCatalog
from mini_agent.providers.base import ProviderUsage
from mini_agent.resume import prepare_resume
from mini_agent.runtime import AgentRuntime
from mini_agent.session import (
    DurableToolBoundary, SessionStore, SessionValidationError,
)
from mini_agent.state import AgentState, DelegationUsage
from mini_agent.skills import SkillCatalog
from mini_agent.tools import create_registry
from mini_agent.tools.base import ToolExecutor, ToolRegistry
from mini_agent.tools.delegation import make_background_subagent_tools


def _call(name: str, arguments: dict, call_id: str) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _report(summary: str) -> dict:
    return {"role": "assistant", "content": json.dumps({
        "summary": summary, "findings": [], "evidence": [], "limitations": [],
    })}


def _contract(goal: str = "inspect another detail", *, child_id: str | None = None,
              budget: dict | None = None) -> dict:
    arguments = {
        "goal": goal,
        "scope": ["."],
        "constraints": [],
        "expected_findings": [],
        "requested_tools": ["calculate"],
        "selected_parent_facts": [],
        "purpose": "investigation",
    }
    if child_id is None:
        arguments["agent_profile"] = "general"
    else:
        arguments["child_session_id"] = child_id
    if budget is not None:
        arguments["budget"] = budget
    return arguments


def _runtime(state: AgentState, context: ContextManager, registry, llm,
             *, boundary=None) -> AgentRuntime:
    gate = PermissionGate(PermissionPolicy({
        name: ALLOW for name in (
            "spawn_subagent", "followup_subagent", "get_subagent_status",
            "get_subagent_result", "cancel_subagent",
        )
    }))
    return AgentRuntime(
        llm_client=llm,
        context=context,
        executor=ToolExecutor(registry, gate),
        policy=ParentRuntimePolicy(),
        max_rounds=8,
        session_boundary=boundary,
    )


def _finish_and_claim(state, context, registry, runtime, child_id: str,
                      *, boundary=None) -> str:
    manager = registry._delegation_manager
    assert manager.wait(2)
    runtime.collect_background_events()
    claim_calls = []
    claim_call_id = f"claim-result-{len(state.delegation_records)}"

    def parent(_messages, **_options):
        claim_calls.append(True)
        if len(claim_calls) == 1:
            return {"role": "assistant", "content": None, "tool_calls": [
                _call("get_subagent_result", {"child_session_id": child_id}, claim_call_id),
            ]}
        return {"role": "assistant", "content": "结果已领取"}

    result = _runtime(state, context, registry, parent, boundary=boundary).run()
    assert result.stop_reason == "text"
    return next(
        item["content"] for item in context.history
        if item.get("role") == "tool" and item.get("tool_call_id") == claim_call_id
    )


def test_followup_tool_reuses_child_history_and_keeps_round_results_distinct(tmp_path: Path):
    state = AgentState()
    state.begin_task("parent-only-secret: 6a812f")
    context = ContextManager(
        state, [{"role": "user", "content": "parent-only-secret: 6a812f"}],
        observability=False,
    )
    child_messages: list[list[dict]] = []

    def child(messages, **_options):
        child_messages.append(deepcopy(messages))
        contracts = [
            json.loads(item["content"])["contract"]["goal"]
            for item in messages
            if item.get("role") == "user" and item.get("content", "").startswith("{")
        ]
        return _report(f"child report for {contracts[-1]}")

    registry = create_registry(state, workspace_root=tmp_path, subagent_llm=child)
    followup_schema = registry.get("followup_subagent").parameters
    assert "agent_profile" not in followup_schema["properties"]
    assert "model_profile" not in followup_schema["properties"]
    assert "agent_profile" not in followup_schema["required"]
    assert "source_id" not in followup_schema["properties"]
    first_calls = []

    def parent_start(_messages, **_options):
        first_calls.append(True)
        if len(first_calls) == 1:
            return {"role": "assistant", "content": None, "tool_calls": [
                _call("spawn_subagent", _contract("first investigation"), "spawn-first"),
            ]}
        return {"role": "assistant", "content": "等待第一轮调查"}

    first_runtime = _runtime(state, context, registry, parent_start)
    assert first_runtime.run().stop_reason == "awaiting_subagents"
    first_child_id = state.child_session_records[0].child_session_id
    first_raw = _finish_and_claim(state, context, registry, first_runtime, first_child_id)
    first_result = json.loads(first_raw)
    first_record = state.delegation_records[0]
    original_result_hash = first_record.result_hash

    budget = {
        "max_rounds": 4, "max_llm_calls": 4, "max_tool_calls": 8,
        "max_tokens": 8_000, "timeout_seconds": 10,
    }
    followup_calls = []

    def parent_followup(_messages, **_options):
        followup_calls.append(True)
        if len(followup_calls) == 1:
            return {"role": "assistant", "content": None, "tool_calls": [
                _call("followup_subagent", _contract(
                    "second investigation", child_id=first_child_id, budget=budget,
                ), "followup-second"),
            ]}
        return {"role": "assistant", "content": "等待第二轮调查"}

    registry._delegation_manager.validate_followup_arguments(
        _contract("second investigation", child_id=first_child_id, budget=budget), state,
    )
    second_runtime = _runtime(state, context, registry, parent_followup)
    second_runtime_result = second_runtime.run()
    assert second_runtime_result.stop_reason == "awaiting_subagents", (
        second_runtime_result.stop_reason, state.status, state.planning_state.phase,
    )
    second_raw = _finish_and_claim(
        state, context, registry, second_runtime, first_child_id,
    )
    second_result = json.loads(second_raw)

    assert len(child_messages) == 2
    prior_child_history = json.dumps(child_messages[1], ensure_ascii=False)
    assert "first investigation" in prior_child_history
    assert "child report for first investigation" in prior_child_history
    assert "second investigation" in prior_child_history
    assert "parent-only-secret: 6a812f" not in prior_child_history
    assert first_result["round_index"] == 1
    assert second_result["round_index"] == 2
    assert first_result["delegation_id"] != second_result["delegation_id"]
    assert first_result["result_id"] != second_result["result_id"]
    assert first_result["usage"]["llm_calls"] == second_result["usage"]["llm_calls"] == 1
    assert state.delegation_records[0].result_hash == original_result_hash
    assert state.child_session_records[0].round_index == 2
    assert state.child_session_records[0].cumulative_usage.llm_calls == 2
    assert state.delegation_budget.created_subagents == 1
    assert registry._delegation_manager.background_result(first_child_id) == second_raw


def test_claimed_child_snapshot_survives_safe_point_resume_and_keeps_cumulative_budget(
    tmp_path: Path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = AgentState()
    state.begin_task("resume an already claimed child session")
    context = ContextManager(state, [{"role": "user", "content": "resume this task"}],
                             observability=False)
    catalog = ProviderCatalog.from_settings(
        {"test": {"protocol": "openai_chat",
                  "endpoint": "https://example.test/v1/chat/completions",
                  "api_key": "test-only"}},
        {"default": {"provider_id": "test", "model_id": "model",
                      "context_window": 4096, "max_output_tokens": 256}},
        parent_profile="default",
    )
    child_messages: list[list[dict]] = []

    def child(messages, **_options):
        child_messages.append(deepcopy(messages))
        return _report(f"saved round {len(child_messages)}")

    registry = create_registry(
        state, workspace_root=workspace, provider_catalog=catalog, subagent_llm=child,
    )
    manager = registry._delegation_manager
    startup = json.loads(manager.spawn_background(_contract("saved first round"), state))
    child_id = startup["child_session_id"]
    manager.confirm_background_startup(child_id)
    manager.commit_background_spawn_round()
    manager.activate_background_tasks()
    assert manager.wait(2)
    manager.collect_background_events()
    first_raw = manager.background_result(child_id, state)
    first_result = json.loads(first_raw)
    state.commit_delegation_tool_result(first_raw)
    manager.mark_background_claimed(child_id, first_result["result_id"])
    context.history.extend([
        {"role": "assistant", "content": None, "tool_calls": [
            _call("get_subagent_result", {"child_session_id": child_id}, "saved-claim"),
        ]},
        {"role": "tool", "tool_call_id": "saved-claim", "content": first_raw},
    ])
    first_hash = state.delegation_records[0].result_hash

    store = SessionStore(tmp_path / "sessions")
    saved = store.save(
        None, state, context, workspace_root=workspace,
        handoff_status="clean", save_kind="safe_point",
        child_sessions=registry._delegation_manager.export_child_sessions(state),
    )
    assert saved["schema_version"] == 4
    assert len(saved["child_sessions"]) == 1

    resumed = prepare_resume(store, saved["session_id"], workspace, catalog).claim()
    resumed_manager = resumed.registry._delegation_manager
    assert resumed.child_session_issues == []
    assert resumed.state.child_session_records[0].status == "idle"
    resumed_manager.subagent_llm = child

    confirmation = json.loads(resumed_manager.followup_background(
        _contract("after restart", child_id=child_id, budget={
            "max_rounds": 4, "max_llm_calls": 4, "max_tool_calls": 8,
            "max_tokens": 8_000, "timeout_seconds": 10,
        }),
        resumed.state,
    ))
    assert confirmation["accepted"] is True
    assert confirmation["round_index"] == 2
    with pytest.raises(DelegationError, match="当前不可续接"):
        resumed_manager.validate_followup_arguments(
            _contract("double followup", child_id=child_id), resumed.state,
        )
    resumed_manager.confirm_background_startup(child_id)
    resumed_manager.commit_background_spawn_round()
    resumed_manager.activate_background_tasks()
    assert resumed_manager.wait(2)
    resumed_manager.collect_background_events()
    second_raw = resumed_manager.background_result(child_id, resumed.state)
    second_result = json.loads(second_raw)
    resumed.state.commit_delegation_tool_result(second_raw)
    resumed_manager.mark_background_claimed(child_id, second_result["result_id"])
    resumed.state.commit_delegation_tool_result(second_raw)
    resumed_manager.mark_background_claimed(child_id, second_result["result_id"])

    assert first_result["round_index"] == 1
    assert second_result["round_index"] == 2
    assert second_result["delegation_id"] != first_result["delegation_id"]
    assert second_result["result_id"] != first_result["result_id"]
    assert second_result["usage"]["llm_calls"] == 1
    lifecycle = resumed.state.child_session_records[0]
    assert lifecycle.cumulative_usage.llm_calls == 2
    assert lifecycle.last_claimed_result_id == second_result["result_id"]
    assert resumed.state.delegation_budget.created_subagents == 1
    assert resumed.state.delegation_records[0].result_hash == first_hash
    assert json.loads(resumed_manager.background_result(child_id))["result_id"] == second_result["result_id"]
    resumed_manager._background.pop(child_id)
    restored_status = resumed_manager.background_status(child_id, resumed.state)
    assert restored_status["round_index"] == 2
    assert restored_status["result_id"] == second_result["result_id"]
    resumed_history = json.dumps(child_messages[-1], ensure_ascii=False)
    assert "saved first round" in resumed_history
    assert "after restart" in resumed_history

    lifecycle = resumed.state.child_session_records[0]
    snapshot = resumed_manager._child_snapshots[child_id]
    inflated = replace(lifecycle.cumulative_usage, llm_calls=14)
    resumed.state.child_session_records[0] = replace(lifecycle, cumulative_usage=inflated)
    resumed_manager._child_snapshots[child_id] = replace(
        snapshot, cumulative_usage=replace(snapshot.cumulative_usage, llm_calls=14),
    )
    with pytest.raises(ValueError, match="累计 LLM 调用预算不足"):
        resumed_manager.validate_followup_arguments(
            _contract("over cumulative budget", child_id=child_id, budget={
                "max_rounds": 4, "max_llm_calls": 4, "max_tool_calls": 8,
                "max_tokens": 8_000, "timeout_seconds": 10,
            }),
            resumed.state,
        )

    resumed.state.child_session_records[0] = replace(
        resumed.state.child_session_records[0], round_index=4,
    )
    resumed_manager._child_snapshots[child_id] = replace(
        resumed_manager._child_snapshots[child_id], round_index=4,
    )
    with pytest.raises(ValueError, match="4 轮上限"):
        resumed_manager.validate_followup_arguments(
            _contract("over round limit", child_id=child_id), resumed.state,
        )


def test_followup_is_rejected_until_completed_result_is_claimed(tmp_path: Path):
    state = AgentState()
    state.begin_task("require a claimed successful result")
    manager = DelegationManager(
        tmp_path, subagent_llm=lambda *_args, **_kwargs: _report("complete"),
        parent_state=state,
    )
    confirmation = json.loads(manager.spawn_background(_contract("first"), state))
    child_id = confirmation["child_session_id"]
    manager.confirm_background_startup(child_id)
    manager.commit_background_spawn_round()
    manager.activate_background_tasks()
    assert manager.wait(2)
    manager.collect_background_events()

    arguments = _contract("follow up", child_id=child_id)
    with pytest.raises(DelegationError, match="已领取成功快照"):
        manager.validate_followup_arguments(arguments, state)

    raw_result = manager.background_result(child_id, state)
    result = json.loads(raw_result)
    state.commit_delegation_tool_result(raw_result)
    manager.mark_background_claimed(child_id, result["result_id"])
    manager.validate_followup_arguments(arguments, state)

    corrupt = manager.export_child_sessions(state)
    assert len(corrupt) == 1
    state_export = state.export_session()
    state_export["child_session_records"][0]["cumulative_usage"]["llm_calls"] += 1
    with pytest.raises(SessionValidationError, match="累计用量与 State lifecycle 不匹配"):
        # The validator is exercised through a normal envelope write; the
        # invalid State/child snapshot pair must be rejected before commit.
        SessionStore(tmp_path / "sessions").save(
            None, state_export,
            ContextManager(state, [], observability=False).export_session(),
            workspace_root=tmp_path, handoff_status="clean", save_kind="safe_point",
            child_sessions=corrupt,
        )


def test_changed_skill_file_marks_only_its_child_snapshot_incompatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "workspace"
    skill_dir = workspace / "skills" / "notes"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\nname: notes\ndescription: Local investigation notes\n---\nOriginal body.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime_config, "AGENT_PROFILES", {
        "reader": {
            "description": "Read files and use an approved note workflow.",
            "prompt": "Inspect the requested files and report observations.",
            "tools": ["read_file", "grep"], "skills": ["notes"],
        },
    })
    state = AgentState()
    state.begin_task("resume with a checked child skill")
    context = ContextManager(state, [{"role": "user", "content": "inspect"}],
                             observability=False)
    catalog = ProviderCatalog.from_settings(
        {"test": {"protocol": "openai_chat", "endpoint": "https://example.test/v1/chat/completions",
                  "api_key": "test-only"}},
        {"default": {"provider_id": "test", "model_id": "model",
                      "context_window": 4096, "max_output_tokens": 256}},
        parent_profile="default",
    )
    manager = DelegationManager(
        workspace, parent_state=state, provider_catalog=catalog,
        agent_profile_catalog=AgentProfileCatalog(runtime_config.AGENT_PROFILES,
                                                  provider_catalog=catalog),
        skill_catalog=SkillCatalog(workspace),
        subagent_llm=lambda *_args, **_kwargs: _report("complete before replacement"),
    )
    manager.parent_permission_gate = PermissionGate(PermissionPolicy({"skill": ALLOW}))
    arguments = _contract("inspect one file")
    arguments["agent_profile"] = "reader"
    arguments["requested_tools"] = ["read_file"]
    confirmation = json.loads(manager.spawn_background(arguments, state))
    child_id = confirmation["child_session_id"]
    manager.confirm_background_startup(child_id)
    manager.commit_background_spawn_round()
    manager.activate_background_tasks()
    assert manager.wait(2)
    manager.collect_background_events()
    raw = manager.background_result(child_id, state)
    result = json.loads(raw)
    state.commit_delegation_tool_result(raw)
    manager.mark_background_claimed(child_id, result["result_id"])

    store = SessionStore(tmp_path / "sessions")
    saved = store.save(
        None, state, context, workspace_root=workspace,
        handoff_status="clean", child_sessions=manager.export_child_sessions(state),
    )
    skill_file.write_text(
        "---\nname: notes\ndescription: Local investigation notes\n---\nReplaced body.\n",
        encoding="utf-8",
    )

    resumed = prepare_resume(store, saved["session_id"], workspace, catalog).claim()
    assert len(resumed.child_session_issues) == 1
    assert resumed.child_session_issues[0]["child_session_id"] == child_id
    assert "Skill Catalog 或文件身份变化" in resumed.child_session_issues[0]["reason"]
    assert resumed.state.child_session_records[0].status == "incompatible"
    assert resumed.registry._delegation_manager.export_child_sessions(resumed.state) == []
    assert json.loads(raw)["summary"] == "complete before replacement"


def test_provider_usage_source_stays_equal_in_state_and_child_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    state = AgentState()
    state.begin_task("retain provider usage")
    catalog = ProviderCatalog.from_settings(
        {"test": {"protocol": "openai_chat", "endpoint": "https://example.test/v1/chat/completions",
                  "api_key": "test-only"}},
        {"default": {"provider_id": "test", "model_id": "model",
                      "context_window": 4096, "max_output_tokens": 256}},
        parent_profile="default",
    )

    def metered_llm(self):
        def reply(_messages, **_options):
            self.model_binding.usage_meter.record(
                ProviderUsage(12, 8, "provider"),
                estimated_input_tokens=12, estimated_output_tokens=8,
            )
            return _report("provider usage preserved")
        return reply

    monkeypatch.setattr(delegation_module.SubagentRunner, "_llm_callable", metered_llm)
    manager = DelegationManager(tmp_path, parent_state=state, provider_catalog=catalog)
    confirmation = json.loads(manager.spawn_background(_contract("metered child"), state))
    child_id = confirmation["child_session_id"]
    manager.confirm_background_startup(child_id)
    manager.commit_background_spawn_round()
    manager.activate_background_tasks()
    assert manager.wait(2)
    manager.collect_background_events()
    raw = manager.background_result(child_id, state)
    result = json.loads(raw)
    assert result["usage"]["token_accounting"] == "provider"
    state.commit_delegation_tool_result(raw)
    manager.mark_background_claimed(child_id, result["result_id"])
    snapshot = manager.export_child_sessions(state)[0]
    assert snapshot["cumulative_usage"]["token_accounting"] == "provider"
    assert state.child_session_records[0].cumulative_usage.token_accounting == "provider"
    saved = SessionStore(tmp_path / "sessions").save(
        None, state, ContextManager(state, [], observability=False),
        workspace_root=tmp_path, handoff_status="clean", save_kind="safe_point",
        child_sessions=[snapshot],
    )
    assert saved["child_sessions"][0]["cumulative_usage"]["token_accounting"] == "provider"
    manager.validate_followup_arguments(_contract("next", child_id=child_id), state)


def test_snapshot_limit_keeps_completed_report_and_actual_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(delegation_module, "CHILD_SESSION_MAX_SNAPSHOT_BYTES", 128)
    state = AgentState()
    state.begin_task("keep usage when snapshot is too large")
    manager = DelegationManager(
        tmp_path, parent_state=state,
        subagent_llm=lambda *_args, **_kwargs: _report("completed investigation"),
    )
    confirmation = json.loads(manager.spawn_background(_contract("inspect"), state))
    child_id = confirmation["child_session_id"]
    manager.confirm_background_startup(child_id)
    manager.commit_background_spawn_round()
    manager.activate_background_tasks()
    assert manager.wait(2)
    manager.collect_background_events()
    raw = manager.background_result(child_id, state)
    result = json.loads(raw)
    assert result["outcome"] == "completed"
    assert result["summary"] == "completed investigation"
    assert result["error_kind"] == "snapshot_unavailable"
    assert result["usage"]["llm_calls"] == 1
    state.commit_delegation_tool_result(raw)
    manager.mark_background_claimed(child_id, result["result_id"])
    assert state.delegation_budget.used_llm_calls == 1
    assert state.child_session_records[0].status == "closed"
    assert manager.export_child_sessions(state) == []
    with pytest.raises(DelegationError, match="已领取成功快照"):
        manager.validate_followup_arguments(_contract("next", child_id=child_id), state)


def test_same_process_followup_rejects_replaced_skill_before_child_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "workspace"
    skill_dir = workspace / "skills" / "notes"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\nname: notes\ndescription: Local investigation notes\n---\nOriginal body.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime_config, "AGENT_PROFILES", {
        "reader": {
            "description": "Read files and use approved notes.",
            "prompt": "Inspect the requested files.",
            "tools": ["read_file"], "skills": ["notes"],
        },
    })
    state = AgentState()
    state.begin_task("check current Skill identity")
    calls = []
    def child(_messages, **_options):
        calls.append(1)
        return _report("done")

    manager = DelegationManager(
        workspace, parent_state=state,
        skill_catalog=SkillCatalog(workspace), subagent_llm=child,
    )
    manager.parent_permission_gate = PermissionGate(PermissionPolicy({"skill": ALLOW}))
    arguments = _contract("first")
    arguments["agent_profile"] = "reader"
    arguments["requested_tools"] = ["read_file"]
    confirmation = json.loads(manager.spawn_background(arguments, state))
    child_id = confirmation["child_session_id"]
    manager.confirm_background_startup(child_id)
    manager.commit_background_spawn_round()
    manager.activate_background_tasks()
    assert manager.wait(2)
    manager.collect_background_events()
    raw = manager.background_result(child_id, state)
    state.commit_delegation_tool_result(raw)
    manager.mark_background_claimed(child_id, json.loads(raw)["result_id"])
    assert len(calls) == 1

    skill_file.write_text(
        "---\nname: notes\ndescription: Local investigation notes\n---\nReplaced body.\n",
        encoding="utf-8",
    )
    followup = _contract("second", child_id=child_id)
    followup["requested_tools"] = ["read_file"]
    with pytest.raises(DelegationError, match="Skill 文件身份变化"):
        manager.validate_followup_arguments(followup, state)
    assert len(calls) == 1


def test_duplicate_followups_in_one_parent_round_are_rejected_before_child_start(tmp_path: Path):
    state = AgentState()
    state.begin_task("reject duplicate followups")
    child_calls = []

    def child(*_args, **_options):
        child_calls.append(True)
        return _report("complete")

    manager = DelegationManager(tmp_path, subagent_llm=child, parent_state=state)
    startup = json.loads(manager.spawn_background(_contract("first"), state))
    child_id = startup["child_session_id"]
    manager.confirm_background_startup(child_id)
    manager.commit_background_spawn_round()
    manager.activate_background_tasks()
    assert manager.wait(2)
    manager.collect_background_events()
    raw = manager.background_result(child_id, state)
    result = json.loads(raw)
    state.commit_delegation_tool_result(raw)
    manager.mark_background_claimed(child_id, result["result_id"])

    context = ContextManager(state, [{"role": "user", "content": "follow up once"}],
                             observability=False)
    registry = ToolRegistry()
    for tool in make_background_subagent_tools(state, manager):
        registry.register(tool)
    duplicated = _contract("duplicate", child_id=child_id)
    calls = []

    def parent(_messages, **_options):
        calls.append(True)
        if len(calls) == 1:
            return {"role": "assistant", "content": None, "tool_calls": [
                _call("followup_subagent", duplicated, "duplicate-1"),
                _call("followup_subagent", duplicated, "duplicate-2"),
            ]}
        return {"role": "assistant", "content": "继续"}

    runtime = _runtime(state, context, registry, parent)
    runtime.run()
    tool_results = [item["content"] for item in context.history if item.get("role") == "tool"]
    assert len(tool_results) == 2
    assert all("同一工具回合不能重复续接" in value for value in tool_results)
    assert len(state.delegation_records) == 1
    assert state.child_session_records[0].status == "idle"
    assert child_calls == [True]


def test_followup_worker_waits_for_durable_parent_round_commit(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(tmp_path / "sessions")
    state = AgentState()
    state.begin_task("commit a followup before running it")
    context = ContextManager(state, [{"role": "user", "content": "inspect"}],
                             observability=False)
    child_calls = []

    def child(*_args, **_options):
        child_calls.append(True)
        return _report(f"result {len(child_calls)}")

    registry = create_registry(state, workspace_root=workspace, subagent_llm=child)
    manager = registry._delegation_manager
    startup = json.loads(manager.spawn_background(_contract("initial"), state))
    child_id = startup["child_session_id"]
    manager.confirm_background_startup(child_id)
    manager.commit_background_spawn_round()
    manager.activate_background_tasks()
    assert manager.wait(2)
    manager.collect_background_events()
    first_raw = manager.background_result(child_id, state)
    first_result = json.loads(first_raw)
    state.commit_delegation_tool_result(first_raw)
    manager.mark_background_claimed(child_id, first_result["result_id"])
    context.history.extend([
        {"role": "assistant", "content": None, "tool_calls": [
            _call("get_subagent_result", {"child_session_id": child_id}, "claim-initial"),
        ]},
        {"role": "tool", "tool_call_id": "claim-initial", "content": first_raw},
    ])
    initial = store.save(
        None, state, context, workspace_root=workspace,
        handoff_status="clean", child_sessions=manager.export_child_sessions(state),
    )
    assert child_calls == [True]

    class FailingRoundBoundary(DurableToolBoundary):
        def complete_round(self, *_args, **_kwargs):
            raise RuntimeError("injected followup round commit failure")

    followup = _contract("next round", child_id=child_id)

    def parent(_messages, **_options):
        return {"role": "assistant", "content": None, "tool_calls": [
            _call("followup_subagent", followup, "followup-failure"),
        ]}

    with pytest.raises(RuntimeError, match="followup round commit failure"):
        _runtime(
            state, context, registry, parent,
            boundary=FailingRoundBoundary(store, initial["session_id"], workspace),
        ).run()
    assert child_calls == [True]
    assert state.delegation_budget.created_subagents == 1
    assert state.delegation_budget.reserved_subagents == 0
    unstarted = next(
        item for item in state.delegation_records
        if item.mode == "background" and item.subagent_id == child_id
        and item.delegation_id != first_result["delegation_id"]
    )
    detached = AgentState.restore_session(
        state.export_session(allow_pending=True), str(workspace), allow_pending=True,
    )
    detached.cancel_unstarted_background_delegation(
        unstarted.delegation_id, "injected uncommitted followup round",
    )
    assert detached.delegation_budget.created_subagents == 1
    assert detached.delegation_budget.reserved_subagents == 0
    assert manager.cleanup_background(state.task_id, timeout=1)["complete"]
    assert child_calls == [True]
