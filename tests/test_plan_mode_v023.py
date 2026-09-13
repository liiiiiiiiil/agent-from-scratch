"""v0.23 read-only planning and user handoff."""

import json
import sys
from unittest.mock import patch

import pytest

from mini_agent.agent import agent_loop
from mini_agent import __main__ as cli
from mini_agent.context import ContextManager
from mini_agent.permission import ALLOW, PermissionGate, PermissionPolicy
from mini_agent.state import AgentState, PlanRejected
from mini_agent.tools import create_registry
from mini_agent.tools.base import ToolExecutor


def _plan(**extra):
    result = {
        "goal": "inspect then edit", "constraints": [],
        "success_criteria": ["verification passes"],
        "steps": [{"step_id": "inspect", "content": "inspect", "depends_on": [],
                   "success_criteria": ["understood"], "replaces": []}],
        "reason": "initial",
    }
    result.update(extra)
    return result


def _call(name, arguments, call_id="c1"):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments),
    }}


def _executor(state, gate=None):
    return ToolExecutor(create_registry(state), gate or PermissionGate(PermissionPolicy({
        "begin_plan": ALLOW, "cancel_planning": ALLOW, "commit_plan": ALLOW,
        "read_file": ALLOW, "list_dir": ALLOW, "write_file": ALLOW, "run_shell": ALLOW,
    })))


def _investigate(state):
    result = _executor(state).execute_result("list_dir", {}, state)
    assert result.ok
    state.record_execution_result(result)


def test_plan_only_requires_successful_read_only_investigation():
    state = AgentState(); state.begin_task("task", mode="plan_only")
    executor = _executor(state)
    rejected = executor.execute_result("commit_plan", _plan(), state)
    assert rejected.error_kind == "plan_rejected"
    assert state.snapshot()["current_generation_id"] == 0
    assert not state.snapshot()["plan_revisions"]
    _investigate(state)
    assert executor.execute_result("commit_plan", _plan(), state).ok


def test_verification_shell_reserves_generation_for_possible_side_effect(tmp_path):
    state = AgentState(); state.begin_task("verify")
    executor = _executor(state)
    target = tmp_path / "written-by-verification.txt"
    result = executor.execute_result("run_shell", {
        "command": f"printf changed > '{target}'", "purpose": "verification",
    }, state)
    state.record_execution_result(result)
    assert result.ok and result.effect_class == "possible"
    assert target.read_text() == "changed"
    snapshot = state.snapshot()
    assert snapshot["current_generation_id"] == 1
    assert snapshot["attempts"][-1]["pre_generation_id"] == 0
    assert snapshot["verification_evidence"][-1]["generation_id"] == 1


def test_auto_entry_and_cancel_keep_direct_path():
    state = AgentState(); state.begin_task("task")
    executor = _executor(state)
    assert executor.execute_result("begin_plan", {}, state).ok
    assert state.snapshot()["planning_state"]["phase"] == "exploring"
    assert executor.execute_result("cancel_planning", {}, state).ok
    assert state.snapshot()["planning_state"]["phase"] == "direct"
    assert state.snapshot()["current_generation_id"] == 0
    assert not state.snapshot()["attempts"]


def test_executor_rejects_effects_and_verification_before_permission_or_handler(tmp_path):
    state = AgentState(); state.begin_task("task", mode="plan_only")
    target = tmp_path / "untouched.txt"

    class Guard:
        def __init__(self): self.calls = []
        def guard(self, name, arguments):
            self.calls.append(name)
            return None

    guard = Guard()
    executor = _executor(state, guard)
    write = executor.execute_result("write_file", {"path": str(target), "content": "bad"}, state)
    verify = executor.execute_result("run_shell", {
        "command": "python -c 'print(1)'", "purpose": "verification",
    }, state)
    recover = executor.execute_result("recover", {}, state)
    for result in (write, verify, recover):
        assert result.permission == "not_checked"
        assert not result.handler_admitted
        assert result.error_kind == "planning_phase_gate" or result.error_kind == "invalid_arguments"
        state.record_execution_result(result)
    assert guard.calls == []
    assert not target.exists()
    snapshot = state.snapshot()
    assert snapshot["current_generation_id"] == 0
    assert not snapshot["failures"] and not snapshot["verification_evidence"]
    assert not snapshot["recovery_actions"]


def test_plan_only_commit_waits_for_current_revision_approval():
    state = AgentState(); state.begin_task("task", mode="plan_only")
    _investigate(state)
    executor = _executor(state)
    committed = executor.execute_result("commit_plan", _plan(), state)
    assert committed.ok
    assert state.planning_state.phase == "awaiting_approval"
    assert state.completion_reminder() is None
    context = ContextManager(state, [{"role": "user", "content": "task"}])
    with patch("mini_agent.agent.call_llm") as llm:
        assert agent_loop(context, executor) == "计划等待用户决定"
    llm.assert_not_called()
    denied = executor.execute_result("read_file", {"path": "missing"}, state)
    assert denied.error_kind == "planning_phase_gate"
    with pytest.raises(PlanRejected): state.decide_plan("approved", 2)
    state.decide_plan("approved", 1)
    assert state.planning_state.phase == "executing"
    with pytest.raises(PlanRejected): state.commit_plan(**_plan(
        parent_revision_id=1, goal="unapproved replacement"))
    with pytest.raises(PlanRejected): state.decide_plan("approved", 1)
    assert state.snapshot()["user_plan_decisions"][-1]["decision"] == "approved"


def test_approval_does_not_authorize_write(tmp_path):
    state = AgentState(); state.begin_task("task", mode="plan_only")
    _investigate(state)
    state.commit_plan(**_plan())
    state.decide_plan("approved", 1)
    target = tmp_path / "not-created.txt"
    executor = ToolExecutor(create_registry(state), PermissionGate(PermissionPolicy({
        "write_file": "deny",
    })))
    result = executor.execute_result("write_file", {
        "path": str(target), "content": "no",
    }, state)
    assert result.permission == "denied"
    assert not result.handler_admitted and not target.exists()


def test_feedback_revision_and_unchanged_review_paths():
    state = AgentState(); state.begin_task("task", mode="plan_only")
    _investigate(state)
    state.commit_plan(**_plan())
    state.decide_plan("continue_exploring", 1, "inspect another module")
    trigger = state.planning_state.active_trigger_id
    assert state.snapshot()["active_plan"]["revision_id"] == 1
    with pytest.raises(PlanRejected): state.commit_plan(**_plan(
        parent_revision_id=1, goal="new goal"))
    assert state.planning_state.active_trigger_id == trigger
    state.review_current_plan(1)
    assert state.planning_state.phase == "awaiting_approval"
    assert state.snapshot()["replan_triggers"][-1]["status"] == "rejected"
    state.decide_plan("approved", 1)

    second = AgentState(); second.begin_task("task", mode="plan_only")
    _investigate(second)
    second.commit_plan(**_plan())
    second.decide_plan("rejected", 1, "need a second check")
    with pytest.raises(PlanRejected): second.review_current_plan(1)
    trigger = second.planning_state.active_trigger_id
    revision = second.commit_plan(**_plan(
        parent_revision_id=1, trigger_id=trigger,
        steps=[*_plan()["steps"], {
            "step_id": "verify", "content": "verify", "depends_on": ["inspect"],
            "success_criteria": ["passes"], "replaces": [],
        }], reason="incorporate feedback"))
    assert revision.revision_id == 2 and revision.trigger_id == trigger
    assert second.planning_state.phase == "awaiting_approval"
    assert second.snapshot()["replan_triggers"][-1]["result_revision_id"] == 2
    with pytest.raises(PlanRejected): second.decide_plan("approved", 1)


def test_loop_rejects_mixed_commit_and_returns_after_plan_handoff():
    state = AgentState(); state.begin_task("task", mode="plan_only")
    _investigate(state)
    context = ContextManager(state, [{"role": "user", "content": "task"}])
    executor = _executor(state)
    mixed = {"role": "assistant", "content": None, "tool_calls": [
        _call("commit_plan", _plan(), "plan"),
        _call("run_shell", {"command": "python -c 'print(1)'", "purpose": "execution"}, "shell"),
    ]}
    clean = {"role": "assistant", "content": None, "tool_calls": [_call("commit_plan", _plan())]}
    with patch("mini_agent.agent.call_llm", side_effect=[mixed, clean]) as llm:
        assert agent_loop(context, executor) == "计划等待用户决定"
    assert llm.call_count == 2
    results = [item for item in context.history if item.get("role") == "tool"]
    assert len(results) == 3
    assert "plan_rejected" in results[0]["content"]
    assert "规划阶段切换" in results[1]["content"]
    assert state.snapshot()["current_generation_id"] == 0
    assert state.planning_state.phase == "awaiting_approval"


def test_mixed_begin_plan_does_not_run_effect_before_phase_switch(tmp_path):
    state = AgentState(); state.begin_task("task")
    context = ContextManager(state, [{"role": "user", "content": "task"}])
    executor = _executor(state)
    target = tmp_path / "untouched.txt"
    mixed = {"role": "assistant", "content": None, "tool_calls": [
        _call("write_file", {"path": str(target), "content": "bad"}, "write"),
        _call("begin_plan", {}, "begin"),
    ]}
    with patch("mini_agent.agent.call_llm", side_effect=[mixed, {
        "role": "assistant", "content": "finished",
    }]):
        assert agent_loop(context, executor) == "finished"
    assert not target.exists()
    assert state.planning_state.phase == "direct"
    assert len([item for item in context.history if item.get("role") == "tool"]) == 2


def test_continued_investigation_can_pause_for_unchanged_review():
    state = AgentState(); state.begin_task("task", mode="plan_only")
    _investigate(state)
    state.commit_plan(**_plan())
    state.decide_plan("continue_exploring", 1, "look again")
    context = ContextManager(state, [{"role": "user", "content": "look again"}])
    with patch("mini_agent.agent.call_llm", return_value={
        "role": "assistant", "content": "Original plan still fits.",
    }) as llm:
        assert agent_loop(context, _executor(state)) == "Original plan still fits."
    assert llm.call_count == 1
    assert state.status == "running"
    assert state.planning_state.phase == "exploring"
    state.review_current_plan(1)
    assert state.planning_state.phase == "awaiting_approval"


def test_feedback_survives_context_compaction_and_new_task_clears_it():
    state = AgentState(); state.begin_task("task", mode="plan_only")
    _investigate(state)
    state.commit_plan(**_plan())
    state.decide_plan("rejected", 1, "check the dependency first")
    history = [{"role": "user", "content": "task"}]
    context = ContextManager(state, history, summarizer=lambda _: "summary", keep_rounds=1)
    context.compact()
    rendered = next(item["content"] for item in context.prepare_messages()
                    if item.get("content", "").startswith("[Structured State]"))
    assert "check the dependency first" in rendered
    assert "Active plan trigger: 1" in rendered
    state.begin_task("next")
    snapshot = state.snapshot()
    assert not snapshot["user_plan_decisions"] and not snapshot["replan_triggers"]
    assert snapshot["planning_state"]["phase"] == "direct"


def test_cli_plan_flag_and_revision_approval_resume_same_task():
    phases = []

    def fake_loop(context, executor):
        state = context.state
        phases.append(state.planning_state.phase)
        if len(phases) == 1:
            _investigate(state)
            state.commit_plan(**_plan())
            return "计划等待用户决定"
        assert state.planning_state.active_revision_id == 1
        return "继续执行"

    with patch.object(sys, "argv", ["mini_agent", "--plan", "inspect task"]), \
            patch("mini_agent.__main__.agent_loop", side_effect=fake_loop), \
            patch("builtins.input", side_effect=["/approve 1", "exit"]):
        cli.main()
    assert phases == ["exploring", "executing"]


def test_cli_continue_review_then_approve_keeps_revision():
    phases = []

    def fake_loop(context, executor):
        state = context.state
        phases.append(state.planning_state.phase)
        if len(phases) == 1:
            _investigate(state)
            state.commit_plan(**_plan())
            return "计划等待用户决定"
        if len(phases) == 2:
            assert state.planning_state.active_revision_id == 1
            assert state.snapshot()["user_plan_decisions"][-1]["feedback"] == "inspect more"
            return "The plan still fits."
        assert state.planning_state.active_revision_id == 1
        return "Continue execution."

    with patch.object(sys, "argv", ["mini_agent", "--plan", "inspect task"]), \
            patch("mini_agent.__main__.agent_loop", side_effect=fake_loop), \
            patch("builtins.input", side_effect=[
                "/continue 1 inspect more", "/review 1", "/approve 1", "exit",
            ]):
        cli.main()
    assert phases == ["exploring", "exploring", "executing"]


def test_cli_handoff_renders_committed_plan_in_quiet_mode(capsys):
    calls = []

    def fake_loop(context, executor):
        calls.append(context.state.planning_state.phase)
        _investigate(context.state)
        context.state.commit_plan(**_plan(
            goal="实现文件检索器", reason="先确认现有工具",
            constraints=["保持标准库实现"],
            success_criteria=["检索结果通过测试"],
            steps=[
                {"step_id": "inspect", "content": "检查现有文件工具",
                 "depends_on": [], "success_criteria": ["找到现有入口"], "replaces": []},
                {"step_id": "implement", "content": "实现检索器",
                 "depends_on": ["inspect"], "success_criteria": ["测试通过"],
                 "replaces": []},
            ],
        ))
        return "计划等待用户决定"  # No natural-language plan in the model response.

    with patch.object(sys, "argv", ["mini_agent", "--plan", "实现文件检索器"]), \
            patch.object(cli, "OUTPUT_MODE", "quiet"), \
            patch.object(cli, "agent_loop", side_effect=fake_loop), \
            patch.object(cli, "InputSession") as session:
        session.return_value.read.side_effect = ["exit"]
        cli.main()

    output = capsys.readouterr().out
    assert calls == ["exploring"]
    for expected in (
        "计划 revision 1", "目标：实现文件检索器", "制订原因：先确认现有工具",
        "- 保持标准库实现", "任务验收标准：\n- 检索结果通过测试",
        "1. [pending] inspect：检查现有文件工具", "依赖：无",
        "2. [pending] implement：实现检索器", "依赖：inspect",
        "本步骤验收：", "- 测试通过", "/approve 1", "/reject 1 <反馈>",
        "/continue 1 <反馈>",
    ):
        assert expected in output


def test_cli_review_renders_same_plan_again(capsys):
    calls = []

    def fake_loop(context, executor):
        calls.append(context.state.planning_state.phase)
        if len(calls) == 1:
            _investigate(context.state)
            context.state.commit_plan(**_plan(goal="原计划目标"))
        return "计划仍合适"

    with patch.object(sys, "argv", ["mini_agent", "--plan", "inspect task"]), \
            patch.object(cli, "agent_loop", side_effect=fake_loop), \
            patch.object(cli, "InputSession") as session:
        session.return_value.read.side_effect = [
            "/continue 1 再调查", "/review 1", "exit",
        ]
        cli.main()

    output = capsys.readouterr().out
    assert calls == ["exploring", "exploring"]
    assert output.count("目标：原计划目标") == 2
    assert output.count("/approve 1") == 2


def test_plan_renderer_uses_active_projection_and_never_executes():
    state = AgentState(); state.begin_task("task")
    state.begin_plan()
    state.commit_plan(**_plan(goal="第一行\n第二行\x1b[31m"))
    state.update_plan_progress(1, "inspect", "in_progress", "started")
    before = state.snapshot()

    with patch.object(cli, "agent_loop") as loop, \
            patch("mini_agent.agent.call_llm") as llm, \
            patch.object(ToolExecutor, "execute_result") as tool, \
            patch.object(PermissionGate, "guard") as permission:
        rendered = cli._render_plan_for_approval(state)

    assert "目标：第一行\n  第二行\\x1b[31m" in rendered
    assert "1. [in_progress] inspect：inspect" in rendered
    assert state.snapshot() == before
    loop.assert_not_called()
    llm.assert_not_called()
    tool.assert_not_called()
    permission.assert_not_called()


def test_plan_renderer_shows_revision_parent_replacements_and_status():
    state = AgentState(); state.begin_task("task", mode="plan_only")
    _investigate(state)
    state.commit_plan(**_plan())
    state.decide_plan("continue_exploring", 1, "replace inspect")
    trigger = state.planning_state.active_trigger_id
    state.commit_plan(**_plan(
        goal="revised goal", parent_revision_id=1, trigger_id=trigger,
        steps=[{"step_id": "inspect_new", "content": "inspect replacement",
                "depends_on": [], "success_criteria": ["understood"],
                "replaces": ["inspect"]}],
    ))

    rendered = cli._render_plan_for_approval(state)
    assert "计划 revision 2" in rendered
    assert "父 revision：1" in rendered
    assert "替换：inspect" in rendered
    assert "1. [pending] inspect_new：inspect replacement" in rendered
    assert "/approve 2" in rendered
    assert "/approve 1" not in rendered


@pytest.mark.parametrize("active_plan", [None, {"revision_id": 2}])
def test_plan_renderer_reports_missing_or_mismatched_active_plan(active_plan):
    class InconsistentState:
        def snapshot(self):
            return {"planning_state": {"active_revision_id": 1},
                    "active_plan": active_plan}

    rendered = cli._render_plan_for_approval(InconsistentState())
    assert "计划状态异常：待批 revision 1" in rendered
    assert "/approve" not in rendered


def test_cli_handoff_reports_missing_active_plan_without_crashing(capsys):
    def inconsistent_loop(context, executor):
        _investigate(context.state)
        context.state.commit_plan(**_plan())
        context.state.plan_revisions.clear()  # Simulate a corrupt State snapshot.
        return "计划等待用户决定"

    with patch.object(sys, "argv", ["mini_agent", "--plan", "task"]), \
            patch.object(cli, "agent_loop", side_effect=inconsistent_loop), \
            patch.object(cli, "InputSession") as session:
        session.return_value.read.side_effect = ["exit"]
        cli.main()

    output = capsys.readouterr().out
    assert "计划状态异常：待批 revision 1" in output
    assert "/approve 1" not in output
