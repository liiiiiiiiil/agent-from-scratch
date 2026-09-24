"""v0.47 named Subagent roles, model binding and Skill isolation."""
from __future__ import annotations

from pathlib import Path
from dataclasses import asdict, replace
import hashlib
import json
from types import SimpleNamespace

import pytest

from mini_agent.agent_profiles import AgentProfileCatalog
from mini_agent.agent import ParentRuntimePolicy
from mini_agent.context import ContextManager
from mini_agent.delegation import (
    DelegationError, ScopeGate, SubagentRunner, build_delegated_task,
    validate_delegation_arguments,
)
from mini_agent.permission import ALLOW, DENY, PermissionGate, PermissionPolicy
from mini_agent.providers.base import ProviderResponse, ProviderUsage, UsageMeter
from mini_agent.providers.catalog import ModelBindingRef
from mini_agent.runtime import AgentRuntime
from mini_agent.session import DurableToolBoundary, SessionStore
from mini_agent.skills import SkillCatalog
from mini_agent.state import AgentState
from mini_agent.tools import create_registry
from mini_agent.tools.base import ToolExecutor


def _arguments(**changes):
    value = {
        "goal": "inspect the implementation",
        "scope": ["src"],
        "constraints": [],
        "expected_findings": [],
        "requested_tools": ["read_file", "grep"],
        "selected_parent_facts": [],
        "purpose": "investigation",
    }
    value.update(changes)
    return value


def _report(summary="checked", limitations=None):
    return {"role": "assistant", "content": json.dumps({
        "summary": summary,
        "findings": [],
        "evidence": [],
        "limitations": limitations or [],
    }, ensure_ascii=False)}


def _custom_profile(**changes):
    value = {
        "description": "Review a focused code area",
        "prompt": "Report only evidence-based findings.",
        "tools": ["read_file", "grep"],
        "permissions": {},
        "skills": [],
    }
    value.update(changes)
    return value


class _Catalog:
    subagent_allowed_profiles = ("child-a", "child-b")

    def resolve_child_profile(self, requested=None):
        value = requested or "child-a"
        if value not in self.subagent_allowed_profiles:
            raise ValueError(f"profile not in child allowlist: {value}")
        return value

    def bind(self, profile):
        binding = SimpleNamespace()
        binding.profile = SimpleNamespace(
            name=profile, context_window=128_000, max_output_tokens=8_192,
        )
        binding.reference = ModelBindingRef(
            profile, f"provider-{profile}", "openai_chat",
            hashlib.sha256(profile.encode()).hexdigest(),
        )
        binding.usage_meter = UsageMeter()
        binding.complete = lambda messages, **_kwargs: ProviderResponse(
            _report("summary"), "stop", ProviderUsage(1, 1, "provider"),
        )
        return binding


def _write_skill(root: Path, name: str, body: str) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    path = directory / "SKILL.md"
    path.write_text(
        f"---\nname: {name}\ndescription: {name} workflow\n---\n{body}",
        encoding="utf-8",
    )
    return path


def test_builtin_roles_and_custom_role_freeze_and_tool_permission_intersection(tmp_path):
    catalog = AgentProfileCatalog({"docs_auditor": _custom_profile(
        permissions={"read_file": "deny", "grep": "allow"},
    )})
    profiles = {item.profile_id: item for item in catalog.profiles}
    assert set(profiles) == {"explorer", "reviewer", "tester", "general", "docs_auditor"}
    assert set(profiles["explorer"].tools) == {"read_file", "list_dir", "grep"}
    assert "calculate" in profiles["general"].tools
    assert "不得执行测试" in profiles["tester"].prompt

    raw = _custom_profile()
    frozen = AgentProfileCatalog({"custom": raw})
    raw["prompt"] = "mutated after Runtime creation"
    task = build_delegated_task(
        _arguments(agent_profile="docs_auditor"), workspace_root=tmp_path,
        agent_profile_catalog=catalog,
    )
    assert task.allowed_tools == ("grep",)
    assert "custom" not in frozen.resolve("custom").prompt
    assert task.agent_profile_fingerprint == profiles["docs_auditor"].fingerprint
    assert "prompt" not in task.to_dict()


@pytest.mark.parametrize("configured", [
    {"explorer": _custom_profile()},
    {"bad Role": _custom_profile()},
    {"bad_tool": _custom_profile(tools=["run_shell"])},
    {"bad_permission": _custom_profile(permissions={"calculate": "allow"})},
    {"bad_action": _custom_profile(permissions={"grep": "ask"})},
    {"bad_skill": _custom_profile(skills=["../outside"])},
])
def test_invalid_profile_definitions_are_rejected(configured):
    with pytest.raises(ValueError):
        AgentProfileCatalog(configured)


def test_unknown_role_and_model_conflict_fail_during_contract_validation(tmp_path):
    provider_catalog = _Catalog()
    profiles = AgentProfileCatalog({
        "specialist": _custom_profile(model_profile="child-b"),
    }, provider_catalog=provider_catalog)
    with pytest.raises(DelegationError, match="未知 agent_profile"):
        validate_delegation_arguments(
            _arguments(agent_profile="missing"), provider_catalog=provider_catalog,
            agent_profile_catalog=profiles,
        )
    with pytest.raises(DelegationError, match="同一子模型"):
        validate_delegation_arguments(
            _arguments(agent_profile="specialist", model_profile="child-a"),
            provider_catalog=provider_catalog, agent_profile_catalog=profiles,
        )
    valid = build_delegated_task(
        _arguments(agent_profile="specialist", model_profile="child-b"),
        workspace_root=tmp_path, provider_catalog=provider_catalog,
        agent_profile_catalog=profiles,
    )
    assert valid.model_profile == "child-b"
    with pytest.raises(ValueError, match="model_profile 无效"):
        AgentProfileCatalog({
            "specialist": _custom_profile(model_profile="unknown"),
        }, provider_catalog=provider_catalog)


def test_role_contract_identity_changes_without_changing_legacy_identity(tmp_path):
    profiles = AgentProfileCatalog({})
    legacy = build_delegated_task(_arguments(), workspace_root=tmp_path)
    legacy_payload = {
        "delegation_id": legacy.delegation_id,
        "subagent_id": legacy.subagent_id,
        "parent_task_id": legacy.parent_task_id,
        "parent_generation_id": legacy.parent_generation_id,
        "goal": legacy.goal,
        "scope": list(legacy.scope),
        "constraints": list(legacy.constraints),
        "expected_findings": list(legacy.expected_findings),
        "requested_tools": list(legacy.requested_tools),
        "allowed_tools": list(legacy.allowed_tools),
        "selected_parent_facts": list(legacy.selected_parent_facts),
        "purpose": legacy.purpose,
        "source_id": legacy.source_id,
        "budget": asdict(legacy.budget),
        "depth": legacy.depth,
        "created_at": legacy.created_at,
    }
    old_contract_hash = hashlib.sha256(json.dumps(
        legacy_payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()
    assert legacy.contract_hash == old_contract_hash
    assert "agent_profile" not in legacy.to_dict()

    explorer = profiles.resolve("explorer")
    reviewer = profiles.resolve("reviewer")
    role_task = replace(
        legacy, contract_hash="", allowed_tools=("read_file", "grep"),
        agent_profile=explorer.profile_id,
        agent_profile_fingerprint=explorer.fingerprint,
    )
    other_role = replace(
        role_task, contract_hash="", agent_profile=reviewer.profile_id,
        agent_profile_fingerprint=reviewer.fingerprint,
    )
    assert role_task.contract_hash != other_role.contract_hash


def test_tester_report_cannot_claim_tests_passed(tmp_path):
    task = build_delegated_task(
        _arguments(agent_profile="tester"), workspace_root=tmp_path,
        agent_profile_catalog=AgentProfileCatalog({}),
    )
    gate = ScopeGate(tmp_path, ["src"])
    with pytest.raises(DelegationError, match="不能报告测试"):
        SubagentRunner._parse_report(task, {
            "summary": "All tests passed.", "findings": [], "evidence": [], "limitations": [],
        }, gate)
    with pytest.raises(DelegationError, match="不能报告测试"):
        SubagentRunner._parse_report(task, {
            "summary": "Review complete.", "findings": [], "evidence": [],
            "limitations": ["本次未执行测试；tests passed"],
        }, gate)
    parsed = SubagentRunner._parse_report(task, {
        "summary": "Suggested three test cases.", "findings": [], "evidence": [],
        "limitations": ["本次未执行测试"],
    }, gate)
    assert parsed[0] == "Suggested three test cases."


def test_parent_pre_authorizes_only_listed_skills_and_child_context_is_isolated(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    skill_root = workspace / "skills"
    granted_path = _write_skill(skill_root, "granted", "UNTRUSTED_GRANTED_GUIDANCE")
    denied_path = _write_skill(skill_root, "denied", "MUST_NEVER_ENTER_CHILD")
    skill_catalog = SkillCatalog(workspace, global_root=tmp_path / "missing-skills")
    role_catalog = AgentProfileCatalog({"guided": _custom_profile(
        prompt="Inspect the task, and use available guidance only when relevant.",
        tools=["read_file"], skills=["granted", "denied"],
    )})
    state = AgentState()
    state.begin_task("parent task")
    child_views = []

    def child_llm(messages, **options):
        registry = options["tool_registry"]
        child_views.append((messages, {tool.name for tool in registry.list_tools()}))
        if len(child_views) == 1:
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "child-skill", "type": "function",
                "function": {"name": "skill", "arguments": json.dumps({"name": "granted"})},
            }]}
        return _report("used one approved workflow")

    registry = create_registry(
        state, workspace_root=workspace, subagent_llm=child_llm,
        skill_catalog=skill_catalog, agent_profile_catalog=role_catalog,
        include_mcp=False,
    )
    role_description = registry.get("delegate_task").description
    assert "guided" in role_description and "Inspect the task" not in role_description
    assert "reviewer" in role_description and "审阅" in role_description
    gate = PermissionGate(PermissionPolicy({
        "delegate_task": ALLOW,
        "skill": {"granted": ALLOW, "denied": DENY},
    }))
    executor = ToolExecutor(registry, gate=gate, on_result=state.record_tool)
    result = executor.execute_result("delegate_task", _arguments(
        scope=["src"], requested_tools=["read_file"], agent_profile="guided",
        selected_parent_facts=["parent-only fact"],
    ), state=state, notify=False)
    payload = json.loads(result.output)
    assert payload["outcome"] == "completed"
    assert payload["agent_profile"] == "guided"
    assert payload["agent_profile_fingerprint"] == role_catalog.resolve("guided").fingerprint
    assert "skill" in child_views[0][1]
    rendered = json.dumps(child_views[0][0], ensure_ascii=False)
    assert "granted" in rendered and "denied" not in rendered
    assert "parent-only fact" in rendered
    assert "parent history" not in rendered
    assert "MUST_NEVER_ENTER_CHILD" not in result.output
    assert "UNTRUSTED_GRANTED_GUIDANCE" not in result.output
    assert granted_path.exists() and denied_path.exists()


def test_role_skill_load_rejects_frozen_file_replacement(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    skill_path = _write_skill(workspace / "skills", "replace_me", "ORIGINAL_BODY")
    skill_catalog = SkillCatalog(workspace, global_root=tmp_path / "missing-skills")
    profiles = AgentProfileCatalog({"guided": _custom_profile(
        tools=["read_file"], skills=["replace_me"],
    )})
    state = AgentState()
    state.begin_task("load approved guidance")
    child_calls = []

    def child_llm(messages, **options):
        child_calls.append(messages)
        if len(child_calls) == 1:
            skill_path.write_text(
                "---\nname: replace_me\ndescription: replaced workflow\n---\nREPLACED_BODY",
                encoding="utf-8",
            )
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "load-replaced", "type": "function",
                "function": {"name": "skill", "arguments": json.dumps({"name": "replace_me"})},
            }]}
        return _report("The changed Skill was rejected.")

    registry = create_registry(
        state, workspace_root=workspace, subagent_llm=child_llm,
        skill_catalog=skill_catalog, agent_profile_catalog=profiles, include_mcp=False,
    )
    executor = ToolExecutor(registry, gate=PermissionGate(PermissionPolicy({
        "delegate_task": ALLOW, "skill": {"replace_me": ALLOW},
    })))
    result = executor.execute_result("delegate_task", _arguments(
        scope=["src"], requested_tools=["read_file"], agent_profile="guided",
    ), state=state, notify=False)
    assert json.loads(result.output)["outcome"] == "completed"
    assert "skill_access_error" in str(child_calls[-1])
    assert "ORIGINAL_BODY" not in result.output and "REPLACED_BODY" not in result.output


def test_child_unknown_skill_is_denied_without_prompt(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    _write_skill(workspace / "skills", "granted", "GRANTED_BODY")
    _write_skill(workspace / "skills", "unlisted", "UNLISTED_BODY")
    profiles = AgentProfileCatalog({"guided": _custom_profile(skills=["granted"])})
    state = AgentState()
    state.begin_task("inspect")
    calls = []

    def child_llm(messages, **_options):
        calls.append(messages)
        if len(calls) == 1:
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "unknown-skill", "type": "function",
                "function": {"name": "skill", "arguments": json.dumps({"name": "unlisted"})},
            }]}
        return _report("unknown skill was denied")

    monkeypatch.setattr("builtins.input", lambda *_args: pytest.fail("child requested permission"))
    registry = create_registry(
        state, workspace_root=workspace, subagent_llm=child_llm,
        skill_catalog=SkillCatalog(workspace, global_root=tmp_path / "missing-skills"),
        agent_profile_catalog=profiles, include_mcp=False,
    )
    executor = ToolExecutor(registry, gate=PermissionGate(PermissionPolicy({
        "delegate_task": ALLOW, "skill": {"granted": ALLOW},
    })))
    result = executor.execute_result("delegate_task", _arguments(
        agent_profile="guided",
    ), state=state, notify=False)
    assert json.loads(result.output)["outcome"] == "completed"
    assert "权限拒绝" in str(calls[-1])
    assert "UNLISTED_BODY" not in str(calls[-1])


def test_unknown_profile_skill_rejects_delegation_before_child_runs(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    profiles = AgentProfileCatalog({"guided": _custom_profile(skills=["missing"])})
    state = AgentState()
    state.begin_task("inspect")
    registry = create_registry(
        state, workspace_root=workspace,
        subagent_llm=lambda *_args, **_kwargs: pytest.fail("child must not run"),
        skill_catalog=SkillCatalog(workspace, global_root=tmp_path / "missing-skills"),
        agent_profile_catalog=profiles, include_mcp=False,
    )
    monkeypatch.setattr("builtins.input", lambda *_args: pytest.fail("unknown skill prompted"))
    executor = ToolExecutor(registry, gate=PermissionGate(PermissionPolicy({
        "delegate_task": ALLOW, "skill": {"missing": ALLOW},
    })))
    result = executor.execute_result("delegate_task", _arguments(
        agent_profile="guided",
    ), state=state, notify=False)
    assert json.loads(result.output)["outcome"] == "failed"
    assert "不存在或不可用的 Skill: missing" in result.output


def test_scope_gate_rejects_sensitive_files_session_directories_and_symlinks(tmp_path):
    workspace = tmp_path / "workspace"
    source = workspace / "src"
    source.mkdir(parents=True)
    config = workspace / "config_local.py"
    config.write_text("SECRET = True", encoding="utf-8")
    session_dir = workspace / ".mini_agent" / "sessions"
    session_dir.mkdir(parents=True)
    session_file = session_dir / "session.json"
    session_file.write_text("sensitive", encoding="utf-8")
    (source / "config-alias").symlink_to(config)
    (source / "sessions-alias").symlink_to(session_dir, target_is_directory=True)

    gate = ScopeGate(workspace, ["src"])
    for path in ("src/config-alias", "src/sessions-alias/session.json",
                 ".mini_agent/sessions/session.json"):
        with pytest.raises(DelegationError):
            gate.validate_path(path)
    with pytest.raises(DelegationError):
        ScopeGate(workspace, [".mini_agent/sessions"])


def test_custom_session_store_root_is_bound_to_child_scope(tmp_path):
    workspace = tmp_path / "workspace"
    source = workspace / "src"
    source.mkdir(parents=True)
    store = SessionStore(source / "private" / "agent-data")
    (store.root / "session.json").write_text("SESSION_SECRET", encoding="utf-8")
    (source / "session-alias").symlink_to(store.root, target_is_directory=True)
    state = AgentState()
    state.begin_task("inspect")
    context = ContextManager(state, [{"role": "user", "content": "inspect"}], observability=False)
    envelope = store.save(None, state, context, workspace_root=workspace)
    registry = create_registry(state, workspace_root=workspace, include_mcp=False)
    runtime = AgentRuntime(
        llm_client=lambda *_args, **_kwargs: _report(), context=context,
        executor=ToolExecutor(registry), policy=ParentRuntimePolicy(), max_rounds=1,
        session_boundary=DurableToolBoundary(store, envelope["session_id"], workspace),
    )
    manager = runtime.executor.registry._delegation_manager
    assert manager.session_root == str(store.root.resolve())
    with pytest.raises(DelegationError, match="session 敏感目录"):
        manager.create_task(_arguments(scope=["src/private/agent-data"]), state)
    child_gate = ScopeGate(workspace, ["src"], session_root=manager.session_root)
    for path in ("src/private/agent-data/session.json", "src/session-alias/session.json"):
        with pytest.raises(DelegationError, match="session 敏感目录"):
            child_gate.validate_path(path)


def test_named_role_identity_survives_schema_three_result_commit(tmp_path):
    state = AgentState()
    state.begin_task("persist reviewer delegation")
    context = ContextManager(state, [{"role": "user", "content": "parent history"}], observability=False)
    store = SessionStore(tmp_path / "sessions")
    initial = store.save(None, state, context, workspace_root=tmp_path)
    boundary = DurableToolBoundary(store, initial["session_id"], tmp_path)
    profiles = AgentProfileCatalog({})
    registry = create_registry(
        state, workspace_root=tmp_path, include_mcp=False,
        agent_profile_catalog=profiles,
        subagent_llm=lambda *_args, **_kwargs: _report("reviewed"),
    )
    parent_calls = [0]

    def parent_llm(_messages, **_options):
        parent_calls[0] += 1
        if parent_calls[0] == 1:
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "review-call", "type": "function",
                "function": {"name": "delegate_task", "arguments": json.dumps(
                    _arguments(scope=["."], requested_tools=["read_file"],
                               agent_profile="reviewer"),
                )},
            }]}
        return {"role": "assistant", "content": "done"}

    runtime = AgentRuntime(
        llm_client=parent_llm, context=context,
        executor=ToolExecutor(registry), policy=ParentRuntimePolicy(),
        max_rounds=3, session_boundary=boundary,
    )
    result = runtime.run()
    assert result.stop_reason == "text"
    envelope = store.load(initial["session_id"])
    record = envelope["state"]["delegation_records"][0]
    call = envelope["tool_boundary"]["calls"][0]
    raw_result = json.loads(call["result"]["content"])
    expected_fingerprint = profiles.resolve("reviewer").fingerprint
    assert record["agent_profile"] == raw_result["agent_profile"] == "reviewer"
    assert record["agent_profile_fingerprint"] == expected_fingerprint
    assert call["agent_profile_fingerprint"] == expected_fingerprint
    assert raw_result["agent_profile_fingerprint"] == expected_fingerprint
    assert record["delivery_status"] == "committed"
    assert record["usage"]["llm_calls"] == 1
    restored = AgentState.restore_session(envelope["state"], workspace_root=str(tmp_path))
    assert restored.delegation_records[0].agent_profile == "reviewer"
    assert restored.delegation_records[0].agent_profile_fingerprint == expected_fingerprint


def test_same_round_distinct_roles_settle_budget_and_keep_role_outputs(tmp_path):
    state = AgentState()
    state.begin_task("parallel role review")
    context = ContextManager(state, [{"role": "user", "content": "parent only"}], observability=False)
    profiles = AgentProfileCatalog({})
    child_roles = []

    def child_llm(messages, **_options):
        contract_message = next(
            message["content"] for message in messages if message.get("role") == "user"
        )
        contract = json.loads(contract_message)["contract"]
        child_roles.append(contract["agent_profile"])
        assert "parent only" not in json.dumps(messages, ensure_ascii=False)
        return _report(contract["agent_profile"] + " completed")

    registry = create_registry(
        state, workspace_root=tmp_path, include_mcp=False,
        agent_profile_catalog=profiles, subagent_llm=child_llm,
    )
    parent_calls = [0]

    def parent_llm(_messages, **_options):
        parent_calls[0] += 1
        if parent_calls[0] == 1:
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": f"role-{name}", "type": "function",
                "function": {"name": "delegate_task", "arguments": json.dumps(_arguments(
                    scope=["."], requested_tools=["read_file"], agent_profile=name,
                ))},
            } for name in ("explorer", "reviewer")]}
        return {"role": "assistant", "content": "done"}

    result = AgentRuntime(
        llm_client=parent_llm, context=context,
        executor=ToolExecutor(registry), policy=ParentRuntimePolicy(), max_rounds=3,
    ).run()
    assert result.stop_reason == "text"
    assert set(child_roles) == {"explorer", "reviewer"}
    assert {item.agent_profile for item in state.delegation_records} == {"explorer", "reviewer"}
    assert all(item.delivery_status == "committed" for item in state.delegation_records)
    budget = state.delegation_budget_snapshot()
    assert budget["created_subagents"] == 2
    assert budget["reserved_subagents"] == 0
    assert budget["used_llm_calls"] == 2
