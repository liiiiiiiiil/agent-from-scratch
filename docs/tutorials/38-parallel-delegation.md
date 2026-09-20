# 第 38 课：让多个只读子代理有界并行

上一课：[给子代理加上生命周期和总预算](37-subagent-lifecycle-budget.md) · [教程总览](README.md) · 下一课：[持久委派交付](39-durable-delegation.md)

> 代码快照：`v0.38` · 相邻差异：`v0.37..v0.38` · 命令环境：Bash/zsh

> 运行要求：Python 3.10+。本课只并行调度只读子代理，不改变父 Agent 的修改、权限和验证边界。

## 本课目标

第 37 课可以记录预算和取消，但同一时间只能运行一个子代理。如果三个调查彼此独立，父任务就必须先等 A 完成，再等 B，再等 C。

本课允许父模型在一个 assistant 回合中提出多个 `delegate_task`。Scheduler（调度器）最多同时启动配置允许数量的子代理；它们可以乱序完成，但父模型最终仍按 A、B、C 的原始调用顺序收到工具结果。读完后，你应能解释：

- 为什么只有“纯委派回合”才允许走并行路径；
- 为什么预算额度和运行槽位是两种不同的限制；
- 为什么 B 先完成，也不能越过 A 先写入父 Context；
- 为什么并行 worker 不能直接修改父 State。

## 上一版的问题

v0.37 的 `max_concurrency` 固定为 1。它保护了生命周期和账本，却把互不依赖的调查强制排成一列，等待时间接近所有子任务耗时之和。

本版只改变“何时运行子代理”，不改变“谁拥有任务”。父 Agent 仍独占工作区修改、权限请求、主计划、generation、权威验证和完成判定；子代理仍是 depth=1 的只读实例。

## 前置条件与版本切换

需要第 37 课、基础 Python、Bash/zsh 和 Git 知识。先看相邻差异，再进入本课快照：

```bash
git checkout v0.37
git diff --stat v0.37..v0.38
git diff v0.37..v0.38 -- src/mini_agent/delegation.py src/mini_agent/runtime.py src/mini_agent/state.py src/mini_agent/agent.py
git checkout v0.38
```

## 新增与改动文件

本版沿着“批量准入 → 有界执行 → 按序交付”展开：

| 文件 | 作用 |
|---|---|
| [`src/mini_agent/delegation.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.38/src/mini_agent/delegation.py) | 定义 `DelegationScheduler`、批量合同冻结、槽位调度和取消。 |
| [`src/mini_agent/runtime.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.38/src/mini_agent/runtime.py) | 识别纯委派回合、收集结果并按 index 提交。 |
| [`src/mini_agent/state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.38/src/mini_agent/state.py) | 在锁内批量预留预算并结算实际 usage。 |
| [`src/mini_agent/agent.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.38/src/mini_agent/agent.py) | 允许多个委派同轮出现，拒绝与其他工具混用。 |
| [`src/mini_agent/tools/base.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.38/src/mini_agent/tools/base.py) | 让已准入的委派结果沿用统一工具结果边界。 |

## 版本变更定位

图例：`[旧]` v0.37 已有，`[+]` v0.38 新增，`[~]` v0.38 修改，`[C]` 主要消费者，`[B]` 本课边界。

v0.37 只有一个运行槽位：

```text
[旧][C] 父 assistant: delegate_task A
      -> [旧] 单任务 Manager
      -> [旧] SubagentRunner A
      -> [旧] 父 role=tool A
      -> [旧] 下一次父 LLM
      [B] max_concurrency=1
```

v0.38 为一个纯委派回合增加批量调度：

```text
[C] 父 assistant: delegate_task A, B, C
      -> [~] 逐项准入
      -> [~] State 锁内批量预留额度
      -> [+] Scheduler(max_concurrency=2)
           ├─ A worker 运行
           ├─ B worker 运行 -> 完成
           └─ 释放槽位 -> 启动 C worker
      -> [+] 结果缓冲：B、C 可以先完成
      -> [~] 父线程等待连续的 A，再按 A、B、C 提交
      -> [C] 下一次父 LLM

[B] 子代理仍只读、depth=1；第 39 课才保存跨进程可恢复的结果原文
```

例如 B 比 A 先完成，调度器可以马上启动 C，但父 history 仍必须保持 A、B、C。执行完成和父侧交付是两个时刻。

## 核心概念与数据结构

### 1. 只有纯委派回合才能并行

并行调度只适合多个互不依赖的只读调查。如果同一 assistant 回合同时出现 `delegate_task` 和写文件、shell、计划或其他普通工具，系统无法把它们安全地拆开执行。因此 `ParentRuntimePolicy.prepare_tool_round()` 只有在“这一轮全部是 `delegate_task`”时才进入并行路径；混合回合的所有调用都会得到 `delegation_batch_gate`，不会启动任何子模型。

这不是把委派工具变成特权入口。每个委派仍先经过 schema、阶段、权限和 durable admission；并行路径只决定调度方式。

### 2. 预算额度和运行槽位是两件事

预算回答“父任务总共允许消耗多少”，槽位回答“现在最多同时跑多少个”。例如：

```python
MAX_SUBAGENTS = 3
MAX_CONCURRENCY = 2
MAX_TOTAL_LLM_CALLS = 24
MAX_TOTAL_TOOL_CALLS = 72
MAX_TOTAL_TOKENS = 96_000
```

A、B 运行时，C 可以排队，但三个合同的预算应在启动前一起检查并预留。排队中的 C 不占运行槽位，却已经占用父任务的子代理数和预留额度。任务完成后释放未用预留，再按实际 usage 结算；如果 provider 报告的 token 超过预留，账本记录真实超额，后续任务看到更少的余额。

### 3. worker 执行，父线程提交

每个 worker 有独立的 SubagentRunner、State、Context、model binding、HTTP 连接、流缓冲和 usage 计数。worker 只返回 `SubagentResult`，不直接修改父 State 或父 Context。

调度器用按 index 编址的结果缓冲保存乱序完成结果。B 完成后可以释放槽位并启动 C，但只有当前面连续的 A 也有结果时，父线程才会将 A、B、C 推进到 `result_ready`、`role=tool` 和 `committed`。

### 4. durable boundary 仍按模型顺序收口

启用 `/save` 时，顺序仍是：

```text
start_round
  -> A/B/C 各自 admission 与 handler_admitted
  -> 批量预留并启动有限 worker
  -> 收集结果，等待连续前序 index
  -> 按 A、B、C 提交 State + role=tool + tool boundary
  -> complete_round
  -> 下一次父 LLM
```

任一持久化提交失败，都会阻止尚未安全收口的后续流程和下一次父 LLM。并行不允许产生“B 已写入、A 还没有”的半轮。

## 关键流程

正常路径可以压缩为：

```text
父模型提交 A、B、C
  -> 三个合同一起做权限、scope、预算检查
  -> 同时启动 A、B，C 等待槽位
  -> B 完成，启动 C
  -> A 完成
  -> 父侧按 A、B、C 交付三个唯一 tool result
  -> 整轮提交完成后请求父 LLM
```

失败路径中，某个子代理可以得到 `failed`、`timed_out`、`cancelled` 或 `budget_exhausted`；这个失败结果仍占它在父回合中的位置，其他已完成结果不会因此被抹掉。父任务取消时，调度器会请求全部活动 worker 停止，并保留没有及时收束的登记。

## 运行与观察

在已配置模型后，用 Bash/zsh 启动 CLI：

```bash
PYTHONPATH=src python -m mini_agent
```

当父模型一次提出多个独立的 `delegate_task` 时，观察活动数最多不超过 `max_concurrency`；较早完成的 B 不会提前出现在父 Context，父侧结果仍按模型原始顺序进入。若混入普通工具，则应看到批量拒绝，而不是部分启动。命令行首条任务处理后，CLI 仍会进入交互循环。

## 实现拆解

`reserve_delegation_batch()` 在一把 State 锁中完成批量预算检查，防止多个 worker 同时超发。`DelegationScheduler` 维护有限线程、待启动队列和按 index 的结果表；worker 只负责执行，父线程负责提交。父线程在每次连续结果交付时仍沿用第 37 课的生命周期和 durable 边界。

因此“并行”只发生在子代理内部执行阶段。父 Context、State、tool result 和 session 的可见顺序仍然由模型调用顺序决定。

## 为什么这样设计

直接把所有 `delegate_task` 丢进普通工具线程池，会混淆普通 handler 并发与子代理生命周期：子代理还需要独立 Runtime、冻结模型绑定、父聚合预算、取消和结果合同。专用 Scheduler 能把这些资源边界集中管理。

把预算和槽位分开，既能让 C 排队，又能在启动前知道 A、B、C 是否整体负担得起。代价是等待队列也会占父任务的预留额度。

父侧按 index 提交可能让已经完成的 B 等待 A，但这是保持模型消息顺序和 durable 原子收口所付出的成本。这样线程调度不会改变下一次模型看到的对话。

## 设计边界

- 只有纯委派回合进入并行路径；委派与普通工具混用时整轮拒绝。
- 子代理仍不能写文件、运行 shell、管理进程、再次委派或决定父任务完成。
- 每个 worker 使用独立 State、Context、连接、流缓冲和 usage；不共享可变运行数据。
- 结果可以乱序产生，但父 `role=tool`、State 和 durable boundary 必须按模型顺序提交。
- v0.38 不保存完整子 Context，也不恢复跨进程的内存结果；第 39 课才持久化 `result_ready` 原文。

## 本版特性、下一课与代码索引

本课完成有界并行、批量预算预留、独立 worker、部分失败交付、全体取消和父侧按序提交。下一课会解决一个新的恢复缺口：子代理结果已经产生，但父结果尚未交付时进程崩溃怎么办。

核心源码：

- [`src/mini_agent/delegation.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.38/src/mini_agent/delegation.py)
- [`src/mini_agent/runtime.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.38/src/mini_agent/runtime.py)
- [`src/mini_agent/state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.38/src/mini_agent/state.py)
- [`src/mini_agent/agent.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.38/src/mini_agent/agent.py)
- [`src/mini_agent/tools/base.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.38/src/mini_agent/tools/base.py)
