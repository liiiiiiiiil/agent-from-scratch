# 第 12 课：预算与裁剪

> 版本 v0.12 | [上一课](11-context-architecture.md) | [返回教程总览](README.md) | [下一课](13-context-compaction.md)

> 代码快照：`v0.12` · 相邻差异：`v0.11..v0.12` · 命令环境：Bash/zsh
>
> 运行要求：Python 3.10+。该 tag 的 `pyproject.toml` 仍标 3.9，但源码已使用 3.10 语法。

## 本课目标

对话和工具结果会不断累积。模型的 Context Window（上下文窗口）有限，等到服务端报超限错误时，这一轮请求已经失败了。

这一课让 Agent 在窗口有限时仍能继续工作：调用前先预留预算，优先缩短价值较低的内容，并且不破坏工具调用消息的对应关系。读完本课，你应该能够：

- 解释为什么上下文管理需要“预算”，而不只是等 API 返回超限错误。
- 看懂 `ContextBudget` 如何从模型窗口计算本次请求的消息上限。
- 说明为什么 tool calling（工具调用）消息必须按完整轮次裁剪。
- 通过小窗口观察 tool result（工具结果）截断和旧轮次删除。

本课不追求精确计算 token（模型处理文本的计量单位）。重点是三个直接的约束：**调用前检查、先处理低价值内容、不能破坏协议结构**。

## 前置条件与版本切换

- 已读第 11 课，理解 `AgentState` 与 LLM Context（发给模型的上下文）已经分离。
- 切换到本版代码：

```bash
git checkout v0.12
git diff --stat v0.11..v0.12
git diff v0.11..v0.12 -- src/mini_agent/context.py src/mini_agent/config.py src/mini_agent/config_example.py
```

如果需要继续开发而不是只阅读 tag，请回到你的开发分支后再修改文件。

## 上一版的问题

v0.11 已把状态与上下文分开，但 `prepare_messages()` 仍会发送完整 history。任务轮次变多后，以下内容会不断累积：

- system prompt（系统提示词）和用户任务。
- assistant 发出的 tool calls（工具调用）。
- 文件内容、搜索结果和 shell 输出等 tool results（工具结果）。
- assistant 的阶段性判断。

最终请求可能超过模型的 Context Window。等服务端报错再处理有两个问题：当前轮已经失败，调用方也不知道哪些消息能安全删除。

因此 v0.12 一定会在调用 LLM 之前检查预算。

## 新增与改动文件

| 文件 | 改动 |
|---|---|
| `src/mini_agent/context.py` | 新增 token 估算、预算模型、轮次划分与 `TrimPolicy` |
| `src/mini_agent/config.py` | 新增 `CONTEXT_WINDOW = 128_000` |
| `src/mini_agent/config_example.py` | 展示可覆盖的窗口配置 |
| `CHANGELOG.md` / `AGENTS.md` | 记录 v0.12 的设计决策与版本状态 |

## 关键流程

`agent.py` 不需要修改。v0.11 已经让 Agent loop（代理循环）统一调用 `context_manager.prepare_messages()`，所以预算策略仍然只落在 ContextManager 内：

```text
agent_loop
    |
    v
ContextManager.prepare_messages()
    |
    +-- 复制完整 history
    +-- 计算预算
    +-- TrimPolicy.trim()
    v
call_llm(prepared_messages)
```

当预算未超限时，这条流程返回消息字典的副本；超限时只修改副本，并按后文的优先级截断或删除。无论哪条路径，完整 `history` 都继续由 loop 保存。

## 实现拆解

### 1. Token 只需要足够好的估算

精确分词需要额外依赖，但本项目保持零第三方依赖，因此不引入 `tiktoken`。`count_tokens()` 用 `len(text) // 3` 粗略估算字符串，并递归统计消息中的 dict、list 和 tuple：

```python
def count_tokens(text_or_messages: object) -> int:
    if text_or_messages is None:
        return 0
    if isinstance(text_or_messages, str):
        return len(text_or_messages) // 3
    if isinstance(text_or_messages, dict):
        return sum(count_tokens(value) for value in text_or_messages.values())
    if isinstance(text_or_messages, (list, tuple)):
        return sum(count_tokens(value) for value in text_or_messages)
    return len(str(text_or_messages)) // 3
```

这不是 tokenizer（分词器）。它不会计算协议包装开销，也不能精确反映不同模型对中英文和标点的编码差异。

这里仅用它判断“是否应开始缩短上下文”，所以允许合理误差。输出预留和历史比例会吸收一部分误差。

### 2. 预算不是一个固定上限

一次请求不能只设一个固定上限，因为输入、模型输出和历史消息都要占窗口。`ContextBudget` 用三个量描述这些边界：

```python
@dataclass(frozen=True)
class ContextBudget:
    window: int = CONTEXT_WINDOW
    output_reserve_ratio: float = 0.15
    history_ratio: float = 0.45
```

| 字段 | 默认值 | 含义 |
|---|---:|---|
| `window` | `128_000` | 目标模型的 Context Window |
| `output_reserve_ratio` | `0.15` | 给模型输出预留 15% |
| `history_ratio` | `0.45` | `prefix`（受保护前缀）之外的历史最多使用 45% |

三个派生值的关系是：

```python
input_limit = int(window * (1 - output_reserve_ratio))
history_limit = int(window * history_ratio)
message_limit = max(
    protected_tokens,
    min(input_limit, protected_tokens + history_limit),
)
```

例如窗口为 100，输出预留 20%，历史比例 40%，而 system + 首条 user task 占 10 token：

```text
input_limit   = 100 * 80% = 80
history_limit = 100 * 40% = 40
message_limit = min(80, 10 + 40) = 50
```

这里的 `max(protected_tokens, ...)` 是保底规则。即使 system 和首条 user task 自己超过预算，它们也一定保留。

此时 ContextManager 会打印超限日志，不会为了满足数字目标删掉任务本身。

可在不进 git 的 `src/mini_agent/config_local.py` 中覆盖模型窗口：

```python
CONTEXT_WINDOW = 32_000
```

### 3. 按协议轮次原子裁剪

工具调用消息不能随便单条删除。一次工具轮次由一条带 `tool_calls` 的 assistant 消息和紧随其后的全部 tool result 组成：

```text
assistant(tool_calls: call-1, call-2)
tool(tool_call_id: call-1)
tool(tool_call_id: call-2)
```

OpenAI tool calling 协议要求每条 `role=tool` 消息都对应前面的 `tool_call_id`。只删除 assistant 消息会留下孤儿 tool result；只删除一个 tool result 又会让 assistant 声明的调用缺少响应。这两种情况都可能被 API 拒绝。

因此 `_split_rounds()` 先找首条 user 消息。它和之前的 system 消息组成不能删除的 `prefix`；后续消息再切成 `rounds`（历史轮次）。遇到 tool-calling assistant 时，它和后续连续的 tool 消息会进入同一个列表：

```python
if _is_tool_call_message(message):
    round_messages = [message]
    index += 1
    while index < len(messages) and messages[index].get("role") == "tool":
        round_messages.append(messages[index])
        index += 1
    rounds.append(round_messages)
```

普通 assistant 或后续 user 消息各自构成单消息轮次。裁剪可能缩短某条 tool result 的文本，但删除历史时一定只删除完整 `round`（轮次）。

`_split_rounds()` 只按连续的 `role=tool` 消息分组，不负责修复已经非法的 `tool_call_id`；正常协议序列由 agent loop 保证。如果 history 中没有 user 消息，所有消息都会留在受保护的 `prefix` 中，不会有可删除的历史轮次。

### 4. 裁剪算法

如果直接改 history，之后就无法还原完整记录。`TrimPolicy.trim()` 因此总是先浅复制消息字典，所有修改只作用于本次发送的副本：

```python
prepared = [dict(message) for message in messages]
```

随后它按从低价值到高价值的顺序处理：

1. 如果估算值未超过 `message_limit`，直接返回副本。
2. 从最老轮次开始检查 tool result。
3. 对超过 120 字符的结果做定量截断，首尾都保留，中间加入省略标记。
4. 如果仍超限，从最老的完整 round 开始删除。
5. rounds 全部删完仍超限时，保留 prefix 并打印 protected messages 超限日志。

工具输出的结论、报错或汇总经常出现在末尾，所以截断时会保留首尾，而不是只留开头：

```text
beginning of result
[... omitted 780 characters ...]
ending of result
```

`needed_characters = (current_tokens - target) * 3` 把要节省的 token 换算为字符数。`minimum_tool_result_characters = 120` 防止结果被反复压到完全不可读。

如果截断仍不够，算法会删除完整轮次，因此最终一定会收敛。

## 为什么需要本版

v0.12 的方案围绕“请求不能超限，同时不能破坏任务和协议”做了几项取舍：

- **用启发式估算而不是精确 tokenizer**：保持零第三方依赖；代价是估算会有误差，所以用输出预留和比例预算留出余量。
- **用比例而不是固定历史条数**：能适配不同模型窗口；代价是实际可保留的消息数量会随内容长度变化。
- **先截断 tool result，再删除完整轮次**：尽量保留工具结论，同时保证 tool calling 协议完整；代价是旧轮次最终可能完全丢失。
- **把策略放在 ContextManager**：Agent loop（代理循环）只负责调用统一入口，后续更换策略不必改 loop；代价是 loop 不直接知道本轮上下文被裁掉了什么。
- **保留受保护前缀和完整 history**：任务仍可执行，原始记录仍可审计；代价是前缀本身可能超过预算，且 v0.12 不提供历史摘要。

### 为什么不修改原始 history

`history` 保存本地完整执行记录，`prepared_messages` 只是某一次 LLM 请求的临时视图：

```text
完整 history ---------------------> 下一轮继续追加
      |
      +-- copy --> trim --> LLM request
```

直接裁掉 history 后，就无法更换策略、重新摘要或排查 Agent 实际执行过什么。v0.12 也不会把 `AgentState` 注入 messages；这两个边界为后续版本保留了可靠的数据源。

### 本版边界

`ContextBudget` 的非法窗口或比例会在构造时抛出 `ValueError`；受保护内容超限时记录日志，但不会删除 system 或首条 user 消息。`_split_rounds()` 假设 history 来自正常的 Agent loop，不负责修复任意格式错误的消息。历史摘要和 State 注入不属于本版。

## 运行与观察（按需）

要观察上述流程，可以用极小窗口构造一条工具轮次；该命令只调用 `ContextManager`，不需要真实 LLM：

```bash
PYTHONPATH=src python - <<'PY'
from mini_agent.context import ContextBudget, ContextManager
from mini_agent.state import AgentState

history = [
    {"role": "system", "content": "system"},
    {"role": "user", "content": "读取日志并定位错误"},
    {"role": "assistant", "content": None, "tool_calls": [{"id": "call-1"}]},
    {"role": "tool", "tool_call_id": "call-1", "content": "HEAD-" + "x" * 900 + "-TAIL"},
]
context = ContextManager(
    AgentState(task="读取日志并定位错误"),
    history,
    ContextBudget(window=250, output_reserve_ratio=0, history_ratio=0.8),
)

prepared = context.prepare_messages()
print(prepared[-1]["content"])
print("history unchanged:", len(history[-1]["content"]) == 910)
PY
```

可观察到类似日志：

```text
[Context] token budget exceeded: ...
[Context] truncated tool result call-1, saved ... tokens
[Context] prepared context: ... tokens
```

最后一行应为 `history unchanged: True`，说明发送副本被缩短而原记录未被污染。

## 本版特性、下一课与代码索引

- 调用 LLM 前进行预算检查。
- tool result 支持保留首尾的定量截断。
- tool calling 历史按协议轮次原子删除。
- 原始 history 和 AgentState 均不受裁剪影响。

下一课会讨论如何在删除旧轮次前保留一份可压缩的历史信息，并继续沿用本课的预算与协议边界。

### tag 固定代码索引

- [`src/mini_agent/context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.12/src/mini_agent/context.py) — 预算与 TrimPolicy
- [`src/mini_agent/config.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.12/src/mini_agent/config.py) — CONTEXT_WINDOW 配置
- [`src/mini_agent/config_example.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.12/src/mini_agent/config_example.py) — 配置模板
- [`src/mini_agent/agent.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.12/src/mini_agent/agent.py) — prepare_messages 调用入口
