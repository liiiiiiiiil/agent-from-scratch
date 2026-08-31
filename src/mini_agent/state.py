"""Mutable execution state kept separately from LLM messages."""

from copy import deepcopy
from dataclasses import dataclass, field
import re
from threading import Lock
from typing import Any, Literal


@dataclass(frozen=True)
class TodoItem:
    content: str
    status: Literal["pending", "in_progress", "completed"] = "pending"

@dataclass(frozen=True)
class VerificationEvidence:
    command: str
    outcome: str
    exit_code: int | None
    output: str

    @property
    def result(self) -> str:
        """Compatibility alias for the captured (truncated) command result."""
        return self.output


@dataclass
class AgentState:
    task: str = ""
    current_goal: str = ""
    tool_history: list[dict] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    status: str = "running"
    todos: list[TodoItem] = field(default_factory=list)
    verification_evidence: list[VerificationEvidence] = field(default_factory=list)
    _verification_generation: int = field(default=0, init=False, repr=False)
    _last_verified_generation: int = field(default=-1, init=False, repr=False)
    _verification_required: bool = field(default=False, init=False, repr=False)
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
                self._invalidate_verification()

            # An execution shell is potentially mutating even when its
            # command looks read-only or exits non-zero.  Permission
            # rejection means the handler never ran and therefore cannot
            # invalidate evidence.
            if name == "run_shell" and args_copy.get("purpose", "execution") == "execution":
                if "权限拒绝" not in brief:
                    self._invalidate_verification()

            if name == "run_shell" and args_copy.get("purpose", "execution") == "verification":
                text = str(brief)
                timeout = "[timeout]" in text or "超时" in text
                match = re.search(r"\[exit=(-?\d+)\]", text)
                code = int(match.group(1)) if match else (0 if ok and not timeout else None)
                passed = bool(ok and not timeout and code == 0)
                evidence = VerificationEvidence(
                    command=str(args_copy.get("command", "")),
                    outcome="passed" if passed else "failed",
                    exit_code=code,
                    output=text,
                )
                self.verification_evidence.append(evidence)
                if passed:
                    self._last_verified_generation = self._verification_generation
                    self._verification_required = False
                else:
                    self._last_verified_generation = -1
                    self._verification_required = True

            if not ok:
                self.errors.append(f"{name}: {brief}")

    def begin_task(self, task: str) -> None:
        with self._lock:
            self.task = task
            self.current_goal = ""
            self.tool_history.clear(); self.files_changed.clear(); self.errors.clear()
            self.todos.clear(); self.verification_evidence.clear()
            self._verification_generation += 1
            self._last_verified_generation = -1
            self._verification_required = False
            self.status = "running"

    def _invalidate_verification(self) -> None:
        """Invalidate evidence after an operation that may change the environment."""
        self._verification_generation += 1
        self.verification_evidence.clear()
        self._last_verified_generation = -1
        self._verification_required = True

    def unfinished_todos(self) -> list[dict[str, str]]:
        with self._lock:
            return [{"content": t.content, "status": t.status} for t in self.todos if t.status != "completed"]

    def has_verification_evidence(self) -> bool:
        with self._lock:
            return bool(self.verification_evidence) and self._last_verified_generation == self._verification_generation and self.verification_evidence[-1].outcome == "passed"

    def completion_reminder(self) -> dict[str, object] | None:
        with self._lock:
            missing = [t.content for t in self.todos if t.status != "completed"]
            needs_verify = self._verification_required
            if not missing and not needs_verify:
                return None
            return {"unfinished_todos": missing, "verification_required": needs_verify, "message": "任务尚未满足完成条件，请继续执行并验证。"}

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
                "verification_evidence": [
                    {"command": e.command, "outcome": e.outcome, "exit_code": e.exit_code, "output": e.output}
                    for e in self.verification_evidence
                ],
                "verification_required": self._verification_required,
            }
            return snapshot
