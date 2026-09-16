"""带工具的 Agent 入口与父 Agent 策略。"""

import hashlib
import http.client
import json
import math
from urllib.parse import urlparse

from mini_agent.config import BASE_URL, API_KEY, MODEL, MAX_ITERATIONS, OUTPUT_MODE
from mini_agent.context import ContextManager
from mini_agent.output import TerminalOutput
from mini_agent.runtime import AgentRuntime, RuntimeDecision, ToolRoundPlan
from mini_agent.state import canonical_arguments_hash
from mini_agent.tools import registry
from mini_agent.tools.base import ExecutionResult, ToolExecutor, validate_arguments


_MAX_PROVIDER_ERROR_LENGTH = 1000


class LLMResponseError(RuntimeError):
    """Raised when the provider returns an unusable or error response."""


def _response_text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _clip_provider_detail(value):
    text = " ".join(_response_text(value).split())
    if len(text) <= _MAX_PROVIDER_ERROR_LENGTH:
        return text
    return text[: _MAX_PROVIDER_ERROR_LENGTH - 1] + "…"


def _provider_error_detail(payload):
    """Extract a bounded, human-readable error from a provider payload."""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict):
        for key in ("message", "detail", "error"):
            value = error.get(key)
            if value:
                return _clip_provider_detail(value)
        if error:
            return _clip_provider_detail(error)
    elif error:
        return _clip_provider_detail(error)
    for key in ("message", "detail"):
        value = payload.get(key)
        if value:
            return _clip_provider_detail(value)
    return ""


def _provider_error_from_body(body):
    text = _response_text(body)
    if not text.strip():
        return ""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return _clip_provider_detail(text)
    return _provider_error_detail(payload) or _clip_provider_detail(text)


def _safe_print(*args, **kwargs):
    """Best-effort observation output that cannot break agent execution."""
    try:
        print(*args, **kwargs)
    except Exception:
        pass


def _recovery_rejection_content(state, arguments, detail):
    """Record one identifiable rejected recover call and return its result."""
    if state is None or not hasattr(state, "reject_recovery"):
        return json.dumps({"status": "rejected", "message": str(detail)[:1200]}, ensure_ascii=False)
    arguments = arguments if isinstance(arguments, dict) else {}
    record = state.reject_recovery(
        arguments.get("action", "block"),
        arguments.get("caused_by_failure_id", "<missing>"),
        arguments.get("reason", ""),
        str(detail),
        arguments.get("requested_attempt"),
        arguments.get("requested_tool"),
        arguments.get("requested_arguments"),
        arguments.get("checkpoint_id"),
    )
    payload = {
        "status": "rejected",
        "recovery_id": record.recovery_id,
        "message": str(detail)[:1200],
    }
    if getattr(state, "status", None) in ("blocked", "failed"):
        payload["error_kind"] = "task_terminal"
    return json.dumps(payload, ensure_ascii=False)


_PLAN_CONTROL_TOOLS = {
    "begin_plan", "cancel_planning", "commit_plan", "update_plan_progress",
    "request_replan",
}
_STAGNATION_EXCLUDED_TOOLS = _PLAN_CONTROL_TOOLS | {"recover", "rollback_checkpoint"}


def _normalized_action_arguments(tool_registry, name, arguments):
    try:
        return validate_arguments(tool_registry.get(name).parameters, arguments)
    except (TypeError, ValueError):
        return arguments


def _round_fingerprint(tool_registry, parsed_calls):
    parts = [
        (name, canonical_arguments_hash(
            _normalized_action_arguments(tool_registry, name, arguments),
        ))
        for name, arguments in parsed_calls
    ]
    encoded = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _stable_observation_hash(execution):
    """Hash a successful read-only result without copying it into State."""
    def scrub(value):
        if isinstance(value, dict):
            return {
                key: scrub(item) for key, item in value.items()
                if key not in {"attempt_id", "duration_ms", "elapsed_ms", "timing_ms"}
            }
        if isinstance(value, (list, tuple)):
            return [scrub(item) for item in value]
        return value

    payload = {
        "tool": execution.tool,
        "outcome": execution.outcome,
        "output": scrub(execution.output),
        "exit_code": execution.exit_code,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def call_llm(messages, include_tools=True, stream_output=None, tool_registry=None,
             on_content=None, timeout=None):
    """流式调用 LLM，使用标准库 HTTP 和 ``Accept-Encoding: identity``。"""
    if stream_output is None:
        stream_output = OUTPUT_MODE != "quiet"
    request_timeout = 120 if timeout is None else timeout
    if (isinstance(request_timeout, bool)
            or not isinstance(request_timeout, (int, float))
            or not math.isfinite(request_timeout)
            or request_timeout <= 0):
        raise ValueError("timeout 必须是正数")
    p = urlparse(BASE_URL)
    if p.scheme == "https":
        conn = http.client.HTTPSConnection(p.hostname, p.port or 443, timeout=request_timeout)
    else:
        conn = http.client.HTTPConnection(p.hostname, p.port or 80, timeout=request_timeout)
    request_body = {"model": MODEL, "messages": messages, "stream": True}
    if include_tools:
        request_body["tools"] = (
            tool_registry if tool_registry is not None else registry
        ).schemas()
    body = json.dumps(request_body, ensure_ascii=False).encode()
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        "Accept-Encoding": "identity",
        "Accept": "text/event-stream",
    }
    conn.request("POST", f"{p.path.rstrip('/')}/chat/completions", body=body, headers=headers)
    resp = conn.getresponse()

    status = getattr(resp, "status", None)
    if isinstance(status, int) and not 200 <= status < 300:
        try:
            detail = _provider_error_from_body(resp.read())
        finally:
            conn.close()
        reason = _clip_provider_detail(getattr(resp, "reason", ""))
        message = f"服务商 HTTP {status}"
        if reason:
            message += f" {reason}"
        if detail:
            message += f": {detail}"
        raise LLMResponseError(message)

    content_parts = []
    tool_calls_acc = {}
    non_sse_parts = []
    saw_sse_event = False
    stream_error = ""

    for raw in resp:
        line = raw.decode("utf-8").strip()
        if not line:
            continue
        if not line.startswith("data:"):
            if len(non_sse_parts) < 8:
                non_sse_parts.append(line)
            continue
        saw_sse_event = True
        if line == "data: [DONE]":
            break
        chunk = json.loads(line[6:])
        stream_error = stream_error or _provider_error_detail(chunk)
        choices = chunk.get("choices", [])
        if not choices:
            continue
        delta = choices[0].get("delta", {})
        if delta.get("content"):
            content = delta["content"]
            content_parts.append(content)
            if stream_output:
                if on_content is None:
                    _safe_print(content, end="", flush=True)
                else:
                    try:
                        on_content(content)
                    except Exception:
                        pass
        for tc in delta.get("tool_calls") or []:
            if not isinstance(tc, dict):
                idx = len(tool_calls_acc)
                while idx in tool_calls_acc:
                    idx += 1
                slot = tool_calls_acc.setdefault(idx, {
                    "id": "", "type": "function",
                    "function": {"name": "", "arguments": ""},
                })
                slot["function"] = tc
                continue
            idx = tc.get("index", 0)
            try:
                hash(idx)
            except TypeError:
                idx = len(tool_calls_acc)
                while idx in tool_calls_acc:
                    idx += 1
            slot = tool_calls_acc.setdefault(idx, {
                "id": "", "type": "function",
                "function": {"name": "", "arguments": ""},
            })
            if tc.get("id"):
                slot["id"] = tc["id"]
            if "function" not in tc:
                continue
            fn = tc["function"]
            if not isinstance(fn, dict):
                slot["function"] = fn
                continue
            if not isinstance(slot.get("function"), dict):
                continue
            if "name" in fn:
                slot["function"]["name"] = fn["name"]
            if "arguments" in fn:
                raw_arguments = fn["arguments"]
                if isinstance(raw_arguments, str):
                    existing_arguments = slot["function"].get("arguments", "")
                    if isinstance(existing_arguments, str):
                        slot["function"]["arguments"] += raw_arguments
                else:
                    slot["function"]["arguments"] = raw_arguments

    conn.close()
    if stream_error:
        raise LLMResponseError(f"服务商返回错误：{stream_error}")
    if not saw_sse_event:
        body = "\n".join(non_sse_parts)
        detail = _provider_error_from_body(body)
        if detail:
            raise LLMResponseError(f"服务商返回了非 SSE 响应：{detail}")
        raise LLMResponseError("服务商返回了无法解析的响应：预期 SSE data: 事件")

    message = {"role": "assistant", "content": "".join(content_parts) or None}
    if tool_calls_acc:
        message["tool_calls"] = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
    return message


def summarize_messages(messages):
    """Summarize context without tool schemas or terminal streaming."""
    return call_llm(messages, include_tools=False, stream_output=False).get("content", "") or ""


class ParentRuntimePolicy:
    """Parent-only state, process, planning, repair, and completion policy."""

    def __init__(self) -> None:
        self.reminded_progress_marker = None
        self.legacy_reminded = False

    @staticmethod
    def _terminal_state_result(state):
        status = getattr(state, "status", None) if state is not None else None
        if status == "blocked":
            return f"任务已阻塞：{getattr(state, 'terminal_reason', '') or '任务已阻塞'}"
        if status == "failed":
            return f"任务已失败：{getattr(state, 'terminal_reason', '') or '任务已失败'}"
        return None

    @staticmethod
    def _sync_processes(runtime):
        context = runtime.context
        executor = runtime.executor
        state = getattr(context, "state", None)
        manager = getattr(getattr(executor, "registry", None), "_process_manager", None)
        if state is None or manager is None or not hasattr(state, "sync_processes"):
            return
        task_id = getattr(state, "task_id", "")
        if not task_id:
            return
        facts = manager.sync_processes(task_id)
        state.sync_processes(facts)
        if runtime.session_boundary is not None and facts:
            runtime.session_boundary.persist_process_sync(state, context)

    def before_run(self, runtime):
        state = getattr(runtime.context, "state", None)
        if (state is not None and
                getattr(getattr(state, "planning_state", None), "phase", None) == "awaiting_approval"):
            return RuntimeDecision("finish", "计划等待用户决定", "awaiting_approval")
        return None

    def before_prepare(self, runtime):
        state = getattr(runtime.context, "state", None)
        status_before_sync = getattr(state, "status", None)
        self._sync_processes(runtime)
        terminal = self._terminal_state_result(state)
        if (terminal is not None
                and status_before_sync not in ("blocked", "failed")):
            return RuntimeDecision("finish", terminal, getattr(state, "status", "terminal"))
        return None

    def before_llm(self, runtime):
        return None

    def llm_options(self, runtime):
        options = {"stream_output": True}
        if runtime.output is not None and hasattr(runtime.output, "assistant_delta"):
            options["on_content"] = runtime.output.assistant_delta
        return options

    @staticmethod
    def _rejection(runtime, index, text, error_kind, *, wrapped=None):
        name, arguments = runtime.parsed_calls[index]
        if wrapped is not None:
            text = json.dumps(wrapped, ensure_ascii=False)
        return ExecutionResult(
            name, arguments, "not_checked", False, "invalid", 0,
            runtime.effects[index], text, text[:200], error_kind=error_kind,
        )

    def on_text(self, runtime, content):
        state = getattr(runtime.context, "state", None)
        self._sync_processes(runtime)
        terminal = self._terminal_state_result(state)
        if terminal is not None:
            return RuntimeDecision("finish", terminal, getattr(state, "status", "terminal"))
        if (state is not None and hasattr(state, "active_process_records")
                and state.active_process_records()):
            state.enter_awaiting_process("still_running")
            return RuntimeDecision("finish", content, "awaiting_process")
        if (state is not None
                and getattr(getattr(state, "planning_state", None), "phase", None) == "exploring"
                and getattr(state, "repair_phase", "idle") == "idle"
                and getattr(state, "user_plan_decisions", None)
                and state.user_plan_decisions[-1].decision == "continue_exploring"
                and state.user_plan_decisions[-1].revision_id == state.planning_state.active_revision_id):
            return RuntimeDecision("finish", content, "continue_exploring")
        reminder = state.completion_reminder() if state is not None and hasattr(state, "completion_reminder") else None
        if reminder:
            if "progress_marker" not in reminder:
                if self.legacy_reminded:
                    if state is not None:
                        state.status = "blocked"
                    return RuntimeDecision("finish", content, "blocked")
                self.legacy_reminded = True
            else:
                marker = reminder.get("progress_marker")
                if marker == self.reminded_progress_marker:
                    if state is not None:
                        state.status = "blocked"
                    return RuntimeDecision("finish", content, "blocked")
                self.reminded_progress_marker = marker
            # A Runtime Notice retry is an internal continuation of the same
            # round from the terminal user's point of view.
            runtime.suppress_next_round_output = True
            return RuntimeDecision(
                "continue",
                notice=str(reminder.get(
                    "message",
                    "请在下一条回复中调用推进任务的工具；确实无法继续时说明具体阻塞原因。",
                )),
            )
        return RuntimeDecision("finish", content, "text")

    def prepare_tool_round(self, runtime, calls):
        parsed_calls = runtime.parsed_calls
        effects = runtime.effects
        state = getattr(runtime.context, "state", None)
        process_observation_tools = {"get_process", "read_process", "list_processes", "wait_process"}
        has_possible = "possible" in effects or any(name == "recover" for name, _ in parsed_calls)
        has_serial_plan_write = any(name in _PLAN_CONTROL_TOOLS for name, _ in parsed_calls)
        has_serial_process_observation = any(name in process_observation_tools for name, _ in parsed_calls)
        rejections = {}

        if len(parsed_calls) != 1 and any(name == "wait_process" for name, _ in parsed_calls):
            for index in range(len(parsed_calls)):
                rejections[index] = self._rejection(
                    runtime, index,
                    "工具调用拒绝: wait_process 必须独占一个工具回合",
                    "wait_batch_gate",
                    wrapped={"status": "error", "error_kind": "wait_batch_gate",
                             "message": "工具调用拒绝: wait_process 必须独占一个工具回合"},
                )

        planning_phase = getattr(getattr(state, "planning_state", None), "phase", "direct")
        plan_controls = {"begin_plan", "cancel_planning"}
        if (len(parsed_calls) != 1 and
                (any(name in plan_controls for name, _ in parsed_calls)
                 or any(name == "request_replan" for name, _ in parsed_calls)
                 or (planning_phase == "exploring"
                     and any(name == "commit_plan" for name, _ in parsed_calls)))):
            detail = (
                "工具调用拒绝: request_replan、规划阶段切换或 exploring 中的 "
                "commit_plan 必须独占一个工具回合"
            )
            for index in range(len(parsed_calls)):
                if index not in rejections:
                    name = parsed_calls[index][0]
                    if name in _PLAN_CONTROL_TOOLS:
                        rejections[index] = self._rejection(
                            runtime, index,
                            json.dumps({"status": "plan_rejected", "message": detail}, ensure_ascii=False),
                            "plan_rejected",
                        )
                    else:
                        rejections[index] = self._rejection(runtime, index, detail, "planning_phase_gate")

        if any(name == "delegate_task" for name, _ in parsed_calls) and len(parsed_calls) != 1:
            detail = "工具调用拒绝: delegate_task 必须独占一个工具回合"
            for index in range(len(parsed_calls)):
                if index not in rejections:
                    rejections[index] = self._rejection(
                        runtime, index,
                        json.dumps({"status": "error", "error_kind": "delegation_batch_gate",
                                    "message": detail}, ensure_ascii=False),
                        "delegation_batch_gate",
                    )

        has_other_possible = any(
            effect == "possible" and not (
                name == "run_shell" and args.get("purpose", "execution") == "verification"
            )
            for (name, args), effect in zip(parsed_calls, effects)
        )
        invalid_verifications = {
            index for index, (name, args) in enumerate(parsed_calls)
            if has_other_possible and name == "run_shell"
            and args.get("purpose", "execution") == "verification"
        }
        for index in invalid_verifications:
            if index not in rejections:
                rejections[index] = self._rejection(
                    runtime, index,
                    "工具调用失败: verification 不能与 possible effect 处于同一回合",
                    "mixed_verification",
                )

        repair_phase = getattr(state, "repair_phase", "idle") if state is not None else "idle"
        recovery_indexes = [index for index, (name, _) in enumerate(parsed_calls) if name == "recover"]
        if repair_phase == "diagnosis_required" and recovery_indexes and len(parsed_calls) != 1:
            detail = "工具调用失败: diagnosis_required 阶段的 recover 必须独占一个工具回合"
            for index in range(len(parsed_calls)):
                if index not in rejections:
                    kind = "recovery_rejected" if parsed_calls[index][0] == "recover" else "repair_phase_gate"
                    rejections[index] = self._rejection(runtime, index, detail, kind)
        elif repair_phase == "verification_required":
            valid_single = (
                len(parsed_calls) == 1
                and parsed_calls[0][0] == "run_shell"
                and parsed_calls[0][1].get("purpose", "execution") == "verification"
            )
            if not valid_single:
                detail = "工具调用失败: verification_required 阶段下一工具回合只能是单个独立 verification"
                for index in range(len(parsed_calls)):
                    if index not in rejections:
                        kind = "plan_rejected" if parsed_calls[index][0] in _PLAN_CONTROL_TOOLS else (
                            "recovery_rejected" if parsed_calls[index][0] == "recover" else "repair_phase_gate"
                        )
                        rejections[index] = self._rejection(runtime, index, detail, kind)
        return ToolRoundPlan(
            serial=has_possible or has_serial_plan_write or has_serial_process_observation,
            rejection_by_index=rejections,
        )

    def after_tool_result(self, runtime, call, execution):
        name, arguments = runtime.parsed_calls[next(
            index for index, candidate in enumerate(runtime.normalized.calls)
            if candidate["id"] == call["id"]
        )]
        if name == "recover":
            if execution.error_kind == "recovery_rejected":
                try:
                    payload = json.loads(execution.output)
                except (TypeError, ValueError):
                    payload = None
                if isinstance(payload, dict) and payload.get("recovery_id"):
                    return execution.tool_content()
                return _recovery_rejection_content(runtime.context.state, arguments, execution.output_excerpt)
            if execution.error_kind == "malformed_tool_call":
                return _recovery_rejection_content(runtime.context.state, arguments, execution.output_excerpt)
            if execution.error_kind not in ("task_terminal",) and execution.outcome != "succeeded":
                return _recovery_rejection_content(runtime.context.state, arguments, execution.output_excerpt)
        return execution.tool_content()

    def after_tool_round(self, runtime, calls, results):
        state = getattr(runtime.context, "state", None)
        self._sync_processes(runtime)
        terminal = self._terminal_state_result(state)
        if terminal is not None:
            return RuntimeDecision("finish", terminal, getattr(state, "status", "terminal"))
        if (len(runtime.parsed_calls) == 1 and runtime.parsed_calls[0][0] == "wait_process"
                and results and results[0].outcome == "succeeded"):
            try:
                wait_result = json.loads(results[0].output)
            except (TypeError, ValueError):
                wait_result = {}
            if (wait_result.get("reason") == "still_running" and state is not None
                    and hasattr(state, "active_process_records") and state.active_process_records()):
                state.enter_awaiting_process("still_running")
                return RuntimeDecision(
                    "finish", "后台进程或 stdin 写入仍未收束；可在 CLI 继续当前任务。",
                    "awaiting_process",
                )
        if (state is not None and hasattr(state, "observe_tool_round")
                and getattr(runtime.executor, "registry", None) is not None
                and hasattr(runtime.executor, "execute_result")):
            tool_registry = getattr(runtime.executor, "registry", None)
            action_fingerprint = _round_fingerprint(tool_registry, runtime.parsed_calls)
            observations = []
            effect_actions = []
            for execution in results:
                if (execution.outcome != "succeeded" or not execution.handler_admitted
                        or execution.permission != "allowed"):
                    continue
                is_verification = (
                    execution.tool == "run_shell"
                    and execution.arguments.get("purpose", "execution") == "verification"
                )
                if execution.effect_class == "none" and not is_verification:
                    if execution.tool not in _STAGNATION_EXCLUDED_TOOLS:
                        observations.append(_stable_observation_hash(execution))
                elif execution.effect_class == "possible" and not is_verification:
                    if execution.tool not in _STAGNATION_EXCLUDED_TOOLS:
                        effect_actions.append(canonical_arguments_hash({
                            "tool": execution.tool, "arguments": execution.arguments,
                        }))
            observation = state.observe_tool_round(
                action_fingerprint, observations, effect_actions,
                tuple(name for name, _ in runtime.parsed_calls),
            )
            if observation.get("warning") and hasattr(runtime.context, "set_runtime_notice"):
                runtime.context.set_runtime_notice(str(observation["warning"]))
            if observation.get("blocked"):
                reason = observation.get("terminal_reason") or getattr(state, "terminal_reason", "")
                return RuntimeDecision("finish", f"任务已阻塞：{reason}", "blocked")
            terminal = self._terminal_state_result(state)
            if terminal is not None:
                return RuntimeDecision("finish", terminal, getattr(state, "status", "terminal"))
        if (state is not None
                and getattr(getattr(state, "planning_state", None), "phase", None) == "awaiting_approval"):
            return RuntimeDecision("finish", "计划等待用户决定", "awaiting_approval")
        return None

    def on_round_limit(self, runtime):
        state = getattr(runtime.context, "state", None)
        if state is not None and getattr(state, "status", None) not in ("blocked", "failed"):
            state.status = "failed"
        return RuntimeDecision("finish", "达到最大迭代次数", "round_limit")


def agent_loop(context_manager: ContextManager, tool_executor: ToolExecutor):
    """Compatibility entry point assembled through the canonical Runtime."""
    output = TerminalOutput(OUTPUT_MODE)
    runtime = AgentRuntime(
        llm_client=lambda messages, **kwargs: call_llm(messages, **kwargs),
        context=context_manager,
        executor=tool_executor,
        policy=ParentRuntimePolicy(),
        max_rounds=MAX_ITERATIONS,
        output=output,
        session_boundary=getattr(tool_executor, "session_boundary", None),
    )
    return runtime.run().content
