"""Context budgeting and trimming before LLM calls."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Callable

from mini_agent.config import CONTEXT_OBSERVABILITY, CONTEXT_WINDOW, OUTPUT_MODE
from mini_agent.providers.base import UsageMeter
from mini_agent.state import AgentState
from mini_agent.tools.base import format_tool_result


Message = dict[str, object]
STRUCTURED_STATE_MAX_CHARS = 6000
_REDACTED_WRITE_INPUT = "<redacted:write_process.input>"


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
    output_reserve_tokens: int | None = None

    def __post_init__(self) -> None:
        if isinstance(self.window, bool) or not isinstance(self.window, int) or self.window <= 0:
            raise ValueError("window 必须大于 0")
        for name, value in (
            ("output_reserve_ratio", self.output_reserve_ratio),
            ("history_ratio", self.history_ratio),
        ):
            if not 0 <= value < 1:
                raise ValueError(f"{name} 必须在 [0, 1) 内")
        if self.output_reserve_tokens is not None:
            if (isinstance(self.output_reserve_tokens, bool)
                    or not isinstance(self.output_reserve_tokens, int)
                    or not 0 <= self.output_reserve_tokens < self.window):
                raise ValueError("output_reserve_tokens 必须是小于 window 的非负整数")

    @property
    def input_limit(self) -> int:
        if self.output_reserve_tokens is not None:
            return self.window - self.output_reserve_tokens
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

    @staticmethod
    def count_tokens(value: object) -> int:
        """Expose the conservative estimator to child runtimes."""
        return count_tokens(value)

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
        model_binding: object | None = None,
        usage_meter: UsageMeter | None = None,
    ) -> None:
        self.state = state
        self.history = history
        self.protected_messages = protected_messages
        self.model_binding = model_binding
        self.usage_meter = usage_meter or getattr(model_binding, "usage_meter", None)
        self.budget = budget or ContextBudget()
        self.trim_policy = trim_policy or TrimPolicy()
        if summarizer is None:
            if model_binding is not None:
                def summarizer(messages: list[Message]) -> str:
                    response = model_binding.complete(
                        messages, include_tools=False, stream_output=False,
                    )
                    return response.message.get("content", "") or ""
            else:
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

    def export_session(self, *, allow_partial: bool = False) -> dict[str, object]:
        """Export task history and compaction state, excluding protected prompts.

        A durable tool boundary may contain only the final assistant tool-call
        message and an ordered prefix of its results.  Ordinary session saves
        keep the original complete-round requirement.
        """
        history = json.loads(json.dumps(deepcopy(self.history), ensure_ascii=False))
        if not isinstance(history, list):
            raise ValueError("Context history 必须是列表")
        write_inputs: list[str] = []
        for message in self.history:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            for call in message.get("tool_calls", []) or []:
                function = call.get("function", {}) if isinstance(call, dict) else {}
                if not isinstance(function, dict) or function.get("name") != "write_process":
                    continue
                raw_arguments = function.get("arguments")
                try:
                    arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
                except (TypeError, json.JSONDecodeError):
                    arguments = {}
                if isinstance(arguments, dict) and isinstance(arguments.get("input"), str):
                    write_inputs.append(arguments["input"])
        for message in history:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            for call in message.get("tool_calls", []) or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                if not isinstance(function, dict) or function.get("name") != "write_process":
                    continue
                raw_arguments = function.get("arguments")
                if isinstance(raw_arguments, str):
                    try:
                        arguments = json.loads(raw_arguments)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if isinstance(arguments, dict) and "input" in arguments:
                        arguments["input"] = _REDACTED_WRITE_INPUT
                        function["arguments"] = json.dumps(
                            arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                        )
                elif isinstance(raw_arguments, dict) and "input" in raw_arguments:
                    raw_arguments["input"] = _REDACTED_WRITE_INPUT
        # The tool argument is the only field whose provenance is known.  A
        # blind replace would corrupt unrelated text (and replace("", ...) would
        # expand every string).  If the same bytes also occur in ordinary
        # history or the summary, fail closed instead of silently changing it.
        probe_history = deepcopy(history)
        for message in probe_history:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            for call in message.get("tool_calls", []) or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                if isinstance(function, dict) and function.get("name") == "write_process":
                    raw_arguments = function.get("arguments")
                    if isinstance(raw_arguments, str):
                        try:
                            raw_arguments = json.loads(raw_arguments)
                        except json.JSONDecodeError:
                            continue
                    if isinstance(raw_arguments, dict):
                        raw_arguments.pop("input", None)
                        function["arguments"] = raw_arguments

        def contains_input(value: object, input_text: str) -> bool:
            if isinstance(value, str):
                return input_text in value
            if isinstance(value, dict):
                return any(contains_input(item, input_text) for item in value.values())
            if isinstance(value, list):
                return any(contains_input(item, input_text) for item in value)
            return False

        summary = self._summary
        runtime_notice = self._runtime_notice
        for input_text in set(write_inputs):
            if input_text and (contains_input(probe_history, input_text)
                               or input_text in summary
                               or (runtime_notice is not None and input_text in runtime_notice)):
                raise ValueError("write_process.input 出现在其他会话文本中，拒绝保存")
        payload = {
            "format": "mini_agent.context",
            "format_version": 1,
            "history": history,
            "summary": summary,
            "compacted": self._compacted,
            "summarized_rounds": self._summarized_rounds,
            "runtime_notice": runtime_notice,
        }
        binding_ref = getattr(getattr(self, "model_binding", None), "reference", None)
        if binding_ref is not None:
            payload["model_binding_ref"] = binding_ref.to_dict()
        normalized = json.loads(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        self.validate_session_export(normalized, allow_partial=allow_partial)
        return normalized

    def export_tool_boundary(self) -> dict[str, object]:
        """Export a boundary snapshot while a final tool round is incomplete."""
        return self.export_session(allow_partial=True)

    @staticmethod
    def validate_session_export(payload: object, *, allow_partial: bool = False) -> None:
        """Check ordered assistant tool-call/result pairing in an export."""
        if not isinstance(payload, dict) or payload.get("format") != "mini_agent.context" or payload.get("format_version") != 1:
            raise ValueError("未知或不支持的 Context 导出版本")
        history = payload.get("history")
        if not isinstance(history, list):
            raise ValueError("Context history 必须是列表")
        if not isinstance(payload.get("summary"), str) or not isinstance(payload.get("compacted"), bool):
            raise ValueError("Context 摘要字段类型无效")
        if not isinstance(payload.get("summarized_rounds"), int) or payload["summarized_rounds"] < 0:
            raise ValueError("Context summarized_rounds 无效")
        notice = payload.get("runtime_notice")
        if notice is not None and not isinstance(notice, str):
            raise ValueError("Context runtime_notice 类型无效")
        binding_ref = payload.get("model_binding_ref")
        if binding_ref is not None:
            from mini_agent.providers.catalog import ModelBindingRef
            if not isinstance(binding_ref, dict) or set(binding_ref) != {
                "profile", "provider", "protocol", "fingerprint",
            }:
                raise ValueError("Context model_binding_ref 结构无效")
            try:
                ModelBindingRef(**binding_ref)
            except (TypeError, ValueError) as error:
                raise ValueError("Context model_binding_ref 无效") from error
        expected: list[str] = []
        partial_assistant = False
        seen_call_ids: set[str] = set()
        for message in history:
            if not isinstance(message, dict) or not isinstance(message.get("role"), str):
                raise ValueError("Context history 含无效消息")
            if expected:
                if message.get("role") != "tool" or message.get("tool_call_id") != expected[0]:
                    raise ValueError("assistant tool call 缺少按序对应的 tool 结果")
                expected.pop(0)
                continue
            role = message["role"]
            if role == "tool":
                raise ValueError("孤立的 role=tool 结果")
            calls = message.get("tool_calls")
            if role != "assistant" or not calls:
                continue
            if not isinstance(calls, list):
                raise ValueError("assistant tool_calls 形状无效")
            for call in calls:
                if not isinstance(call, dict) or not isinstance(call.get("id"), str) or not call["id"]:
                    raise ValueError("tool_call_id 无效")
                if call["id"] in seen_call_ids:
                    raise ValueError("tool_call_id 重复")
                seen_call_ids.add(call["id"])
                function = call.get("function")
                if not isinstance(function, dict) or not isinstance(function.get("name"), str):
                    raise ValueError("tool call function 无效")
                raw_arguments = function.get("arguments")
                if isinstance(raw_arguments, str):
                    try:
                        arguments = json.loads(raw_arguments)
                    except (TypeError, json.JSONDecodeError) as error:
                        raise ValueError("tool call arguments 不是合法 JSON") from error
                    if not isinstance(arguments, dict):
                        raise ValueError("tool call arguments 必须是 JSON object")
                elif not isinstance(raw_arguments, dict):
                    raise ValueError("tool call arguments 类型无效")
                expected.append(call["id"])
            if expected and message is history[-1]:
                partial_assistant = True
        if expected and history and isinstance(history[-1], dict) and history[-1].get("role") == "tool":
            partial_assistant = True
        if expected and not (allow_partial and partial_assistant):
            raise ValueError("assistant tool call 结果未完整回灌")

    @classmethod
    def restore_session(
        cls,
        state: AgentState,
        payload: object,
        *,
        budget: ContextBudget | None = None,
        trim_policy: TrimPolicy | None = None,
        summarizer: Callable[[list[Message]], str] | None = None,
        keep_rounds: int = 6,
        observability: bool = CONTEXT_OBSERVABILITY,
        observer: Observer | None = None,
        protected_messages: list[Message] | None = None,
        model_binding: object | None = None,
        usage_meter: UsageMeter | None = None,
    ) -> "ContextManager":
        """Rebuild history and compaction state without restoring prompts."""
        cls.validate_session_export(payload)
        if not isinstance(payload, dict):
            raise ValueError("Context 导出必须是 JSON object")
        history = deepcopy(payload["history"])
        # Keep protocol order exactly as persisted.  Validation above rejects
        # both orphan tool results and assistant calls without all results.
        context = cls(
            state, history, budget=budget, trim_policy=trim_policy,
            summarizer=summarizer, keep_rounds=keep_rounds,
            observability=observability, observer=observer,
            protected_messages=protected_messages, model_binding=model_binding,
            usage_meter=usage_meter,
        )
        context._summary = payload["summary"]
        context._compacted = payload["compacted"]
        context._summarized_rounds = payload["summarized_rounds"]
        context._runtime_notice = payload.get("runtime_notice")
        return context

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

    def append_assistant(self, message: Message) -> None:
        """Append one provider-normalized assistant protocol message."""
        self.history.append(message)

    def append_tool_result(self, tool_call_id: str, content: str) -> None:
        """Append one tool result without applying runtime policy."""
        self.history.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
        })

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
        if snapshot.get("task_id"): base_lines.append(f"Task ID: {bounded(snapshot['task_id'], 120)}")
        if snapshot["current_goal"]: base_lines.append(f"Current goal: {bounded(snapshot['current_goal'], 800)}")
        planning_state = snapshot.get("planning_state", {})
        active_plan = snapshot.get("active_plan")
        if active_plan:
            plan_lines: list[str] = [
                "Plan: "
                f"mode={planning_state.get('mode', 'auto')}; "
                f"phase={planning_state.get('phase', 'direct')}; "
                f"active_revision={planning_state.get('active_revision_id') or '-'}"
            ]
        else:
            # Keep Direct Path visible in normal windows without making the
            # protected state disproportionately expensive for tiny test or
            # emergency windows whose existing fallback must retain history.
            plan_lines = [] if self.budget.window < 512 and planning_state.get("phase") == "direct" else [
                f"Plan: {planning_state.get('mode', 'auto')}/"
                f"{planning_state.get('phase', 'direct')}/-"
            ]
        if active_plan:
            plan_lines.append("Plan goal: " + bounded(active_plan.get("goal", ""), 1200))
            plan_lines.append(
                "Plan success criteria: " + bounded(
                    "; ".join(active_plan.get("success_criteria", [])), 900,
                )
            )
            constraints = active_plan.get("constraints", [])
            if constraints:
                plan_lines.append("Plan constraints: " + bounded("; ".join(constraints), 700))
            active_steps = active_plan.get("steps", [])
            current_steps = [step for step in active_steps if step.get("status") == "in_progress"]
            if current_steps:
                step = current_steps[0]
                plan_lines.append(
                    "Current plan step: "
                    + bounded(
                        f"{step.get('step_id', '?')} | {step.get('content', '')}; "
                        f"depends_on={step.get('depends_on', [])}; "
                        f"success_criteria={step.get('success_criteria', [])}",
                        1500,
                    )
                )
            step_by_id = {step.get("step_id"): step for step in active_steps}
            ready = [
                step.get("step_id", "?") for step in active_steps
                if step.get("status") == "pending"
                and all(step_by_id.get(dependency, {}).get("status") == "completed"
                        for dependency in step.get("depends_on", []))
            ]
            blocked = [
                f"{step.get('step_id', '?')}<-" + ",".join(
                    dependency for dependency in step.get("depends_on", [])
                    if step_by_id.get(dependency, {}).get("status") != "completed"
                )
                for step in active_steps
                if step.get("status") == "pending"
                and any(step_by_id.get(dependency, {}).get("status") != "completed"
                        for dependency in step.get("depends_on", []))
            ]
            completed_count = sum(step.get("status") == "completed" for step in active_steps)
            pending_count = sum(step.get("status") == "pending" for step in active_steps)
            in_progress_count = sum(step.get("status") == "in_progress" for step in active_steps)
            omitted_count = max(0, len(active_steps) - len(ready[:5]) - len(blocked[:10])
                                - completed_count - in_progress_count)
            plan_lines.append("Plan ready steps (max 5): " + bounded(", ".join(ready[:5]) or "none", 500))
            plan_lines.append(
                "Plan blocked steps (max 10): " + bounded("; ".join(blocked[:10]) or "none", 900)
            )
            plan_lines.append(
                f"Plan counts: completed={completed_count}; pending={pending_count}; "
                f"in_progress={in_progress_count}; omitted={omitted_count}"
            )
        base_lines.extend(plan_lines)
        crash_items = [item for item in snapshot.get("crash_issues", [])
                       if isinstance(item, dict)]
        unresolved_crash = [item for item in crash_items
                            if item.get("status") in ("unresolved", "investigating")]
        if crash_items:
            base_lines.append(
                "Crash recovery issues (protected facts): " + bounded("; ".join(
                    f"{item.get('issue_id', '?')} tool={item.get('tool', '?')} "
                    f"class={item.get('classification', '?')} "
                    f"admitted={str(bool(item.get('handler_admitted'))).lower()} "
                    f"status={item.get('status', '?')}"
                    for item in crash_items[:32]
                ), 2200)
            )
            if unresolved_crash:
                base_lines.append(
                    "Crash recovery next action: read-only investigation; then "
                    f"/resolve {unresolved_crash[0].get('issue_id', '?')} investigate|continue|block"
                )
        decisions = snapshot.get("user_plan_decisions", [])
        if decisions:
            latest = decisions[-1]
            base_lines.append(
                "User plan decision: "
                f"{latest.get('decision')} revision={latest.get('revision_id')}; "
                f"feedback={bounded(latest.get('feedback') or '-', 600)}"
            )
        if planning_state.get("active_trigger_id") is not None:
            active_trigger = next(
                (item for item in snapshot.get("replan_triggers", [])
                 if item.get("trigger_id") == planning_state["active_trigger_id"]),
                None,
            )
            if active_trigger:
                source = (
                    active_trigger.get("caused_by_failure_id")
                    or active_trigger.get("caused_by_attempt_id")
                    or active_trigger.get("caused_by_decision_id")
                    or "-"
                )
                base_lines.append(
                    "Active plan trigger: "
                    f"{planning_state['active_trigger_id']} "
                    f"kind={active_trigger.get('kind', '?')}; source={source}; "
                    f"reason={bounded(active_trigger.get('reason') or '-', 600)}"
                )
            else:
                base_lines.append(f"Active plan trigger: {planning_state['active_trigger_id']}")
        # Keep the established emergency view for very small windows so the
        # State message does not consume all room reserved for protocol history.
        if self.budget.window >= 512:
            base_lines.append(
                "Replan budget: "
                f"used={planning_state.get('replans_used', 0)}; "
                f"remaining={planning_state.get('replans_remaining', '?')}; "
                f"trigger_no_progress={planning_state.get('trigger_no_progress_commits', 0)}"
            )
            stagnation = snapshot.get("loop_stagnation", snapshot.get("stagnation", {}))
            base_lines.append(
                "Stagnation: "
                f"epoch={stagnation.get('progress_epoch', 0)}; "
                f"consecutive_no_progress={stagnation.get('consecutive_no_progress_rounds', 0)}; "
                f"warning={stagnation.get('warning_kind') or '-'}; "
                f"last={str(stagnation.get('last_round_fingerprint') or '-')[:12]}; "
                f"reason={bounded(stagnation.get('last_reason') or '-', 300)}"
            )
            base_lines.append(
                "Allowed next action: "
                + str(snapshot.get("allowed_next_action") or "按当前 Planning / Repair gate 执行")
            )
        if snapshot["files_changed"]:
            base_lines.append("Files changed: " + bounded(", ".join(snapshot["files_changed"]), 600))
        base_lines.append(f"Status: {snapshot['status']}; generation: {snapshot.get('current_generation_id', 0)}")
        process_records = snapshot.get("processes", [])
        if process_records:
            base_lines.append("Background processes:")
            for process in process_records[-8:]:
                if not isinstance(process, dict):
                    continue
                base_lines.append(
                    f"  {process.get('process_id', '?')} status={process.get('status', '?')} "
                    f"pid={process.get('pid', '?')} "
                    f"stdout_offset={process.get('stdout_offset', 0)} "
                    f"stderr_offset={process.get('stderr_offset', 0)} "
                    f"stdin={process.get('stdin_mode', 'closed')}/"
                    f"{process.get('stdin_state', 'disabled')} "
                    f"write_pending={str(bool(process.get('write_pending', False))).lower()} "
                    f"command={bounded(process.get('command_summary', ''), 240)}"
                )
        if snapshot.get("awaiting_process"):
            waiting = snapshot["awaiting_process"]
            base_lines.append(
                "Awaiting process: "
                f"ids={bounded(waiting.get('process_ids', []), 300)}; "
                f"reason={bounded(waiting.get('reason', ''), 120)}"
            )
        repair_loop = snapshot.get("repair_loop", {})
        if repair_loop and repair_loop.get("phase") != "idle":
            base_lines.append(
                "Repair loop: "
                f"phase={repair_loop.get('phase', '?')}; "
                f"active_failure={repair_loop.get('active_failure_id') or '-'}; "
                f"active_recovery={repair_loop.get('active_recovery_id') or '-'}; "
                f"cycles={repair_loop.get('cycles_used', '?')}/{repair_loop.get('cycles_used', 0) + repair_loop.get('cycles_remaining', 0)}; "
                f"next={repair_loop.get('required_next_action', '?')}"
            )

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
        checkpoint_records = snapshot.get("checkpoints", [])
        checkpoint_lines = []
        for checkpoint in checkpoint_records:
            if not isinstance(checkpoint, dict):
                continue
            before_hash = checkpoint.get("before_sha256") or "-"
            after_hash = checkpoint.get("after_sha256") or "-"
            line = (
                f"{checkpoint.get('checkpoint_id', '<unknown>')} "
                f"path={checkpoint.get('path', '<unknown>')} "
                f"attempt={checkpoint.get('attempt_id', '<unknown>')} "
                f"generation={checkpoint.get('generation_id', '?')} "
                f"status={checkpoint.get('status', '<unknown>')} "
                f"before_sha256={before_hash} after_sha256={after_hash}"
            )
            if checkpoint.get("unavailable_reason"):
                line += f" reason={bounded(checkpoint['unavailable_reason'], 180)}"
            checkpoint_lines.append(line)
        checkpoint_state_line = None
        rollback_state_line = None
        if checkpoint_lines:
            checkpoint_state_line = "Checkpoints: " + bounded("; ".join(checkpoint_lines), 2200)
            critical_lines.append(checkpoint_state_line)
            ready_ids = [
                item.get("checkpoint_id", "<unknown>")
                for item in snapshot.get("rollback_checkpoints", [])
                if isinstance(item, dict)
            ]
            rollback_state_line = (
                "Rollback checkpoints (ready): " +
                bounded(", ".join(ready_ids) if ready_ids else "none", 500)
            )
            critical_lines.append(rollback_state_line)
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
            compact_lines = [
                "[Structured State]",
                f"Task ID: {bounded(snapshot.get('task_id') or '-', 120)}",
                f"Status: {snapshot['status']}; generation: {snapshot.get('current_generation_id', 0)}",
            ]
            if process_records:
                compact_lines.append(
                    "Background processes: " + bounded("; ".join(
                        f"{item.get('process_id', '?')}={item.get('status', '?')}"
                        f"/pid:{item.get('pid', '?')}"
                        f"/stdin:{item.get('stdin_mode', 'closed')}/{item.get('stdin_state', 'disabled')}"
                        f"/pending:{str(bool(item.get('write_pending', False))).lower()}"
                        f"/out:{item.get('stdout_offset', 0)},{item.get('stderr_offset', 0)}"
                        for item in process_records[-8:] if isinstance(item, dict)
                    ), 1200)
                )
            if snapshot.get("awaiting_process"):
                compact_lines.append(
                    "Awaiting process: " + bounded(
                        str(snapshot["awaiting_process"]), 500,
                    )
                )
            compact_lines.extend(bounded(line, 1500) for line in plan_lines)
            if crash_items:
                compact_lines.append("Crash recovery: " + bounded("; ".join(
                    f"{item.get('issue_id', '?')}={item.get('classification', '?')}/{item.get('status', '?')}"
                    for item in crash_items[:16]
                ), 1200))
            active_trigger = next(
                (item for item in snapshot.get("replan_triggers", [])
                 if item.get("trigger_id") == planning_state.get("active_trigger_id")),
                None,
            )
            if active_trigger is not None:
                compact_lines.append(
                    "Active plan trigger: "
                    f"{planning_state.get('active_trigger_id')} "
                    f"kind={active_trigger.get('kind')}; source="
                    f"{active_trigger.get('caused_by_failure_id') or active_trigger.get('caused_by_attempt_id') or active_trigger.get('caused_by_decision_id') or active_trigger.get('caused_by_crash_recovery_id') or '-'}; "
                    f"reason={bounded(active_trigger.get('reason') or '-', 450)}"
                )
            compact_lines.append(
                "Replan budget: "
                f"used={planning_state.get('replans_used', 0)}; "
                f"remaining={planning_state.get('replans_remaining', '?')}; "
                f"trigger_no_progress={planning_state.get('trigger_no_progress_commits', 0)}"
            )
            compact_lines.append(
                "Stagnation: " + bounded(str(snapshot.get("loop_stagnation", {})), 500)
            )
            compact_lines.append(
                "Allowed next action: "
                + bounded(str(snapshot.get("allowed_next_action") or "-"), 700)
            )
            # Checkpoint metadata is part of the causal recovery state. Keep
            # it ahead of lower-priority history when the state is degraded.
            for line, limit in (
                (checkpoint_state_line, 2100),
                (rollback_state_line, 450),
                ("Recent failures: " + bounded("; ".join(failure_lines), 850) if failure_lines else None, 850),
                ("Recent recovery actions: " + bounded("; ".join(recovery_lines), 700) if recovery_lines else None, 700),
                ("Budgets: " +
                 f"failure_retries_remaining={budgets.get('failure_retries_remaining', '?')}; "
                 f"recovery_actions_remaining={budgets.get('recovery_actions_remaining', '?')}; "
                 f"repair_cycles_remaining={budgets.get('repair_cycles_remaining', '?')}; "
                 f"fingerprint_attempts_remaining={bounded(budgets.get('fingerprint_attempts_remaining', []), 600)}", 650),
                ("Recovery notice: " + bounded(snapshot["recovery_notice"], 350)
                 if snapshot.get("recovery_notice") else None, 350),
                ("Verification required: true" if snapshot.get("verification_required") else None, 100),
                ("Repair loop: " + bounded(str(repair_loop), 600)
                 if repair_loop and repair_loop.get("phase") != "idle" else None, 600),
                ("Blocking reason: " + bounded(snapshot["terminal_reason"], 350)
                 if snapshot.get("terminal_reason") else None, 350),
            ):
                if line is not None:
                    compact_lines.append(bounded(line, limit))
            content = "\n".join(compact_lines)
        if len(content) > STRUCTURED_STATE_MAX_CHARS:
            # The individual critical fields are already bounded. This final
            # fallback is only for an unusually large number of bounded records;
            # do not cut a failure reference or terminal reason in half.
            compact_lines = [
                "[Structured State]",
                f"Task ID: {bounded(snapshot.get('task_id') or '-', 120)}",
                f"Status: {snapshot['status']}; generation: {snapshot.get('current_generation_id', 0)}",
            ]
            if process_records:
                compact_lines.append(
                    "Background processes: " + bounded("; ".join(
                        f"{item.get('process_id', '?')}={item.get('status', '?')}"
                        f"/pid:{item.get('pid', '?')}"
                        f"/stdin:{item.get('stdin_mode', 'closed')}/{item.get('stdin_state', 'disabled')}"
                        f"/pending:{str(bool(item.get('write_pending', False))).lower()}"
                        f"/out:{item.get('stdout_offset', 0)},{item.get('stderr_offset', 0)}"
                        for item in process_records[-8:] if isinstance(item, dict)
                    ), 1200)
                )
            if snapshot.get("awaiting_process"):
                compact_lines.append(
                    "Awaiting process: " + bounded(
                        str(snapshot["awaiting_process"]), 500,
                    )
                )
            if active_plan:
                # Preserve the execution-critical plan shape even when the
                # full critical-state block must degrade again: revision,
                # goal, current step, ready/blocked queues, and counts.
                compact_lines.append(bounded(plan_lines[0], 300))
                compact_lines.append(bounded(plan_lines[1], 600))
                compact_lines.append(bounded(plan_lines[2], 500))
                current_plan_line = next(
                    (line for line in plan_lines if line.startswith("Current plan step:")),
                    None,
                )
                if current_plan_line is not None:
                    compact_lines.append(bounded(current_plan_line, 700))
                compact_lines.extend(bounded(line, 500) for line in plan_lines[-3:-1])
                compact_lines.append(bounded(plan_lines[-1], 200))
            else:
                compact_lines.extend(bounded(line, 300) for line in plan_lines)
            if crash_items:
                compact_lines.append("Crash recovery: " + bounded("; ".join(
                    f"{item.get('issue_id', '?')}={item.get('classification', '?')}/{item.get('status', '?')}"
                    for item in crash_items[:12]
                ), 900))
            if active_trigger is not None:
                compact_lines.append(
                    "Active plan trigger: "
                    f"{planning_state.get('active_trigger_id')} "
                    f"kind={active_trigger.get('kind')}; source="
                    f"{active_trigger.get('caused_by_failure_id') or active_trigger.get('caused_by_attempt_id') or active_trigger.get('caused_by_decision_id') or active_trigger.get('caused_by_crash_recovery_id') or '-'}; "
                    f"reason={bounded(active_trigger.get('reason') or '-', 350)}"
                )
            compact_lines.append(
                "Replan budget: " + bounded(str(planning_state), 500)
            )
            compact_lines.append(
                "Stagnation: " + bounded(str(snapshot.get("loop_stagnation", {})), 500)
            )
            if repair_loop and repair_loop.get("phase") != "idle":
                compact_lines.append("Repair loop: " + bounded(str(repair_loop), 800))
            if checkpoint_state_line is not None:
                compact_lines.append(bounded(checkpoint_state_line, 1800))
            if rollback_state_line is not None:
                compact_lines.append(bounded(rollback_state_line, 350))
            content = "\n".join(compact_lines)
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
