"""Discover and load project-local AGENTS.md instructions."""

from __future__ import annotations

import os


class InstructionLoader:
    """Load AGENTS.md files applicable to a startup working directory."""

    def __init__(self, cwd: str, max_chars: int = 12000) -> None:
        if max_chars <= 0:
            raise ValueError("max_chars 必须大于 0")
        self.cwd = os.path.abspath(cwd)
        self.max_chars = max_chars

    def _git_root(self) -> str | None:
        path = self.cwd
        while True:
            git_marker = os.path.join(path, ".git")
            if os.path.isdir(git_marker) or os.path.isfile(git_marker):
                return path
            parent = os.path.dirname(path)
            if parent == path:
                return None
            path = parent

    def discover(self) -> list[str]:
        root = self._git_root()
        if root is None:
            candidate = os.path.join(self.cwd, "AGENTS.md")
            return [candidate] if os.path.isfile(candidate) else []

        relative_parts = os.path.relpath(self.cwd, root).split(os.sep)
        if relative_parts == [os.curdir]:
            relative_parts = []
        directories = [root]
        current = root
        for part in relative_parts:
            current = os.path.join(current, part)
            directories.append(current)
        return [
            os.path.join(directory, "AGENTS.md")
            for directory in directories
            if os.path.isfile(os.path.join(directory, "AGENTS.md"))
        ]

    def load(self) -> str:
        chunks: list[str] = []
        used = 0
        for path in self.discover():
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    content = handle.read()
            except OSError:
                content = "[读取失败，已跳过]"

            chunk = f"Source: {path}\n{content}"
            separator = "\n\n" if chunks else ""
            available = self.max_chars - used - len(separator)
            if available <= 0:
                break
            if len(chunk) > available:
                marker = f"\n[指令已截断，最多保留 {self.max_chars} 个字符]"
                keep = max(0, available - len(marker))
                chunk = chunk[:keep] + marker
                chunks.append(separator + chunk)
                break
            chunks.append(separator + chunk)
            used += len(separator) + len(chunk)
        return "".join(chunks)
