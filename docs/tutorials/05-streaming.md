# 第 5 课：流式输出

> 版本 v0.05 | [上一课](04-permission-gate.md) | [下一课](06-concurrent-tool-calls.md)

> 代码快照：`v0.05` · 相邻差异：`v0.04..v0.05` · 命令环境：Bash/zsh
>
> 运行要求：Python 3.10+。该 tag 的 `pyproject.toml` 仍标 3.9，但源码已使用 3.10 语法。

## 本课目标

上一版要等模型生成完整回复后，终端才会显示内容。回复一长，等待会很明显。
这一版让 `call_llm` 边接收边显示回复，并处理 `tool_calls` 被拆到多个 chunk 的情况。

## 前置

- 已读 [第 4 课](04-permission-gate.md)，理解权限闸门
- `git checkout v0.05` 切到本版代码

## 新增/改动了什么

```
 src/mini_agent/
 └── agent.py            # 改：call_llm 改流式（stream=True + SSE 解析 + chunk 拼接）
```

> 本版只改 `agent.py` 一个文件。工具、权限、config 都不变。

## 核心概念

### 非流式 vs 流式

上一版是非流式（模型生成完再一次返回）：LLM 先生成完整 JSON，`call_llm` 解析后才返回 message。
所以终端必须等模型全部生成完，长回复时会长时间没有反馈。

这一版改用流式（模型生成一点就发送一点）。这些小片段叫 chunk，传输格式是 Server-Sent Events（服务器发送事件，简称 SSE）。
`call_llm` 逐个读取并打印 chunk，因此终端会逐字出现内容；但这只改变显示方式，不代表模型一定更快完成。

### SSE 协议（Server-Sent Events）

问题在于：流式响应不再是一个完整 JSON，而是一串文本。每行是一个 `data:` 事件：

```
data: {"choices":[{"delta":{"content":"你"}}]}
data: {"choices":[{"delta":{"content":"好"}}]}
data: {"choices":[{"delta":{"content":"！"}}]}
data: [DONE]
```

每个 chunk 的 `delta` 里可能包含：
- `content`：一小段文本（边收边打印）
- `tool_calls`：工具调用的片段（跨 chunk 拼接）

收到 `data: [DONE]` 后，流才确定结束。

### call_llm 流式实现（`src/mini_agent/agent.py:12`）

要让服务端按流式方式返回，需要在请求中打开开关并声明接收 SSE：
```python
body = json.dumps({
    "model": MODEL,
    "messages": messages,
    "stream": True,              # ← 新增：开启流式
    "tools": registry.schemas(),
}, ensure_ascii=False).encode()

headers = {
    ...
    "Accept": "text/event-stream",  # ← 新增：声明接收 SSE
}
```

收到响应后，代码逐行读取 `resp`，再解析每个 `data:` 事件：

```python
content_parts = []
tool_calls_acc = {}

for raw in resp:
    line = raw.decode("utf-8").strip()
    if not line or not line.startswith("data:"):
        continue
    if line == "data: [DONE]":
        break
    chunk = json.loads(line[6:])
    delta = chunk["choices"][0].get("delta", {})

    # content 边收边打印（打字机效果）
    if delta.get("content"):
        content_parts.append(delta["content"])
        print(delta["content"], end="", flush=True)

    # tool_calls 的 arguments 跨 chunk 拼接
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
```

循环结束后，再拼成与非流式格式一致的 message，后续 agent loop 不需要知道本次是流式响应：
```python
message = {"role": "assistant", "content": "".join(content_parts) or None}
if tool_calls_acc:
    message["tool_calls"] = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
return message
```

### tool_calls 的跨 chunk 拼接

文本可以直接逐段显示，但工具调用不能这样处理。流式传输时，一个 tool_call 可能被拆成多个 chunk：

```
chunk 1: {"tool_calls":[{"index":0, "id":"call_abc", "function":{"name":"calculate"}}]}
chunk 2: {"tool_calls":[{"index":0, "function":{"arguments":"{\"expr"}}]}
chunk 3: {"tool_calls":[{"index":0, "function":{"arguments":"ession\": \"3+5\"}"}}]}
```

这里的 `arguments` 是字符串片段，要用 `+=` 拼起来，不是把几个 JSON 对象合并。`index` 用来标识同一个 tool_call。
代码按 `index` 暂存片段，只有流结束后才能得到完整的 tool_calls 列表。

## 为什么这样设计

### 为什么用 http.client 而不是 requests

实际接入时，`your-gateway-host` 网关对 `Accept-Encoding: gzip` 的响应会异常返回 502。
压缩后的 SSE 流还可能让 chunk 边界难以判断。`http.client` 配合 `Accept-Encoding: identity` 会请求未压缩的原始文本，逐行解析才可靠。

### 为什么 content 边收边 print 而不是收完再 print

边收边 `print` 会产生“打字机效果”：用户能先看到已经生成的文字，不必等几秒后整段出现。
`flush=True` 会立即刷新输出；否则文字可能还留在缓冲区，终端看起来仍像一次性显示。

### 为什么 tool_calls 不边收边 print

content 是普通文本，逐字打印有意义。tool_calls 是包含工具名和参数 JSON 的结构化数据，打印半截参数只会得到难以阅读的内容。
因此代码等它收完整后，再打印一次决策：`print(f"  决策调用: {name}({arguments})")`。

### 为什么 agent_loop 里 print 标记移到 call_llm 之前

v0.04 的 `agent_loop` 先调用 `call_llm`，再打印 `=== [N] LLM 回复 ===`。
这一版必须先打印标记再调用 `call_llm`。因为 content 会在 `call_llm` 内立即输出，标记放在后面就会出现在回复之后，终端顺序会乱。

## 使用指导

### 本版可用的命令

```bash
# 命令行首条任务（观察打字机效果）
PYTHONPATH=src python -m mini_agent "用一句话介绍你自己"

# 带工具调用
PYTHONPATH=src python -m mini_agent "计算 123 * 456"

# 跑 smoke test
PYTHONPATH=src python tests/test_tools.py
```

### 本版典型示例

**示例 1：观察打字机效果**
```bash
PYTHONPATH=src python -m mini_agent "用一句话介绍你自己"
```
预期：终端会逐字出现 LLM 的回复，而不是等几秒后一次性弹出。
注意：回复目前会打印两遍。第一遍来自 `call_llm` 的流式输出，第二遍来自 `__main__.py` 的 `print(reply)`。
这是 v0.05 已知的显示问题，后续版本会调整 `__main__.py`。

**示例 2：流式 + 工具调用**
```bash
PYTHONPATH=src python -m mini_agent "计算 123 * 456"
```
预期输出：
```
=== [1] LLM 回复 ===
  决策调用: calculate({"expression": "123 * 456"})
[Executor] 执行 Tool: calculate
...
  执行结果: 56088

=== [2] LLM 回复 ===
123 × 456 的计算结果是 56088。
```
注意：第 2 轮没有工具调用，最终回复会由流式输出逐字显示。

**示例 3：长回复体感对比**
```bash
# v0.04（非流式）：等 3-5 秒后一次性弹出
git checkout v0.04
PYTHONPATH=src python -m mini_agent "详细介绍 Python 的历史"

# v0.05（流式）：立刻开始逐字出现
git checkout v0.05
PYTHONPATH=src python -m mini_agent "详细介绍 Python 的历史"
```

### 本版独有特性

- **打字机效果**：终端会逐字出现 LLM 回复，这是 v0.05 最容易观察到的变化。
- **SSE 解析**：`call_llm` 逐行读取 `data:` 事件，再解析 JSON chunk。
- **tool_calls 跨 chunk 拼接**：`arguments` 分片到达后用 `+=` 拼接，并按 `index` 归并。
- **回复打印两遍**：`call_llm` 流式打印后，`__main__.py` 还会执行 `print(reply)`；这是已知问题。

## 动手验证

1. **跑 smoke test**：
   ```bash
   PYTHONPATH=src python tests/test_tools.py
   ```
   预期：8 个 PASS（工具测试不受流式影响）。

2. **观察打字机效果**：
   ```bash
   PYTHONPATH=src python -m mini_agent "用一句话介绍你自己"
   ```
   预期：终端逐字出现回复。

3. **对比 v0.04 vs v0.05**：
   ```bash
   git checkout v0.04
   PYTHONPATH=src python -m mini_agent "详细介绍 Python 的历史"
   # 感受：等几秒后一次性弹出

   git checkout v0.05
   PYTHONPATH=src python -m mini_agent "详细介绍 Python 的历史"
   # 感受：立刻开始逐字出现
   ```

## 本版完整代码

- [`src/mini_agent/agent.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.05/src/mini_agent/agent.py) — 流式 `call_llm` + `agent_loop`
