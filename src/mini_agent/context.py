"""Context budgeting and trimming before LLM calls."""

from __future__ import annotations

from dataclasses import dataclass

from mini_agent.config import CONTEXT_WINDOW
from mini_agent.state import AgentState


Message = dict[str, object]


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

    def trim(self, messages: list[Message], budget: ContextBudget) -> list[Message]:
        """Return a protocol-safe, budgeted copy of ``messages``."""
        prepared = [dict(message) for message in messages]
        prefix, rounds = _split_rounds(prepared)
        target = budget.message_limit(count_tokens(prefix))
        before = count_tokens(prepared)
        if before <= target:
            return prepared

        print(f"[Context] token budget exceeded: {before}/{target}")
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
                print(
                    "[Context] truncated tool result "
                    f"{message.get('tool_call_id', '<unknown>')}, saved {saved} tokens"
                )

        while rounds and count_tokens(_flatten(prefix, rounds)) > target:
            removed = rounds.pop(0)
            print(f"[Context] removed oldest round, saved {count_tokens(removed)} tokens")

        prepared = _flatten(prefix, rounds)
        after = count_tokens(prepared)
        if after > target:
            print(f"[Context] protected system/task messages exceed budget: {after}/{target}")
        else:
            print(f"[Context] prepared context: {after}/{target} tokens")
        return prepared


class ContextManager:
    """Prepare a budgeted LLM context while preserving full local history."""

    def __init__(
        self,
        state: AgentState,
        history: list[Message],
        budget: ContextBudget | None = None,
        trim_policy: TrimPolicy | None = None,
    ) -> None:
        self.state = state
        self.history = history
        self.budget = budget or ContextBudget()
        self.trim_policy = trim_policy or TrimPolicy()

    def prepare_messages(self) -> list[Message]:
        """Build the LLM request context without mutating ``history``."""
        return self.trim_policy.trim(self.history, self.budget)
