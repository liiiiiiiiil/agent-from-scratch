"""Mutable execution state kept separately from LLM messages."""

from copy import deepcopy
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Literal


@dataclass(frozen=True)
class TodoItem:
    content: str
    status: Literal["pending", "in_progress", "completed"] = "pending"


@dataclass
class AgentState:
    task: str = ""
    current_goal: str = ""
    tool_history: list[dict] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    status: str = "running"
    todos: list[TodoItem] = field(default_factory=list)
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
        # Todo is task intent, not an execution fact. Keep it out of the
        # executor-owned history and error/file tracking.
        if name == "update_todo":
            return
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

    def update_todos(self, todos: list[dict[str, Any]]) -> None:
        """Validate and atomically replace the model-maintained todo list."""
        if not isinstance(todos, list):
            raise ValueError("todos 必须是数组")
        if len(todos) > 50:
            raise ValueError("Todo 数量不能超过 50")
        parsed: list[TodoItem] = []
        in_progress = 0
        for item in todos:
            if not isinstance(item, dict):
                raise ValueError("Todo 项必须是对象")
            content = item.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("Todo content 必须是非空字符串")
            content = content.strip()
            if len(content) > 240:
                raise ValueError("Todo content 不能超过 240 个字符")
            status = item.get("status", "pending")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError("Todo status 非法")
            if status == "in_progress":
                in_progress += 1
            parsed.append(TodoItem(content, status))
        if in_progress > 1:
            raise ValueError("最多只能有一个 in_progress Todo")
        with self._lock:
            self.todos = parsed
            self.current_goal = next((t.content for t in parsed if t.status == "in_progress"), "")

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
                "todos": [
                    {"content": todo.content, "status": todo.status}
                    for todo in self.todos
                ],
            }
            return snapshot
