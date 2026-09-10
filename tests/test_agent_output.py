import json
import os
import sys
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import mini_agent.agent as agent_module
from mini_agent.agent import agent_loop, call_llm
from mini_agent.context import ContextManager
from mini_agent.permission import ALLOW, PermissionGate, PermissionPolicy
from mini_agent.state import AgentState
from mini_agent.tools.base import Tool, ToolExecutor, ToolRegistry


def _sse(delta):
    return ("data: " + json.dumps({"choices": [{"delta": delta}]})).encode() + b"\n"


class _Response:
    def __init__(self, lines):
        self.lines = lines

    def __iter__(self):
        return iter(self.lines)


class _Connection:
    response = _Response([])

    def __init__(self, *args, **kwargs):
        self.closed = False

    def request(self, *args, **kwargs):
        pass

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def _call_with_sse(lines, **kwargs):
    _Connection.response = _Response(lines + [b"data: [DONE]\n"])
    with patch("mini_agent.agent.http.client.HTTPConnection", _Connection), \
            patch("mini_agent.agent.http.client.HTTPSConnection", _Connection):
        return call_llm([], **kwargs)


def test_call_llm_forwards_content_chunks_to_callback_without_printing():
    chunks = []
    output = StringIO()
    with redirect_stdout(output):
        message = _call_with_sse([_sse({"content": "你"}), _sse({"content": "好"})], on_content=chunks.append)

    assert message["content"] == "你好"
    assert chunks == ["你", "好"]
    assert output.getvalue() == ""


def test_call_llm_standalone_print_and_stream_output_false_compatibility():
    lines = [_sse({"content": "standalone"})]
    output = StringIO()
    with redirect_stdout(output):
        message = _call_with_sse(lines)
    assert message["content"] == "standalone"
    assert output.getvalue() == "standalone"

    chunks = []
    output = StringIO()
    with redirect_stdout(output):
        message = _call_with_sse(lines, stream_output=False, on_content=chunks.append)
    assert message["content"] == "standalone"
    assert chunks == []
    assert output.getvalue() == ""


def test_call_llm_callback_failure_does_not_break_sse_parsing():
    def fail(_content):
        raise RuntimeError("renderer failed")

    message = _call_with_sse([_sse({"content": "still parsed"})], on_content=fail)
    assert message["content"] == "still parsed"


def test_quiet_call_llm_and_summary_suppress_streaming_output():
    chunks = []
    output = StringIO()
    _Connection.response = _Response([_sse({"content": "quiet"}), b"data: [DONE]\n"])
    with patch.object(agent_module, "OUTPUT_MODE", "quiet"), redirect_stdout(output):
        with patch("mini_agent.agent.http.client.HTTPConnection", _Connection), \
                patch("mini_agent.agent.http.client.HTTPSConnection", _Connection):
            message = call_llm([], on_content=chunks.append)
            summary = agent_module.summarize_messages([])

    assert message["content"] == "quiet"
    assert summary == "quiet"
    assert chunks == []
    assert output.getvalue() == ""


def test_agent_loop_renders_stream_once_and_keeps_history_protocol_unchanged():
    state = AgentState(task="answer")
    context = ContextManager(state, [{"role": "user", "content": "answer"}])

    def fake_llm(messages, **kwargs):
        kwargs["on_content"]("最终答案")
        return {"role": "assistant", "content": "最终答案"}

    output = StringIO()
    with patch.object(agent_module, "OUTPUT_MODE", "normal"), \
            patch.object(agent_module, "call_llm", side_effect=fake_llm), \
            redirect_stdout(output):
        result = agent_loop(context, ToolExecutor(ToolRegistry()))

    assert result == "最终答案"
    assert output.getvalue() == "助手 › 最终答案\n\n"
    assert context.history == [
        {"role": "user", "content": "answer"},
        {"role": "assistant", "content": "最终答案"},
    ]


def test_agent_loop_quiet_suppresses_progress_even_when_callback_is_used():
    state = AgentState(task="quiet")
    context = ContextManager(state, [{"role": "user", "content": "quiet"}])

    def fake_llm(messages, **kwargs):
        kwargs["on_content"]("hidden")
        return {"role": "assistant", "content": "hidden"}

    output = StringIO()
    with patch.object(agent_module, "OUTPUT_MODE", "quiet"), \
            patch.object(agent_module, "call_llm", side_effect=fake_llm), \
            redirect_stdout(output):
        agent_loop(context, ToolExecutor(ToolRegistry()))
    assert output.getvalue() == ""


def test_agent_loop_orders_concurrent_tool_display_and_keeps_tool_messages_clean():
    state = AgentState(task="inspect")
    context = ContextManager(state, [{"role": "user", "content": "inspect"}])
    registry = ToolRegistry()
    registry.register(Tool("first_tool", "first", {"type": "object", "properties": {}}, lambda: "first"))
    registry.register(Tool("second_tool", "second", {"type": "object", "properties": {}}, lambda: "second"))
    executor = ToolExecutor(registry, PermissionGate(PermissionPolicy({"first_tool": ALLOW, "second_tool": ALLOW})))
    responses = iter([
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "first", "type": "function", "function": {"name": "first_tool", "arguments": "{}"}},
            {"id": "second", "type": "function", "function": {"name": "second_tool", "arguments": "{}"}},
        ]},
        {"role": "assistant", "content": "done"},
    ])

    def fake_llm(messages, **kwargs):
        return next(responses)

    output = StringIO()
    with patch.object(agent_module, "OUTPUT_MODE", "normal"), \
            patch.object(agent_module, "call_llm", side_effect=fake_llm), \
            redirect_stdout(output):
        assert agent_loop(context, executor) == "done"

    rendered = output.getvalue()
    assert "执行中 · first_tool × 1，second_tool × 1" in rendered
    assert rendered.index("完成 · first_tool") < rendered.index("完成 · second_tool")
    assert context.history[-3:-1] == [
        {"role": "tool", "tool_call_id": "first", "content": "first"},
        {"role": "tool", "tool_call_id": "second", "content": "second"},
    ]


def test_recovery_schema_display_metadata_is_not_recorded_twice():
    state = AgentState(); state.begin_task("recover")
    registry = ToolRegistry()
    registry.register(Tool(
        "recover", "recover", {"type": "object", "properties": {
            "action": {"type": "string"},
            "caused_by_failure_id": {"type": "string"},
            "reason": {"type": "string"},
        }, "required": ["action", "caused_by_failure_id", "reason"]},
        lambda **kwargs: "recovered", effect_class="possible",
    ))
    executor = ToolExecutor(registry, PermissionGate(PermissionPolicy({"recover": ALLOW})))
    context = ContextManager(state, [{"role": "user", "content": "recover"}])
    responses = iter([
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "recover", "type": "function", "function": {
                "name": "recover", "arguments": json.dumps({"action": "adjust"}),
            },
        }]},
        {"role": "assistant", "content": "blocked"},
    ])

    def fake_llm(messages, **kwargs):
        return next(responses)

    output = StringIO()
    with patch.object(agent_module, "OUTPUT_MODE", "normal"), \
            patch.object(agent_module, "call_llm", side_effect=fake_llm), \
            redirect_stdout(output):
        agent_loop(context, executor)

    assert "无效 · recover" in output.getvalue()
    assert len(state.snapshot()["recovery_actions"]) == 1


def test_terminal_batch_displays_each_result_without_recording_or_dropping_protocol():
    state = AgentState(); state.begin_task("terminal")
    state.status = "blocked"
    state.terminal_reason = "already stopped"
    registry = ToolRegistry()
    calls = []
    registry.register(Tool(
        "read_file", "read", {"type": "object", "properties": {"path": {"type": "string"}}},
        lambda path: calls.append(("read_file", path)) or "never called",
    ))
    registry.register(Tool(
        "run_shell", "shell", {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
        lambda command: calls.append(("run_shell", command)) or "never called",
    ))
    executor = ToolExecutor(registry, PermissionGate(PermissionPolicy({"read_file": ALLOW, "run_shell": ALLOW})))
    context = ContextManager(state, [{"role": "user", "content": "terminal"}])
    responses = iter([
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "read", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"x.py"}'}},
            {"id": "shell", "type": "function", "function": {"name": "run_shell", "arguments": '{"command":"never"}'}},
        ]},
        {"role": "assistant", "content": "terminal explained"},
    ])

    def fake_llm(messages, **kwargs):
        return next(responses)

    output = StringIO()
    with patch.object(agent_module, "OUTPUT_MODE", "normal"), \
            patch.object(agent_module, "call_llm", side_effect=fake_llm), \
            redirect_stdout(output):
        assert agent_loop(context, executor) == "terminal explained"

    rendered = output.getvalue()
    assert "无效 · read_file · path=x.py" in rendered
    assert "无效 · run_shell · command=never" in rendered
    tool_messages = [message for message in context.history if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in tool_messages] == ["read", "shell"]
    assert all("task_terminal" in message["content"] for message in tool_messages)
    assert calls == []
    snapshot = state.snapshot()
    assert snapshot["attempts"] == []
    assert snapshot["recovery_actions"] == []


def test_internal_completion_retry_streams_each_response_without_empty_title():
    class ReminderState:
        status = "running"
        current_generation_id = 0

        def __init__(self):
            self.reminders = 0

        def completion_reminder(self):
            self.reminders += 1
            return {"unfinished_todos": ["verify"], "verification_required": True, "message": "continue"}

    class Context:
        def __init__(self):
            self.state = ReminderState()
            self.history = [{"role": "user", "content": "retry"}]
            self.notices = []

        def prepare_messages(self):
            return list(self.history)

        def set_runtime_notice(self, message):
            self.notices.append(message)

    context = Context()
    responses = iter([
        {"role": "assistant", "content": "first"},
        {"role": "assistant", "content": "final"},
    ])

    def fake_llm(messages, **kwargs):
        response = next(responses)
        kwargs["on_content"](response["content"])
        return response

    output = StringIO()
    with patch.object(agent_module, "OUTPUT_MODE", "normal"), \
            patch.object(agent_module, "call_llm", side_effect=fake_llm), \
            redirect_stdout(output):
        assert agent_loop(context, ToolExecutor(ToolRegistry())) == "final"

    rendered = output.getvalue()
    assert rendered.count("助手 › ") == 2
    assert "助手 › \n" not in rendered
    assert context.state.status == "blocked"


def test_long_tool_protocol_content_is_preserved_while_terminal_display_is_bounded():
    def run(mode):
        state = AgentState(task="long result")
        context = ContextManager(state, [{"role": "user", "content": "long result"}])
        registry = ToolRegistry()
        body = "x" * 2000
        registry.register(Tool(
            "read_file", "read", {"type": "object", "properties": {"path": {"type": "string"}}},
            lambda path: body,
        ))
        executor = ToolExecutor(registry, PermissionGate(PermissionPolicy({"read_file": ALLOW})))
        responses = iter([
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "long", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"x.py"}'},
            }]},
            {"role": "assistant", "content": "done"},
        ])

        def fake_llm(messages, **kwargs):
            return next(responses)

        output = StringIO()
        with patch.object(agent_module, "OUTPUT_MODE", mode), \
                patch.object(agent_module, "call_llm", side_effect=fake_llm), \
                redirect_stdout(output):
            agent_loop(context, executor)
        tool_content = next(message["content"] for message in context.history if message["role"] == "tool")
        return body, tool_content, output.getvalue(), context.history

    body, normal_content, normal_text, normal_history = run("normal")
    _, debug_content, debug_text, debug_history = run("debug")
    assert normal_content == body == debug_content
    assert body not in normal_text
    assert normal_history == debug_history == [
        {"role": "user", "content": "long result"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "long", "type": "function", "function": {
                "name": "read_file", "arguments": '{"path":"x.py"}',
            },
        }]},
        {"role": "tool", "tool_call_id": "long", "content": body},
        {"role": "assistant", "content": "done"},
    ]
    debug_result = debug_text.split("完成 · read_file", 1)[-1]
    assert len(debug_result) < 1400
    assert "[..." in debug_text or "…" in debug_text
    assert "x" * 1201 not in debug_text
