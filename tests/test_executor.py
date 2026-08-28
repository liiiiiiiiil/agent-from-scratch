"""Unit tests for ToolExecutor result callbacks.

可独立运行：PYTHONPATH=src python tests/test_executor.py
"""

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Lock, get_ident

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mini_agent.permission import ALLOW, DENY, PermissionGate, PermissionPolicy
from mini_agent.tools.base import (
    RESULT_BRIEF_MAX_LENGTH,
    Tool,
    ToolExecutor,
    ToolRegistry,
)


def make_registry(handler):
    registry = ToolRegistry()
    registry.register(Tool(
        name="dummy",
        description="test tool",
        parameters={},
        handler=handler,
    ))
    return registry


def test_success_calls_callback_once():
    events = []
    arguments = {"value": 3}

    def on_result(name, received_arguments, ok, brief):
        events.append((name, received_arguments, ok, brief))

    registry = make_registry(lambda value: f"value={value}")
    gate = PermissionGate(PermissionPolicy({"dummy": ALLOW}))
    executor = ToolExecutor(registry, gate, on_result)

    result = executor.execute("dummy", arguments)

    assert result == "value=3"
    assert events == [("dummy", arguments, True, "value=3")]


def test_permission_denial_calls_callback_once():
    events = []
    handler_called = False

    def handler():
        nonlocal handler_called
        handler_called = True
        return "not reached"

    def on_result(name, arguments, ok, brief):
        events.append((name, arguments, ok, brief))

    registry = make_registry(handler)
    gate = PermissionGate(PermissionPolicy({"dummy": DENY}))
    executor = ToolExecutor(registry, gate=gate, on_result=on_result)

    result = executor.execute("dummy", {})

    assert result == "权限拒绝: 规则禁止调用 dummy(*)"
    assert events == [("dummy", {}, False, result)]
    assert not handler_called


def test_handler_exception_calls_callback_once():
    events = []

    def on_result(name, arguments, ok, brief):
        events.append((name, arguments, ok, brief))

    def handler():
        raise RuntimeError("boom")

    registry = make_registry(handler)
    gate = PermissionGate(PermissionPolicy({"dummy": ALLOW}))
    executor = ToolExecutor(registry, gate=gate, on_result=on_result)

    result = executor.execute("dummy", {})

    assert result == "Tool 执行失败: boom"
    assert events == [("dummy", {}, False, result)]


def test_callback_exception_does_not_change_success_result():
    calls = []

    def on_result(name, arguments, ok, brief):
        calls.append((name, arguments, ok, brief))
        raise RuntimeError("callback boom")

    registry = make_registry(lambda: "success")
    gate = PermissionGate(PermissionPolicy({"dummy": ALLOW}))
    executor = ToolExecutor(registry, gate=gate, on_result=on_result)

    assert executor.execute("dummy", {}) == "success"
    assert len(calls) == 1


def test_callback_exception_does_not_change_permission_denial():
    calls = []

    def on_result(name, arguments, ok, brief):
        calls.append((name, arguments, ok, brief))
        raise RuntimeError("callback boom")

    registry = make_registry(lambda: "not reached")
    gate = PermissionGate(PermissionPolicy({"dummy": DENY}))
    executor = ToolExecutor(registry, gate=gate, on_result=on_result)

    result = executor.execute("dummy", {})

    assert result == "权限拒绝: 规则禁止调用 dummy(*)"
    assert len(calls) == 1


def test_callback_exception_does_not_change_handler_error():
    calls = []

    def on_result(name, arguments, ok, brief):
        calls.append((name, arguments, ok, brief))
        raise RuntimeError("callback boom")

    def handler():
        raise RuntimeError("handler boom")

    registry = make_registry(handler)
    gate = PermissionGate(PermissionPolicy({"dummy": ALLOW}))
    executor = ToolExecutor(registry, gate=gate, on_result=on_result)

    result = executor.execute("dummy", {})

    assert result == "Tool 执行失败: handler boom"
    assert len(calls) == 1


def test_unstringifiable_result_gets_safe_brief():
    events = []

    class Unstringifiable:
        def __str__(self):
            raise RuntimeError("cannot stringify")

    result = Unstringifiable()

    def on_result(name, arguments, ok, brief):
        events.append((name, arguments, ok, brief))

    registry = make_registry(lambda: result)
    gate = PermissionGate(PermissionPolicy({"dummy": ALLOW}))
    executor = ToolExecutor(registry, gate=gate, on_result=on_result)

    assert executor.execute("dummy", {}) is result
    assert events[0][2] is True
    assert isinstance(events[0][3], str)
    assert events[0][3] == "<unavailable>"


def test_brief_is_truncated_to_result_prefix():
    events = []
    result = "prefix-" + "x" * RESULT_BRIEF_MAX_LENGTH

    def on_result(name, arguments, ok, brief):
        events.append(brief)

    registry = make_registry(lambda: result)
    gate = PermissionGate(PermissionPolicy({"dummy": ALLOW}))
    executor = ToolExecutor(registry, gate=gate, on_result=on_result)

    assert executor.execute("dummy", {}) == result
    assert len(events) == 1
    assert events[0] == result[:RESULT_BRIEF_MAX_LENGTH]
    assert len(events[0]) == RESULT_BRIEF_MAX_LENGTH


def test_callback_exception_does_not_fail_thread_pool():
    calls = []
    calls_lock = Lock()

    def on_result(name, arguments, ok, brief):
        with calls_lock:
            calls.append(arguments["value"])
        raise RuntimeError("callback boom")

    registry = make_registry(lambda value: value)
    gate = PermissionGate(PermissionPolicy({"dummy": ALLOW}))
    executor = ToolExecutor(registry, gate=gate, on_result=on_result)

    values = list(range(12))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(
            lambda value: executor.execute("dummy", {"value": value}),
            values,
        ))

    assert results == values
    assert sorted(calls) == values


def test_concurrent_callbacks_complete_in_execution_threads():
    events = []
    handler_threads = {}
    callback_threads = {}
    lock = Lock()

    def handler(value):
        with lock:
            handler_threads[value] = get_ident()
        time.sleep(0.001)
        return value

    def on_result(name, arguments, ok, brief):
        with lock:
            value = arguments["value"]
            events.append(value)
            callback_threads[value] = get_ident()

    registry = make_registry(handler)
    gate = PermissionGate(PermissionPolicy({"dummy": ALLOW}))
    executor = ToolExecutor(registry, gate=gate, on_result=on_result)

    values = list(range(12))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(
            lambda value: executor.execute("dummy", {"value": value}),
            values,
        ))

    assert results == values
    assert len(events) == len(values)
    assert set(events) == set(values)
    assert callback_threads == handler_threads


def test_executor_without_callback_keeps_old_behavior():
    registry = make_registry(lambda: "unchanged")
    gate = PermissionGate(PermissionPolicy({"dummy": ALLOW}))
    executor = ToolExecutor(registry, gate)

    assert executor.execute("dummy", {}) == "unchanged"


def test_unknown_tool_does_not_call_callback():
    events = []

    def on_result(name, arguments, ok, brief):
        events.append((name, arguments, ok, brief))

    executor = ToolExecutor(ToolRegistry(), on_result=on_result)

    try:
        executor.execute("unknown", {})
        assert False, "未知工具应抛 ValueError"
    except ValueError:
        pass

    assert events == []


if __name__ == "__main__":
    test_success_calls_callback_once()
    test_permission_denial_calls_callback_once()
    test_handler_exception_calls_callback_once()
    test_callback_exception_does_not_change_success_result()
    test_callback_exception_does_not_change_permission_denial()
    test_callback_exception_does_not_change_handler_error()
    test_unstringifiable_result_gets_safe_brief()
    test_brief_is_truncated_to_result_prefix()
    test_callback_exception_does_not_fail_thread_pool()
    test_concurrent_callbacks_complete_in_execution_threads()
    test_executor_without_callback_keeps_old_behavior()
    test_unknown_tool_does_not_call_callback()
    print("\n全部 executor test 通过")
