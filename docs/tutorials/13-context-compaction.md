# 第 13 课：上下文压缩

> 版本 v0.13 | [上一课](12-token-budget-trimming.md) | [返回教程总览](README.md)

## 本课目标

v0.12 能把请求压回预算内，但做法是截断或删除旧历史。任务一长，删掉的内容可能正好是模型下一步需要的结论。v0.13 因此加入语义压缩：用一次不带工具的 LLM 请求整理旧轮次，得到 `Historical Summary`（历史摘要）；近期原文仍保留，真实执行状态则单独渲染为 `Structured State`（结构化状态）。

读完本课，你应该能够：

- 区分完整 history、Historical Summary 与 Structured State 的职责。
- 解释自动压缩从预算检查到失败降级的完整调用顺序。
- 看懂摘要请求为什么不提供 tools，也不输出到终端。
- 使用 mock summarizer 验证单次压缩、自动触发和多次压缩。

本课的核心原则是：**Summary 可以遗漏细节，State 必须准确。**

## 前置条件

- 已读第 11、12 课，理解 State 与 Context 分离、预算计算和轮次原子性。
- v0.13 完成后使用对应 tag 阅读；在 tag 创建前，可直接查看当前开发分支中的实现：

```bash
git checkout v0.13
git diff --stat v0.12..v0.13
git diff v0.12..v0.13 -- src/mini_agent/context.py src/mini_agent/agent.py tests/test_context.py
```

## 新增与改动文件

| 文件 | 改动 |
|---|---|
| `src/mini_agent/context.py` | 新增摘要器注入、Structured State 渲染、消息重建与 `compact()` |
| `src/mini_agent/agent.py` | 新增 `summarize_messages()`，复用 LLM 通道但关闭 tools 和终端流式输出 |
| `src/mini_agent/config.py` | `MAX_ITERATIONS` 从 10 提高到 50 |
| `src/mini_agent/config_example.py` | 同步迭代上限示例配置 |
| `tests/test_context.py` | 新增压缩、失败降级、自动触发和多次压缩测试 |
| `tests/test_loop.py` | 验证超过十轮的任务可以继续执行 |

## 为什么需要本版

v0.12 的 trimming（裁剪）解决了“请求太长”，却没有解决“旧信息仍然有价值”：

```text
旧 tool result 被截断  -> 细节消失
旧 round 被删除        -> 决策和结论一起消失
```

短任务通常不受影响。长任务中，模型可能忘记试过的方案、改过的文件或下一步计划。全部保留又会再次超限，所以需要把旧历史从原始消息改成更短的语义记录。

摘要不是事实数据库。模型可能漏掉内容，多次摘要还可能逐渐改写原意。因此 v0.11 建立的 `AgentState` 在这里作为事实锚：它记录真实工具结果，不从摘要中猜测。

## 关键流程

压缩成功后，主模型收到的消息会按以下顺序重新组成：

```text
Original System
+ Structured State        <- 每次 prepare 时从 AgentState.snapshot() 重新渲染
+ Historical Summary      <- summarizer 返回的有损语义摘要
+ Current Task            <- 原始首条 user task
+ Recent Messages         <- 最近 N 个完整 rounds，默认 6
```

`_build_messages()` 的代码如下：

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

这里要注意两点：

- 完整 `history` 没有被替换或删除。ContextManager 只在构建请求时选择 summary 和近期轮次。
- Recent Messages 仍通过 `_split_rounds()` 选取，所以 assistant tool calls 和相应 tool results 不会被拆散。

## 实现拆解

### Structured State：压缩后不失忆的事实锚

`_render_state()` 调用 `AgentState.snapshot()`，生成一条 system 消息：

```text
[Structured State]
Task: 修复登录失败
Current goal: 运行回归测试
Files changed: src/auth.py
Errors: run_shell: exit code 1
Status: running
Tools executed: 8
```

这些字段来自 Executor 回调记录的真实工具结果，不是从 summary 反推：

| 信息 | 来源 | 可靠性定位 |
|---|---|---|
| `Historical Summary` | LLM 对老消息的转述 | 允许有损，用于延续语义 |
| `Structured State` | `AgentState.snapshot()` | 执行事实锚，不能依赖摘要猜测 |
| Recent Messages | 完整 history 的近期轮次 | 原文保留，用于当前局部推理 |

State 不只在 `compact()` 成功时生成一次。只要 ContextManager 进入压缩模式，后续每次 `_build_messages()` 都会重新调用 `_render_state()`。因此压缩后新修改的文件和新出现的错误，仍会出现在下一次请求中。

### Historical Summary：一次独立的无工具请求

`ContextManager` 接受一个可注入的 summarizer（摘要器）：

```python
def __init__(
    self,
    state,
    history,
    summarizer: Callable[[list[Message]], str] | None = None,
    keep_rounds: int = 6,
): ...
```

生产环境默认延迟导入 `agent.summarize_messages()`。这样 `agent.py` 和 `context.py` 在模块加载时不会形成循环依赖：

```python
def summarizer(messages):
    from mini_agent.agent import summarize_messages
    return summarize_messages(messages)
```

`summarize_messages()` 仍复用已有的 `call_llm()` 和 `http.client` 通道，只调整两个参数：

```python
def summarize_messages(messages):
    return call_llm(
        messages,
        include_tools=False,
        stream_output=False,
    ).get("content", "") or ""
```

- `include_tools=False`：摘要器只整理文本，不应在摘要过程中执行文件或 shell 工具。
- `stream_output=False`：摘要是内部上下文处理，不应把中间产物打印成 Agent 的最终回复。

HTTP 请求仍使用流式协议，代码会完整收集内容；这里只是不逐块打印到终端。

### compact() 的执行过程

`compact(keep_rounds=None)` 返回 bool，表示这次是否成功生成新摘要：

```text
完整 history
    |
    +-- _split_rounds()
    |
    +-- rounds 数量 <= keep_rounds? -- 是 --> False
    |
    +-- 旧 rounds + State + 已有摘要组成摘要 prompt
    |
    +-- summarizer(prompt)
          | 异常、空字符串、非字符串 --> False
          | 有效字符串
          v
       保存 _summary
       更新 keep_rounds
       _compacted = True
       return True
```

摘要 prompt 要求按“任务、已完成、已执行工具、已修改文件、错误、结论、下一步”组织，并明确禁止虚构。它还提供两类校正信息：

- 当前 Structured State：帮助摘要器以真实执行记录为准。
- 已有摘要：多次压缩时保留前一次已经提炼出的语义。

`keep_rounds` 默认是 6。传入负数一定抛出 `ValueError`；传入 0 表示所有历史 rounds 都进入摘要，请求中不保留近期原文。

### 自动触发的真实顺序

每次主循环调用 `prepare_messages()` 时，顺序如下：

```text
1. _build_messages() 构造当前请求视图
2. 计算该视图是否超过预算，记为 over_budget
3. TrimPolicy.trim() 先生成可用的降级结果
4. 如果初始视图超限，调用 compact()
5. compact 成功：按 State + Summary + Recent Messages 重建，再 trim 一次
6. compact 未执行或失败：返回第 3 步的 trimming 结果
```

先 trim 不会损坏摘要来源。第 3 步只改发送副本，`compact()` 始终从完整 `self.history` 读取旧轮次。因为先得到了 trimming 结果，所以摘要不可用时，系统仍有一个协议合法且尽量符合预算的请求可交给主模型。

自动触发有两个条件：初始请求视图超过预算，并且完整 history 中的历史轮次多于 `keep_rounds`。也可以主动调用：

```python
did_compact = context.compact(keep_rounds=6)
```

### 失败降级为什么放在 ContextManager

摘要会增加一次网络调用，可能抛异常、返回空内容或返回非字符串。`compact()` 在上下文层处理这些失败，并返回 `False`：

```python
try:
    summary = self.summarizer(prompt)
    if not isinstance(summary, str) or not summary.strip():
        return False
except Exception:
    print("[Context] compaction failed; falling back to trimming")
    return False
```

这不违反“agent loop 不加 try/except 兜底”的约束。因为容错属于上下文策略，所以放在 ContextManager；loop 仍只调用 `prepare_messages()`，不需要理解摘要和降级细节。

失败时不会发生以下变化：

- 原始 history 不变。
- `_summary` 不会被空结果覆盖。
- ContextManager 不会因为失败进入新的压缩状态。

主请求会继续使用 v0.12 已生成的 trimming 结果。

### 多次压缩

长任务第一次压缩后仍会向完整 history 追加轮次。请求再次超限时，`compact()` 会重新读取 history 中最近 N 轮以外的旧轮次，并把已有摘要也放入新的摘要 prompt。

多次摘要可能遗漏或改写语义。因此不要求 summary 逐字稳定，验收应关注：

- Structured State 每次从最新 snapshot 重新渲染。
- 新发生的成功写入会出现在 `Files changed`。
- 新失败的工具调用会出现在 `Errors`。
- Recent Messages 仍然保持 tool calling 协议合法。

## 最小可运行示例

注入 mock summarizer（模拟摘要器）后，无需网络也能查看压缩后的消息结构：

```bash
PYTHONPATH=src python - <<'PY'
from mini_agent.context import ContextManager
from mini_agent.state import AgentState

def tool_round(number):
    call_id = f"call-{number}"
    return [
        {"role": "assistant", "content": None, "tool_calls": [{"id": call_id}]},
        {"role": "tool", "tool_call_id": call_id, "content": f"result {number}"},
    ]

history = [
    {"role": "system", "content": "system"},
    {"role": "user", "content": "修改 main.py 并验证"},
]
for number in range(8):
    history.extend(tool_round(number))

state = AgentState(task="修改 main.py 并验证")
state.record_tool("write_file", {"path": "main.py"}, True, "written")
context = ContextManager(
    state,
    history,
    summarizer=lambda prompt: "已修改 main.py，等待验证。",
    keep_rounds=2,
)

print("compacted:", context.compact())
for message in context.prepare_messages():
    print(message["role"], str(message.get("content"))[:80])
PY
```

输出应包含：

- 一条 `[Structured State]`，其中包含 `main.py`。
- 一条 `[Historical Summary]`。
- 原始 user task。
- 最近两个 assistant/tool 完整轮次。

## 自动触发示例

缩小预算窗口，就能验证 `prepare_messages()` 会自动调用 summarizer：

```python
from mini_agent.context import ContextBudget, ContextManager

calls = []
context = ContextManager(
    state,
    history,
    budget=ContextBudget(
        window=120,
        output_reserve_ratio=0,
        history_ratio=0.5,
    ),
    summarizer=lambda prompt: calls.append(prompt) or "automatic summary",
    keep_rounds=2,
)
prepared = context.prepare_messages()

assert calls
assert any(
    str(message.get("content", "")).startswith("[Historical Summary]")
    for message in prepared
)
```

## 测试与验收

v0.13 的压缩测试使用 mock summarizer，因此不依赖 API_KEY 或网络。开发环境中运行：

```bash
PYTHONPATH=src python -m pytest tests/test_context.py tests/test_loop.py -q
```

重点覆盖以下情况：

- 主动压缩后注入 Structured State 和 Historical Summary。
- 只保留最近 N 个完整轮次，无孤儿 tool result。
- summarizer 抛异常时返回 `False`，原 history 不变。
- 请求初始超预算时，`prepare_messages()` 自动触发压缩。
- 第二次压缩后 Structured State 包含第一次压缩后发生的新文件变更。
- `MAX_ITERATIONS = 50` 后，mock LLM 超过十轮仍能继续执行并正常结束。

直接执行 `tests/test_context.py` 的脚本入口，仍可回归 v0.12 基础测试；v0.13 新增用例需要通过 pytest 收集运行。

## 设计选择与边界

- **摘要复用同一个 LLM 通道**：不需要第二套客户端或配置。
- **摘要请求不带 tools**：内部整理一定不能产生副作用。
- **State 与 Summary 双层信息**：State 提供事实，Summary 补充较丰富的语义连续性。
- **近期轮次保留原文**：当前局部推理不必完全依赖有损摘要。
- **失败回退 trimming**：摘要只是增强能力，不能成为主循环的新单点故障。
- **迭代上限提高到 50**：上下文能收缩后，Agent 才可能安全执行更长任务；硬上限仍负责阻止无限循环。

### 本版边界

- 不会自动评分摘要质量，也不保证多次摘要逐字稳定。
- `_summary` 只存在当前 ContextManager 进程中，不会写入磁盘。
- 不提供向量检索、外部记忆或按需召回；这些留给后续阶段。
- `AgentState` 只保存现有字段，不是完整事件日志。
- protected prefix（原始 system、任务以及压缩后注入的 State/Summary）本身超过模型窗口时，compaction 也无法解决；系统只会保留并记录超限。

## 本版独有特性

- 老历史可压缩成 Historical Summary，近期轮次保留原文。
- AgentState 以 Structured State 形式进入压缩后的请求。
- 自动压缩、主动压缩和失败降级走同一个 ContextManager 边界。
- 支持多次压缩，事实锚随真实执行状态刷新。
- `MAX_ITERATIONS` 从 10 提高到 50，允许长任务跨压缩继续执行。

## Context Observability

只看到最终请求成功时，很难知道预算花在哪里、何时裁剪、哪些轮次被压缩。为了让这些过程可见，v0.13 同时提供 token 统计和上下文事件，供教学和调试使用。

每次主 LLM 请求准备完成后，默认会打印实际发送消息的 token 统计：

```text
[Context]
tokens: 82,341 / 128,000
system:       2,100
task:         1,200
state:        1,500
history:     65,000
tool_result: 12,541
reserve:     19,200
```

五个输入分桶不会重叠，合计一定等于 `tokens`。`reserve` 是输出预留，不算在输入总和中。发生裁剪或压缩时，还会打印 `[Context Trim]` 和 `[Context Compact]`，说明轮次、对象和节省的 token。

代码也提供结构化快照，调用方不必解析终端文本：

```python
from mini_agent.context import ContextManager

context = ContextManager(state, history)
context.prepare_messages()
stats = context.stats_snapshot()
print(stats.tokens, stats.tool_result)
```

需要关闭终端观测时，在 `config_local.py` 中设置：

```python
CONTEXT_OBSERVABILITY = False
```

关闭只影响默认日志，不影响预算、trimming、compaction 或 `stats_snapshot()`。也可以向 `ContextManager(..., observer=callback)` 传入回调接收 `ContextEvent`；回调即使抛异常，也不会影响 agent。

## 本版特性、下一课与代码索引

到这里，阶段四形成完整链路：v0.11 分离 State 和 Context，v0.12 建立预算和保证协议安全的 trimming，v0.13 用 Summary + State 减少裁剪带来的遗忘，再通过统计和事件展示过程。

下一课 v0.14 会加入 Project Instructions，让项目规则作为受保护上下文参与每次请求。后续的规划能力仍应复用 ContextManager 和 AgentState，不应把计划逻辑重新放回核心 loop。

## 本版完整代码

- `src/mini_agent/context.py` — Structured State、Historical Summary、compact 与自动触发
- `src/mini_agent/agent.py` — 无工具、无终端流式输出的 summarize_messages
- `src/mini_agent/config.py` / `config_example.py` — MAX_ITERATIONS = 50

- [context.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/context.py)
- [agent.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/agent.py)
- [config.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/config.py)
- [test_context.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/tests/test_context.py)
- [test_loop.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/tests/test_loop.py)
- `tests/test_context.py` — 压缩、降级、自动触发和多次压缩测试
- `tests/test_loop.py` — 长任务迭代上限回归
