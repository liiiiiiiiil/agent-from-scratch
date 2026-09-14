"""Private, atomic v0.30 session storage.

This module only validates and stores a safe-point representation.  It does
not restore an AgentState, rebuild a ContextManager, call an LLM, or execute
tools; those are deliberately deferred to v0.31.
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
from mini_agent.state import AgentState, SessionExportError


SCHEMA_VERSION = 1
SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
_INTEGRITY_ALGORITHM = "sha256"


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


def _validate_context_export(payload: Any) -> None:
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
    if expected:
        raise SessionValidationError("assistant tool call 结果未完整回灌")


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
                       handoff_status: str, save_kind: str) -> dict[str, Any]:
        self._check_id(session_id)
        if handoff_status not in {"active", "clean"}:
            raise SessionValidationError("handoff_status 必须是 active 或 clean")
        if save_kind not in {"safe_point", "tool_boundary"}:
            raise SessionValidationError("save_kind 无效")
        try:
            AgentState.validate_session_export(state)
        except (SessionExportError, KeyError, TypeError, ValueError) as error:
            raise SessionValidationError(f"State 导出校验失败: {error}") from error
        _validate_context_export(context)
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "session_id": session_id,
            "writer_version": __version__,
            "workspace_root": _normal_workspace_root(workspace_root),
            "saved_at": self._saved_at(),
            "save_kind": save_kind,
            "handoff_status": handoff_status,
            "state": deepcopy(state),
            "context": deepcopy(context),
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
             handoff_status: str = "active", save_kind: str = "safe_point") -> dict[str, Any]:
        """Atomically save an exported State/Context and return the envelope."""
        selected_id = self._check_id(session_id) if session_id is not None else _new_session_id()
        try:
            state_export = state.export_session() if hasattr(state, "export_session") else deepcopy(state)
            context_export = context.export_session() if hasattr(context, "export_session") else deepcopy(context)
        except (SessionExportError, SessionValidationError, KeyError, TypeError, ValueError) as error:
            raise SessionValidationError(str(error)) from error
        envelope = self._make_envelope(
            selected_id, state_export, context_export, workspace_root,
            handoff_status, save_kind,
        )
        lock_fd = self._acquire_lock(selected_id)
        try:
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
        expected_fields = {
            "schema_version", "session_id", "writer_version", "workspace_root",
            "saved_at", "save_kind", "handoff_status", "state", "context", "integrity",
        }
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
        if envelope.get("schema_version") != SCHEMA_VERSION:
            raise SessionValidationError("未知或不支持的 session schema_version")
        if envelope.get("session_id") != requested_id:
            raise SessionValidationError("session_id 与文件名不一致")
        if not isinstance(envelope.get("writer_version"), str) or not envelope["writer_version"]:
            raise SessionValidationError("writer_version 无效")
        if envelope.get("handoff_status") not in {"active", "clean"}:
            raise SessionValidationError("handoff_status 无效")
        if envelope.get("save_kind") not in {"safe_point", "tool_boundary"}:
            raise SessionValidationError("save_kind 无效")
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
            AgentState.validate_session_export(envelope.get("state"))
        except (SessionExportError, KeyError, TypeError, ValueError) as error:
            raise SessionValidationError(f"State 引用校验失败: {error}") from error
        try:
            _validate_context_export(envelope.get("context"))
        except SessionValidationError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise SessionValidationError(f"Context 引用校验失败: {error}") from error
