"""v0.19 checkpoint and bounded rollback tests."""

import json
import os
from pathlib import Path
import stat
import tempfile
from unittest.mock import patch

from mini_agent.checkpoint import CheckpointStore
from mini_agent.permission import ALLOW, PermissionGate, PermissionPolicy
from mini_agent.state import AgentState
from mini_agent.tools import create_registry
from mini_agent.tools.base import ToolExecutor


def _make(tmp_path):
    state = AgentState()
    state.begin_task("checkpoint test")
    registry = create_registry(state, str(tmp_path))
    policy = PermissionPolicy({
        "write_file": ALLOW,
        "edit_file": ALLOW,
        "run_shell": ALLOW,
        "recover": ALLOW,
        "rollback_checkpoint": ALLOW,
    })
    return state, ToolExecutor(registry, PermissionGate(policy))


def _record(executor, state, name, arguments):
    result = executor.execute_result(name, arguments, state)
    # The agent protocol records the internal recovery target, not the
    # control-plane recover tool call itself.
    if name != "recover":
        state.record_execution_result(result)
    return result


def _fail_verification(executor, state):
    _record(executor, state, "run_shell", {
        "command": "false", "purpose": "verification",
    })


def test_existing_file_checkpoint_restores_bytes_and_mode():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / "sample.txt"
        path.write_bytes(b"before\n")
        os.chmod(path, 0o640)
        state, executor = _make(root)

        _record(executor, state, "write_file", {
            "path": str(path), "content": "after\n",
        })
        checkpoint = state.snapshot()["checkpoints"][0]
        assert checkpoint["before_type"] == "regular_file"
        assert checkpoint["before_sha256"]
        assert checkpoint["after_type"] == "regular_file"
        assert checkpoint["mode"] == 0o640
        assert checkpoint["status"] == "ready"
        assert "before\n" not in json.dumps(checkpoint)
        assert str(root) not in json.dumps(checkpoint)

        _fail_verification(executor, state)
        result = _record(executor, state, "recover", {
            "action": "rollback", "caused_by_failure_id": "f-1",
            "reason": "restore the known-good file", "checkpoint_id": "cp-1",
        })

        assert json.loads(result.output)["status"] == "executed"
        assert path.read_bytes() == b"before\n"
        assert stat.S_IMODE(path.stat().st_mode) == 0o640
        snapshot = state.snapshot()
        assert snapshot["current_generation_id"] == 2
        assert snapshot["checkpoints"][0]["status"] == "restored"
        assert snapshot["recovery_actions"][0]["checkpoint_id"] == "cp-1"
        assert snapshot["attempts"][-1]["tool"] == "rollback_checkpoint"
        assert snapshot["attempts"][-1]["effect_class"] == "possible"


def test_absent_tombstone_removes_new_file_only_when_unchanged():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / "new.txt"
        state, executor = _make(root)
        _record(executor, state, "write_file", {
            "path": str(path), "content": "created",
        })
        assert state.snapshot()["checkpoints"][0]["before_type"] == "absent"
        _fail_verification(executor, state)
        _record(executor, state, "recover", {
            "action": "rollback", "caused_by_failure_id": "f-1",
            "reason": "remove created file", "checkpoint_id": "cp-1",
        })
        assert not path.exists()
        assert state.status == "running"


def test_external_change_rejects_without_overwriting_and_blocks():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / "conflict.txt"
        path.write_text("before", encoding="utf-8")
        state, executor = _make(root)
        _record(executor, state, "edit_file", {
            "path": str(path), "old_string": "before", "new_string": "agent",
        })
        path.write_text("external", encoding="utf-8")
        _fail_verification(executor, state)

        result = _record(executor, state, "recover", {
            "action": "rollback", "caused_by_failure_id": "f-1",
            "reason": "detect external edit", "checkpoint_id": "cp-1",
        })

        assert json.loads(result.output)["status"] == "executed"
        assert path.read_text(encoding="utf-8") == "external"
        snapshot = state.snapshot()
        assert snapshot["status"] == "blocked"
        assert snapshot["failures"][-1]["category"] == "unknown"
        assert snapshot["failures"][-1]["affected_files"] == ("conflict.txt",)
        assert snapshot["attempts"][-1]["error_kind"] == "rollback_conflict"
        assert snapshot["checkpoints"][0]["status"] == "ready"


def test_invalid_rollback_is_rejected_once_and_blocks_without_generation():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state, executor = _make(root)
        _record(executor, state, "run_shell", {
            "command": "false", "purpose": "verification",
        })
        generation = state.current_generation_id
        result = _record(executor, state, "recover", {
            "action": "rollback", "caused_by_failure_id": "f-1",
            "reason": "missing checkpoint", "checkpoint_id": "missing",
        })
        payload = json.loads(result.output)
        assert payload["status"] == "rejected"
        assert len(state.snapshot()["recovery_actions"]) == 1
        assert state.snapshot()["status"] == "blocked"
        assert state.current_generation_id == generation


def test_internal_rollback_tool_is_not_in_schema_or_directly_callable():
    with tempfile.TemporaryDirectory() as directory:
        state, executor = _make(Path(directory))
        names = [item["function"]["name"] for item in executor.registry.schemas()]
        assert "rollback_checkpoint" not in names
        result = executor.execute_result("rollback_checkpoint", {
            "checkpoint_id": "cp-1",
        }, state)
        assert result.error_kind == "internal_tool"
        assert not result.handler_admitted


def test_unavailable_checkpoint_does_not_stop_original_write():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "root"
        outside = Path(directory) / "outside"
        root.mkdir()
        outside.mkdir()
        path = outside / "outside.txt"
        state, executor = _make(root)
        result = _record(executor, state, "write_file", {
            "path": str(path), "content": "still written",
        })
        assert result.ok
        assert path.read_text(encoding="utf-8") == "still written"
        checkpoint = state.snapshot()["checkpoints"][0]
        assert checkpoint["status"] == "unavailable"
        assert "工作区之外" in checkpoint["unavailable_reason"]


def test_checkpoint_size_boundary_and_reset():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / "exact.bin"
        path.write_bytes(b"x" * 8)
        store = CheckpointStore(str(root), max_bytes=8)
        capture = store.capture_before("a-1", 1, str(path))
        checkpoint = store.capture_after(capture)
        assert checkpoint.status == "ready"
        assert checkpoint.before_sha256
        store.clear()
        assert store.snapshot() == []
        assert store.get("cp-1") is None

        path.write_bytes(b"x" * 9)
        capture = store.capture_before("a-1", 1, str(path))
        checkpoint = store.capture_after(capture)
        assert checkpoint.status == "unavailable"
        assert "超过" in (checkpoint.unavailable_reason or "")


def test_restore_failure_marks_checkpoint_and_preserves_file():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / "failure.txt"
        path.write_text("before", encoding="utf-8")
        state, executor = _make(root)
        _record(executor, state, "write_file", {
            "path": str(path), "content": "after",
        })
        _fail_verification(executor, state)
        with patch("mini_agent.checkpoint.os.replace", side_effect=OSError("replace failed")):
            result = _record(executor, state, "recover", {
                "action": "rollback", "caused_by_failure_id": "f-1",
                "reason": "exercise interrupted restore", "checkpoint_id": "cp-1",
            })
        assert result.ok
        assert state.status == "blocked"
        assert path.read_text(encoding="utf-8") == "after"
        assert state.snapshot()["attempts"][-1]["error_kind"] == "rollback_restore_failed"
        assert state.snapshot()["checkpoints"][0]["status"] == "restore_failed"
