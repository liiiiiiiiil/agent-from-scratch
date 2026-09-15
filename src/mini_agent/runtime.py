"""Reusable runtime shell for parent and child agent loops.

The v0.33 parent implementation remains available as an injected loop
implementation so its durable/process/repair semantics stay compatible while
callers can now instantiate a runtime.  The generic path keeps the shared
tool-call protocol for small integrations.
"""

from __future__ import annotations

import inspect
import json
from typing import Any, Callable

from mini_agent.context import ContextManager
from mini_agent.tools.base import ExecutionResult, ToolExecutor


def invoke_llm_once(llm_client: Callable, messages: list[dict[str, Any]],
                    registry: Any = None, timeout: float | None = None) -> dict[str, Any]:
    """Invoke a provider once after adapting its supported keyword surface.

    Signature inspection happens before the request.  A ``TypeError`` raised
    by the provider is never treated as a probe, so the same model request
    cannot be resent and silently consume another budget unit.
    """
    kwargs = {
        "include_tools": True,
        "stream_output": False,
        "tool_registry": registry,
    }
    if timeout is not None:
        kwargs["timeout"] = timeout
    try:
        signature = inspect.signature(llm_client)
    except (TypeError, ValueError):
        signature = None
    if signature is not None:
        parameters = signature.parameters
        if not any(item.kind == inspect.Parameter.VAR_KEYWORD
                   for item in parameters.values()):
            kwargs = {name: value for name, value in kwargs.items()
                      if name in parameters}
    message = llm_client(messages, **kwargs)
    if not isinstance(message, dict):
        raise TypeError("LLM 必须返回 message dict")
    if "choices" in message and isinstance(message.get("choices"), list):
        choice = message["choices"][0] if message["choices"] else {}
        if isinstance(choice, dict) and isinstance(choice.get("message"), dict):
            message = choice["message"]
    return message


def normalize_tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize a provider tool-call message without executing anything."""
    calls = message.get("tool_calls") or []
    if not isinstance(calls, list):
        raise ValueError("tool_calls 必须是列表")
    result = []
    seen: set[str] = set()
    for index, call in enumerate(calls):
        if not isinstance(call, dict) or call.get("type") != "function":
            raise ValueError("tool_call 格式非法")
        function = call.get("function")
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            raise ValueError("tool_call function 格式非法")
        call_id = str(call.get("id") or f"local-call-{index}")
        if call_id in seen:
            raise ValueError("tool_call_id 重复")
        seen.add(call_id)
        raw_arguments = function.get("arguments", "{}")
        if isinstance(raw_arguments, str):
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError as error:
                raise ValueError("tool_call arguments 不是 JSON") from error
        else:
            arguments = raw_arguments
        if not isinstance(arguments, dict):
            raise ValueError("tool_call arguments 必须是对象")
        result.append({
            "id": call_id,
            "type": "function",
            "function": {
                "name": function["name"],
                "arguments": json.dumps(arguments, ensure_ascii=False),
            },
        })
    return result


class AgentRuntime:
    """An injectable runtime shell with the common tool-call protocol."""

    def __init__(self, llm_client: Callable,
                 context: ContextManager | None = None,
                 executor: ToolExecutor | None = None,
                 loop_policy: Any = None,
                 output: Any = None,
                 *, context_manager: ContextManager | None = None,
                 tool_executor: ToolExecutor | None = None,
                 max_rounds: int = 50,
                 completion_policy: Callable[[Any, str], bool] | None = None,
                 output_adapter: Any = None,
                 loop_impl: Callable[[ContextManager, ToolExecutor], str] | None = None):
        self.llm_client = llm_client
        self.context = context if context is not None else context_manager
        self.executor = executor if executor is not None else tool_executor
        if self.context is None or self.executor is None:
            raise TypeError("AgentRuntime 需要 context 和 executor")
        if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or max_rounds <= 0:
            raise ValueError("max_rounds 必须是正整数")
        self.max_rounds = max_rounds
        self.loop_policy = loop_policy
        self.completion_policy = completion_policy
        self.output = output if output is not None else output_adapter
        self.loop_impl = loop_impl

    def run(self) -> str:
        if self.loop_impl is not None:
            return self.loop_impl(self.context, self.executor)
        return self._run_common()

    def invoke(self, messages: list[dict[str, Any]],
               timeout: float | None = None) -> dict[str, Any]:
        """Invoke this runtime's LLM with its own frozen tool surface."""
        registry = getattr(self.executor, "registry", None)
        return invoke_llm_once(self.llm_client, messages, registry, timeout)

    def _call(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        return self.invoke(messages)

    def _run_common(self) -> str:
        for _ in range(self.max_rounds):
            message = self._call(self.context.prepare_messages())
            calls = normalize_tool_calls(message)
            assistant = {"role": "assistant", "content": message.get("content")}
            if calls:
                assistant["tool_calls"] = calls
            self.context.history.append(assistant)
            if not calls:
                content = message.get("content", "") or ""
                if self.completion_policy is None or self.completion_policy(self.context.state, content):
                    return str(content)
                if hasattr(self.context, "set_runtime_notice"):
                    self.context.set_runtime_notice("请调用推进任务的工具，不能只输出文本结束。")
                continue
            for call in calls:
                arguments = json.loads(call["function"]["arguments"])
                result = self.executor.execute_result(
                    call["function"]["name"], arguments,
                    state=getattr(self.context, "state", None), notify=False,
                )
                if isinstance(result, ExecutionResult):
                    state = getattr(self.context, "state", None)
                    if state is not None and hasattr(state, "record_execution_result"):
                        state.record_execution_result(result)
                    content = result.tool_content()
                else:
                    content = str(result)
                self.context.history.append({
                    "role": "tool", "tool_call_id": call["id"], "content": content,
                })
        return "任务达到最大运行轮次"
