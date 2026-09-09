"""Context budgeting and trimming before LLM calls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from mini_agent.config import CONTEXT_OBSERVABILITY, CONTEXT_WINDOW, OUTPUT_MODE
from mini_agent.state import AgentState
from mini_agent.tools.base import format_tool_result


Message = dict[str, object]
STRUCTURED_STATE_MAX_CHARS = 6000


@dataclass(frozen=True)
class ContextStats:
    """Token accounting for the exact message view sent to the LLM."""

    tokens: int
    window: int
    input_limit: int
    reserve: int
    system: int
    task: int
    state: int
    history: int
    tool_result: int


@dataclass(frozen=True)
class ContextEvent:
    """Observable context lifecycle event."""

    kind: str
    stats: ContextStats | None
    details: dict[str, object]


Observer = Callable[[ContextEvent], None]


def _serialize_message(message: Message) -> str:
    role = message.get("role", "unknown")
    if role == "tool":
        return "[TOOL_RESULT]\n" + f"id={message.get('tool_call_id', '<unknown>')}\n" + format_tool_result(message.get("content", ""), 1600)
    if role == "assistant" and message.get("tool_calls"):
        calls = []
        for call in message.get("tool_calls", []):
            function = call.get("function", {}) if isinstance(call, dict) else {}
            calls.append(
                f"id={call.get('id', '<unknown>')} name={function.get('name', '<unknown>')} "
                f"arguments={format_tool_result(function.get('arguments', ''), 800)}"
            )
        return "[TOOL_CALL]\n" + "\n".join(calls)
    return f"[{str(role).upper()}]\n{format_tool_result(message.get('content', ''), 1600)}"


def _default_observer(event: ContextEvent) -> None:
    if OUTPUT_MODE == "quiet":
        return
    details = event.details
    if event.kind == "prepared" and event.stats is not None:
        if OUTPUT_MODE != "debug":
            return
        stats = event.stats
        print("[Context]")
        print(f"tokens: {stats.tokens:,} / {stats.window:,}")
        for name in ("system", "task", "state", "history", "tool_result", "reserve"):
            print(f"{name + ':':12}{getattr(stats, name):>10,}")
    elif event.kind == "trimmed":
        if OUTPUT_MODE != "debug":
            return
        action = details.get("action")
        if action == "truncate":
            print("[Context Trim]")
            print(f"truncated tool_call {details.get('tool_call_id', '<unknown>')}")
            print(f"tool_result: -{int(details.get('saved_tokens', 0)):,} tokens")
        elif action == "remove_round":
            print("[Context Trim]")
            print(f"removed turn #{details.get('round', '?')}")
            print(f"tool_result: -{int(details.get('saved_tokens', 0)):,} tokens")
    elif event.kind == "compacted":
        if OUTPUT_MODE != "debug":
            return
        if details.get("failed"):
            print("[Context Compact]")
            print("failed; fallback: trimming")
        else:
            print("[Context Compact]")
            print(
                f"compressed turns: {details.get('start_round')}-{details.get('end_round')}"
            )
            print(f"summary tokens: {int(details.get('summary_tokens', 0)):,}")
            print(
                f"recent turns: {details.get('recent_start_round')}-"
                f"{details.get('recent_end_round')}"
            )


def count_tokens(text_or_messages: object) -> int:
    """Estimate tokens with ``len(text) // 3`` for mixed Chinese and English."""
    if text_or_messages is None:
        return 0
    if isinstance(text_or_messages, str):
        return len(text_or_messages) // 3
    if isinstance(text_or_messages, dict):
        return sum(count_tokens(value) for value in text_or_messages.values())
    if isinstance(text_or_messages, (list, tuple)):
        return sum(count_tokens(value) for value in text_or_messages)
    return len(str(text_or_messages)) // 3


@dataclass(frozen=True)
class ContextBudget:
    """Token budget for one LLM request."""

    window: int = CONTEXT_WINDOW
    output_reserve_ratio: float = 0.15
    history_ratio: float = 0.45

    def __post_init__(self) -> None:
        if self.window <= 0:
            raise ValueError("window 必须大于 0")
        for name, value in (
            ("output_reserve_ratio", self.output_reserve_ratio),
            ("history_ratio", self.history_ratio),
        ):
            if not 0 <= value < 1:
                raise ValueError(f"{name} 必须在 [0, 1) 内")

    @property
    def input_limit(self) -> int:
        return int(self.window * (1 - self.output_reserve_ratio))

    @property
    def history_limit(self) -> int:
        return int(self.window * self.history_ratio)

    def message_limit(self, protected_tokens: int) -> int:
        return max(
            protected_tokens,
            min(self.input_limit, protected_tokens + self.history_limit),
        )


def _is_tool_call_message(message: Message) -> bool:
    return message.get("role") == "assistant" and bool(message.get("tool_calls"))


def _split_rounds(messages: list[Message]) -> tuple[list[Message], list[list[Message]]]:
    """Split messages into protected prefix and atomic history rounds."""
    first_user_index = next(
        (index for index, message in enumerate(messages) if message.get("role") == "user"),
        None,
    )
    if first_user_index is None:
        return list(messages), []

    prefix = list(messages[: first_user_index + 1])
    rounds: list[list[Message]] = []
    index = first_user_index + 1
    while index < len(messages):
        message = messages[index]
        if _is_tool_call_message(message):
            round_messages = [message]
            index += 1
            while index < len(messages) and messages[index].get("role") == "tool":
                round_messages.append(messages[index])
                index += 1
            rounds.append(round_messages)
            continue
        rounds.append([message])
        index += 1
    return prefix, rounds


def _flatten(prefix: list[Message], rounds: list[list[Message]]) -> list[Message]:
    return prefix + [message for round_messages in rounds for message in round_messages]


def _truncate_content(content: object, target_characters: int) -> str | None:
    if not isinstance(content, str) or len(content) <= target_characters:
        return None
    if target_characters < 2:
        return "[tool result omitted]"

    omitted = len(content) - target_characters
    marker = f"\n[... omitted {omitted} characters ...]\n"
    keep = max(2, target_characters - len(marker))
    head = (keep + 1) // 2
    tail = keep // 2
    return content[:head] + marker + content[-tail:]


class TrimPolicy:
    """Trim low-value tool output before deleting complete history rounds."""

    minimum_tool_result_characters = 120

    def trim(
        self,
        messages: list[Message],
        budget: ContextBudget,
        observer: Observer | None = None,
    ) -> list[Message]:
        """Return a protocol-safe, budgeted copy of ``messages``."""
        prepared = [dict(message) for message in messages]
        prefix, rounds = _split_rounds(prepared)
        target = budget.message_limit(count_tokens(prefix))
        before = count_tokens(prepared)
        if before <= target:
            return prepared

        for round_messages in rounds:
            for message in round_messages:
                current_tokens = count_tokens(_flatten(prefix, rounds))
                if current_tokens <= target:
                    break
                if message.get("role") != "tool":
                    continue
                content = message.get("content")
                if not isinstance(content, str) or len(content) <= self.minimum_tool_result_characters:
                    continue
                needed_characters = max(1, (current_tokens - target) * 3)
                target_characters = max(
                    self.minimum_tool_result_characters,
                    len(content) - needed_characters,
                )
                shortened = _truncate_content(content, target_characters)
                if shortened is None:
                    continue
                old_tokens = count_tokens(content)
                message["content"] = shortened
                saved = old_tokens - count_tokens(shortened)
                if observer:
                    observer(ContextEvent("trimmed", None, {
                        "action": "truncate",
                        "tool_call_id": message.get("tool_call_id", "<unknown>"),
                        "saved_tokens": saved,
                    }))

        round_number = 1
        while rounds and count_tokens(_flatten(prefix, rounds)) > target:
            removed = rounds.pop(0)
            if observer:
                observer(ContextEvent("trimmed", None, {
                    "action": "remove_round",
                    "round": round_number,
                    "saved_tokens": count_tokens(removed),
                    "message_count": len(removed),
                }))
            round_number += 1

        prepared = _flatten(prefix, rounds)
        after = count_tokens(prepared)
        return prepared


class ContextManager:
    """Prepare a budgeted LLM context while preserving full local history."""

    def __init__(
        self,
        state: AgentState,
        history: list[Message],
        budget: ContextBudget | None = None,
        trim_policy: TrimPolicy | None = None,
        summarizer: Callable[[list[Message]], str] | None = None,
        keep_rounds: int = 6,
        observability: bool = CONTEXT_OBSERVABILITY,
        observer: Observer | None = None,
        protected_messages: list[Message] | None = None,
    ) -> None:
        self.state = state
        self.history = history
        self.protected_messages = protected_messages
        self.budget = budget or ContextBudget()
        self.trim_policy = trim_policy or TrimPolicy()
        if summarizer is None:
            def summarizer(messages: list[Message]) -> str:
                from mini_agent.agent import summarize_messages
                return summarize_messages(messages)
        self.summarizer = summarizer
        self.keep_rounds = keep_rounds
        self._summary = ""
        self._compacted = False
        self._summarized_rounds = 0
        self.observability = observability
        self.observer = observer or (_default_observer if observability else None)
        self.last_stats: ContextStats | None = None
        self._runtime_notice: str | None = None

    def reset_task(self) -> None:
        """Discard task-local context while preserving protected messages."""
        self.history.clear()
        self._summary = ""
        self._compacted = False
        self._summarized_rounds = 0
        self.last_stats = None
        self._runtime_notice = None

    def set_runtime_notice(self, notice: str | None) -> None:
        self._runtime_notice = notice

    def stats_snapshot(self) -> ContextStats | None:
        return self.last_stats

    def _emit(self, kind: str, details: dict[str, object], stats: ContextStats | None = None) -> None:
        if self.observer is None:
            return
        try:
            self.observer(ContextEvent(kind, stats, details))
        except Exception:
            # Observability is strictly observational and cannot break execution.
            return

    def _stats(self, messages: list[Message]) -> ContextStats:
        first_user = next(
            (index for index, message in enumerate(messages) if message.get("role") == "user"),
            None,
        )
        buckets = {"system": 0, "task": 0, "state": 0, "history": 0, "tool_result": 0}
        for index, message in enumerate(messages):
            amount = count_tokens(message)
            role = message.get("role")
            content = message.get("content")
            if role == "tool":
                buckets["tool_result"] += amount
            elif index == first_user:
                buckets["task"] += amount
            elif role == "system" and isinstance(content, str) and content.startswith("[Structured State]"):
                buckets["state"] += amount
            elif role == "system":
                buckets["system"] += amount
            else:
                buckets["history"] += amount
        return ContextStats(
            tokens=sum(buckets.values()),
            window=self.budget.window,
            input_limit=self.budget.input_limit,
            reserve=self.budget.window - self.budget.input_limit,
            **buckets,
        )

    def _render_state(self) -> Message:
        snapshot = self.state.snapshot()
        def bounded(value: object, limit: int) -> str:
            text = str(value)
            if len(text) <= limit:
                return text
            return text[:limit] + " [... truncated]"

        base_lines = ["[Structured State]"]
        if snapshot["task"]: base_lines.append(f"Task: {bounded(snapshot['task'], 1200)}")
        if snapshot["current_goal"]: base_lines.append(f"Current goal: {bounded(snapshot['current_goal'], 800)}")
        if snapshot["todos"]:
            base_lines.append("Todos: " + bounded("; ".join(
                f"[{todo['status']}] {todo['content']}" for todo in snapshot["todos"]), 1000))
        if snapshot["files_changed"]:
            base_lines.append("Files changed: " + bounded(", ".join(snapshot["files_changed"]), 600))
        base_lines.append(f"Status: {snapshot['status']}; generation: {snapshot.get('current_generation_id', 0)}")

        optional_lines = []
        if snapshot["errors"]:
            optional_lines.append("Recent errors: " + bounded(", ".join(snapshot["errors"][-3:]), 800))
        successful_tools = [item for item in snapshot["tool_history"] if item.get("ok")]
        if successful_tools:
            optional_lines.append(f"Tools executed: {len(snapshot['tool_history'])}")
            optional_lines.append("Recent completed tools (do not repeat): " + bounded("; ".join(
                f"{item['tool']} -> {format_tool_result(item.get('brief', ''), 180)}"
                for item in successful_tools[-3:]), 700))
        if snapshot.get("verification_evidence"):
            optional_lines.append("Verification: " + bounded("; ".join(
                f"{item['command']} => {item['outcome']} ({item['exit_code']}) @g{item.get('generation_id', 0)}"
                for item in snapshot["verification_evidence"]), 800))

        attempts = {item["attempt_id"]: item for item in snapshot.get("attempts", [])}
        failure_lines = []
        for failure in snapshot.get("failures", [])[-3:]:
            attempt = attempts.get(failure.get("caused_by_attempt_id"), {})
            failure_lines.append(
                f"{failure['failure_id']} tool={attempt.get('tool', '<unknown>')} "
                f"attempt={failure.get('caused_by_attempt_id', '<unknown>')} "
                f"generation={failure.get('generation_id')} category={failure.get('category')} "
                f"retryable={str(failure.get('retryable')).lower()}"
            )
        critical_lines = []
        if failure_lines:
            critical_lines.append("Recent failures: " + bounded("; ".join(failure_lines), 1500))
        recovery_lines = []
        for action in snapshot.get("recovery_actions", [])[-3:]:
            recovery_lines.append(
                f"{action['recovery_id']} action={action['action']} status={action['status']} "
                f"failure={action['caused_by_failure_id']} generation={action['generation_id']} "
                f"result_attempt={action.get('result_attempt') or '-'}"
            )
        if recovery_lines:
            critical_lines.append("Recent recovery actions: " + bounded("; ".join(recovery_lines), 1500))
        budgets = snapshot.get("budgets", {})
        critical_lines.append(
            "Budgets: "
            f"failure_retries_remaining={budgets.get('failure_retries_remaining', '?')}; "
            f"recovery_actions_remaining={budgets.get('recovery_actions_remaining', '?')}; "
            f"repair_cycles_remaining={budgets.get('repair_cycles_remaining', '?')}; "
            f"fingerprint_attempts_remaining={bounded(budgets.get('fingerprint_attempts_remaining', []), 900)}"
        )
        if snapshot.get("recovery_notice"):
            critical_lines.append("Recovery notice: " + bounded(snapshot["recovery_notice"], 600))
        if snapshot.get("verification_required"):
            critical_lines.append("Verification required: true")
        if snapshot.get("terminal_reason"):
            critical_lines.append("Blocking reason: " + bounded(snapshot["terminal_reason"], 600))

        content = "\n".join(base_lines + optional_lines + critical_lines)
        if len(content) > STRUCTURED_STATE_MAX_CHARS:
            # Keep the causal and budget block intact; low-priority observation
            # text may be dropped after state is rebuilt from the snapshot.
            content = "\n".join([
                "[Structured State]",
                f"Status: {snapshot['status']}; generation: {snapshot.get('current_generation_id', 0)}",
                *critical_lines,
            ])
        if len(content) > STRUCTURED_STATE_MAX_CHARS:
            # The individual critical fields are already bounded. This final
            # fallback is only for an unusually large number of bounded records;
            # do not cut a failure reference or terminal reason in half.
            content = "\n".join([
                "[Structured State]",
                f"Status: {snapshot['status']}; generation: {snapshot.get('current_generation_id', 0)}",
                *critical_lines,
            ])
        return {"role": "system", "content": content}

    def _build_messages(self) -> list[Message]:
        source = ([dict(message) for message in self.protected_messages] if self.protected_messages is not None else [])
        source.extend(dict(message) for message in self.history)
        if not self._compacted:
            first_user = next((i for i, m in enumerate(source) if m.get("role") == "user"), len(source))
            return source[:first_user] + [self._render_state()] + source[first_user:]
        prefix, rounds = _split_rounds(source)
        recent = rounds[-self.keep_rounds:] if self.keep_rounds else []
        first_user = next(
            (index for index, message in enumerate(prefix) if message.get("role") == "user"),
            len(prefix),
        )
        protected = prefix[:first_user]
        task_prefix = prefix[first_user:]
        messages = protected + [self._render_state()]
        if self._summary:
            messages.append({"role": "system", "content": "[Historical Summary]\n" + self._summary})
        messages.extend(task_prefix)
        messages.extend(message for round_messages in recent for message in round_messages)
        return messages

    def compact(self, keep_rounds: int | None = None) -> bool:
        """Summarize old complete rounds and retain recent raw messages."""
        keep = self.keep_rounds if keep_rounds is None else keep_rounds
        if keep < 0:
            raise ValueError("keep_rounds 必须大于等于 0")
        source = ([dict(message) for message in self.protected_messages] if self.protected_messages is not None else [])
        source.extend(dict(message) for message in self.history)
        prefix, rounds = _split_rounds(source)
        if len(rounds) <= keep:
            return False
        eligible_end = len(rounds) - keep if keep else len(rounds)
        start = min(self._summarized_rounds, eligible_end)
        if eligible_end <= start:
            return False
        old_rounds = rounds[start:eligible_end]
        old_messages = [message for round_messages in old_rounds for message in round_messages]
        if self.summarizer is None:
            return False
        prompt = [{"role": "user", "content": (
            "请总结以下历史消息，严格按任务、已完成步骤、最后一次成功工具调用、"
            "已修改文件、错误、当前进度、下一步组织。禁止虚构事实，"
            "不要重复已经完成的工具调用，也不要把旧命令当作下一步。\n" +
            ("已有摘要：\n" + self._summary + "\n" if self._summary else "") +
            "历史：\n" + "\n\n".join(_serialize_message(message) for message in old_messages)
        )}]
        try:
            summary = self.summarizer(prompt)
            if not isinstance(summary, str) or not summary.strip():
                return False
        except Exception:
            self._emit("compacted", {"failed": True})
            return False
        self._summary = summary.strip()
        self.keep_rounds = keep
        self._compacted = True
        self._summarized_rounds = eligible_end
        self._emit("compacted", {
            "start_round": start + 1,
            "end_round": eligible_end,
            "summary_tokens": count_tokens(self._summary),
            "recent_start_round": eligible_end + 1,
            "recent_end_round": len(rounds),
        })
        return True

    def prepare_messages(self) -> list[Message]:
        """Build the LLM request context without mutating ``history``."""
        # Keep the notice local until the final message view is built.  A
        # compaction rebuilds messages, so consuming it before that rebuild
        # would silently drop the correction reminder.
        notice = self._runtime_notice

        def with_notice(source: list[Message]) -> list[Message]:
            if notice:
                source.insert(0, {"role": "system", "content": "[Runtime Notice]\n" + notice})
            return source

        messages = with_notice(self._build_messages())
        prefix, _ = _split_rounds(messages)
        target = self.budget.message_limit(count_tokens(prefix))
        over_budget = count_tokens(messages) > target
        trim_observer = lambda event: self._emit(event.kind, event.details, event.stats)
        trimmed = self.trim_policy.trim(messages, self.budget, observer=trim_observer)
        if over_budget and self.compact():
            trimmed = self.trim_policy.trim(
                with_notice(self._build_messages()), self.budget, observer=trim_observer
            )
        self.last_stats = self._stats(trimmed)
        self._emit("prepared", {}, self.last_stats)
        # Consume only after the final context was successfully constructed.
        if notice == self._runtime_notice:
            self._runtime_notice = None
        return trimmed
