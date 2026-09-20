"""The canonical parent/child Agent Runtime loop.

The runtime owns the protocol boundary between a model response and the next
model request. Parent and subagent behaviour is supplied by small policies;
policies never call a model or enter a tool handler.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import inspect
import json
from collections.abc import Mapping
from typing import Any, Callable, Protocol

from mini_agent.context import ContextManager, count_tokens
from mini_agent.providers.base import ProviderResponse, UsageMeter
from mini_agent.tools.base import (
    ExecutionResult,
    ToolAdmission,
    ToolExecutor,
    format_tool_result,
)


@dataclass(frozen=True)
class RuntimeResult:
    content: str
    stop_reason: str
    rounds: int
    llm_calls: int
    tool_calls: int
    estimated_tokens: int
    input_tokens: int = 0
    output_tokens: int = 0
    token_accounting: str = "estimated"
    model_binding_ref: Any = None


@dataclass(frozen=True)
class RuntimeDecision:
    action: str
    content: str = ""
    stop_reason: str = ""
    notice: str | None = None

    def __post_init__(self) -> None:
        if self.action not in {"continue", "finish"}:
            raise ValueError("RuntimeDecision.action 必须是 continue 或 finish")


@dataclass(frozen=True)
class ToolRoundPlan:
    serial: bool
    rejection_by_index: Mapping[int, ExecutionResult] = field(default_factory=dict)
    parallel_delegation: bool = False


@dataclass(frozen=True)
class NormalizedToolRound:
    calls: tuple[dict[str, Any], ...]
    errors_by_call_id: Mapping[str, str] = field(default_factory=dict)


class LLMMessage(dict):
    """Dict-compatible normalized message with private provider metadata."""

    def __init__(self, message: Mapping[str, Any], response: ProviderResponse | None = None):
        super().__init__(message)
        self.provider_response = response


class RuntimePolicy(Protocol):
    def before_run(self, runtime: "AgentRuntime") -> RuntimeDecision | None: ...

    def before_prepare(self, runtime: "AgentRuntime") -> RuntimeDecision | None: ...

    def before_llm(self, runtime: "AgentRuntime") -> RuntimeDecision | None: ...

    def llm_options(self, runtime: "AgentRuntime") -> dict[str, Any]: ...

    def on_text(self, runtime: "AgentRuntime", content: str) -> RuntimeDecision: ...

    def prepare_tool_round(
        self, runtime: "AgentRuntime", calls: tuple[dict[str, Any], ...]
    ) -> ToolRoundPlan: ...

    def after_tool_result(
        self, runtime: "AgentRuntime", call: dict[str, Any], execution: ExecutionResult
    ) -> str: ...

    def after_tool_round(
        self,
        runtime: "AgentRuntime",
        calls: tuple[dict[str, Any], ...],
        results: tuple[ExecutionResult, ...],
    ) -> RuntimeDecision | None: ...

    def on_round_limit(self, runtime: "AgentRuntime") -> RuntimeDecision: ...


def invoke_llm_once(
    llm_client: Callable,
    messages: list[dict[str, Any]],
    registry: Any = None,
    timeout: float | None = None,
    **options: Any,
) -> dict[str, Any]:
    """Invoke a provider once after adapting its supported keyword surface."""
    kwargs: dict[str, Any] = {
        "include_tools": True,
        "stream_output": False,
        "tool_registry": registry,
    }
    if timeout is not None:
        kwargs["timeout"] = timeout
    kwargs.update(options)
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
    response = llm_client(messages, **kwargs)
    if isinstance(response, ProviderResponse):
        return LLMMessage(response.message, response)
    message = response
    if not isinstance(message, dict):
        raise TypeError("LLM 必须返回 message dict")
    if "choices" in message and isinstance(message.get("choices"), list):
        choice = message["choices"][0] if message["choices"] else {}
        if isinstance(choice, dict) and isinstance(choice.get("message"), dict):
            message = choice["message"]
    return LLMMessage(message)


def _invalid_call(
    detail: str,
    *,
    name: str = "invalid_tool_call",
    arguments: dict[str, Any] | None = None,
) -> ExecutionResult:
    text = f"工具调用失败: {detail}"
    return ExecutionResult(
        name,
        arguments or {},
        "not_checked",
        False,
        "invalid",
        0,
        "none",
        text,
        text[:200],
        error_kind="malformed_tool_call",
    )


def normalize_tool_calls(message: dict[str, Any]) -> NormalizedToolRound:
    """Normalize every advertised call and retain one error per bad call."""
    raw_calls = message.get("tool_calls") or []
    if not isinstance(raw_calls, list):
        raw_calls = [raw_calls]
    original_ids = {
        call.get("id") for call in raw_calls
        if isinstance(call, dict)
        and isinstance(call.get("id"), str)
        and call.get("id", "").strip()
    }
    normalized: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    seen_ids: set[str] = set()
    next_local_id = 0

    for call in raw_calls:
        call_errors: list[str] = []
        function = call.get("function") if isinstance(call, dict) else None
        raw_id = call.get("id") if isinstance(call, dict) else None
        raw_type = call.get("type") if isinstance(call, dict) else None
        if not isinstance(call, dict):
            call_errors.append("tool_call 格式非法")
        elif raw_type != "function":
            call_errors.append("非法的 tool_call.type")
        if not isinstance(function, dict):
            call_errors.append("tool_call 缺少 function")
            function = {}

        raw_name = function.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            call_errors.append("非法的 function.name")
            name = "invalid_tool_call"
        else:
            name = raw_name

        raw_arguments = function.get("arguments", "{}")
        arguments_text = raw_arguments if isinstance(raw_arguments, str) else "{}"
        if isinstance(raw_arguments, str):
            try:
                arguments = json.loads(raw_arguments)
            except (TypeError, json.JSONDecodeError):
                arguments = {}
                call_errors.append("非法的 function.arguments")
        else:
            arguments = raw_arguments
            call_errors.append("非法的 function.arguments")
        if not isinstance(arguments, dict):
            arguments = {}
            if "非法的 function.arguments" not in call_errors:
                call_errors.append("非法的 function.arguments")
        if "非法的 function.arguments" in call_errors:
            arguments_text = "{}"

        if not isinstance(raw_id, str) or not raw_id.strip():
            call_errors.append("无效的 tool_call_id")
        elif raw_id in seen_ids:
            call_errors.append("重复的 tool_call_id")

        if call_errors:
            for _ in range(len(raw_calls) + next_local_id + 2):
                call_id = f"local-error-{next_local_id}"
                next_local_id += 1
                if call_id not in seen_ids and call_id not in original_ids:
                    break
            else:
                raise RuntimeError("无法生成唯一的本地 tool_call_id")
            errors[call_id] = "; ".join(call_errors)
        else:
            call_id = raw_id
        seen_ids.add(call_id)
        normalized.append({
            "id": call_id,
            "type": "function",
            "function": {
                "name": name,
                "arguments": arguments_text,
            },
        })
    return NormalizedToolRound(tuple(normalized), errors)


class AgentRuntime:
    """Own the single model → tool → observation loop for one agent."""

    def __init__(
        self,
        *,
        llm_client: Callable,
        context: ContextManager,
        executor: ToolExecutor,
        policy: RuntimePolicy,
        max_rounds: int,
        output: Any = None,
        session_boundary: Any = None,
        model_binding: Any = None,
        usage_meter: UsageMeter | None = None,
    ) -> None:
        if context is None or executor is None:
            raise TypeError("AgentRuntime 需要 context 和 executor")
        if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or max_rounds <= 0:
            raise ValueError("max_rounds 必须是正整数")
        self.llm_client = llm_client
        self.context = context
        self.executor = executor
        self.policy = policy
        self.max_rounds = max_rounds
        self.output = output
        self.session_boundary = session_boundary
        self.model_binding = model_binding or getattr(context, "model_binding", None)
        self.usage_meter = usage_meter or getattr(self.model_binding, "usage_meter", None)
        self._usage_start = self.usage_meter.snapshot() if self.usage_meter is not None else None
        self.rounds = 0
        self.llm_calls = 0
        self.tool_calls = 0
        self.estimated_tokens = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.token_accounting = "estimated"
        self.request_tokens = 0
        self.response_tokens = 0
        self.prepared_messages: list[dict[str, Any]] = []
        self.normalized: NormalizedToolRound | None = None
        self.parsed_calls: list[tuple[str, dict[str, Any]]] = []
        self.effects: list[str] = []
        self.executions: list[ExecutionResult] = []
        self.suppress_next_round_output = False
        self._streamed_content = False

    def invoke(
        self,
        messages: list[dict[str, Any]],
        timeout: float | None = None,
        **options: Any,
    ) -> dict[str, Any]:
        """Invoke this runtime's LLM with its own frozen tool surface."""
        registry = getattr(self.executor, "registry", None)
        return invoke_llm_once(
            self.llm_client, messages, registry, timeout, **options,
        )

    def _decision_result(self, decision: RuntimeDecision) -> RuntimeResult:
        self._refresh_usage()
        return RuntimeResult(
            content=str(decision.content),
            stop_reason=decision.stop_reason or decision.action,
            rounds=self.rounds,
            llm_calls=self.llm_calls,
            tool_calls=self.tool_calls,
            estimated_tokens=self.estimated_tokens,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            token_accounting=self.token_accounting,
            model_binding_ref=getattr(self.model_binding, "reference", None),
        )

    def _refresh_usage(self) -> None:
        if self.usage_meter is None or self._usage_start is None:
            self.estimated_tokens = self.input_tokens + self.output_tokens
            return
        delta = self.usage_meter.delta(self._usage_start)
        self.llm_calls = int(delta["llm_calls"])
        self.input_tokens = int(delta["input_tokens"])
        self.output_tokens = int(delta["output_tokens"])
        self.token_accounting = str(delta["token_accounting"])
        self.estimated_tokens = self.input_tokens + self.output_tokens

    def _apply_decision(self, decision: RuntimeDecision | None) -> RuntimeResult | None:
        if decision is None:
            return None
        if decision.notice is not None and hasattr(self.context, "set_runtime_notice"):
            self.context.set_runtime_notice(decision.notice)
        if decision.action == "finish":
            if self.output is not None and hasattr(self.output, "close"):
                self.output.close()
            return self._decision_result(decision)
        return None

    def _append_assistant(self, message: dict[str, Any]) -> None:
        if hasattr(self.context, "append_assistant"):
            self.context.append_assistant(message)
        else:
            self.context.history.append(message)

    def _append_tool_result(self, call_id: str, content: str) -> None:
        if hasattr(self.context, "append_tool_result"):
            self.context.append_tool_result(call_id, content)
        else:
            self.context.history.append({
                "role": "tool", "tool_call_id": call_id, "content": content,
            })

    def _parse_calls(self, calls: tuple[dict[str, Any], ...]) -> None:
        self.parsed_calls = []
        self.effects = []
        registry = getattr(self.executor, "registry", None)
        structured = registry is not None and hasattr(self.executor, "execute_result")
        for call in calls:
            function = call.get("function", {})
            try:
                arguments = json.loads(function.get("arguments", "{}"))
                if not isinstance(arguments, dict):
                    raise TypeError("tool arguments 必须是 JSON object")
            except (TypeError, json.JSONDecodeError):
                arguments = {}
            name = function.get("name", "invalid_tool_call")
            self.parsed_calls.append((name, arguments))
            try:
                effect = registry.effect_for(name, arguments) if structured else "none"
            except (TypeError, ValueError):
                effect = "none"
            self.effects.append(effect)

    @staticmethod
    def _safe_call_content(execution: ExecutionResult) -> str:
        try:
            return execution.tool_content()
        except Exception as error:
            return f"工具调用失败: {type(error).__name__}"

    @staticmethod
    def _compat_execution(name: str, arguments: dict[str, Any], value: Any) -> ExecutionResult:
        content = format_tool_result(value)
        return ExecutionResult(
            name, arguments, "not_checked", True, "succeeded", 0, "none",
            value, content[:200],
        )

    @staticmethod
    def _exception_execution(name: str, arguments: dict[str, Any], error: Exception) -> ExecutionResult:
        text = f"工具调用失败: {type(error).__name__}"
        return ExecutionResult(
            name, arguments, "not_checked", False, "failed", 0, "none",
            text, text[:200], error_kind="executor_exception",
        )

    def _execute_call(
        self,
        index: int,
        *,
        admission: ToolAdmission | None = None,
        admit_only: bool = False,
    ) -> tuple[ExecutionResult | None, ExecutionResult]:
        name, arguments = self.parsed_calls[index]
        invocation_id = f"r-{self.rounds}-c-{index}"
        structured = hasattr(self.executor, "execute_result")
        if self.session_boundary is not None and structured:
            if admission is None:
                admission = self.executor.admit(name, arguments, self.context.state)
                if isinstance(admission, ToolAdmission):
                    self.session_boundary.record_admission(
                        invocation_id, admission, self.context.state, self.context,
                    )
            if isinstance(admission, ToolAdmission):
                if admit_only:
                    return None, admission
                execution = self.executor.execute_admitted(admission, notify=False)
            else:
                execution = admission
        else:
            try:
                if structured:
                    execution = self.executor.execute_result(
                        name,
                        arguments,
                        state=getattr(self.context, "state", None),
                        notify=False,
                    )
                else:
                    return None, self._compat_execution(
                        name, arguments, self.executor.execute(name, arguments),
                    )
            except Exception as error:
                return self._exception_execution(name, arguments, error), self._exception_execution(
                    name, arguments, error,
                )
        if isinstance(execution, ToolAdmission):
            execution = self.executor.execute_admitted(execution, notify=False)
        if not isinstance(execution, ExecutionResult):
            execution = self._exception_execution(
                name, arguments, TypeError("executor 必须返回 ExecutionResult"),
            )
        return execution, execution

    def _commit_one(
        self,
        index: int,
        call: dict[str, Any],
        actual: ExecutionResult | None,
        display: ExecutionResult,
        content: str,
    ) -> None:
        structured = hasattr(self.executor, "execute_result")
        name = self.parsed_calls[index][0]
        attempt = None
        if structured and actual is not None and name != "recover":
            state = getattr(self.context, "state", None)
            if state is not None and hasattr(state, "record_execution_result"):
                attempt = state.record_execution_result(actual)
        self._append_tool_result(call["id"], content)
        if self.session_boundary is not None:
            self.session_boundary.record_execution_result(
                f"r-{self.rounds}-c-{index}",
                actual or display,
                content,
                getattr(self.context, "state", None),
                self.context,
                attempt,
            )
        state = getattr(self.context, "state", None)
        if name == "delegate_task" and state is not None and hasattr(state, "commit_delegation_tool_result"):
            # The role=tool message has been appended (and, when enabled,
            # durably recorded) before the parent lifecycle becomes committed.
            state.commit_delegation_tool_result(content)

    def _start_durable_round(self, calls: tuple[dict[str, Any], ...]) -> None:
        if self.session_boundary is None:
            return
        history = getattr(self.context, "history", [])
        round_id = sum(
            1 for item in history
            if isinstance(item, dict)
            and item.get("role") == "assistant"
            and item.get("tool_calls")
        )
        self.session_boundary.start_round(
            max(1, round_id),
            history[-1],
            [
                {
                    "invocation_id": f"r-{self.rounds}-c-{index}",
                    "tool_call_id": call["id"],
                    "tool": self.parsed_calls[index][0],
                    "arguments": self.parsed_calls[index][1],
                    "effect_class": self.effects[index],
                }
                for index, call in enumerate(calls)
            ],
            getattr(self.context, "state", None),
            self.context,
        )

    def _run_tool_call(
        self,
        index: int,
        call: dict[str, Any],
        plan: ToolRoundPlan,
    ) -> tuple[ExecutionResult | None, ExecutionResult, str]:
        if self.normalized and call["id"] in self.normalized.errors_by_call_id:
            execution = _invalid_call(
                self.normalized.errors_by_call_id[call["id"]],
                name=self.parsed_calls[index][0],
                arguments=self.parsed_calls[index][1],
            )
            content = self.policy.after_tool_result(self, call, execution)
            return execution, execution, format_tool_result(content)
        if index in plan.rejection_by_index:
            execution = plan.rejection_by_index[index]
            actual = execution if execution.error_kind in {
                "mixed_verification", "repair_phase_gate", "invalid_result",
                "budget_exhausted", "invalid_tool_call",
            } else None
            content = self.policy.after_tool_result(self, call, execution)
            return actual, execution, format_tool_result(content)
        actual, display = self._execute_call(index)
        content = self.policy.after_tool_result(self, call, display)
        if not isinstance(content, str):
            content = format_tool_result(content)
        return actual, display, content

    def _run_tool_round(
        self,
        calls: tuple[dict[str, Any], ...],
        plan: ToolRoundPlan,
    ) -> tuple[ExecutionResult, ...]:
        self._start_durable_round(calls)
        if self.output is not None and hasattr(self.output, "tools_start"):
            self.output.tools_start(calls)
        if plan.parallel_delegation:
            return self._run_parallel_delegation_round(calls, plan)
        records: list[tuple[ExecutionResult | None, ExecutionResult, str] | None] = [None] * len(calls)
        if plan.serial:
            for index, call in enumerate(calls):
                record = self._run_tool_call(index, call, plan)
                records[index] = record
                self._commit_one(index, call, *record)
        elif self.session_boundary is not None and hasattr(self.executor, "admit"):
            staged: dict[int, ToolAdmission] = {}
            immediate: dict[int, tuple[ExecutionResult | None, ExecutionResult, str]] = {}
            for index, call in enumerate(calls):
                if self.normalized and call["id"] in self.normalized.errors_by_call_id:
                    immediate[index] = self._run_tool_call(index, call, plan)
                    continue
                if index in plan.rejection_by_index:
                    immediate[index] = self._run_tool_call(index, call, plan)
                    continue
                name, arguments = self.parsed_calls[index]
                admission = self.executor.admit(name, arguments, self.context.state)
                if isinstance(admission, ToolAdmission):
                    self.session_boundary.record_admission(
                        f"r-{self.rounds}-c-{index}",
                        admission,
                        self.context.state,
                        self.context,
                    )
                    staged[index] = admission
                else:
                    immediate[index] = (
                        admission,
                        admission,
                        self.policy.after_tool_result(self, call, admission),
                    )
            with ThreadPoolExecutor(max_workers=max(1, len(staged))) as pool:
                futures = {
                    index: pool.submit(
                        self._execute_call, index, admission=admission,
                    )
                    for index, admission in staged.items()
                }
                for index, call in enumerate(calls):
                    if index in immediate:
                        record = immediate[index]
                    else:
                        actual, display = futures[index].result()
                        record = (
                            actual,
                            display,
                            self.policy.after_tool_result(self, call, display),
                        )
                    records[index] = record
                    self._commit_one(index, call, *record)
        else:
            with ThreadPoolExecutor(max_workers=max(1, len(calls))) as pool:
                futures = {
                    index: pool.submit(self._run_tool_call, index, call, plan)
                    for index, call in enumerate(calls)
                }
                for index, call in enumerate(calls):
                    record = futures[index].result()
                    records[index] = record
                    self._commit_one(index, call, *record)

        if self.output is not None and hasattr(self.output, "tool_result"):
            for call, record in zip(calls, records):
                actual, display, content = record
                function = call.get("function", {})
                rendered_execution = display
                if actual is None and not hasattr(self.executor, "execute_result"):
                    rendered_execution = None
                self.output.tool_result(
                    function.get("name", "<missing>"),
                    function.get("arguments", "{}"),
                    content,
                    rendered_execution,
                )
            if hasattr(self.output, "close"):
                self.output.close()
        if self.session_boundary is not None:
            self.session_boundary.complete_round(
                getattr(self.context, "state", None), self.context,
            )
        self.executions = [record[1] for record in records]
        self.tool_calls += len(calls)
        return tuple(self.executions)

    def _run_parallel_delegation_round(
        self,
        calls: tuple[dict[str, Any], ...],
        plan: ToolRoundPlan,
    ) -> tuple[ExecutionResult, ...]:
        """Run a pure delegate batch through its dedicated scheduler.

        Admission and durable ``handler_admitted`` commits stay in model
        order.  Only the frozen child workers may complete out of order; the
        callback below is invoked by the scheduler for the next ready prefix.
        """
        records: list[tuple[ExecutionResult | None, ExecutionResult, str] | None] = [None] * len(calls)
        admissions: dict[int, ToolAdmission] = {}
        immediate: dict[int, tuple[ExecutionResult | None, ExecutionResult, str]] = {}
        for index, call in enumerate(calls):
            if self.normalized and call["id"] in self.normalized.errors_by_call_id:
                immediate[index] = self._run_tool_call(index, call, plan)
                continue
            if index in plan.rejection_by_index:
                immediate[index] = self._run_tool_call(index, call, plan)
                continue
            name, arguments = self.parsed_calls[index]
            admission = self.executor.admit(name, arguments, self.context.state)
            if isinstance(admission, ToolAdmission):
                if self.session_boundary is not None:
                    self.session_boundary.record_admission(
                        f"r-{self.rounds}-c-{index}", admission,
                        self.context.state, self.context,
                    )
                admissions[index] = admission
            else:
                content = self.policy.after_tool_result(self, call, admission)
                if not isinstance(content, str):
                    content = format_tool_result(content)
                immediate[index] = (admission, admission, content)

        manager = getattr(getattr(self.executor, "registry", None), "_delegation_manager", None)
        if manager is None or not hasattr(manager, "prepare_batch"):
            # This should only be reachable for a custom registry.  Preserve
            # the normal executor behavior rather than bypassing a handler.
            for index, call in enumerate(calls):
                if index not in immediate:
                    admission = admissions[index]
                    execution = self.executor.execute_admitted(admission, notify=False)
                    content = self.policy.after_tool_result(self, call, execution)
                    if not isinstance(content, str):
                        content = format_tool_result(content)
                    immediate[index] = (execution, execution, content)
            for index, call in enumerate(calls):
                record = immediate[index]
                records[index] = record
                self._commit_one(index, call, *record)
        else:
            tasks, ready, rejected_tasks = manager.prepare_batch(admissions, self.context.state)

            def commit_ready(index: int, result: Any) -> None:
                if isinstance(result, tuple) and len(result) == 3:
                    record = result
                elif index in admissions:
                    admission = admissions[index]
                    execution = self.executor.execute_admitted_delegation(admission, result)
                    call = calls[index]
                    content = self.policy.after_tool_result(self, call, execution)
                    if not isinstance(content, str):
                        content = format_tool_result(content)
                    record = (execution, execution, content)
                else:
                    # A contract failure happened after ToolExecutor's normal
                    # admission boundary.  Keep a unique bounded tool result,
                    # but do not claim that its handler ran.
                    name, arguments = self.parsed_calls[index]
                    content = result.to_json() if hasattr(result, "to_json") else format_tool_result(result)
                    execution = ExecutionResult(
                        name, arguments, "not_checked", False, "invalid", 0,
                        self.effects[index], content, content[:200],
                        error_kind=getattr(result, "error_kind", "invalid_contract"),
                    )
                    content = self.policy.after_tool_result(self, calls[index], execution)
                    record = (None, execution, format_tool_result(content))
                records[index] = record
                self._commit_one(index, calls[index], *record)

            # Immediate results are safe to deliver only through the same
            # ordered prefix.  The scheduler includes them in its ready map.
            for index, record in immediate.items():
                records[index] = record
            all_ready = dict(ready)
            all_ready.update(immediate)
            manager.run_prepared_batch(
                tasks, self.context.state, ready=all_ready,
                rejected_tasks=rejected_tasks,
                on_result=commit_ready,
            )
            if any(record is None for record in records):
                raise RuntimeError("委派调度器未返回对应结果")

        if self.output is not None and hasattr(self.output, "tool_result"):
            for call, record in zip(calls, records):
                actual, display, content = record
                function = call.get("function", {})
                self.output.tool_result(
                    function.get("name", "<missing>"),
                    function.get("arguments", "{}"), content, display,
                )
            if hasattr(self.output, "close"):
                self.output.close()
        if self.session_boundary is not None:
            self.session_boundary.complete_round(
                getattr(self.context, "state", None), self.context,
            )
        self.executions = [record[1] for record in records]
        self.tool_calls += len(calls)
        return tuple(self.executions)

    def run(self) -> RuntimeResult:
        early = self._apply_decision(self.policy.before_run(self))
        if early is not None:
            return early
        while self.rounds < self.max_rounds:
            early = self._apply_decision(self.policy.before_prepare(self))
            if early is not None:
                return early
            self.prepared_messages = self.context.prepare_messages()
            self._refresh_usage()
            self.request_tokens = count_tokens(self.prepared_messages)
            early = self._apply_decision(self.policy.before_llm(self))
            if early is not None:
                return early
            self.rounds += 1
            self.llm_calls += 1
            if (not self.suppress_next_round_output and self.output is not None
                    and hasattr(self.output, "round_start")):
                self.output.round_start(self.rounds)
            self.suppress_next_round_output = False
            self._streamed_content = False
            # Count the request before invoking the provider so timeout and
            # provider-error results still report consumed input tokens.
            self.input_tokens += self.request_tokens
            self.estimated_tokens = self.input_tokens + self.output_tokens
            meter_before = self.usage_meter.snapshot() if self.usage_meter is not None else None
            try:
                message = self.invoke(
                    self.prepared_messages,
                    **self.policy.llm_options(self),
                )
            except Exception:
                if (self.usage_meter is not None and meter_before is not None
                        and self.usage_meter.snapshot()["llm_calls"] == meter_before["llm_calls"]):
                    self.usage_meter.record(
                        None, estimated_input_tokens=self.request_tokens,
                    )
                self._refresh_usage()
                raise
            self.response_tokens = count_tokens(message)
            if (self.usage_meter is not None and meter_before is not None
                    and self.usage_meter.snapshot()["llm_calls"] == meter_before["llm_calls"]):
                self.usage_meter.record(
                    None,
                    estimated_input_tokens=self.request_tokens,
                    estimated_output_tokens=self.response_tokens,
                )
            if self.usage_meter is None:
                self.output_tokens += self.response_tokens
            self._refresh_usage()
            if (not self._streamed_content and message.get("content")
                    and self.output is not None and hasattr(self.output, "assistant_delta")):
                self.output.assistant_delta(message["content"])
            if self.output is not None and hasattr(self.output, "assistant_end"):
                self.output.assistant_end()

            after_llm = getattr(self.policy, "after_llm", None)
            if after_llm is not None:
                early = self._apply_decision(after_llm(self, message))
                if early is not None:
                    return early

            normalized = normalize_tool_calls(message)
            self.normalized = normalized
            assistant = dict(message)
            assistant["tool_calls"] = list(normalized.calls)
            if not normalized.calls:
                assistant.pop("tool_calls", None)
            self._append_assistant(assistant)
            if not normalized.calls:
                decision = self.policy.on_text(
                    self, message.get("content", "") or "",
                )
                early = self._apply_decision(decision)
                if early is not None:
                    return early
                continue

            self._parse_calls(normalized.calls)
            plan = self.policy.prepare_tool_round(self, normalized.calls)
            if not isinstance(plan, ToolRoundPlan):
                raise TypeError("RuntimePolicy.prepare_tool_round 必须返回 ToolRoundPlan")
            self._run_tool_round(normalized.calls, plan)
            decision = self.policy.after_tool_round(
                self, normalized.calls, tuple(self.executions),
            )
            early = self._apply_decision(decision)
            if early is not None:
                return early

        decision = self.policy.on_round_limit(self)
        if decision.action != "finish":
            decision = RuntimeDecision(
                "finish", decision.content, decision.stop_reason or "round_limit",
            )
        return self._apply_decision(decision) or self._decision_result(decision)
