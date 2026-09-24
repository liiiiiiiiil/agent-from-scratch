"""Private, atomic session storage and durable tool boundaries.

Schema 4 keeps State, Context, child snapshots, and the current tool boundary in one atomic
session file. Loading remains a side-effect-free validation operation;
runtime construction and resume admission live in :mod:`mini_agent.resume`.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
from typing import Any

from mini_agent import __version__
from mini_agent.config import MAX_SESSION_FILE_BYTES
from mini_agent.state import (
    AgentState, SessionExportError, canonical_arguments_hash, delegation_result_hash,
    redacted_arguments,
)


SCHEMA_VERSION = 4
SCHEMA_3_VERSION = 3
SCHEMA_2_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
_INTEGRITY_ALGORITHM = "sha256"
_MANIFEST_FORMAT = "mini_agent.workspace_manifest"
_MANIFEST_FORMAT_VERSION = 1
_MAX_MANIFEST_ENTRIES = 4096
_MAX_MANIFEST_DIRECTORY_ENTRIES = 2048
_MAX_CRASH_CLAIM_BYTES = 128 * 1024
_NO_OUTER_ATTEMPT_BOUNDARY_TOOLS = {
    "begin_plan", "cancel_planning", "commit_plan", "update_plan_progress", "request_replan",
    "recover",
}
_MAX_PENDING_DELEGATION_RESULTS_BYTES = 128 * 1024
_MAX_PENDING_DELEGATION_RESULT_BYTES = 16 * 1024
_MAX_CHILD_SESSION_SNAPSHOTS = 3
_MAX_CHILD_SESSION_SNAPSHOT_BYTES = 256 * 1024
_MAX_CHILD_SESSION_SNAPSHOTS_BYTES = 720 * 1024
_CHILD_SESSION_UUID_RE = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)
_DELEGATION_RESULT_FIELDS = {
    "result_id", "delegation_id", "subagent_id", "parent_task_id", "outcome",
    "summary", "findings", "evidence", "limitations", "usage", "contract_hash",
    "started_at", "finished_at", "error_kind", "error_detail", "model_profile",
    "binding_fingerprint", "agent_profile", "agent_profile_fingerprint",
    "round_index",
}


def _canonical_delegation_result(raw: Any) -> tuple[str, str, dict[str, Any]]:
    """Validate and hash one persisted child result without importing delegation."""
    if not isinstance(raw, dict):
        raise SessionValidationError("待交付委派结果必须是 object")
    if set(raw) - _DELEGATION_RESULT_FIELDS:
        raise SessionValidationError("待交付委派结果含未知字段")
    role_id = raw.get("agent_profile")
    role_fingerprint = raw.get("agent_profile_fingerprint")
    if (role_id is None) != (role_fingerprint is None):
        raise SessionValidationError("待交付委派结果 agent_profile 字段不完整")
    if role_id is not None and (
            not isinstance(role_id, str)
            or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", role_id)
            or not isinstance(role_fingerprint, str)
            or not re.fullmatch(r"[0-9a-f]{64}", role_fingerprint)):
        raise SessionValidationError("待交付委派结果 agent_profile 身份无效")
    required = {
        "result_id", "delegation_id", "subagent_id", "parent_task_id", "outcome",
        "summary", "findings", "evidence", "limitations", "usage", "contract_hash",
        "started_at", "finished_at",
    }
    if not required.issubset(raw):
        raise SessionValidationError("待交付委派结果字段不完整")
    if (not isinstance(raw["result_id"], str) or not raw["result_id"]
            or not isinstance(raw["delegation_id"], str) or not raw["delegation_id"]
            or not isinstance(raw["subagent_id"], str) or not raw["subagent_id"]
            or not isinstance(raw["parent_task_id"], str)
            or raw["outcome"] not in {"completed", "failed", "timed_out", "cancelled", "budget_exhausted"}
            or not isinstance(raw["summary"], str)
            or not isinstance(raw["findings"], list)
            or not isinstance(raw["evidence"], list)
            or not isinstance(raw["limitations"], list)
            or not isinstance(raw["usage"], dict)
            or not isinstance(raw["contract_hash"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", raw["contract_hash"])
            or not isinstance(raw["started_at"], str)
            or not isinstance(raw["finished_at"], str)):
        raise SessionValidationError("待交付委派结果字段值无效")
    if "round_index" in raw and (
            isinstance(raw["round_index"], bool) or not isinstance(raw["round_index"], int)
            or not 1 <= raw["round_index"] <= 4):
        raise SessionValidationError("待交付委派结果 round_index 无效")
    normalized = json.loads(json.dumps(raw, ensure_ascii=False, sort_keys=True))
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_PENDING_DELEGATION_RESULT_BYTES:
        raise SessionSizeError("单个待交付委派结果超过大小上限")
    return encoded.decode("utf-8"), delegation_result_hash(normalized), normalized


class SessionError(RuntimeError):
    """Base class for safe session storage failures."""


class SessionValidationError(SessionError):
    """The session file is malformed, unsupported, or internally inconsistent."""


class SessionBusyError(SessionError):
    """Another writer owns the session lock, including an unverified stale lock."""


class SessionSizeError(SessionError):
    """A session file or serialized envelope exceeds the configured bound."""


class SessionCommitUncertainError(SessionError):
    """The file was replaced, but final durability or lock cleanup failed."""

    def __init__(self, session_id: str, detail: str) -> None:
        super().__init__(detail)
        self.session_id = session_id


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _normal_workspace_root(value: str | os.PathLike[str] | None) -> str:
    candidate = os.fspath(value) if value is not None else os.getcwd()
    if not isinstance(candidate, str) or not candidate:
        raise SessionValidationError("workspace_root 必须是非空路径")
    try:
        normalized = os.path.realpath(os.path.abspath(os.path.expanduser(candidate)))
    except (OSError, TypeError, ValueError) as error:
        raise SessionValidationError(f"workspace_root 无法规范化: {type(error).__name__}") from error
    if not os.path.isabs(normalized):
        raise SessionValidationError("workspace_root 必须是绝对路径")
    return normalized


def _new_session_id() -> str:
    # URL-safe random bytes avoid predictable task-local counters and are
    # directly safe to use as a filename component.
    return secrets.token_urlsafe(24)


def _relative_workspace_path(root: str, value: Any) -> tuple[str | None, str | None]:
    """Keep the task's path spelling and reject links below the workspace."""
    if not isinstance(value, str) or not value or "\x00" in value:
        return None, "路径必须是非空字符串且不含空字符"
    try:
        candidate = os.path.abspath(value if os.path.isabs(value) else os.path.join(root, value))
        if os.path.commonpath((root, candidate)) != root:
            return None, "路径位于工作区之外"
        relative = os.path.relpath(candidate, root)
        current = root
        for part in [] if relative == os.curdir else relative.split(os.sep):
            current = os.path.join(current, part)
            try:
                if stat.S_ISLNK(os.lstat(current).st_mode):
                    return None, "路径包含符号链接，不能建立恢复基线"
            except FileNotFoundError:
                break
        return "." if relative == os.curdir else relative, None
    except (OSError, TypeError, ValueError) as error:
        return None, f"路径无法规范化: {type(error).__name__}"


def _manifest_kind(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "special"


def _hash_file(path: str) -> tuple[str | None, str | None]:
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest(), None
    except (OSError, ValueError) as error:
        return None, f"文件内容无法检查: {type(error).__name__}"


def _inspect_path(root: str, relative: str, *, include_children: bool = True) -> dict[str, Any]:
    absolute = os.path.join(root, relative) if relative != "." else root
    entry: dict[str, Any] = {"path": relative}
    try:
        info = os.lstat(absolute)
    except FileNotFoundError:
        entry.update({"kind": "absent", "available": True})
        return entry
    except OSError as error:
        entry.update({"kind": "unavailable", "available": False,
                      "reason": f"路径无法检查: {type(error).__name__}"})
        return entry

    kind = _manifest_kind(info.st_mode)
    entry.update({"kind": kind, "available": True})
    if kind == "file":
        digest, reason = _hash_file(absolute)
        if reason:
            entry.update({"available": False, "reason": reason})
        else:
            entry["sha256"] = digest
        return entry
    if kind == "symlink":
        entry.update({"available": False, "reason": "符号链接不作为恢复基线"})
        return entry
    if kind != "directory" or not include_children:
        if kind == "special":
            entry.update({"available": False, "reason": "特殊文件类型不作为恢复基线"})
        return entry

    try:
        with os.scandir(absolute) as iterator:
            children = sorted(iterator, key=lambda item: item.name)
    except OSError as error:
        entry.update({"available": False, "reason": f"目录条目无法检查: {type(error).__name__}"})
        return entry
    if len(children) > _MAX_MANIFEST_DIRECTORY_ENTRIES:
        entry.update({"available": False,
                      "reason": f"目录条目超过 {_MAX_MANIFEST_DIRECTORY_ENTRIES} 项上限"})
        return entry
    child_records: list[dict[str, Any]] = []
    for child in children:
        child_record: dict[str, Any] = {"name": child.name}
        try:
            child_info = child.stat(follow_symlinks=False)
            child_kind = _manifest_kind(child_info.st_mode)
            child_record["kind"] = child_kind
            child_record["available"] = True
            if child_kind == "file":
                child_digest, reason = _hash_file(child.path)
                if reason:
                    entry.update({"available": False, "reason": reason})
                    child_record["available"] = False
                    child_record["reason"] = reason
                else:
                    child_record["sha256"] = child_digest
            elif child_kind == "symlink":
                child_record["available"] = False
                child_record["reason"] = "符号链接不作为恢复基线"
                entry["available"] = False
                entry.setdefault("reason", "目录包含不可检查的符号链接")
            else:
                child_record["available"] = True
        except OSError as error:
            child_record.update({"kind": "unavailable", "available": False,
                                 "reason": f"目录条目无法检查: {type(error).__name__}"})
            entry.update({"available": False, "reason": child_record["reason"]})
        child_records.append(child_record)
    entry["entries"] = child_records
    return entry


def _collect_manifest_paths(state: dict[str, Any]) -> tuple[list[tuple[str, str]], list[str]]:
    """Return (path, source) candidates from structured task facts.

    Shell command text is intentionally never inspected.  A grep search
    directory is expanded at save time, including files with no matches.
    """
    candidates: list[tuple[str, str]] = []
    issues: list[str] = []

    def add(value: Any, source: str) -> None:
        if isinstance(value, str) and value:
            if value == "<unavailable>":
                issues.append(f"{source} 路径不可用")
                return
            candidates.append((value, source))
        elif value is not None:
            issues.append(f"{source} 路径无法建立清单")

    private = state.get("private", {})
    originals = private.get("original_attempt_arguments", []) if isinstance(private, dict) else []
    for item in originals:
        if not isinstance(item, dict) or not isinstance(item.get("arguments"), dict):
            continue
        arguments = item["arguments"]
        tool = next((attempt.get("tool") for attempt in state.get("attempts", [])
                     if isinstance(attempt, dict) and attempt.get("attempt_id") == item.get("attempt_id")), "")
        if tool in {"read_file", "write_file", "edit_file", "list_dir"}:
            add(arguments.get("path", ".") if tool == "list_dir" else arguments.get("path"), tool)
        elif tool == "grep":
            add(arguments.get("path", "."), "grep directory")
        elif tool == "rollback_checkpoint":
            checkpoint_id = arguments.get("checkpoint_id")
            for checkpoint in state.get("checkpoint_metadata", []):
                if isinstance(checkpoint, dict) and checkpoint.get("checkpoint_id") == checkpoint_id:
                    add(checkpoint.get("path"), "checkpoint")

    for path in state.get("files_changed", []):
        add(path, "files_changed")
    for failure in state.get("failures", []):
        if isinstance(failure, dict):
            for path in failure.get("affected_files", []) or []:
                add(path, "failure")
    for checkpoint in state.get("checkpoint_metadata", []):
        if isinstance(checkpoint, dict):
            add(checkpoint.get("path"), "checkpoint")
    return candidates, issues


def _expand_grep_directory(root: str, relative: str,
                           entries: dict[str, dict[str, Any]], issues: list[str]) -> None:
    """Snapshot every directory in a recursive grep scope, within one bound."""
    pending = [relative]
    observed = 0
    while pending:
        current = pending.pop()
        absolute = os.path.join(root, current)
        try:
            with os.scandir(absolute) as iterator:
                children = list(iterator)
        except OSError as error:
            issues.append(f"grep directory {current} 无法遍历: {type(error).__name__}")
            return
        observed += len(children)
        if observed > _MAX_MANIFEST_ENTRIES:
            issues.append(f"grep 搜索范围超过 {_MAX_MANIFEST_ENTRIES} 项上限")
            return
        for child in children:
            try:
                if stat.S_ISDIR(child.stat(follow_symlinks=False).st_mode):
                    child_relative = os.path.relpath(child.path, root)
                    if child_relative not in entries:
                        entries[child_relative] = _inspect_path(root, child_relative)
                    pending.append(child_relative)
            except OSError as error:
                issues.append(f"grep directory {current} 条目无法检查: {type(error).__name__}")
                return


def build_workspace_manifest(state: dict[str, Any], workspace_root: str | os.PathLike[str] | None) -> dict[str, Any]:
    """Build a deterministic, bounded content baseline for a v0.31 save."""
    root = _normal_workspace_root(workspace_root)
    candidates, issues = _collect_manifest_paths(state)
    entries: dict[str, dict[str, Any]] = {}
    for value, source in candidates:
        relative, error = _relative_workspace_path(root, value)
        if error:
            issues.append(f"{source}: {error}")
            continue
        assert relative is not None
        if relative not in entries:
            entries[relative] = _inspect_path(root, relative)
        if source == "grep directory" and entries[relative].get("kind") == "directory":
            _expand_grep_directory(root, relative, entries, issues)
    if len(entries) > _MAX_MANIFEST_ENTRIES:
        issues.append(f"工作区清单超过 {_MAX_MANIFEST_ENTRIES} 项上限")
    normalized_entries = [entries[key] for key in sorted(entries)[:_MAX_MANIFEST_ENTRIES]]
    for entry in normalized_entries:
        if not entry.get("available", False):
            issues.append(f"无法检查路径: {entry.get('path', '<unknown>')}")
    return {
        "format": _MANIFEST_FORMAT,
        "format_version": _MANIFEST_FORMAT_VERSION,
        "root": root,
        "complete": not issues,
        "recoverable": not issues,
        "issues": sorted(set(issues)),
        "entries": normalized_entries,
    }


def _validate_context_export(payload: Any, *, allow_partial: bool = False) -> None:
    if not isinstance(payload, dict) or payload.get("format") != "mini_agent.context" or payload.get("format_version") != 1:
        raise SessionValidationError("未知或不支持的 Context 导出版本")
    if not isinstance(payload.get("history"), list):
        raise SessionValidationError("Context history 必须是列表")
    if not isinstance(payload.get("summary"), str):
        raise SessionValidationError("Context summary 必须是字符串")
    if not isinstance(payload.get("compacted"), bool):
        raise SessionValidationError("Context compacted 类型无效")
    if not isinstance(payload.get("summarized_rounds"), int) or payload["summarized_rounds"] < 0:
        raise SessionValidationError("Context summarized_rounds 无效")
    notice = payload.get("runtime_notice")
    if notice is not None and not isinstance(notice, str):
        raise SessionValidationError("Context runtime_notice 类型无效")

    seen_call_ids: set[str] = set()
    expected: list[str] = []
    partial_assistant = False
    for message in payload["history"]:
        if not isinstance(message, dict) or not isinstance(message.get("role"), str):
            raise SessionValidationError("Context history 含无效消息")
        role = message["role"]
        if expected:
            if role != "tool" or message.get("tool_call_id") != expected[0]:
                raise SessionValidationError("assistant tool call 缺少按序对应的 tool 结果")
            expected.pop(0)
            continue
        if role == "tool":
            raise SessionValidationError("孤立的 role=tool 结果")
        if role != "assistant" or not message.get("tool_calls"):
            continue
        calls = message["tool_calls"]
        if not isinstance(calls, list) or not calls:
            raise SessionValidationError("assistant tool_calls 形状无效")
        for call in calls:
            if not isinstance(call, dict) or not isinstance(call.get("id"), str) or not call["id"]:
                raise SessionValidationError("tool_call_id 无效")
            call_id = call["id"]
            if call_id in seen_call_ids:
                raise SessionValidationError("tool_call_id 重复")
            seen_call_ids.add(call_id)
            function = call.get("function")
            if not isinstance(function, dict) or not isinstance(function.get("name"), str):
                raise SessionValidationError("tool call function 无效")
            raw_arguments = function.get("arguments")
            if isinstance(raw_arguments, str):
                try:
                    arguments = json.loads(raw_arguments)
                except (TypeError, json.JSONDecodeError) as error:
                    raise SessionValidationError("tool call arguments 不是合法 JSON") from error
                if not isinstance(arguments, dict):
                    raise SessionValidationError("tool call arguments 必须是 JSON object")
            elif not isinstance(raw_arguments, dict):
                raise SessionValidationError("tool call arguments 类型无效")
            expected.append(call_id)
        if expected and message is payload["history"][-1]:
            partial_assistant = True
    if expected and payload["history"] and isinstance(payload["history"][-1], dict):
        partial_assistant = partial_assistant or payload["history"][-1].get("role") == "tool"
    if expected and not (allow_partial and partial_assistant):
        raise SessionValidationError("assistant tool call 结果未完整回灌")


def _empty_tool_boundary() -> dict[str, Any]:
    return {
        "round_id": 0,
        "assistant_message": None,
        "calls": [],
        "status": "committed",
    }


def _validate_tool_boundary(boundary: Any, state: dict[str, Any],
                           context: dict[str, Any]) -> None:
    """Validate the durable round ledger against State and Context exports."""
    if not isinstance(boundary, dict):
        raise SessionValidationError("tool_boundary 必须是 object")
    expected_fields = {"round_id", "assistant_message", "calls", "status"}
    optional_fields = {"pending_delegation_results"}
    if set(boundary) - expected_fields - optional_fields:
        raise SessionValidationError("tool_boundary 字段无效")
    round_id = boundary.get("round_id")
    if isinstance(round_id, bool) or not isinstance(round_id, int) or round_id < 0:
        raise SessionValidationError("tool_boundary round_id 无效")
    status = boundary.get("status")
    if status not in {"pending", "committed"}:
        raise SessionValidationError("tool_boundary status 无效")
    calls = boundary.get("calls")
    if not isinstance(calls, list):
        raise SessionValidationError("tool_boundary calls 必须是列表")
    assistant = boundary.get("assistant_message")
    if not calls:
        if assistant is not None:
            raise SessionValidationError("空 tool_boundary 不能包含 assistant_message")
        if status != "committed":
            raise SessionValidationError("空 tool_boundary 不能是 pending")
        return
    if not isinstance(assistant, dict) or assistant.get("role") != "assistant":
        raise SessionValidationError("tool_boundary 缺少 assistant_message")
    assistant_calls = assistant.get("tool_calls")
    if not isinstance(assistant_calls, list) or len(assistant_calls) != len(calls):
        raise SessionValidationError("tool_boundary assistant 调用数量不一致")
    history = context.get("history") if isinstance(context, dict) else None
    if not isinstance(history, list):
        raise SessionValidationError("tool_boundary 缺少 Context history")
    round_count = sum(
        1 for item in history
        if isinstance(item, dict) and item.get("role") == "assistant" and item.get("tool_calls")
    )
    if round_id != round_count:
        raise SessionValidationError("tool_boundary round_id 倒退或与 Context 不一致")
    raw_ids: list[str] = []
    for raw in assistant_calls:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
            raise SessionValidationError("tool_boundary 原始 tool_call_id 无效")
        raw_ids.append(raw["id"])
    if len(raw_ids) != len(set(raw_ids)):
        raise SessionValidationError("tool_boundary 原始 tool_call_id 重复")

    state_attempts = {
        item.get("attempt_id"): item
        for item in state.get("attempts", [])
        if isinstance(item, dict)
    }
    generation_ids = {
        item.get("generation_id") for item in state.get("generations", [])
        if isinstance(item, dict)
    }
    private = state.get("private")
    pending_attempts = set(private.get("pending_attempts", [])) if isinstance(private, dict) else set()
    state_delegations = {
        item.get("delegation_id"): item
        for item in state.get("delegation_records", [])
        if isinstance(item, dict) and isinstance(item.get("delegation_id"), str)
    }
    raw_pending_results = boundary.get("pending_delegation_results", [])
    if not isinstance(raw_pending_results, list):
        raise SessionValidationError("pending_delegation_results 必须是列表")
    pending_result_by_invocation: dict[str, dict[str, Any]] = {}
    pending_result_bytes = 0
    for item in raw_pending_results:
        if not isinstance(item, dict):
            raise SessionValidationError("待交付委派结果记录无效")
        expected_result_fields = {
            "invocation_id", "delegation_id", "result_id", "result_hash",
            "result_json", "result_summary",
        }
        if set(item) != expected_result_fields:
            raise SessionValidationError("待交付委派结果字段无效")
        invocation_id = item.get("invocation_id")
        if (not isinstance(invocation_id, str) or not invocation_id
                or invocation_id in pending_result_by_invocation):
            raise SessionValidationError("待交付委派结果 invocation_id 重复或无效")
        result_json = item.get("result_json")
        if not isinstance(result_json, str):
            raise SessionValidationError("待交付委派结果原文无效")
        if len(result_json.encode("utf-8")) > _MAX_PENDING_DELEGATION_RESULT_BYTES:
            raise SessionSizeError("单个待交付委派结果超过大小上限")
        try:
            parsed = json.loads(result_json)
        except (TypeError, json.JSONDecodeError) as error:
            raise SessionValidationError("待交付委派结果原文不是合法 JSON") from error
        canonical, result_hash, parsed = _canonical_delegation_result(parsed)
        if canonical != result_json:
            raise SessionValidationError("待交付委派结果原文不是 canonical JSON")
        if item.get("result_hash") != result_hash:
            raise SessionValidationError("待交付委派结果 hash 不匹配")
        if item.get("result_id") != parsed.get("result_id"):
            raise SessionValidationError("待交付委派结果 result_id 不匹配")
        if not isinstance(item.get("delegation_id"), str) or item["delegation_id"] != parsed.get("delegation_id"):
            raise SessionValidationError("待交付委派结果 delegation_id 不匹配")
        if not isinstance(item.get("result_summary"), str) or len(item["result_summary"]) > 1200:
            raise SessionValidationError("待交付委派结果摘要无效")
        pending_result_bytes += len(result_json.encode("utf-8"))
        pending_result_by_invocation[invocation_id] = item
    if pending_result_bytes > _MAX_PENDING_DELEGATION_RESULTS_BYTES:
        raise SessionSizeError("待交付委派结果区超过大小上限")
    seen_invocations: set[str] = set()
    seen_pending = False
    referenced_pending_attempts: set[str] = set()
    for index, call in enumerate(calls):
        if not isinstance(call, dict):
            raise SessionValidationError("tool_boundary call 无效")
        call_fields = {
            "sequence", "invocation_id", "tool_call_id", "tool", "arguments_summary",
            "arguments_hash", "effect_class", "permission", "handler_admitted", "attempt_id",
            "pre_generation_id", "generation_id", "status", "result",
        }
        extended_call_fields = call_fields | {
            "delegation_id", "subagent_id", "delegation_result_hash",
            "agent_profile", "agent_profile_fingerprint", "child_session_id",
            "round_index",
        }
        # v0.32 boundaries did not retain the original reservation hash.  They
        # remain readable for clean replay diagnostics, but a pending admitted
        # call without this field is not eligible for crash hand-off because
        # reconstructing it from redacted arguments would break its identity.
        legacy_call_fields = call_fields - {"arguments_hash"}
        if not set(call).issubset(extended_call_fields) or not (
                call_fields - {"arguments_hash"} <= set(call)
                or call_fields <= set(call)):
            raise SessionValidationError("tool_boundary call 字段无效")
        if call.get("sequence") != index:
            raise SessionValidationError("tool_boundary 调用顺序无效")
        invocation_id = call.get("invocation_id")
        if not isinstance(invocation_id, str) or not invocation_id or invocation_id in seen_invocations:
            raise SessionValidationError("tool_boundary invocation_id 重复或无效")
        seen_invocations.add(invocation_id)
        if call.get("tool_call_id") != raw_ids[index] or not isinstance(call.get("tool"), str):
            raise SessionValidationError("tool_boundary 调用身份不匹配")
        delegation_id = call.get("delegation_id")
        if delegation_id is not None and (
                call.get("tool") not in {"delegate_task", "spawn_subagent", "followup_subagent", "get_subagent_result"}
                or not isinstance(delegation_id, str) or not delegation_id):
            raise SessionValidationError("tool_boundary delegation_id 引用无效")
        child_session_id = call.get("child_session_id")
        if child_session_id is not None and (
                call.get("tool") not in {"spawn_subagent", "followup_subagent", "get_subagent_result"}
                or not isinstance(child_session_id, str)
                or not _CHILD_SESSION_UUID_RE.fullmatch(child_session_id)):
            raise SessionValidationError("tool_boundary child_session_id 引用无效")
        if call.get("subagent_id") is not None and (
                call.get("tool") not in {"delegate_task", "spawn_subagent", "followup_subagent", "get_subagent_result"}
                or not isinstance(call.get("subagent_id"), str)
                or not call.get("subagent_id")
        ):
            raise SessionValidationError("tool_boundary subagent_id 引用无效")
        role_id = call.get("agent_profile")
        role_fingerprint = call.get("agent_profile_fingerprint")
        if (role_id is None) != (role_fingerprint is None):
            raise SessionValidationError("tool_boundary agent_profile 身份字段不完整")
        if role_id is not None and (
                call.get("tool") not in {"delegate_task", "spawn_subagent", "followup_subagent", "get_subagent_result"}
                or not isinstance(role_id, str)
                or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", role_id)
                or not isinstance(role_fingerprint, str)
                or not re.fullmatch(r"[0-9a-f]{64}", role_fingerprint)):
            raise SessionValidationError("tool_boundary agent_profile 身份无效")
        if call.get("delegation_result_hash") is not None and (
                not isinstance(call.get("delegation_result_hash"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", call["delegation_result_hash"])
        ):
            raise SessionValidationError("tool_boundary delegation result hash 无效")
        if call.get("round_index") is not None and (
                isinstance(call.get("round_index"), bool)
                or not isinstance(call.get("round_index"), int)
                or not 1 <= call["round_index"] <= 4):
            raise SessionValidationError("tool_boundary round_index 无效")
        if not isinstance(call.get("arguments_summary"), dict):
            raise SessionValidationError("tool_boundary 参数摘要无效")
        if "arguments_hash" in call and (
                not isinstance(call.get("arguments_hash"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", call["arguments_hash"])):
            raise SessionValidationError("tool_boundary arguments_hash 无效")
        if call.get("effect_class") not in {"none", "possible"}:
            raise SessionValidationError("tool_boundary effect_class 无效")
        if call.get("permission") not in {"allowed", "denied", "not_checked"}:
            raise SessionValidationError("tool_boundary permission 无效")
        if not isinstance(call.get("handler_admitted"), bool):
            raise SessionValidationError("tool_boundary handler_admitted 无效")
        if call.get("status") not in {"pending", "committed"}:
            raise SessionValidationError("tool_boundary call status 无效")
        if call.get("status") == "pending":
            seen_pending = True
        elif seen_pending:
            raise SessionValidationError("tool_boundary 结果必须是模型顺序前缀")
        for name in ("pre_generation_id", "generation_id"):
            value = call.get(name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)
                                      or value not in generation_ids):
                raise SessionValidationError(f"tool_boundary {name} 引用无效")
        attempt_id = call.get("attempt_id")
        if attempt_id is not None and (not isinstance(attempt_id, str) or not attempt_id.startswith("a-")):
            raise SessionValidationError("tool_boundary attempt_id 无效")
        if call.get("handler_admitted") and call.get("permission") != "allowed":
            raise SessionValidationError("handler_admitted 必须对应 allowed permission")
        if (call.get("handler_admitted") and attempt_id is None
                and call.get("tool") not in _NO_OUTER_ATTEMPT_BOUNDARY_TOOLS):
            raise SessionValidationError("handler_admitted 缺少 attempt 引用")
        if call.get("status") == "pending" and call.get("result") is not None:
            raise SessionValidationError("pending call 不能包含 result")
        if call.get("tool") == "delegate_task" and delegation_id is not None:
            delegation = state_delegations.get(delegation_id)
            if delegation is None:
                raise SessionValidationError("tool_boundary delegate_task 缺少 State delegation record")
            if call.get("subagent_id") is not None and call["subagent_id"] != delegation.get("subagent_id"):
                raise SessionValidationError("tool_boundary subagent_id 与 State 不一致")
            if (call.get("agent_profile") != delegation.get("agent_profile")
                    or call.get("agent_profile_fingerprint")
                    != delegation.get("agent_profile_fingerprint")):
                raise SessionValidationError("tool_boundary agent_profile 与 State 不一致")
            if (delegation.get("parent_attempt_id") is not None
                    and delegation["parent_attempt_id"] != attempt_id):
                raise SessionValidationError("tool_boundary parent attempt 与 State 不一致")
            if call.get("status") == "pending" and delegation.get("delivery_status") == "result_ready":
                ready = pending_result_by_invocation.get(invocation_id)
                if ready is None:
                    raise SessionValidationError("result_ready 委派缺少待交付原文")
                if (ready.get("delegation_id") != delegation_id
                        or ready.get("result_id") != delegation.get("result_id")
                        or ready.get("result_hash") != delegation.get("result_hash")):
                    raise SessionValidationError("待交付委派结果与 State 不一致")
                try:
                    ready_raw = json.loads(ready["result_json"])
                except (TypeError, KeyError, json.JSONDecodeError) as error:
                    raise SessionValidationError("待交付委派结果原文无法解析") from error
                if (ready_raw.get("subagent_id") != delegation.get("subagent_id")
                        or ready_raw.get("parent_task_id") != delegation.get("parent_task_id")
                        or ready_raw.get("contract_hash") != delegation.get("task_contract_hash")
                        or ready_raw.get("agent_profile") != delegation.get("agent_profile")
                        or ready_raw.get("agent_profile_fingerprint")
                        != delegation.get("agent_profile_fingerprint")):
                    raise SessionValidationError("待交付委派结果身份或合同不一致")
            if call.get("status") == "committed":
                delivery_status = delegation.get("delivery_status")
                interrupted = (delivery_status == "interrupted"
                               and isinstance(call.get("result"), dict)
                               and call["result"].get("outcome") == "uncertain"
                               and call.get("delegation_result_hash") is None)
                if delivery_status != "committed" and not interrupted:
                    raise SessionValidationError("committed delegate_task 与 State 生命周期不一致")
                result_hash = call.get("delegation_result_hash")
                if result_hash is not None and result_hash != delegation.get("result_hash"):
                    raise SessionValidationError("committed delegate_task result hash 不一致")
                if result_hash is not None:
                    result = call.get("result")
                    content = result.get("content") if isinstance(result, dict) else None
                    try:
                        _, content_hash, content_raw = _canonical_delegation_result(json.loads(content))
                    except (TypeError, json.JSONDecodeError, SessionError) as error:
                        raise SessionValidationError("committed 委派结果原文无效") from error
                    if (content_hash != delegation.get("result_hash")
                            or content_raw.get("result_id") != delegation.get("result_id")
                            or content_raw.get("delegation_id") != delegation_id
                            or content_raw.get("agent_profile") != delegation.get("agent_profile")
                            or content_raw.get("agent_profile_fingerprint")
                            != delegation.get("agent_profile_fingerprint")):
                        raise SessionValidationError("committed 委派结果与 State 不一致")
        elif call.get("tool") in {"spawn_subagent", "followup_subagent"} and child_session_id is not None:
            delegation = state_delegations.get(delegation_id)
            if (delegation is None or delegation.get("mode") != "background"
                    or delegation.get("subagent_id") != child_session_id):
                raise SessionValidationError("后台启动与 State 委派身份不一致")
            if call.get("subagent_id") != child_session_id:
                raise SessionValidationError("后台启动 subagent_id 与 child_session_id 不一致")
            if (call.get("agent_profile") != delegation.get("agent_profile")
                    or call.get("agent_profile_fingerprint")
                    != delegation.get("agent_profile_fingerprint")):
                raise SessionValidationError("后台启动角色与 State 不一致")
            if call.get("status") == "committed":
                try:
                    confirmation = json.loads(call.get("result", {}).get("content", ""))
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    raise SessionValidationError("后台启动确认无效") from error
                if (not isinstance(confirmation, dict) or confirmation.get("accepted") is not True
                        or confirmation.get("child_session_id") != child_session_id
                        or confirmation.get("delegation_id") != delegation_id
                        or confirmation.get("agent_profile") != delegation.get("agent_profile")):
                    raise SessionValidationError("后台启动确认与 State 不一致")
                expected_round = 1 if call.get("tool") == "spawn_subagent" else call.get("round_index")
                if (confirmation.get("round_index") != expected_round
                        or delegation.get("round_index") is not None):
                    # Round ownership lives in the child_session_records ledger.
                    lifecycle = next((item for item in state.get("child_session_records", [])
                                      if item.get("child_session_id") == child_session_id), None)
                    if (lifecycle is None or confirmation.get("round_index") != lifecycle.get("round_index")
                            or call.get("round_index") not in (None, lifecycle.get("round_index"))):
                        raise SessionValidationError("后台启动确认轮次与 State 不一致")
                if status == "committed" and delegation.get("startup_confirmed") is not True:
                    raise SessionValidationError("committed 后台启动缺少 State 启动确认事实")
        elif call.get("tool") == "get_subagent_result" and delegation_id is not None:
            delegation = state_delegations.get(delegation_id)
            if (delegation is None or delegation.get("mode") != "background"
                    or delegation.get("subagent_id") != child_session_id
                    or call.get("subagent_id") != child_session_id):
                raise SessionValidationError("后台结果领取与 State 身份不一致")
            if call.get("status") == "committed" and call.get("delegation_result_hash") is not None:
                if (delegation.get("delivery_status") != "committed"
                        or call.get("delegation_result_hash") != delegation.get("result_hash")):
                    raise SessionValidationError("后台结果领取 hash 与 State 不一致")
                try:
                    _, content_hash, raw_result = _canonical_delegation_result(
                        json.loads(call.get("result", {}).get("content", "")),
                    )
                except (TypeError, ValueError, json.JSONDecodeError, SessionError) as error:
                    raise SessionValidationError("后台结果领取正文无效") from error
                if (content_hash != delegation.get("result_hash")
                        or raw_result.get("result_id") != delegation.get("result_id")
                        or raw_result.get("delegation_id") != delegation_id
                        or raw_result.get("subagent_id") != child_session_id):
                    raise SessionValidationError("后台结果领取正文与 State 不一致")
        elif invocation_id in pending_result_by_invocation:
            raise SessionValidationError("待交付结果只能引用 delegate_task")
        if (call.get("status") == "pending" and call.get("handler_admitted")
                and "arguments_hash" not in call):
            raise SessionValidationError("pending admitted call 缺少 durable arguments_hash")
        result = call.get("result")
        if call.get("status") == "committed":
            if not isinstance(result, dict):
                raise SessionValidationError("committed call 缺少 result")
            result_fields = {"outcome", "content", "output_excerpt", "error_kind", "exit_code"}
            if (set(result) != result_fields
                    or not isinstance(result.get("content"), str)
                    or not isinstance(result.get("output_excerpt"), str)
                    or (result.get("error_kind") is not None
                        and not isinstance(result.get("error_kind"), str))
                    or (result.get("exit_code") is not None
                        and (isinstance(result.get("exit_code"), bool)
                             or not isinstance(result.get("exit_code"), int)))):
                raise SessionValidationError("tool_boundary result 字段无效")
            if result.get("outcome") not in {
                    "succeeded", "failed", "denied", "timeout", "invalid", "uncertain"}:
                raise SessionValidationError("tool_boundary result outcome 无效")
        if attempt_id is not None:
            if call.get("status") == "pending":
                if attempt_id not in pending_attempts:
                    raise SessionValidationError("tool_boundary pending attempt 引用无效")
                referenced_pending_attempts.add(attempt_id)
            else:
                attempt = state_attempts.get(attempt_id)
                if attempt is None:
                    raise SessionValidationError("tool_boundary committed attempt 引用无效")
                for key in ("tool", "effect_class", "permission", "handler_admitted", "generation_id"):
                    if attempt.get(key) != call.get(key):
                        raise SessionValidationError(f"tool_boundary attempt {key} 引用不一致")
                if call.get("pre_generation_id") != attempt.get("pre_generation_id"):
                    raise SessionValidationError("tool_boundary pre_generation_id 引用不一致")
                if ("arguments_hash" in call
                        and call.get("arguments_hash") != attempt.get("arguments_hash")):
                    raise SessionValidationError("tool_boundary arguments_hash 引用不一致")
        elif (call.get("status") == "pending" and call.get("handler_admitted")
              and call.get("tool") not in _NO_OUTER_ATTEMPT_BOUNDARY_TOOLS):
            raise SessionValidationError("pending admitted call 缺少 attempt")

    if status == "committed" and any(call.get("status") != "committed" for call in calls):
        raise SessionValidationError("complete tool_boundary 仍有 pending call")
    if status == "committed" and pending_attempts:
        raise SessionValidationError("complete tool_boundary 仍包含 pending attempt")
    if status == "pending" and pending_attempts != referenced_pending_attempts:
        raise SessionValidationError("tool_boundary 与 State pending attempt 不一致")
    referenced_ready = {
        call.get("invocation_id") for call in calls
        if isinstance(call, dict) and call.get("tool") == "delegate_task"
        and call.get("status") == "pending"
        and state_delegations.get(call.get("delegation_id"), {}).get("delivery_status") == "result_ready"
    }
    if referenced_ready != set(pending_result_by_invocation):
        raise SessionValidationError("tool_boundary 待交付结果与 State result_ready 集合不一致")
    assistant_index = next(
        (index for index in range(len(history) - 1, -1, -1)
         if isinstance(history[index], dict)
         and history[index].get("role") == "assistant"
         and history[index].get("tool_calls")),
        None,
    )
    if assistant_index is None:
        raise SessionValidationError("tool_boundary assistant 消息未写入 Context")
    if history[assistant_index].get("tool_calls") != assistant_calls:
        raise SessionValidationError("tool_boundary assistant 消息与 Context 不一致")
    tool_history = history[assistant_index + 1:]
    committed_ids = [call["tool_call_id"] for call in calls if call["status"] == "committed"]
    actual_ids = [
        item.get("tool_call_id") for item in tool_history
        if isinstance(item, dict) and item.get("role") == "tool"
    ]
    if actual_ids != committed_ids:
        raise SessionValidationError("tool_boundary 结果与 Context 顺序不一致")
    if status == "committed" and len(actual_ids) < len(calls):
        raise SessionValidationError("complete tool_boundary 缺少 Context 结果")


def _validate_child_session_snapshots(
    snapshots: Any, state: dict[str, Any], workspace_root: str,
    *, save_kind: str,
) -> None:
    if not isinstance(snapshots, list) or len(snapshots) > _MAX_CHILD_SESSION_SNAPSHOTS:
        raise SessionValidationError("child_sessions 数量超过上限")
    lifecycle_records = state.get("child_session_records", [])
    lifecycle_by_id = {
        item.get("child_session_id"): item for item in lifecycle_records
        if isinstance(item, dict)
    }
    delegations_by_child: dict[str, list[dict[str, Any]]] = {}
    for record in state.get("delegation_records", []):
        if isinstance(record, dict) and record.get("mode") == "background":
            delegations_by_child.setdefault(record.get("subagent_id", ""), []).append(record)
    expected_workspace = hashlib.sha256(
        os.path.normcase(os.path.realpath(os.path.abspath(workspace_root))).encode("utf-8")
    ).hexdigest()
    seen: set[str] = set()
    total_bytes = 0
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            raise SessionValidationError("child_sessions 含非 object 快照")
        expected_fields = {
            "format", "format_version", "child_session_id", "parent_task_id",
            "workspace_fingerprint", "agent_profile", "agent_profile_fingerprint",
            "model_binding_ref", "skill_identities", "round_index", "cumulative_usage",
            "last_claimed_result_id", "last_claimed_result_hash", "last_result_json",
            "observations", "context",
        }
        if (set(snapshot) != expected_fields
                or snapshot.get("format") != "mini_agent.child_session"
                or snapshot.get("format_version") != 1):
            raise SessionValidationError("child session 快照字段或版本无效")
        child_id = snapshot.get("child_session_id")
        if (not isinstance(child_id, str) or not _CHILD_SESSION_UUID_RE.fullmatch(child_id)
                or child_id in seen):
            raise SessionValidationError("child session 快照 ID 无效或重复")
        seen.add(child_id)
        lifecycle = lifecycle_by_id.get(child_id)
        if lifecycle is None:
            raise SessionValidationError(f"child session {child_id} 缺少 State lifecycle")
        if (snapshot.get("parent_task_id") != state.get("task_id")
                or snapshot.get("parent_task_id") != lifecycle.get("parent_task_id")):
            raise SessionValidationError(f"child session {child_id} parent task_id 不匹配")
        if (snapshot.get("workspace_fingerprint") != expected_workspace
                or snapshot.get("workspace_fingerprint") != lifecycle.get("workspace_fingerprint")):
            raise SessionValidationError(f"child session {child_id} workspace 身份不匹配")
        if (snapshot.get("agent_profile") != lifecycle.get("agent_profile")
                or snapshot.get("agent_profile_fingerprint") != lifecycle.get("agent_profile_fingerprint")
                or snapshot.get("model_binding_ref") != lifecycle.get("model_binding_ref")):
            raise SessionValidationError(f"child session {child_id} 角色或模型来源摘要不匹配")
        round_index = snapshot.get("round_index")
        if isinstance(round_index, bool) or not isinstance(round_index, int) or not 1 <= round_index <= 4:
            raise SessionValidationError(f"child session {child_id} round_index 无效")
        usage = snapshot.get("cumulative_usage")
        usage_fields = {
            "rounds", "llm_calls", "tool_calls", "input_tokens", "output_tokens",
            "token_accounting", "elapsed_ms", "result_bytes",
        }
        if not isinstance(usage, dict) or set(usage) != usage_fields:
            raise SessionValidationError(f"child session {child_id} 累计用量字段无效")
        for key in usage_fields - {"token_accounting"}:
            value = usage.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise SessionValidationError(f"child session {child_id} 累计用量 {key} 无效")
        if usage.get("token_accounting") not in {"provider", "estimated", "mixed"}:
            raise SessionValidationError(f"child session {child_id} token_accounting 无效")
        if usage != lifecycle.get("cumulative_usage"):
            raise SessionValidationError(f"child session {child_id} 累计用量与 State lifecycle 不匹配")
        if (usage["llm_calls"] > 16 or usage["tool_calls"] > 48
                or usage["input_tokens"] + usage["output_tokens"] > 64_000
                or usage["elapsed_ms"] > 240_000):
            raise SessionValidationError(f"child session {child_id} 累计用量超过固定上限")
        if not isinstance(snapshot.get("last_claimed_result_id"), str) or not snapshot["last_claimed_result_id"]:
            raise SessionValidationError(f"child session {child_id} 缺少最近领取的 result_id")
        result_hash = snapshot.get("last_claimed_result_hash")
        if not isinstance(result_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", result_hash):
            raise SessionValidationError(f"child session {child_id} 最近结果 hash 无效")
        try:
            last_result = json.loads(snapshot.get("last_result_json", ""))
            result_json, computed_hash, parsed_result = _canonical_delegation_result(last_result)
        except (TypeError, ValueError, json.JSONDecodeError, SessionError) as error:
            raise SessionValidationError(f"child session {child_id} 最近结果无法校验") from error
        if (result_json != snapshot.get("last_result_json") or computed_hash != result_hash
                or parsed_result.get("result_id") != snapshot.get("last_claimed_result_id")
                or parsed_result.get("subagent_id") != child_id
                or parsed_result.get("outcome") != "completed"
                or parsed_result.get("round_index") != round_index):
            raise SessionValidationError(f"child session {child_id} 最近结果 ID/hash/轮次不匹配")
        model_ref = snapshot.get("model_binding_ref")
        if model_ref is not None and (
                not isinstance(model_ref, dict)
                or set(model_ref) != {"provider", "profile", "protocol", "fingerprint"}
                or any(not isinstance(model_ref.get(key), str)
                       for key in ("provider", "profile", "protocol", "fingerprint"))
                or not re.fullmatch(r"[0-9a-f]{64}", model_ref.get("fingerprint", ""))):
            raise SessionValidationError(f"child session {child_id} model binding 来源摘要无效")
        skills = snapshot.get("skill_identities")
        if not isinstance(skills, list) or len(skills) > 16:
            raise SessionValidationError(f"child session {child_id} Skill 身份列表无效")
        seen_skills: set[str] = set()
        for skill in skills:
            if (not isinstance(skill, dict) or set(skill) != {"skill_id", "source", "fingerprint"}
                    or not isinstance(skill.get("skill_id"), str)
                    or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", skill["skill_id"])
                    or skill["skill_id"] in seen_skills
                    or skill.get("source") not in {"project", "global"}
                    or not isinstance(skill.get("fingerprint"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", skill["fingerprint"])):
                raise SessionValidationError(f"child session {child_id} Skill 身份摘要无效")
            seen_skills.add(skill["skill_id"])
        observations = snapshot.get("observations")
        if not isinstance(observations, list) or len(observations) > 256:
            raise SessionValidationError(f"child session {child_id} 只读观察摘要无效")
        for fact in observations:
            if (not isinstance(fact, dict)
                    or set(fact) - {"kind", "tool", "path", "line", "hash"}
                    or fact.get("kind") not in {"tool_observation", "file_location"}
                    or not isinstance(fact.get("tool"), str)
                    or not isinstance(fact.get("hash"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", fact["hash"])):
                raise SessionValidationError(f"child session {child_id} 观察事实无效")
            if fact.get("path") is not None and not isinstance(fact.get("path"), str):
                raise SessionValidationError(f"child session {child_id} 观察路径无效")
            if fact.get("line") is not None and (
                    isinstance(fact.get("line"), bool) or not isinstance(fact.get("line"), int)
                    or fact["line"] < 1):
                raise SessionValidationError(f"child session {child_id} 观察行号无效")
        try:
            _validate_context_export(snapshot.get("context"))
        except SessionValidationError as error:
            raise SessionValidationError(f"child session {child_id} Context 校验失败: {error}") from error
        encoded_size = len(_canonical_bytes(snapshot))
        if encoded_size > _MAX_CHILD_SESSION_SNAPSHOT_BYTES:
            raise SessionSizeError(f"child session {child_id} 快照超过 {_MAX_CHILD_SESSION_SNAPSHOT_BYTES} bytes")
        total_bytes += encoded_size
        records = delegations_by_child.get(child_id, [])
        if round_index > len(records):
            raise SessionValidationError(f"child session {child_id} 轮次引用缺失父记录")
        round_record = records[round_index - 1]
        if (round_record.get("result_id") != snapshot["last_claimed_result_id"]
                or round_record.get("result_hash") != result_hash
                or round_record.get("delivery_status") != "committed"):
            raise SessionValidationError(f"child session {child_id} 最近结果与父记录不匹配")
        if save_kind == "safe_point":
            if (lifecycle.get("status") != "idle" or lifecycle.get("round_index") != round_index
                    or lifecycle.get("last_claimed_result_id") != snapshot["last_claimed_result_id"]
                    or lifecycle.get("last_claimed_result_hash") != result_hash):
                raise SessionValidationError(f"safe_point child session {child_id} 不是当前 idle 快照")
        elif lifecycle.get("status") in {"starting", "running", "result_ready"}:
            if round_index != lifecycle.get("round_index") - 1:
                raise SessionValidationError(f"活动 child session {child_id} 基础快照轮次不匹配")
    if total_bytes > _MAX_CHILD_SESSION_SNAPSHOTS_BYTES:
        raise SessionSizeError(
            "child_sessions 总大小超过 720 KiB；child_session_id="
            + ",".join(sorted(seen))
        )
    if save_kind == "safe_point":
        for child_id, lifecycle in lifecycle_by_id.items():
            if lifecycle.get("status") == "idle" and child_id not in seen:
                raise SessionValidationError(f"child session {child_id} 缺少安全点快照")


class SessionStore:
    """Store one JSON session per random ID using an exclusive writer lock."""

    schema_version = SCHEMA_VERSION
    max_file_bytes = MAX_SESSION_FILE_BYTES

    def __init__(self, root: str | os.PathLike[str] | None = None,
                 max_file_bytes: int = MAX_SESSION_FILE_BYTES,
                 data_dir: str | os.PathLike[str] | None = None) -> None:
        if root is not None and data_dir is not None:
            raise ValueError("root 与 data_dir 只能指定一个")
        selected = data_dir if data_dir is not None else root
        if selected is None:
            selected = os.path.join(os.path.expanduser("~"), ".mini_agent", "sessions")
        if isinstance(max_file_bytes, bool) or not isinstance(max_file_bytes, int) or max_file_bytes <= 0:
            raise ValueError("max_file_bytes 必须是正整数")
        self.root = Path(os.path.abspath(os.path.expanduser(os.fspath(selected))))
        self.max_file_bytes = max_file_bytes
        self._ensure_private_directory()

    def _ensure_private_directory(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if hasattr(os, "chmod"):
                os.chmod(self.root, stat.S_IRWXU)
        except OSError as error:
            raise SessionError(f"无法创建私有会话目录: {type(error).__name__}") from error
        if not self.root.is_dir():
            raise SessionError("会话存储路径不是目录")
        try:
            directory_stat = self.root.stat()
            mode = stat.S_IMODE(directory_stat.st_mode)
        except OSError as error:
            raise SessionError(f"无法检查会话目录权限: {type(error).__name__}") from error
        if mode & 0o077:
            raise SessionError("会话目录必须只允许当前用户访问")
        if hasattr(os, "getuid") and directory_stat.st_uid != os.getuid():
            raise SessionError("会话目录不属于当前用户")

    @staticmethod
    def _check_id(session_id: str) -> str:
        if not isinstance(session_id, str) or not SESSION_ID_PATTERN.fullmatch(session_id):
            raise SessionValidationError("session_id 格式无效")
        return session_id

    def path_for(self, session_id: str) -> Path:
        return self.root / (self._check_id(session_id) + ".json")

    def session_path(self, session_id: str) -> Path:
        """Compatibility spelling for callers that need a diagnostic path."""
        return self.path_for(session_id)

    def _lock_path(self, session_id: str) -> Path:
        return self.root / (self._check_id(session_id) + ".lock")

    def _acquire_lock(self, session_id: str) -> int:
        try:
            return os.open(
                os.fspath(self._lock_path(session_id)),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError as error:
            raise SessionBusyError("session 已被其他写入者锁定；不会自动抢占遗留锁") from error
        except OSError as error:
            raise SessionError(f"无法创建 session 独占锁: {type(error).__name__}") from error

    def _release_lock(self, session_id: str, fd: int) -> None:
        try:
            os.close(fd)
        finally:
            try:
                self._lock_path(session_id).unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _saved_at() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

    def _make_envelope(self, session_id: str, state: dict[str, Any], context: dict[str, Any],
                       workspace_root: str | os.PathLike[str] | None,
                       handoff_status: str, save_kind: str,
                       *, session_generation: int = 1,
                       workspace_manifest: dict[str, Any] | None = None,
                       tool_boundary: dict[str, Any] | None = None,
                       child_sessions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        self._check_id(session_id)
        if handoff_status not in {"active", "clean"}:
            raise SessionValidationError("handoff_status 必须是 active 或 clean")
        if save_kind not in {"safe_point", "tool_boundary"}:
            raise SessionValidationError("save_kind 无效")
        if (isinstance(session_generation, bool) or not isinstance(session_generation, int)
                or session_generation < 1):
            raise SessionValidationError("session_generation 无效")
        try:
            AgentState.validate_session_export(state, allow_pending=save_kind == "tool_boundary")
        except (SessionExportError, KeyError, TypeError, ValueError) as error:
            raise SessionValidationError(f"State 导出校验失败: {error}") from error
        if save_kind == "safe_point":
            active_delegations = [
                item.get("delegation_id", "?")
                for item in state.get("delegation_records", [])
                if isinstance(item, dict)
                and item.get("delivery_status") not in {"committed", "interrupted", "abandoned"}
            ]
            if active_delegations:
                raise SessionValidationError(
                    "safe_point 不能包含活动或待提交委派: " + ", ".join(active_delegations)
                )
        boundary = deepcopy(tool_boundary) if tool_boundary is not None else _empty_tool_boundary()
        child_snapshots = deepcopy(child_sessions) if child_sessions is not None else []
        _validate_context_export(
            context,
            allow_partial=(save_kind == "tool_boundary" and boundary.get("status") == "pending"),
        )
        _validate_tool_boundary(boundary, state, context)
        _validate_child_session_snapshots(
            child_snapshots, state, _normal_workspace_root(workspace_root), save_kind=save_kind,
        )
        if boundary.get("status") == "pending" and (save_kind != "tool_boundary" or handoff_status != "active"):
            raise SessionValidationError("pending tool_boundary 只能以 active tool_boundary 保存")
        if save_kind == "safe_point" and boundary.get("status") != "committed":
            raise SessionValidationError("safe_point 必须包含 committed tool_boundary")
        manifest = workspace_manifest or build_workspace_manifest(state, workspace_root)
        if not isinstance(manifest, dict):
            raise SessionValidationError("workspace_manifest 无效")
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "session_id": session_id,
            "writer_version": __version__,
            "workspace_root": _normal_workspace_root(workspace_root),
            "saved_at": self._saved_at(),
            "save_kind": save_kind,
            "handoff_status": handoff_status,
            "session_generation": session_generation,
            "workspace_manifest": deepcopy(manifest),
            "state": deepcopy(state),
            "context": deepcopy(context),
            "tool_boundary": boundary,
            "child_sessions": child_snapshots,
        }
        digest = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
        payload["integrity"] = {"algorithm": _INTEGRITY_ALGORITHM, "sha256": digest}
        return payload

    def _write_atomic(self, session_id: str, envelope: dict[str, Any]) -> None:
        encoded = _canonical_bytes(envelope)
        if len(encoded) > self.max_file_bytes:
            raise SessionSizeError(
                f"session 文件超过上限 {self.max_file_bytes} bytes"
            )
        target = self.path_for(session_id)
        temporary: str | None = None
        replaced = False
        try:
            fd, temporary = tempfile.mkstemp(
                prefix=f".{session_id}.", suffix=".tmp", dir=os.fspath(self.root)
            )
            with os.fdopen(fd, "wb") as handle:
                if hasattr(os, "fchmod"):
                    os.fchmod(handle.fileno(), stat.S_IRUSR | stat.S_IWUSR)
                else:
                    os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            replaced = True
            temporary = None
            directory_fd = os.open(os.fspath(self.root), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except SessionError:
            raise
        except OSError as error:
            if replaced:
                raise SessionCommitUncertainError(
                    session_id,
                    f"session 文件已替换，但目录同步未确认: {type(error).__name__}",
                ) from error
            raise SessionError(f"session 原子写入失败: {type(error).__name__}") from error
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def save(self, session_id: str | None, state: Any, context: Any,
             *, workspace_root: str | os.PathLike[str] | None = None,
             handoff_status: str = "active", save_kind: str = "safe_point",
             tool_boundary: dict[str, Any] | None = None,
             child_sessions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Atomically save an exported State/Context and return the envelope."""
        selected_id = self._check_id(session_id) if session_id is not None else _new_session_id()
        if save_kind not in {"safe_point", "tool_boundary"}:
            raise SessionValidationError("save_kind 无效")
        selected_boundary = deepcopy(tool_boundary) if tool_boundary is not None else None
        selected_child_sessions = deepcopy(child_sessions) if child_sessions is not None else None
        try:
            if selected_boundary is None and session_id is not None:
                try:
                    previous_for_boundary = self.load(session_id)
                    if previous_for_boundary.get("schema_version") in {SCHEMA_3_VERSION, SCHEMA_VERSION}:
                        selected_boundary = deepcopy(previous_for_boundary.get("tool_boundary"))
                    if selected_child_sessions is None and save_kind == "tool_boundary":
                        selected_child_sessions = deepcopy(previous_for_boundary.get("child_sessions", []))
                except SessionError:
                    pass
            if selected_boundary is None:
                selected_boundary = _empty_tool_boundary()
            if selected_child_sessions is None:
                selected_child_sessions = []
            partial = save_kind == "tool_boundary" and selected_boundary.get("status") == "pending"
            state_export = (
                state.export_session(allow_pending=(save_kind == "tool_boundary"))
                if hasattr(state, "export_session") else deepcopy(state)
            )
            context_export = (
                context.export_session(allow_partial=partial)
                if hasattr(context, "export_session") else deepcopy(context)
            )
        except (SessionExportError, SessionValidationError, KeyError, TypeError, ValueError) as error:
            raise SessionValidationError(str(error)) from error
        manifest = build_workspace_manifest(state_export, workspace_root)
        lock_fd = self._acquire_lock(selected_id)
        try:
            previous_generation = 0
            target = self.path_for(selected_id)
            if target.exists():
                try:
                    previous = self.load(selected_id)
                    previous_generation = int(previous.get("session_generation", 1))
                except SessionError:
                    # A caller may intentionally replace a legacy diagnostic
                    # file after acquiring its lock.  The new v2 file starts a
                    # fresh local commit sequence.
                    previous_generation = 0
            envelope = self._make_envelope(
                selected_id, state_export, context_export, workspace_root,
                handoff_status, save_kind,
                session_generation=previous_generation + 1,
                workspace_manifest=manifest,
                tool_boundary=selected_boundary,
                child_sessions=selected_child_sessions,
            )
            self._write_atomic(selected_id, envelope)
        except BaseException:
            try:
                self._release_lock(selected_id, lock_fd)
            except OSError:
                # Preserve the original write outcome; the lock remains for
                # manual inspection and will prevent an unsafe follow-up save.
                pass
            raise
        try:
            self._release_lock(selected_id, lock_fd)
        except OSError as error:
            raise SessionCommitUncertainError(
                selected_id,
                f"session 文件已替换，但独占锁清理失败: {type(error).__name__}",
            ) from error
        return deepcopy(envelope)

    def claim_resume(self, session_id: str, expected: dict[str, Any], *,
                     state_export: dict[str, Any] | None = None,
                     context_export: dict[str, Any] | None = None,
                     child_sessions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Atomically claim one validated clean v2/v3 commit for this runtime.

        The expected integrity digest is checked again while the exclusive lock
        is held.  A race or an uncertain commit never returns a runnable
        object to the caller.
        """
        selected_id = self._check_id(session_id)
        if not isinstance(expected, dict) or expected.get("session_id") != selected_id:
            raise SessionValidationError("待恢复 session 提交不匹配")
        if state_export is not None:
            try:
                AgentState.validate_session_export(state_export)
            except (SessionExportError, KeyError, TypeError, ValueError) as error:
                raise SessionValidationError(f"恢复后的 State 导出校验失败: {error}") from error
        if context_export is not None:
            _validate_context_export(context_export)
        lock_fd = self._acquire_lock(selected_id)
        try:
            current = self.load(selected_id)
            if current.get("schema_version") not in {SCHEMA_2_VERSION, SCHEMA_3_VERSION, SCHEMA_VERSION}:
                raise SessionValidationError("待恢复 session 不是 schema 2/3/4")
            if current.get("handoff_status") != "clean" or current.get("save_kind") != "safe_point":
                raise SessionValidationError("session 不是可恢复的 clean safe_point")
            if current.get("integrity") != expected.get("integrity"):
                raise SessionValidationError("session 在恢复提交前发生变化")
            # A candidate may wait for user or filesystem work before claim.
            # The session lock serializes writers, but the workspace can still
            # change independently during that interval.
            from mini_agent.resume import check_workspace_manifest
            workspace_issues = check_workspace_manifest(current, current["workspace_root"])
            if workspace_issues:
                raise SessionValidationError(
                    "恢复占用前工作区检查失败：" + "；".join(workspace_issues[:20])
                )
            active = deepcopy(current)
            if current.get("schema_version") in {SCHEMA_2_VERSION, SCHEMA_3_VERSION}:
                active["schema_version"] = SCHEMA_VERSION
                if current.get("schema_version") == SCHEMA_2_VERSION:
                    active["tool_boundary"] = _empty_tool_boundary()
                active["child_sessions"] = []
            active["handoff_status"] = "active"
            active["session_generation"] = int(current.get("session_generation", 1)) + 1
            active["saved_at"] = self._saved_at()
            if state_export is not None:
                active["state"] = deepcopy(state_export)
            if context_export is not None:
                active["context"] = deepcopy(context_export)
            if child_sessions is not None:
                active["child_sessions"] = deepcopy(child_sessions)
            elif "child_sessions" not in active:
                active["child_sessions"] = []
            without_integrity = {key: value for key, value in active.items() if key != "integrity"}
            active["integrity"] = {
                "algorithm": _INTEGRITY_ALGORITHM,
                "sha256": hashlib.sha256(_canonical_bytes(without_integrity)).hexdigest(),
            }
            self._validate_envelope(active, selected_id)
            self._write_atomic(selected_id, active)
        except BaseException:
            try:
                self._release_lock(selected_id, lock_fd)
            except OSError:
                pass
            raise
        try:
            self._release_lock(selected_id, lock_fd)
        except OSError as error:
            raise SessionCommitUncertainError(
                selected_id,
                f"恢复占用已写入，但独占锁清理失败: {type(error).__name__}",
            ) from error
        return deepcopy(active)

    def _crash_claim_path(self) -> Path:
        return self.root / "crash_recovery_claims.json"

    def _crash_claim_lock_path(self) -> Path:
        return self.root / "crash_recovery_claims.lock"

    def _acquire_crash_claim_lock(self) -> int:
        try:
            return os.open(
                os.fspath(self._crash_claim_lock_path()),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError as error:
            raise SessionBusyError("crash recovery claim 正在由其他恢复者提交；不会自动抢占遗留锁") from error
        except OSError as error:
            raise SessionError(f"无法创建 crash recovery claim 独占锁: {type(error).__name__}") from error

    def _release_crash_claim_lock(self, fd: int) -> None:
        try:
            os.close(fd)
        finally:
            try:
                self._crash_claim_lock_path().unlink()
            except FileNotFoundError:
                pass

    def _read_crash_claims(self) -> list[dict[str, Any]]:
        """Read the private idempotency sidecar without exposing call data."""
        path = self._crash_claim_path()
        try:
            info = path.lstat()
        except FileNotFoundError:
            return []
        except OSError as error:
            raise SessionError(f"无法读取 crash recovery claim: {type(error).__name__}") from error
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise SessionValidationError("crash recovery claim 文件权限或类型无效")
        if info.st_size > _MAX_CRASH_CLAIM_BYTES:
            raise SessionSizeError("crash recovery claim 文件超过大小上限")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SessionValidationError("crash recovery claim 文件损坏") from error
        if not isinstance(payload, dict) or set(payload) != {"format", "format_version", "claims", "integrity"}:
            raise SessionValidationError("crash recovery claim 字段无效")
        if payload.get("format") != "mini_agent.crash_recovery_claims" or payload.get("format_version") != 1:
            raise SessionValidationError("未知 crash recovery claim 版本")
        claims = payload.get("claims")
        if not isinstance(claims, list) or len(claims) > 2048:
            raise SessionValidationError("crash recovery claim 列表无效")
        integrity = payload.get("integrity")
        digest = integrity.get("sha256") if isinstance(integrity, dict) else None
        if not isinstance(integrity, dict) or set(integrity) != {"algorithm", "sha256"} \
                or integrity.get("algorithm") != _INTEGRITY_ALGORITHM:
            raise SessionValidationError("crash recovery claim 完整性字段无效")
        without_integrity = {key: value for key, value in payload.items() if key != "integrity"}
        if not isinstance(digest, str) or not secrets.compare_digest(
                hashlib.sha256(_canonical_bytes(without_integrity)).hexdigest(), digest):
            raise SessionValidationError("crash recovery claim 完整性校验失败")
        seen: set[str] = set()
        for claim in claims:
            if not isinstance(claim, dict):
                raise SessionValidationError("crash recovery claim 记录无效")
            base_fields = {
                "source_session_id", "source_integrity", "derived_session_id", "recovery_id",
            }
            if set(claim) not in (base_fields, base_fields | {"status"}):
                raise SessionValidationError("crash recovery claim 记录字段无效")
            status = claim.get("status", "committed")
            if status not in {"preparing", "committed"}:
                raise SessionValidationError("crash recovery claim 状态无效")
            key = f"{claim.get('source_session_id')}:{claim.get('source_integrity')}"
            if key in seen:
                raise SessionValidationError("crash recovery claim 含重复源提交")
            seen.add(key)
            self._check_id(claim.get("source_session_id"))
            self._check_id(claim.get("derived_session_id"))
            if (not isinstance(claim.get("source_integrity"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", claim["source_integrity"])
                    or not isinstance(claim.get("recovery_id"), str)
                    or not claim.get("recovery_id")):
                raise SessionValidationError("crash recovery claim 引用无效")
        return deepcopy(claims)

    def _write_crash_claims(self, claims: list[dict[str, Any]], source_session_id: str) -> None:
        payload: dict[str, Any] = {
            "format": "mini_agent.crash_recovery_claims",
            "format_version": 1,
            "claims": deepcopy(claims),
        }
        payload["integrity"] = {
            "algorithm": _INTEGRITY_ALGORITHM,
            "sha256": hashlib.sha256(_canonical_bytes(payload)).hexdigest(),
        }
        encoded = _canonical_bytes(payload)
        if len(encoded) > _MAX_CRASH_CLAIM_BYTES:
            raise SessionSizeError("crash recovery claim 文件超过大小上限")
        temporary: str | None = None
        replaced = False
        try:
            fd, temporary = tempfile.mkstemp(
                prefix=".crash_recovery_claims.", suffix=".tmp", dir=os.fspath(self.root)
            )
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), stat.S_IRUSR | stat.S_IWUSR)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._crash_claim_path())
            replaced = True
            temporary = None
            directory_fd = os.open(os.fspath(self.root), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as error:
            if replaced:
                raise SessionCommitUncertainError(
                    source_session_id,
                    f"crash recovery claim 已替换，但耐久性未确认: {type(error).__name__}",
                ) from error
            raise SessionError(f"crash recovery claim 原子写入失败: {type(error).__name__}") from error
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def claim_crash_recovery(
        self, session_id: str, expected: dict[str, Any], *,
        state_export: dict[str, Any], context_export: dict[str, Any],
        tool_boundary: dict[str, Any], workspace_root: str | os.PathLike[str] | None = None,
        workspace_manifest: dict[str, Any] | None = None,
        child_sessions: list[dict[str, Any]] | None = None,
        recovery_id: str = "",
    ) -> dict[str, Any]:
        """Idempotently derive a runnable recovery branch while preserving its source."""
        selected_id = self._check_id(session_id)
        if not isinstance(expected, dict) or expected.get("session_id") != selected_id:
            raise SessionValidationError("待恢复 crash session 提交不匹配")
        try:
            AgentState.validate_session_export(state_export)
        except (SessionExportError, KeyError, TypeError, ValueError) as error:
            raise SessionValidationError(f"崩溃恢复 State 校验失败: {error}") from error
        _validate_context_export(context_export)
        if tool_boundary.get("status") != "committed":
            raise SessionValidationError("崩溃恢复派生 boundary 必须 committed")

        def recovery_source_allowed(envelope: dict[str, Any]) -> bool:
            if (envelope.get("schema_version") not in {SCHEMA_3_VERSION, SCHEMA_VERSION}
                    or envelope.get("handoff_status") != "active"
                    or envelope.get("save_kind") != "tool_boundary"):
                return False
            boundary = envelope.get("tool_boundary", {})
            if boundary.get("status") == "pending":
                return True
            if boundary.get("status") != "committed":
                return False
            state = envelope.get("state", {})
            return any(
                isinstance(item, dict)
                and item.get("mode") == "background"
                and item.get("startup_confirmed") is True
                and item.get("delivery_status") in {"created", "running", "result_ready"}
                for item in state.get("delegation_records", [])
            )

        if not recovery_source_allowed(expected):
            raise SessionValidationError("源 session 不是可恢复的 active tool_boundary")
        lock_fd = self._acquire_lock(selected_id)
        claim_lock_fd: int | None = None
        try:
            claim_lock_fd = self._acquire_crash_claim_lock()
            current = self.load(selected_id)
            if not recovery_source_allowed(current):
                raise SessionValidationError("源 session 已不是可恢复的 active tool_boundary")
            expected_digest = expected.get("integrity", {}).get("sha256")
            current_digest = current.get("integrity", {}).get("sha256")
            if current.get("integrity") != expected.get("integrity"):
                raise SessionValidationError("源 session 在 claim 前发生变化；请重新准备恢复候选")
            claims = self._read_crash_claims()
            existing = next((item for item in claims if item.get("source_session_id") == selected_id
                             and item.get("source_integrity") == expected_digest), None)
            envelope: dict[str, Any] | None = None
            if existing is not None:
                derived_id = existing["derived_session_id"]
                if existing.get("recovery_id") != str(recovery_id):
                    raise SessionValidationError("crash recovery claim 与当前 recovery_id 不一致")
                derived_path = self.path_for(derived_id)
                if existing.get("status", "committed") == "committed":
                    if not derived_path.exists():
                        raise SessionValidationError(
                            "claim 已 committed 但派生 session 不存在；拒绝猜测或重新派生"
                        )
                    # A completed claim is a duplicate hand-off, not a new
                    # runtime admission.  The CLI reports the existing branch
                    # and the caller must use that session explicitly.
                    self.load(derived_id)
                    raise SessionValidationError(
                        "该源提交已经派生 crash recovery session：" + derived_id
                    )
                elif derived_path.exists():
                    # The previous writer may have replaced the derived file
                    # but failed while committing the final sidecar state.
                    # Validate the unpublished intermediate, then rewrite the
                    # same ID from this freshly checked candidate.  Reusing
                    # the old envelope would let workspace drift observed by
                    # this retry exist only in memory while disk kept stale
                    # State and Context.
                    self.load(derived_id)
                else:
                    # A durable preparing record is intentionally resumable.
                    # Continue writing the predetermined derived ID.
                    pass
            else:
                derived_id = _new_session_id()
                claim = {
                    "source_session_id": selected_id,
                    "source_integrity": current_digest,
                    "derived_session_id": derived_id,
                    "recovery_id": str(recovery_id),
                    "status": "preparing",
                }
                # Phase 1: durable intent.  If this write fails, there is no
                # claim and a later prepare may safely retry with a new ID.
                self._write_crash_claims(claims + [claim], selected_id)

            if envelope is None:
                state_export = deepcopy(state_export)
                for record in state_export.get("crash_recoveries", []):
                    if (not recovery_id) or record.get("recovery_id") == recovery_id:
                        record["derived_session_id"] = derived_id
                manifest = workspace_manifest or build_workspace_manifest(
                    state_export, workspace_root or current["workspace_root"],
                )
                envelope = self._make_envelope(
                    derived_id, state_export, context_export,
                    workspace_root or current["workspace_root"], "active", "tool_boundary",
                    session_generation=1, workspace_manifest=manifest,
                    tool_boundary=deepcopy(tool_boundary),
                    child_sessions=deepcopy(child_sessions or []),
                )
                # Phase 2a: durable derived branch.  A failure leaves the
                # preparing record intact so retry can use this same ID.
                self._write_atomic(derived_id, envelope)
                # Phase 2b: publish the completed claim only after the branch
                # has been written and validated by _write_atomic.
                completed_claims = []
                for item in claims if existing is not None else claims + [claim]:
                    if item.get("source_session_id") == selected_id and item.get("source_integrity") == expected_digest:
                        completed_claims.append(dict(item, status="committed"))
                    else:
                        completed_claims.append(item)
                self._write_crash_claims(completed_claims, selected_id)
        except BaseException:
            if claim_lock_fd is not None:
                try:
                    self._release_crash_claim_lock(claim_lock_fd)
                except OSError:
                    pass
            try:
                self._release_lock(selected_id, lock_fd)
            except OSError:
                pass
            raise
        try:
            if claim_lock_fd is not None:
                self._release_crash_claim_lock(claim_lock_fd)
        except OSError as error:
            try:
                self._release_lock(selected_id, lock_fd)
            except OSError:
                pass
            raise SessionCommitUncertainError(
                selected_id,
                f"崩溃恢复 claim 已提交，但 claim 锁清理未确认: {type(error).__name__}",
            ) from error
        try:
            self._release_lock(selected_id, lock_fd)
        except OSError as error:
            raise SessionCommitUncertainError(
                selected_id,
                f"崩溃恢复派生已写入，但源 session 锁清理未确认: {type(error).__name__}",
            ) from error
        return deepcopy(envelope)

    def create(self, state: Any, context: Any, **kwargs: Any) -> dict[str, Any]:
        """Create a new random-ID session; equivalent to ``save(None, ...)``."""
        return self.save(None, state, context, **kwargs)

    def load(self, session_id: str) -> dict[str, Any]:
        """Read and validate a session without constructing runtime objects."""
        path = self.path_for(session_id)
        try:
            path_stat = path.lstat()
        except FileNotFoundError as error:
            raise SessionValidationError("session 文件不存在") from error
        except OSError as error:
            raise SessionError(f"无法读取 session 文件大小: {type(error).__name__}") from error
        if not stat.S_ISREG(path_stat.st_mode):
            raise SessionValidationError("session 路径不是普通文件")
        size = path_stat.st_size
        if size > self.max_file_bytes:
            raise SessionSizeError(f"session 文件超过上限 {self.max_file_bytes} bytes")
        try:
            mode = stat.S_IMODE(path_stat.st_mode)
            if mode & 0o077:
                raise SessionValidationError("session 文件必须只允许当前用户访问")
            if hasattr(os, "getuid") and path_stat.st_uid != os.getuid():
                raise SessionValidationError("session 文件不属于当前用户")
            raw = path.read_bytes()
        except SessionError:
            raise
        except OSError as error:
            raise SessionError(f"无法读取 session 文件: {type(error).__name__}") from error
        if len(raw) > self.max_file_bytes:
            raise SessionSizeError(f"session 文件超过上限 {self.max_file_bytes} bytes")
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SessionValidationError("session 不是合法 UTF-8 JSON") from error
        if not isinstance(envelope, dict):
            raise SessionValidationError("session 顶层必须是 JSON object")
        self._validate_envelope(envelope, session_id)
        return deepcopy(envelope)

    def read(self, session_id: str) -> dict[str, Any]:
        """Compatibility spelling for ``load``."""
        return self.load(session_id)

    def _validate_envelope(self, envelope: dict[str, Any], requested_id: str) -> None:
        schema_version = envelope.get("schema_version")
        if (isinstance(schema_version, bool)
                or not isinstance(schema_version, int)
                or schema_version not in {LEGACY_SCHEMA_VERSION, SCHEMA_2_VERSION, SCHEMA_3_VERSION, SCHEMA_VERSION}):
            raise SessionValidationError("未知或不支持的 session schema_version")
        if schema_version == LEGACY_SCHEMA_VERSION:
            expected_fields = {
                "schema_version", "session_id", "writer_version", "workspace_root",
                "saved_at", "save_kind", "handoff_status", "state", "context", "integrity",
            }
        elif schema_version == SCHEMA_2_VERSION:
            expected_fields = {
                "schema_version", "session_id", "writer_version", "workspace_root",
                "saved_at", "save_kind", "handoff_status", "session_generation",
                "workspace_manifest", "state", "context", "integrity",
            }
        elif schema_version == SCHEMA_3_VERSION:
            expected_fields = {
                "schema_version", "session_id", "writer_version", "workspace_root",
                "saved_at", "save_kind", "handoff_status", "session_generation",
                "workspace_manifest", "state", "context", "tool_boundary", "integrity",
            }
        elif schema_version == SCHEMA_VERSION:
            expected_fields = {
                "schema_version", "session_id", "writer_version", "workspace_root",
                "saved_at", "save_kind", "handoff_status", "session_generation",
                "workspace_manifest", "state", "context", "tool_boundary", "child_sessions",
                "integrity",
            }
        else:
            expected_fields = {"schema_version"}
        missing = sorted(expected_fields - set(envelope))
        unknown = sorted(set(envelope) - expected_fields)
        if missing or unknown:
            if "integrity" in missing:
                raise SessionValidationError("缺少完整性字段 integrity")
            detail = []
            if missing:
                detail.append("缺少 " + ", ".join(missing))
            if unknown:
                detail.append("未知 " + ", ".join(unknown))
            raise SessionValidationError("session 字段无效: " + "; ".join(detail))
        if envelope.get("session_id") != requested_id:
            raise SessionValidationError("session_id 与文件名不一致")
        if not isinstance(envelope.get("writer_version"), str) or not envelope["writer_version"]:
            raise SessionValidationError("writer_version 无效")
        if envelope.get("handoff_status") not in {"active", "clean"}:
            raise SessionValidationError("handoff_status 无效")
        if envelope.get("save_kind") not in {"safe_point", "tool_boundary"}:
            raise SessionValidationError("save_kind 无效")
        if schema_version in {SCHEMA_2_VERSION, SCHEMA_3_VERSION, SCHEMA_VERSION}:
            generation = envelope.get("session_generation")
            if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
                raise SessionValidationError("session_generation 无效")
            self._validate_workspace_manifest(envelope.get("workspace_manifest"), envelope["workspace_root"])
        if not isinstance(envelope.get("saved_at"), str) or not envelope["saved_at"].endswith("Z"):
            raise SessionValidationError("saved_at 无效")
        if envelope.get("workspace_root") != _normal_workspace_root(envelope.get("workspace_root")):
            raise SessionValidationError("workspace_root 未规范化")
        integrity = envelope.get("integrity")
        if not isinstance(integrity, dict) or integrity.get("algorithm") != _INTEGRITY_ALGORITHM:
            raise SessionValidationError("缺少或不支持的完整性字段")
        digest = integrity.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SessionValidationError("SHA-256 完整性字段格式无效")
        without_integrity = {key: value for key, value in envelope.items() if key != "integrity"}
        actual = hashlib.sha256(_canonical_bytes(without_integrity)).hexdigest()
        if not secrets.compare_digest(actual, digest):
            raise SessionValidationError("session SHA-256 校验失败")
        try:
            AgentState.validate_session_export(
                envelope.get("state"),
                allow_pending=(schema_version in {SCHEMA_3_VERSION, SCHEMA_VERSION}
                                and envelope.get("save_kind") == "tool_boundary"),
            )
        except (SessionExportError, KeyError, TypeError, ValueError) as error:
            raise SessionValidationError(f"State 引用校验失败: {error}") from error
        try:
            allow_partial = (
                schema_version in {SCHEMA_3_VERSION, SCHEMA_VERSION}
                and isinstance(envelope.get("tool_boundary"), dict)
                and envelope["tool_boundary"].get("status") == "pending"
            )
            _validate_context_export(envelope.get("context"), allow_partial=allow_partial)
        except SessionValidationError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise SessionValidationError(f"Context 引用校验失败: {error}") from error
        if schema_version in {SCHEMA_3_VERSION, SCHEMA_VERSION}:
            _validate_tool_boundary(
                envelope.get("tool_boundary"), envelope["state"], envelope["context"],
            )
            boundary_status = envelope["tool_boundary"].get("status")
            if boundary_status == "pending":
                if envelope.get("save_kind") != "tool_boundary" or envelope.get("handoff_status") != "active":
                    raise SessionValidationError("pending tool_boundary 只能存在于 active tool_boundary session")
            elif envelope.get("save_kind") == "safe_point" and boundary_status != "committed":
                raise SessionValidationError("safe_point 的 tool_boundary 必须 committed")
        if schema_version == SCHEMA_VERSION:
            _validate_child_session_snapshots(
                envelope.get("child_sessions"), envelope["state"], envelope["workspace_root"],
                save_kind=envelope.get("save_kind"),
            )

    @staticmethod
    def _validate_workspace_manifest(manifest: Any, workspace_root: str) -> None:
        if not isinstance(manifest, dict):
            raise SessionValidationError("workspace_manifest 必须是 object")
        expected = {"format", "format_version", "root", "complete", "recoverable", "issues", "entries"}
        if set(manifest) != expected:
            raise SessionValidationError("workspace_manifest 字段无效")
        if (manifest.get("format") != _MANIFEST_FORMAT
                or isinstance(manifest.get("format_version"), bool)
                or manifest.get("format_version") != _MANIFEST_FORMAT_VERSION):
            raise SessionValidationError("未知或不支持的 workspace_manifest 版本")
        if manifest.get("root") != _normal_workspace_root(workspace_root):
            raise SessionValidationError("workspace_manifest 根路径不一致")
        if not isinstance(manifest.get("complete"), bool) or not isinstance(manifest.get("recoverable"), bool):
            raise SessionValidationError("workspace_manifest 可用性字段无效")
        if not isinstance(manifest.get("issues"), list) or any(not isinstance(item, str) for item in manifest["issues"]):
            raise SessionValidationError("workspace_manifest issues 无效")
        entries = manifest.get("entries")
        if not isinstance(entries, list) or len(entries) > _MAX_MANIFEST_ENTRIES:
            raise SessionValidationError("workspace_manifest entries 无效")
        paths: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise SessionValidationError("workspace_manifest entry 无效")
            path = entry["path"]
            if (not path or "\x00" in path or os.path.isabs(path)
                    or (path != "." and os.path.normpath(path) in {os.curdir, os.pardir})
                    or os.path.normpath(path).startswith(os.pardir + os.sep)):
                raise SessionValidationError("workspace_manifest 路径必须位于工作区内")
            if path in paths:
                raise SessionValidationError("workspace_manifest 路径重复")
            paths.add(path)
            if entry.get("kind") not in {"file", "directory", "absent", "symlink", "special", "unavailable"}:
                raise SessionValidationError("workspace_manifest 路径类型无效")
            if not isinstance(entry.get("available"), bool):
                raise SessionValidationError("workspace_manifest entry 可用性无效")
            if entry.get("kind") == "file" and entry.get("available") and not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", ""))):
                raise SessionValidationError("workspace_manifest 文件哈希无效")
            if entry.get("kind") != "directory" and "entries" in entry:
                raise SessionValidationError("workspace_manifest 非目录不能包含 entries")
            child_entries = entry.get("entries")
            if child_entries is not None:
                if (entry.get("kind") != "directory" or not isinstance(child_entries, list)
                        or len(child_entries) > _MAX_MANIFEST_DIRECTORY_ENTRIES):
                    raise SessionValidationError("workspace_manifest 目录 entries 无效")
                child_names: set[str] = set()
                for child in child_entries:
                    if not isinstance(child, dict) or not isinstance(child.get("name"), str):
                        raise SessionValidationError("workspace_manifest 目录条目无效")
                    name = child["name"]
                    if (not name or name in child_names or name in {os.curdir, os.pardir}
                            or os.path.basename(name) != name or "\x00" in name):
                        raise SessionValidationError("workspace_manifest 目录条目名称无效")
                    child_names.add(name)
                    if child.get("kind") not in {"file", "directory", "symlink", "special", "unavailable"}:
                        raise SessionValidationError("workspace_manifest 目录条目类型无效")
                    if not isinstance(child.get("available"), bool):
                        raise SessionValidationError("workspace_manifest 目录条目可用性无效")
                    if (child.get("kind") == "file" and child.get("available")
                            and not re.fullmatch(r"[0-9a-f]{64}", str(child.get("sha256", "")))):
                        raise SessionValidationError("workspace_manifest 目录文件哈希无效")


class DurableToolBoundary:
    """Coordinate atomic schema 4 commits for one live tool round."""

    def __init__(self, store: SessionStore, session_id: str,
                 workspace_root: str | os.PathLike[str]) -> None:
        self.store = store
        self.session_id = SessionStore._check_id(session_id)
        self.workspace_root = os.fspath(workspace_root)
        self.boundary = _empty_tool_boundary()

    def _context_export(self, context: Any) -> dict[str, Any]:
        partial = self.boundary.get("status") == "pending"
        if hasattr(context, "export_session"):
            return context.export_session(allow_partial=partial)
        return deepcopy(context)

    def _save(self, state: Any, context: Any) -> dict[str, Any]:
        envelope = self.store.save(
            self.session_id, state, context,
            workspace_root=self.workspace_root,
            handoff_status="active", save_kind="tool_boundary",
            tool_boundary=deepcopy(self.boundary),
        )
        self.session_id = envelope["session_id"]
        return envelope

    @staticmethod
    def _result_raw(result: Any) -> tuple[str, str, dict[str, Any]]:
        raw = result.to_dict() if hasattr(result, "to_dict") else result
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, json.JSONDecodeError) as error:
                raise SessionValidationError("委派结果原文不是合法 JSON") from error
        return _canonical_delegation_result(raw)

    def start_round(self, round_id: int, assistant_message: dict[str, Any],
                    calls: list[dict[str, Any]], state: Any, context: Any) -> dict[str, Any]:
        """Persist an assistant message before any handler can run."""
        context_export = (
            context.export_session(allow_partial=True)
            if hasattr(context, "export_session") else deepcopy(context)
        )
        history = context_export.get("history", [])
        persisted_assistant = next(
            (message for message in reversed(history)
             if isinstance(message, dict)
             and message.get("role") == "assistant"
             and message.get("tool_calls")),
            None,
        )
        if persisted_assistant is None:
            raise SessionValidationError("工具回合开始时 Context 缺少 assistant 消息")
        records: list[dict[str, Any]] = []
        for index, call in enumerate(calls):
            args = call.get("arguments", {})
            records.append({
                "sequence": index,
                "invocation_id": call["invocation_id"],
                "tool_call_id": call["tool_call_id"],
                "tool": call["tool"],
                "arguments_summary": redacted_arguments(args if isinstance(args, dict) else {}),
                # This is the reservation/fingerprint identity, not a
                # replayable argument payload.  It is required to reconnect a
                # pending admitted call to its original budget reservation.
                "arguments_hash": canonical_arguments_hash(args if isinstance(args, dict) else {}),
                "effect_class": call.get("effect_class", "none"),
                "permission": "not_checked",
                "handler_admitted": False,
                "attempt_id": None,
                "pre_generation_id": None,
                "generation_id": None,
                "status": "pending",
                "result": None,
            })
        self.boundary = {
            "round_id": round_id,
            "assistant_message": deepcopy(persisted_assistant),
            "calls": records,
            "status": "pending",
        }
        return self._save(state, context)

    def persist_delegation_batch(
        self, tasks: dict[int, Any] | list[tuple[int, Any]], state: Any, context: Any,
    ) -> dict[str, Any]:
        """Persist frozen child contracts after reservation and before workers start."""
        entries = tasks.items() if isinstance(tasks, dict) else tasks
        calls = self.boundary.get("calls", [])
        for index, task in sorted(entries, key=lambda item: item[0]):
            if not isinstance(index, int) or index < 0 or index >= len(calls):
                raise SessionValidationError("委派任务索引与 tool_boundary 不一致")
            call = calls[index]
            if call.get("tool") != "delegate_task":
                raise SessionValidationError("委派合同只能绑定 delegate_task")
            if (hasattr(state, "bind_delegation_parent_attempt")
                    and call.get("attempt_id") is not None):
                state.bind_delegation_parent_attempt(task.delegation_id, call.get("attempt_id"))
            call.update({
                "delegation_id": str(getattr(task, "delegation_id", "")),
                "subagent_id": str(getattr(task, "subagent_id", "")),
            })
            role_id = getattr(task, "agent_profile", None)
            if role_id is not None:
                call["agent_profile"] = role_id
                call["agent_profile_fingerprint"] = getattr(
                    task, "agent_profile_fingerprint", None,
                )
        return self._save(state, context)

    # Explicit spelling used by integrations that treat reservation/start as
    # one durable lifecycle point.
    persist_delegation_start = persist_delegation_batch

    def _call(self, invocation_id: str) -> dict[str, Any]:
        for call in self.boundary.get("calls", []):
            if call.get("invocation_id") == invocation_id:
                return call
        raise SessionValidationError("未知 durable invocation_id")

    @staticmethod
    def _reservation_fields(admission: Any) -> dict[str, Any]:
        reservation = getattr(admission, "reservation", None)
        if reservation is None:
            return {"attempt_id": None, "pre_generation_id": None, "generation_id": None}
        return {
            "attempt_id": reservation.attempt_id,
            "pre_generation_id": reservation.pre_generation_id,
            "generation_id": reservation.generation_id,
        }

    def record_admission(self, invocation_id: str, admission: Any,
                         state: Any, context: Any) -> dict[str, Any]:
        """Persist handler admission before entering the handler."""
        call = self._call(invocation_id)
        admitted_arguments = getattr(admission, "arguments", None)
        call.update({
            "effect_class": getattr(admission, "effect_class", call["effect_class"]),
            "permission": "allowed",
            "handler_admitted": True,
            **self._reservation_fields(admission),
        })
        if isinstance(admitted_arguments, dict):
            # Tool validation may add normalized defaults after start_round.
            # The durable identity must match the exact arguments used for
            # the reservation that recovery will later settle.
            call["arguments_summary"] = redacted_arguments(admitted_arguments)
            call["arguments_hash"] = canonical_arguments_hash(admitted_arguments)
        return self._save(state, context)

    def record_execution_result(self, invocation_id: str, execution: Any,
                                content: str, state: Any, context: Any,
                                attempt: Any = None) -> dict[str, Any]:
        """Persist one State fact and its matching tool result."""
        if getattr(execution, "outcome", None) == "uncertain":
            raise SessionValidationError(
                "uncertain outcome 只能由 crash recovery 合成，不能来自普通 handler"
            )
        call = self._call(invocation_id)
        reservation = getattr(execution, "reservation", None)
        attempt_id = getattr(reservation, "attempt_id", None)
        pre_generation_id = getattr(reservation, "pre_generation_id", None)
        generation_id = getattr(reservation, "generation_id", None)
        if attempt is not None:
            attempt_id = getattr(attempt, "attempt_id", attempt_id)
            pre_generation_id = getattr(attempt, "pre_generation_id", pre_generation_id)
            generation_id = getattr(attempt, "generation_id", generation_id)
        call.update({
            "effect_class": getattr(execution, "effect_class", call["effect_class"]),
            "permission": getattr(execution, "permission", call["permission"]),
            "handler_admitted": bool(getattr(execution, "handler_admitted", False)),
            "attempt_id": attempt_id,
            "pre_generation_id": pre_generation_id,
            "generation_id": generation_id,
            "status": "committed",
            "result": {
                "outcome": getattr(execution, "outcome", "invalid"),
                "content": str(content),
                "output_excerpt": str(getattr(execution, "output_excerpt", "")),
                "error_kind": getattr(execution, "error_kind", None),
                "exit_code": getattr(execution, "exit_code", None),
            },
        })
        if call.get("tool") in {"spawn_subagent", "followup_subagent"} and getattr(execution, "outcome", None) == "succeeded":
            try:
                confirmation = json.loads(str(content))
            except (TypeError, ValueError, json.JSONDecodeError):
                confirmation = None
            if isinstance(confirmation, dict) and confirmation.get("accepted") is True:
                child_session_id = confirmation.get("child_session_id")
                delegation_id = confirmation.get("delegation_id")
                record = next((item for item in getattr(state, "delegation_records", [])
                               if item.delegation_id == delegation_id), None)
                if (not isinstance(child_session_id, str) or record is None
                        or record.mode != "background"
                        or record.subagent_id != child_session_id):
                    raise SessionValidationError("后台启动确认缺少匹配的 State 委派合同")
                call.update({
                    "child_session_id": child_session_id,
                    "delegation_id": delegation_id,
                    "subagent_id": child_session_id,
                    "agent_profile": record.agent_profile,
                    "agent_profile_fingerprint": record.agent_profile_fingerprint,
                    "round_index": confirmation.get("round_index", 1),
                })
                if hasattr(state, "bind_delegation_parent_attempt") and call.get("attempt_id"):
                    state.bind_delegation_parent_attempt(delegation_id, call["attempt_id"])
        elif call.get("tool") == "get_subagent_result" and getattr(execution, "outcome", None) == "succeeded":
            try:
                raw_result = json.loads(str(content))
                result_json, result_hash, parsed_result = _canonical_delegation_result(raw_result)
            except (TypeError, ValueError, json.JSONDecodeError, SessionError):
                raw_result = None
            if isinstance(raw_result, dict) and isinstance(parsed_result, dict):
                delegation_id = parsed_result.get("delegation_id")
                record = next((item for item in getattr(state, "delegation_records", [])
                               if item.delegation_id == delegation_id), None)
                if (record is None or record.mode != "background"
                        or record.delivery_status != "committed"
                        or record.result_id != parsed_result.get("result_id")
                        or record.result_hash != result_hash
                        or result_json != str(content)):
                    raise SessionValidationError("领取的后台结果与 State ID/hash 不一致")
                lifecycle = next((item for item in getattr(state, "child_session_records", [])
                                  if item.child_session_id == record.subagent_id), None)
                if (parsed_result.get("round_index") is not None and lifecycle is not None
                        and parsed_result.get("round_index") != lifecycle.round_index):
                    raise SessionValidationError("领取结果 round_index 与 child session 不一致")
                call.update({
                    "child_session_id": record.subagent_id,
                    "delegation_id": record.delegation_id,
                    "subagent_id": record.subagent_id,
                    "delegation_result_hash": result_hash,
                    "agent_profile": record.agent_profile,
                    "agent_profile_fingerprint": record.agent_profile_fingerprint,
                    **({"round_index": parsed_result["round_index"]}
                       if parsed_result.get("round_index") is not None else {}),
                })
        return self._save(state, context)

    def record_delegation_result_ready(
        self, invocation_id: str, result: Any, state: Any, context: Any,
    ) -> dict[str, Any]:
        """Persist one validated child result while keeping its parent call pending."""
        call = self._call(invocation_id)
        if call.get("tool") != "delegate_task" or call.get("status") != "pending":
            raise SessionValidationError("只有 pending delegate_task 可以进入 result_ready")
        result_json, result_hash, raw = self._result_raw(result)
        delegation_id = call.get("delegation_id") or raw.get("delegation_id")
        if not isinstance(delegation_id, str) or not delegation_id:
            raise SessionValidationError("result_ready 委派缺少 delegation_id")
        if call.get("delegation_id") not in (None, delegation_id):
            raise SessionValidationError("result_ready delegation_id 不一致")
        if call.get("subagent_id") not in (None, raw.get("subagent_id")):
            raise SessionValidationError("result_ready subagent_id 不一致")
        state_record = next(
            (item for item in getattr(state, "delegation_records", [])
             if item.delegation_id == delegation_id), None,
        )
        if state_record is None or state_record.delivery_status != "result_ready":
            raise SessionValidationError("State 必须先保存 result_ready 生命周期")
        if state_record.result_id != raw.get("result_id") or state_record.result_hash != result_hash:
            raise SessionValidationError("result_ready 与 State 摘要不一致")
        if (state_record.agent_profile != raw.get("agent_profile")
                or state_record.agent_profile_fingerprint
                != raw.get("agent_profile_fingerprint")):
            raise SessionValidationError("result_ready agent_profile 与 State 不一致")
        # A budget rejection can be materialized after batch preparation, so
        # its boundary call did not yet receive the frozen task identity.
        # Attach the identity before saving the result-ready boundary.
        call["delegation_id"] = delegation_id
        call.setdefault("subagent_id", raw.get("subagent_id"))
        if raw.get("agent_profile") is not None:
            call["agent_profile"] = raw["agent_profile"]
            call["agent_profile_fingerprint"] = raw["agent_profile_fingerprint"]
        if hasattr(state, "bind_delegation_parent_attempt") and call.get("attempt_id") is not None:
            state.bind_delegation_parent_attempt(delegation_id, call["attempt_id"])
        pending = self.boundary.setdefault("pending_delegation_results", [])
        existing = next((item for item in pending if item.get("invocation_id") == invocation_id), None)
        entry = {
            "invocation_id": invocation_id,
            "delegation_id": delegation_id,
            "result_id": raw["result_id"],
            "result_hash": result_hash,
            "result_json": result_json,
            "result_summary": str(raw.get("summary") or "")[:1200],
        }
        if existing is not None:
            if existing != entry:
                raise SessionValidationError("重复 result_ready 与既有结果冲突")
            return self._save(state, context)
        pending.append(entry)
        return self._save(state, context)

    # Compatibility aliases make the lifecycle boundary discoverable without
    # exposing a new public tool argument.
    persist_delegation_result_ready = record_delegation_result_ready
    save_delegation_result_ready = record_delegation_result_ready

    def commit_delegation_result(
        self, invocation_id: str, execution: Any, content: str,
        state: Any, context: Any, attempt: Any = None,
    ) -> dict[str, Any]:
        """Atomically remove a pending raw result and commit parent delivery."""
        call = self._call(invocation_id)
        if call.get("tool") != "delegate_task" or call.get("status") != "pending":
            raise SessionValidationError("只有 pending delegate_task 可以 committed")
        pending = self.boundary.get("pending_delegation_results", [])
        entry = next((item for item in pending if item.get("invocation_id") == invocation_id), None)
        if entry is None:
            raise SessionValidationError("committed 委派缺少已持久化的 result_ready 原文")
        raw_json, result_hash, raw = self._result_raw(execution.output if hasattr(execution, "output") else execution)
        if raw_json != entry.get("result_json") or result_hash != entry.get("result_hash"):
            raise SessionValidationError("committed 委派结果与 result_ready 原文不一致")
        if str(content) != raw_json:
            raise SessionValidationError("父 tool 结果不是可恢复的委派原文")
        delegation_id = call.get("delegation_id")
        if delegation_id != entry.get("delegation_id"):
            raise SessionValidationError("committed 委派身份不一致")
        state_record = next(
            (item for item in getattr(state, "delegation_records", [])
             if item.delegation_id == delegation_id), None,
        )
        if state_record is None or state_record.delivery_status != "committed":
            raise SessionValidationError("State 必须先 committed 委派结果")
        reservation = getattr(execution, "reservation", None)
        call.update({
            "status": "committed",
            "attempt_id": getattr(attempt, "attempt_id", getattr(reservation, "attempt_id", call.get("attempt_id"))),
            "pre_generation_id": getattr(attempt, "pre_generation_id", getattr(reservation, "pre_generation_id", call.get("pre_generation_id"))),
            "generation_id": getattr(attempt, "generation_id", getattr(reservation, "generation_id", call.get("generation_id"))),
            "delegation_result_hash": result_hash,
            "result": {
                "outcome": getattr(execution, "outcome", "succeeded"),
                "content": str(content),
                "output_excerpt": str(getattr(execution, "output_excerpt", "")),
                "error_kind": getattr(execution, "error_kind", None),
                "exit_code": getattr(execution, "exit_code", None),
            },
        })
        self.boundary["pending_delegation_results"] = [
            item for item in pending if item.get("invocation_id") != invocation_id
        ]
        if not self.boundary["pending_delegation_results"]:
            self.boundary.pop("pending_delegation_results", None)
        return self._save(state, context)

    def complete_round(self, state: Any, context: Any) -> dict[str, Any]:
        """Commit the complete ordered round before another LLM request."""
        if any(call.get("status") != "committed" for call in self.boundary.get("calls", [])):
            raise SessionValidationError("工具回合尚未完成，不能提交 complete")
        self.boundary["status"] = "committed"
        return self._save(state, context)

    def persist_process_sync(self, state: Any, context: Any) -> dict[str, Any]:
        """Persist natural process-exit facts without adding a tool message."""
        return self._save(state, context)
