"""v0.41 lexical Memory retrieval and ephemeral Context tests."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mini_agent.context import ContextBudget, ContextManager, MEMORY_CONTEXT_PREFIX  # noqa: E402
from mini_agent.memory import MemoryCorruptError, MemoryStore  # noqa: E402
from mini_agent.permission import ALLOW, PermissionGate, PermissionPolicy  # noqa: E402
from mini_agent.retrieval import MemoryRetriever, MemorySearchResult, normalize_text  # noqa: E402
from mini_agent.state import AgentState  # noqa: E402
from mini_agent.tools import create_registry  # noqa: E402
from mini_agent.tools.base import ToolExecutor  # noqa: E402


def _store(tmp_path: Path) -> MemoryStore:
    tmp_path.mkdir(parents=True, exist_ok=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return MemoryStore(workspace, tmp_path / "memory")


def _write_records(store: MemoryStore, records: list[dict]) -> None:
    Path(store.root).mkdir(mode=0o700, exist_ok=True)
    Path(store.path).write_text(
        json.dumps({"schema_version": 1, "memories": records}, ensure_ascii=False),
        encoding="utf-8",
    )


def _record(memory_id: str, title: str, body: str, *, updated_at: str,
            tags: list[str] | None = None, source: str = "notes") -> dict:
    return {
        "memory_id": memory_id,
        "revision": 1,
        "title": title,
        "body": body,
        "tags": tags or [],
        "source": source,
        "created_at": updated_at,
        "updated_at": updated_at,
    }


def test_snapshot_is_detached_and_does_not_create_or_write(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    memory_root = tmp_path / "memory"
    store = MemoryStore(workspace, memory_root)

    assert store.snapshot() == []
    assert not memory_root.exists()
    record = store.remember("title", "body", [], "source")
    snapshot = store.snapshot()
    snapshot[0]["title"] = "changed locally"
    snapshot[0]["tags"].append("local")
    assert store.read(record["memory_id"])["title"] == "title"
    assert store.read(record["memory_id"])["tags"] == []


def test_normalization_chinese_fragments_and_code_tokens(tmp_path: Path):
    store = _store(tmp_path)
    store.remember("ＦｏｏＢａｒ", "持续集成 uses read_file", ["Ｐｙｔｈｏｎ"], "DOCS")
    retriever = MemoryRetriever(store)

    assert retriever.search("foobar")
    assert retriever.search("PYTHON")
    assert retriever.search("持续集成")
    assert retriever.search("集")
    assert retriever.search("read_file")


def test_weighted_fields_phrase_bonus_and_stable_ties(tmp_path: Path):
    store = _store(tmp_path)
    _write_records(store, [
        _record("b", "other", "python python", updated_at="2026-01-01T00:00:00Z"),
        _record("a", "Python guide", "unrelated", updated_at="2026-01-01T00:00:00Z"),
        _record("c", "other", "unrelated", tags=["python"], updated_at="2026-01-02T00:00:00Z"),
        _record("d", "other", "unrelated", source="python", updated_at="2026-01-03T00:00:00Z"),
    ])
    result = MemoryRetriever(store).search_with_total("python", limit=10)
    assert result.total_matches == 4
    assert [item["memory_id"] for item in result.memories] == ["a", "c", "d", "b"]
    assert result.memories[0]["matched_fields"] == ["title"]
    assert result.memories[0]["source_status"] == "unverified"

    phrase = MemoryRetriever(store).search_with_total("python guide")
    assert phrase.memories[0]["memory_id"] == "a"
    assert phrase.memories[0]["score"] > result.memories[0]["score"]


def test_empty_query_limit_and_bounded_snippet(tmp_path: Path):
    store = _store(tmp_path)
    store.remember("title", "prefix " + ("x" * 400) + " python " + ("y" * 400), [], "/looks/like/a/file")
    retriever = MemoryRetriever(store)
    assert retriever.search_with_total("   ").total_matches == 0
    with pytest.raises(ValueError):
        retriever.search("python", limit=11)
    item = retriever.search("python")[0]
    assert len(item["snippet"]) <= 240
    assert item["source_status"] == "unverified"
    assert item["source"] == "/looks/like/a/file"


def test_snippet_maps_nfkc_expansion_back_to_raw_body(tmp_path: Path):
    store = _store(tmp_path)
    body = ("ﬃ " * 100) + "needle" + (" tail" * 30)
    _write_records(store, [_record(
        "nfkc", "NFKC", body, updated_at="2026-01-01T00:00:00Z",
    )])

    item = MemoryRetriever(store).search("needle")[0]

    assert len(item["snippet"]) <= 240
    assert "needle" in normalize_text(item["snippet"])


def test_memory_query_keeps_latest_follow_up_with_long_initial_task():
    initial_task = "initial task " + ("x" * 1200)
    latest = "latest unique keyword"
    state = AgentState(task=initial_task)
    history = [
        {"role": "user", "content": initial_task},
        {"role": "assistant", "content": "ignore"},
        {"role": "user", "content": latest},
    ]

    query = ContextManager._memory_query_from(state, history)

    assert latest in query
    assert len(query) <= 1200
    assert state.task == initial_task


def test_memory_query_deduplicates_equal_task_and_keeps_short_parts():
    state = AgentState(task="  same task  ")
    assert ContextManager._memory_query_from(
        state, [{"role": "user", "content": " same task "}],
    ) == "same task"

    query = ContextManager._memory_query_from(
        AgentState(task="  initial  "),
        [{"role": "user", "content": " follow-up "}],
    )
    assert query == "initial\nfollow-up"
    assert len(query) <= 1200


def test_search_tool_is_read_only_allowed_and_hidden_from_child(tmp_path: Path):
    store = _store(tmp_path)
    store.remember("Python", "Use Python", [], "test")
    state = AgentState()
    state.begin_task("search")
    registry = create_registry(state, workspace_root=store.workspace_root, memory_store=store)
    assert registry.get("search_memories").effect_class == "none"
    assert "search_memories" not in {
        tool.name for tool in registry.filtered_for_subagent({"search_memories"}).list_tools()
    }
    executor = ToolExecutor(
        registry,
        gate=PermissionGate(PermissionPolicy({"search_memories": ALLOW})),
    )
    payload = json.loads(executor.execute("search_memories", {"query": "python"}))
    assert payload["status"] == "ok"
    assert payload["returned"] == 1
    rejected = executor.execute_result("search_memories", {"query": "  "})
    assert rejected.outcome == "invalid"
    assert rejected.error_kind == "invalid_arguments"


def test_parent_context_refreshes_without_persisting_candidates(tmp_path: Path):
    store = _store(tmp_path)
    first = store.remember("first", "Python context", [], "user")
    state = AgentState(task="Python context")
    history = [{"role": "user", "content": "Python context"}]
    context = ContextManager(
        state, history, memory_retriever=MemoryRetriever(store), observability=False,
    )
    prepared = context.prepare_messages()
    assert any(MEMORY_CONTEXT_PREFIX in str(item.get("content")) for item in prepared)
    assert first["memory_id"] in str(prepared)
    assert first["memory_id"] not in json.dumps(context.export_session(), ensure_ascii=False)

    store.remember("second", "follow-up Rust", [], "user")
    history.append({"role": "assistant", "content": "ignore this assistant output"})
    history.append({"role": "tool", "tool_call_id": "x", "content": "ignore this tool output"})
    history.append({"role": "user", "content": "Rust follow-up"})
    refreshed = context.prepare_messages()
    assert "second" in str(refreshed)
    assert context.stats_snapshot().memory > 0
    assert context.stats_snapshot().tokens == sum(
        getattr(context.stats_snapshot(), field)
        for field in ("system", "task", "state", "history", "tool_result", "memory")
    )


def test_restored_context_rebinds_current_retriever_without_session_candidates(tmp_path: Path):
    old_store = _store(tmp_path / "old")
    old_store.remember("old", "Python context", [], "old")
    new_store = _store(tmp_path / "new")
    new_store.remember("new", "Python context", [], "new")
    state = AgentState(task="Python context")
    history = [{"role": "user", "content": "Python context"}]
    original = ContextManager(
        state, history, memory_retriever=MemoryRetriever(old_store), observability=False,
    )
    original.prepare_messages()
    payload = original.export_session()

    restored = ContextManager.restore_session(
        state, payload, memory_retriever=MemoryRetriever(new_store), observability=False,
    )

    assert restored.memory_retriever.store is new_store
    assert restored._memory_candidates == []
    assert all(field not in payload for field in (
        "memory_retriever", "memory_candidates", "memory_query", "score",
    ))
    assert "old" not in json.dumps(payload, ensure_ascii=False)


def test_one_prepare_reads_one_snapshot_and_does_not_compact_for_memory():
    class SpyRetriever:
        def __init__(self):
            self.calls = []

        def search_with_total(self, query, limit=4):
            self.calls.append((query, limit))
            return MemorySearchResult(
                query=query,
                memories=(
                    {
                        "memory_id": "memory-1",
                        "title": "relevant",
                        "snippet": "relevant " * 40,
                        "source": "test",
                        "source_status": "unverified",
                        "updated_at": "2026-01-01T00:00:00Z",
                        "score": 10,
                        "matched_fields": ["body"],
                    },
                ),
                total_matches=1,
            )

    history = [{"role": "user", "content": "task"}]
    for index in range(2):
        history.extend([
            {"role": "assistant", "content": f"answer {index}"},
            {"role": "tool", "tool_call_id": str(index), "content": "tool result"},
        ])
    observations = []
    retriever = SpyRetriever()
    context = ContextManager(
        AgentState(task="task"), history,
        budget=ContextBudget(window=220, output_reserve_ratio=0, history_ratio=0.5),
        keep_rounds=1,
        summarizer=lambda _: observations.append("summary") or "summary",
        memory_retriever=retriever,
        observability=False,
        observer=observations.append,
    )

    prepared = context.prepare_messages()

    assert len(retriever.calls) == 1
    assert observations.count("summary") == 0
    assert sum(event.kind == "memory_retrieved"
               for event in observations if hasattr(event, "kind")) == 1
    assistant_indexes = [
        i for i, message in enumerate(prepared)
        if message.get("role") == "assistant" and message.get("content")
    ]
    for index in assistant_indexes:
        assert index + 1 < len(prepared)
        assert prepared[index + 1].get("role") == "tool"


def test_runtime_notice_is_counted_once_when_fitting_memory():
    class FixedRetriever:
        def search_with_total(self, query, limit=4):
            return MemorySearchResult(
                query=query,
                memories=({
                    "memory_id": "memory-1", "title": "relevant",
                    "snippet": "small", "source": "test",
                    "source_status": "unverified",
                    "updated_at": "2026-01-01T00:00:00Z", "score": 10,
                    "matched_fields": ["body"],
                },),
                total_matches=1,
            )

    context = ContextManager(
        AgentState(task="task"), [{"role": "user", "content": "task"}],
        budget=ContextBudget(window=210, output_reserve_ratio=0, history_ratio=0.5),
        memory_retriever=FixedRetriever(), observability=False,
    )
    context.set_runtime_notice("continue with the next tool call")

    prepared = context.prepare_messages()

    assert sum("[Runtime Notice]" in str(item.get("content")) for item in prepared) == 1
    assert any(MEMORY_CONTEXT_PREFIX in str(item.get("content")) for item in prepared)


def test_search_memories_is_allowed_during_crash_investigation():
    state = AgentState()
    state.begin_task("recover")
    state.begin_crash_recovery(
        "source-session", 1, 1, "a" * 64, 0, [{
            "invocation_id": "call-1", "tool": "remember",
            "effect_class": "possible", "handler_admitted": True,
            "attempt_id": "a-1", "generation_id": 1, "pre_generation_id": 0,
            "permission": "allowed", "arguments_summary": {},
            "arguments_hash": "b" * 64,
        }],
    )
    issue = state.crash_issues[0]
    state.resolve_crash_issue(issue.issue_id, "investigate", "search current memory")

    assert state.crash_recovery_gate("search_memories", {"query": "task"}, "none") is None


def test_memory_failure_degrades_current_view_and_retries(tmp_path: Path):
    store = _store(tmp_path)
    state = AgentState(task="task")
    events = []
    context = ContextManager(
        state, [{"role": "user", "content": "task"}],
        memory_retriever=MemoryRetriever(store), observability=False,
        observer=events.append,
    )
    Path(store.root).mkdir(mode=0o700)
    Path(store.path).write_text("{bad", encoding="utf-8")
    prepared = context.prepare_messages()
    assert "记忆检索不可用" in str(prepared)
    failed = [event for event in events if event.kind == "memory_retrieval_failed"]
    assert failed and "path" not in json.dumps(failed[0].details)
    assert "{bad" not in json.dumps(failed[0].details)

    _write_records(store, [_record(
        "recovered", "task", "recovered memory", updated_at="2026-01-01T00:00:00Z",
    )])
    recovered = context.prepare_messages()
    assert "recovered" in str(recovered)
    assert sum(event.kind == "memory_retrieval_failed" for event in events) == 1
    assert sum(event.kind == "memory_retrieved" for event in events) == 1


def test_tiny_budget_and_disabled_retrieval_preserve_task(tmp_path: Path):
    store = _store(tmp_path)
    store.remember("task", "task body", [], "test")
    state = AgentState(task="task")
    history = [{"role": "user", "content": "task"}]
    context = ContextManager(
        state, history, budget=ContextBudget(window=30, output_reserve_ratio=0, history_ratio=0.2),
        memory_retriever=MemoryRetriever(store), observability=False,
    )
    prepared = context.prepare_messages()
    assert any(message.get("role") == "user" for message in prepared)
    assert not any(MEMORY_CONTEXT_PREFIX in str(message.get("content")) for message in prepared)

    class MustNotRead:
        def search_with_total(self, query, limit=4):
            raise AssertionError("disabled retrieval read the store")

    disabled = ContextManager(
        state, history, memory_retriever=MustNotRead(),
        memory_retrieval_enabled=False, observability=False,
    )
    assert not any(MEMORY_CONTEXT_PREFIX in str(message.get("content"))
                   for message in disabled.prepare_messages())


if __name__ == "__main__":
    pytest.main([__file__])
