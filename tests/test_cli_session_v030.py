"""CLI lifecycle tests for the explicit v0.30 /save opt-in."""

from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mini_agent import __main__ as cli
from mini_agent.processes import CleanupItem, CleanupReport, ProcessManager
from mini_agent.session import SessionStore
from mini_agent.state import ProcessRecord


class _Inputs:
    def __init__(self, values):
        self.values = list(values)

    def read(self, prompt):
        if not self.values:
            raise EOFError
        return self.values.pop(0)


class _Manager(ProcessManager):
    def __init__(self, incomplete: bool = False):
        super().__init__()
        self.incomplete = incomplete

    def sync_processes(self, task_id):
        return []

    def cleanup(self, task_id):
        if not self.incomplete:
            return CleanupReport(task_id, ())
        return CleanupReport(task_id, (
            CleanupItem(task_id=task_id, process_id="proc-fail", pid=4321,
                        terminated=False, killed=False, complete=False,
                        reason="injected cleanup failure"),
        ))


def _run_cli(monkeypatch, tmp_path: Path, inputs, *, incomplete=False, agent=None):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["mini_agent"])
    monkeypatch.setattr(cli, "InputSession", lambda: _Inputs(inputs))
    monkeypatch.setattr(cli, "ProcessManager", lambda: _Manager(incomplete))
    if agent is None:
        def agent(context, executor):
            context.history.append({"role": "assistant", "content": "done"})
            return "done"
    monkeypatch.setattr(cli, "agent_loop", agent)
    cli.main()
    session_dir = tmp_path / ".mini_agent" / "sessions"
    return session_dir, list(session_dir.glob("*.json")) if session_dir.exists() else []


def test_save_first_and_repeated_call_updates_one_session(monkeypatch, tmp_path, capsys):
    session_dir, files = _run_cli(monkeypatch, tmp_path, ["task", "/save", "/save", "exit"])

    assert len(files) == 1
    envelope = SessionStore(session_dir).load(files[0].stem)
    assert envelope["handoff_status"] == "clean"
    output = capsys.readouterr().out
    assert "已保存会话：" in output
    assert "已更新会话：" in output


def test_save_without_a_task_reports_usage_and_creates_no_file(monkeypatch, tmp_path, capsys):
    session_dir, files = _run_cli(monkeypatch, tmp_path, ["/save", "exit"])

    assert files == []
    assert "用法: /save" in capsys.readouterr().out


@pytest.mark.parametrize("boundary", ["/new next task", "/reset"])
def test_new_and_reset_commit_old_session_clean_after_cleanup(monkeypatch, tmp_path, boundary):
    session_dir, files = _run_cli(monkeypatch, tmp_path, ["task", "/save", boundary, "exit"])

    assert len(files) == 1
    loaded = SessionStore(session_dir).load(files[0].stem)
    assert loaded["handoff_status"] == "clean"


def test_cleanup_failure_leaves_last_session_active(monkeypatch, tmp_path, capsys):
    session_dir, files = _run_cli(
        monkeypatch, tmp_path, ["task", "/save", "/reset", "exit"], incomplete=True,
    )

    assert len(files) == 1
    loaded = SessionStore(session_dir).load(files[0].stem)
    assert loaded["handoff_status"] == "active"
    assert "清理不完整" in capsys.readouterr().out


def test_active_process_or_pending_attempt_rejects_save(monkeypatch, tmp_path, capsys):
    def agent(context, executor):
        state = context.state
        state._pending_attempts.add("a-pending")
        state.process_records.append(ProcessRecord(
            "proc-live", state.task_id, "a-1", 0, "sleep", str(tmp_path),
            1234, "running", "now", stdin_mode="pipe", stdin_state="write_pending",
            write_pending=True,
        ))
        context.history.append({"role": "assistant", "content": "waiting"})
        return "waiting"

    session_dir, files = _run_cli(monkeypatch, tmp_path, ["task", "/save", "exit"], agent=agent)

    assert files == []
    output = capsys.readouterr().out
    assert "会话保存失败" in output
    assert "活动后台进程" in output or "未结算" in output


def test_exceptional_exit_does_not_write_clean(monkeypatch, tmp_path):
    calls = {"count": 0}

    def agent(context, executor):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("injected model failure")
        context.history.append({"role": "assistant", "content": "done"})
        return "done"

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["mini_agent"])
    monkeypatch.setattr(cli, "InputSession", lambda: _Inputs(["task", "/save", "boom"]))
    monkeypatch.setattr(cli, "ProcessManager", lambda: _Manager(False))
    monkeypatch.setattr(cli, "agent_loop", agent)
    with pytest.raises(RuntimeError, match="injected"):
        cli.main()

    session_dir = tmp_path / ".mini_agent" / "sessions"
    files = list(session_dir.glob("*.json"))
    assert len(files) == 1
    assert SessionStore(session_dir).load(files[0].stem)["handoff_status"] == "active"
