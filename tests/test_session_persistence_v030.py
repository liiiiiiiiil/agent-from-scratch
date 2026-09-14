"""v0.30 session format, export boundaries, and atomic storage tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mini_agent.context import ContextManager
from mini_agent.session import (
    SessionBusyError,
    SessionCommitUncertainError,
    SessionError,
    SessionSizeError,
    SessionStore,
    SessionValidationError,
)
from mini_agent.state import AgentState, SessionExportError


def _context(state: AgentState, secret: str = "stdin-secret") -> ContextManager:
    history = [
        {"role": "user", "content": "drive the process"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "write_process",
                    "arguments": json.dumps({
                        "process_id": "proc-1",
                        "input": secret,
                        "close_stdin": False,
                    }),
                },
            }],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "written_bytes=0"},
    ]
    context = ContextManager(state, history)
    context._summary = "older rounds"
    context._compacted = True
    context._summarized_rounds = 2
    context.set_runtime_notice("continue with a tool")
    return context


def _state() -> AgentState:
    state = AgentState()
    state.begin_task("persist a plan")
    state.commit_plan(
        goal="persist a plan", constraints=["keep it local"],
        success_criteria=["state is checked"],
        steps=[{
            "step_id": "inspect",
            "content": "inspect the task",
            "depends_on": [],
            "success_criteria": ["facts recorded"],
            "replaces": [],
        }],
        reason="initial plan",
    )
    state.update_plan_progress(1, "inspect", "in_progress", "start")
    return state


def test_state_and_context_exports_round_trip_authoritative_facts():
    state = _state()
    state._repair_cycles = 2
    state._reserved_repair_cycles = 0
    context = _context(state)

    state_export = state.export_session()
    context_export = context.export_session()

    AgentState.validate_session_export(json.loads(json.dumps(state_export)))
    ContextManager.validate_session_export(json.loads(json.dumps(context_export)))
    assert state_export["plan_revisions"][0]["revision_id"] == 1
    assert state_export["plan_progress_history"][0]["step_id"] == "inspect"
    assert state_export["private"]["next_plan_revision"] == 2
    assert state_export["private"]["repair_phase"] == "idle"
    assert state_export["private"]["repair_cycles"] == 2
    assert state_export["private"]["reserved_repair_cycles"] == 0
    assert context_export["summary"] == "older rounds"
    assert context_export["compacted"] is True
    assert context_export["summarized_rounds"] == 2
    assert context_export["runtime_notice"] == "continue with a tool"
    assert context_export["history"][1]["tool_calls"][0]["id"] == "call-1"
    assert context_export["history"][2]["tool_call_id"] == "call-1"


def test_write_process_input_is_redacted_but_json_shape_and_call_id_remain():
    state = AgentState(task="task")
    context = _context(state, "DO-NOT-PERSIST")
    exported = context.export_session()
    raw = json.dumps(exported, ensure_ascii=False, sort_keys=True)

    assert "DO-NOT-PERSIST" not in raw
    assert exported["summary"] == "older rounds"
    call = exported["history"][1]["tool_calls"][0]
    args = json.loads(call["function"]["arguments"])
    assert call["id"] == "call-1"
    assert args["process_id"] == "proc-1"
    assert args["close_stdin"] is False
    assert args["input"] == "<redacted:write_process.input>"


def test_write_process_input_repeated_in_ordinary_text_rejects_save(tmp_path: Path):
    state = _state()
    context = _context(state, "DO-NOT-PERSIST")
    context.history[1]["content"] = "about to write DO-NOT-PERSIST"
    store = SessionStore(tmp_path)

    with pytest.raises(SessionValidationError, match="其他会话文本") as error:
        store.save(None, state, context, workspace_root=tmp_path)
    assert "DO-NOT-PERSIST" not in str(error.value)
    assert not list(tmp_path.glob("*.json"))


def test_write_process_input_repeated_in_another_argument_rejects_save(tmp_path: Path):
    state = _state()
    context = _context(state, "DO-NOT-PERSIST")
    arguments = json.loads(context.history[1]["tool_calls"][0]["function"]["arguments"])
    arguments["process_id"] = "DO-NOT-PERSIST"
    context.history[1]["tool_calls"][0]["function"]["arguments"] = json.dumps(arguments)

    with pytest.raises(SessionValidationError, match="其他会话文本"):
        SessionStore(tmp_path).save(None, state, context, workspace_root=tmp_path)
    assert not list(tmp_path.glob("*.json"))


def test_empty_write_process_input_leaves_summary_and_notice_intact():
    state = AgentState(task="task")
    context = _context(state, "")
    context._summary = "task"
    context.set_runtime_notice("next")

    exported = context.export_session()
    assert exported["summary"] == "task"
    assert exported["runtime_notice"] == "next"
    assert json.loads(exported["history"][1]["tool_calls"][0]["function"]["arguments"])["input"] == "<redacted:write_process.input>"


def test_context_export_requires_exact_ordered_tool_results():
    state = AgentState(task="task")
    context = ContextManager(state, [
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "a", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "wrong", "content": "x"},
    ])
    with pytest.raises(ValueError, match="按序"):
        context.export_session()


def test_state_export_rejects_pending_attempts():
    state = AgentState(task="task")
    state.reserve_attempt("possible", "write_file", {"path": "a.txt", "content": "x"})
    with pytest.raises(SessionExportError, match="未结算"):
        state.export_session()


def test_session_store_round_trip_private_directory_and_repeated_update(tmp_path: Path):
    state = _state()
    context = _context(state)
    store = SessionStore(tmp_path)

    first = store.save(None, state, context, workspace_root=tmp_path)
    second = store.save(first["session_id"], state, context, workspace_root=tmp_path)
    loaded = store.load(first["session_id"])

    assert second["session_id"] == first["session_id"]
    assert loaded["schema_version"] == 2
    assert loaded["handoff_status"] == "active"
    assert loaded["state"] == first["state"]
    assert stat.S_IMODE(tmp_path.stat().st_mode) & 0o077 == 0
    assert stat.S_IMODE(store.path_for(first["session_id"]).stat().st_mode) & 0o077 == 0
    assert not list(tmp_path.glob("*.lock"))
    assert not list(tmp_path.glob("*.tmp"))


def test_corrupt_hash_unknown_version_and_missing_integrity_are_rejected(tmp_path: Path):
    state = _state()
    context = _context(state)
    store = SessionStore(tmp_path)
    envelope = store.save(None, state, context, workspace_root=tmp_path)
    path = store.path_for(envelope["session_id"])

    corrupted = json.loads(path.read_text(encoding="utf-8"))
    corrupted["state"]["task"] = "tampered"
    path.write_text(json.dumps(corrupted), encoding="utf-8")
    with pytest.raises(SessionValidationError, match="SHA-256"):
        store.load(envelope["session_id"])

    corrupted.pop("integrity", None)
    path.write_text(json.dumps(corrupted), encoding="utf-8")
    with pytest.raises(SessionValidationError, match="完整性"):
        store.load(envelope["session_id"])

    unknown = dict(envelope)
    unknown["schema_version"] = 999
    path.write_text(json.dumps(unknown), encoding="utf-8")
    with pytest.raises(SessionValidationError, match="schema_version"):
        store.load(envelope["session_id"])


def test_invalid_reference_is_rejected_even_with_a_valid_recomputed_hash(tmp_path: Path):
    state = _state()
    context = _context(state)
    store = SessionStore(tmp_path)
    envelope = store.save(None, state, context, workspace_root=tmp_path)
    path = store.path_for(envelope["session_id"])
    value = json.loads(path.read_text(encoding="utf-8"))
    value["state"]["plan_progress_history"][0]["revision_id"] = 999
    without_integrity = {key: item for key, item in value.items() if key != "integrity"}
    import hashlib
    value["integrity"] = {
        "algorithm": "sha256",
        "sha256": hashlib.sha256(
            json.dumps(without_integrity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(SessionValidationError, match="引用"):
        store.load(envelope["session_id"])


def test_oversized_file_is_rejected_before_reading_payload(tmp_path: Path):
    state = AgentState(task="task")
    context = ContextManager(state, [{"role": "user", "content": "x" * 1000}])
    store = SessionStore(tmp_path, max_file_bytes=500)
    with pytest.raises(SessionSizeError):
        store.save(None, state, context, workspace_root=tmp_path)
    assert not list(tmp_path.glob("*.json"))


def test_atomic_write_failure_keeps_previous_complete_file(tmp_path: Path, monkeypatch):
    state = _state()
    context = _context(state)
    store = SessionStore(tmp_path)
    envelope = store.save(None, state, context, workspace_root=tmp_path)
    before = store.load(envelope["session_id"])

    def fail_replace(source, target):
        raise OSError("injected replace failure")

    monkeypatch.setattr("mini_agent.session.os.replace", fail_replace)
    with pytest.raises(SessionError, match="原子写入失败"):
        store.save(envelope["session_id"], state, context, workspace_root=tmp_path)
    assert store.load(envelope["session_id"]) == before
    assert not list(tmp_path.glob("*.tmp"))


def test_directory_sync_failure_reports_uncertain_commit_with_session_id(tmp_path: Path, monkeypatch):
    state = _state()
    context = _context(state)
    store = SessionStore(tmp_path)
    first = store.save(None, state, context, workspace_root=tmp_path)
    state.task = "changed after replace"
    real_fsync = os.fsync

    def fail_directory_sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("injected directory sync failure")
        return real_fsync(fd)

    monkeypatch.setattr("mini_agent.session.os.fsync", fail_directory_sync)
    with pytest.raises(SessionCommitUncertainError, match="已替换") as error:
        store.save(first["session_id"], state, context, workspace_root=tmp_path)
    assert error.value.session_id == first["session_id"]
    assert store.load(first["session_id"])["state"]["task"] == "changed after replace"
    assert not list(tmp_path.glob("*.lock"))


def test_lock_cleanup_failure_after_replace_reports_uncertain_commit(tmp_path: Path, monkeypatch):
    state = _state()
    context = _context(state)
    store = SessionStore(tmp_path)
    first = store.save(None, state, context, workspace_root=tmp_path)
    state.task = "changed before lock cleanup"

    def fail_release(session_id, fd):
        os.close(fd)
        raise OSError("injected lock cleanup failure")

    monkeypatch.setattr(store, "_release_lock", fail_release)
    with pytest.raises(SessionCommitUncertainError, match="独占锁清理失败") as error:
        store.save(first["session_id"], state, context, workspace_root=tmp_path)
    assert error.value.session_id == first["session_id"]
    assert store.load(first["session_id"])["state"]["task"] == "changed before lock cleanup"


def test_exclusive_lock_is_not_automatically_stolen(tmp_path: Path):
    state = _state()
    context = _context(state)
    store = SessionStore(tmp_path)
    envelope = store.save(None, state, context, workspace_root=tmp_path)
    lock = tmp_path / f"{envelope['session_id']}.lock"
    lock.write_text("owner", encoding="utf-8")
    with pytest.raises(SessionBusyError):
        store.save(envelope["session_id"], state, context, workspace_root=tmp_path)
