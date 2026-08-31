"""Unit tests for v0.12 context budgeting and trimming.

可独立运行：PYTHONPATH=src python tests/test_context.py
"""

import os
import sys
from typing import get_type_hints

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mini_agent.context import (
    ContextBudget,
    ContextEvent,
    ContextManager,
    ContextStats,
    Message,
    _split_rounds,
    count_tokens,
)
from mini_agent.state import AgentState


def test_constructor_preserves_references():
    state = AgentState(task="update the app")
    history = [{"role": "user", "content": "update the app"}]

    context = ContextManager(state, history)

    assert context.state is state
    assert context.history is history


def test_prepare_messages_returns_a_copy_when_within_budget():
    history = [{"role": "user", "content": "calculate 1 + 1"}]
    context = ContextManager(AgentState(), history)

    prepared = context.prepare_messages()

    assert prepared == history
    assert prepared is not history
    assert prepared[0] is not history[0]


def test_message_type_annotations_are_explicit():
    init_hints = get_type_hints(ContextManager.__init__)
    prepare_hints = get_type_hints(ContextManager.prepare_messages)

    assert Message == dict[str, object]
    assert init_hints["history"] == list[Message]
    assert prepare_hints["return"] == list[Message]


def test_prepare_messages_preserves_order_and_content():
    history = [
        {"role": "system", "content": "system instructions"},
        {"role": "user", "content": "read main.py"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call-1", "function": {"name": "read_file"}}],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "file contents"},
    ]
    context = ContextManager(AgentState(), history)

    prepared = context.prepare_messages()

    assert prepared == history
    assert prepared[0] is not history[0]
    assert prepared[3] is not history[3]


def test_state_is_not_injected_or_changed():
    state = AgentState(task="update the app", current_goal="edit main.py")
    state.record_tool("read_file", {"path": "main.py"}, True, "read")
    state_before = state.snapshot()
    history = [{"role": "user", "content": "update the app"}]
    context = ContextManager(state, history)

    prepared = context.prepare_messages()

    assert prepared == [{"role": "user", "content": "update the app"}]
    assert state.snapshot() == state_before


def test_count_tokens_uses_the_documented_heuristic():
    assert count_tokens("abcdef") == 2
    assert count_tokens(None) == 0
    assert count_tokens({"content": "abcdef", "role": "user"}) == 3
    assert count_tokens([{"content": "abcdef"}, {"content": "xyz"}]) == 3


def test_context_budget_reserves_output_and_limits_history():
    budget = ContextBudget(window=100, output_reserve_ratio=0.2, history_ratio=0.4)

    assert budget.input_limit == 80
    assert budget.history_limit == 40
    assert budget.message_limit(10) == 50
    assert budget.message_limit(90) == 90


def test_split_rounds_keeps_tool_calls_and_results_together():
    assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "call-1"}],
    }
    first_tool = {"role": "tool", "tool_call_id": "call-1", "content": "one"}
    second_tool = {"role": "tool", "tool_call_id": "call-2", "content": "two"}
    history = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "task"},
        assistant,
        first_tool,
        second_tool,
        {"role": "assistant", "content": "done"},
    ]

    prefix, rounds = _split_rounds(history)

    assert prefix == history[:2]
    assert rounds == [[assistant, first_tool, second_tool], [history[-1]]]


def test_trim_truncates_old_tool_result_without_mutating_history():
    tool_content = "begin-" + "x" * 900 + "-end"
    history = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call-1"}]},
        {"role": "tool", "tool_call_id": "call-1", "content": tool_content},
    ]
    context = ContextManager(AgentState(), history, ContextBudget(window=250, output_reserve_ratio=0, history_ratio=0.8))

    prepared = context.prepare_messages()

    assert prepared[-1]["content"] != tool_content
    assert prepared[-1]["content"].startswith("begin-")
    assert prepared[-1]["content"].endswith("-end")
    assert history[-1]["content"] == tool_content
    assert [message["role"] for message in prepared] == ["system", "user", "assistant", "tool"]


def test_trim_removes_oldest_complete_round_without_orphan_tool_results():
    first_assistant = {"role": "assistant", "content": None, "tool_calls": [{"id": "call-1"}]}
    second_assistant = {"role": "assistant", "content": None, "tool_calls": [{"id": "call-2"}]}
    history = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "task"},
        first_assistant,
        {"role": "tool", "tool_call_id": "call-1", "content": "a" * 100},
        second_assistant,
        {"role": "tool", "tool_call_id": "call-2", "content": "b" * 100},
    ]
    context = ContextManager(AgentState(), history, ContextBudget(window=100, output_reserve_ratio=0, history_ratio=0.5))

    prepared = context.prepare_messages()

    assert prepared[:2] == history[:2]
    assert second_assistant in prepared
    assert first_assistant not in prepared
    call_ids = {call["id"] for message in prepared if message.get("role") == "assistant" for call in message.get("tool_calls", [])}
    assert all(
        message.get("role") != "tool" or message["tool_call_id"] in call_ids
        for message in prepared
    )


def test_protected_messages_remain_when_they_exceed_budget():
    history = [
        {"role": "system", "content": "s" * 300},
        {"role": "user", "content": "u" * 300},
        {"role": "assistant", "content": "old reply"},
    ]
    context = ContextManager(AgentState(), history, ContextBudget(window=30, output_reserve_ratio=0, history_ratio=0.1))

    prepared = context.prepare_messages()

    assert prepared == history[:2]


def test_invalid_budget_is_rejected():
    for kwargs in (
        {"window": 0},
        {"output_reserve_ratio": 1},
        {"history_ratio": -0.1},
    ):
        try:
            ContextBudget(**kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {kwargs}")


def _tool_round(number):
    call_id = f"call-{number}"
    return [
        {"role": "assistant", "content": None, "tool_calls": [{"id": call_id}]},
        {"role": "tool", "tool_call_id": call_id, "content": f"result {number}"},
    ]


def test_compact_injects_summary_and_structured_state():
    history = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}]
    for number in range(8):
        history.extend(_tool_round(number))
    state = AgentState(task="task")
    state.record_tool("write_file", {"path": "main.py"}, True, "written")
    context = ContextManager(state, history, summarizer=lambda prompt: "done: summary", keep_rounds=3)

    assert context.compact()
    prepared = context.prepare_messages()
    assert any(message.get("content", "").startswith("[Structured State]") for message in prepared)
    assert any(message.get("content", "").startswith("[Historical Summary]") for message in prepared)
    assert sum(message.get("role") == "tool" for message in prepared) == 3
    call_ids = {call["id"] for message in prepared if message.get("role") == "assistant" for call in message.get("tool_calls", [])}
    assert all(message.get("role") != "tool" or message["tool_call_id"] in call_ids for message in prepared)


def test_compact_failure_falls_back_without_mutating_history():
    history = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}]
    for number in range(4):
        history.extend(_tool_round(number))
    original = list(history)
    context = ContextManager(AgentState(), history, summarizer=lambda prompt: (_ for _ in ()).throw(RuntimeError()))

    assert context.compact(keep_rounds=2) is False
    assert history == original


def test_prepare_messages_automatically_compacts_when_over_budget():
    history = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}]
    for number in range(8):
        history.extend(_tool_round(number))
    calls = []
    context = ContextManager(
        AgentState(task="task"), history,
        ContextBudget(window=120, output_reserve_ratio=0, history_ratio=0.5),
        summarizer=lambda prompt: calls.append(prompt) or "automatic summary",
        keep_rounds=2,
    )

    prepared = context.prepare_messages()

    assert calls
    assert any(message.get("content", "").startswith("[Historical Summary]") for message in prepared)
    assert sum(message.get("role") == "tool" for message in prepared) <= 2


def test_multiple_compactions_refresh_structured_state():
    history = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}]
    for number in range(8):
        history.extend(_tool_round(number))
    state = AgentState(task="task")
    summaries = []
    context = ContextManager(
        state, history, summarizer=lambda prompt: summaries.append(prompt) or f"summary {len(summaries)}", keep_rounds=2
    )

    assert context.compact()
    state.record_tool("write_file", {"path": "after-first.py"}, True, "written")
    for number in range(8, 14):
        context.history.extend(_tool_round(number))
    assert context.compact()
    prepared = context.prepare_messages()

    assert len(summaries) == 2
    state_message = next(message for message in prepared if message.get("content", "").startswith("[Structured State]"))
    assert "after-first.py" in state_message["content"]


def test_structured_state_includes_recent_completed_tools():
    state = AgentState(task="task")
    state.record_tool("run_shell", {"command": "echo round-11"}, True, "round-11")
    context = ContextManager(state, [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}])

    rendered = context._render_state()["content"]

    assert "echo round-11" in rendered
    assert "do not repeat" in rendered


def test_repeated_prepare_does_not_resummarize_same_rounds():
    history = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}]
    for number in range(8):
        history.extend(_tool_round(number))
    calls = []
    context = ContextManager(
        AgentState(task="task"), history,
        ContextBudget(window=120, output_reserve_ratio=0, history_ratio=0.5),
        summarizer=lambda prompt: calls.append(prompt) or "stable summary",
        keep_rounds=2,
    )

    context.prepare_messages()
    context.prepare_messages()

    assert len(calls) == 1


def test_context_stats_use_mutually_exclusive_buckets():
    history = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "reply"},
        {"role": "tool", "tool_call_id": "call-1", "content": "tool output"},
    ]
    state = AgentState(task="task")
    context = ContextManager(state, history, observability=False)

    prepared = context.prepare_messages()
    stats = context.stats_snapshot()

    assert isinstance(stats, ContextStats)
    assert stats.tokens == count_tokens(prepared)
    assert stats.tokens == stats.system + stats.task + stats.state + stats.history + stats.tool_result
    assert stats.reserve == context.budget.window - context.budget.input_limit
    assert stats.system > 0 and stats.task > 0 and stats.history > 0 and stats.tool_result > 0


def test_observer_receives_prepared_trimmed_and_compacted_events():
    events = []
    history = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}]
    for number in range(8):
        history.extend(_tool_round(number))
    context = ContextManager(
        AgentState(task="task"), history,
        ContextBudget(window=120, output_reserve_ratio=0, history_ratio=0.5),
        summarizer=lambda prompt: "summary",
        keep_rounds=2,
        observability=False,
        observer=events.append,
    )

    context.prepare_messages()

    assert all(isinstance(event, ContextEvent) for event in events)
    assert "prepared" in [event.kind for event in events]
    assert "compacted" in [event.kind for event in events]
    assert any(event.details.get("action") == "remove_round" for event in events)


def test_observability_can_be_disabled_and_observer_errors_are_ignored(capsys):
    history = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}]
    context = ContextManager(
        AgentState(), history, observability=False,
        observer=lambda event: (_ for _ in ()).throw(RuntimeError("observer")),
    )

    context.prepare_messages()

    assert capsys.readouterr().out == ""
    assert isinstance(context.stats_snapshot(), ContextStats)


if __name__ == "__main__":
    test_constructor_preserves_references()
    test_prepare_messages_returns_a_copy_when_within_budget()
    test_message_type_annotations_are_explicit()
    test_prepare_messages_preserves_order_and_content()
    test_state_is_not_injected_or_changed()
    test_count_tokens_uses_the_documented_heuristic()
    test_context_budget_reserves_output_and_limits_history()
    test_split_rounds_keeps_tool_calls_and_results_together()
    test_trim_truncates_old_tool_result_without_mutating_history()
    test_trim_removes_oldest_complete_round_without_orphan_tool_results()
    test_protected_messages_remain_when_they_exceed_budget()
    test_invalid_budget_is_rejected()
    print("\n全部 context test 通过")
