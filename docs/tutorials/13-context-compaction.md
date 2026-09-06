# 第 13 课：上下文压缩（v0.13）

上一课：[预算与裁剪](12-token-budget-trimming.md) · [教程总览](README.md) · 下一课：[项目级指令](14-project-instructions.md)

> 代码快照：`v0.13` · 相邻差异：`v0.12..v0.13` · 命令环境：Bash/zsh
>
> 运行要求：Python 3.10+。历史 tag 的 `pyproject.toml` 仍标记 Python 3.9，但源码已使用 3.10 语法。

## 本课目标

第 12 课的 trimming（裁剪）能让请求回到预算内，但会截断工具结果或删除旧轮次。长任务因此可能丢失已经验证过的结论。本课介绍 v0.13 的 compaction（压缩）：把较老的完整轮次交给一次不带工具的 LLM 请求整理成 `Historical Summary`（历史摘要），同时保留近期原文，并从 `AgentState` 重新生成 `Structured State`（结构化状态）。

读完本课，你应该能够：

- 区分完整 `history`、历史摘要、结构化状态和近期原文的职责；
- 按顺序解释预算超限时 trimming、compaction 和失败回退；
- 说明摘要请求为什么关闭 tools（工具定义）和终端流式输出；
- 理解摘要器注入点、重复压缩以及 `keep_rounds` 的边界。

本课的核心不变量是：**摘要允许有损，执行状态必须来自真实工具结果。**

## 前置条件与版本切换

- 已读第 11、12 课，理解 `AgentState` 与消息历史分离、预算计算和工具轮次原子性。
- 以下命令均适用于 Bash/zsh。`git diff` 用于观察相邻 tag 的真实改动；阅读完成后切回 v0.13。

```bash
git checkout v0.12
git diff --stat v0.12..v0.13
git diff v0.12..v0.13 -- src/mini_agent/context.py src/mini_agent/agent.py src/mini_agent/config.py src/mini_agent/config_example.py
git checkout v0.13
```

## 新增与改动文件

| 文件 | 变化 | 作用 |
|---|---|---|
| `src/mini_agent/context.py` | 增加摘要器注入、状态渲染、`compact()` 和压缩后的消息重建 | 在统一的 `prepare_messages()` 入口内完成压缩与回退 |
| `src/mini_agent/agent.py` | 增加 `summarize_messages()` | 复用已有 HTTP/LLM 通道执行一次内部摘要请求 |
| `src/mini_agent/config.py` | `MAX_ITERATIONS` 从 10 调为 50 | 允许长任务跨越多次上下文压缩 |
| `src/mini_agent/config_example.py` | 同步迭代上限示例 | 展示默认配置 |
| `tests/test_context.py` | 增加压缩、失败、多次压缩和自动触发覆盖 | 固化 ContextManager 的行为边界 |
| `tests/test_loop.py` | 增加长任务覆盖 | 验证提高上限后 loop 可以继续运行 |

## 上一版的问题

v0.12 的 `prepare_messages()` 始终从完整 `history` 构造发送副本，然后按“截断工具结果、删除最老完整轮次”的顺序处理。它解决了请求过长，却无法保留被删除轮次中的决策和结论：

```text
完整 history --trimming--> 较短请求
     |
     +-- 旧方案没有语义记录，模型可能忘记已经尝试过什么
```

无限保留原文会再次超过窗口，只保留摘要又会丢失当前局部推理所需的工具调用关系。因此本版需要三种信息共同工作：摘要补回较老的语义、近期轮次保留原文、State 锚定执行事实。

## 版本变更定位

v0.13 不改变 v0.12 的 trimming 策略，也不修改主 loop 的调用方式。新增入口仍是 `ContextManager.prepare_messages()`；它在判断初始视图超预算后调用 `compact()`，摘要请求由 `agent.summarize_messages()` 提供。

```text
v0.12：agent_loop -> prepare_messages() -> TrimPolicy.trim() -> call_llm()

v0.13：agent_loop -> prepare_messages()
                       |-- TrimPolicy.trim()        (先生成兜底副本)
                       |-- compact()                (超预算且存在旧轮次)
                       |     |-- summarizer(prompt)  (无 tools、无终端流)
                       |     +-- 保存 Summary，标记压缩状态
                       +-- 重建 State + Summary + Recent rounds
                       -> TrimPolicy.trim() -> call_llm()
```

主要消费者仍是主 LLM 请求。`AgentState` 是摘要之外的事实来源；本版不负责摘要持久化、外部记忆或摘要质量评分。

## 为什么这样设计

本版选择“摘要 + 近期原文 + 结构化状态”，而不是只保留一种信息：

- **摘要**降低旧历史带来的遗忘，但它由模型转述，可能遗漏或改写细节；
- **近期原文**保留当前 tool calling（工具调用）所需的 assistant/tool 配对，避免模型只依赖摘要继续推理；
- **Structured State**由执行器记录的真实结果生成，不从摘要猜测文件、错误或工具成败。

摘要复用已有 `call_llm()` 通道，保持标准库 HTTP 客户端和配置单一；代价是预算超限时多一次网络请求。摘要失败、返回空字符串或非字符串时回退到 v0.12 的 trimming，因此压缩是增强能力，不是主 loop 的新单点故障。

本版也把容错放在 `ContextManager`：loop 只需要调用 `prepare_messages()`，不需要理解摘要格式或失败原因。完整 `history` 始终保留在本地，发送副本的裁剪不会破坏后续摘要来源。

## 关键流程

压缩成功后的请求视图按如下顺序组成：

```text
Original System       <- 原始 system 消息
+ Structured State    <- 每次构建时从 state.snapshot() 重新渲染
+ Historical Summary  <- summarizer 返回的有损摘要（可没有）
+ Current Task        <- 原始首条 user 消息
+ Recent Messages     <- 最近 keep_rounds 个完整轮次，默认 6
```

自动触发的具体顺序是：

1. `_build_messages()` 构造当前视图并计算受保护前缀；
2. 记录该视图是否超过 `message_limit`；
3. `TrimPolicy.trim()` 先返回协议安全的降级副本；
4. 初始视图超限时调用 `compact()`；
5. 压缩成功则按 State、Summary 和近期轮次重建，再 trimming 一次；失败或不满足条件则直接使用第 3 步结果。

只有“初始视图超预算”且“完整 history 的轮次数量多于 `keep_rounds`”才会自动压缩。也可以主动调用 `context.compact(keep_rounds=6)`。

## 实现拆解

### 1. 保留事实锚

压缩模式下，`_render_state()` 每次读取最新的 `AgentState.snapshot()`：

```python
messages = prefix[:1] + [self._render_state()]
if self._summary:
    messages.append({
        "role": "system",
        "content": "[Historical Summary]\n" + self._summary,
    })
messages.extend(prefix[1:])
messages.extend(recent_messages)
```

因此新发生的文件修改和工具错误会出现在下一次请求中。Summary 只负责延续语义，不能替代 State 的事实记录；近期消息仍由 `_split_rounds()` 按完整轮次选取，assistant 的 `tool_calls` 与对应 `role=tool` 结果不会被拆开。

### 2. 隔离摘要请求

`ContextManager` 接受可注入的 `summarizer`，生产默认值采用延迟导入，避免 `context.py` 与 `agent.py` 在加载时循环依赖：

```python
def summarizer(messages):
    from mini_agent.agent import summarize_messages
    return summarize_messages(messages)
```

摘要函数只改变两个调用选项，仍复用 `call_llm()` 的 `http.client` 通道：

```python
def summarize_messages(messages):
    return call_llm(
        messages,
        include_tools=False,
        stream_output=False,
    ).get("content", "") or ""
```

关闭 tools 防止摘要过程产生文件或 shell 副作用；关闭流式输出则让内部摘要不显示为 Agent 的终端回复。HTTP 层仍按流式协议收集完整内容。

### 3. `compact()` 的输入、输出和失败路径

`compact(keep_rounds=None)` 返回布尔值。它从完整 history 划分轮次，只把“旧轮次”组成摘要 prompt，近期轮次不送入本次摘要：

```text
rounds <= keep_rounds       -> False，什么也不改变
keep_rounds < 0             -> ValueError
summarizer 异常/空/非字符串  -> False，保留旧 summary，继续 trimming
有效字符串                  -> 保存 summary，标记压缩成功，返回 True
```

摘要 prompt 要求按任务、已完成步骤、工具调用、修改文件、错误、当前进度和下一步组织，并禁止虚构。多次压缩时还会带上已有摘要，让新增的旧轮次接续之前的语义。

成功后 `_summary` 更新、`_compacted` 设为真、`_summarized_rounds` 前移；原始 history 不会被删除或改写。`keep_rounds=0` 表示所有轮次都进入摘要，发送视图只保留前缀、State 和 Summary。

实现只在摘要返回有效文本后改变压缩状态；这也是失败回退能够保持旧状态的原因：

```python
summary = self.summarizer(prompt)
if not isinstance(summary, str) or not summary.strip():
    return False
self._summary = summary.strip()
self.keep_rounds = keep
self._compacted = True
return True
```

### 4. 多次压缩的不变量

长任务在首次压缩后仍会追加新消息。再次超限时，只摘要尚未处理的旧轮次，近期轮次继续保留。由于摘要是有损的，不能要求多次摘要逐字稳定；实现必须保持以下不变量：

- State 每次从最新 snapshot 渲染；
- 完整 history 始终可用于下一次压缩；
- 近期 assistant/tool 消息保持协议合法；
- 摘要失败不会覆盖已有有效摘要。

## 设计边界

- 估算仍使用 v0.12 的启发式 token 计数，不保证与服务端 tokenizer 完全一致。
- `_summary` 只存在当前进程，不写磁盘，也不提供向量检索或外部记忆。
- State 只包含现有执行字段，不是完整事件日志；摘要器不会凭空补齐缺失事实。
- 受保护的 system、任务、State 和 Summary 本身若超过模型窗口，compaction 也无法解决；系统会保留它们并允许预算超限日志出现。
- `MAX_ITERATIONS` 提高到 50 只提供更长的执行机会，不能保证任务一定收敛。

## 运行与观察

配置好本地 LLM 后运行真实任务。命令行首条任务处理完成后，程序仍进入交互循环：

```bash
PYTHONPATH=src python -m mini_agent "检查登录流程并运行回归测试"
```

当请求视图超出预算且存在足够旧轮次时，可以在后续请求中观察到 `[Structured State]`、`[Historical Summary]` 和最近的完整工具轮次。摘要请求失败时，主任务继续使用 trimming 结果。

## v0.13.1 补丁：Context Observability

v0.13.1 是本课的补丁 tag，增加上下文统计和生命周期事件，不改变压缩策略：

> 代码快照：`v0.13.1` · 相邻差异：`v0.13..v0.13.1` · 命令环境：Bash/zsh

```bash
git checkout v0.13
git diff --stat v0.13..v0.13.1
git diff v0.13..v0.13.1 -- src/mini_agent/context.py tests/test_context.py
git checkout v0.13.1
```

`ContextManager.prepare_messages()` 现在会为实际发送的消息保存 `ContextStats`，并可发出 `prepared`、`trimmed` 和 `compacted` 事件。五个输入分桶互不重叠：`system`、`task`、`state`、`history`、`tool_result`；`reserve` 是输出预留，不计入输入 token 总和。

```python
context.prepare_messages()
stats = context.stats_snapshot()
print(stats.tokens, stats.tool_result)
```

默认 observer 会打印上下文、裁剪和压缩日志；`CONTEXT_OBSERVABILITY = False` 只关闭默认日志，不影响预算或 `stats_snapshot()`。也可以传入 `observer=callback`，observer 抛出的异常会被隔离，不影响 Agent 执行。

补丁仍不负责持久化统计、远程指标或摘要质量评估。它只是让本课已经存在的上下文决策可观察。

## 本版特性、下一课与代码索引

v0.13 在 v0.12 的预算与 trimming 之上增加历史摘要、Structured State 注入、自动/主动压缩、失败回退和多次压缩；同时把迭代上限调到 50。v0.13.1 补充可选的统计和事件观测。下一课 v0.14 会把项目级指令作为新的受保护上下文注入，每次压缩都应继续保留它们。

完整实现固定在对应 tag：

- [`src/mini_agent/context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.13/src/mini_agent/context.py) — v0.13 压缩与消息重建
- [`src/mini_agent/agent.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.13/src/mini_agent/agent.py) — v0.13 摘要请求
- [`tests/test_context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.13/tests/test_context.py) — v0.13 压缩边界
- [`src/mini_agent/context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.13.1/src/mini_agent/context.py) — v0.13.1 统计与事件
- [`tests/test_context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.13.1/tests/test_context.py) — v0.13.1 观测性覆盖
