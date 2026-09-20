# 第 38 课：有界并行子代理

代码快照：`v0.38` · 相邻差异：`v0.37..v0.38`

## 本课目标

v0.37 已经能让父 Agent 委派一个只读子代理，并为它记录预算和生命周期。它仍然一次只运行一个子代理。调查任务彼此独立时，这会让总等待时间被最慢的任务决定。

本课加入有界并行：父模型可以在一个 assistant 回合提交多个 `delegate_task`。调度器最多同时运行配置允许的数量，子代理完成顺序可以与模型调用顺序不同；父模型最终仍按原始 tool-call 顺序收到唯一的 `role=tool` 结果。

本课只改变调查任务的执行调度。子代理仍是单层、只读实例，父 Agent 仍独占工作区修改、权限、主计划、generation、权威验证和完成判定。

## 前置条件

读者需要基础 Python、Bash/zsh 和 Git 知识，并先阅读[第 37 课：子代理生命周期与聚合预算](37-subagent-lifecycle-budget.md)。示例命令都适用于 Bash/zsh。

在工作区中查看本课差异：

```bash
git checkout v0.38
git diff --stat v0.37..v0.38
```

上面的命令把源码切到本课快照，并显示相邻版本改动规模。若只想阅读当前分支的实现，可以直接查看本课列出的 tag 固定链接。

## 新增与改动文件

本课的入口和数据流分布在四个位置：

| 文件 | 作用 |
| --- | --- |
| `src/mini_agent/delegation.py` | `DelegationScheduler`、批量合同冻结、槽位调度和多任务取消。 |
| `src/mini_agent/runtime.py` | 纯委派回合的准入、批量执行和按 index 提交。 |
| `src/mini_agent/state.py` | 可并发预算校验、State 锁内批量预留和实际 usage 结算。 |
| `src/mini_agent/agent.py` | 允许纯委派回合包含多个调用，拒绝委派与其他工具混用。 |
| `src/mini_agent/tools/base.py` | 让已准入的冻结委派结果沿用 ToolExecutor 的结果边界。 |
| `tests/test_parallel_subagents_v038.py` | 用乱序完成和部分失败验证并发上限与父侧顺序。 |

## 关键流程

先看 v0.37 的限制。`delegate_task` 只能独占一个工具回合，Manager 只有一个活动任务，父线程要等这个子代理结束后才能收到结果：

```text
[旧 v0.37]
[C] 父 assistant: delegate_task A
      -> [~] ToolExecutor / DelegationManager 单任务入口
      -> [C] SubagentRunner A
      -> [C] A 完成
      -> [C] 父 role=tool A
      -> [C] 下一次父 LLM

[B] max_concurrency=1
```

v0.38 在同一回合加入专用调度器。`[~]` 表示旧节点被修改，`[+]` 表示本课新增，`[C]` 表示主要消费者，`[B]` 表示本课边界：

```text
[C] 父 assistant: delegate_task A, B, C
      -> [~] ParentRuntimePolicy.prepare_tool_round
      -> [~] ToolExecutor.admit（按 A, B, C）
      -> [~] State 锁内批量预留额度
      -> [+] DelegationScheduler(max_concurrency=2)
           ├─ [C] A worker ────────────────┐
           ├─ [C] B worker -> 完成          │
           └─ [+] B 释放槽位，启动 C worker  │
                                             v
      [+] 有界结果缓冲：B, C 可先完成，但等待 A
      -> [~] 父线程按 A, B, C 推进 result_ready
      -> [~] 追加 A, B, C 的 role=tool
      -> [~] 依次提交 committed
      -> [C] 下一次父 LLM

[B] 子代理仍 depth=1、只读；不持久化完整子 Context
[B] v0.39 才处理跨进程原始结果恢复
```

这个例子中，A 和 B 先占用两个运行槽位。B 先完成后，C 可以启动；即使完成顺序是 B、C、A，父 history 仍然是 A、B、C。执行完成和父结果交付是两个时刻：结果可以暂存在调度器中，但不能越过前序 call 写入父 Context 或 schema 3 boundary。

## 实现拆解

### 1. 纯委派回合与混合回合

`ParentRuntimePolicy.prepare_tool_round()` 现在识别纯委派回合。多个 `delegate_task` 可以进入专用路径；只要同一回合混入其他工具，整轮每个 index 都得到 `delegation_batch_gate`，因此不会启动子 LLM。计划、修复、验证和崩溃恢复阶段的原有准入规则继续优先。

`ToolRoundPlan.parallel_delegation` 只决定父 Runtime 的入口。普通工具仍使用普通执行路径；专用路径仍先调用 `ToolExecutor.admit()`，所以 schema、阶段、权限和 durable admission 仍由同一处负责。

### 2. 预留额度和运行槽位分开

`DelegationBudget` 允许 `1 <= max_concurrency <= max_subagents`。State 在一把锁中按模型顺序处理批量合同：

```python
budget = state.delegation_budget
outcomes = state.reserve_delegation_batch(tasks)
```

成功的合同增加 `created_subagents` 和 `reserved_*`，但不会因为排在等待队列中就消耗运行槽位。只有 Scheduler 已启动线程的任务才占用 `max_concurrency`。子代理结束后，父线程释放该任务未使用的预留，并按 `UsageRecord` 增加实际 usage。服务商报告的 token 若超过预留，State 照实增加已用量，后续合同会看到较少的剩余额度。

因此默认配置可以容纳三个当前单任务默认预算的调查：

```python
MAX_SUBAGENTS = 3
MAX_CONCURRENCY = 2
MAX_TOTAL_LLM_CALLS = 24
MAX_TOTAL_TOOL_CALLS = 72
MAX_TOTAL_TOKENS = 96_000
```

预算拒绝会生成有界 `SubagentResult(outcome="budget_exhausted")`，并在对应 index 交付；它不会请求子 LLM。重复合同也在这个边界前拒绝，不能靠重新生成 ID 重置额度。

### 3. Scheduler 只负责执行，父线程负责提交

`DelegationScheduler` 使用至多 `max_concurrency` 个标准库后台线程、待启动队列和按 index 编址的结果字典。worker 为每个合同新建 `SubagentRunner`，独立拥有 State、Context、模型绑定、连接、解析缓冲、usage 和取消事件。worker 只返回 `SubagentResult`，不直接写父 State 或父 Context。若父结果提交失败，调度器请求取消并立即返回错误；仍在运行的线程保留登记，供任务边界有界等待并报告。

调度器在结果完成时先放入结果缓冲。它可以立即补充运行队列，但只把当前连续的前序结果交给父线程。父线程再完成 `result_ready`、`role=tool` 和 `committed`。这个分工同时解释了两个可观察现象：活动数可以在 B 完成后下降并启动 C，而父 history 仍等待 A；某个任务失败不会抹掉其他任务的缓冲结果。

### 4. Durable boundary 仍按模型顺序收口

开启 `/save` 时，父回合的顺序是：

```text
start_round
  -> A/B/C 逐项 admission 与 handler_admitted 提交
  -> 批量预留
  -> 按 max_concurrency 启动 worker
  -> 收集结果，等待连续的前序 index
  -> 按 A, B, C 提交 State、role=tool 和 tool boundary
  -> complete_round
  -> 下一次父 LLM
```

任一持久化提交失败都会中止尚未启动的队列，并阻止下一次父 LLM。v0.38 不保存完整子 Context，也不从跨进程 session 恢复仍在缓冲中的原始结果；schema 3 pending boundary 继续交给已有 crash recovery 处理。

## 为什么这样设计

通用工具线程池解决的是“同一轮工具 handler 可以并行”这个局部问题。委派还需要冻结模型绑定、隔离子 Runtime、预留父聚合预算、处理取消和结算生命周期，因此 v0.38 使用独立 Scheduler，避免普通工具调用数直接变成子代理线程数。

把预算预留和运行槽位分开，可以让第三个任务在 A 仍运行时排队，同时保证三个合同的额度在启动前已经检查。代价是等待队列也会占用父任务的总子代理数和预留额度；释放发生在实际结果结算后，避免把额度重复分配给后来的合同。

父侧按 index 提交会增加等待前序任务的时间，但保留了 tool-call 协议的确定性。模型看到的消息顺序不依赖线程调度，durable boundary 也不会出现 B 已提交而 A 尚未提交的半轮形态。

## 本版特性、下一课与代码索引

本版完成多个只读子代理的有界并行、批量预算预留、独立 worker 隔离、部分失败交付、全体取消和父结果按序提交。子代理仍不能写工作区、执行 shell、再次委派或决定父任务完成。下一课处理跨进程的 `result_ready` 与原始结果持久交付和恢复。

核心实现入口：

- [`src/mini_agent/delegation.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.38/src/mini_agent/delegation.py)
- [`src/mini_agent/runtime.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.38/src/mini_agent/runtime.py)
- [`src/mini_agent/state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.38/src/mini_agent/state.py)
- [`src/mini_agent/agent.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.38/src/mini_agent/agent.py)
- [`tests/test_parallel_subagents_v038.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.38/tests/test_parallel_subagents_v038.py)

完整计划见 [`docs/plans/subagent-delegation-plan.md`](../plans/subagent-delegation-plan.md)，运行配置和任务边界见 [`docs/operation/manual.md`](../operation/manual.md)。
