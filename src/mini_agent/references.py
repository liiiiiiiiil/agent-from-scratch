"""Bounded, read-only access to named local reference directories.

References deliberately live outside ``AgentState`` and the session model.  A
catalog freezes configuration at runtime construction, while every operation
resolves and checks its target again so a changed symlink or configuration
object cannot widen the directory boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import hashlib
import errno
import os
import re
import stat
from typing import Any, Iterable


ALIAS_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
MAX_REFERENCES = 32
MAX_ALIAS_CHARS = 64
MAX_DESCRIPTION_CHARS = 240
MAX_REFERENCE_PATH_CHARS = 1024
MAX_QUERY_CHARS = 256
MAX_INCLUDE_CHARS = 256
MAX_FILE_BYTES = 1024 * 1024
MAX_SEARCH_FILES = 2_000
MAX_SEARCH_ENTRIES = 10_000
MAX_SEARCH_BYTES = 64 * 1024 * 1024
MAX_SEARCH_MATCHES = 100
MAX_SEARCH_LINE_CHARS = 240
MAX_READ_LINES = 200
MAX_READ_OFFSET = 1_000_000
MAX_READ_LINE_CHARS = 4_096


class ReferenceError(RuntimeError):
    """Base class for safe, user-facing reference failures."""


class ReferenceConfigError(ReferenceError, ValueError):
    """The configured reference catalog is invalid."""

    error_kind = "reference_config_error"


class ReferenceAccessError(ReferenceError):
    """A reference target is missing, unsafe, inaccessible, or unsupported."""

    error_kind = "reference_access_error"


class _ReferenceScanLimit(ReferenceError):
    """Internal signal that a search resource budget was reached."""


@dataclass(frozen=True)
class ReferenceDefinition:
    """One frozen reference definition.

    ``root`` is intentionally an in-process field.  It is never returned by
    the tool contract and is not part of State, Context, Trace, or session data.
    """

    alias: str
    description: str
    root: str
    root_dev: int
    root_ino: int


@dataclass(frozen=True)
class _TargetSnapshot:
    resolved: str
    relative: str
    path_fingerprint: tuple[tuple[int, int, int, str | None], ...]
    target_identity: tuple[int, int]


def _bounded_string(value: Any, field: str, maximum: int, *, empty: bool = True) -> str:
    if not isinstance(value, str):
        raise ReferenceConfigError(f"{field} 必须是字符串")
    if not empty and not value:
        raise ReferenceConfigError(f"{field} 不能为空")
    if len(value) > maximum:
        raise ReferenceConfigError(f"{field} 超过 {maximum} 字符上限")
    return value


def _within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((root, path)) == root
    except (OSError, ValueError):
        return False


def _within_filesystem(path: str, root: str) -> bool:
    """Return whether an existing path is rooted at ``root`` by identity.

    ``realpath`` preserves caller-supplied casing on common macOS filesystems,
    so string-only containment can miss a sensitive directory reached through
    a differently-cased spelling.  Walk the existing ancestor chain and use
    ``samefile`` to retain the filesystem's own identity semantics.
    """
    if _within(path, root):
        return True
    current = path
    while True:
        try:
            if os.path.samefile(current, root):
                return True
        except (FileNotFoundError, OSError, ValueError):
            pass
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def _relative(root: str, path: str) -> str:
    result = os.path.relpath(path, root)
    return "." if result == os.curdir else result.replace(os.sep, "/")


def _has_parent_component(value: str) -> bool:
    return any(part == ".." for part in value.replace("/", os.sep).split(os.sep))


class ReferenceCatalog:
    """Frozen catalog with bounded, alias-scoped local file access."""

    def __init__(
        self,
        references: Any = None,
        *,
        config_base_dir: str | os.PathLike[str] | None = None,
        sensitive_roots: Iterable[str | os.PathLike[str]] = (),
    ) -> None:
        if references is None:
            references = []
        if not isinstance(references, list):
            raise ReferenceConfigError("REFERENCES 必须是数组")
        if len(references) > MAX_REFERENCES:
            raise ReferenceConfigError(f"REFERENCES 不能超过 {MAX_REFERENCES} 项")

        base = config_base_dir if config_base_dir is not None else os.getcwd()
        try:
            base = os.path.realpath(os.path.abspath(os.path.expanduser(os.fspath(base))))
        except (OSError, TypeError, ValueError) as error:
            raise ReferenceConfigError("References 配置基准目录无法解析") from error
        if not os.path.isabs(base):
            raise ReferenceConfigError("References 配置基准目录必须是绝对路径")

        frozen_sensitive: list[str] = []
        for raw in sensitive_roots:
            try:
                value = os.path.realpath(os.path.abspath(os.path.expanduser(os.fspath(raw))))
            except (OSError, TypeError, ValueError) as error:
                raise ReferenceConfigError("敏感目录无法解析") from error
            if value not in frozen_sensitive:
                frozen_sensitive.append(value)

        definitions: list[ReferenceDefinition] = []
        aliases: set[str] = set()
        for index, raw in enumerate(references):
            if not isinstance(raw, dict):
                raise ReferenceConfigError(f"REFERENCES[{index}] 必须是对象")
            if set(raw) != {"alias", "path", "description"}:
                raise ReferenceConfigError(
                    f"REFERENCES[{index}] 字段必须恰为 alias、path、description"
                )
            alias = _bounded_string(raw["alias"], "alias", MAX_ALIAS_CHARS, empty=False)
            if ALIAS_PATTERN.fullmatch(alias) is None:
                raise ReferenceConfigError(
                    "alias 必须以小写字母开头，只能包含小写字母、数字、_、-，且不超过 64 字符"
                )
            normalized_alias = alias.casefold()
            if normalized_alias in aliases:
                raise ReferenceConfigError(f"References alias 重复: {alias}")
            aliases.add(normalized_alias)
            description = _bounded_string(
                raw["description"], "description", MAX_DESCRIPTION_CHARS,
            )
            configured_path = _bounded_string(
                raw["path"], "path", MAX_REFERENCE_PATH_CHARS, empty=False,
            )
            if "\x00" in configured_path:
                raise ReferenceConfigError("path 不能包含 NUL")
            try:
                candidate = configured_path
                if not os.path.isabs(candidate):
                    candidate = os.path.join(base, candidate)
                root = os.path.realpath(os.path.abspath(os.path.expanduser(candidate)))
                info = os.stat(root)
            except (OSError, TypeError, ValueError) as error:
                raise ReferenceConfigError(f"Reference {alias} 目录无法访问") from error
            if not stat.S_ISDIR(info.st_mode):
                raise ReferenceConfigError(f"Reference {alias} path 不是目录")
            if any(_within_filesystem(root, sensitive) for sensitive in frozen_sensitive):
                raise ReferenceConfigError(f"Reference {alias} 目录位于敏感目录内")
            definitions.append(ReferenceDefinition(
                alias, description, root, int(info.st_dev), int(info.st_ino),
            ))

        self._definitions = tuple(definitions)
        self._by_alias = {item.alias: item for item in self._definitions}
        self._sensitive_roots = tuple(frozen_sensitive)

    @property
    def definitions(self) -> tuple[ReferenceDefinition, ...]:
        return self._definitions

    def list_references(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "references": [
                {"alias": item.alias, "description": item.description}
                for item in self._definitions
            ],
            "total": len(self._definitions),
        }

    def _definition(self, alias: Any) -> ReferenceDefinition:
        if not isinstance(alias, str) or alias not in self._by_alias:
            raise ReferenceAccessError("未知 Reference alias")
        return self._by_alias[alias]

    @staticmethod
    def _validate_relative(value: Any, field: str, *, allow_dot: bool) -> str:
        if not isinstance(value, str):
            raise ReferenceAccessError(f"{field} 必须是字符串")
        if not value or "\x00" in value:
            raise ReferenceAccessError(f"{field} 不能为空且不能包含 NUL")
        if len(value) > MAX_REFERENCE_PATH_CHARS:
            raise ReferenceAccessError(f"{field} 超过长度上限")
        if os.path.isabs(value) or value.startswith(("/", "\\")):
            raise ReferenceAccessError(f"{field} 必须是 alias 内相对路径")
        normalized = value.replace("\\", os.sep)
        if _has_parent_component(normalized):
            raise ReferenceAccessError(f"{field} 不允许包含 ..")
        if value == "." and not allow_dot:
            raise ReferenceAccessError(f"{field} 不能是目录根")
        return value

    def _sensitive(self, path: str) -> bool:
        return any(_within_filesystem(path, root) for root in self._sensitive_roots)

    @staticmethod
    def _same_identity(info: os.stat_result, definition: ReferenceDefinition) -> bool:
        return (int(info.st_dev), int(info.st_ino)) == (
            definition.root_dev, definition.root_ino,
        )

    def _check_root_identity(self, definition: ReferenceDefinition) -> None:
        try:
            info = os.stat(definition.root)
        except (OSError, ValueError) as error:
            raise ReferenceAccessError("Reference 根目录无法访问") from error
        if not stat.S_ISDIR(info.st_mode) or not self._same_identity(info, definition):
            raise ReferenceAccessError("Reference 根目录已发生变化")

    def _check_resolved_target(
        self,
        definition: ReferenceDefinition,
        resolved: str,
        *,
        directory: bool,
    ) -> os.stat_result:
        """Validate one already-canonical target without resolving it again."""
        if not _within(resolved, definition.root):
            raise ReferenceAccessError("Reference 路径越界")
        if (self._sensitive(resolved)
                or os.path.basename(resolved).casefold() == "config_local.py"):
            raise ReferenceAccessError("Reference 目标位于敏感路径")
        self._check_root_identity(definition)
        try:
            info = os.stat(resolved)
        except FileNotFoundError as error:
            raise ReferenceAccessError("Reference 目标不存在") from error
        except PermissionError as error:
            raise ReferenceAccessError("Reference 目标无权访问") from error
        except (OSError, ValueError) as error:
            raise ReferenceAccessError("Reference 目标无法解析") from error
        if directory:
            if not stat.S_ISDIR(info.st_mode):
                raise ReferenceAccessError("Reference 搜索范围不是目录")
        elif not stat.S_ISREG(info.st_mode):
            raise ReferenceAccessError("Reference 目标不是普通文件")
        return info

    @staticmethod
    def _path_fingerprint(
        definition: ReferenceDefinition,
        relative: str,
    ) -> tuple[tuple[int, int, int, str | None], ...]:
        current = definition.root
        result: list[tuple[int, int, int, str | None]] = []
        parts = [] if relative == "." else relative.replace("\\", os.sep).split(os.sep)
        for part in parts:
            current = os.path.join(current, part)
            try:
                info = os.lstat(current)
                link = os.readlink(current) if stat.S_ISLNK(info.st_mode) else None
            except (OSError, ValueError) as error:
                raise ReferenceAccessError("Reference 路径在打开时发生变化") from error
            result.append((int(info.st_dev), int(info.st_ino), int(info.st_mode), link))
        return tuple(result)

    def _check_path_fingerprint(
        self,
        definition: ReferenceDefinition,
        relative: str,
        expected: tuple[tuple[int, int, int, str | None], ...],
    ) -> None:
        if self._path_fingerprint(definition, relative) != expected:
            raise ReferenceAccessError("Reference 路径在打开时发生变化")

    def _safe_target(
        self,
        definition: ReferenceDefinition,
        relative: str,
        *,
        directory: bool,
    ) -> _TargetSnapshot:
        self._validate_relative(relative, "path", allow_dot=directory)
        candidate = definition.root if relative == "." else os.path.abspath(
            os.path.join(definition.root, relative.replace("/", os.sep))
        )
        try:
            # This is the one final canonical resolution for the requested
            # alias-relative path.  The open path below is derived from this
            # result, never from the original user-controlled spelling.
            resolved = os.path.realpath(candidate)
            info = self._check_resolved_target(
                definition, resolved, directory=directory,
            )
        except ReferenceAccessError:
            raise
        except (OSError, ValueError) as error:
            raise ReferenceAccessError("Reference 目标无法解析") from error

        # A directory symlink in an explicit path is not a safe way to widen
        # the search root.  A final file symlink is permitted when its target
        # has already resolved inside the frozen root.
        current = definition.root
        parts = [] if relative == "." else relative.replace("\\", os.sep).split(os.sep)
        for index, part in enumerate(parts):
            current = os.path.join(current, part)
            try:
                mode = os.lstat(current).st_mode
            except OSError as error:
                raise ReferenceAccessError("Reference 目标无法检查") from error
            if stat.S_ISLNK(mode) and (directory or index < len(parts) - 1):
                raise ReferenceAccessError("Reference 路径不允许经过目录符号链接")
        lexical_relative = _relative(definition.root, candidate)
        return _TargetSnapshot(
            resolved,
            lexical_relative,
            self._path_fingerprint(definition, lexical_relative),
            (int(info.st_dev), int(info.st_ino)),
        )

    @staticmethod
    def _open_flags(*, directory: bool = False, nonblocking: bool = False) -> int:
        flags = os.O_RDONLY
        if directory and hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if nonblocking and hasattr(os, "O_NONBLOCK"):
            flags |= os.O_NONBLOCK
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        return flags

    @staticmethod
    def _supports_dir_fd() -> bool:
        return (
            os.open in getattr(os, "supports_dir_fd", set())
            and hasattr(os, "O_NOFOLLOW")
            and hasattr(os, "O_DIRECTORY")
        )

    def _open_canonical_fd(
        self,
        definition: ReferenceDefinition,
        resolved: str,
        *,
        directory: bool,
    ) -> int:
        """Open a canonical path one component at a time beneath root."""
        self._check_root_identity(definition)
        relative = os.path.relpath(resolved, definition.root)
        if relative == os.curdir:
            if not directory:
                raise ReferenceAccessError("Reference 路径越界")
            try:
                fd = os.open(definition.root, self._open_flags(directory=True))
                info = os.fstat(fd)
                if not stat.S_ISDIR(info.st_mode) or not self._same_identity(info, definition):
                    os.close(fd)
                    raise ReferenceAccessError("Reference 根目录已发生变化")
                return fd
            except ReferenceAccessError:
                raise
            except OSError as error:
                raise ReferenceAccessError("Reference 目录无法读取") from error
        if relative == os.pardir or os.path.isabs(relative):
            raise ReferenceAccessError("Reference 路径越界")
        parts = [part for part in relative.split(os.sep) if part not in ("", os.curdir)]
        if any(part == os.pardir for part in parts) or not parts:
            raise ReferenceAccessError("Reference 路径越界")
        current_fd: int | None = None
        try:
            current_fd = os.open(definition.root, self._open_flags(directory=True))
            root_info = os.fstat(current_fd)
            if not stat.S_ISDIR(root_info.st_mode) or not self._same_identity(root_info, definition):
                raise ReferenceAccessError("Reference 根目录已发生变化")
            for index, part in enumerate(parts):
                last = index == len(parts) - 1
                flags = self._open_flags(directory=not last or directory, nonblocking=not directory)
                try:
                    next_fd = os.open(part, flags, dir_fd=current_fd)
                except FileNotFoundError as error:
                    raise ReferenceAccessError("Reference 目标不存在") from error
                except PermissionError as error:
                    raise ReferenceAccessError("Reference 目标无权访问") from error
                except OSError as error:
                    if error.errno in (errno.ELOOP, errno.ENOTDIR):
                        raise ReferenceAccessError("Reference 路径在打开时发生变化") from error
                    raise ReferenceAccessError("Reference 目标无法打开") from error
                os.close(current_fd)
                current_fd = next_fd
            info = os.fstat(current_fd)
            if directory:
                if not stat.S_ISDIR(info.st_mode):
                    raise ReferenceAccessError("Reference 搜索范围不是目录")
            elif not stat.S_ISREG(info.st_mode):
                raise ReferenceAccessError("Reference 目标不是普通文件")
            return current_fd
        except Exception:
            if current_fd is not None:
                try:
                    os.close(current_fd)
                except OSError:
                    pass
            raise

    def _open_regular_file(
        self,
        definition: ReferenceDefinition,
        resolved: str,
        *,
        relative: str,
        path_fingerprint: tuple[tuple[int, int, int, str | None], ...],
        target_identity: tuple[int, int],
        max_bytes: int | None = None,
    ) -> tuple[int, os.stat_result]:
        """Open a canonical regular file and validate the live file descriptor."""
        read_limit = MAX_FILE_BYTES if max_bytes is None else max_bytes
        if read_limit < 0:
            raise _ReferenceScanLimit()
        if self._supports_dir_fd():
            fd = self._open_canonical_fd(definition, resolved, directory=False)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise ReferenceAccessError("Reference 目标不是普通文件")
                if int(info.st_size) > MAX_FILE_BYTES:
                    raise ReferenceAccessError("Reference 文件超过大小上限")
                if int(info.st_size) > read_limit:
                    raise _ReferenceScanLimit()
                if (int(info.st_dev), int(info.st_ino)) != target_identity:
                    raise ReferenceAccessError("Reference 文件在打开时发生变化")
                self._check_path_fingerprint(definition, relative, path_fingerprint)
                self._check_root_identity(definition)
                return fd, info
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise

        # Platforms without the dir_fd/O_NOFOLLOW combination get a more
        # conservative fallback.  The file is not read until the post-open
        # canonical path and frozen-root identity checks succeed.
        self._check_root_identity(definition)
        try:
            fd = os.open(resolved, self._open_flags(nonblocking=True))
        except FileNotFoundError as error:
            raise ReferenceAccessError("Reference 文件不存在") from error
        except PermissionError as error:
            raise ReferenceAccessError("Reference 文件无权读取") from error
        except OSError as error:
            raise ReferenceAccessError("Reference 文件无法打开") from error
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ReferenceAccessError("Reference 目标不是普通文件")
            if int(info.st_size) > MAX_FILE_BYTES:
                raise ReferenceAccessError("Reference 文件超过大小上限")
            if int(info.st_size) > read_limit:
                raise _ReferenceScanLimit()
            post_resolved = os.path.realpath(resolved)
            self._check_resolved_target(definition, post_resolved, directory=False)
            if (int(info.st_dev), int(info.st_ino)) != target_identity:
                raise ReferenceAccessError("Reference 文件在打开时发生变化")
            self._check_path_fingerprint(definition, relative, path_fingerprint)
            self._check_root_identity(definition)
            return fd, info
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise

    def _read_file(
        self,
        definition: ReferenceDefinition,
        path: str,
        *,
        relative: str,
        path_fingerprint: tuple[tuple[int, int, int, str | None], ...],
        target_identity: tuple[int, int],
        max_bytes: int | None = None,
    ) -> tuple[str, str, bytes]:
        fd, _info = self._open_regular_file(
            definition, path, relative=relative,
            path_fingerprint=path_fingerprint,
            target_identity=target_identity,
            max_bytes=max_bytes,
        )
        try:
            read_limit = MAX_FILE_BYTES if max_bytes is None else max_bytes
            chunks: list[bytes] = []
            total = 0
            while total <= read_limit:
                chunk = os.read(fd, min(64 * 1024, read_limit - total + 1))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > read_limit:
                    raise _ReferenceScanLimit()
            raw = b"".join(chunks)
        except _ReferenceScanLimit:
            raise
        except (OSError, ValueError) as error:
            raise ReferenceAccessError("Reference 文件无法读取") from error
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        self._check_path_fingerprint(definition, relative, path_fingerprint)
        self._check_root_identity(definition)
        if len(raw) > MAX_FILE_BYTES:
            raise ReferenceAccessError("Reference 文件超过大小上限")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ReferenceAccessError("Reference 文件不是有效 UTF-8 文本") from error
        return text, hashlib.sha256(raw).hexdigest(), raw

    @staticmethod
    def _line_text(value: str, maximum: int) -> tuple[str, bool]:
        if len(value) <= maximum:
            return value, False
        return value[:maximum], True

    def read_reference(self, alias: str, path: str, offset: int = 0, limit: int = MAX_READ_LINES) -> dict[str, Any]:
        definition = self._definition(alias)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or offset > MAX_READ_OFFSET:
            raise ReferenceAccessError("offset 超出允许范围")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > MAX_READ_LINES:
            raise ReferenceAccessError("limit 超出允许范围")
        target = self._safe_target(definition, path, directory=False)
        text, digest, _raw = self._read_file(
            definition,
            target.resolved,
            relative=target.relative,
            path_fingerprint=target.path_fingerprint,
            target_identity=target.target_identity,
        )
        raw_lines = text.splitlines()
        selected = raw_lines[offset:offset + limit]
        lines: list[dict[str, Any]] = []
        line_truncated = False
        for index, line in enumerate(selected, offset + 1):
            bounded, truncated = self._line_text(line, MAX_READ_LINE_CHARS)
            line_truncated = line_truncated or truncated
            lines.append({"line": index, "text": bounded})
        truncated = line_truncated or offset + len(selected) < len(raw_lines)
        return {
            "status": "ok",
            "alias": alias,
            "path": target.relative,
            "offset": offset,
            "limit": limit,
            "start_line": offset + 1 if selected else None,
            "end_line": offset + len(selected) if selected else None,
            "returned_lines": len(lines),
            "omitted_lines": max(0, len(raw_lines) - (offset + len(lines))),
            "total_lines": len(raw_lines),
            "sha256": digest,
            "lines": lines,
            "truncated": truncated,
        }

    def _open_directory_for_scan(
        self,
        definition: ReferenceDefinition,
        target: _TargetSnapshot,
    ) -> int:
        resolved = target.resolved
        if self._supports_dir_fd():
            fd = self._open_canonical_fd(definition, resolved, directory=True)
            try:
                self._check_path_fingerprint(
                    definition, target.relative, target.path_fingerprint,
                )
                return fd
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
        self._check_root_identity(definition)
        try:
            fd = os.open(resolved, self._open_flags(directory=True))
        except (FileNotFoundError, PermissionError, OSError) as error:
            raise ReferenceAccessError("Reference 目录无法读取") from error
        try:
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode):
                raise ReferenceAccessError("Reference 搜索范围不是目录")
            post_resolved = os.path.realpath(resolved)
            self._check_resolved_target(definition, post_resolved, directory=True)
            self._check_path_fingerprint(
                definition, target.relative, target.path_fingerprint,
            )
            self._check_root_identity(definition)
            return fd
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise

    def _iter_files(
        self,
        target: _TargetSnapshot,
        definition: ReferenceDefinition,
        include: str = "*",
    ) -> tuple[list[str], bool, int]:
        """Bounded deterministic DFS returning paths, truncation, and visits."""
        files: list[str] = []
        stack: list[tuple[int, str]] = []
        root_fd = self._open_directory_for_scan(definition, target)
        stack.append((root_fd, target.relative))
        visited_entries = 0
        candidate_files = 0
        truncated = False
        try:
            while stack and not truncated:
                current_fd, current_relative = stack.pop()
                try:
                    entries: list[os.DirEntry[str]] = []
                    try:
                        with os.scandir(current_fd) as iterator:
                            for entry in iterator:
                                if visited_entries >= MAX_SEARCH_ENTRIES:
                                    truncated = True
                                    break
                                visited_entries += 1
                                entries.append(entry)
                    except (OSError, ValueError) as error:
                        raise ReferenceAccessError("Reference 目录无法读取") from error
                    entries.sort(key=lambda item: item.name)
                    child_directories: list[tuple[int, str]] = []
                    for entry in entries:
                        if truncated:
                            break
                        name = entry.name
                        relative = name if current_relative == "." else f"{current_relative}/{name}"
                        if name.casefold() == "config_local.py":
                            continue
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                child_path = os.path.join(
                                    definition.root, relative.replace("/", os.sep),
                                )
                                child_resolved = os.path.realpath(child_path)
                                if (
                                    _within(child_resolved, definition.root)
                                    and not self._sensitive(child_resolved)
                                ):
                                    try:
                                        child_fd = os.open(
                                            name,
                                            self._open_flags(directory=True),
                                            dir_fd=current_fd,
                                        )
                                    except OSError as error:
                                        if error.errno in (errno.ELOOP, errno.ENOTDIR):
                                            raise ReferenceAccessError(
                                                "Reference 目录在遍历时发生变化"
                                            ) from error
                                        raise ReferenceAccessError(
                                            "Reference 目录无法读取"
                                        ) from error
                                    child_directories.append((child_fd, relative))
                                continue
                            if entry.is_symlink():
                                # Never follow directory symlinks.  A file
                                # symlink is collected only when its endpoint
                                # is still inside the frozen root.
                                entry_path = os.path.join(
                                    definition.root, relative.replace("/", os.sep),
                                )
                                resolved = os.path.realpath(entry_path)
                                if (
                                    not _within(resolved, definition.root)
                                    or self._sensitive(resolved)
                                    or os.path.basename(resolved).casefold() == "config_local.py"
                                ):
                                    continue
                                if os.path.isdir(resolved):
                                    continue
                                if not os.path.isfile(resolved):
                                    continue
                            elif not entry.is_file(follow_symlinks=False):
                                continue
                            candidate_files += 1
                            if (
                                fnmatch.fnmatchcase(relative, include)
                                or fnmatch.fnmatchcase(name, include)
                            ):
                                files.append(os.path.join(
                                    definition.root, relative.replace("/", os.sep),
                                ))
                            if candidate_files >= MAX_SEARCH_FILES:
                                truncated = True
                                break
                        except OSError as error:
                            raise ReferenceAccessError("Reference 目录项无法检查") from error
                    # Stack order is reversed so traversal remains sorted DFS.
                    for child in reversed(child_directories):
                        if truncated:
                            try:
                                os.close(child[0])
                            except OSError:
                                pass
                        else:
                            stack.append(child)
                finally:
                    try:
                        os.close(current_fd)
                    except OSError:
                        pass
        finally:
            for fd, _relative_path in stack:
                try:
                    os.close(fd)
                except OSError:
                    pass
        files.sort(key=lambda value: _relative(definition.root, value))
        return files, truncated, visited_entries

    def search_reference(
        self,
        alias: str,
        query: str,
        path: str = ".",
        include: str = "*",
        limit: int = 20,
    ) -> dict[str, Any]:
        definition = self._definition(alias)
        if not isinstance(query, str) or not query:
            raise ReferenceAccessError("query 不能为空")
        if len(query) > MAX_QUERY_CHARS:
            raise ReferenceAccessError("query 超过长度上限")
        if not isinstance(include, str) or not include or len(include) > MAX_INCLUDE_CHARS:
            raise ReferenceAccessError("include 超过长度上限")
        if "\x00" in query or "\x00" in include:
            raise ReferenceAccessError("query 或 include 不能包含 NUL")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > MAX_SEARCH_MATCHES:
            raise ReferenceAccessError("limit 超出允许范围")
        search_target = self._safe_target(definition, path, directory=True)
        search_root, relative_root = search_target.resolved, search_target.relative
        files, scan_truncated, visited_entries = self._iter_files(
            search_target, definition, include,
        )
        matches: list[dict[str, Any]] = []
        total_matches = 0
        scanned_files = 0
        scanned_bytes = 0
        query_folded = query.casefold()
        for full in sorted(files, key=lambda value: _relative(definition.root, value)):
            relative = _relative(definition.root, full)
            scanned_files += 1
            target = self._safe_target(definition, relative, directory=False)
            remaining = MAX_SEARCH_BYTES - scanned_bytes
            try:
                text, digest, raw = self._read_file(
                    definition,
                    target.resolved,
                    relative=target.relative,
                    path_fingerprint=target.path_fingerprint,
                    target_identity=target.target_identity,
                    max_bytes=remaining,
                )
            except _ReferenceScanLimit:
                scan_truncated = True
                break
            scanned_bytes += len(raw)
            for line_number, line in enumerate(text.splitlines(), 1):
                if query_folded not in line.casefold():
                    continue
                total_matches += 1
                if len(matches) < limit:
                    fragment, _line_truncated = self._line_text(line, MAX_SEARCH_LINE_CHARS)
                    matches.append({
                        "path": relative,
                        "line": line_number,
                        "text": fragment,
                        "sha256": digest,
                    })
        return {
            "status": "ok",
            "alias": alias,
            "path": relative_root,
            "query": query,
            "include": include,
            "matches": matches,
            "total_matches": total_matches,
            "returned_matches": len(matches),
            "scanned_files": scanned_files,
            "scanned_bytes": scanned_bytes,
            "visited_entries": visited_entries,
            "scan_truncated": scan_truncated,
            "truncated": total_matches > len(matches),
        }


__all__ = [
    "ReferenceCatalog", "ReferenceDefinition", "ReferenceError",
    "ReferenceConfigError", "ReferenceAccessError", "MAX_REFERENCES",
    "MAX_ALIAS_CHARS", "MAX_DESCRIPTION_CHARS", "MAX_REFERENCE_PATH_CHARS",
    "MAX_FILE_BYTES", "MAX_SEARCH_FILES", "MAX_SEARCH_ENTRIES", "MAX_SEARCH_BYTES",
    "MAX_SEARCH_MATCHES", "MAX_SEARCH_LINE_CHARS", "MAX_READ_LINES",
    "MAX_READ_OFFSET", "MAX_READ_LINE_CHARS", "MAX_QUERY_CHARS",
    "MAX_INCLUDE_CHARS",
]
