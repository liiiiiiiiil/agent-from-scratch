"""Safe v0.31 session admission and fresh-runtime assembly."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import os
import stat
from typing import Any

from mini_agent.context import ContextManager
from mini_agent.instructions import InstructionLoader
from mini_agent.permission import PermissionGate
from mini_agent.processes import ProcessManager
from mini_agent.prompt import build_system_prompt
from mini_agent.session import (
    SCHEMA_VERSION,
    SessionError,
    SessionStore,
    SessionValidationError,
    _normal_workspace_root,
)
from mini_agent.state import AgentState
from mini_agent.tools import create_registry
from mini_agent.tools.base import ToolExecutor


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


def check_workspace_manifest(envelope: dict[str, Any], workspace_root: str | os.PathLike[str] | None = None) -> list[str]:
    """Return explicit workspace changes or uncheckable paths."""
    stored_root = envelope.get("workspace_root")
    try:
        normalized_current = _normal_workspace_root(workspace_root)
    except SessionValidationError as error:
        return [str(error)]
    if stored_root != normalized_current:
        return [f"工作区根路径不一致：session={stored_root} 当前={normalized_current}"]
    if not os.path.isdir(normalized_current):
        return [f"当前工作区根路径不可用：{normalized_current}"]
    manifest = envelope.get("workspace_manifest")
    if not isinstance(manifest, dict):
        return ["session 缺少工作区清单，不能恢复"]
    issues: list[str] = []
    if manifest.get("root") != normalized_current:
        issues.append("工作区清单根路径不一致")
    if manifest.get("recoverable") is not True or manifest.get("complete") is not True:
        issues.extend(str(item) for item in manifest.get("issues", []) if isinstance(item, str))
        if not issues:
            issues.append("工作区清单不完整，不能恢复")
        return sorted(set(issues))
    for entry in manifest.get("entries", []):
        if not isinstance(entry, dict):
            issues.append("工作区清单含无效记录")
            continue
        relative = entry.get("path")
        absolute, error = _manifest_path(normalized_current, relative)
        label = str(relative or "<unknown>")
        if error or absolute is None:
            issues.append(f"{label}: {error or '无法检查'}")
            continue
        expected_kind = entry.get("kind")
        try:
            info = os.lstat(absolute)
            current_kind = _kind(info.st_mode)
        except FileNotFoundError:
            current_kind = "absent"
            info = None
        except OSError as exc:
            issues.append(f"{label}: 无法检查 ({type(exc).__name__})")
            continue
        if current_kind != expected_kind:
            issues.append(f"{label}: 类型从 {expected_kind} 变为 {current_kind}")
            continue
        if expected_kind == "file" and entry.get("available"):
            digest, read_error = _hash_file(absolute)
            if read_error:
                issues.append(f"{label}: {read_error}")
            elif digest != entry.get("sha256"):
                issues.append(f"{label}: 文件内容已变化")
        elif expected_kind == "directory" and entry.get("available"):
            if "entries" in entry:
                _compare_directory(absolute, entry.get("entries", []), issues, label)
        elif entry.get("available") is not True:
            issues.append(f"{label}: 该路径在保存时就无法完整检查")
    return sorted(set(issues))


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


@dataclass
class ResumeCandidate:
    store: SessionStore
    expected_envelope: dict[str, Any]
    _runtime: ResumeRuntime | None

    def claim(self) -> ResumeRuntime:
        """Make the disk commit active and return the already-built runtime."""
        runtime = self._runtime
        if runtime is None:
            raise ResumeError("恢复候选对象已经结算，不能再次占用 session")
        # Persist the resume transformation together with the active handoff.
        # If either export or the locked commit fails, no runnable runtime is
        # returned and the old clean commit is never presented as resumed.
        try:
            claimed = self.store.claim_resume(
                runtime.session_id,
                self.expected_envelope,
                state_export=runtime.state.export_session(),
                context_export=runtime.context.export_session(),
            )
        except BaseException:
            self._runtime = None
            raise
        self._runtime = None
        runtime.envelope = claimed
        return runtime


def prepare_resume(store: SessionStore, session_id: str,
                   workspace_root: str | os.PathLike[str] | None = None) -> ResumeCandidate:
    """Read, check, and build a candidate without calling LLMs or handlers."""
    try:
        envelope = store.load(session_id)
    except SessionError:
        raise
    if envelope.get("schema_version") != SCHEMA_VERSION:
        raise ResumeError("schema 1 会话只供诊断，不能续跑")
    if envelope.get("save_kind") != "safe_point":
        raise ResumeError("只有 safe_point 会话可以恢复")
    if envelope.get("handoff_status") != "clean":
        raise ResumeError("只有 clean 会话可以恢复；当前会话仍是 active")
    issues = check_workspace_manifest(envelope, workspace_root)
    if issues:
        raise ResumeError("工作区检查失败：" + "；".join(issues[:20]))

    root = envelope["workspace_root"]
    raw_state = envelope["state"]
    active_records = [
        item for item in raw_state.get("process_records", [])
        if isinstance(item, dict)
        and (item.get("status") == "running" or item.get("write_pending"))
    ]
    if raw_state.get("status") == "awaiting_process" or active_records:
        raise ResumeError("会话包含旧后台进程或在途 stdin，v0.31 不恢复该会话")

    state = AgentState.restore_session(raw_state, root)
    state.begin_resume()
    instructions = InstructionLoader(root).load()
    system_prompt = build_system_prompt(project_instructions=instructions) if instructions else build_system_prompt()
    protected_messages: list[dict[str, object]] = [{"role": "system", "content": system_prompt}]
    context = ContextManager.restore_session(
        state, envelope["context"], protected_messages=protected_messages,
    )
    historical_process_ids = [item.process_id for item in state.processes]
    process_manager = ProcessManager(historical_process_ids=historical_process_ids)
    # The restored State already owns the metadata-only checkpoint store rooted
    # at the validated workspace.  Leaving workspace_root unset preserves that
    # store instead of asking the normal fresh-runtime path to create another.
    registry = create_registry(state, process_manager=process_manager)
    permission_gate = PermissionGate()
    tool_executor = ToolExecutor(
        registry, gate=permission_gate, on_result=state.record_tool,
    )
    runtime = ResumeRuntime(
        session_id=session_id, envelope=deepcopy(envelope), state=state,
        context=context, registry=registry, process_manager=process_manager,
        permission_gate=permission_gate, tool_executor=tool_executor,
        protected_messages=protected_messages,
    )
    return ResumeCandidate(store, envelope, runtime)


def resume_session(store: SessionStore, session_id: str,
                   workspace_root: str | os.PathLike[str] | None = None) -> ResumeRuntime:
    """Convenience API used by integrations that do not need the candidate."""
    return prepare_resume(store, session_id, workspace_root).claim()
