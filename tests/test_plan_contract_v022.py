"""v0.22 Plan Contract coverage."""

from copy import deepcopy
import json
from concurrent.futures import ThreadPoolExecutor
import tempfile

import pytest

from mini_agent.context import ContextManager, STRUCTURED_STATE_MAX_CHARS
from mini_agent.checkpoint import CheckpointStore
from mini_agent.permission import ALLOW, PermissionGate, PermissionPolicy
from mini_agent.state import (
    AgentState,
    PlanProgressEvent,
    PlanRevision,
    PlanStep,
    PlanningState,
    PlanRejected,
)
from mini_agent.tools import create_registry
from mini_agent.tools.base import Tool, ToolExecutor


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
        "goal": "finish the task",
        "constraints": [],
        "success_criteria": ["verification passes"],
        "steps": [_step("inspect"), _step("edit", depends_on=["inspect"])],
        "reason": "initial plan",
    }
    value.update(overrides)
    return value


def _executor(state):
    registry = create_registry(state)
    policy = PermissionPolicy({"commit_plan": ALLOW, "update_plan_progress": ALLOW})
    return ToolExecutor(registry, PermissionGate(policy))


def test_plan_dataclasses_are_frozen_and_registry_replaces_old_protocol():
    assert PlanStep.__dataclass_params__.frozen
    assert PlanRevision.__dataclass_params__.frozen
    assert PlanProgressEvent.__dataclass_params__.frozen
    assert PlanningState.__dataclass_params__.frozen

    state = AgentState()
    names = {tool.name for tool in create_registry(state).list_tools()}
    assert {"commit_plan", "update_plan_progress"} <= names
    assert "update_todo" not in names


def test_non_plan_plan_rejected_is_recorded_as_a_normal_failed_attempt():
    state = AgentState()
    state.begin_task("task")
    registry = create_registry(state)
    registry.register(Tool(
        name="possible_custom",
        description="custom possible tool",
        parameters={"type": "object", "properties": {}},
        handler=lambda: (_ for _ in ()).throw(PlanRejected("handler rejected")),
        effect_class="possible",
    ))
    executor = ToolExecutor(
        registry,
        PermissionGate(PermissionPolicy({"possible_custom": ALLOW})),
    )

    result = executor.execute_result("possible_custom", {}, state)
    assert result.error_kind == "handler_exception"
    assert result.reservation is not None
    state.record_execution_result(result)

    snapshot = state.snapshot()
    assert snapshot["current_generation_id"] == 1
    assert snapshot["generations"][-1]["opened_by_attempt_id"] == "a-1"
    assert len(snapshot["attempts"]) == 1
    assert len(snapshot["failures"]) == 1
    assert snapshot["repair_loop"]["phase"] == "diagnosis_required"


def test_initial_commit_progress_and_snapshot_projection():
    state = AgentState()
    state.begin_task("task")
    executor = _executor(state)

    result = executor.execute_result("commit_plan", _plan(), state)
    assert result.ok
    state.record_execution_result(result)
    first = state.snapshot()
    assert first["planning_state"] == {
        "mode": "auto", "phase": "executing", "active_revision_id": 1,
        "active_trigger_id": None, "replans_used": 0, "replans_remaining": None,
    }
    assert first["active_plan"]["steps"][0]["status"] == "pending"
    assert first["current_generation_id"] == 0

    result = executor.execute_result("update_plan_progress", {
        "revision_id": 1, "step_id": "inspect", "status": "in_progress",
        "reason": "start",
    }, state)
    state.record_execution_result(result)
    assert state.snapshot()["current_goal"] == "inspect"
    result = executor.execute_result("update_plan_progress", {
        "revision_id": 1, "step_id": "inspect", "status": "completed",
        "reason": "done",
    }, state)
    state.record_execution_result(result)
    assert state.snapshot()["active_plan"]["steps"][0]["status"] == "completed"
    assert state.snapshot()["active_plan"]["steps"][1]["status"] == "pending"
    assert state.snapshot()["plan_progress_history"][-1]["generation_id"] == 0


def test_revision_inherits_status_and_preserves_old_revision():
    state = AgentState()
    state.begin_task("task")
    state.commit_plan(**_plan())
    state.update_plan_progress(1, "inspect", "in_progress", "start")
    state.update_plan_progress(1, "inspect", "completed", "done")
    state.commit_plan(
        goal="finish the revised task",
        constraints=[],
        success_criteria=["verification passes"],
        steps=[_step("inspect"), _step("replace", depends_on=["inspect"], replaces=["edit"])],
        reason="replace edit step",
        parent_revision_id=1,
    )
    snapshot = state.snapshot()
    assert len(snapshot["plan_revisions"]) == 2
    assert snapshot["plan_revisions"][0]["steps"][0]["status"] == "pending"
    assert snapshot["active_plan"]["steps"][0]["status"] == "completed"
    assert snapshot["active_plan"]["steps"][1]["status"] == "pending"
    assert snapshot["plan_revisions"][1]["generation_id"] == 0


@pytest.mark.parametrize("bad", [
    {"steps": [_step("x"), _step("x")]},
    {"steps": [_step("x", depends_on=["missing"])]},
    {"steps": [_step("x", depends_on=["x"])]},
    {"steps": [_step("a", depends_on=["b"]), _step("b", depends_on=["a"])]},
    {"steps": [_step("1bad")]},
    {"steps": [_step("x", content="x" * 241)]},
    {"success_criteria": []},
])
def test_rejected_plan_keeps_complete_snapshot_unchanged(bad):
    state = AgentState()
    state.begin_task("task")
    state.commit_plan(**_plan())
    before = deepcopy(state.snapshot())
    values = _plan(parent_revision_id=1, reason="change", goal="changed")
    values.update(bad)
    with pytest.raises((ValueError, PlanRejected)):
        state.commit_plan(**values)
    assert state.snapshot() == before


def test_progress_rejections_are_atomic_and_plan_rejection_is_not_failure():
    state = AgentState()
    state.begin_task("task")
    executor = _executor(state)
    result = executor.execute_result("commit_plan", _plan(), state)
    state.record_execution_result(result)
    before = deepcopy(state.snapshot())
    result = executor.execute_result("update_plan_progress", {
        "revision_id": 1, "step_id": "edit", "status": "in_progress", "reason": "too early",
    }, state)
    assert result.error_kind == "plan_rejected"
    assert json.loads(result.output)["status"] == "plan_rejected"
    assert state.snapshot() == before
    assert state.snapshot()["failures"] == []

    result = executor.execute_result("commit_plan", _plan(parent_revision_id=1), state)
    assert result.error_kind == "plan_rejected"
    assert state.snapshot() == before


def test_direct_path_keeps_no_plan_completion_and_current_goal_read_only():
    state = AgentState(task="simple")
    assert state.snapshot()["planning_state"]["phase"] == "direct"
    assert state.snapshot()["current_goal"] == ""
    with pytest.raises(AttributeError):
        state.current_goal = "not a plan step"
    assert state.completion_reminder() is None


def test_structured_state_contains_bounded_plan_execution_view():
    state = AgentState()
    state.begin_task("task")
    state.commit_plan(**_plan(steps=[_step("inspect"), _step("edit", depends_on=["inspect"])]))
    state.update_plan_progress(1, "inspect", "in_progress", "start")
    content = ContextManager(state, [{"role": "user", "content": "task"}]).prepare_messages()[0]["content"]
    assert len(content) <= STRUCTURED_STATE_MAX_CHARS
    assert "active_revision=1" in content
    assert "Current plan step" in content
    assert "success_criteria" in content
    assert "Plan counts" in content
    assert "Todos:" not in content


def test_second_state_degradation_keeps_current_step_when_constraints_exist():
    with tempfile.TemporaryDirectory() as directory:
        state = AgentState()
        state.begin_task("t" * 1200)
        state.bind_checkpoint_store(CheckpointStore(directory))
        current_step = _step("inspect", content="i" * 240)
        current_step["success_criteria"] = ["z" * 240 for _ in range(10)]
        state.commit_plan(
            goal="g" * 1200,
            constraints=["c" * 240],
            success_criteria=["s" * 240],
            steps=[
                current_step,
                *[_step(f"step_{index}", content="x" * 240) for index in range(1, 50)],
            ],
            reason="populate long plan",
        )
        state.update_plan_progress(1, "inspect", "in_progress", "start")
        state.errors.append("e" * 20000)
        for index in range(10):
            path = f"checkpoint_{index}_" + ("x" * 200)
            capture = state.checkpoint_store.capture_before(f"a-{index + 1}", 0, path)
            state.checkpoint_store.capture_after(capture)
        rendered = ContextManager(
            state, [{"role": "user", "content": "task"}],
        )._render_state()["content"]

        assert len(rendered) <= STRUCTURED_STATE_MAX_CHARS
        assert "Current plan step:" in rendered


def test_plan_tool_schema_declares_array_items_and_transition_targets():
    state = AgentState()
    schemas = {
        tool["function"]["name"]: tool["function"]["parameters"]
        for tool in create_registry(state).schemas()
        if tool["function"]["name"] in {"commit_plan", "update_plan_progress"}
    }
    commit = schemas["commit_plan"]["properties"]
    assert commit["constraints"]["items"] == {
        "type": "string", "minLength": 1, "maxLength": 240,
    }
    assert commit["success_criteria"]["items"]["type"] == "string"
    step = commit["steps"]["items"]["properties"]
    assert step["step_id"]["pattern"] == "^[A-Za-z][A-Za-z0-9_-]{0,63}$"
    assert step["depends_on"]["items"]["type"] == "string"
    assert step["replaces"]["items"]["type"] == "string"
    assert step["success_criteria"]["items"]["type"] == "string"
    assert schemas["update_plan_progress"]["properties"]["status"]["enum"] == [
        "in_progress", "completed",
    ]
    assert schemas["update_plan_progress"]["properties"]["step_id"]["pattern"] == (
        "^[A-Za-z][A-Za-z0-9_-]{0,63}$"
    )


def test_concurrent_commit_and_progress_have_one_valid_winner():
    state = AgentState()
    state.begin_task("task")
    plan = _plan()
    with ThreadPoolExecutor(max_workers=2) as pool:
        commits = list(pool.map(lambda _: _try_commit(state, plan), range(2)))
    assert sum(item is None for item in commits) == 1
    assert len(state.snapshot()["plan_revisions"]) == 1

    with ThreadPoolExecutor(max_workers=2) as pool:
        progress = list(pool.map(
            lambda _: _try_progress(state, 1, "inspect", "in_progress"), range(2)
        ))
    assert sum(item is None for item in progress) == 1
    assert state.snapshot()["active_plan"]["steps"][0]["status"] == "in_progress"


def _try_commit(state, plan):
    try:
        return state.commit_plan(**plan)
    except ValueError:
        return None


def _try_progress(state, revision_id, step_id, status):
    try:
        return state.update_plan_progress(revision_id, step_id, status, "concurrent")
    except ValueError:
        return None
