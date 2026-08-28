"""Mutable execution state kept separately from LLM messages."""

from copy import deepcopy
from dataclasses import dataclass, field
from threading import Lock
from typing import Any


@dataclass
class AgentState:
    task: str = ""
    current_goal: str = ""
    tool_history: list[dict] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    status: str = "running"
    _lock: Any = field(
        default_factory=Lock,
        init=False,
        repr=False,
        compare=False,
    )

    def record_tool(
        self,
        name: str,
        args: dict[str, Any],
        ok: bool,
        brief: str,
    ) -> None:
        """Record one tool result and update the derived execution state."""
        args_copy: dict[str, Any] = deepcopy(args)
        record: dict[str, Any] = {
            "tool": name,
            "args": args_copy,
            "ok": ok,
            "brief": brief,
        }

        with self._lock:
            self.tool_history.append(record)

            if ok and name in ("write_file", "edit_file"):
                path: Any = args_copy.get("path")
                if isinstance(path, str) and path not in self.files_changed:
                    self.files_changed.append(path)

            if not ok:
                self.errors.append(f"{name}: {brief}")

    def snapshot(self) -> dict[str, Any]:
        """Return an independent, consistent view of the public state.

        Callers should use this method rather than reading the public lists
        while tool calls may still be updating the state.
        """
        with self._lock:
            snapshot: dict[str, Any] = {
                "task": self.task,
                "current_goal": self.current_goal,
                "tool_history": deepcopy(self.tool_history),
                "files_changed": deepcopy(self.files_changed),
                "errors": deepcopy(self.errors),
                "status": self.status,
            }
            return snapshot
