# 第 12 课：预算与裁剪

v0.12 让 Agent 在 Context Window 有限时仍能继续工作。核心不在精确计算 token，而在调用 LLM 前主动减少低价值历史，并始终保持 OpenAI tool calling 消息协议合法。

## 本版改动

| 文件 | 改动 |
|---|---|
| `src/mini_agent/context.py` | 新增 token 估算、预算模型、轮次划分和裁剪策略 |
| `src/mini_agent/config.py` | 新增 `CONTEXT_WINDOW` 配置 |
| `tests/test_context.py` | 新增预算、截断、原子删除和保底消息测试 |

`agent.py` 不需要改。它仍然只调用 `context_manager.prepare_messages()`；上下文管理继续收敛在 ContextManager 内。

## 1. Token 是估算值

本项目保持零第三方依赖，不引入 `tiktoken`。裁剪决策使用简单启发式：

```python
def count_tokens(text_or_messages):
    if isinstance(text_or_messages, str):
        return len(text_or_messages) // 3
```

中英文混合时它不精确，但只用于决定是否应当裁剪，允许有合理误差。

## 2. 用比例表达预算

```python
@dataclass(frozen=True)
class ContextBudget:
    window: int = CONTEXT_WINDOW
    output_reserve_ratio: float = 0.15
    history_ratio: float = 0.45
```

- `window`：目标模型可用的上下文窗口，默认 `128000`。
- `output_reserve_ratio`：预留给模型回复的空间。
- `history_ratio`：历史消息可使用的窗口比例。

可在 `config_local.py` 覆盖：

```python
CONTEXT_WINDOW = 32_000
```

system instructions 和首条 user task 是保底内容。它们即使超过预算也不会被删除。

## 3. 为什么必须按轮次删除

一次工具轮次是：

```text
assistant(tool_calls)
tool result 1
tool result 2
...
```

OpenAI 协议要求每条 `role=tool` 消息都对应此前 assistant 消息中的 `tool_call_id`。只删 assistant 或只删某条 tool result 都会留下无效上下文。

`_split_rounds()` 因此将带 `tool_calls` 的 assistant 消息和其后连续 tool results 组成一个原子单元。需要删除历史时，整个单元一起删除。

## 4. 裁剪优先级

`TrimPolicy.trim()` 每次从完整 history 复制出一个可发送副本，按如下顺序降低上下文占用：

1. 从最老轮次开始截断 tool result，保留首尾和省略标记。
2. 仍超限时，从最老的完整轮次开始删除。
3. 没有可删历史时，只保留 system 与首条 user task。

原始 `history` 不会被修改，`AgentState` 也不进入 context。这两个边界使 v0.13 可以在不丢失执行事实的前提下加入历史摘要。

运行时可观察到类似日志：

```text
[Context] token budget exceeded: 5200/4000
[Context] truncated tool result call-1, saved 800 tokens
[Context] removed oldest round, saved 1200 tokens
[Context] prepared context: 3900/4000 tokens
```

## 5. 验证

```bash
PYTHONPATH=src python tests/test_context.py
PYTHONPATH=src python tests/test_loop.py
```

`test_context.py` 覆盖以下红线：

- system 和首条 user task 永远保留。
- tool result 先截断，原始 history 不被污染。
- 删除一个工具轮次时，不会保留孤儿 `role=tool` 消息。
- 极小预算下，裁剪仍然收敛并返回协议合法消息。

## 本版边界

v0.12 只做预算与裁剪，不调用 LLM 摘要历史，也不把 Structured State 注入 messages。老历史的摘要、失败降级和更长的 agent loop 属于 v0.13。
