"""Unit tests for the v0.11 ContextManager.

可独立运行：PYTHONPATH=src python tests/test_context.py
"""

import os
import sys
from typing import get_type_hints

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mini_agent.context import ContextManager, Message
from mini_agent.state import AgentState


def test_constructor_preserves_references():
    state = AgentState(task="update the app")
    history = [{"role": "user", "content": "update the app"}]

    context = ContextManager(state, history)

    assert context.state is state
    assert context.history is history


def test_prepare_messages_returns_same_history_list():
    history = [{"role": "user", "content": "calculate 1 + 1"}]
    context = ContextManager(AgentState(), history)

    assert context.prepare_messages() is history


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

    assert context.prepare_messages() == history
    assert context.prepare_messages()[0] is history[0]
    assert context.prepare_messages()[3] is history[3]


def test_state_is_not_injected_or_changed():
    state = AgentState(task="update the app", current_goal="edit main.py")
    state.record_tool("read_file", {"path": "main.py"}, True, "read")
    state_before = state.snapshot()
    history = [{"role": "user", "content": "update the app"}]
    context = ContextManager(state, history)

    prepared = context.prepare_messages()

    assert prepared is history
    assert prepared == [{"role": "user", "content": "update the app"}]
    assert state.snapshot() == state_before


if __name__ == "__main__":
    test_constructor_preserves_references()
    test_prepare_messages_returns_same_history_list()
    test_message_type_annotations_are_explicit()
    test_prepare_messages_preserves_order_and_content()
    test_state_is_not_injected_or_changed()
    print("\n全部 context test 通过")
