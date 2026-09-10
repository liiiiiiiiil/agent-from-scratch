"""Task-local, single-file checkpoints for bounded rollback.

The public :class:`FileCheckpoint` contains metadata only.  Original bytes
and absolute filesystem locations live in :class:`CheckpointStore`'s private
maps and are never rendered into State or tool results.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import os
import stat
import tempfile
from threading import Lock
from typing import Any, Literal

from mini_agent.config import MAX_CHECKPOINT_BYTES


CheckpointStatus = Literal["ready", "unavailable", "restored", "restore_failed"]
ImageType = Literal["absent", "regular_file", "symlink", "directory", "special", "unavailable"]


class CheckpointError(RuntimeError):
    """Base error raised by the internal rollback handler."""

    error_kind = "rollback_restore_failed"


class CheckpointConflictError(CheckpointError):
    """The current target no longer matches the saved after-image."""

    error_kind = "rollback_conflict"


class CheckpointRestoreError(CheckpointError):
    """The target matched, but restoring the before-image failed."""

    error_kind = "rollback_restore_failed"


@dataclass(frozen=True)
class FileCheckpoint:
    """JSON-safe checkpoint metadata; file bytes and absolute paths are private."""

    checkpoint_id: str
    attempt_id: str
    generation_id: int
    path: str
    before_type: ImageType
    before_sha256: str | None
    after_type: ImageType
    after_sha256: str | None
    mode: int | None
    status: CheckpointStatus
    unavailable_reason: str | None = None
    created_at: int = 0

    def snapshot(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _Capture:
    checkpoint_id: str
    absolute_path: str


@dataclass(frozen=True)
class _Image:
    image_type: ImageType
    digest: str | None = None
    mode: int | None = None
    data: bytes | None = None
    reason: str | None = None


class CheckpointStore:
    """In-memory task store for bounded, single-file checkpoints."""

    def __init__(self, workspace_root: str | None = None,
                 max_bytes: int = MAX_CHECKPOINT_BYTES):
        if max_bytes < 0:
            raise ValueError("max_bytes 必须非负")
        root = os.path.abspath(workspace_root or os.getcwd())
        self._workspace_root = os.path.realpath(root)
        self._max_bytes = int(max_bytes)
        self._checkpoints: dict[str, FileCheckpoint] = {}
        self._before_bytes: dict[str, bytes] = {}
        self._locations: dict[str, str] = {}
        self._next_checkpoint = 1
        self._sequence = 0
        self._lock = Lock()

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    def clear(self) -> None:
        """Drop all metadata, bytes, locations, and reset checkpoint numbering."""
        with self._lock:
            self._checkpoints.clear()
            self._before_bytes.clear()
            self._locations.clear()
            self._next_checkpoint = 1
            self._sequence = 0

    def list_checkpoints(self) -> list[FileCheckpoint]:
        with self._lock:
            return list(self._checkpoints.values())

    def get(self, checkpoint_id: str) -> FileCheckpoint | None:
        with self._lock:
            return self._checkpoints.get(checkpoint_id)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [checkpoint.snapshot() for checkpoint in self._checkpoints.values()]

    def available(self) -> list[FileCheckpoint]:
        with self._lock:
            return [
                checkpoint for checkpoint in self._checkpoints.values()
                if checkpoint.status == "ready"
            ]

    def _new_id(self) -> str:
        checkpoint_id = f"cp-{self._next_checkpoint}"
        self._next_checkpoint += 1
        self._sequence += 1
        return checkpoint_id

    def _relative_path(self, absolute_path: str) -> str:
        try:
            return os.path.relpath(absolute_path, self._workspace_root)
        except ValueError:
            return "<unavailable>"

    def _resolve(self, path: Any) -> tuple[str, str, str | None]:
        """Return (absolute path, workspace-relative path, reason)."""
        if not isinstance(path, str) or not path:
            return "", "<unavailable>", "path 必须是非空字符串"
        if "\x00" in path:
            return "", "<unavailable>", "path 包含非法空字符"
        try:
            candidate = os.path.abspath(path if os.path.isabs(path)
                                        else os.path.join(self._workspace_root, path))
            resolved_candidate = os.path.realpath(candidate)
            relative = os.path.relpath(resolved_candidate, self._workspace_root)
        except (TypeError, ValueError, OSError) as exc:
            return "", "<unavailable>", f"路径解析失败: {type(exc).__name__}"

        outside = relative == os.pardir or relative.startswith(os.pardir + os.sep)

        # Walk the original spelling from the filesystem root.  Begin
        # checking after the first prefix that resolves to the workspace root;
        # this permits harmless aliases such as /var -> /private/var while
        # still rejecting a symlink below the task root.
        current = os.path.abspath(os.sep)
        checking = False
        parts = [part for part in candidate.split(os.sep) if part]
        for index, part in enumerate(parts):
            current = os.path.join(current, part)
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                if checking and index == len(parts) - 1:
                    # A missing final component is the supported tombstone.
                    break
                if checking:
                    return candidate, relative, "父目录无效或不存在"
                continue
            except OSError as exc:
                if checking:
                    return candidate, relative, f"路径检查失败: {type(exc).__name__}"
                continue
            if checking and stat.S_ISLNK(info.st_mode):
                return candidate, relative, "目标或路径组件是符号链接"
            if checking and index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
                return candidate, relative, "父目录无效"
            try:
                checking = os.path.realpath(current) == self._workspace_root
            except OSError:
                pass

        if outside:
            return candidate, relative, "路径位于工作区之外"

        # A second realpath check covers races and unusual platform path
        # resolution rules without exposing the resolved absolute path.
        try:
            resolved_root = os.path.realpath(self._workspace_root)
            resolved_candidate = os.path.realpath(candidate)
            if os.path.commonpath((resolved_root, resolved_candidate)) != resolved_root:
                return candidate, relative, "路径位于工作区之外"
        except (ValueError, OSError) as exc:
            return candidate, relative, f"路径解析失败: {type(exc).__name__}"
        return candidate, relative, None

    @staticmethod
    def _kind_from_mode(mode: int) -> ImageType:
        if stat.S_ISLNK(mode):
            return "symlink"
        if stat.S_ISDIR(mode):
            return "directory"
        if stat.S_ISREG(mode):
            return "regular_file"
        return "special"

    def _open_readonly(self, absolute_path: str):
        flags = os.O_RDONLY
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if nofollow:
            flags |= nofollow
        fd = os.open(absolute_path, flags)
        return os.fdopen(fd, "rb")

    def _read_before(self, absolute_path: str) -> _Image:
        try:
            info = os.lstat(absolute_path)
        except FileNotFoundError:
            return _Image("absent")
        except OSError as exc:
            return _Image("unavailable", reason=f"读取前镜像失败: {type(exc).__name__}")
        kind = self._kind_from_mode(info.st_mode)
        if kind != "regular_file":
            return _Image(kind, reason="目标不是普通文件")
        if info.st_size > self._max_bytes:
            return _Image("unavailable", reason=f"前镜像超过 {self._max_bytes} 字节上限")
        try:
            with self._open_readonly(absolute_path) as handle:
                opened_info = os.fstat(handle.fileno())
                if not stat.S_ISREG(opened_info.st_mode):
                    return _Image("unavailable", reason="目标不是普通文件")
                if opened_info.st_size > self._max_bytes:
                    return _Image("unavailable", reason=f"前镜像超过 {self._max_bytes} 字节上限")
                data = handle.read(self._max_bytes + 1)
                final_info = os.fstat(handle.fileno())
            if len(data) > self._max_bytes:
                return _Image("unavailable", reason=f"前镜像超过 {self._max_bytes} 字节上限")
            if final_info.st_size > self._max_bytes:
                return _Image("unavailable", reason=f"前镜像超过 {self._max_bytes} 字节上限")
            # lstat before open plus O_NOFOLLOW prevents the relevant symlink
            # escape.  The original mode is intentionally retained.
            return _Image("regular_file", hashlib.sha256(data).hexdigest(),
                          stat.S_IMODE(final_info.st_mode), data)
        except (OSError, ValueError) as exc:
            return _Image("unavailable", reason=f"读取前镜像失败: {type(exc).__name__}")

    def _read_after(self, absolute_path: str) -> _Image:
        try:
            info = os.lstat(absolute_path)
        except FileNotFoundError:
            return _Image("absent")
        except OSError as exc:
            return _Image("unavailable", reason=f"读取后镜像失败: {type(exc).__name__}")
        kind = self._kind_from_mode(info.st_mode)
        if kind != "regular_file":
            return _Image(kind, reason="后镜像不是普通文件")
        digest = hashlib.sha256()
        try:
            with self._open_readonly(absolute_path) as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            return _Image("regular_file", digest.hexdigest(), stat.S_IMODE(info.st_mode))
        except (OSError, ValueError) as exc:
            return _Image("unavailable", reason=f"读取后镜像失败: {type(exc).__name__}")

    def capture_before(self, attempt_id: str, generation_id: int,
                       path: str) -> _Capture:
        """Create a checkpoint before the file handler runs."""
        with self._lock:
            checkpoint_id = self._new_id()
            absolute_path, relative, resolve_error = self._resolve(path)
            if resolve_error:
                before = _Image("unavailable", reason=resolve_error)
            else:
                before = self._read_before(absolute_path)
            status: CheckpointStatus = "ready" if before.image_type in ("absent", "regular_file") else "unavailable"
            checkpoint = FileCheckpoint(
                checkpoint_id, attempt_id, generation_id, relative,
                before.image_type, before.digest, "unavailable", None,
                before.mode, status, before.reason, self._sequence,
            )
            self._checkpoints[checkpoint_id] = checkpoint
            self._locations[checkpoint_id] = absolute_path
            if before.data is not None:
                self._before_bytes[checkpoint_id] = before.data
            return _Capture(checkpoint_id, absolute_path)

    # Friendly alias for callers that use the plan's terminology.
    start_checkpoint = capture_before

    def capture_after(self, capture: _Capture) -> FileCheckpoint:
        """Record the actual post-handler image and finalize status."""
        with self._lock:
            current = self._checkpoints[capture.checkpoint_id]
            safe_path, _, resolve_error = self._resolve(current.path)
            after = (
                _Image("unavailable", reason=resolve_error)
                if resolve_error else self._read_after(safe_path)
            )
            if current.status == "unavailable":
                status: CheckpointStatus = "unavailable"
                reason = current.unavailable_reason or after.reason
            elif after.image_type in ("absent", "regular_file"):
                status = "ready"
                reason = None
            else:
                status = "unavailable"
                reason = after.reason or "无法确定后镜像"
            updated = FileCheckpoint(
                current.checkpoint_id, current.attempt_id, current.generation_id,
                current.path, current.before_type, current.before_sha256,
                after.image_type, after.digest, current.mode, status, reason,
                current.created_at,
            )
            self._checkpoints[current.checkpoint_id] = updated
            return updated

    # Friendly alias for callers that use the plan's terminology.
    finish_checkpoint = capture_after

    def validate_rollback(self, checkpoint_id: Any) -> tuple[FileCheckpoint | None, str | None]:
        with self._lock:
            if not isinstance(checkpoint_id, str) or not checkpoint_id:
                return None, "rollback 必须提供 checkpoint_id"
            checkpoint = self._checkpoints.get(checkpoint_id)
            if checkpoint is None:
                return None, "未知 checkpoint"
            if checkpoint.status != "ready":
                return None, f"checkpoint 状态为 {checkpoint.status}，不可 rollback"
            if checkpoint.before_type not in ("absent", "regular_file"):
                return None, "checkpoint 前镜像不可恢复"
            if checkpoint.after_type not in ("absent", "regular_file"):
                return None, "checkpoint 后镜像不可确定"
            return checkpoint, None

    def restore(self, checkpoint_id: str) -> str:
        """Restore a ready checkpoint, or raise a typed rollback error."""
        with self._lock:
            checkpoint = self._checkpoints.get(checkpoint_id)
            if checkpoint is None:
                raise CheckpointRestoreError("未知 checkpoint")
            if checkpoint.status != "ready":
                raise CheckpointRestoreError(
                    f"checkpoint 状态为 {checkpoint.status}，不可 rollback"
                )
            absolute_path = self._locations.get(checkpoint_id)
            before_data = self._before_bytes.get(checkpoint_id)
            if absolute_path is None:
                raise CheckpointRestoreError("checkpoint 路径不可用")

            # Re-run component validation immediately before the current-image
            # check.  A parent that became a symlink must never be used by the
            # restore write, even if the final target digest happens to match.
            safe_path, _, resolve_error = self._resolve(checkpoint.path)
            if resolve_error:
                raise CheckpointConflictError(
                    f"rollback_conflict: 当前路径不可安全访问 ({checkpoint.path})"
                )
            absolute_path = safe_path

            current = self._read_after(absolute_path)
            if (current.image_type != checkpoint.after_type or
                    current.digest != checkpoint.after_sha256):
                raise CheckpointConflictError(
                    f"rollback_conflict: 当前文件与 checkpoint 后镜像不一致 ({checkpoint.path})"
                )

            try:
                if checkpoint.before_type == "regular_file":
                    if before_data is None:
                        raise OSError("前镜像字节不可用")
                    parent = os.path.dirname(absolute_path)
                    fd, temporary = tempfile.mkstemp(
                        prefix=".mini-agent-rollback-", dir=parent
                    )
                    try:
                        with os.fdopen(fd, "wb") as handle:
                            handle.write(before_data)
                            handle.flush()
                        os.chmod(
                            temporary,
                            checkpoint.mode if checkpoint.mode is not None else 0o644,
                        )
                        os.replace(temporary, absolute_path)
                    except Exception:
                        try:
                            os.unlink(temporary)
                        except Exception:
                            pass
                        raise
                elif checkpoint.before_type == "absent":
                    if checkpoint.after_type == "regular_file":
                        os.unlink(absolute_path)
                else:
                    raise OSError("不支持的前镜像类型")
            except CheckpointConflictError:
                raise
            except CheckpointError:
                failed = FileCheckpoint(
                    checkpoint.checkpoint_id, checkpoint.attempt_id,
                    checkpoint.generation_id, checkpoint.path,
                    checkpoint.before_type, checkpoint.before_sha256,
                    checkpoint.after_type, checkpoint.after_sha256, checkpoint.mode,
                    "restore_failed", checkpoint.unavailable_reason,
                    checkpoint.created_at,
                )
                self._checkpoints[checkpoint_id] = failed
                raise
            except Exception as exc:
                failed = FileCheckpoint(
                    checkpoint.checkpoint_id, checkpoint.attempt_id,
                    checkpoint.generation_id, checkpoint.path,
                    checkpoint.before_type, checkpoint.before_sha256,
                    checkpoint.after_type, checkpoint.after_sha256, checkpoint.mode,
                    "restore_failed", checkpoint.unavailable_reason,
                    checkpoint.created_at,
                )
                self._checkpoints[checkpoint_id] = failed
                raise CheckpointRestoreError(
                    f"恢复失败: {type(exc).__name__}: {exc}"
                ) from exc

            restored = FileCheckpoint(
                checkpoint.checkpoint_id, checkpoint.attempt_id,
                checkpoint.generation_id, checkpoint.path,
                checkpoint.before_type, checkpoint.before_sha256,
                checkpoint.after_type, checkpoint.after_sha256, checkpoint.mode,
                "restored", checkpoint.unavailable_reason, checkpoint.created_at,
            )
            self._checkpoints[checkpoint_id] = restored
            return f"已恢复 checkpoint {checkpoint_id}: {checkpoint.path}"


def make_rollback_checkpoint_tool(store: CheckpointStore):
    """Build the model-invisible internal tool bound to one task store."""
    from mini_agent.tools.base import Tool

    return Tool(
        name="rollback_checkpoint",
        description="内部单文件 checkpoint 恢复工具；仅由 RecoveryRuntime 调用。",
        parameters={
            "type": "object",
            "properties": {"checkpoint_id": {"type": "string"}},
            "required": ["checkpoint_id"],
            "additionalProperties": False,
        },
        handler=store.restore,
        effect_class="possible",
        internal=True,
    )
