"""带工具的 Agent Loop：调 LLM -> 若要工具则执行 -> 结果回灌 -> 再调，循环到纯文本回复或上限。"""

import http.client
import json
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from mini_agent.tools import registry, executor
from mini_agent.config import BASE_URL, API_KEY, MODEL, MAX_ITERATIONS


def call_llm(messages):
    """流式调用 LLM。逐 chunk 累积，返回与非流式格式一致的 message dict。

    用 http.client + Accept-Encoding: identity 绕过网关 502。
    content 边收边 print（打字机效果），tool_calls 的 arguments 跨 chunk 拼接。
    """
    p = urlparse(BASE_URL)
    conn = http.client.HTTPConnection(p.hostname, p.port or 80, timeout=120)
    body = json.dumps(
        {"model": MODEL, "messages": messages, "stream": True, "tools": registry.schemas()},
        ensure_ascii=False,
    ).encode()
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

        if delta.get("content"):
            content_parts.append(delta["content"])
            print(delta["content"], end="", flush=True)

        for tc in delta.get("tool_calls", []):
            idx = tc.get("index", 0)
            slot = tool_calls_acc.setdefault(idx, {
                "id": "", "type": "function",
                "function": {"name": "", "arguments": ""},
            })
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function", {})
            if fn.get("name"):
                slot["function"]["name"] = fn["name"]
            if fn.get("arguments"):
                slot["function"]["arguments"] += fn["arguments"]

    conn.close()

    message = {"role": "assistant", "content": "".join(content_parts) or None}
    if tool_calls_acc:
        message["tool_calls"] = [
            tool_calls_acc[i] for i in sorted(tool_calls_acc)
        ]
    return message


def agent_loop(messages):
    """循环：调 LLM -> 有 tool_calls 就执行并回灌 -> 无则结束。

    messages 由调用方维护并跨轮复用，本函数只往里 append。
    """
    for i in range(MAX_ITERATIONS):
        msg = call_llm(messages)
        messages.append(msg)

        # 打印 LLM 本轮的完整决策原文：想调什么工具、传什么参数
        print(f"\n=== [{i+1}] LLM 回复 ===")
        # content 已在 call_llm 中流式打印，此处不再重复
        for tc in msg.get("tool_calls", []):
            print(f"决策调用: {tc['function']['name']}({tc['function']['arguments']})")

        # 无 tool_calls = 模型给出最终文本回复，结束
        if not msg.get("tool_calls"):
            return msg.get("content", "")

        # 有 tool_calls：并行执行，结果按原顺序作为 role=tool 回灌
        # 同一轮的多个 tool_calls 互不依赖，用线程池并发执行以加速
        tool_calls = msg["tool_calls"]

        def _run(tc):
            name = tc["function"]["name"]
            args = json.loads(tc["function"]["arguments"])
            result = executor.execute(name, args)
            print(f"  执行 {name} -> {result}")
            return tc["id"], str(result)

        with ThreadPoolExecutor(max_workers=len(tool_calls)) as pool:
            results = list(pool.map(_run, tool_calls))

        for tool_call_id, content in results:
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": content,
            })

    return "达到最大迭代次数"
