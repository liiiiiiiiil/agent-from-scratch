# 第 32 课：持久化工具执行边界（v0.32）

上一课：[从完整安全点恢复会话](31-safe-resume.md) · [教程总览](README.md) · 下一课：[v0.33 崩溃后的调用交接](33-crash-recovery.md)

> 代码快照：`v0.32` · 相邻差异：`v0.31..v0.32` · 命令环境：Bash/zsh

> 运行要求：Python 3.10+。本课的 `v0.32` tag 由仓库维护者在交付后手动创建；助手不创建、移动或推送 tag。

## 本课目标

上一课解决了“完整安全点可以在新进程中恢复”这件事，但完整安全点之间仍有一个危险窗口：模型已经让 Agent 调用了工具，进程却可能在工具结果写回 session 之前退出。磁盘上的旧安全点看不出 handler 是否已经进入，更无法证明下一次请求模型时 State、消息历史和工具结果属于同一个时刻。

本课要建立一个 durable tool boundary。这里的 durable 意思是“已经写入并校验过 session 文件”；边界记录工具回合处于 `pending` 还是 `committed`，并记录每个调用是否已经获得进入 handler 的准入。完成本课后，你应能解释为什么 handler 前要提交一次、为什么每个结果还要单独提交，以及为什么 v0.32 仍然拒绝恢复半轮。

## 前置条件

需要基础 Python、命令行和第 31 课的 session、State、Context 概念。先查看本课相对上一课的真实差异，再回到本课代码运行测试：

```bash
git diff --stat v0.31..v0.32
git diff v0.31..v0.32 -- src/mini_agent/session.py src/mini_agent/context.py src/mini_agent/state.py src/mini_agent/tools/base.py src/mini_agent/agent.py
git checkout v0.31
git checkout v0.32
PYTHONPATH=src python -m pytest -q tests/test_durable_tool_boundaries_v032.py
```

## 新增与改动文件

| 文件 | 变化 | 作用 |
|---|---|---|
| `session.py` | 增加 schema 3 与 `tool_boundary` | 在单个原子文件中校验 State、Context、调用顺序和提交状态 |
| `context.py` | 增加最后一轮结果前缀导出 | 允许 durable pending 边界保存受控半轮 |
| `state.py` | 增加边界专用 pending 导出 | 保留 attempt、预算和 generation 的权威引用 |
| `tools/base.py` | 拆出 `admit()` / `execute_admitted()` | 在 handler 前完成可持久化准入 |
| `agent.py` | 逐调用写入和整轮收口 | 按模型顺序回灌结果，complete 后才请求 LLM |
| `__main__.py`、`resume.py` | 接入 session 边界和 schema 3 恢复规则 | 区分存储失败，并拒绝半轮续跑 |
| `tests/test_durable_tool_boundaries_v032.py` | 增加受控故障注入 | 检查准入、结果、引用和脱敏边界 |

## 上一版的问题：结果可能已经发生，但没有落盘

一次工具回合至少有三个事实：模型发出了 assistant `tool_calls`，某个调用通过了权限和参数检查，handler 返回了结果并把 `role=tool` 回灌给模型。v0.31 只在三件事都完成后保存安全点。若 handler 先运行，保存随后失败，下一次读取旧文件会把“已经执行”和“完全没有执行”混在一起。

直观地看，旧流程只有一个收口：

```text
assistant tool_calls -> handler -> State + role=tool -> 完整安全点
                                      ↑ 中途退出时没有 durable 事实
```

v0.32 把中间事实也写入同一个 session 文件。session 仍是单个原子 JSON 文件，没有另建 journal；`session_generation` 继续作为递增提交序号。

## 核心概念

### 1. 回合开始先写 pending

工具回合开始时，agent loop 先把 assistant 消息和按模型顺序排列的调用写入 `tool_boundary`。每个调用获得一个稳定的 `invocation_id`，同时保留模型原始的 `tool_call_id`。此时 `handler_admitted` 是 `false`，还没有任何 handler 可以运行。

参数摘要经过脱敏，`content`、`command` 和 `input` 等长文本只保留类型和长度；`write_process.input` 仍然在 Context 中使用明确的占位符。原始参数不会因为写入边界而变成可重放凭据。

```json
{
  "schema_version": 3,
  "session_generation": 12,
  "save_kind": "tool_boundary",
  "tool_boundary": {
    "round_id": 4,
    "status": "pending",
    "calls": [{
      "invocation_id": "r-4-c-0",
      "tool_call_id": "call-7",
      "tool": "write_file",
      "effect_class": "possible",
      "permission": "not_checked",
      "handler_admitted": false,
      "attempt_id": null,
      "status": "pending"
    }]
  }
}
```

这个提交只回答“模型要求了什么、调用还没有完成到哪一步”，不回答 handler 有没有执行。它的价值是让保存失败或进程退出后，诊断可以知道最后一个完整边界在哪里。

### 2. 准入提交发生在 handler 前

工具执行器把前置检查和 handler 调用分成两个入口。前置阶段会检查工具是否存在、参数是否符合 schema、计划和修复阶段是否允许、进程归属是否正确、权限是否允许，并为需要的调用预留 attempt 和 generation。

只有这些检查全部通过，执行器才返回 `ToolAdmission`。agent loop 先把 `permission=allowed`、`handler_admitted=true`、`attempt_id` 和 generation 引用写入 session，然后才调用 `execute_admitted()`。准入提交失败时 handler 不会运行。

权限拒绝、参数错误、计划校验失败没有 handler 准入，但仍然会产生确定的工具结果。`commit_plan` 的拒绝继续使用 `plan_rejected`，不创建 FailureEvent、不推进 generation，也不会因为持久化而改变原有预算语义。

### 3. 一个结果对应一个原子提交

handler 返回后，主线程按模型顺序处理调用。它先让 State 记录 `ExecutionResult`，再追加对应的 `role=tool`，最后把 call 标记为 `committed` 并原子替换 session 文件。这个文件同时包含新的 State、Context 和边界记录，所以不会把“State 已记账、模型却看不到结果”当成成功提交。

串行副作用调用保持原顺序。`effect_class` 为 `none` 的调用可以并发产生结果，但所有调用先逐个完成准入提交；主线程随后等待下一个模型顺序位置，结果一旦可用便立即记账和回灌，后面的结果不能越过仍在等待的前项。这保留了并发的等待优势，也让 attempt、generation 和消息顺序可校验。`recover` 可能进入另一个工具或执行回滚，因此外层边界保守记为 `possible`；它不伪造外层 attempt，实际恢复 attempt 和 generation 仍由恢复运行时管理。

整轮所有调用都 committed 后，边界自身才变成 committed，agent loop 才能再次请求 LLM：

```text
[+] assistant + pending calls -> 原子提交
    ↓
[+] permission / 参数 / 预算检查
    ↓
[+] handler_admitted + attempt/generation -> 原子提交
    ↓
[+] handler 返回
    ↓
[+] State + role=tool + call committed -> 原子提交
    ↓
[+] 所有 call committed -> 整轮 committed -> 允许下一次 LLM
```

### 4. Context 和 State 的半轮导出有明确范围

普通安全点仍要求完整的 assistant tool call/result 配对，State 仍拒绝未结算 attempt。只有 `tool_boundary` 保存可以放宽这两个检查，而且范围很窄：Context 只允许最后一轮存在按模型顺序排列的结果前缀，State 只允许边界中被引用的 pending attempt。

Session 加载时会交叉检查边界、State 和 Context。它会拒绝缺少结果、重复 `invocation_id` 或 `tool_call_id`、倒退的序号、无效的 complete 标记、不存在的 attempt/generation 引用，以及 `committed` 边界中的 pending attempt。

### 5. 异步进程事实单独提交

后台进程可能在模型下一次请求之前自然退出。同步进程管理器后，State 会追加进程事件、必要的 failure 和新的 generation；这些事实使用当前边界单独保存，不额外制造一条 `role=tool`。这样模型消息协议仍然只反映模型实际收到的工具结果。

## 关键流程

下面的流程图只保留本课必须理解的四个保存点。箭头后的“允许下一次 LLM”是整轮提交的结果，不是单个 call 的结果。

```text
assistant tool_calls
  -> pending round commit
  -> preflight / permission / reservation
  -> handler_admitted commit
  -> handler
  -> State + role=tool + call committed commit
  -> all calls committed
  -> next LLM request
```

## 运行与观察

本课最适合先运行故障注入测试，因为它们把几个磁盘提交点变成可观察的结果：

```bash
PYTHONPATH=src python -m pytest -q tests/test_durable_tool_boundaries_v032.py
```

重点观察三件事：准入提交失败时 handler 的记录为空；成功回合的 `tool_boundary.status` 是 `committed`，每个 call 恰好有一个 `role=tool`；篡改 attempt 引用或删除结果后，`SessionStore.load()` 在校验阶段拒绝文件。再用真实 CLI 输入 `/save`，session 文件中会看到 `schema_version: 3` 和 `tool_boundary`，而不是额外的 journal 文件。

如果在一个工具回合的中间让进程退出，v0.32 只留下 pending 或部分 committed 的事实供诊断，`--resume` 会拒绝该会话，不会补结果、重试 handler 或调用 LLM。下一课才会把这些事实分类并交给用户处理。

## 实现拆解

[`session.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.32/src/mini_agent/session.py) 定义 schema 3、边界引用和顺序校验，并复用 v0.31 的锁、哈希和同目录原子替换。[`context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.32/src/mini_agent/context.py) 提供最后一轮结果前缀的受控导出；[`state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.32/src/mini_agent/state.py) 只在边界保存时放行 pending attempt，公开 `snapshot()` 和 Trace 不变。

[`tools/base.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.32/src/mini_agent/tools/base.py) 的 `admit()` 和 `execute_admitted()` 划开准入与 handler；[`agent.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.32/src/mini_agent/agent.py) 负责回合开始、逐调用结果提交和整轮收口；[`__main__.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.32/src/mini_agent/__main__.py) 在 `/save` 开启后传入当前 session 的边界能力，并把存储失败与普通工具失败分开报告。

## 为什么这样设计

pending 只说明某次边界提交成功了。即使它显示 `handler_admitted=true`，进程也可能在 handler 返回前退出；若 `effect_class=possible`，外部文件、命令或进程的效果更无法由 session 单独推断。v0.32 不根据命令文本、文件表面状态或缺失结果猜测副作用，也不自动重放调用。

因此本版的承诺是“精确记录中断窗口，并在不安全时拒绝续跑”。schema 2 的 clean 安全点仍能恢复，但恢复占用会升级为 schema 3；schema 3 的 clean、完整、committed 安全点也能恢复。崩溃后的调用分类、用户交接和任何有限重试属于 v0.33。

## 本版特性、下一课与代码索引

本版新增的是单文件 schema 3、handler 前准入提交、单 call 结果提交、整轮 complete 约束和异步进程事实提交。它没有新增 journal，也没有改变公开 State snapshot、Trace、PermissionGate 或 `run_shell` 的 effect 分类。

核心代码索引：[`session.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.32/src/mini_agent/session.py)、[`context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.32/src/mini_agent/context.py)、[`state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.32/src/mini_agent/state.py)、[`tools/base.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.32/src/mini_agent/tools/base.py)、[`agent.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.32/src/mini_agent/agent.py)。下一课 v0.33 将讨论如何读取 pending 边界并把不确定性交给用户。
