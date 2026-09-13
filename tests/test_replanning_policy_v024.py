"""v0.24 trigger, replan budget, and stagnation policy coverage."""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
from unittest.mock import patch

import pytest

from mini_agent.agent import agent_loop
from mini_agent import config
from mini_agent.context import ContextManager
from mini_agent.permission import ALLOW, PermissionGate, PermissionPolicy
from mini_agent.state import AgentState, PlanRejected
from mini_agent.tools import create_registry
from mini_agent.tools.base import Tool, ToolExecutor, ToolRegistry


def _step(step_id, content=None, depends_on=None, replaces=None):
    return {
        "step_id": step_id,
        "content": content or step_id,
        "depends_on": depends_on or [],
        "success_criteria": [f"{step_id} done"],
        "replaces": replaces or [],
    }


def _plan(**overrides):
    value = {
        "goal": "finish",
        "constraints": [],
        "success_criteria": ["check passes"],
        "steps": [_step("inspect"), _step("edit", depends_on=["inspect"])],
        "reason": "initial",
    }
    value.update(overrides)
    return value


def _executor(state, extra=None):
    registry = create_registry(state)
    rules = {
        "begin_plan": ALLOW, "cancel_planning": ALLOW,
        "commit_plan": ALLOW, "update_plan_progress": ALLOW,
        "request_replan": ALLOW, "read_file": ALLOW,
        "run_shell": ALLOW, "recover": ALLOW,
    }
    if extra is not None:
        registry.register(extra)
        rules[extra.name] = ALLOW
    return ToolExecutor(registry, PermissionGate(PermissionPolicy(rules)))


def _record(executor, state, name, arguments):
    result = executor.execute_result(name, arguments, state)
    state.record_execution_result(result)
    return result


def test_default_fingerprint_budget_leaves_room_for_stagnation_rounds():
    assert config.MAX_ATTEMPT_FINGERPRINTS == 4
    assert config.MAX_ATTEMPT_FINGERPRINTS >= config.MAX_STAGNANT_ROUNDS + 1
    previous = config.MAX_ATTEMPT_FINGERPRINTS
    try:
        config.MAX_ATTEMPT_FINGERPRINTS = config.MAX_STAGNANT_ROUNDS
        with pytest.raises(ValueError):
            config.validate_runtime_config()
    finally:
        config.MAX_ATTEMPT_FINGERPRINTS = previous


def test_failure_and_direct_blocked_resume_can_start_triggered_first_revision():
    state = AgentState(); state.begin_task("failure")
    executor = _executor(state)
    failed = _record(executor, state, "run_shell", {
        "command": "false", "purpose": "verification",
    })
    assert state.repair_phase == "diagnosis_required"
    requested = executor.execute_result("request_replan", {
        "kind": "failure", "source_id": "f-1", "reason": "the approach is invalid",
    }, state)
    assert requested.ok
    trigger_id = state.planning_state.active_trigger_id
    revision = state.commit_plan(**_plan(trigger_id=trigger_id))
    assert revision.parent_revision_id is None
    assert revision.trigger_id == trigger_id
    assert state.repair_phase == "idle"
    assert state.snapshot()["failures"][0]["failure_id"] == "f-1"

    blocked = AgentState(); blocked.begin_task("blocked")
    for index in range(3):
        observation = blocked.observe_tool_round(
            "same-round", (), (), ("read_file",),
        )
        if index == 1:
            assert observation["warning"]
        if index == 2:
            assert observation["blocked"]
    assert blocked.status == "blocked"
    decision = blocked.resume_blocked("外部条件已经补齐")
    trigger_id = blocked.planning_state.active_trigger_id
    blocked_trigger = blocked.snapshot()["replan_triggers"][-1]
    revision = blocked.commit_plan(**_plan(trigger_id=trigger_id))
    assert decision.decision == "resume_blocked"
    assert decision.revision_id is None
    assert blocked_trigger["caused_by_decision_id"] == decision.decision_id
    assert blocked_trigger["caused_by_failure_id"] is None
    assert blocked_trigger["caused_by_attempt_id"] is None
    assert revision.parent_revision_id is None
    assert revision.trigger_id == trigger_id
    assert blocked.status == "running"


def test_observation_trigger_requires_new_successful_read_only_attempt_and_diff_is_runtime_computed():
    outputs = iter(("before", "after"))
    observe = Tool(
        "observe", "read-only observation", {"type": "object", "properties": {}},
        lambda: next(outputs), effect_class="none",
    )
    state = AgentState(); state.begin_task("observe")
    executor = _executor(state, observe)
    state.commit_plan(**_plan())
    with pytest.raises(PlanRejected):
        state.request_replan("observation", "a-1", "too early")
    _record(executor, state, "observe", {})
    trigger = state.request_replan("observation", "a-1", "the observation changes the approach")
    assert trigger.caused_by_attempt_id == "a-1"
    changed = state.commit_plan(
        **_plan(
            goal="revised", parent_revision_id=1, trigger_id=trigger.trigger_id,
            steps=[_step("inspect"), _step("new_step", depends_on=["inspect"])],
        )
    )
    diff = state.snapshot()["plan_revisions"][-1]["diff"]
    assert changed.revision_id == 2
    assert diff["retained"] == [{"step_id": "inspect", "dependencies_changed": False}]
    assert diff["added"] == ["new_step"]
    assert diff["cancelled"] == ["edit"]
    assert diff["goal_changed"] is True
    assert state.snapshot()["replan_triggers"][-1]["status"] == "resolved"
    with pytest.raises(PlanRejected):
        state.request_replan("observation", "a-1", "a trigger is one shot")


def test_unchanged_replan_commits_are_bounded_without_consuming_revision_or_trigger():
    state = AgentState(); state.begin_task("replan")
    state.commit_plan(**_plan())
    executor = _executor(state)
    _record(executor, state, "run_shell", {"command": "false", "purpose": "verification"})
    trigger = state.request_replan("failure", "f-1", "replace the plan")
    before = deepcopy(state.snapshot())
    for count in (1, 2):
        with pytest.raises(PlanRejected):
            state.commit_plan(**_plan(trigger_id=trigger.trigger_id, parent_revision_id=1))
        assert len(state.plan_revisions) == 1
        if count == 1:
            assert state.status == "running"
            assert state.planning_state.trigger_no_progress_commits == 1
        else:
            assert state.status == "blocked"
            assert "replan_no_progress" in state.terminal_reason
    assert state.planning_state.replans_used == before["planning_state"]["replans_used"]
    assert state.planning_state.active_trigger_id == trigger.trigger_id


def test_replan_budget_is_shared_and_fourth_request_blocks():
    observe = Tool(
        "observe", "read-only", {"type": "object", "properties": {"value": {"type": "string"}}},
        lambda value: f"new observation {value}", effect_class="none",
    )
    state = AgentState(); state.begin_task("budget")
    executor = _executor(state, observe)
    state.commit_plan(**_plan())
    for revision_id in range(1, 4):
        _record(executor, state, "observe", {"value": str(revision_id)})
        trigger = state.request_replan("observation", f"a-{revision_id}", f"change {revision_id}")
        state.commit_plan(**_plan(
            goal=f"revision {revision_id}", parent_revision_id=revision_id,
            trigger_id=trigger.trigger_id,
        ))
    assert state.planning_state.replans_used == 3
    assert state.planning_state.replans_remaining == 0
    _record(executor, state, "observe", {"value": "4"})
    with pytest.raises(PlanRejected):
        state.request_replan("observation", "a-4", "fourth change")
    assert state.status == "blocked"
    assert "replan_budget_exhausted" in state.terminal_reason


def test_loop_observes_after_tool_results_and_blocks_repeated_rounds():
    state = AgentState(); state.begin_task("loop")
    state.begin_plan()
    registry = create_registry(state)
    registry.register(Tool(
        "inspect", "read-only", {"type": "object", "properties": {}},
        lambda: "same fact",
    ))
    executor = ToolExecutor(registry, PermissionGate(PermissionPolicy({"inspect": ALLOW})))
    context = ContextManager(state, [{"role": "user", "content": "loop"}])
    responses = iter([
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "one", "type": "function",
            "function": {"name": "inspect", "arguments": "{}"},
        }]},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "two", "type": "function",
            "function": {"name": "inspect", "arguments": "{}"},
        }]},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "three", "type": "function",
            "function": {"name": "inspect", "arguments": "{}"},
        }]},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "four", "type": "function",
            "function": {"name": "inspect", "arguments": "{}"},
        }]},
    ])
    notices = []
    context.set_runtime_notice = lambda value: notices.append(value)
    with patch("mini_agent.agent.call_llm", side_effect=lambda *args, **kwargs: next(responses)):
        result = agent_loop(context, executor)
    assert result.startswith("任务已阻塞：")
    assert state.status == "blocked"
    assert "no_new_observation" in state.terminal_reason
    assert len([item for item in context.history if item.get("role") == "tool"]) == 4
    assert len(notices) == 1


def test_failure_replan_keeps_failure_and_requires_new_generation_verification():
    mutation = Tool(
        "mutate", "possible mutation", {"type": "object", "properties": {}},
        lambda: "changed", effect_class="possible",
    )
    state = AgentState(); state.begin_task("repair and replan")
    executor = _executor(state, mutation)
    state.commit_plan(**_plan())
    _record(executor, state, "run_shell", {
        "command": "false", "purpose": "verification",
    })
    first_generation = state.current_generation_id
    trigger = state.request_replan("failure", "f-1", "the old plan cannot pass verification")
    state.commit_plan(**_plan(
        goal="revised repair", parent_revision_id=1,
        trigger_id=trigger.trigger_id,
    ))
    assert state.repair_phase == "idle"
    assert state.current_generation_id == first_generation
    assert state.snapshot()["failures"][0]["failure_id"] == "f-1"
    _record(executor, state, "mutate", {})
    assert state.current_generation_id == first_generation + 1
    verified = _record(executor, state, "run_shell", {
        "command": "true", "purpose": "verification",
    })
    assert verified.ok
    snapshot = state.snapshot()
    assert snapshot["repair_loop"]["phase"] == "idle"
    assert snapshot["verification_evidence"][-1]["generation_id"] == first_generation + 1
    assert snapshot["failures"][0]["failure_id"] == "f-1"


def test_user_feedback_and_blocked_resume_triggers_keep_source_fields_separate():
    state = AgentState(); state.begin_task("handoff", mode="plan_only")
    state.commit_plan(**_plan())
    state.decide_plan("rejected", 1, "change the execution order")
    trigger = state.snapshot()["replan_triggers"][-1]
    assert trigger["kind"] == "user_feedback"
    assert trigger["caused_by_decision_id"] is not None
    assert trigger["caused_by_failure_id"] is None
    assert trigger["caused_by_attempt_id"] is None
    state.status = "blocked"
    # An active user-feedback trigger represents an unfinished handoff and
    # prevents a second recovery decision from being appended.
    with pytest.raises(PlanRejected):
        state.resume_blocked("duplicate")


def test_request_replan_gate_precedes_permission_and_rejects_mixed_round():
    state = AgentState(); state.begin_task("gates")
    registry = create_registry(state)

    class GuardPolicy:
        def __init__(self):
            self.calls = []

        def check(self, name, pattern):
            self.calls.append(name)
            return ALLOW

    guard = GuardPolicy()
    executor = ToolExecutor(registry, PermissionGate(guard))
    malformed = executor.execute_result("request_replan", {
        "kind": "failure", "source_id": "f-1",
    }, state)
    assert malformed.error_kind == "plan_rejected"
    assert guard.calls == []

    state.begin_plan()
    gated = executor.execute_result("request_replan", {
        "kind": "observation", "source_id": "a-1", "reason": "changed",
    }, state)
    assert gated.error_kind == "plan_rejected"
    assert guard.calls == []
    state.cancel_planning()

    context = ContextManager(state, [{"role": "user", "content": "gates"}])
    responses = iter([
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "replan", "type": "function", "function": {
                "name": "request_replan",
                "arguments": json.dumps({
                    "kind": "observation", "source_id": "a-1", "reason": "changed",
                }),
            }},
            {"id": "read", "type": "function", "function": {
                "name": "read_file", "arguments": json.dumps({"path": "missing"}),
            }},
        ]},
        {"role": "assistant", "content": "继续调查。"},
    ])
    with patch("mini_agent.agent.call_llm", side_effect=lambda *args, **kwargs: next(responses)):
        agent_loop(context, executor)
    tool_results = [item for item in context.history if item.get("role") == "tool"]
    assert len(tool_results) == 2
    assert not state.attempts


def test_concurrent_revision_submission_resolves_one_trigger_without_partial_writes():
    state = AgentState(); state.begin_task("concurrent")
    state.commit_plan(**_plan())
    executor = _executor(state)
    _record(executor, state, "run_shell", {"command": "false", "purpose": "verification"})
    trigger = state.request_replan("failure", "f-1", "change the plan")

    def submit(goal):
        try:
            state.commit_plan(**_plan(
                goal=goal, parent_revision_id=1, trigger_id=trigger.trigger_id,
            ))
            return "committed"
        except PlanRejected as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, ("revision A", "revision B")))
    assert results.count("committed") == 1
    assert len(state.plan_revisions) == 2
    assert state.snapshot()["replan_triggers"][-1]["status"] == "resolved"
    assert state.planning_state.replans_used == 1
    assert state.planning_state.active_trigger_id is None


def test_ask_resume_returns_to_diagnosis_and_keeps_verification_obligation():
    state = AgentState(); state.begin_task("resume repair")
    executor = _executor(state)
    _record(executor, state, "run_shell", {
        "command": "false", "purpose": "verification",
    })
    failure_id = state.active_failure_id
    asked = executor.execute_result("recover", {
        "action": "ask", "caused_by_failure_id": failure_id,
        "reason": "等待外部条件",
    }, state)
    generation = state.current_generation_id
    cycles = state.snapshot()["repair_loop"]["cycles_used"]
    assert asked.ok
    assert state.status == "blocked"
    assert state.repair_phase == "verification_required"

    decision = state.resume_blocked("外部条件已补齐")
    assert decision.decision == "resume_blocked"
    assert state.status == "running"
    assert state.repair_phase == "diagnosis_required"
    assert state.active_failure_id == failure_id
    assert state.active_recovery_id is None
    assert state.current_generation_id == generation
    assert state.snapshot()["repair_loop"]["cycles_used"] == cycles
    assert state.snapshot()["verification_required"] is True

    trigger_id = state.planning_state.active_trigger_id
    revision = state.commit_plan(**_plan(
        goal="revised after ask", trigger_id=trigger_id,
    ))
    assert revision.parent_revision_id is None
    assert state.repair_phase == "idle"
    assert state.current_generation_id == generation
    assert state.snapshot()["verification_required"] is True
    assert state.snapshot()["failures"][0]["failure_id"] == failure_id

    verified = _record(executor, state, "run_shell", {
        "command": "true", "purpose": "verification",
    })
    assert verified.ok
    assert state.snapshot()["verification_evidence"][-1]["generation_id"] == generation
    assert state.snapshot()["verification_required"] is False


def test_real_loop_recover_ask_resume_explore_commit_and_verify():
    observe = Tool(
        "inspect", "read-only observation", {"type": "object", "properties": {}},
        lambda: "external condition is ready", effect_class="none",
    )
    state = AgentState(); state.begin_task("real recovery loop")
    executor = _executor(state, observe)
    _record(executor, state, "run_shell", {
        "command": "false", "purpose": "verification",
    })
    context = ContextManager(state, [{"role": "user", "content": "recover"}])
    first_response = {"role": "assistant", "content": None, "tool_calls": [{
        "id": "ask", "type": "function", "function": {
            "name": "recover", "arguments": json.dumps({
                "action": "ask", "caused_by_failure_id": "f-1",
                "reason": "等待外部条件",
            }),
        },
    }]}
    first_calls = []

    def first_llm(*args, **kwargs):
        first_calls.append(True)
        return first_response

    with patch("mini_agent.agent.OUTPUT_MODE", "quiet"), \
            patch("mini_agent.agent.call_llm", side_effect=first_llm):
        result = agent_loop(context, executor)
    assert result.startswith("任务已阻塞：")
    assert len(first_calls) == 1

    generation = state.current_generation_id
    state.resume_blocked("外部条件已补齐")
    trigger_id = state.planning_state.active_trigger_id
    revised_plan = _plan(
        goal="revised after external condition",
        steps=[_step("inspect")],
        trigger_id=trigger_id,
    )
    responses = iter([
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "inspect", "type": "function", "function": {
                "name": "inspect", "arguments": "{}",
            },
        }]},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "commit", "type": "function", "function": {
                "name": "commit_plan", "arguments": json.dumps(revised_plan),
            },
        }]},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "verify", "type": "function", "function": {
                "name": "run_shell", "arguments": json.dumps({
                    "command": "true", "purpose": "verification",
                }),
            },
        }]},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "start", "type": "function", "function": {
                "name": "update_plan_progress", "arguments": json.dumps({
                    "revision_id": 1, "step_id": "inspect",
                    "status": "in_progress", "reason": "调查完成",
                }),
            },
        }]},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "finish", "type": "function", "function": {
                "name": "update_plan_progress", "arguments": json.dumps({
                    "revision_id": 1, "step_id": "inspect",
                    "status": "completed", "reason": "修订后的验证通过",
                }),
            },
        }]},
        {"role": "assistant", "content": "已完成。"},
    ])
    second_calls = []

    def second_llm(*args, **kwargs):
        second_calls.append(True)
        return next(responses)

    with patch("mini_agent.agent.OUTPUT_MODE", "quiet"), \
            patch("mini_agent.agent.call_llm", side_effect=second_llm):
        result = agent_loop(context, executor)
    assert result == "已完成。"
    assert len(second_calls) == 6
    assert state.status == "running"
    assert state.repair_phase == "idle"
    assert state.current_generation_id == generation
    snapshot = state.snapshot()
    assert snapshot["verification_required"] is False
    assert snapshot["failures"][0]["failure_id"] == "f-1"
    assert snapshot["replan_triggers"][-1]["status"] == "resolved"
    assert len([item for item in context.history if item.get("role") == "tool"]) == 6


def test_loop_returns_after_replan_budget_terminal_without_another_llm_call():
    observe = Tool(
        "inspect", "read-only observation", {
            "type": "object", "properties": {
                "value": {"type": "string"},
            }, "required": ["value"],
        },
        lambda value: f"observation {value}", effect_class="none",
    )
    state = AgentState(); state.begin_task("replan budget loop")
    executor = _executor(state, observe)
    state.commit_plan(**_plan())
    for revision_id in range(1, 4):
        _record(executor, state, "inspect", {"value": str(revision_id)})
        trigger = state.request_replan(
            "observation", f"a-{revision_id}", f"revise {revision_id}",
        )
        state.commit_plan(**_plan(
            goal=f"revision {revision_id}",
            parent_revision_id=revision_id,
            trigger_id=trigger.trigger_id,
        ))
    _record(executor, state, "inspect", {"value": "4"})
    context = ContextManager(state, [{"role": "user", "content": "budget"}])
    responses = iter([
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "replan", "type": "function", "function": {
                "name": "request_replan", "arguments": json.dumps({
                    "kind": "observation", "source_id": "a-4",
                    "reason": "第四次调查要求改变方案",
                }),
            },
        }]},
        {"role": "assistant", "content": "不应被调用"},
    ])
    llm_calls = []

    def fake_llm(*args, **kwargs):
        llm_calls.append(True)
        return next(responses)

    with patch("mini_agent.agent.OUTPUT_MODE", "quiet"), \
            patch("mini_agent.agent.call_llm", side_effect=fake_llm):
        result = agent_loop(context, executor)
    assert result.startswith("任务已阻塞：")
    assert "replan_budget_exhausted" in state.terminal_reason
    assert len(llm_calls) == 1
    assert len([item for item in context.history if item.get("role") == "tool"]) == 1


def test_loop_returns_after_default_fingerprint_budget_without_another_llm_call():
    calls = []
    inspect = Tool(
        "inspect", "read-only observation", {
            "type": "object", "properties": {
                "query": {"type": "string"},
            }, "required": ["query"],
        },
        lambda query: calls.append(query) or f"fact {len(calls)}", effect_class="none",
    )
    state = AgentState(); state.begin_task("fingerprint budget loop")
    executor = _executor(state, inspect)
    context = ContextManager(state, [{"role": "user", "content": "fingerprint"}])
    responses = iter([
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": str(index), "type": "function", "function": {
                "name": "inspect", "arguments": json.dumps({"query": "same"}),
            },
        }]}
        for index in range(5)
    ])
    llm_calls = []

    def fake_llm(*args, **kwargs):
        llm_calls.append(True)
        return next(responses)

    with patch("mini_agent.agent.OUTPUT_MODE", "quiet"), \
            patch("mini_agent.agent.call_llm", side_effect=fake_llm):
        result = agent_loop(context, executor)
    assert result.startswith("任务已阻塞：")
    assert "参数指纹尝试预算" in state.terminal_reason
    assert calls == ["same"] * 4
    assert len(llm_calls) == 5
    assert len([item for item in context.history if item.get("role") == "tool"]) == 5


def test_repeated_successful_verification_does_not_open_progress_epochs():
    registry = ToolRegistry()
    registry.register(Tool(
        "run_shell", "verification", {
            "type": "object", "properties": {
                "command": {"type": "string"},
                "purpose": {"type": "string", "enum": ["verification"],
                            "default": "verification"},
            }, "required": ["command"],
        },
        lambda command, purpose: "[exit=0] same result", effect_class="none",
    ))
    state = AgentState(); state.begin_task("repeat verification")
    executor = ToolExecutor(registry, PermissionGate(PermissionPolicy({
        "run_shell": ALLOW,
    })))
    context = ContextManager(state, [{"role": "user", "content": "verify"}])
    responses = iter([
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": str(index), "type": "function", "function": {
                "name": "run_shell", "arguments": json.dumps({
                    "command": "check", "purpose": "verification",
                }),
            },
        }]}
        for index in range(4)
    ])
    notices = []
    context.set_runtime_notice = lambda value: notices.append(value)
    with patch("mini_agent.agent.OUTPUT_MODE", "quiet"), \
            patch("mini_agent.agent.call_llm", side_effect=lambda *args, **kwargs: next(responses)):
        result = agent_loop(context, executor)
    assert result.startswith("任务已阻塞：")
    assert state.snapshot()["loop_stagnation"]["progress_epoch"] == 1
    assert state.snapshot()["loop_stagnation"]["consecutive_no_progress_rounds"] == 3
    assert len(state.snapshot()["verification_evidence"]) == 4
    assert len(notices) == 1


def test_repeated_same_failure_does_not_open_progress_epochs_for_new_failure_ids():
    registry = ToolRegistry()
    registry.register(Tool(
        "inspect", "failing observation", {"type": "object", "properties": {}},
        lambda: (_ for _ in ()).throw(RuntimeError("same failure")),
        effect_class="none",
    ))
    state = AgentState(); state.begin_task("repeat failure")
    executor = ToolExecutor(registry, PermissionGate(PermissionPolicy({
        "inspect": ALLOW,
    })))
    context = ContextManager(state, [{"role": "user", "content": "failure"}])
    responses = iter([
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": str(index), "type": "function", "function": {
                "name": "inspect", "arguments": "{}",
            },
        }]}
        for index in range(4)
    ])
    with patch("mini_agent.agent.OUTPUT_MODE", "quiet"), \
            patch("mini_agent.agent.call_llm", side_effect=lambda *args, **kwargs: next(responses)):
        result = agent_loop(context, executor)
    assert result.startswith("任务已阻塞：")
    assert "repeated_action" in state.terminal_reason
    assert [failure.failure_id for failure in state.failures] == [
        "f-1", "f-2", "f-3", "f-4",
    ]
    assert state.snapshot()["loop_stagnation"]["progress_epoch"] == 1


def test_different_observation_arguments_with_same_output_do_not_reset_stagnation():
    inspect = Tool(
        "inspect", "read-only observation", {
            "type": "object", "properties": {
                "query": {"type": "string"},
            }, "required": ["query"],
        },
        lambda query: "same search result", effect_class="none",
    )
    state = AgentState(); state.begin_task("same observation")
    executor = _executor(state, inspect)
    context = ContextManager(state, [{"role": "user", "content": "search"}])
    responses = iter([
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": str(index), "type": "function", "function": {
                "name": "inspect", "arguments": json.dumps({
                    "query": f"query-{index}",
                }),
            },
        }]}
        for index in range(4)
    ])
    notices = []
    context.set_runtime_notice = lambda value: notices.append(value)
    with patch("mini_agent.agent.OUTPUT_MODE", "quiet"), \
            patch("mini_agent.agent.call_llm", side_effect=lambda *args, **kwargs: next(responses)):
        result = agent_loop(context, executor)
    assert result.startswith("任务已阻塞：")
    assert "no_new_observation" in state.terminal_reason
    assert state.snapshot()["loop_stagnation"]["progress_epoch"] == 0
    assert len(notices) == 1
