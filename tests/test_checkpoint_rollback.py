"""v0.19 checkpoint and bounded rollback tests."""

import json
import os
from pathlib import Path
import stat
import tempfile
from unittest.mock import patch

from mini_agent.checkpoint import CheckpointStore
from mini_agent.context import ContextBudget, ContextManager
from mini_agent.permission import ALLOW, PermissionGate, PermissionPolicy
from mini_agent.state import AgentState
from mini_agent.tools import create_registry
from mini_agent.tools.base import Tool, ToolExecutor


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
        result = state.snapshot()["tool_history"][-1]
        assert "checkpoint_id=cp-1" in result["brief"]
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


def test_partial_file_handler_failure_remains_recoverable():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / "partial.txt"
        path.write_text("before", encoding="utf-8")
        state, executor = _make(root)
        tool = executor.registry.get("write_file")
        original_handler = tool.handler

        def partial_write(path, content):
            Path(path).write_text("partial", encoding="utf-8")
            raise RuntimeError("write interrupted")

        tool.handler = partial_write
        try:
            result = _record(executor, state, "write_file", {
                "path": str(path), "content": "after",
            })
        finally:
            tool.handler = original_handler

        assert not result.ok
        assert path.read_text(encoding="utf-8") == "partial"
        snapshot = state.snapshot()
        assert snapshot["status"] == "running"
        assert snapshot["checkpoints"][0]["status"] == "ready"
        assert "cp-1" in snapshot["recovery_notice"]
        assert "checkpoint_id=cp-1" in result.output

        _fail_verification(executor, state)
        rollback = _record(executor, state, "recover", {
            "action": "rollback", "caused_by_failure_id": "f-1",
            "reason": "restore after interrupted write", "checkpoint_id": "cp-1",
        })
        assert json.loads(rollback.output)["status"] == "executed"
        assert path.read_text(encoding="utf-8") == "before"


def test_recovery_rejects_internal_adjust_before_generation_activation():
    with tempfile.TemporaryDirectory() as directory:
        state, executor = _make(Path(directory))
        _fail_verification(executor, state)
        generation = state.current_generation_id

        result = _record(executor, state, "recover", {
            "action": "adjust", "caused_by_failure_id": "f-1",
            "reason": "try internal target",
            "requested_tool": "rollback_checkpoint",
            "requested_arguments": {"checkpoint_id": "cp-1"},
        })

        assert json.loads(result.output)["status"] == "rejected"
        snapshot = state.snapshot()
        assert snapshot["status"] == "running"
        assert state.current_generation_id == generation
        assert snapshot["recovery_actions"][-1]["status"] == "rejected"
        assert snapshot["recovery_actions"][-1]["requested_tool"] == "rollback_checkpoint"


def test_recovery_rejects_internal_retry_before_generation_activation():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state, executor = _make(root)
        executor.registry.register(Tool(
            "internal_timeout", "test-only internal tool", {"type": "object"},
            lambda: "[timeout] test", internal=True,
        ))
        reservation = state.reserve_attempt("none", "internal_timeout", {})
        failed = executor.execute_internal_result(
            "internal_timeout", {}, state=state, notify=False, reservation=reservation,
            permission_already_checked=True,
        )
        attempt = state.record_execution_result(failed)
        generation = state.current_generation_id

        result = _record(executor, state, "recover", {
            "action": "retry", "caused_by_failure_id": attempt.failure_id,
            "reason": "try internal retry", "requested_attempt": attempt.attempt_id,
        })

        assert json.loads(result.output)["status"] == "rejected"
        snapshot = state.snapshot()
        assert snapshot["status"] == "running"
        assert state.current_generation_id == generation
        assert snapshot["recovery_actions"][-1]["status"] == "rejected"


def test_zero_mode_is_restored_as_zero():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / "mode-zero.txt"
        path.write_bytes(b"before")
        store = CheckpointStore(str(root))
        with patch("mini_agent.checkpoint.stat.S_IMODE", return_value=0):
            capture = store.capture_before("a-1", 1, str(path))
            checkpoint = store.capture_after(capture)
        assert checkpoint.mode == 0
        path.write_bytes(b"before")
        store.restore(checkpoint.checkpoint_id)
        assert stat.S_IMODE(path.stat().st_mode) == 0


def test_checkpoint_metadata_is_visible_after_compaction_and_long_state_fallback():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / "visible.txt"
        path.write_text("before", encoding="utf-8")
        state, executor = _make(root)
        _record(executor, state, "write_file", {
            "path": str(path), "content": "after",
        })
        state.task = "t" * 1200
        state.current_goal = "g" * 800
        state.files_changed.extend(f"file-{index}.py" for index in range(20))
        state.errors.append("e" * 20000)
        state.update_todos([{"content": f"todo-{index}"} for index in range(50)])
        history = [{"role": "user", "content": "task"}]
        for index in range(4):
            call_id = f"call-{index}"
            history.extend([
                {"role": "assistant", "content": None, "tool_calls": [{"id": call_id}]},
                {"role": "tool", "tool_call_id": call_id, "content": "result"},
            ])
        context = ContextManager(
            state, history, budget=ContextBudget(window=80, output_reserve_ratio=0, history_ratio=0.5),
            summarizer=lambda _: "summary", keep_rounds=1,
        )

        first = context._render_state()["content"]
        assert len(first) <= 6000
        assert "cp-1" in first
        assert "path=visible.txt" in first
        assert "attempt=a-1" in first
        assert "generation=1" in first
        assert "status=ready" in first
        assert context.compact()
        second = context._render_state()["content"]
        assert len(second) <= 6000
        assert "cp-1" in second
        assert "Rollback checkpoints (ready): cp-1" in second
