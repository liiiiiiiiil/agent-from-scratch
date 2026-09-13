"""Unit tests for AgentState.

可独立运行：PYTHONPATH=src python tests/test_state.py
"""

import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mini_agent.state import AgentState


def _step(step_id, depends_on=None):
    return {
        "step_id": step_id,
        "content": step_id,
        "depends_on": depends_on or [],
        "success_criteria": [f"{step_id} done"],
        "replaces": [],
    }


def _commit(state, steps):
    state.commit_plan(
        goal="test plan", constraints=[], success_criteria=["checks pass"],
        steps=steps, reason="test",
    )


def test_defaults_are_independent():
    first = AgentState()
    second = AgentState()

    assert first.task == ""
    assert first.current_goal == ""
    assert first.tool_history == []
    assert first.files_changed == []
    assert first.errors == []
    assert first.status == "running"
    assert first.todos == []

    first.tool_history.append({"tool": "calculate"})
    first.files_changed.append("first.py")
    first.errors.append("error")
    assert second.tool_history == []
    assert second.files_changed == []
    assert second.errors == []


def test_todo_updates_are_atomic_and_derive_current_goal():
    state = AgentState()
    _commit(state, [_step("inspect-code"), _step("run-tests")])
    state.update_plan_progress(1, "run-tests", "in_progress", "start")
    assert state.snapshot()["todos"] == [
        {"content": "inspect-code", "status": "pending"},
        {"content": "run-tests", "status": "in_progress"},
    ]
    before = state.snapshot()
    try:
        state.update_plan_progress(1, "inspect-code", "in_progress", "parallel")
        assert False, "重复 in_progress 应拒绝"
    except ValueError:
        pass
    assert state.snapshot() == before


def test_todo_does_not_enter_execution_state():
    state = AgentState()
    state.record_tool("commit_plan", {}, True, "committed")
    state.record_tool("update_plan_progress", {}, False, "rejected")
    assert state.snapshot()["tool_history"] == []
    assert state.snapshot()["errors"] == []

def test_invalid_todo_shapes_leave_snapshot_unchanged():
    state = AgentState()
    _commit(state, [_step("keep")])
    before = state.snapshot()
    invalid = [
        None,
        [_step("" )],
        [_step("x", depends_on=["missing"])],
        [_step("x")] * 51,
        [{**_step("x"), "content": "x" * 241}],
        [{**_step("x"), "success_criteria": []}],
    ]
    for value in invalid:
        try:
            state.commit_plan(
                goal="test plan", constraints=[], success_criteria=["ok"],
                steps=value, reason="bad", parent_revision_id=1,
            )
        except (TypeError, ValueError):
            pass
        else:
            raise AssertionError("非法 Todo 应拒绝")
        assert state.snapshot() == before

def test_verification_evidence_generation_and_purpose():
    state = AgentState()
    state.record_tool("run_shell", {"command": "make", "purpose": "execution"}, True, "[exit=0] ok")
    assert not state.has_verification_evidence()


def test_execution_shell_invalidates_evidence_even_without_file_changes():
    state = AgentState()
    state.record_tool("run_shell", {"command": "pytest", "purpose": "verification"}, True, "[exit=0] passed")
    assert state.has_verification_evidence()
    state.record_tool("run_shell", {"command": "echo ok", "purpose": "execution"}, True, "[exit=0] ok")
    assert not state.has_verification_evidence()
    assert state.completion_reminder()["verification_required"] is True


def test_execution_failure_invalidates_but_permission_rejection_does_not():
    state = AgentState()
    state.record_tool("run_shell", {"command": "pytest", "purpose": "verification"}, True, "[exit=0] passed")
    state.record_tool("run_shell", {"command": "false", "purpose": "execution"}, False, "[exit=1] failed")
    assert not state.has_verification_evidence()
    state.record_tool("run_shell", {"command": "pytest", "purpose": "verification"}, True, "[exit=0] passed")
    state.record_tool("run_shell", {"command": "rm *", "purpose": "execution"}, False, "权限拒绝: 用户拒绝执行 run_shell")
    assert state.has_verification_evidence()


def test_failed_verification_requires_retry():
    state = AgentState()
    state.record_tool("run_shell", {"command": "pytest", "purpose": "verification"}, False, "[exit=1] failed")
    reminder = state.completion_reminder()
    assert reminder is not None and reminder["verification_required"] is True
    state.record_tool("run_shell", {"command": "pytest", "purpose": "verification"}, True, "[exit=0] passed")
    assert state.has_verification_evidence()
    state.record_tool("write_file", {"path": "a.py"}, True, "written")
    assert not state.has_verification_evidence()
    state.record_tool("run_shell", {"command": "pytest", "purpose": "verification"}, True, "[exit=1] failed")
    assert not state.has_verification_evidence()
    state.record_tool("run_shell", {"command": "pytest", "purpose": "verification"}, True, "[timeout] 命令超时")
    assert not state.has_verification_evidence()


def test_completion_progress_marker_tracks_real_facts_and_ignores_duplicate_todo():
    state = AgentState(task="progress")
    _commit(state, [_step("inspect")])
    first = state.completion_reminder()["progress_marker"]
    try:
        state.commit_plan(
            goal="test plan", constraints=[], success_criteria=["checks pass"],
            steps=[_step("inspect")], reason="duplicate", parent_revision_id=1,
        )
    except ValueError:
        pass
    assert state.completion_reminder()["progress_marker"] == first

    state.update_plan_progress(1, "inspect", "in_progress", "start")
    todo_progress = state.completion_reminder()["progress_marker"]
    assert todo_progress != first

    state.record_tool("read_file", {"path": "a.py"}, False, "not found")
    tool_progress = state.completion_reminder()["progress_marker"]
    assert tool_progress != todo_progress

    state.record_tool("run_shell", {"command": "check", "purpose": "verification"}, True, "[exit=0] ok")
    assert state.completion_reminder()["progress_marker"] != tool_progress


def test_completed_todos_and_current_passed_verification_allow_finish():
    state = AgentState(task="finish")
    _commit(state, [_step("inspect")])
    state.update_plan_progress(1, "inspect", "in_progress", "start")
    state.update_plan_progress(1, "inspect", "completed", "done")
    state.record_tool("run_shell", {"command": "check", "purpose": "verification"}, True, "[exit=0] ok")
    assert state.completion_reminder() is None

def test_begin_task_resets_runtime_state_and_completion_reminder():
    state = AgentState(task="old")
    _commit(state, [_step("done")])
    state.update_plan_progress(1, "done", "in_progress", "start")
    state.update_plan_progress(1, "done", "completed", "done")
    state.record_tool("write_file", {"path": "a"}, True, "written")
    assert state.completion_reminder() is not None
    state.begin_task("new")
    assert state.snapshot()["task"] == "new"
    assert state.snapshot()["todos"] == []
    assert state.snapshot()["files_changed"] == []
    assert state.completion_reminder() is None


def test_reset_task_clears_stage_six_state_in_place():
    state = AgentState()
    state.begin_task("old")
    _commit(state, [_step("old-todo")])
    state.update_plan_progress(1, "old-todo", "in_progress", "start")
    state.begin_task("new")
    snapshot = state.snapshot()
    assert snapshot["task"] == "new"
    assert snapshot["todos"] == []
    assert snapshot["failures"] == []
    assert snapshot["recovery_actions"] == []
    assert snapshot["attempts"] == []
    assert snapshot["current_generation_id"] == 0
    state.reset_task()
    assert state.snapshot()["task"] == ""
    assert state.snapshot()["failures"] == []


def test_record_success_and_failure():
    state = AgentState()

    state.record_tool("calculate", {"expression": "1 + 1"}, True, "2")
    state.record_tool("run_shell", {"command": "false"}, False, "exit code 1")

    assert state.tool_history == [
        {
            "tool": "calculate",
            "args": {"expression": "1 + 1"},
            "ok": True,
            "brief": "2",
        },
        {
            "tool": "run_shell",
            "args": {"command": "false"},
            "ok": False,
            "brief": "exit code 1",
        },
    ]
    assert state.errors == ["run_shell: exit code 1"]


def test_files_changed_are_deduplicated_in_first_seen_order():
    state = AgentState()

    state.record_tool("write_file", {"path": "a.py"}, True, "written")
    state.record_tool("edit_file", {"path": "b.py"}, True, "edited")
    state.record_tool("write_file", {"path": "a.py"}, True, "written again")

    assert state.files_changed == ["a.py", "b.py"]


def test_only_string_paths_are_recorded_as_file_changes():
    state = AgentState()

    state.record_tool("write_file", {"path": 123}, True, "written")
    state.record_tool("edit_file", {"path": None}, True, "edited")
    state.record_tool("write_file", {"path": ["not", "a", "path"]}, True, "written")

    assert state.files_changed == []


def test_failed_file_tool_does_not_record_file_change():
    state = AgentState()

    state.record_tool("write_file", {"path": "failed.py"}, False, "write failed")
    state.record_tool("edit_file", {"path": "also-failed.py"}, False, "edit failed")

    assert state.files_changed == []
    assert len(state.errors) == 2


def test_recorded_args_are_independent_copies():
    state = AgentState()
    args = {"path": "main.py", "options": {"mode": "safe"}}

    state.record_tool("write_file", args, True, "written")
    args["path"] = "changed.py"
    args["options"]["mode"] = "changed"

    assert state.tool_history[0]["args"] == {
        "path": "main.py",
        "options": {"mode": "safe"},
    }
    assert state.files_changed == ["main.py"]


def test_snapshot_is_consistent_and_independent():
    state = AgentState(task="update app")
    state.record_tool("write_file", {"path": "main.py"}, True, "written")

    snapshot = state.snapshot()
    snapshot["tool_history"][0]["args"]["path"] = "changed.py"
    snapshot["tool_history"].append({"tool": "fake"})
    snapshot["files_changed"].append("fake.py")
    snapshot["errors"].append("fake error")

    assert snapshot["task"] == "update app"
    assert snapshot["current_goal"] == ""
    assert snapshot["status"] == "running"
    assert state.tool_history[0]["args"]["path"] == "main.py"
    assert state.tool_history != snapshot["tool_history"]
    assert state.files_changed == ["main.py"]
    assert state.errors == []


def test_concurrent_record_tool_updates_are_safe():
    state = AgentState()

    def record(index):
        state.record_tool(
            "write_file",
            {"path": f"file-{index % 10}.py", "index": index},
            True,
            "written",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(record, range(200)))

    snapshot = state.snapshot()
    assert len(snapshot["tool_history"]) == 200
    assert len({entry["args"]["index"] for entry in snapshot["tool_history"]}) == 200
    assert len(snapshot["files_changed"]) == 10
    assert set(snapshot["files_changed"]) == {f"file-{index}.py" for index in range(10)}


if __name__ == "__main__":
    test_defaults_are_independent()
    test_record_success_and_failure()
    test_files_changed_are_deduplicated_in_first_seen_order()
    test_only_string_paths_are_recorded_as_file_changes()
    test_failed_file_tool_does_not_record_file_change()
    test_recorded_args_are_independent_copies()
    test_snapshot_is_consistent_and_independent()
    test_concurrent_record_tool_updates_are_safe()
    test_todo_updates_are_atomic_and_derive_current_goal()
    test_todo_does_not_enter_execution_state()
    test_invalid_todo_shapes_leave_snapshot_unchanged()
    test_verification_evidence_generation_and_purpose()
    test_begin_task_resets_runtime_state_and_completion_reminder()
    test_completion_progress_marker_tracks_real_facts_and_ignores_duplicate_todo()
    test_completed_todos_and_current_passed_verification_allow_finish()
    print("\n全部 state test 通过")
