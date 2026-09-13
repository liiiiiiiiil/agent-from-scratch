"""带工具的 Agent Loop（流式）：调 LLM -> 若要工具则执行 -> 结果回灌 -> 再调，循环到纯文本回复或上限。"""

import http.client
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from mini_agent.context import ContextManager
from mini_agent.output import TerminalOutput
from mini_agent.tools import registry
from mini_agent.tools.base import ExecutionResult, ToolExecutor
from mini_agent.state import canonical_arguments_hash
from mini_agent.tools.base import validate_arguments
from mini_agent.config import BASE_URL, API_KEY, MODEL, MAX_ITERATIONS, OUTPUT_MODE


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
    """Record one identifiable rejected recover call and return its tool result."""
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
_STAGNATION_EXCLUDED_TOOLS = _PLAN_CONTROL_TOOLS | {
    "recover", "rollback_checkpoint",
}


def _normalized_action_arguments(registry, name, arguments):
    try:
        return validate_arguments(registry.get(name).parameters, arguments)
    except (TypeError, ValueError):
        return arguments


def _round_fingerprint(registry, parsed_calls):
    parts = [
        (name, canonical_arguments_hash(
            _normalized_action_arguments(registry, name, arguments),
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
             on_content=None):
    """流式调用 LLM。逐 chunk 累积，返回与非流式格式一致的 message dict。

    用 http.client + Accept-Encoding: identity 绕过网关 502。
    按 BASE_URL 的 scheme 选 HTTP/HTTPSConnection（https 网关如 api.deepseek.com）。
    ``stream_output`` 为真时，正文通过 ``on_content`` 逐 chunk 观察；没有
    回调时保留独立调用的 print 行为。为假时既不调用回调也不打印正文。
    tool_calls 的 arguments 跨 chunk 拼接。
    """
    if stream_output is None:
        stream_output = OUTPUT_MODE != "quiet"
    p = urlparse(BASE_URL)
    if p.scheme == "https":
        conn = http.client.HTTPSConnection(p.hostname, p.port or 443, timeout=120)
    else:
        conn = http.client.HTTPConnection(p.hostname, p.port or 80, timeout=120)
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

        # content 边收边输出（打字机效果）；回调是观察能力，失败不能
        # 影响 SSE 累积和后续协议解析。
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

        # tool_calls 的 arguments 跨 chunk 拼接
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
        message["tool_calls"] = [
            tool_calls_acc[i] for i in sorted(tool_calls_acc)
        ]
    return message


def summarize_messages(messages):
    """Summarize context without tool schemas or terminal streaming."""
    return call_llm(messages, include_tools=False, stream_output=False).get("content", "") or ""


def agent_loop(context_manager: ContextManager, tool_executor: ToolExecutor):
    """循环调用 LLM，并通过注入的 executor 回灌工具结果。

    ContextManager 拥有并维护 history；本函数只向其 history 追加
    assistant 和 tool 消息，不把 AgentState 序列化到 messages。Executor
    Runtime 将结构化 ExecutionResult 记录到 AgentState。每个带
    tool_calls 的 assistant 消息在下一次 LLM 调用或本函数返回前，都会
    追加全部对应的 tool result，且结果保持 tool_calls 的原始顺序。
    """
    # Remind once for each observable progress state. Old/custom State
    # implementations without ``progress_marker`` retain one-reminder
    # compatibility behavior.
    reminded_progress_marker = None
    legacy_reminded = False
    internal_retry = False
    output = TerminalOutput(OUTPUT_MODE)

    def _finish(value):
        output.close()
        return value

    def _terminal_state_result(state):
        """Return the loop result for a terminal State, if it became terminal."""
        status = getattr(state, "status", None) if state is not None else None
        if status == "blocked":
            return f"任务已阻塞：{getattr(state, 'terminal_reason', '') or '任务已阻塞'}"
        if status == "failed":
            return f"任务已失败：{getattr(state, 'terminal_reason', '') or '任务已失败'}"
        return None

    initial_state = getattr(context_manager, "state", None)
    if (initial_state is not None and
            getattr(getattr(initial_state, "planning_state", None), "phase", None) == "awaiting_approval"):
        return _finish("计划等待用户决定")

    for i in range(MAX_ITERATIONS):
        prepared_messages = context_manager.prepare_messages()
        if not internal_retry:
            output.round_start(i + 1)
        internal_retry = False
        run_registry = getattr(tool_executor, "registry", None)
        if run_registry is None:
            msg = call_llm(prepared_messages, on_content=output.assistant_delta)
        else:
            msg = call_llm(
                prepared_messages,
                tool_registry=run_registry,
                on_content=output.assistant_delta,
            )
        output.assistant_end()

        tool_calls = msg.get("tool_calls", [])
        invalid_tool_call_ids = set()
        if tool_calls:
            original_ids = {
                tc.get("id")
                for tc in tool_calls
                if (
                    isinstance(tc, dict)
                    and isinstance(tc.get("id"), str)
                    and tc.get("id").strip()
                )
            }
            used_ids = set()
            next_local_id = 0
            normalized_tool_calls = []
            invalid_tool_call_errors = {}

            for tc in tool_calls:
                raw_id = tc.get("id") if isinstance(tc, dict) else None
                raw_type = tc.get("type") if isinstance(tc, dict) else None
                raw_function = tc.get("function") if isinstance(tc, dict) else None
                raw_name = raw_function.get("name") if isinstance(raw_function, dict) else None
                raw_arguments = (
                    raw_function.get("arguments")
                    if isinstance(raw_function, dict)
                    else None
                )

                errors = []
                if not isinstance(raw_id, str) or not raw_id.strip():
                    errors.append("无效的 tool_call_id")
                elif raw_id in used_ids:
                    errors.append("重复的 tool_call_id")
                if raw_type != "function":
                    errors.append("非法的 tool_call.type")

                valid_name = isinstance(raw_name, str) and bool(raw_name.strip())
                if not valid_name:
                    errors.append("非法的 function.name")

                valid_arguments = isinstance(raw_arguments, str)
                if valid_arguments:
                    try:
                        parsed_arguments = json.loads(raw_arguments)
                    except json.JSONDecodeError:
                        valid_arguments = False
                    else:
                        valid_arguments = isinstance(parsed_arguments, dict)
                if not valid_arguments:
                    errors.append("非法的 function.arguments")

                if not errors:
                    tool_call_id = raw_id
                else:
                    while True:
                        tool_call_id = f"local-error-{next_local_id}"
                        next_local_id += 1
                        if tool_call_id not in used_ids and tool_call_id not in original_ids:
                            break
                    invalid_tool_call_errors[tool_call_id] = (
                        f"工具调用失败: {'; '.join(errors)}"
                    )

                used_ids.add(tool_call_id)
                normalized_tool_calls.append({
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": raw_name if valid_name else "invalid_tool_call",
                        "arguments": raw_arguments if valid_arguments else "{}",
                    },
                })

            msg = dict(msg)
            msg["tool_calls"] = normalized_tool_calls

        context_manager.history.append(msg)

        # 无 tool_calls = 模型给出最终文本回复，结束
        if not msg.get("tool_calls"):
            state = getattr(context_manager, "state", None)
            if (state is not None and
                    getattr(getattr(state, "planning_state", None), "phase", None) == "exploring" and
                    getattr(state, "repair_phase", "idle") == "idle" and
                    getattr(state, "user_plan_decisions", None) and
                    state.user_plan_decisions[-1].decision == "continue_exploring" and
                    state.user_plan_decisions[-1].revision_id == state.planning_state.active_revision_id):
                # The user may review the unchanged revision after more
                # investigation; this text pauses at the CLI handoff.
                return _finish(msg.get("content", ""))
            reminder = state.completion_reminder() if state is not None and hasattr(state, "completion_reminder") else None
            if reminder:
                if "progress_marker" not in reminder:
                    # Compatibility for old/custom State objects.
                    if legacy_reminded:
                        if state is not None:
                            state.status = "blocked"
                        return _finish(msg.get("content", ""))
                    legacy_reminded = True
                else:
                    marker = reminder.get("progress_marker")
                    if marker == reminded_progress_marker:
                        if state is not None:
                            state.status = "blocked"
                        return _finish(msg.get("content", ""))
                    reminded_progress_marker = marker
                if hasattr(context_manager, "set_runtime_notice"):
                    context_manager.set_runtime_notice(str(reminder.get(
                        "message", "请在下一条回复中调用推进任务的工具；确实无法继续时说明具体阻塞原因。"
                    )))
                internal_retry = True
                continue
            return _finish(msg.get("content", ""))

        # 只有全 effect_class=none 的回合可以并发。任何 possible effect
        # 都令整轮按模型顺序执行和提交，保证 generation 的确定性。
        tool_calls = msg["tool_calls"]
        state = getattr(context_manager, "state", None)
        structured = hasattr(tool_executor, "execute_result") and run_registry is not None

        parsed_calls = []
        effects = []
        for tc in tool_calls:
            function = tc.get("function", {}) if isinstance(tc, dict) else {}
            try:
                args = json.loads(function.get("arguments", "{}"))
                if not isinstance(args, dict):
                    raise TypeError("tool arguments 必须是 JSON object")
            except (TypeError, json.JSONDecodeError):
                args = {}
            name = function.get("name", "invalid_tool_call")
            parsed_calls.append((name, args))
            try:
                effect = run_registry.effect_for(name, args) if structured else "none"
            except (TypeError, ValueError):
                effect = "none"
            effects.append(effect)
        has_possible = "possible" in effects or any(name == "recover" for name, _ in parsed_calls)
        has_serial_plan_write = any(
            name in _PLAN_CONTROL_TOOLS for name, _ in parsed_calls
        )
        planning_batch_errors = {}
        planning_phase = getattr(getattr(state, "planning_state", None), "phase", "direct")
        plan_controls = {"begin_plan", "cancel_planning"}
        if (len(parsed_calls) != 1 and
                (any(name in plan_controls for name, _ in parsed_calls) or
                 any(name == "request_replan" for name, _ in parsed_calls) or
                 (planning_phase == "exploring" and
                  any(name == "commit_plan" for name, _ in parsed_calls)))):
            detail = (
                "工具调用拒绝: request_replan、规划阶段切换或 exploring 中的 "
                "commit_plan 必须独占一个工具回合"
            )
            planning_batch_errors = {index: detail for index in range(len(parsed_calls))}
        invalid_verifications = {
            index for index, (name, args) in enumerate(parsed_calls)
            if has_possible and name == "run_shell" and args.get("purpose", "execution") == "verification"
        }
        repair_batch_errors = {}
        if state is not None and hasattr(state, "repair_phase"):
            repair_phase = state.repair_phase
            recovery_indexes = [
                index for index, (name, _) in enumerate(parsed_calls)
                if name == "recover"
            ]
            if repair_phase == "diagnosis_required" and recovery_indexes and len(parsed_calls) != 1:
                detail = "工具调用失败: diagnosis_required 阶段的 recover 必须独占一个工具回合"
                repair_batch_errors = {index: detail for index in range(len(parsed_calls))}
            elif repair_phase == "verification_required":
                valid_single_verification = (
                    len(parsed_calls) == 1
                    and parsed_calls[0][0] == "run_shell"
                    and parsed_calls[0][1].get("purpose", "execution") == "verification"
                )
                if not valid_single_verification:
                    detail = "工具调用失败: verification_required 阶段下一工具回合只能是单个独立 verification"
                    repair_batch_errors = {index: detail for index in range(len(parsed_calls))}

        def _run(index_tc):
            index, tc = index_tc
            tool_call_id = tc.get("id") if isinstance(tc, dict) else None
            if structured and state is not None and getattr(state, "is_terminal", lambda: False)():
                name, args = parsed_calls[index]
                terminal = tool_executor.execute_result(name, args, state, notify=False)
                return tool_call_id, terminal.tool_content(), None, terminal
            if tool_call_id in invalid_tool_call_errors:
                text = invalid_tool_call_errors[tool_call_id]
                if structured and state is not None and parsed_calls[index][0] == "recover":
                    content = _recovery_rejection_content(state, parsed_calls[index][1], text)
                    display = ExecutionResult(
                        "recover", parsed_calls[index][1], "not_checked", False,
                        "invalid", 0, "none", content, content[:200],
                        error_kind="malformed_tool_call",
                    )
                    return tool_call_id, content, None, display
                invalid = ExecutionResult(
                    "invalid_tool_call", {}, "not_checked", False, "invalid", 0,
                    "none", text, text[:200], error_kind="malformed_tool_call",
                ) if structured else None
                return tool_call_id, text, invalid, invalid

            name, args = parsed_calls[index]
            if index in planning_batch_errors:
                text = planning_batch_errors[index]
                is_plan = name in _PLAN_CONTROL_TOOLS
                if is_plan:
                    text = json.dumps({"status": "plan_rejected", "message": text}, ensure_ascii=False)
                invalid = ExecutionResult(
                    name, args, "not_checked", False, "invalid", 0, effects[index],
                    text, text[:200],
                    error_kind="plan_rejected" if is_plan else "planning_phase_gate",
                ) if structured else None
                return tool_call_id, text, None, invalid
            if index in repair_batch_errors:
                text = repair_batch_errors[index]
                if parsed_calls[index][0] in _PLAN_CONTROL_TOOLS:
                    text = json.dumps({
                        "status": "plan_rejected",
                        "message": text,
                    }, ensure_ascii=False)
                    invalid = ExecutionResult(
                        parsed_calls[index][0], parsed_calls[index][1], "not_checked", False,
                        "invalid", 0, "none", text, text[:200], error_kind="plan_rejected",
                    ) if structured else None
                    return tool_call_id, text, None, invalid
                if structured and state is not None and name == "recover":
                    content = _recovery_rejection_content(state, args, text)
                    invalid = ExecutionResult(
                        name, args, "not_checked", False, "invalid", 0, "none",
                        content, content[:200], error_kind="recovery_rejected",
                    )
                    return tool_call_id, content, None, invalid
                invalid = ExecutionResult(
                    name, args, "not_checked", False, "invalid", 0, "none",
                    text, text[:200], error_kind="repair_phase_gate",
                ) if structured else None
                return tool_call_id, text, invalid, invalid
            if index in invalid_verifications:
                text = "工具调用失败: verification 不能与 possible effect 处于同一回合"
                invalid = ExecutionResult(
                    name, args, "not_checked", False, "invalid", 0, "none",
                    text, text[:200], error_kind="mixed_verification",
                ) if structured else None
                return tool_call_id, text, invalid, invalid

            try:
                function = tc["function"]
                name = function["name"]
                raw_arguments = function["arguments"]
                args = json.loads(raw_arguments)
                if not isinstance(args, dict):
                    raise TypeError("tool arguments 必须是 JSON object")
            except (KeyError, TypeError, json.JSONDecodeError) as error:
                text = f"工具调用失败: {type(error).__name__}"
                display = ExecutionResult(
                    "invalid_tool_call", {}, "not_checked", False, "invalid", 0,
                    "none", text, text[:200], error_kind="malformed_tool_call",
                ) if structured else None
                return tool_call_id, text, None, display

            # Isolate only this tool boundary so pool.map still returns one
            # protocol result per call; LLM and CLI exceptions remain uncaught.
            try:
                if structured:
                    execution = tool_executor.execute_result(
                        name, args, state,
                        notify=False,
                    )
                    if name == "recover":
                        if execution.error_kind in ("task_terminal", "recovery_rejected"):
                            content = execution.tool_content()
                        elif execution.outcome != "succeeded":
                            content = _recovery_rejection_content(state, args, execution.output_excerpt)
                        else:
                            content = execution.tool_content()
                        return tool_call_id, content, None, execution
                    if execution.error_kind == "plan_rejected":
                        return tool_call_id, execution.tool_content(), None, execution
                    if execution.error_kind == "task_terminal":
                        return tool_call_id, execution.tool_content(), None, execution
                    return tool_call_id, execution.tool_content(), execution, execution
                result = tool_executor.execute(name, args)
                return tool_call_id, str(result), None, None
            except Exception as error:
                text = f"工具调用失败: {type(error).__name__}"
                display = ExecutionResult(
                    name, args, "not_checked", False, "failed", 0, "none",
                    text, text[:200], error_kind="executor_exception",
                )
                return tool_call_id, text, None, display

        indexed_calls = list(enumerate(tool_calls))
        output.tools_start(tool_calls)
        if has_possible or has_serial_plan_write:
            results = []
            for item in indexed_calls:
                result = _run(item)
                results.append(result)
                if (structured and state is not None and result[2] is not None
                        and parsed_calls[item[0]][0] != "recover"):
                    state.record_execution_result(result[2])
        else:
            with ThreadPoolExecutor(max_workers=len(tool_calls)) as pool:
                results = list(pool.map(_run, indexed_calls))
            # Concurrent handlers finish in arbitrary order; facts commit in
            # model order so attempt ids and snapshots stay deterministic.
            if structured and state is not None:
                for _, _, execution, _ in results:
                    if execution is not None:
                        state.record_execution_result(execution)

        # Threads execute concurrently, but terminal output follows tool-call order.
        for tc, (tool_call_id, content, _, display) in zip(tool_calls, results):
            function = tc.get("function", {}) if isinstance(tc, dict) else {}
            name = function.get("name", "<missing>") if isinstance(function, dict) else "<missing>"
            arguments = function.get("arguments", {}) if isinstance(function, dict) else {}
            output.tool_result(name, arguments, content, display)
        output.close()

        for tool_call_id, content, _, _ in results:
            context_manager.history.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": content,
            })

        # A tool handler may exhaust a budget or apply a recovery strategy
        # that makes the task terminal.  All results above must still be
        # visible to the model protocol, but no additional LLM call may
        # overwrite that terminal fact.
        terminal_result = _terminal_state_result(state)
        if terminal_result is not None:
            return _finish(terminal_result)

        # Observe only after every call has been executed or rejected, its
        # State fact has been committed in model order, and its tool result is
        # visible in history.  This keeps stagnation detection from creating a
        # second, partial tool-round protocol.
        if (state is not None and hasattr(state, "observe_tool_round")
                and structured):
            action_fingerprint = _round_fingerprint(run_registry, parsed_calls)
            observations = []
            effect_actions = []
            for execution in (
                item[2] for item in results
                if isinstance(item[2], ExecutionResult)
            ):
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
                            "tool": execution.tool,
                            "arguments": execution.arguments,
                        }))
            observation = state.observe_tool_round(
                action_fingerprint, observations, effect_actions,
                tuple(name for name, _ in parsed_calls),
            )
            if observation.get("warning") and hasattr(context_manager, "set_runtime_notice"):
                context_manager.set_runtime_notice(str(observation["warning"]))
            if observation.get("blocked"):
                reason = observation.get("terminal_reason") or getattr(state, "terminal_reason", "")
                return _finish(f"任务已阻塞：{reason}")
            terminal_result = _terminal_state_result(state)
            if terminal_result is not None:
                return _finish(terminal_result)
        if state is not None and getattr(state.planning_state, "phase", None) == "awaiting_approval":
            return _finish("计划等待用户决定")

    state = getattr(context_manager, "state", None)
    if state is not None and getattr(state, "status", None) not in ("blocked", "failed"):
        state.status = "failed"
    return _finish("达到最大迭代次数")
