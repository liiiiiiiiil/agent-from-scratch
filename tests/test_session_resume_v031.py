"""v0.31 safe-resume admission, workspace checks, and fresh runtime tests."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mini_agent.checkpoint import CheckpointStore
from mini_agent.context import ContextManager
from mini_agent.processes import ProcessManager
from mini_agent.resume import ResumeError, prepare_resume
from mini_agent.session import SessionBusyError, SessionStore, SessionValidationError
from mini_agent.state import (
    AgentState, ExecutionAttempt, VerificationEvidence, canonical_arguments_hash,
)
from mini_agent.tools.base import ExecutionResult
from mini_agent.trace import build_trace


ROOT = Path(__file__).resolve().parents[1]


def _make_session(tmp_path: Path, state: AgentState | None = None,
                  history: list[dict] | None = None):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    session_dir = tmp_path / "sessions"
    store = SessionStore(session_dir)
    state = state or AgentState()
    if not state.task:
        state.begin_task("resume this task")
    context = ContextManager(state, history or [{"role": "user", "content": "continue"}])
    envelope = store.save(
        None, state, context, workspace_root=workspace, handoff_status="clean",
    )
    return workspace, store, context, envelope


def _plan_state(mode: str = "auto") -> AgentState:
    state = AgentState()
    state.begin_task("preserve the plan", mode=mode)
    if mode == "plan_only":
        # The plan-only admission rule requires one real read-only observation.
        from mini_agent.state import ExecutionAttempt, canonical_arguments_hash

        arguments = {"path": "README.md"}
        state.attempts.append(ExecutionAttempt(
            "a-1", 0, 0, "read_file", canonical_arguments_hash(arguments),
            arguments, "succeeded", 1, "none", True, "allowed",
        ))
        state._next_attempt = 2
        state._original_attempt_arguments["a-1"] = arguments
        state.commit_plan(
            "preserve the plan", ["keep approval"], ["state survives"],
            [{"step_id": "inspect", "content": "inspect", "depends_on": [],
              "success_criteria": ["facts recorded"], "replaces": []}],
            "initial plan",
        )
        return state
    state.commit_plan(
        "preserve the plan", ["keep budget"], ["state survives"],
        [{"step_id": "inspect", "content": "inspect", "depends_on": [],
          "success_criteria": ["facts recorded"], "replaces": []}],
        "initial plan",
    )
    state.update_plan_progress(1, "inspect", "in_progress", "start")
    return state


def test_clean_schema2_resume_rebuilds_runtime_and_invalidates_old_verification(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("恢复后必须重新读取当前项目指令。\n", encoding="utf-8")
    watched = workspace / "task.txt"
    watched.write_text("before", encoding="utf-8")

    state = AgentState()
    state.begin_task("resume a file task")
    state.files_changed.append("task.txt")
    state.verification_evidence.append(
        VerificationEvidence("python -m pytest", "passed", 0, "[exit=0]", 0)
    )
    state.verification_history.extend(state.verification_evidence)
    state._last_verified_generation = 0
    state._verification_required = False
    _, store, _, envelope = _make_session(tmp_path, state)

    candidate = prepare_resume(store, envelope["session_id"], workspace)
    assert store.load(envelope["session_id"])["handoff_status"] == "clean"
    runtime = candidate.claim()

    assert runtime.state.task == "resume a file task"
    assert runtime.state.current_generation_id == 1
    assert runtime.state.verification_evidence == []
    assert runtime.state.verification_history[0].outcome == "passed"
    assert runtime.state.has_verification_evidence() is False
    assert runtime.state._verification_required is True
    assert runtime.state.generations[-1].open_reason == "resume"
    assert "重新读取当前项目指令" in runtime.protected_messages[0]["content"]
    assert runtime.permission_gate is not None
    claimed = store.load(envelope["session_id"])
    assert claimed["handoff_status"] == "active"
    assert claimed["session_generation"] == envelope["session_generation"] + 1
    assert claimed["state"]["private"]["verification_generation"] == 1
    assert claimed["state"]["verification_evidence"] == []
    assert watched.read_text(encoding="utf-8") == "before"


def test_clean_schema2_claim_upgrades_the_active_commit_to_schema3(tmp_path: Path):
    workspace, store, _, envelope = _make_session(tmp_path)
    path = store.path_for(envelope["session_id"])
    legacy = json.loads(path.read_text(encoding="utf-8"))
    legacy["schema_version"] = 2
    legacy.pop("tool_boundary")
    without_integrity = {key: value for key, value in legacy.items() if key != "integrity"}
    legacy["integrity"] = {
        "algorithm": "sha256",
        "sha256": hashlib.sha256(
            json.dumps(without_integrity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    runtime = prepare_resume(store, envelope["session_id"], workspace).claim()
    upgraded = store.load(envelope["session_id"])
    assert upgraded["schema_version"] == 3
    assert upgraded["handoff_status"] == "active"
    assert upgraded["tool_boundary"]["status"] == "committed"
    assert runtime.state.task == "resume this task"


@pytest.mark.parametrize(
    "kind",
    ["direct", "exploring", "awaiting_approval", "verification", "repair", "blocked"],
)
def test_state_machine_safe_points_restore_without_automatic_execution(tmp_path: Path, kind: str):
    state = AgentState()
    if kind == "exploring":
        state.begin_task("investigate", mode="plan_only")
    elif kind == "awaiting_approval":
        state = _plan_state("plan_only")
    else:
        state.begin_task(f"resume {kind}")
    if kind == "verification":
        state._verification_required = True
    elif kind == "repair":
        state._repair_phase = "diagnosis_required"
    elif kind == "blocked":
        state.status = "blocked"
        state.terminal_reason = "blocked for resume test"

    workspace, store, _, envelope = _make_session(tmp_path, state)
    runtime = prepare_resume(store, envelope["session_id"], workspace).claim()
    assert runtime.state.task == state.task
    assert runtime.state.current_generation_id == state.current_generation_id + 1
    if kind == "exploring":
        assert runtime.state.planning_state.phase == "exploring"
    if kind == "awaiting_approval":
        assert runtime.state.planning_state.phase == "awaiting_approval"
        assert runtime.state.planning_state.active_revision_id == 1
    if kind == "blocked":
        assert runtime.state.status == "blocked"
        assert runtime.state.terminal_reason == "blocked for resume test"
    if kind == "repair":
        assert runtime.state.repair_phase == "diagnosis_required"


def test_plan_decisions_budgets_and_projected_goal_round_trip(tmp_path: Path):
    state = _plan_state("plan_only")
    state.planning_state = replace(
        state.planning_state, replans_used=2, replans_remaining=3,
    )
    state._repair_cycles = 1
    workspace, store, _, envelope = _make_session(tmp_path, state)

    runtime = prepare_resume(store, envelope["session_id"], workspace).claim()
    assert runtime.state.planning_state.replans_used == 2
    assert runtime.state.planning_state.replans_remaining == 3
    assert runtime.state._repair_cycles == 1
    assert runtime.state.user_plan_decisions == []
    assert runtime.state.snapshot()["active_plan"]["revision_id"] == 1
    assert runtime.state.current_goal == ""

    runtime.state.decide_plan("approved", 1)
    assert runtime.state.planning_state.phase == "executing"


def test_trace_replays_old_facts_and_new_resume_origin_from_state_only(tmp_path: Path):
    state = _plan_state("auto")
    workspace, store, _, envelope = _make_session(tmp_path, state)
    runtime = prepare_resume(store, envelope["session_id"], workspace).claim()

    report = build_trace(runtime.state.snapshot())
    assert report["integrity"] == {"status": "complete", "issues": []}
    assert any(item.get("kind") == "session_resumed" for item in report["trace_events"])
    assert any(item.get("kind") == "task_started" for item in report["trace_events"])


def test_clean_schema3_resume_preserves_unresolved_crash_handoff(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = AgentState()
    state.begin_task("preserve crash handoff")
    state.begin_crash_recovery(
        "source-session", 1, 1, "a" * 64, 2,
        [{
            "invocation_id": "inv-1", "tool": "write_file", "effect_class": "possible",
            "handler_admitted": True, "attempt_id": "a-1", "generation_id": 1,
            "pre_generation_id": 0, "permission": "allowed", "arguments_hash": "b" * 64,
            "arguments_summary": {"path": "task.txt", "content": "<str:4>"},
        }],
    )
    context = ContextManager(state, [{"role": "user", "content": "continue"}])
    store = SessionStore(tmp_path / "sessions")
    envelope = store.save(None, state, context, workspace_root=workspace, handoff_status="clean")

    runtime = prepare_resume(store, envelope["session_id"], workspace).claim()
    assert runtime.recovery_mode == "safe_point"
    assert runtime.state.unresolved_crash_issues[0].classification == "uncertain_side_effect"
    assert runtime.state.verification_evidence == []
    issue = runtime.state.unresolved_crash_issues[0]
    runtime.state.resolve_crash_issue(issue.issue_id, "investigate", "调查恢复后的事实")
    runtime.state.record_execution_result(ExecutionResult(
        "read_file", {"path": str(workspace)}, "allowed", True, "succeeded", 0,
        "none", "observation", "observation",
    ))
    runtime.state.resolve_crash_issue(issue.issue_id, "continue", "接受用户风险决定")
    assert runtime.state.crash_recoveries[0].status == "replanned"


def test_workspace_file_directory_and_untracked_changes_are_rejected(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "watched.txt").write_text("one", encoding="utf-8")
    watched_dir = workspace / "watched-dir"
    watched_dir.mkdir()
    (watched_dir / "one.txt").write_text("one", encoding="utf-8")

    state = AgentState()
    state.begin_task("watch workspace")
    state.files_changed.extend(["watched.txt", "watched-dir", "."])
    _, store, _, envelope = _make_session(tmp_path, state)

    (workspace / "watched.txt").write_text("changed", encoding="utf-8")
    with pytest.raises(ResumeError, match="工作区检查失败"):
        prepare_resume(store, envelope["session_id"], workspace)

    # A fresh clean commit demonstrates directory and untracked-entry checks.
    state.files_changed = ["watched-dir", "."]
    (workspace / "watched.txt").write_text("one", encoding="utf-8")
    envelope = store.save(
        envelope["session_id"], state, ContextManager(state, [{"role": "user", "content": "watch"}]),
        workspace_root=workspace, handoff_status="clean",
    )
    (watched_dir / "two.txt").write_text("two", encoding="utf-8")
    with pytest.raises(ResumeError, match="工作区检查失败"):
        prepare_resume(store, envelope["session_id"], workspace)

    envelope = store.save(
        envelope["session_id"], state, ContextManager(state, [{"role": "user", "content": "watch again"}]),
        workspace_root=workspace, handoff_status="clean",
    )
    (watched_dir / "one.txt").write_text("changed", encoding="utf-8")
    with pytest.raises(ResumeError, match="工作区检查失败"):
        prepare_resume(store, envelope["session_id"], workspace)


def test_claim_rechecks_workspace_after_candidate_was_prepared(tmp_path: Path):
    state = AgentState()
    state.begin_task("watch file")
    state.files_changed.append("watched.txt")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    watched = workspace / "watched.txt"
    watched.write_text("before", encoding="utf-8")
    _, store, _, envelope = _make_session(tmp_path, state)

    candidate = prepare_resume(store, envelope["session_id"], workspace)
    watched.write_text("after", encoding="utf-8")
    with pytest.raises(SessionValidationError, match="占用前工作区检查失败"):
        candidate.claim()
    assert store.load(envelope["session_id"])["handoff_status"] == "clean"


def test_recursive_grep_baseline_covers_unmatched_nested_files(tmp_path: Path):
    workspace = tmp_path / "workspace"
    nested = workspace / "nested"
    nested.mkdir(parents=True)
    watched = nested / "unmatched.txt"
    watched.write_text("before", encoding="utf-8")
    state = AgentState()
    state.begin_task("search recursively")
    arguments = {"path": ".", "pattern": "no-match"}
    fingerprint = canonical_arguments_hash(arguments)
    state.attempts.append(ExecutionAttempt(
        "a-1", 0, 0, "grep", fingerprint, {}, "succeeded", 1,
        "none", True, "allowed",
    ))
    state._next_attempt = 2
    state._original_attempt_arguments["a-1"] = arguments
    state.tool_history.append({
        "tool": "grep", "arguments_hash": fingerprint, "ok": True, "brief": "无匹配",
    })
    _, store, _, envelope = _make_session(tmp_path, state)
    assert envelope["workspace_manifest"]["recoverable"] is True

    watched.write_text("after", encoding="utf-8")
    with pytest.raises(ResumeError, match="工作区检查失败"):
        prepare_resume(store, envelope["session_id"], workspace)


def test_task_symlink_path_is_not_recorded_as_its_target(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_text("A", encoding="utf-8")
    alias = workspace / "alias.txt"
    try:
        alias.symlink_to("a.txt")
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symbolic links unavailable: {error}")
    state = AgentState()
    state.begin_task("use alias")
    state.files_changed.append("alias.txt")
    _, store, _, envelope = _make_session(tmp_path, state)

    manifest = envelope["workspace_manifest"]
    assert manifest["recoverable"] is False
    assert not any(entry["path"] == "a.txt" for entry in manifest["entries"])
    with pytest.raises(ResumeError, match="符号链接"):
        prepare_resume(store, envelope["session_id"], workspace)


def test_outside_and_uncheckable_paths_are_diagnostic_but_not_recoverable(tmp_path: Path):
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    state = AgentState()
    state.begin_task("outside path")
    state.files_changed.append(str(outside))
    workspace, store, _, envelope = _make_session(tmp_path, state)
    assert store.load(envelope["session_id"])["workspace_manifest"]["recoverable"] is False
    with pytest.raises(ResumeError, match="工作区检查失败"):
        prepare_resume(store, envelope["session_id"], workspace)

    state = AgentState()
    state.begin_task("unavailable path")
    state.files_changed.append("<unavailable>")
    workspace, store, _, envelope = _make_session(tmp_path / "second", state)
    assert store.load(envelope["session_id"])["workspace_manifest"]["recoverable"] is False
    with pytest.raises(ResumeError, match="工作区检查失败"):
        prepare_resume(store, envelope["session_id"], workspace)


def test_schema1_active_corrupt_and_lock_contention_never_build_a_runtime(tmp_path: Path):
    workspace, store, _, envelope = _make_session(tmp_path)
    path = store.path_for(envelope["session_id"])

    legacy = json.loads(path.read_text(encoding="utf-8"))
    legacy.pop("session_generation")
    legacy.pop("workspace_manifest")
    legacy.pop("tool_boundary")
    legacy["schema_version"] = 1
    without_integrity = {key: value for key, value in legacy.items() if key != "integrity"}
    legacy["integrity"] = {
        "algorithm": "sha256",
        "sha256": hashlib.sha256(
            json.dumps(without_integrity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ResumeError, match="schema 1"):
        prepare_resume(store, envelope["session_id"], workspace)

    active_state = AgentState()
    active_state.begin_task("active")
    envelope = store.save(
        envelope["session_id"], active_state,
        ContextManager(active_state, []),
        workspace_root=workspace, handoff_status="active",
    )
    with pytest.raises(ResumeError, match="clean"):
        prepare_resume(store, envelope["session_id"], workspace)

    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(SessionValidationError):
        prepare_resume(store, envelope["session_id"], workspace)

    # Restore a valid clean file and hold the lock only for the final claim.
    workspace, store, _, envelope = _make_session(tmp_path / "lock")
    lock = store.root / f"{envelope['session_id']}.lock"
    lock.write_text("held", encoding="utf-8")
    candidate = prepare_resume(store, envelope["session_id"], workspace)
    with pytest.raises(SessionBusyError):
        candidate.claim()
    assert store.load(envelope["session_id"])["handoff_status"] == "clean"


def test_old_checkpoint_metadata_is_unavailable_and_new_ids_skip_history():
    checkpoints = CheckpointStore()
    checkpoints.import_snapshot([{
        "checkpoint_id": "cp-1", "attempt_id": "a-1", "generation_id": 0,
        "path": "a.txt", "before_type": "absent", "before_sha256": None,
        "after_type": "regular_file", "after_sha256": "a" * 64, "mode": 0o600,
        "status": "ready", "unavailable_reason": None, "created_at": 1,
    }, {
        "checkpoint_id": "cp-3", "attempt_id": "a-2", "generation_id": 0,
        "path": "b.txt", "before_type": "regular_file", "before_sha256": "b" * 64,
        "after_type": "regular_file", "after_sha256": "c" * 64, "mode": 0o600,
        "status": "unavailable", "unavailable_reason": "old", "created_at": 2,
    }])
    assert checkpoints.available() == []
    assert checkpoints.get("cp-1").unavailable_reason == "恢复后缺少前镜像字节，旧 checkpoint 仅供审计"
    assert checkpoints._new_id() == "cp-4"

    manager = ProcessManager(historical_process_ids=["proc-1", "proc-3"])
    assert manager._allocate_id() == "proc-2"
    assert manager._allocate_id() == "proc-4"


def test_real_new_python_process_resumes_then_cleanly_hands_off(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    store = SessionStore(home / ".mini_agent" / "sessions")
    state = AgentState()
    state.begin_task("resume in a new Python process")
    context = ContextManager(state, [{"role": "user", "content": "saved"}])
    envelope = store.save(None, state, context, workspace_root=workspace, handoff_status="clean")

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run(
        [sys.executable, "-m", "mini_agent", "--resume", envelope["session_id"]],
        cwd=workspace, input="exit\n", text=True, capture_output=True, env=env,
    )
    assert completed.returncode == 0, completed.stderr
    assert "会话已恢复" in completed.stdout
    assert "等待用户输入" in completed.stdout
    assert "LLM" not in completed.stdout
    final = SessionStore(home / ".mini_agent" / "sessions").load(envelope["session_id"])
    assert final["handoff_status"] == "clean"
    assert final["session_generation"] == envelope["session_generation"] + 2
