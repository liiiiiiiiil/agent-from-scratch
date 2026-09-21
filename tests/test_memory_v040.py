"""v0.40 persistent workspace memory tests."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mini_agent.memory import (  # noqa: E402
    MemoryCommitUncertainError,
    MemoryConflictError,
    MemoryCorruptError,
    MemoryStore,
    MemoryStoreError,
    MemoryValidationError,
)
from mini_agent.agent import agent_loop  # noqa: E402
from mini_agent.context import ContextManager  # noqa: E402
from mini_agent.permission import ALLOW, PermissionGate, PermissionPolicy  # noqa: E402
from mini_agent.resume import prepare_resume  # noqa: E402
from mini_agent.session import DurableToolBoundary, SessionError, SessionStore  # noqa: E402
from mini_agent.state import AgentState  # noqa: E402
from mini_agent.tools import create_registry  # noqa: E402
from mini_agent.tools.base import ToolExecutor  # noqa: E402


def _store(tmp_path: Path, name: str = "workspace") -> MemoryStore:
    workspace = tmp_path / name
    workspace.mkdir(exist_ok=True)
    return MemoryStore(workspace, tmp_path / "memory")


def test_crud_and_revision_conflict_returns_bounded_views(tmp_path: Path):
    store = _store(tmp_path)
    record = store.remember("Build rule", "Use the standard library", ["python"], "user note")
    memory_id = record["memory_id"]

    summaries = store.list()
    assert summaries[0]["memory_id"] == memory_id
    assert "body" not in summaries[0]
    assert store.read(memory_id)["body"] == "Use the standard library"

    revised = store.revise(memory_id, 1, "Updated", "Check current files", [], "agent note")
    assert revised["revision"] == 2
    with pytest.raises(MemoryConflictError):
        store.revise(memory_id, 1, "stale", "must not win", [], "stale")
    assert store.read(memory_id)["title"] == "Updated"

    forgotten = store.forget(memory_id, 2)
    assert forgotten == {"memory_id": memory_id, "revision": 2}
    assert store.list() == []


def test_workspace_isolation_and_process_restart(tmp_path: Path):
    first = _store(tmp_path, "one")
    second = _store(tmp_path, "two")
    first.remember("one", "only one", [], "test")
    assert second.list() == []
    assert first.workspace_key != second.workspace_key
    assert Path(first.path).name == first.workspace_key + ".json"

    code = (
        "from mini_agent.memory import MemoryStore; "
        "import json,sys; "
        "print(json.dumps(MemoryStore(sys.argv[1], sys.argv[2]).list(), ensure_ascii=False))"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    output = subprocess.check_output(
        [sys.executable, "-c", code, str(tmp_path / "one"), str(tmp_path / "memory")],
        env=env,
        text=True,
    )
    assert json.loads(output)[0]["title"] == "one"


def test_memory_directory_must_be_outside_workspace_even_through_symlink(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(MemoryValidationError, match="工作区内"):
        MemoryStore(workspace, workspace / "memory")

    link = tmp_path / "memory-link"
    link.symlink_to(workspace, target_is_directory=True)
    with pytest.raises(MemoryValidationError, match="工作区内"):
        MemoryStore(workspace, link)

    external = tmp_path / "external-memory"
    store = MemoryStore(workspace, external)
    store.remember("external", "available", [], "test")
    assert store.list()[0]["title"] == "external"


def test_memory_listing_paginates_and_tool_finds_records_after_first_page(tmp_path: Path):
    store = _store(tmp_path)
    for index in range(21):
        store.remember(f"title-{index}", f"body-{index}", [], "test")

    first = store.list_page(limit=20)
    assert first["total"] == 21
    assert len(first["memories"]) == 20
    assert first["next_offset"] == 20
    assert store.list(limit=20)[-1]["title"] == "title-19"

    page = store.list(offset=20, limit=20)
    assert page["total"] == 21
    assert page["next_offset"] is None
    assert [item["title"] for item in page["memories"]] == ["title-20"]

    state = AgentState()
    state.begin_task("page memory")
    registry = create_registry(state, workspace_root=store.workspace_root, memory_store=store)
    executor = ToolExecutor(
        registry, gate=PermissionGate(PermissionPolicy({"list_memories": ALLOW})),
    )
    tool_page = json.loads(executor.execute("list_memories", {"offset": 20}))
    assert tool_page["total"] == 21
    assert tool_page["next_offset"] is None
    assert tool_page["memories"][0]["title"] == "title-20"


def test_limits_corrupt_schema_and_no_overwrite(tmp_path: Path):
    store = _store(tmp_path)
    store.remember("", "body", [], "source")
    with pytest.raises(ValueError):
        store.remember("title", "x", ["t" * 33], "source")
    with pytest.raises(ValueError):
        store.remember("title", "x", [], "s" * 241)

    for index in range(255):
        store.remember(str(index), "body", [], "test")
    with pytest.raises(ValueError, match="256"):
        store.remember("overflow", "body", [], "test")

    Path(store.path).write_text("{not-json", encoding="utf-8")
    before = Path(store.path).read_bytes()
    with pytest.raises(MemoryCorruptError):
        store.remember("new", "body", [], "test")
    assert Path(store.path).read_bytes() == before

    Path(store.path).write_text(
        json.dumps({"schema_version": 99, "memories": []}), encoding="utf-8"
    )
    with pytest.raises(MemoryCorruptError, match="schema"):
        store.list()


def test_private_permissions_and_atomic_write_failure(tmp_path: Path):
    store = _store(tmp_path)
    record = store.remember("title", "body", [], "source")
    assert stat.S_IMODE(os.stat(store.root).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    before = Path(store.path).read_bytes()
    with patch("mini_agent.memory.os.replace", side_effect=OSError("replace failed")):
        with pytest.raises(MemoryStoreError):
            store.revise(record["memory_id"], 1, "new", "body", [], "source")
    assert Path(store.path).read_bytes() == before
    assert not Path(store.lock_path).exists()


def test_directory_sync_uncertainty_halts_only_future_writes(tmp_path: Path):
    store = _store(tmp_path)
    state = AgentState()
    state.begin_task("record memory uncertainty")
    registry = create_registry(state, workspace_root=store.workspace_root, memory_store=store)
    executor = ToolExecutor(
        registry, gate=PermissionGate(PermissionPolicy({"remember": ALLOW})),
    )
    with patch.object(store, "_sync_root", side_effect=OSError("fsync failed")):
        result = executor.execute_result(
            "remember",
            {"title": "title", "body": "body", "tags": [], "source": "source"},
            state=state,
        )
    assert result.outcome == "failed"
    assert result.error_kind == "memory_commit_uncertain"
    assert "已原子替换" in result.output and "提交状态未确认" in result.output
    attempt = state.record_execution_result(result)
    assert attempt.error_kind == "memory_commit_uncertain"
    assert state.failures[-1].category == "unknown"
    assert state.failures[-1].retryable is False
    assert "不能直接重试" in state.recovery_notice
    assert "memory_commit_uncertain" in state.terminal_reason
    assert store.write_halted
    assert store.list()[0]["title"] == "title"
    with pytest.raises(MemoryCommitUncertainError):
        store.remember("second", "body", [], "source")
    with pytest.raises(MemoryCommitUncertainError):
        MemoryStore(store.workspace_root, store.root).remember("third", "body", [], "source")


def test_concurrent_writes_do_not_lose_updates(tmp_path: Path):
    base = _store(tmp_path)

    def write(index: int):
        MemoryStore(base.workspace_root, base.root).remember(
            f"title-{index}", f"body-{index}", [], "thread"
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(24)))
    assert len(base.list(limit=256)) == 24


def test_registry_parent_only_memory_tools_and_permissions(tmp_path: Path):
    state = AgentState()
    registry = create_registry(state, workspace_root=tmp_path, memory_store=_store(tmp_path))
    names = {tool.name for tool in registry.list_tools()}
    assert {"list_memories", "read_memory", "remember", "revise_memory", "forget_memory", "search_memories"} <= names
    assert {tool.name for tool in registry.filtered_for_subagent({"list_memories", "read_memory"}).list_tools()} == set()
    assert registry.get("list_memories").effect_class == "none"
    assert registry.get("remember").effect_class == "possible"

    gate = PermissionGate(PermissionPolicy())
    assert gate.policy.check("list_memories") == ALLOW
    assert gate.policy.check("read_memory") == ALLOW
    assert gate.policy.check("search_memories") == ALLOW
    assert gate.policy.check("remember") != ALLOW
    assert "secret body" in gate._prompt_arguments(
        "remember", {"title": "t", "body": "secret body", "tags": [], "source": "user"}
    )
    executor = ToolExecutor(registry, gate=PermissionGate(PermissionPolicy({
        "remember": ALLOW, "list_memories": ALLOW, "read_memory": ALLOW,
    })))
    result = json.loads(executor.execute("remember", {
        "title": "t", "body": "secret body", "tags": [], "source": "user",
    }))
    listed = json.loads(executor.execute("list_memories", {}))
    assert "body" not in listed["memories"][0]
    assert json.loads(executor.execute("read_memory", {
        "memory_id": result["memory"]["memory_id"],
    }))['memory']['body'] == "secret body"


def _durable_memory_runtime(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = MemoryStore(workspace, tmp_path / "memory")
    state = AgentState()
    state.begin_task("durable memory")
    context = ContextManager(state, [{"role": "user", "content": "remember it"}], observability=False)
    sessions = SessionStore(tmp_path / "sessions")
    envelope = sessions.save(None, state, context, workspace_root=workspace)
    registry = create_registry(state, workspace_root=workspace, memory_store=store)
    executor = ToolExecutor(
        registry, gate=PermissionGate(PermissionPolicy({"remember": ALLOW})),
    )
    executor.session_boundary = DurableToolBoundary(
        sessions, envelope["session_id"], workspace,
    )
    return store, context, executor, sessions, envelope, workspace


def _remember_message(call_id: str = "memory-1") -> dict:
    return {
        "role": "assistant", "content": None, "tool_calls": [{
            "id": call_id, "type": "function", "function": {
                "name": "remember",
                "arguments": json.dumps({
                    "title": "durable", "body": "body", "tags": [], "source": "test",
                }),
            },
        }],
    }


def test_durable_admission_failure_does_not_write_memory(tmp_path: Path):
    store, context, executor, _sessions, _envelope, _workspace = _durable_memory_runtime(tmp_path)

    def fail_admission(*args, **kwargs):
        raise SessionError("injected admission failure")

    executor.session_boundary.record_admission = fail_admission
    with patch("mini_agent.agent.call_llm", return_value=_remember_message()):
        with pytest.raises(SessionError, match="admission"):
            agent_loop(context, executor)
    assert store.list() == []


def test_durable_result_commit_failure_does_not_replay_memory_write(tmp_path: Path):
    store, context, executor, sessions, envelope, workspace = _durable_memory_runtime(tmp_path)

    def fail_result(*args, **kwargs):
        raise SessionError("injected result commit failure")

    executor.session_boundary.record_execution_result = fail_result
    with patch("mini_agent.agent.call_llm", return_value=_remember_message()) as llm:
        with pytest.raises(SessionError, match="result commit"):
            agent_loop(context, executor)
    assert llm.call_count == 1
    assert store.list()[0]["title"] == "durable"

    candidate = prepare_resume(sessions, envelope["session_id"], workspace)
    runtime = candidate.claim()
    assert runtime.session_id != envelope["session_id"]
    derived = sessions.load(runtime.session_id)
    assert derived["tool_boundary"]["status"] == "committed"
    recovered_call = derived["tool_boundary"]["calls"][0]
    assert recovered_call["result"]["outcome"] == "uncertain"
    assert recovered_call["result"]["error_kind"] == "crash_recovery_uncertain"
    issue = runtime.state.crash_issues[0]
    assert issue.classification == "uncertain_side_effect"
    assert "记忆文件可能已经替换" in issue.reason
    assert store.list()[0]["title"] == "durable"
