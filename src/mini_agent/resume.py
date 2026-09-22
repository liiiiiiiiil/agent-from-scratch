"""Safe session admission and fresh-runtime assembly for schema 2/3."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import json
import os
import stat
from typing import Any

from mini_agent.context import ContextBudget, ContextManager
from mini_agent.instructions import InstructionLoader
from mini_agent.permission import PermissionGate
from mini_agent.processes import ProcessManager
from mini_agent.prompt import build_system_prompt
from mini_agent.session import (
    SCHEMA_2_VERSION,
    SCHEMA_VERSION,
    SessionError,
    SessionStore,
    SessionValidationError,
    _normal_workspace_root,
)
from mini_agent.state import AgentState
from mini_agent.tools import create_registry
from mini_agent.tools.base import ToolExecutor
from mini_agent.providers.catalog import ProviderCatalog, load_provider_catalog
from mini_agent.config import MEMORY_RETRIEVAL_ENABLED
from mini_agent.retrieval import MemoryRetriever


class ResumeError(SessionError):
    """The session is readable but cannot safely become a live runtime."""


def _hash_file(path: str) -> tuple[str | None, str | None]:
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest(), None
    except (OSError, ValueError) as error:
        return None, f"无法读取文件: {type(error).__name__}"


def _kind(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "special"


def _manifest_path(root: str, relative: str) -> tuple[str | None, str | None]:
    if not isinstance(relative, str) or not relative or os.path.isabs(relative):
        return None, "清单路径不是工作区相对路径"
    candidate = root if relative == "." else os.path.abspath(os.path.join(root, relative))
    try:
        if os.path.commonpath((root, os.path.realpath(candidate))) != root:
            return None, "清单路径位于工作区之外"
        # Do not permit a newly introduced symlink in any path component.
        current = os.path.abspath(os.sep)
        for part in [item for item in candidate.split(os.sep) if item]:
            current = os.path.join(current, part)
            info = os.lstat(current)
            if stat.S_ISLNK(info.st_mode):
                return None, "路径组件是符号链接"
        return candidate, None
    except FileNotFoundError:
        return candidate, None
    except (OSError, ValueError) as error:
        return None, f"路径无法安全解析: {type(error).__name__}"


def _compare_directory(path: str, expected: list[dict[str, Any]], issues: list[str], label: str) -> None:
    try:
        with os.scandir(path) as iterator:
            actual = sorted(iterator, key=lambda item: item.name)
    except OSError as error:
        issues.append(f"{label}: 目录条目无法检查 ({type(error).__name__})")
        return
    actual_names = [item.name for item in actual]
    expected_names = [item.get("name") for item in expected]
    if actual_names != expected_names:
        missing = sorted(set(expected_names) - set(actual_names))
        added = sorted(set(actual_names) - set(expected_names))
        detail = []
        if missing:
            detail.append("缺少 " + ", ".join(missing[:20]))
        if added:
            detail.append("新增 " + ", ".join(added[:20]))
        issues.append(f"{label}: 目录条目变化 ({'; '.join(detail)})")
        return
    by_name = {item.name: item for item in actual}
    for expected_item in expected:
        name = expected_item.get("name")
        current = by_name.get(name)
        if current is None:
            continue
        try:
            info = current.stat(follow_symlinks=False)
            current_kind = _kind(info.st_mode)
        except OSError as error:
            issues.append(f"{label}/{name}: 条目无法检查 ({type(error).__name__})")
            continue
        if current_kind != expected_item.get("kind"):
            issues.append(f"{label}/{name}: 类型从 {expected_item.get('kind')} 变为 {current_kind}")
            continue
        if current_kind == "file" and expected_item.get("available"):
            digest, error = _hash_file(current.path)
            if error:
                issues.append(f"{label}/{name}: {error}")
            elif digest != expected_item.get("sha256"):
                issues.append(f"{label}/{name}: 文件内容已变化")


def _observation_record(entry: dict[str, Any]) -> dict[str, Any]:
    """Keep only stable, non-content workspace facts for comparison."""
    result: dict[str, Any] = {
        "path": entry.get("path"),
        "kind": entry.get("kind"),
        "available": entry.get("available"),
    }
    if entry.get("kind") == "file" and entry.get("available"):
        result["sha256"] = entry.get("sha256")
    children = entry.get("entries")
    if entry.get("kind") == "directory" and isinstance(children, list):
        result["entries"] = [
            _observation_record({
                "path": child.get("name"),
                "kind": child.get("kind"),
                "available": child.get("available"),
                "sha256": child.get("sha256"),
            }) for child in children if isinstance(child, dict)
        ]
    return result


def _observe_path(root: str, relative: str) -> dict[str, Any]:
    """Sample one manifest path without following symlinks."""
    absolute, error = _manifest_path(root, relative)
    if error or absolute is None:
        return {"path": relative, "kind": "unavailable", "available": False}
    try:
        info = os.lstat(absolute)
    except FileNotFoundError:
        return {"path": relative, "kind": "absent", "available": True}
    except OSError:
        return {"path": relative, "kind": "unavailable", "available": False}
    kind = _kind(info.st_mode)
    result: dict[str, Any] = {"path": relative, "kind": kind, "available": True}
    if kind == "file":
        digest, read_error = _hash_file(absolute)
        if read_error:
            result["available"] = False
        else:
            result["sha256"] = digest
    elif kind == "directory":
        children: list[dict[str, Any]] = []
        try:
            with os.scandir(absolute) as iterator:
                actual = sorted(iterator, key=lambda item: item.name)
            for child in actual[:2048]:
                try:
                    child_info = child.stat(follow_symlinks=False)
                    child_kind = _kind(child_info.st_mode)
                    child_record: dict[str, Any] = {
                        "path": child.name, "kind": child_kind, "available": True,
                    }
                    if child_kind == "file":
                        digest, read_error = _hash_file(child.path)
                        if read_error:
                            child_record["available"] = False
                        else:
                            child_record["sha256"] = digest
                    elif child_kind == "symlink":
                        child_record["available"] = False
                    children.append(child_record)
                except OSError:
                    children.append({
                        "path": child.name, "kind": "unavailable", "available": False,
                    })
        except OSError:
            result["available"] = False
        result["entries"] = children
    elif kind == "symlink":
        result["available"] = False
    return result


def observe_workspace_manifest(envelope: dict[str, Any], workspace_root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Capture a bounded structured workspace observation for crash hand-off.

    Diagnostic strings are presentation only.  Claim compares the normalized
    path/type/availability/hash snapshot and therefore detects a second edit
    even when its human-readable wording is unchanged.
    """
    stored_root = envelope.get("workspace_root")
    try:
        normalized_current = _normal_workspace_root(workspace_root)
    except SessionValidationError as error:
        return {"issues": [str(error)], "expected_digest": "", "observed_digest": "", "changed": True}
    if stored_root != normalized_current:
        return {
            "issues": [f"工作区根路径不一致：session={stored_root} 当前={normalized_current}"],
            "expected_digest": "", "observed_digest": "", "changed": True,
        }
    if not os.path.isdir(normalized_current):
        return {
            "issues": [f"当前工作区根路径不可用：{normalized_current}"],
            "expected_digest": "", "observed_digest": "", "changed": True,
        }
    manifest = envelope.get("workspace_manifest")
    if not isinstance(manifest, dict):
        return {"issues": ["session 缺少工作区清单，不能恢复"],
                "expected_digest": "", "observed_digest": "", "changed": True}
    issues: list[str] = []
    if manifest.get("root") != normalized_current:
        issues.append("工作区清单根路径不一致")
    if manifest.get("recoverable") is not True or manifest.get("complete") is not True:
        issues.extend(str(item) for item in manifest.get("issues", []) if isinstance(item, str))
        if not issues:
            issues.append("工作区清单不完整，不能恢复")
    expected_entries = [
        _observation_record(entry) for entry in manifest.get("entries", [])
        if isinstance(entry, dict)
    ]
    observed_entries = [_observe_path(normalized_current, entry["path"])
                        for entry in expected_entries if isinstance(entry.get("path"), str)]
    expected_digest = hashlib.sha256(json.dumps(
        {"root": normalized_current, "entries": expected_entries},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    observed_digest = hashlib.sha256(json.dumps(
        {"root": normalized_current, "entries": observed_entries},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    expected_by_path = {item.get("path"): item for item in expected_entries}
    for observed in observed_entries:
        path = observed.get("path")
        expected = expected_by_path.get(path, {})
        if expected.get("kind") != observed.get("kind"):
            issues.append(f"{path}: 类型从 {expected.get('kind')} 变为 {observed.get('kind')}")
        elif expected.get("available") != observed.get("available"):
            issues.append(f"{path}: 可用性发生变化")
        elif (expected.get("kind") == "file" and expected.get("available")
              and expected.get("sha256") != observed.get("sha256")):
            issues.append(f"{path}: 文件内容已变化")
        elif expected.get("kind") == "directory":
            expected_children = expected.get("entries", [])
            observed_children = observed.get("entries", [])
            if expected_children != observed_children:
                expected_names = [item.get("path") for item in expected_children]
                observed_names = [item.get("path") for item in observed_children]
                if expected_names != observed_names:
                    issues.append(f"{path}: 目录条目变化")
                else:
                    issues.append(f"{path}: 目录成员类型、可用性或内容已变化")
        if expected.get("available") is not True:
            issues.append(f"{path}: 该路径在保存时就无法完整检查")
    issues = sorted(set(issues))
    return {
        "issues": issues,
        "expected_entries": expected_entries,
        "observed_entries": observed_entries,
        "expected_digest": expected_digest,
        "observed_digest": observed_digest,
        "changed": bool(issues) or expected_digest != observed_digest,
    }


def check_workspace_manifest(envelope: dict[str, Any], workspace_root: str | os.PathLike[str] | None = None) -> list[str]:
    """Return explicit workspace changes or uncheckable paths."""
    return list(observe_workspace_manifest(envelope, workspace_root).get("issues", []))


@dataclass
class ResumeRuntime:
    session_id: str
    envelope: dict[str, Any]
    state: AgentState
    context: ContextManager
    registry: Any
    process_manager: ProcessManager
    permission_gate: PermissionGate
    tool_executor: ToolExecutor
    protected_messages: list[dict[str, object]]
    recovery_mode: str = "safe_point"
    source_session_id: str | None = None
    workspace_report: list[str] | None = None
    workspace_observation: dict[str, Any] | None = None


@dataclass
class ResumeCandidate:
    store: SessionStore
    expected_envelope: dict[str, Any]
    _runtime: ResumeRuntime | None
    recovery_mode: str = "safe_point"

    def close(self) -> dict[str, Any]:
        """Release a prepared Runtime when its session will not be claimed."""
        runtime = self._runtime
        self._runtime = None
        if runtime is None:
            return {"closed": True, "servers": []}
        manager = getattr(runtime.registry, "_mcp_manager", None)
        return manager.close() if manager is not None else {"closed": True, "servers": []}

    def __enter__(self) -> "ResumeCandidate":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()

    def __del__(self) -> None:
        # Callers should close explicitly; this also covers abandoned candidates.
        try:
            self.close()
        except Exception:
            pass

    def claim(self) -> ResumeRuntime:
        """Make the disk commit active and return the already-built runtime."""
        runtime = self._runtime
        if runtime is None:
            raise ResumeError("恢复候选对象已经结算，不能再次占用 session")
        # Persist the resume transformation together with the active handoff.
        # If either export or the locked commit fails, no runnable runtime is
        # returned and the old clean commit is never presented as resumed.
        try:
            if self.recovery_mode == "crash_recovery":
                latest_workspace_observation = observe_workspace_manifest(
                    self.expected_envelope, runtime.envelope["workspace_root"],
                )
                if (latest_workspace_observation.get("observed_digest")
                        != (runtime.workspace_observation or {}).get("observed_digest")):
                    raise SessionValidationError(
                        "崩溃恢复占用前工作区结构化观察发生变化；请重新准备恢复候选"
                    )
                boundary = deepcopy(self.expected_envelope["tool_boundary"])
                ready_by_invocation = {
                    item.get("invocation_id"): item
                    for item in boundary.get("pending_delegation_results", [])
                    if isinstance(item, dict)
                }
                for call in boundary.get("calls", []):
                    if call.get("status") != "pending":
                        continue
                    ready = ready_by_invocation.get(call.get("invocation_id"))
                    if ready is not None:
                        try:
                            raw_result = json.loads(ready["result_json"])
                        except (TypeError, KeyError, json.JSONDecodeError) as error:
                            raise ResumeError("持久委派结果原文无法解析") from error
                        delegation_id = call.get("delegation_id")
                        if not isinstance(delegation_id, str):
                            raise ResumeError("持久委派结果缺少 delegation_id")
                        record = next(
                            (item for item in runtime.state.delegation_records
                             if item.delegation_id == delegation_id), None,
                        )
                        if record is None or record.delivery_status != "committed":
                            raise ResumeError("持久委派结果的 State 生命周期未 committed")
                        attempt = next(
                            (item for item in runtime.state.attempts
                             if item.attempt_id == call.get("attempt_id")), None,
                        )
                        content = ready["result_json"]
                        call.update({
                            "status": "committed",
                            "permission": "allowed",
                            "handler_admitted": True,
                            "pre_generation_id": attempt.pre_generation_id if attempt else call.get("pre_generation_id"),
                            "generation_id": attempt.generation_id if attempt else call.get("generation_id"),
                            "delegation_result_hash": ready["result_hash"],
                            "result": {
                                "outcome": "succeeded", "content": content,
                                "output_excerpt": content[:200], "error_kind": None,
                                "exit_code": None,
                            },
                        })
                        continue
                    issue = next((item for item in runtime.state.crash_issues
                                  if item.invocation_id == call.get("invocation_id")), None)
                    if issue is None:
                        raise ResumeError("恢复 issue 与源 invocation 不匹配")
                    attempt = next((item for item in runtime.state.attempts
                                    if item.attempt_id == issue.attempt_id), None)
                    if issue.classification == "not_executed":
                        outcome = "invalid"
                        error_kind = "interrupted_before_handler"
                        content = json.dumps({
                            "status": "interrupted_before_handler",
                            "message": "调用在崩溃前未进入 handler，恢复时明确记为未执行。",
                        }, ensure_ascii=False)
                    else:
                        outcome = "uncertain"
                        error_kind = "crash_recovery_uncertain"
                        content = json.dumps({
                            "status": "uncertain", "issue_id": issue.issue_id,
                            "classification": issue.classification,
                            "message": issue.reason,
                        }, ensure_ascii=False)
                    call.update({
                        "status": "committed", "attempt_id": issue.attempt_id,
                        "pre_generation_id": attempt.pre_generation_id if attempt else None,
                        "generation_id": attempt.generation_id if attempt else None,
                        "permission": "allowed" if issue.handler_admitted else call.get("permission", "not_checked"),
                        "handler_admitted": issue.handler_admitted,
                        "result": {
                            "outcome": outcome, "content": content,
                            "output_excerpt": content[:200], "error_kind": error_kind,
                            "exit_code": None,
                        },
                    })
                boundary.pop("pending_delegation_results", None)
                boundary["status"] = "committed"
                claimed = self.store.claim_crash_recovery(
                    runtime.session_id, self.expected_envelope,
                    state_export=runtime.state.export_session(allow_pending=True),
                    context_export=runtime.context.export_session(),
                    tool_boundary=boundary,
                    workspace_root=runtime.envelope["workspace_root"],
                    workspace_manifest=None,
                    recovery_id=(runtime.state.crash_recoveries[-1].recovery_id
                                 if runtime.state.crash_recoveries else ""),
                )
            else:
                claimed = self.store.claim_resume(
                    runtime.session_id,
                    self.expected_envelope,
                    state_export=runtime.state.export_session(),
                    context_export=runtime.context.export_session(),
                )
        except BaseException:
            self.close()
            raise
        self._runtime = None
        runtime.envelope = claimed
        if self.recovery_mode == "crash_recovery":
            runtime.session_id = claimed["session_id"]
            for index, record in enumerate(runtime.state.crash_recoveries):
                if record.derived_session_id is None:
                    runtime.state.crash_recoveries[index] = replace(
                        record, derived_session_id=claimed["session_id"],
                    )
        return runtime


def prepare_resume(store: SessionStore, session_id: str,
                   workspace_root: str | os.PathLike[str] | None = None,
                   provider_catalog: ProviderCatalog | None = None) -> ResumeCandidate:
    """Read, check, and build a candidate without calling LLMs or handlers."""
    try:
        envelope = store.load(session_id)
    except SessionError:
        raise
    if envelope.get("schema_version") == 1:
        raise ResumeError("schema 1 会话只供诊断，不能续跑")
    if envelope.get("schema_version") not in {SCHEMA_2_VERSION, SCHEMA_VERSION}:
        raise ResumeError("不支持的 session schema，不能续跑")
    crash_mode = (
        envelope.get("schema_version") == SCHEMA_VERSION
        and envelope.get("handoff_status") == "active"
        and envelope.get("save_kind") == "tool_boundary"
        and envelope.get("tool_boundary", {}).get("status") == "pending"
    )
    workspace_observation: dict[str, Any] | None = None
    if crash_mode:
        workspace_observation = observe_workspace_manifest(envelope, workspace_root)
        issues = list(workspace_observation.get("issues", []))
    else:
        if envelope.get("save_kind") != "safe_point":
            raise ResumeError("只有 safe_point 会话可以恢复")
        if envelope.get("handoff_status") != "clean":
            raise ResumeError("只有 clean 会话可以恢复；当前会话仍是 active")
        if (envelope.get("schema_version") == SCHEMA_VERSION
                and envelope.get("tool_boundary", {}).get("status") != "committed"):
            raise ResumeError("会话包含未完成但无可用恢复证据的 tool_boundary；v0.33 拒绝猜测并续跑")
        issues = check_workspace_manifest(envelope, workspace_root)
        if issues:
            raise ResumeError("工作区检查失败：" + "；".join(issues[:20]))

    root = envelope["workspace_root"]
    provider_catalog = provider_catalog or load_provider_catalog()
    parent_binding = provider_catalog.parent_binding()
    saved_binding_ref = envelope["context"].get("model_binding_ref")
    if saved_binding_ref is not None and saved_binding_ref != parent_binding.reference.to_dict():
        raise ResumeError("会话的父模型绑定配置已变化；请恢复原 provider/profile 配置后再继续")
    raw_state = envelope["state"]
    active_records = [
        item for item in raw_state.get("process_records", [])
        if isinstance(item, dict)
        and (item.get("status") == "running" or item.get("write_pending"))
    ]
    if not crash_mode and (raw_state.get("status") == "awaiting_process" or active_records):
        raise ResumeError("会话包含旧后台进程或在途 stdin，v0.31 不恢复该会话")

    state = AgentState.restore_session(raw_state, root, allow_pending=crash_mode)
    if crash_mode:
        raw_boundary = envelope["tool_boundary"]
        pending_results = [
            deepcopy(item) for item in raw_boundary.get("pending_delegation_results", [])
            if isinstance(item, dict)
        ]
        ready_invocations = {item.get("invocation_id") for item in pending_results}
        state.reconcile_pending_delegation_boundary(
            raw_boundary["calls"], pending_results,
        )
        # A durable child result is a complete fact.  Settle the parent
        # attempt before opening the fresh crash generation; no child worker or
        # child LLM is started during this path.
        for entry in pending_results:
            call = next((item for item in raw_boundary["calls"]
                         if item.get("invocation_id") == entry.get("invocation_id")), None)
            if call is None or not isinstance(call.get("delegation_id"), str):
                raise ResumeError("持久委派结果缺少对应调用")
            try:
                raw_result = json.loads(entry["result_json"])
            except (TypeError, KeyError, json.JSONDecodeError) as error:
                raise ResumeError("持久委派结果原文无法解析") from error
            state.commit_recovered_delegation_result(
                call["delegation_id"], call, raw_result,
            )
        pending_calls = [
            deepcopy(item) for item in raw_boundary["calls"]
            if item.get("status") == "pending"
            and item.get("invocation_id") not in ready_invocations
        ]
        state.begin_crash_recovery(
            envelope["session_id"], envelope["session_generation"],
            envelope["session_generation"], envelope["integrity"]["sha256"],
            envelope["tool_boundary"].get("round_id", 0), pending_calls, issues,
            workspace_observation.get("observed_digest") if workspace_observation else None,
        )
    else:
        # A clean derived branch may be reopened while its crash issues are
        # still awaiting user decisions.  The crash generation is already the
        # hand-off generation; opening a separate ordinary resume generation
        # here would make the required investigation attempt appear to belong
        # to the wrong generation and deadlock /resolve continue.
        if not state.has_unresolved_crash_recovery():
            state.begin_resume()
    instructions = InstructionLoader(root).load()
    system_prompt = build_system_prompt(project_instructions=instructions) if instructions else build_system_prompt()
    protected_messages: list[dict[str, object]] = [{"role": "system", "content": system_prompt}]
    context_payload = deepcopy(envelope["context"])
    if crash_mode:
        issue_by_invocation = {item.invocation_id: item for item in state.crash_issues}
        ready_by_invocation = {
            item.get("invocation_id"): item
            for item in envelope["tool_boundary"].get("pending_delegation_results", [])
            if isinstance(item, dict)
        }
        for call in envelope["tool_boundary"]["calls"]:
            if call.get("status") != "pending":
                continue
            ready = ready_by_invocation.get(call.get("invocation_id"))
            if ready is not None:
                context_payload["history"].append({
                    "role": "tool", "tool_call_id": call["tool_call_id"],
                    "content": ready["result_json"],
                })
                continue
            issue = issue_by_invocation.get(call.get("invocation_id"))
            if issue is None:
                raise ResumeError("pending invocation 缺少 crash issue")
            if issue.classification == "not_executed":
                content = json.dumps({
                    "status": "interrupted_before_handler",
                    "message": "调用在崩溃前未进入 handler，恢复时明确记为未执行。",
                }, ensure_ascii=False)
            else:
                content = json.dumps({
                    "status": "uncertain", "issue_id": issue.issue_id,
                    "classification": issue.classification, "message": issue.reason,
                }, ensure_ascii=False)
            context_payload["history"].append({
                "role": "tool", "tool_call_id": call["tool_call_id"], "content": content,
            })
    context = ContextManager.restore_session(
        state, context_payload, protected_messages=protected_messages,
        budget=ContextBudget(
            window=parent_binding.profile.context_window,
            output_reserve_tokens=parent_binding.profile.max_output_tokens,
        ),
        summarizer=lambda messages: parent_binding.complete(
            messages, include_tools=False, stream_output=False,
        ).message.get("content", "") or "",
        model_binding=parent_binding,
        usage_meter=parent_binding.usage_meter,
    )
    historical_process_ids = [item.process_id for item in state.processes]
    process_manager = ProcessManager(historical_process_ids=historical_process_ids)
    # create_registry preserves the restored metadata-only checkpoint store;
    # workspace_root is still passed so delegation uses the validated session
    # workspace rather than the caller's ambient current directory.
    registry = create_registry(
        state, workspace_root=root, process_manager=process_manager,
        provider_catalog=provider_catalog,
    )
    # Do not restore candidates from the session.  Bind a fresh parent-side
    # retriever to the current workspace store for the next LLM request.
    try:
        context.memory_retriever = MemoryRetriever(registry._memory_store)
        context.memory_retrieval_enabled = MEMORY_RETRIEVAL_ENABLED
        permission_gate = PermissionGate()
        tool_executor = ToolExecutor(
            registry, gate=permission_gate, on_result=state.record_tool,
        )
        runtime = ResumeRuntime(
            session_id=session_id, envelope=deepcopy(envelope), state=state,
            context=context, registry=registry, process_manager=process_manager,
            permission_gate=permission_gate, tool_executor=tool_executor,
            protected_messages=protected_messages,
            recovery_mode="crash_recovery" if crash_mode else "safe_point",
            source_session_id=envelope["session_id"] if crash_mode else None,
            workspace_report=issues if crash_mode else None,
            workspace_observation=workspace_observation if crash_mode else None,
        )
    except BaseException:
        manager = getattr(registry, "_mcp_manager", None)
        if manager is not None:
            manager.close()
        raise
    return ResumeCandidate(
        store, envelope, runtime, "crash_recovery" if crash_mode else "safe_point",
    )


def resume_session(store: SessionStore, session_id: str,
                   workspace_root: str | os.PathLike[str] | None = None) -> ResumeRuntime:
    """Convenience API used by integrations that do not need the candidate."""
    return prepare_resume(store, session_id, workspace_root).claim()
