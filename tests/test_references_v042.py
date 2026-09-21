"""v0.42 named local References tests."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mini_agent.memory import MemoryStore  # noqa: E402
from mini_agent.context import ContextManager  # noqa: E402
from mini_agent.permission import ALLOW, PermissionGate, PermissionPolicy  # noqa: E402
from mini_agent.references import (  # noqa: E402
    MAX_SEARCH_BYTES,
    MAX_SEARCH_ENTRIES,
    MAX_SEARCH_MATCHES,
    ReferenceAccessError,
    ReferenceCatalog,
    ReferenceConfigError,
)
from mini_agent.state import AgentState, PlanRejected  # noqa: E402
from mini_agent.state import canonical_arguments_hash  # noqa: E402
from mini_agent.session import SessionStore  # noqa: E402
from mini_agent.resume import prepare_resume  # noqa: E402
from mini_agent.tools import create_registry  # noqa: E402
from mini_agent.tools.base import ToolExecutor  # noqa: E402
from mini_agent.tools.references import (  # noqa: E402
    REFERENCE_READ_RESULT_MAX_BYTES,
    REFERENCE_SEARCH_RESULT_MAX_BYTES,
    read_reference as encode_read_reference,
)
import mini_agent.references as references_module  # noqa: E402
import mini_agent.config as runtime_config  # noqa: E402


def _catalog(tmp_path: Path, *, entries: list[dict] | None = None) -> tuple[ReferenceCatalog, Path]:
    root = tmp_path / "reference-root"
    root.mkdir()
    return ReferenceCatalog(
        entries or [{"alias": "docs", "path": str(root), "description": "local docs"}],
        config_base_dir=tmp_path,
    ), root


def test_empty_and_relative_configuration_is_frozen(tmp_path: Path):
    assert ReferenceCatalog([], config_base_dir=tmp_path).list_references()["references"] == []
    relative = tmp_path / "relative"
    relative.mkdir()
    config = [{"alias": "docs", "path": "relative", "description": "D"}]
    catalog = ReferenceCatalog(config, config_base_dir=tmp_path)
    config[0]["path"] = str(tmp_path / "other")
    assert catalog.definitions[0].root == str(relative.resolve())
    assert str(relative) not in json.dumps(catalog.list_references(), ensure_ascii=False)


@pytest.mark.parametrize(
    "entry",
    [
        {"alias": "Bad", "path": ".", "description": "x"},
        {"alias": "1bad", "path": ".", "description": "x"},
        {"alias": "bad.", "path": ".", "description": "x"},
        {"alias": "bad", "path": ".", "description": 1},
        {"alias": "bad", "path": 1, "description": "x"},
        {"alias": "bad", "path": "."},
    ],
)
def test_reference_configuration_validation(tmp_path: Path, entry: dict):
    with pytest.raises(ReferenceConfigError):
        ReferenceCatalog([entry], config_base_dir=tmp_path)

    valid = {"alias": "docs", "path": ".", "description": "x"}
    with pytest.raises(ReferenceConfigError, match="重复"):
        ReferenceCatalog([valid, dict(valid)], config_base_dir=tmp_path)
    missing = dict(valid, path="missing")
    with pytest.raises(ReferenceConfigError):
        ReferenceCatalog([missing], config_base_dir=tmp_path)


def test_read_search_and_sha_are_deterministic(tmp_path: Path):
    catalog, root = _catalog(tmp_path)
    (root / "b.txt").write_text("Needle\nother\n", encoding="utf-8")
    (root / "a.txt").write_text("needle\n", encoding="utf-8")
    digest = hashlib.sha256((root / "a.txt").read_bytes()).hexdigest()

    result = catalog.search_reference("docs", "NEEDLE", include="*.txt", limit=20)
    assert [(item["path"], item["line"]) for item in result["matches"]] == [
        ("a.txt", 1), ("b.txt", 1),
    ]
    assert result["matches"][0]["sha256"] == digest
    assert json.dumps(result, ensure_ascii=False, sort_keys=True) == json.dumps(
        catalog.search_reference("docs", "NEEDLE", include="*.txt", limit=20),
        ensure_ascii=False, sort_keys=True,
    )

    read = catalog.read_reference("docs", "a.txt", offset=0, limit=1)
    assert read["lines"] == [{"line": 1, "text": "needle"}]
    assert read["start_line"] == read["end_line"] == 1
    assert read["sha256"] == digest


def test_read_offset_eof_utf8_and_changed_file(tmp_path: Path):
    catalog, root = _catalog(tmp_path)
    path = root / "unicode.txt"
    path.write_text("一\n二\n三", encoding="utf-8")
    first = catalog.read_reference("docs", "unicode.txt", offset=1, limit=1)
    assert first["lines"] == [{"line": 2, "text": "二"}]
    eof = catalog.read_reference("docs", "unicode.txt", offset=99, limit=1)
    assert eof["lines"] == [] and eof["start_line"] is None
    old_digest = first["sha256"]
    path.write_text("new\n", encoding="utf-8")
    changed = catalog.read_reference("docs", "unicode.txt")
    assert changed["lines"] == [{"line": 1, "text": "new"}]
    assert changed["sha256"] != old_digest
    (root / "bad.bin").write_bytes(b"\xff")
    with pytest.raises(ReferenceAccessError, match="UTF-8"):
        catalog.read_reference("docs", "bad.bin")


def test_read_result_metadata_matches_byte_cropped_lines(tmp_path: Path):
    catalog, root = _catalog(tmp_path)
    (root / "wide.txt").write_text(("x" * 4096 + "\n") * 200, encoding="utf-8")

    payload = json.loads(encode_read_reference(catalog, "docs", "wide.txt"))

    assert payload["returned_lines"] == len(payload["lines"])
    assert payload["start_line"] == payload["lines"][0]["line"]
    assert payload["end_line"] == payload["lines"][-1]["line"]
    assert payload["omitted_lines"] == payload["total_lines"] - payload["returned_lines"]
    assert payload["limit"] == 200
    assert payload["truncated"] is True
    assert len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) <= REFERENCE_READ_RESULT_MAX_BYTES


def test_reference_access_failures_use_executor_error_protocol(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    catalog, root = _catalog(tmp_path)
    (root / "bad.bin").write_bytes(b"\xff")
    (root / "large.txt").write_text("12345", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = AgentState()
    state.begin_task("reference failure")
    registry = create_registry(
        state, workspace_root=workspace,
        memory_store=MemoryStore(workspace, tmp_path / "memory"),
        reference_catalog=catalog,
    )
    executor = ToolExecutor(
        registry, PermissionGate(PermissionPolicy({"read_reference": ALLOW})),
    )

    missing = executor.execute_result(
        "read_reference", {"alias": "docs", "path": "missing.txt"}, state=state,
    )
    assert missing.handler_admitted is True
    assert missing.outcome == "failed"
    assert missing.error_kind == "reference_access_error"
    assert str(root) not in missing.output

    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    traversal = executor.execute_result(
        "read_reference", {"alias": "docs", "path": "../outside.txt"}, state=state,
    )
    (root / "config_local.py").write_text("API_KEY='hidden'", encoding="utf-8")
    sensitive = executor.execute_result(
        "read_reference", {"alias": "docs", "path": "config_local.py"}, state=state,
    )
    assert traversal.outcome == sensitive.outcome == "failed"
    assert traversal.error_kind == sensitive.error_kind == "reference_access_error"

    monkeypatch.setattr(references_module, "MAX_FILE_BYTES", 4)
    oversized = executor.execute_result(
        "read_reference", {"alias": "docs", "path": "large.txt"}, state=state,
    )
    invalid_utf8 = executor.execute_result(
        "read_reference", {"alias": "docs", "path": "bad.bin"}, state=state,
    )
    assert oversized.outcome == invalid_utf8.outcome == "failed"
    assert oversized.error_kind == invalid_utf8.error_kind == "reference_access_error"

    before_generation = state.current_generation_id
    state.record_execution_result(missing)
    assert state.attempts[-1].outcome == "failed"
    assert state.current_generation_id == before_generation
    assert state.verification_evidence == []


def test_search_stops_at_file_entry_and_byte_budgets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    catalog, root = _catalog(tmp_path)
    for index in range(8):
        (root / f"{index:02d}.txt").write_text("needle\n", encoding="utf-8")

    monkeypatch.setattr(references_module, "MAX_SEARCH_FILES", 2)
    result = catalog.search_reference("docs", "needle")
    assert result["scan_truncated"] is True
    assert result["scanned_files"] == 2
    assert len(result["matches"]) == 2

    monkeypatch.setattr(references_module, "MAX_SEARCH_FILES", 2_000)
    monkeypatch.setattr(references_module, "MAX_SEARCH_ENTRIES", 3)
    entry_limited = catalog.search_reference("docs", "needle")
    assert entry_limited["scan_truncated"] is True
    assert entry_limited["visited_entries"] == 3

    monkeypatch.setattr(references_module, "MAX_SEARCH_ENTRIES", MAX_SEARCH_ENTRIES)
    monkeypatch.setattr(references_module, "MAX_SEARCH_BYTES", 10)
    byte_limited = catalog.search_reference("docs", "needle")
    assert byte_limited["scan_truncated"] is True
    assert byte_limited["scanned_bytes"] <= MAX_SEARCH_BYTES
    assert byte_limited["scanned_files"] >= 1


@pytest.mark.parametrize("replacement", ["directory", "fifo", "outside_symlink"])
def test_open_rejects_target_replacement_after_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str,
):
    catalog, root = _catalog(tmp_path)
    target = root / "target.txt"
    target.write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    original_safe_target = catalog._safe_target

    def replace_after_resolve(*args, **kwargs):
        snapshot = original_safe_target(*args, **kwargs)
        target.unlink()
        if replacement == "directory":
            target.mkdir()
        elif replacement == "fifo":
            if not hasattr(os, "mkfifo"):
                pytest.skip("platform has no FIFO")
            os.mkfifo(target)
        else:
            target.symlink_to(outside)
        return snapshot

    monkeypatch.setattr(catalog, "_safe_target", replace_after_resolve)
    with pytest.raises(ReferenceAccessError):
        catalog.read_reference("docs", "target.txt")


def test_fallback_rejects_second_canonical_resolution_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    catalog, root = _catalog(tmp_path)
    target = root / "target.txt"
    target.write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    original_realpath = os.path.realpath
    calls = 0

    def changing_realpath(path):
        nonlocal calls
        calls += 1
        return original_realpath(path) if calls == 1 else str(outside)

    monkeypatch.setattr(catalog, "_supports_dir_fd", lambda: False)
    monkeypatch.setattr(os.path, "realpath", changing_realpath)
    with pytest.raises(ReferenceAccessError):
        catalog.read_reference("docs", "target.txt")
    assert calls >= 2


def test_session_excludes_reference_root_and_resume_rebuilds_current_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    old_root = tmp_path / "old-reference"
    old_root.mkdir()
    current_root = tmp_path / "current-reference"
    current_root.mkdir()
    state = AgentState()
    state.begin_task("resume references")
    context = ContextManager(state, [{"role": "user", "content": "continue"}])
    store = SessionStore(tmp_path / "sessions")
    original = runtime_config.REFERENCES
    try:
        monkeypatch.setattr(runtime_config, "REFERENCES", [{
            "alias": "docs", "path": str(old_root), "description": "old",
        }])
        envelope = store.save(None, state, context, workspace_root=workspace, handoff_status="clean")
        session_text = store.path_for(envelope["session_id"]).read_text(encoding="utf-8")
        assert str(old_root) not in session_text

        monkeypatch.setattr(runtime_config, "REFERENCES", [{
            "alias": "docs", "path": str(current_root), "description": "current",
        }])
        runtime = prepare_resume(store, envelope["session_id"], workspace).claim()
        assert runtime.registry._reference_catalog.definitions[0].root == str(current_root)
        assert runtime.registry._reference_catalog.definitions[0].root != str(old_root)
    finally:
        runtime_config.REFERENCES = original


def test_successful_reference_investigation_can_continue_crash_issue(
    tmp_path: Path,
):
    catalog, root = _catalog(tmp_path)
    (root / "facts.txt").write_text("needle", encoding="utf-8")
    state = AgentState()
    state.begin_task("crash reference investigation")
    arguments = {"alias": "docs", "query": "needle"}
    state.begin_crash_recovery(
        "source-session", 1, 1, "a" * 64, 0, [{
            "invocation_id": "call-1", "tool": "search_reference",
            "effect_class": "none", "handler_admitted": True,
            "attempt_id": "a-1", "generation_id": 0, "pre_generation_id": 0,
            "permission": "allowed", "arguments_summary": arguments,
            "arguments_hash": canonical_arguments_hash(arguments),
        }],
    )
    issue = state.crash_issues[0]
    state.resolve_crash_issue(issue.issue_id, "investigate", "读取当前资料")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor = ToolExecutor(
        create_registry(
            state, workspace_root=workspace,
            memory_store=MemoryStore(workspace, tmp_path / "memory"),
            reference_catalog=catalog,
        ),
        PermissionGate(PermissionPolicy({"search_reference": ALLOW})),
    )
    failed = executor.execute_result(
        "search_reference", {"alias": "docs", "query": "needle", "path": "missing"},
        state=state,
    )
    assert failed.outcome == "failed"
    state.record_execution_result(failed)
    with pytest.raises(PlanRejected, match="成功、获准且无副作用"):
        state.resolve_crash_issue(issue.issue_id, "continue", "失败调查不能结算")

    result = executor.execute_result("search_reference", arguments, state=state)
    assert result.outcome == "succeeded"
    attempt = state.record_execution_result(result)
    assert attempt is not None
    assert state.resolve_crash_issue(issue.issue_id, "continue", "调查完成").decision == "continue"


def test_boundaries_alias_isolation_and_symlinks(tmp_path: Path):
    catalog, root = _catalog(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    (root / "inside.txt").write_text("inside", encoding="utf-8")
    (other / "secret.txt").write_text("secret", encoding="utf-8")
    (root / "escape").symlink_to(other / "secret.txt")
    (root / "up").symlink_to(other, target_is_directory=True)
    with pytest.raises(ReferenceAccessError):
        catalog.read_reference("docs", "/etc/passwd")
    with pytest.raises(ReferenceAccessError):
        catalog.read_reference("docs", "../secret.txt")
    with pytest.raises(ReferenceAccessError):
        catalog.read_reference("docs", ".")
    with pytest.raises(ReferenceAccessError):
        catalog.read_reference("docs", "escape")
    with pytest.raises(ReferenceAccessError):
        catalog.search_reference("docs", "secret", path="up")
    with pytest.raises(ReferenceAccessError):
        catalog.read_reference("unknown", "inside.txt")


def test_sensitive_files_and_wide_root_search_skip(tmp_path: Path):
    wide = tmp_path / "wide"
    wide.mkdir()
    sensitive = wide / "sensitive"
    sensitive.mkdir()
    (sensitive / "secret.txt").write_text("needle", encoding="utf-8")
    (wide / "config_local.py").write_text("API_KEY='secret'", encoding="utf-8")
    memory = tmp_path / "memory"
    memory.mkdir()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    with pytest.raises(ReferenceConfigError):
        ReferenceCatalog(
            [{"alias": "bad", "path": str(memory), "description": "x"}],
            sensitive_roots=(memory, sessions), config_base_dir=tmp_path,
        )
    catalog = ReferenceCatalog(
        [{"alias": "wide", "path": str(wide), "description": "x"}],
        sensitive_roots=(sensitive, sessions), config_base_dir=tmp_path,
    )
    result = catalog.search_reference("wide", "needle")
    assert result["matches"] == []
    with pytest.raises(ReferenceAccessError):
        catalog.read_reference("wide", "config_local.py")
    with pytest.raises(ReferenceAccessError):
        catalog.read_reference("wide", "sensitive/secret.txt")


def test_search_limits_and_include(tmp_path: Path):
    catalog, root = _catalog(tmp_path)
    for index in range(105):
        (root / f"{index:03d}.txt").write_text("needle\n", encoding="utf-8")
    result = catalog.search_reference("docs", "needle", limit=MAX_SEARCH_MATCHES)
    assert len(result["matches"]) == MAX_SEARCH_MATCHES
    assert result["returned_matches"] == len(result["matches"])
    assert result["total_matches"] == 105
    assert result["scan_truncated"] is False
    assert result["truncated"] is True
    assert catalog.search_reference("docs", "needle", include="*.py")["matches"] == []


def test_parent_tools_permissions_and_child_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    catalog, root = _catalog(tmp_path)
    (root / "a.txt").write_text("needle", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    memory = MemoryStore(workspace, tmp_path / "memory")
    state = AgentState()
    state.begin_task("references")
    registry = create_registry(
        state, workspace_root=workspace, memory_store=memory, reference_catalog=catalog,
    )
    assert {name for name in (tool.name for tool in registry.list_tools()) if "reference" in name} == {
        "list_references", "search_reference", "read_reference",
    }
    child = registry.filtered_for_subagent({"list_references", "search_reference", "read_reference"})
    assert child.list_tools() == []
    assert registry.get("search_reference").effect_class == "none"
    assert registry.get("read_reference").delegation_capability == "unavailable"
    assert PermissionPolicy().check("list_references") == ALLOW
    assert PermissionPolicy().check("search_reference", "docs:.") != ALLOW
    assert PermissionPolicy().check("read_reference", "docs:a.txt") != ALLOW
    assert PermissionGate._extract_pattern("read_reference", {"alias": "docs", "path": "a.txt"}) == "docs:a.txt"

    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt: asked.append(prompt) or "reject")
    result = ToolExecutor(registry).execute_result(
        "read_reference", {"alias": "docs", "path": "a.txt"}, state=state,
    )
    assert result.outcome == "denied" and not result.handler_admitted
    assert "alias=docs" in asked[0] and "path=a.txt" in asked[0]
    assert str(root) not in asked[0]


def test_reference_results_are_complete_json_and_excerpt_is_metadata_only(tmp_path: Path):
    catalog, root = _catalog(tmp_path)
    (root / "a.txt").write_text("TOP-SECRET-REFERENCE-BODY\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = AgentState()
    state.begin_task("references")
    registry = create_registry(
        state, workspace_root=workspace,
        memory_store=MemoryStore(workspace, tmp_path / "memory"),
        reference_catalog=catalog,
    )
    executor = ToolExecutor(
        registry,
        PermissionGate(PermissionPolicy({"read_reference": ALLOW, "search_reference": ALLOW})),
    )
    read = executor.execute_result("read_reference", {"alias": "docs", "path": "a.txt"}, state=state)
    assert json.loads(read.output)["lines"][0]["text"] == "TOP-SECRET-REFERENCE-BODY"
    assert "TOP-SECRET" not in read.output_excerpt
    assert str(root) not in read.output_excerpt
    assert len(read.tool_content().encode("utf-8")) <= REFERENCE_READ_RESULT_MAX_BYTES
    search = executor.execute_result("search_reference", {"alias": "docs", "query": "secret"}, state=state)
    assert json.loads(search.output)["matches"]
    assert "TOP-SECRET" not in search.output_excerpt
    assert len(search.tool_content().encode("utf-8")) <= REFERENCE_SEARCH_RESULT_MAX_BYTES


def test_reference_tools_are_allowed_in_exploring_but_not_approval(tmp_path: Path):
    catalog, root = _catalog(tmp_path)
    (root / "a.txt").write_text("x", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = AgentState()
    state.begin_task("plan")
    registry = create_registry(
        state, workspace_root=workspace,
        memory_store=MemoryStore(workspace, tmp_path / "memory"),
        reference_catalog=catalog,
    )
    executor = ToolExecutor(
        registry, PermissionGate(PermissionPolicy({"list_references": ALLOW})),
    )
    assert executor.execute_result("list_references", {}, state=state).handler_admitted
    state.begin_plan()
    assert executor.execute_result("list_references", {}, state=state).handler_admitted
    state.planning_state = state.planning_state.__class__(
        mode="plan_only", phase="awaiting_approval", active_revision_id=1,
        active_trigger_id=None, replans_used=0, replans_remaining=1,
        trigger_no_progress_commits=0,
    )
    blocked = executor.execute_result("list_references", {}, state=state)
    assert blocked.outcome == "invalid" and blocked.error_kind == "planning_phase_gate"
