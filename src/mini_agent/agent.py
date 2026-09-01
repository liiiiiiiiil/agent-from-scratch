"""带工具的 Agent Loop（流式）：调 LLM -> 若要工具则执行 -> 结果回灌 -> 再调，循环到纯文本回复或上限。"""

import http.client
import json
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from mini_agent.context import ContextManager
from mini_agent.tools import registry
from mini_agent.tools.base import ExecutionResult, ToolExecutor
from mini_agent.config import BASE_URL, API_KEY, MODEL, MAX_ITERATIONS


DISPLAY_RESULT_MAX_LENGTH = 1200


def _safe_print(*args, **kwargs):
    """Best-effort observation output that cannot break agent execution."""
    try:
        print(*args, **kwargs)
    except Exception:
        pass


def _display_result(value):
    """Keep terminal output readable without changing the tool result."""
    text = str(value)
    if len(text) > DISPLAY_RESULT_MAX_LENGTH:
        text = text[:DISPLAY_RESULT_MAX_LENGTH] + "... [输出已截断]"
    return text.replace("\n", "\n    ")


def call_llm(messages, include_tools=True, stream_output=True, tool_registry=None):
    """流式调用 LLM。逐 chunk 累积，返回与非流式格式一致的 message dict。

    用 http.client + Accept-Encoding: identity 绕过网关 502。
    按 BASE_URL 的 scheme 选 HTTP/HTTPSConnection（https 网关如 api.deepseek.com）。
    content 边收边 print（打字机效果），tool_calls 的 arguments 跨 chunk 拼接。
    """
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

    content_parts = []
    tool_calls_acc = {}

    for raw in resp:
        line = raw.decode("utf-8").strip()
        if not line or not line.startswith("data:"):
            continue
        if line == "data: [DONE]":
            break
        chunk = json.loads(line[6:])
        choices = chunk.get("choices", [])
        if not choices:
            continue
        delta = choices[0].get("delta", {})

        # content 边收边打印（打字机效果）
        if delta.get("content"):
            content_parts.append(delta["content"])
            if stream_output:
                _safe_print(delta["content"], end="", flush=True)

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
    reminded = False
    for i in range(MAX_ITERATIONS):
        _safe_print(f"\n[第 {i + 1} 轮] 助手: ", end="", flush=True)
        prepared_messages = context_manager.prepare_messages()
        run_registry = getattr(tool_executor, "registry", None)
        if run_registry is None:
            msg = call_llm(prepared_messages)
        else:
            msg = call_llm(prepared_messages, tool_registry=run_registry)

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

        # content 已在 call_llm 中流式打印；这里补齐换行，避免下一段粘连。
        _safe_print()

        for tc in msg.get("tool_calls", []):
            function = tc.get("function", {}) if isinstance(tc, dict) else {}
            if not isinstance(function, dict):
                function = {}
            name = function.get("name", "<missing>")
            arguments = function.get("arguments", "<missing>")
            try:
                arguments = json.dumps(json.loads(arguments), ensure_ascii=False)
            except (TypeError, json.JSONDecodeError):
                arguments = str(arguments)
            _safe_print(f"  工具: {name} {arguments}")

        # 无 tool_calls = 模型给出最终文本回复，结束
        if not msg.get("tool_calls"):
            state = getattr(context_manager, "state", None)
            reminder = state.completion_reminder() if state is not None and hasattr(state, "completion_reminder") else None
            if reminder and not reminded:
                reminded = True
                if hasattr(context_manager, "set_runtime_notice"):
                    context_manager.set_runtime_notice(str(reminder.get("message", "请继续执行并验证。")))
                continue
            if reminder and state is not None:
                state.status = "blocked"
            return msg.get("content", "")

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
        has_possible = "possible" in effects
        invalid_verifications = {
            index for index, (name, args) in enumerate(parsed_calls)
            if has_possible and name == "run_shell" and args.get("purpose", "execution") == "verification"
        }

        def _run(index_tc):
            index, tc = index_tc
            tool_call_id = tc.get("id") if isinstance(tc, dict) else None
            if tool_call_id in invalid_tool_call_errors:
                text = invalid_tool_call_errors[tool_call_id]
                invalid = ExecutionResult(
                    "invalid_tool_call", {}, "not_checked", False, "invalid", 0,
                    "none", text, text[:200], error_kind="malformed_tool_call",
                ) if structured else None
                return tool_call_id, text, invalid

            name, args = parsed_calls[index]
            if index in invalid_verifications:
                text = "工具调用失败: verification 不能与 possible effect 处于同一回合"
                invalid = ExecutionResult(
                    name, args, "not_checked", False, "invalid", 0, "none",
                    text, text[:200], error_kind="mixed_verification",
                ) if structured else None
                return tool_call_id, text, invalid

            try:
                function = tc["function"]
                name = function["name"]
                raw_arguments = function["arguments"]
                args = json.loads(raw_arguments)
                if not isinstance(args, dict):
                    raise TypeError("tool arguments 必须是 JSON object")
            except (KeyError, TypeError, json.JSONDecodeError) as error:
                return tool_call_id, f"工具调用失败: {type(error).__name__}", None

            # Isolate only this tool boundary so pool.map still returns one
            # protocol result per call; LLM and CLI exceptions remain uncaught.
            try:
                if structured:
                    execution = tool_executor.execute_result(
                        name, args, state if effects[index] == "possible" else None,
                        notify=False,
                    )
                    return tool_call_id, execution.tool_content(), execution
                result = tool_executor.execute(name, args)
                return tool_call_id, str(result), None
            except Exception as error:
                return tool_call_id, f"工具调用失败: {type(error).__name__}", None

        indexed_calls = list(enumerate(tool_calls))
        if has_possible:
            results = []
            for item in indexed_calls:
                result = _run(item)
                results.append(result)
                if structured and state is not None and result[2] is not None:
                    state.record_execution_result(result[2])
        else:
            with ThreadPoolExecutor(max_workers=len(tool_calls)) as pool:
                results = list(pool.map(_run, indexed_calls))
            # Concurrent handlers finish in arbitrary order; facts commit in
            # model order so attempt ids and snapshots stay deterministic.
            if structured and state is not None:
                for _, _, execution in results:
                    if execution is not None:
                        state.record_execution_result(execution)

        # Threads execute concurrently, but terminal output follows tool-call order.
        for tc, (tool_call_id, content, _) in zip(tool_calls, results):
            function = tc.get("function", {}) if isinstance(tc, dict) else {}
            name = function.get("name", "<missing>") if isinstance(function, dict) else "<missing>"
            _safe_print(f"  结果 [{name}]:\n    {_display_result(content)}")

        for tool_call_id, content, _ in results:
            context_manager.history.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": content,
            })

    state = getattr(context_manager, "state", None)
    if state is not None:
        state.status = "failed"
    return "达到最大迭代次数"
