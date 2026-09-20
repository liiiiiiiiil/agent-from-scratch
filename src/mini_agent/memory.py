"""Small, workspace-scoped persistent memory store.

Memory is deliberately separate from ``AgentState`` and session persistence.
It is user/model supplied reference material, not an instruction channel,
plan state, or verification evidence.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
from typing import Any, Callable


SCHEMA_VERSION = 1
DEFAULT_MEMORY_DIR = "~/.mini_agent/memory"
MAX_MEMORIES = 256
MAX_TITLE_CHARS = 120
MAX_BODY_CHARS = 2000
MAX_TAGS = 8
MAX_TAG_CHARS = 32
MAX_SOURCE_CHARS = 240
MAX_JSON_BYTES = 1 * 1024 * 1024
LOCK_TIMEOUT_SECONDS = 2.0
_HALTED_WORKSPACES: set[tuple[str, str]] = set()
_HALTED_LOCK = threading.Lock()


class MemoryStoreError(RuntimeError):
    """Base class for readable, non-destructive memory storage errors."""


class MemoryValidationError(MemoryStoreError, ValueError):
    """A memory record or storage document violates the schema."""


class MemoryCorruptError(MemoryStoreError):
    """The workspace memory document is missing valid JSON/schema data."""


class MemoryNotFoundError(MemoryStoreError, KeyError):
    """The requested memory ID does not exist."""


class MemoryConflictError(MemoryStoreError):
    """An optimistic revision check failed; no write was performed."""


class MemoryLockTimeoutError(MemoryStoreError):
    """The workspace-specific lock remained held until the bounded timeout."""


class MemoryCommitUncertainError(MemoryStoreError):
    """An atomic replacement happened but its directory sync was not confirmed."""

    error_kind = "memory_commit_uncertain"


def _normal_workspace_root(workspace_root: str | os.PathLike[str] | None) -> str:
    if workspace_root is None:
        workspace_root = os.getcwd()
    try:
        value = os.fspath(workspace_root)
    except TypeError as error:
        raise MemoryValidationError("工作区根路径必须是路径字符串") from error
    if isinstance(value, bytes):
        raise MemoryValidationError("工作区根路径必须是文本路径")
    value = os.path.realpath(os.path.abspath(value))
    if not value:
        raise MemoryValidationError("工作区根路径不能为空")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _require_string(value: Any, field: str, maximum: int, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise MemoryValidationError(f"{field} 必须是字符串")
    if not allow_empty and not value:
        raise MemoryValidationError(f"{field} 不能为空")
    if len(value) > maximum:
        raise MemoryValidationError(f"{field} 超过 {maximum} 字符上限")
    return value


def _normalize_fields(
    title: Any,
    body: Any,
    tags: Any,
    source: Any,
) -> tuple[str, str, list[str], str]:
    title = _require_string(title, "title", MAX_TITLE_CHARS)
    body = _require_string(body, "body", MAX_BODY_CHARS)
    if not isinstance(tags, (list, tuple)):
        raise MemoryValidationError("tags 必须是数组")
    if len(tags) > MAX_TAGS:
        raise MemoryValidationError(f"tags 不能超过 {MAX_TAGS} 个")
    normalized_tags = []
    for tag in tags:
        normalized_tags.append(_require_string(tag, "tag", MAX_TAG_CHARS))
    source = _require_string(source, "source", MAX_SOURCE_CHARS)
    return title, body, normalized_tags, source


def _validate_revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise MemoryValidationError("expected_revision 必须是正整数")
    return value


def _validate_record(record: Any, *, index: int = 0) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise MemoryCorruptError(f"记忆记录 {index} 不是 JSON object")
    expected = {
        "memory_id", "revision", "title", "body", "tags", "source",
        "created_at", "updated_at",
    }
    if set(record) != expected:
        raise MemoryCorruptError(f"记忆记录 {index} 字段不完整或包含未知字段")
    memory_id = record["memory_id"]
    if not isinstance(memory_id, str) or not memory_id:
        raise MemoryCorruptError(f"记忆记录 {index} 的 memory_id 非法")
    revision = record["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise MemoryCorruptError(f"记忆记录 {index} 的 revision 非法")
    title, body, tags, source = _normalize_fields(
        record["title"], record["body"], record["tags"], record["source"]
    )
    for field in ("created_at", "updated_at"):
        if not isinstance(record[field], str) or not record[field]:
            raise MemoryCorruptError(f"记忆记录 {index} 的 {field} 非法")
    return {
        "memory_id": memory_id,
        "revision": revision,
        "title": title,
        "body": body,
        "tags": tags,
        "source": source,
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
    }


def _json_bytes(payload: dict[str, Any]) -> bytes:
    try:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    except (TypeError, ValueError) as error:
        raise MemoryValidationError("记忆 JSON 无法编码") from error
    if len(raw) > MAX_JSON_BYTES:
        raise MemoryValidationError(f"记忆 JSON 超过 {MAX_JSON_BYTES} 字节上限")
    return raw


class MemoryStore:
    """A single JSON document selected by the canonical workspace identity."""

    def __init__(
        self,
        workspace_root: str | os.PathLike[str] | None = None,
        memory_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        self.workspace_root = _normal_workspace_root(workspace_root)
        if memory_dir is None:
            memory_dir = DEFAULT_MEMORY_DIR
        try:
            memory_dir = os.fspath(memory_dir)
        except TypeError as error:
            raise MemoryValidationError("MEMORY_DIR 必须是路径字符串") from error
        if isinstance(memory_dir, bytes) or not memory_dir:
            raise MemoryValidationError("MEMORY_DIR 必须是非空文本路径")
        self.root = os.path.realpath(os.path.abspath(os.path.expanduser(memory_dir)))
        try:
            inside_workspace = os.path.commonpath((self.workspace_root, self.root)) == self.workspace_root
        except ValueError:
            inside_workspace = False
        if inside_workspace:
            raise MemoryValidationError(
                "MEMORY_DIR 不能位于当前工作区内；请使用工作区之外的目录"
            )
        self.workspace_key = hashlib.sha256(self.workspace_root.encode("utf-8")).hexdigest()
        self.path = os.path.join(self.root, f"{self.workspace_key}.json")
        self.lock_path = os.path.join(self.root, f"{self.workspace_key}.lock")
        self._write_halted = False

    @property
    def _workspace_identity(self) -> tuple[str, str]:
        return self.root, self.workspace_key

    def _halt_writes(self) -> None:
        self._write_halted = True
        with _HALTED_LOCK:
            _HALTED_WORKSPACES.add(self._workspace_identity)

    @property
    def write_halted(self) -> bool:
        if self._write_halted:
            return True
        with _HALTED_LOCK:
            return self._workspace_identity in _HALTED_WORKSPACES

    def _ensure_root(self) -> None:
        try:
            os.makedirs(self.root, mode=0o700, exist_ok=True)
            os.chmod(self.root, 0o700)
        except OSError as error:
            raise MemoryStoreError(f"记忆目录无法创建或访问: {type(error).__name__}") from error

    def _read_records(self) -> list[dict[str, Any]]:
        try:
            size = os.stat(self.path).st_size
        except FileNotFoundError:
            return []
        except OSError as error:
            raise MemoryStoreError(f"记忆文件无法检查: {type(error).__name__}") from error
        if size > MAX_JSON_BYTES:
            raise MemoryCorruptError(f"记忆文件超过 {MAX_JSON_BYTES} 字节上限")
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            return []
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise MemoryCorruptError(f"记忆文件损坏: {type(error).__name__}") from error
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
            raise MemoryCorruptError("记忆文件 schema 未知或非法")
        if set(payload) != {"schema_version", "memories"}:
            raise MemoryCorruptError("记忆文件包含未知顶层字段")
        memories = payload.get("memories")
        if not isinstance(memories, list) or len(memories) > MAX_MEMORIES:
            raise MemoryCorruptError("记忆集合数量或类型非法")
        result = []
        seen: set[str] = set()
        for index, record in enumerate(memories):
            item = _validate_record(record, index=index)
            if item["memory_id"] in seen:
                raise MemoryCorruptError("记忆文件包含重复 memory_id")
            seen.add(item["memory_id"])
            result.append(item)
        # Re-encode the validated document so a hand-written oversized object
        # cannot bypass the byte limit through parser behavior.
        _json_bytes({"schema_version": SCHEMA_VERSION, "memories": result})
        return result

    def _acquire_lock(self) -> int:
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    os.fchmod(fd, 0o600)
                except (AttributeError, OSError):
                    pass
                return fd
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise MemoryLockTimeoutError(
                        "记忆存储锁等待超过 2 秒；不会自动抢占遗留锁"
                    )
                time.sleep(0.01)
            except OSError as error:
                raise MemoryStoreError(f"记忆存储锁无法创建: {type(error).__name__}") from error

    def _release_lock(self, fd: int) -> None:
        error: OSError | None = None
        try:
            os.close(fd)
        except OSError as caught:
            error = caught
        try:
            os.unlink(self.lock_path)
        except OSError as caught:
            error = error or caught
        if error is not None:
            raise MemoryStoreError(
                f"记忆存储锁清理失败: {type(error).__name__}；提交状态未确认"
            ) from error

    def _sync_root(self) -> None:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        directory_fd = os.open(self.root, flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _atomic_write(self, records: list[dict[str, Any]]) -> None:
        payload = {"schema_version": SCHEMA_VERSION, "memories": records}
        raw = _json_bytes(payload)
        fd: int | None = None
        temporary: str | None = None
        replaced = False
        try:
            fd, temporary = tempfile.mkstemp(prefix=f".{self.workspace_key}.", suffix=".tmp", dir=self.root)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                fd = None
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            replaced = True
            temporary = None
            try:
                self._sync_root()
            except OSError as error:
                self._halt_writes()
                raise MemoryCommitUncertainError(
                    "记忆文件已原子替换，但目录同步失败，提交状态未确认；"
                    "当前进程后续记忆写入已停止"
                ) from error
        except MemoryCommitUncertainError:
            raise
        except (OSError, UnicodeError) as error:
            if replaced:
                self._halt_writes()
                raise MemoryCommitUncertainError(
                    "记忆文件替换后存储状态未确认；当前进程后续记忆写入已停止"
                ) from error
            raise MemoryStoreError(f"记忆文件原子写入失败: {type(error).__name__}") from error
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
                except OSError:
                    # The original document remains untouched; report the
                    # primary write error rather than masking it.
                    pass

    def _mutate(self, callback: Callable[[list[dict[str, Any]]], tuple[Any, bool]]) -> Any:
        if self.write_halted:
            raise MemoryCommitUncertainError(
                "当前进程的记忆写入已停止：此前提交状态未确认；只读查看仍可用"
            )
        self._ensure_root()
        lock_fd = self._acquire_lock()
        result: Any = None
        pending: BaseException | None = None
        try:
            # Re-read only after acquiring the workspace-specific lock.
            records = self._read_records()
            result, changed = callback(records)
            if changed:
                self._atomic_write(records)
        except BaseException as error:
            pending = error
        try:
            self._release_lock(lock_fd)
        except BaseException as error:
            self._halt_writes()
            if pending is None:
                pending = error
        if pending is not None:
            raise pending
        return result

    @staticmethod
    def _summaries(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                key: record[key]
                for key in (
                    "memory_id", "revision", "title", "tags", "source",
                    "created_at", "updated_at",
                )
            }
            for record in records
        ]

    @staticmethod
    def _validate_offset(offset: Any) -> int:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise MemoryValidationError("offset 必须是非负整数")
        return offset

    def list_page(self, *, offset: int = 0, limit: int = 20) -> dict[str, Any]:
        offset = self._validate_offset(offset)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_MEMORIES:
            raise MemoryValidationError(f"limit 必须是 1 到 {MAX_MEMORIES} 的整数")
        summaries = self._summaries(self._read_records())
        page = summaries[offset:offset + limit]
        next_offset = offset + len(page) if offset + len(page) < len(summaries) else None
        return {
            "memories": deepcopy(page),
            "total": len(summaries),
            "next_offset": next_offset,
        }

    def list(self, limit: int = 20, *, offset: int | None = None) -> list[dict[str, Any]] | dict[str, Any]:
        """Return the legacy prefix list, or a paged result when offset is supplied."""
        if offset is not None:
            return self.list_page(offset=offset, limit=limit)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_MEMORIES:
            raise MemoryValidationError(f"limit 必须是 1 到 {MAX_MEMORIES} 的整数")
        return deepcopy(self._summaries(self._read_records()[:limit]))

    def list_memories(
        self, limit: int = 20, *, offset: int | None = None,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        return self.list(limit, offset=offset)

    def read(self, memory_id: str) -> dict[str, Any]:
        if not isinstance(memory_id, str) or not memory_id:
            raise MemoryValidationError("memory_id 必须是非空字符串")
        for record in self._read_records():
            if record["memory_id"] == memory_id:
                return deepcopy(record)
        raise MemoryNotFoundError(f"找不到记忆: {memory_id}")

    def read_memory(self, memory_id: str) -> dict[str, Any]:
        return self.read(memory_id)

    def remember(
        self,
        title: str,
        body: str,
        tags: list[str] | tuple[str, ...] = (),
        source: str = "",
    ) -> dict[str, Any]:
        fields = _normalize_fields(title, body, tags, source)

        def mutate(records: list[dict[str, Any]]) -> tuple[dict[str, Any], bool]:
            if len(records) >= MAX_MEMORIES:
                raise MemoryValidationError(f"记忆数量不能超过 {MAX_MEMORIES} 条")
            existing = {item["memory_id"] for item in records}
            memory_id = secrets.token_hex(16)
            while memory_id in existing:
                memory_id = secrets.token_hex(16)
            now = _utc_now()
            record = {
                "memory_id": memory_id,
                "revision": 1,
                "title": fields[0],
                "body": fields[1],
                "tags": list(fields[2]),
                "source": fields[3],
                "created_at": now,
                "updated_at": now,
            }
            records.append(record)
            return deepcopy(record), True

        return self._mutate(mutate)

    def revise(
        self,
        memory_id: str,
        expected_revision: int,
        title: str,
        body: str,
        tags: list[str] | tuple[str, ...] = (),
        source: str = "",
    ) -> dict[str, Any]:
        if not isinstance(memory_id, str) or not memory_id:
            raise MemoryValidationError("memory_id 必须是非空字符串")
        expected_revision = _validate_revision(expected_revision)
        fields = _normalize_fields(title, body, tags, source)

        def mutate(records: list[dict[str, Any]]) -> tuple[dict[str, Any], bool]:
            for record in records:
                if record["memory_id"] != memory_id:
                    continue
                if record["revision"] != expected_revision:
                    raise MemoryConflictError(
                        f"记忆 revision 冲突: expected={expected_revision}, current={record['revision']}"
                    )
                record.update({
                    "revision": record["revision"] + 1,
                    "title": fields[0],
                    "body": fields[1],
                    "tags": list(fields[2]),
                    "source": fields[3],
                    "updated_at": _utc_now(),
                })
                return deepcopy(record), True
            raise MemoryNotFoundError(f"找不到记忆: {memory_id}")

        return self._mutate(mutate)

    def forget(self, memory_id: str, expected_revision: int) -> dict[str, Any]:
        if not isinstance(memory_id, str) or not memory_id:
            raise MemoryValidationError("memory_id 必须是非空字符串")
        expected_revision = _validate_revision(expected_revision)

        def mutate(records: list[dict[str, Any]]) -> tuple[dict[str, Any], bool]:
            for index, record in enumerate(records):
                if record["memory_id"] != memory_id:
                    continue
                if record["revision"] != expected_revision:
                    raise MemoryConflictError(
                        f"记忆 revision 冲突: expected={expected_revision}, current={record['revision']}"
                    )
                records.pop(index)
                return {"memory_id": memory_id, "revision": expected_revision}, True
            raise MemoryNotFoundError(f"找不到记忆: {memory_id}")

        return self._mutate(mutate)


__all__ = [
    "DEFAULT_MEMORY_DIR", "MAX_MEMORIES", "MAX_TITLE_CHARS", "MAX_BODY_CHARS",
    "MAX_TAGS", "MAX_TAG_CHARS", "MAX_SOURCE_CHARS", "MAX_JSON_BYTES",
    "MemoryStore", "MemoryStoreError", "MemoryValidationError", "MemoryCorruptError",
    "MemoryNotFoundError", "MemoryConflictError", "MemoryLockTimeoutError",
    "MemoryCommitUncertainError",
]
