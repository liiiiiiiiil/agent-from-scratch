"""Unit tests for AgentState.

可独立运行：PYTHONPATH=src python tests/test_state.py
"""

import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mini_agent.state import AgentState


def test_defaults_are_independent():
    first = AgentState()
    second = AgentState()

    assert first.task == ""
    assert first.current_goal == ""
    assert first.tool_history == []
    assert first.files_changed == []
    assert first.errors == []
    assert first.status == "running"

    first.tool_history.append({"tool": "calculate"})
    first.files_changed.append("first.py")
    first.errors.append("error")
    assert second.tool_history == []
    assert second.files_changed == []
    assert second.errors == []


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
    state = AgentState(task="update app", current_goal="edit main.py")
    state.record_tool("write_file", {"path": "main.py"}, True, "written")

    snapshot = state.snapshot()
    snapshot["tool_history"][0]["args"]["path"] = "changed.py"
    snapshot["tool_history"].append({"tool": "fake"})
    snapshot["files_changed"].append("fake.py")
    snapshot["errors"].append("fake error")

    assert snapshot["task"] == "update app"
    assert snapshot["current_goal"] == "edit main.py"
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
    print("\n全部 state test 通过")
