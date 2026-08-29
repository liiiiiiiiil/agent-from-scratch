"""Smoke and integration tests for the v0.11 agent loop.

验证 import 链路和函数签名，不实际调用 LLM（避免依赖网络）。
可独立运行：python tests/test_loop.py
（零第三方依赖，仅标准库）
"""

import os
import json
import sys
from threading import Event, Lock
from unittest.mock import patch

# 让 tests/ 目录下也能 import 到 src 布局的包
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mini_agent.agent import call_llm, agent_loop
from mini_agent.config import BASE_URL, API_KEY, MODEL, MAX_ITERATIONS
from mini_agent import __main__ as cli
from mini_agent.state import AgentState
from mini_agent.context import ContextBudget, ContextManager


def test_import():
    assert callable(call_llm), "call_llm 应可调用"
    assert callable(agent_loop), "agent_loop 应可调用"
    assert callable(cli.main), "CLI main 应可调用"
    print("PASS: agent/CLI 模块 import 成功，入口可调用")


def test_config():
    assert BASE_URL, "BASE_URL 不能为空"
    assert API_KEY, "API_KEY 不能为空"
    assert MODEL, "MODEL 不能为空"
    assert isinstance(MAX_ITERATIONS, int) and MAX_ITERATIONS > 0
    print(f"PASS: config 加载成功 (MODEL={MODEL}, MAX_ITERATIONS={MAX_ITERATIONS})")


def test_agent_loop_signature():
    import inspect
    sig = inspect.signature(agent_loop)
    params = list(sig.parameters)
    assert params == ["context_manager", "tool_executor"], (
        f"agent_loop 应接受 context_manager 和 tool_executor，实际: {params}"
    )
    print("PASS: agent_loop(context_manager, tool_executor) 签名正确")


def test_agent_loop_context_and_executor_integration():
    class FakeContextManager:
        def __init__(self):
            self.state = AgentState(task="calculate a value")
            self.history = [
                {"role": "system", "content": "system instructions"},
                {"role": "user", "content": "calculate a value"},
            ]
            self.prepare_calls = 0
            self.prepared_snapshots = []

        def prepare_messages(self):
            self.prepare_calls += 1
            prepared = list(self.history)
            self.prepared_snapshots.append(prepared)
            return prepared

    class FakeExecutor:
        def __init__(self):
            self.calls = []
            self.first_started = Event()
            self.second_completed = Event()
            self.completion_order = []
            self.completion_lock = Lock()

        def execute(self, name, arguments):
            self.calls.append((name, arguments))
            if name == "first_tool":
                self.first_started.set()
                # The timeout only prevents a broken sequential loop from hanging.
                if not self.second_completed.wait(timeout=5):
                    raise AssertionError("first_tool 未等到 second_tool 完成")
            else:
                if not self.first_started.wait(timeout=5):
                    raise AssertionError("second_tool 未观察到 first_tool 启动")
                with self.completion_lock:
                    self.completion_order.append(name)
                self.second_completed.set()
                return f"result-{name}"

            with self.completion_lock:
                self.completion_order.append(name)
            return f"result-{name}"

    context = FakeContextManager()
    tool_executor = FakeExecutor()
    state_before = context.state.snapshot()
    responses = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "first_tool",
                        "arguments": '{"value": 1}',
                    },
                },
                {
                    "id": "call-2",
                    "type": "function",
                    "function": {
                        "name": "second_tool",
                        "arguments": '{"value": 2}',
                    },
                },
            ],
        },
        {"role": "assistant", "content": "both tools completed"},
    ]

    def fake_call_llm(messages):
        assert messages is context.prepared_snapshots[-1]
        return responses.pop(0)

    with patch("mini_agent.agent.call_llm", side_effect=fake_call_llm):
        result = agent_loop(context, tool_executor)

    assert result == "both tools completed"
    assert context.prepare_calls == 2
    assert len(tool_executor.calls) == 2
    assert {name for name, _ in tool_executor.calls} == {"first_tool", "second_tool"}
    assert {name: arguments for name, arguments in tool_executor.calls} == {
        "first_tool": {"value": 1},
        "second_tool": {"value": 2},
    }
    assert tool_executor.first_started.is_set()
    assert tool_executor.second_completed.is_set()
    assert tool_executor.completion_order == ["second_tool", "first_tool"]
    assert [message["role"] for message in context.history] == [
        "system", "user", "assistant", "tool", "tool", "assistant",
    ]
    assert context.history[2]["tool_calls"][0]["id"] == "call-1"
    assert context.history[2]["tool_calls"][1]["id"] == "call-2"
    assert context.history[3:5] == [
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "result-first_tool",
        },
        {
            "role": "tool",
            "tool_call_id": "call-2",
            "content": "result-second_tool",
        },
    ]
    assert [message["role"] for message in context.prepared_snapshots[1]] == [
        "system", "user", "assistant", "tool", "tool",
    ]
    assert context.prepared_snapshots[1][3:5] == context.history[3:5]
    assert all("state" not in message for message in context.history)
    assert context.state.snapshot() == state_before
    print("PASS: agent loop 使用 ContextManager/注入 Executor 并保持消息顺序")


def test_agent_loop_completes_after_more_than_twenty_tool_rounds_with_compaction():
    state = AgentState(task="long task")
    history = [{"role": "system", "content": "system"}, {"role": "user", "content": "long task"}]
    context = ContextManager(
        state,
        history,
        ContextBudget(window=180, output_reserve_ratio=0, history_ratio=0.5),
        summarizer=lambda prompt: "long task summary",
        keep_rounds=2,
    )

    class Executor:
        def execute(self, name, arguments):
            return "x" * 100

    responses = [
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": f"call-{index}", "type": "function",
            "function": {"name": "fake_tool", "arguments": "{}"},
        }]}
        for index in range(21)
    ] + [{"role": "assistant", "content": "long task complete"}]

    with patch("mini_agent.agent.call_llm", side_effect=responses) as mocked_call:
        result = agent_loop(context, Executor())

    assert result == "long task complete"
    assert mocked_call.call_count == 22
    assert context._compacted is True


def test_agent_loop_tool_call_errors_keep_protocol_and_continue():
    class FakeContextManager:
        def __init__(self):
            self.state = AgentState(task="handle tool errors")
            self.history = [
                {"role": "system", "content": "system instructions"},
                {"role": "user", "content": "handle tool errors"},
            ]
            self.prepare_calls = 0
            self.prepared_snapshots = []

        def prepare_messages(self):
            self.prepare_calls += 1
            prepared = list(self.history)
            self.prepared_snapshots.append(prepared)
            return prepared

    class FakeExecutor:
        def __init__(self):
            self.calls = []

        def execute(self, name, arguments):
            self.calls.append((name, arguments))
            raise ValueError(f"未知工具: {name}")

    context = FakeContextManager()
    tool_executor = FakeExecutor()
    state_before = context.state.snapshot()
    responses = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-bad-json",
                    "type": "function",
                    "function": {
                        "name": "valid_tool",
                        "arguments": "{not valid json",
                    },
                },
                {
                    "id": "call-bad-function",
                    "type": "function",
                    "function": None,
                    "index": 1,
                    "extra": "drop this",
                },
                {
                    "id": "call-bad-name",
                    "type": "function",
                    "function": {
                        "name": "",
                        "arguments": "{}",
                        "extra": "drop this too",
                    },
                },
                {
                    "id": "call-bad-arguments",
                    "type": "function",
                    "function": {
                        "name": "valid_tool",
                        "arguments": None,
                    },
                },
                {
                    "id": "call-unknown",
                    "type": "function",
                    "function": {
                        "name": "unknown_tool",
                        "arguments": "{}",
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "valid_tool",
                        "arguments": "{}",
                    },
                },
                {
                    "id": "",
                    "type": "function",
                    "function": {
                        "name": "valid_tool",
                        "arguments": "{}",
                    },
                },
                {
                    "id": 123,
                    "type": "function",
                    "function": {
                        "name": "valid_tool",
                        "arguments": "{}",
                    },
                },
                {
                    "id": "call-unknown",
                    "type": "function",
                    "function": {
                        "name": "valid_tool",
                        "arguments": "{}",
                    },
                },
                {
                    "id": "call-missing-function",
                    "type": "function",
                },
                {
                    "id": "call-missing-type",
                    "function": {
                        "name": "valid_tool",
                        "arguments": "{}",
                    },
                },
                {
                    "id": "call-bad-type",
                    "type": "custom",
                    "function": {
                        "name": "valid_tool",
                        "arguments": "{}",
                    },
                },
            ],
        },
        {"role": "assistant", "content": "continued after tool errors"},
    ]

    def fake_call_llm(messages):
        assert messages is context.prepared_snapshots[-1]
        return responses.pop(0)

    with patch("mini_agent.agent.call_llm", side_effect=fake_call_llm):
        result = agent_loop(context, tool_executor)

    assert result == "continued after tool errors"
    assert context.prepare_calls == 2
    assert tool_executor.calls == [("unknown_tool", {})]
    assert [message["role"] for message in context.history] == (
        ["system", "user", "assistant"] + ["tool"] * 12 + ["assistant"]
    )
    assistant_tool_calls = context.history[2]["tool_calls"]
    tool_results = context.history[3:15]
    assistant_ids = [tool_call["id"] for tool_call in assistant_tool_calls]
    result_ids = [message["tool_call_id"] for message in tool_results]
    assert len(assistant_tool_calls) == 12
    assert all(isinstance(tool_call_id, str) and tool_call_id for tool_call_id in assistant_ids)
    assert len(set(assistant_ids)) == len(assistant_ids)
    assert result_ids == assistant_ids
    assert assistant_ids[4] == "call-unknown"
    invalid_original_ids = {
        0: "call-bad-json",
        1: "call-bad-function",
        2: "call-bad-name",
        3: "call-bad-arguments",
        6: "",
        7: 123,
        8: "call-unknown",
        9: "call-missing-function",
        10: "call-missing-type",
        11: "call-bad-type",
    }
    assert all(assistant_ids[index] != original_id for index, original_id in invalid_original_ids.items())
    assert all(set(tool_call) == {"id", "type", "function"} for tool_call in assistant_tool_calls)
    assert all(
        set(tool_call["function"]) == {"name", "arguments"}
        for tool_call in assistant_tool_calls
    )
    assert all(tool_call["type"] == "function" for tool_call in assistant_tool_calls)
    assert all(
        isinstance(tool_call["function"].get("name"), str)
        and tool_call["function"]["name"]
        and isinstance(tool_call["function"].get("arguments"), str)
        for tool_call in assistant_tool_calls
    )
    assert all("工具调用失败" in message["content"] for message in tool_results)
    assert "ValueError" in tool_results[4]["content"]
    assert [message["role"] for message in context.prepared_snapshots[1]] == (
        ["system", "user", "assistant"] + ["tool"] * 12
    )
    assert context.prepared_snapshots[1][3:15] == tool_results
    assert all("state" not in message for message in context.history)
    assert context.state.snapshot() == state_before
    print("PASS: malformed/unknown tool calls 保持协议完整并继续下一轮")


def test_agent_loop_unstringifiable_result_keeps_protocol():
    class UnstringifiableResult:
        def __str__(self):
            raise RuntimeError("result cannot be stringified")

    class FakeContextManager:
        def __init__(self):
            self.history = [
                {"role": "system", "content": "system instructions"},
                {"role": "user", "content": "run the tool"},
            ]
            self.prepare_calls = 0
            self.prepared_snapshots = []

        def prepare_messages(self):
            self.prepare_calls += 1
            prepared = list(self.history)
            self.prepared_snapshots.append(prepared)
            return prepared

    class FakeExecutor:
        def __init__(self):
            self.calls = []

        def execute(self, name, arguments):
            self.calls.append((name, arguments))
            return UnstringifiableResult()

    context = FakeContextManager()
    tool_executor = FakeExecutor()
    responses = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-bad-result",
                "type": "function",
                "function": {
                    "name": "valid_tool",
                    "arguments": "{}",
                },
            }],
        },
        {"role": "assistant", "content": "continued"},
    ]

    def fake_call_llm(messages):
        assert messages is context.prepared_snapshots[-1]
        return responses.pop(0)

    with patch("mini_agent.agent.call_llm", side_effect=fake_call_llm):
        result = agent_loop(context, tool_executor)

    assert result == "continued"
    assert context.prepare_calls == 2
    assert tool_executor.calls == [("valid_tool", {})]
    assert [message["role"] for message in context.history] == [
        "system", "user", "assistant", "tool", "assistant",
    ]
    assert context.history[3] == {
        "role": "tool",
        "tool_call_id": "call-bad-result",
        "content": "工具调用失败: RuntimeError",
    }
    assert context.prepared_snapshots[1][3] == context.history[3]
    print("PASS: result __str__ 异常仍回灌字符串错误结果")


def test_agent_loop_observation_print_failure_does_not_break_protocol():
    class FakeContextManager:
        def __init__(self):
            self.history = [
                {"role": "system", "content": "system instructions"},
                {"role": "user", "content": "run the tool"},
            ]
            self.prepare_snapshots = []

        def prepare_messages(self):
            prepared = list(self.history)
            self.prepare_snapshots.append(prepared)
            return prepared

    class FakeExecutor:
        def execute(self, name, arguments):
            return "ok"

    context = FakeContextManager()
    tool_executor = FakeExecutor()
    responses = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-print-safe",
                "type": "function",
                "function": {
                    "name": "valid_tool",
                    "arguments": "{}",
                },
            }],
        },
        {"role": "assistant", "content": "continued"},
    ]

    def fake_call_llm(messages):
        assert messages is context.prepare_snapshots[-1]
        return responses.pop(0)

    with patch("mini_agent.agent.call_llm", side_effect=fake_call_llm), \
            patch("builtins.print", side_effect=RuntimeError("logging unavailable")):
        result = agent_loop(context, tool_executor)

    assert result == "continued"
    assert context.prepare_snapshots[0] is not context.history
    assert context.prepare_snapshots[1] is not context.history
    assert [message["role"] for message in context.history] == [
        "system", "user", "assistant", "tool", "assistant",
    ]
    assert context.history[3] == {
        "role": "tool",
        "tool_call_id": "call-print-safe",
        "content": "ok",
    }
    print("PASS: 观测日志异常不影响 tool result 回灌")


def test_call_llm_preserves_malformed_streaming_tool_delta_for_loop():
    def sse(delta):
        chunk = {"choices": [{"delta": delta}]}
        return ("data: " + json.dumps(chunk) + "\n").encode()

    lines = [
        sse({
            "tool_calls": [{
                "index": 0,
                "id": "call-function-none",
                "function": None,
            }],
        }),
        sse({
            "tool_calls": [{
                "index": 1,
                "id": "call-function-text",
                "function": "not-a-dict",
            }],
        }),
        sse({
            "tool_calls": [{
                "index": 2,
                "id": "call-arguments-number",
                "function": {"name": "valid_tool", "arguments": 123},
            }],
        }),
        sse({
            "tool_calls": [{
                "index": 3,
                "id": "call-normal",
                "function": {"name": "valid_tool", "arguments": '{"value": 1}'},
            }],
        }),
        b"data: [DONE]\n",
    ]

    class FakeResponse:
        def __iter__(self):
            return iter(lines)

    class FakeConnection:
        def __init__(self, *args, **kwargs):
            self.requested = False
            self.closed = False

        def request(self, *args, **kwargs):
            self.requested = True

        def getresponse(self):
            return FakeResponse()

        def close(self):
            self.closed = True

    with patch("mini_agent.agent.http.client.HTTPConnection", FakeConnection), \
         patch("mini_agent.agent.http.client.HTTPSConnection", FakeConnection):
        malformed_message = call_llm([])

    raw_tool_calls = malformed_message["tool_calls"]
    assert raw_tool_calls[0]["function"] is None
    assert raw_tool_calls[1]["function"] == "not-a-dict"
    assert raw_tool_calls[2]["function"]["arguments"] == 123
    assert raw_tool_calls[3]["function"] == {
        "name": "valid_tool",
        "arguments": '{"value": 1}',
    }

    class FakeContextManager:
        def __init__(self):
            self.history = [
                {"role": "system", "content": "system instructions"},
                {"role": "user", "content": "handle streamed calls"},
            ]
            self.prepare_snapshots = []

        def prepare_messages(self):
            prepared = list(self.history)
            self.prepare_snapshots.append(prepared)
            return prepared

    class FakeExecutor:
        def __init__(self):
            self.calls = []

        def execute(self, name, arguments):
            self.calls.append((name, arguments))
            return "ok"

    context = FakeContextManager()
    tool_executor = FakeExecutor()
    responses = [malformed_message, {"role": "assistant", "content": "continued"}]

    def fake_call_llm(messages):
        assert messages is context.prepare_snapshots[-1]
        return responses.pop(0)

    with patch("mini_agent.agent.call_llm", side_effect=fake_call_llm):
        result = agent_loop(context, tool_executor)

    assert result == "continued"
    assert tool_executor.calls == [("valid_tool", {"value": 1})]
    assistant_tool_calls = context.history[2]["tool_calls"]
    tool_results = context.history[3:7]
    assert [message["role"] for message in context.history] == [
        "system", "user", "assistant", "tool", "tool", "tool", "tool", "assistant",
    ]
    assert all(
        isinstance(tool_call["id"], str)
        and tool_call["id"]
        and tool_call["type"] == "function"
        and set(tool_call["function"]) == {"name", "arguments"}
        for tool_call in assistant_tool_calls
    )
    assert [message["tool_call_id"] for message in tool_results] == [
        tool_call["id"] for tool_call in assistant_tool_calls
    ]
    assert tool_results[3]["content"] == "ok"
    assert context.prepare_snapshots[1][3:7] == tool_results
    print("PASS: malformed streaming delta 安全累积并完成协议回灌")


def test_cli_marks_state_failed_and_reraises_agent_error():
    state = AgentState()

    def raise_agent_error(context, tool_executor):
        raise RuntimeError("llm failed")

    with patch.object(cli, "AgentState", return_value=state), \
            patch.object(cli, "agent_loop", side_effect=raise_agent_error), \
            patch.object(sys, "argv", ["mini_agent", "failing task"]), \
            patch("builtins.input", return_value="exit"):
        try:
            cli.main()
            assert False, "agent_loop 异常应重新抛出"
        except RuntimeError as error:
            assert str(error) == "llm failed"

    assert state.task == "failing task"
    assert state.status == "failed"
    print("PASS: CLI agent_loop 异常先标记 failed 再重新抛出")


def test_cli_reuses_context_and_executor_for_argv_and_interactive_tasks():
    created_states = []
    created_contexts = []
    created_executors = []

    class FakeState:
        def __init__(self):
            self.task = ""
            self._status = "running"
            self.status_history = []
            self.record_tool = lambda *args: None
            created_states.append(self)

        @property
        def status(self):
            return self._status

        @status.setter
        def status(self, value):
            self._status = value
            self.status_history.append(value)

    class FakeContextManager:
        def __init__(self, state, history):
            self.state = state
            self.history = history
            created_contexts.append(self)

    class FakeExecutor:
        def __init__(self, registry, gate=None, on_result=None):
            self.registry = registry
            self.gate = gate
            self.on_result = on_result
            created_executors.append(self)

    loop_calls = []

    def fake_agent_loop(context, tool_executor):
        loop_calls.append({
            "context": context,
            "executor": tool_executor,
            "task": context.state.task,
            "status": context.state.status,
            "history": list(context.history),
        })
        return "完成" if len(loop_calls) == 1 else "达到最大迭代次数"

    with patch.object(cli, "AgentState", FakeState), \
            patch.object(cli, "ContextManager", FakeContextManager), \
            patch.object(cli, "ToolExecutor", FakeExecutor), \
            patch.object(cli, "build_system_prompt", return_value="system"), \
            patch.object(cli, "agent_loop", side_effect=fake_agent_loop), \
            patch.object(sys, "argv", ["mini_agent", "argv task"]), \
            patch("builtins.input", side_effect=["interactive task", "exit"]):
        cli.main()

    assert len(created_states) == 1
    assert len(created_contexts) == 1
    assert len(created_executors) == 1

    state = created_states[0]
    context = created_contexts[0]
    tool_executor = created_executors[0]
    assert context.state is state
    assert tool_executor.registry is cli.registry
    assert tool_executor.on_result is state.record_tool
    assert [call["context"] for call in loop_calls] == [context, context]
    assert [call["executor"] for call in loop_calls] == [
        tool_executor, tool_executor,
    ]
    assert [call["task"] for call in loop_calls] == [
        "argv task", "interactive task",
    ]
    assert [call["status"] for call in loop_calls] == ["running", "running"]
    assert context.history == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "argv task"},
        {"role": "user", "content": "interactive task"},
    ]
    assert state.task == "interactive task"
    assert state.status_history == ["running", "done", "running", "failed"]
    assert state.status == "failed"
    print("PASS: CLI argv/交互共用并复用同一 State/Context/Executor")


if __name__ == "__main__":
    test_import()
    test_config()
    test_agent_loop_signature()
    test_agent_loop_context_and_executor_integration()
    test_agent_loop_tool_call_errors_keep_protocol_and_continue()
    test_agent_loop_unstringifiable_result_keeps_protocol()
    test_agent_loop_observation_print_failure_does_not_break_protocol()
    test_call_llm_preserves_malformed_streaming_tool_delta_for_loop()
    test_cli_marks_state_failed_and_reraises_agent_error()
    test_cli_reuses_context_and_executor_for_argv_and_interactive_tasks()
    print("\n全部 smoke test 通过")
