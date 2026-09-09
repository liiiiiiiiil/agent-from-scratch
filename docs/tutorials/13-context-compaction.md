# 第 13 课：上下文压缩（v0.13）

上一课：[预算与裁剪](12-token-budget-trimming.md) · [教程总览](README.md) · 下一课：[项目级指令](14-project-instructions.md)

> 代码快照：`v0.13` · 相邻差异：`v0.12..v0.13` · 命令环境：Bash/zsh

> 运行要求：Python 3.10+。历史 tag 的 `pyproject.toml` 标记为 Python 3.9，但源码已经使用 Python 3.10 语法。

## 本课目标

上一课让超长请求能继续发送，但为了满足预算，它会截断工具输出，再删除最早的完整轮次。这些被删掉的内容可能包含已经尝试过的方案和已经确认的结论。

本课加入上下文压缩（compaction）：把较老的完整轮次交给一次内部 LLM 请求写成历史摘要，同时保留最近几轮原文，并把执行器记录的状态重新放进请求。读完后，你应能解释：为什么摘要可以有损，而 `AgentState` 必须仍是执行事实的来源。

本课末尾还以 v0.13.1 补丁增补 Context Observability：你应能读懂 `ContextStats` 的五个 token 分桶，调用 `ContextManager.stats_snapshot()`，区分 `prepared`、`trimmed`、`compacted` 事件，并配置默认终端日志与 `CONTEXT_OBSERVABILITY` 开关；同时理解 observer 异常会被隔离，不会影响 agent。

## v0.13.2：任务边界隔离

上下文压缩解决的是“历史太长”，不自动解决“任务已经切换”。同一 CLI 会话中，普通
后续输入默认继续当前任务；使用 `/new <任务>` 才会清空旧任务并开始新任务，`/reset`
清空当前任务并等待下一条输入。任务完成后也不会靠 `done` 状态猜测用户意图。

任务边界会原地清空所有任务级事实：Todo、工具记录、文件变更、错误、验证证据、
generation、FailureEvent、RecoveryAction、retry/fingerprint/recovery/repair 预算和
后续版本中的 checkpoint。history、Historical Summary、压缩游标和 runtime notice 也会
清空；system prompt、项目级指令和会话级权限授权保留。同步 CLI 只在当前工具批次完整
回灌后读取下一条输入，因此不会在 tool call 与 tool result 之间制造孤儿消息。

Structured State 每轮都会从最新状态重新渲染，并有固定字符上限；任务锚点、当前目标和
状态优先保留，过长的诊断历史会被确定性截断。它是任务事实投影，不会修改本地 State。

## 上一版的问题

v0.12 的 `ContextManager.prepare_messages()` 从完整 `history` 制作副本，再调用 `TrimPolicy.trim()`。它不会改写本地完整历史，但发送给模型的副本可能已经没有早期轮次。

这对协议是安全的：一轮带 `tool_calls` 的 assistant 消息会和紧随其后的所有 tool 结果一起保留或删除。但它不保留语义。任务足够长时，模型不知道哪些文件已改、哪些命令失败过，容易重复工作。

本版不试图无限保存原文，而是让三种信息分工：旧轮次用摘要保留大意，近期轮次保留原文和工具调用关系，`AgentState` 保留由真实工具结果产生的事实。

## 前置条件与版本切换

建议先阅读第 11、12 课，理解 `AgentState` 与消息历史分离、token 预算和完整轮次的含义。以下命令适用于 Bash/zsh：

```bash
git checkout v0.12
git diff --stat v0.12..v0.13
git diff v0.12..v0.13 -- src/mini_agent/context.py src/mini_agent/agent.py src/mini_agent/config.py src/mini_agent/config_example.py
git checkout v0.13
```

## 新增与改动文件

先用上面的 `git diff --stat` 确认范围。本课只聚焦下面直接参与压缩流程的改动。

| 文件 | 变化 | 作用 |
|---|---|---|
| `src/mini_agent/context.py` | 扩展 `ContextManager` | 生成摘要、重建发送视图，并在失败时回退到裁剪结果 |
| `src/mini_agent/agent.py` | 扩展 `call_llm()`，新增 `summarize_messages()` | 复用现有 LLM 通道完成无工具的内部摘要请求 |
| `src/mini_agent/config.py` | `MAX_ITERATIONS` 从 10 调为 50 | 给长任务更多轮次继续执行 |
| `src/mini_agent/config_example.py` | 同步默认轮次 | 保持示例配置一致 |

## 版本变更定位

图例：

```text
[旧] 上一版已有    [+] 本版新增    [~] 本版修改
[C] 主要消费者     [B] 本版边界或降级路径
```

v0.12 的入口、数据流和收口如下。`history` 是完整本地记录；`trim()` 返回的是可发送副本。

```text
v0.12

[旧] agent_loop
        |
        v
[旧] ContextManager.prepare_messages()
        |
        v
[旧] TrimPolicy.trim(history, budget) ---> [C] call_llm(messages)
        |                                      |
        +-- 截断 tool 内容 / 删除最老完整轮次 --+-> 下一轮追加到 history
```

v0.13 在同一入口插入压缩。首次构建的视图超预算时，仍先取得 `trim()` 的降级副本；只有 `compact()` 成功，才以压缩视图重新裁剪。摘要异常、空字符串或非字符串都走这条已有的降级路径。

```text
v0.13

[旧] agent_loop -> [~] prepare_messages() -> [旧] _build_messages()
                                           |          |
                                           |          +-- 初始视图未超限 --> [旧] trim() --> [C] call_llm()
                                           |
                                           +-- 初始视图超限
                                                |
                                                +-> [旧] trim()：先得到降级副本
                                                |
                                                +-> [+] compact()
                                                     |-- [+] summarizer(prompt)
                                                     |     `-> [~] call_llm(tools=False,
                                                     |                     stream_output=False)
                                                     |
                                                     +-- 成功 --> [+] State + Summary + Recent
                                                     |              -> [旧] trim() -> [C] call_llm()
                                                     `-- [B] 异常/空/非字符串 --> 先前的降级副本
```

`ContextManager` 是这里的主要控制点。主 loop 仍只调用 `prepare_messages()`，不需要知道摘要格式，也不捕获摘要失败。

## 核心概念与数据结构

### 1. 压缩的是发送视图，不是完整历史

要解决的问题是既不能无限发送原文，也不能为了缩短请求直接遗忘所有旧信息。直观地说，`history` 像完整档案，压缩后的 messages 是下一次交给模型的工作摘要。

`_split_rounds()` 先把 system 消息和首条 user 任务放入受保护前缀，再把余下消息分成轮次。带工具调用的 assistant 消息与连续的 tool 结果属于同一轮。这个划分保证近期原文不会留下没有对应 `tool_calls` 的 tool 消息。

压缩成功后，`_build_messages()` 只改造发送视图：

```python
prefix, rounds = _split_rounds([dict(message) for message in self.history])
recent = rounds[-self.keep_rounds:] if self.keep_rounds else []
messages = prefix[:1] + [self._render_state()]
if self._summary:
    messages.append({"role": "system", "content": "[Historical Summary]\\n" + self._summary})
messages.extend(prefix[1:])
messages.extend(message for round_messages in recent for message in round_messages)
```

因此正常请求的顺序是：原始 system、`[Structured State]`、可选的 `[Historical Summary]`、原始首条任务、最近的完整轮次。`history` 本身没有删除或改写，后续压缩仍以它为来源。

默认 `keep_rounds` 是 6；`keep_rounds=0` 表示没有近期原文，所有轮次都可作为摘要候选。负数没有合理含义，`compact()` 会抛出 `ValueError`。

### 2. State 是事实锚，摘要只是语义线索

摘要是模型转述，可能遗漏细节。执行状态则由工具执行结果更新，包含任务、当前目标、改动文件、错误、状态和最近四次已完成工具。压缩模式每次构建视图都重新调用 `state.snapshot()`：

```python
snapshot = self.state.snapshot()
return {
    "role": "system",
    "content": (
        "[Structured State]\\n"
        f"Task: {snapshot['task']}\\n"
        f"Files changed: {', '.join(snapshot['files_changed']) or '(none)'}\\n"
        f"Errors: {', '.join(snapshot['errors']) or '(none)'}\\n"
        f"Status: {snapshot['status']}\\n"
        f"Tools executed: {len(snapshot['tool_history'])}\\n"
    ),
}
```

这段代码解决的不是“让摘要更准确”，而是避免把摘要当作唯一事实来源。即使摘要省略了一个写文件操作，下一次请求仍能从 `Files changed` 得知它发生过。边界也很明确：`AgentState` 不是完整事件日志，未记录的细节不能由它补回。

### 3. 摘要请求不携带工具

摘要器接收的不是全部对话，而是较老的轮次，并要求按任务、已完成步骤、最近成功工具调用、改动文件、错误、当前进度和下一步组织。已有摘要会一并给它，因而后续压缩能合并新进入“旧历史”范围的轮次。

生产代码通过延迟导入注入默认摘要器，避免 `context.py` 加载时和 `agent.py` 形成循环导入：

```python
def summarizer(messages: list[Message]) -> str:
    from mini_agent.agent import summarize_messages
    return summarize_messages(messages)
```

实际摘要调用复用已有的 `http.client` 请求路径，但明确关闭工具 schema 和终端流式输出：

```python
def summarize_messages(messages):
    return call_llm(
        messages,
        include_tools=False,
        stream_output=False,
    ).get("content", "") or ""
```

这避免内部请求产生工具调用，也不把摘要文本显示成面向用户的 Agent 回复。底层请求仍按流式响应读取并收集内容；`stream_output=False` 只禁止逐块打印。

## 为什么这样设计

只保留摘要最省空间，却会丢失最近工具调用的原始协议关系；只保留原文最准确，却会再次超出窗口；只依赖状态则会失去许多任务语义。因此 v0.13 选择“摘要 + 近期原文 + 结构化状态”。

这个方案的收益是：旧历史有可读的延续线索，近期轮次保持原样，执行事实不依赖模型复述。代价是首次压缩会多一次 LLM 网络请求，而且摘要本身可能漂移。为避免这次增强成为主任务的新故障点，异常处理放在 `ContextManager.compact()` 内，主 loop 的 LLM 与 CLI 异常边界保持不变。

## 设计边界

- 仅当压缩前的消息视图超出预算，且完整历史的轮次数大于 `keep_rounds` 时，`prepare_messages()` 才自动尝试压缩。
- `compact()` 返回布尔值。轮次不足、没有尚未摘要的旧轮次、摘要器不存在、摘要器异常、返回空白或非字符串时返回 `False`；只有有效摘要才更新 `_summary`、`_compacted` 和 `_summarized_rounds`。
- 摘要失败时，`prepare_messages()` 返回先前已经算出的 trimming 副本。失败不会覆盖已有有效摘要，也不会改写 `history`。
- 多次压缩只处理尚未摘要、且已离开近期窗口的轮次；摘要内容允许有损，不能要求每次结果逐字一致。
- 预算估算仍是 `len(text) // 3`，并不等同于服务端 tokenizer。若受保护消息本身已经超过预算，压缩也不能让它们被删除。
- 本版不持久化摘要、不做向量检索、不评估摘要质量。`MAX_ITERATIONS=50` 只是把最大循环次数从 10 提高到 50，不保证任务一定完成。

## 关键流程

下面是一次超预算请求的实际顺序：

```text
history + state
   |
   v
_build_messages() -> 计算受保护前缀的预算上限 -> 判断初始视图是否超限
   |
   +-- 始终先执行 trim()，得到协议安全的候选请求
   |
   +-- 未超限：候选请求 -> call_llm()
   |
   `-- 超限：compact()
          |
          +-- False：候选请求 -> call_llm()
          `-- True：重建 State + Summary + 最近完整轮次
                    -> trim() -> call_llm()
```

运行时，超预算的裁剪会输出 `[Context] token budget exceeded` 等日志；压缩成功会输出 `[Context] compacted N old rounds`。这说明请求已从完整历史切换为摘要加近期轮次。摘要失败时会输出失败提示，但主任务继续用裁剪后的候选请求，而不是中断。

## 实现拆解

`compact()` 用 `_summarized_rounds` 记录已经进入摘要的轮次数。它先计算旧轮次边界，再只把这次新增的旧轮次展开成摘要输入：

```python
eligible_end = len(rounds) - keep if keep else len(rounds)
start = min(self._summarized_rounds, eligible_end)
if eligible_end <= start:
    return False
old_rounds = rounds[start:eligible_end]
old_messages = [message for round_messages in old_rounds for message in round_messages]
```

这样，第一次压缩后的新增消息在未来离开近期窗口时才会被摘要。成功路径最后才提交状态：

```python
summary = self.summarizer(prompt)
if not isinstance(summary, str) or not summary.strip():
    return False
self._summary = summary.strip()
self.keep_rounds = keep
self._compacted = True
self._summarized_rounds = eligible_end
return True
```

由于赋值在有效摘要之后，失败路径不会留下“已经压缩”的半成品状态。这是摘要失败能够可靠回退到 v0.12 行为的关键。

## 运行与观察

配置本地 LLM 后，可在 Bash/zsh 中用一项足以产生多轮工具调用的真实任务启动。命令行首条任务处理后，程序仍会进入交互循环：

```bash
PYTHONPATH=src python -m mini_agent "检查登录流程并运行回归测试"
```

当任务累积的上下文超过预算、并且旧轮次多于默认保留的六轮时，终端会先显示裁剪日志，随后出现 `compacted` 日志。后续主 LLM 请求包含新的结构化状态和历史摘要，并保留最近完整轮次；这正是本课三层信息分工的可观察结果。

## v0.13.1 补丁：Context Observability

> 这是对 v0.13 的补丁增补，不是 v0.13 tag 原生已有的能力。补丁只为上下文压缩增加可观察性，不改变压缩策略、消息历史或 tool calling 协议。
>
> 补丁快照：`v0.13.1` · 相邻差异：`v0.13..v0.13.1`
>
> 代码快照：`v0.13.1` · 相邻差异：`v0.13..v0.13.1` · 命令环境：Bash/zsh

如果要切到补丁并查看它相对 v0.13 的最小差异：

```bash
git checkout v0.13.1
git diff --stat v0.13..v0.13.1
git diff v0.13..v0.13.1 -- src/mini_agent/context.py src/mini_agent/config.py tests/test_context.py
```

### 为什么仅有压缩结果仍不够观察

v0.13 的日志只能告诉你发生了裁剪或压缩，却不能回答“最终发送了多少 token”“预算被哪一类消息占用”“压缩是否真的成功”。长任务排查窗口不足、工具输出过大或摘要失败时，需要一份与实际发送视图对应的统计快照，以及结构化的生命周期事件。

### `ContextStats`：五个互斥输入分桶

v0.13.1 新增不可变的 `ContextStats`。`tokens` 是本次准备发送的完整消息视图估算值；`window` 是上下文窗口，`input_limit` 是扣除输出预留后的输入上限，`reserve` 是输出预留。输入 token 被分到五个互斥桶：

| 字段 | 含义 |
|---|---|
| `system` | 普通 system 消息 |
| `task` | 第一条 user 任务消息 |
| `state` | 以 `[Structured State]` 开头的结构化状态消息 |
| `history` | 其他 assistant/user 历史消息（不含 tool 结果） |
| `tool_result` | 所有 `role=tool` 结果 |

因此 `tokens == system + task + state + history + tool_result`。估算仍使用 `len(text) // 3`，只是诊断指标，不等同于服务端 tokenizer。

### 用 `stats_snapshot()` 读取最近一次视图

`prepare_messages()` 完成后，`ContextManager.stats_snapshot()` 返回最近一次发送视图的统计；在尚未准备请求前它返回 `None`。最小调用示例：

```python
context = ContextManager(state, history)
messages = context.prepare_messages()
stats = context.stats_snapshot()
if stats is not None:
    print(stats.tokens, stats.tool_result, stats.reserve)
```

快照来自裁剪或压缩后的最终副本，而不是未经处理的完整 `history`，所以它适合和即将发出的请求一起记录。

### trim/compact 事件与默认日志

上下文管理器会发出三类 `ContextEvent`：

- `prepared`：最终视图准备完成，`event.stats` 携带 `ContextStats`。
- `trimmed`：工具结果被截断，或完整旧轮次被移除；`details["action"]` 分别是 `truncate` 与 `remove_round`。
- `compacted`：旧轮次压缩成功，详情包含压缩轮次、摘要 token 数和近期轮次；摘要异常时带有 `failed=True`，表示回退到 trimming。

默认 `CONTEXT_OBSERVABILITY = True`，终端会显示类似下面的输出：

```text
[Context]
tokens: 1,234 / 128,000
system:              42
task:                18
state:               76
history:            510
tool_result:        588
reserve:         19,200
[Context Trim]
removed turn #1
tool_result: -240 tokens
[Context Compact]
compressed turns: 1-4
summary tokens: 96
recent turns: 5-10
```

实际数字取决于消息内容；这组日志用于定位预算变化，不是新的协议消息。

### 用 observer 接入结构化事件

需要写入指标或日志系统时，可以传入 `observer` 回调。回调接收 `ContextEvent`，不必解析终端文本：

```python
def record_context_event(event):
    payload = {
        "kind": event.kind,
        "tokens": event.stats.tokens if event.stats else None,
        "details": event.details,
    }
    metrics.write(payload)

context = ContextManager(
    state,
    history,
    observer=record_context_event,
)
```

传入 observer 后仍可用 `observability=False` 关闭默认终端输出；自定义 observer 仍会收到事件。若 observer 自身抛出异常，`ContextManager` 会吞掉该异常并继续准备请求，观察逻辑不会影响 agent。

### 补丁边界

- 可观测性只读取发送副本，不修改完整 `history`，也不改变摘要失败时回退到 trimming 的行为。
- `prepared`、`trimmed`、`compacted` 是进程内事件，不会追加到发给模型的 messages。
- 不新增工具调用，不改变每个 `tool_calls` 对应 `role=tool` 结果的回灌规则。
- 关闭日志只需在本地 `config_local.py` 设置 `CONTEXT_OBSERVABILITY = False`；该配置模板默认值仍为 `True`。

## 本版特性、下一课与代码索引

v0.13 在 v0.12 的预算和裁剪之上增加了可注入的摘要器、自动或主动压缩、结构化状态注入、近期轮次保留以及摘要失败回退，同时将最大迭代次数提高到 50。v0.13.1 作为同课补丁增补了 `ContextStats` 的 token 分桶、`stats_snapshot()`、`prepared`/`trimmed`/`compacted` 事件、默认终端日志和 `CONTEXT_OBSERVABILITY` 开关，并保证 observer 异常不会影响 agent。下一课会加入项目级指令，并讨论它们为何也需要作为受保护上下文保留。

- [`src/mini_agent/context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.13/src/mini_agent/context.py) - 压缩、状态渲染和发送视图重建
- [`src/mini_agent/agent.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.13/src/mini_agent/agent.py) - 无工具、无终端输出的摘要请求
- [`src/mini_agent/config.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.13/src/mini_agent/config.py) - 50 轮默认上限
- [`src/mini_agent/state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.13/src/mini_agent/state.py) - 结构化执行事实的来源
- [`src/mini_agent/context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.13.1/src/mini_agent/context.py) - `ContextStats`、`ContextEvent`、observer 与可观测生命周期
- [`src/mini_agent/config.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.13.1/src/mini_agent/config.py) - `CONTEXT_OBSERVABILITY` 默认开关
- [`tests/test_context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.13.1/tests/test_context.py) - 分桶、事件和 observer 隔离测试
