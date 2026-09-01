# 第 2 课：第一个工具

> 版本 v0.02 | [上一课](01-minimal-loop.md) | [下一课](03-file-tools.md)

> 代码快照：`v0.02` · 相邻差异：`v0.01..v0.02` · 命令环境：Bash/zsh
>
> 运行要求：Python 3.9+。

## 本课目标

上一课的 LLM 只能生成文本。即使它回答了计算题，也只是依靠模型自己的推断，程序没有真正执行计算。

这一课接入第一个工具 `calculate`，并介绍 function calling，也就是“模型提出工具调用请求，程序负责实际执行”的协议。为了组织这条流程，代码新增 `Tool`、`ToolRegistry` 和 `ToolExecutor`。

## 前置

- 已读 [第 1 课](01-minimal-loop.md)，理解 agent loop 的循环结构
- `git checkout v0.02` 切到本版代码

## 新增/改动了什么

```
 src/mini_agent/
+├── tools/
+│   ├── __init__.py     # 注册中心：创建 registry + executor，注册 calculate
+│   ├── base.py         # Tool / ToolRegistry / ToolExecutor
+│   └── calc.py         # calculate 工具
 ├── agent.py            # 改：call_llm 加 tools 参数；agent_loop 加 tool_calls 执行 + role=tool 回灌
 └── __main__.py         # 改：system prompt 从"你是一个助手"改为"通过调用工具完成任务"
+tests/
+└── test_tools.py        # 新增：工具 smoke test
```

## 核心概念

### 工具三件套：Tool / ToolRegistry / ToolExecutor

程序需要同时告诉模型“有哪些工具”，也要知道“收到请求后调用哪个 Python 函数”。`Tool`（`src/mini_agent/tools/base.py:14`）把这两部分放在同一个定义中：

```python
@dataclass
class Tool:
    name: str           # 给 LLM 看的工具名称
    description: str    # 给 LLM 看的工具说明
    parameters: dict    # 给 LLM 看的参数 Schema（JSON Schema）
    handler: Callable   # Runtime 真正执行的 Python 函数
```

其中 `name`、`description` 和 `parameters` 供 LLM 理解工具；`handler` 是运行时真正调用的函数。`to_llm_schema()` 会把前一部分转换成 function calling 所需的格式。

**ToolRegistry**（`base.py:45`）是工具注册表。它负责保存、查找和列出工具，也能生成发给 LLM 的 schema 列表。schema 是一份结构说明，告诉模型工具需要哪些参数。

**ToolExecutor**（`base.py:82`）是工具执行器。它根据工具名找到对应 `handler`，传入参数，并把可预期的执行异常转换为错误结果。v0.02 还没有权限检查，这部分会在第 4 课加入。

### calculate 工具（`src/mini_agent/tools/calc.py`）

```python
def calculate(expression: str):
    if not re.fullmatch(r"[\d\s+\-*/().]+", expression):
        raise ValueError("表达式包含非法字符")
    return str(eval(expression))
```

`eval` 会执行字符串，因此不能直接接收任意内容。这里先用正则白名单，只允许数字、空白和数学运算符，再把表达式交给 `eval`。

### function calling 协议

v0.01 的 `call_llm` 只发 `model` + `messages`。v0.02 加了 `tools` 参数：

```python
body = json.dumps({
    "model": MODEL,
    "messages": messages,
    "tools": registry.schemas(),   # ← 新增：把所有工具的 schema 发给 LLM
}, ensure_ascii=False).encode()
```

LLM 收到 `tools` 后，可以选择直接回答，也可以在回复中返回 `tool_calls`。`tool_calls` 是一组结构化的工具调用请求，例如：

```python
{
    "role": "assistant",
    "content": None,                # 可能为 null
    "tool_calls": [{
        "id": "call_abc123",
        "type": "function",
        "function": {
            "name": "calculate",
            "arguments": "{\"expression\": \"3+5*2\"}"
        }
    }]
}
```

### agent_loop 的 tool_calls 分支（`src/mini_agent/agent.py:55`）

v0.01 的 loop 只判断"无 tool_calls 就结束"。v0.02 补上了"有 tool_calls"分支：

```python
if not msg.get("tool_calls"):
    return msg.get("content", "")    # 无工具调用 = 最终回复，结束

# 有 tool_calls：执行，结果作为 role=tool 回灌
for tc in msg["tool_calls"]:
    name = tc["function"]["name"]
    args = json.loads(tc["function"]["arguments"])
    result = executor.execute(name, args)

    messages.append({
        "role": "tool",
        "tool_call_id": tc["id"],      # 必须对应 tool_calls 里的 id
        "content": str(result),
    })
# 循环回到顶部，带着工具结果再调 LLM
```

程序不能执行完工具就直接结束，还要把结果发回 LLM。这个动作称为“回灌”。

回灌消息使用 `role=tool`，并且必须携带原请求的 `tool_call_id`。因为一轮里可能有多个调用，所以 LLM 需要用这个 ID 判断每份结果属于哪个请求。所有结果回灌后，loop 才进入下一轮，让模型根据结果继续回答。

## 为什么这样设计

### 为什么 Tool 同时带"给 LLM 的描述"和"给 runtime 的 handler"

如果 schema 和 Python 函数分开注册，修改一边时可能漏掉另一边。`Tool` 把模型看到的描述和运行时执行的函数放在一起，因此注册一次就能同时建立两者的对应关系。

### 为什么 Executor 捕获异常而不是让 loop 崩

文件不存在、参数非法等工具错误很常见，模型拿到错误信息后还可能换参数重试。因此 `ToolExecutor.execute` 会捕获 handler 异常，并把错误作为工具结果回灌。

这个边界与第 1 课一致：工具层处理工具错误，核心 loop 不替 LLM 调用或 CLI 顶层异常兜底。

### 为什么 tool_calls 串行执行

v0.02 会按顺序执行同一轮中的多个 `tool_calls`。这样“取出请求 → 执行工具 → 回灌结果”的顺序最容易观察，但多个互不依赖的工具也必须逐个等待。第 6 课才会引入并发执行。

## 使用指导

### 本版可用的命令

```bash
# 命令行首条任务
PYTHONPATH=src python -m mini_agent "计算 3+5*2"

# 交互模式
PYTHONPATH=src python -m mini_agent

# 跑 smoke test
PYTHONPATH=src python tests/test_tools.py
```

### 本版典型示例

**示例 1：让 LLM 调 calculate**
```bash
PYTHONPATH=src python -m mini_agent "计算 123 * 456"
```
预期输出：
```
=== [1] LLM 回复 ===
  决策调用: calculate({'expression': '123 * 456'})
[Executor] 执行 Tool: calculate
[Executor] 参数: {'expression': '123 * 456'}
  执行结果: 56088

=== [2] LLM 回复 ===
123 × 456 的结果是 56,088。
```
这里一定会经历工具结果回灌，但模型何时给出最终文本由模型决定。示例中用了两轮：第 1 轮请求计算，第 2 轮根据结果回答。

**示例 2：LLM 自己能算的就不调工具**
```bash
PYTHONPATH=src python -m mini_agent "1+1等于几"
```
预期：LLM 可能直接回答"2"而不调 calculate——简单算术它自己能做。这取决于 LLM 的判断。

**示例 3：复杂表达式触发工具**
```bash
PYTHONPATH=src python -m mini_agent "计算 (100 + 200) * 3 / 5"
```
预期：LLM 调 `calculate({'expression': '(100 + 200) * 3 / 5'})`，结果 180。

### 本版独有特性

- **观察 role=tool 回灌**：看终端日志，第 1 轮 LLM 返回 `tool_calls`，执行后结果作为 `role=tool` 消息回灌，第 2 轮 LLM 才给出最终文本回复。
- **content 为 null**：LLM 只返回 tool_calls 时，`content` 字段可能是 `null`，agent.py 里用 `if msg.get("content")` 判空跳过打印。
- **无权限交互**：v0.02 所有工具直接执行，不问用户（v0.04 才加权限闸门）。

## 动手验证

1. **跑 smoke test**：
   ```bash
   PYTHONPATH=src python tests/test_tools.py
   ```
   预期：5 个 PASS + "全部 smoke test 通过"。

2. **让 agent 算数**：
   ```bash
   PYTHONPATH=src python -m mini_agent "计算 999 * 999"
   ```
   预期：LLM 调 calculate，结果 998001。

3. **观察两轮循环**：注意终端里 `=== [1]` 和 `=== [2]` 两个标记，理解"第 1 轮调工具、第 2 轮给答案"的流程。

## 本版完整代码

- [`src/mini_agent/tools/base.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.02/src/mini_agent/tools/base.py) — Tool / ToolRegistry / ToolExecutor
- [`src/mini_agent/tools/calc.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.02/src/mini_agent/tools/calc.py) — calculate 工具
- [`src/mini_agent/tools/__init__.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.02/src/mini_agent/tools/__init__.py) — 注册中心
- [`src/mini_agent/agent.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.02/src/mini_agent/agent.py) — 改造后的 call_llm + agent_loop
- [`tests/test_tools.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.02/tests/test_tools.py) — 工具 smoke test
