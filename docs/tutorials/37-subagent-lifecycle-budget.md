# 第 37 课：给子代理加上生命周期和总预算

上一课：[让父子 Agent 使用不同的模型服务](36-multi-provider.md) · [教程总览](README.md) · 下一课：[有界并行子代理](38-parallel-delegation.md)

> 代码快照：`v0.37` · 相邻差异：`v0.36..v0.37` · 命令环境：Bash/zsh

> 运行要求：Python 3.10+。核心运行时仍只使用标准库；模型连接从本地 `config_local.py` 读取。

## 本课目标

第 36 课的子代理能返回结果，但父任务还不清楚它处在什么阶段，也不能把多次委派的资源消耗放进同一本账。本课给每次委派一个可追踪的生命周期，并让父任务在启动子代理前检查总预算。

读完后，你应能解释：

- 为什么“子代理失败了”和“父侧已经收到失败结果”是两件事；
- 为什么预算要先预留，结束后再按实际用量结算；
- 为什么取消是协作式的，不能假装立即杀死网络请求；
- 为什么子代理没收束时，`/new`、`/reset` 和退出不能直接清空旧任务。

## 上一版的问题

v0.36 的管理器用一个活动标记防止重复启动，却不能回答“是哪次委派在运行”“结果是否已经交给父 Agent”“取消是否已经完成”。如果多次委派都重新取得完整额度，父任务也可能超出总的模型调用、工具调用或 token 限制。

这还关系到 CLI 的任务边界：用户输入 `/new`、`/reset`、EOF 或 `exit` 时，子代理可能仍在同步请求中。程序必须先取消并等待；等待没有收束时，要保留旧任务并明确报告原因，不能把它写成干净退出。

## 前置条件与版本切换

需要第 36 课，并理解 `DelegatedTask`、`SubagentResult`、`AgentRuntime.run()` 和 model binding。命令使用 Bash/zsh：

```bash
git checkout v0.36
git diff --stat v0.36..v0.37
git diff v0.36..v0.37 -- src/mini_agent/config.py src/mini_agent/state.py src/mini_agent/delegation.py src/mini_agent/runtime.py src/mini_agent/agent.py src/mini_agent/session.py src/mini_agent/__main__.py
git checkout v0.37
```

本课的状态和取消回归不需要真实 API；若运行 CLI，仍需按第 36 课配置本地模型。

## 新增与改动文件

文件沿着“记账 → 运行 → 交付 → 任务边界清理”排列：

| 文件 | 作用 |
|---|---|
| [`src/mini_agent/state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.37/src/mini_agent/state.py) | 保存委派记录、预算账本、状态转换和安全点检查。 |
| [`src/mini_agent/delegation.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.37/src/mini_agent/delegation.py) | 管理当前任务、取消事件、预算预留和子代理边界。 |
| [`src/mini_agent/runtime.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.37/src/mini_agent/runtime.py) | 在父工具结果提交后推进委派记录。 |
| [`src/mini_agent/context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.37/src/mini_agent/context.py) | 向父模型展示有限的活动委派和剩余额度。 |
| [`src/mini_agent/session.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.37/src/mini_agent/session.py) | 拒绝保存活动或待提交的委派安全点。 |
| [`src/mini_agent/__main__.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.37/src/mini_agent/__main__.py) | 让 `/new`、`/reset`、EOF、`exit` 和异常退出先清理子代理。 |

## 版本变更定位

图例：`[旧]` v0.36 已有，`[+]` v0.37 新增，`[~]` v0.37 修改，`[C]` 主要消费者，`[B]` 本课边界。

v0.36 主要记录“工具调用返回了什么”：

```text
[C] 父 Agent
      -> [旧] delegate_task
      -> [旧] DelegationManager(_active: bool)
      -> [旧] SubagentRunner
      -> [C] 父 role=tool 结果
      [B] 没有完整的生命周期和父任务总账本
```

v0.37 把运行过程写成父侧可审计的记录：

```text
[C] 父 Agent
      -> [+] State.reserve_delegation()
      -> [+] DelegationRecord: created -> running
      -> [~] 子 Runner 检查取消和单次预算
      -> [+] result_ready + 实际 usage 结算
      -> [~] 父 Context 写入 role=tool
      -> [+] DelegationRecord: committed
      -> [C] 下一次父 LLM

[B] v0.37 仍只有一个同步子代理；v0.38 才并行
```

这里的“交付”表示父 State、父消息和必要的 durable 边界已经共同记录，不只是子模型输出了一段文字。

## 核心概念与数据结构

### 1. 执行结果和交付状态是两条轴

`outcome` 回答“子代理做得怎样”，例如 `completed`、`failed`、`timed_out`、`cancelled` 或 `budget_exhausted`。`delivery_status` 回答“父侧是否已经把这次调用收口”。两者不能混为一谈：子代理即使超时，也必须给父模型一个失败结果。

生命周期可以这样读：

```text
created -> running -> result_ready -> committed
                         |
                         +-- outcome = completed | failed | timed_out
                                      | cancelled | budget_exhausted
```

`result_ready` 表示结果已经通过校验、可以交给父侧；`committed` 表示对应的父 `role=tool` 和 State 事实也已经提交。只有到 `committed`，父 Runtime 才能继续请求下一轮模型。`DelegationRecord` 保存 ID、合同 hash、状态、结果 ID/hash、usage 和简短原因，不保存子代理完整 history。

### 2. 预算要先预留，再结算

父任务的聚合预算包括最大子代理数、并发数、LLM 调用数、工具调用数和 token 数。v0.37 并发数固定为 1，但仍先在 State 锁内检查剩余额度，并把本次允许的单次上限放入 reserved 账本：

```python
MAX_SUBAGENTS = 1
MAX_CONCURRENCY = 1
MAX_TOTAL_LLM_CALLS = 8
MAX_TOTAL_TOOL_CALLS = 24
MAX_TOTAL_TOKENS = 32_000
```

预算不足时，子 LLM 请求不会发生。子代理结束后释放未用预留，按实际 usage 结算；服务商没给 usage 时使用保守估算并标记 `estimated`。若真实 usage 超过预留，账本记录真实超额，后续委派看到更少的剩余额度，不能借重新生成合同来重置预算。

### 3. 取消是协作式的

Manager 保存一个属于当前父任务的 `threading.Event`。CLI 调用 `cancel(task_id, reason)` 时只是设置事件，不强行关闭正在进行的 HTTP/socket。子 Runtime 会在请求模型前、收到响应后、工具回合准入前和结果边界后检查它。

因此，快速返回的请求可以很快变成 `cancelled`；阻塞中的网络请求可能暂时没有收束。CLI 会有界等待，若仍未结束，就保留旧任务并报告 delegation ID 和原因。取消不是成功证明，也不能把未收束任务写成 clean safe point。

## 关键流程

正常路径：

```text
父 assistant: delegate_task
  -> 合同 / 阶段 / 权限检查
  -> State 锁内预留聚合预算，记录 created
  -> 标记 running，进入子 Runtime
  -> 子代理执行并检查取消/预算
  -> 生成并校验 SubagentResult
  -> 标记 result_ready，结算实际 usage
  -> 父 Context 追加恰好一个 role=tool
  -> 标记 committed
  -> 父 Runtime 才能请求下一轮 LLM
```

失败也要闭合协议：

```text
子模型异常 / timeout / cancel / budget_exhausted
  -> 生成唯一、有界的失败 SubagentResult
  -> result_ready(outcome=失败类)
  -> 父 role=tool 仍然提交
  -> committed
```

如果任务边界命令在子调用中到达，CLI 先取消并有界等待；等待失败时跳过 clean 保存和任务切换，旧 State 继续保留。

## 运行与观察

在已配置模型后，用 Bash/zsh 启动 CLI：

```bash
PYTHONPATH=src python -m mini_agent
```

父状态摘要应显示剩余额度、活动委派和近期已提交委派，但不显示子代理完整 history。正常结束时父模型收到 `outcome=completed` 的结构化结果；超时、取消或预算不足时，仍收到对应失败类 JSON。输入 `/new <任务>` 或 `/reset` 时，先观察取消和有界等待，再观察任务是否能安全切换；未收束时旧任务不应被清空。命令行首条任务处理后，CLI 仍会进入交互循环。

## 实现拆解

`AgentState.reserve_delegation()`、`delegation_result_ready()` 和 `commit_delegation()` 在同一把 State 锁内完成预算与状态转换。`DelegationManager` 保存当前父任务 ID、取消事件、预留记录和完成事件，不再只用布尔 `_active`。`SubagentRunner` 仍只负责组装独立的子 State、Context、工具视图和策略，然后进入第 35 课的唯一 `AgentRuntime.run()`。

父 Runtime 在追加 `role=tool` 后推进委派记录；父完成判定会检查是否仍有未提交委派。safe point 拒绝活动或 `result_ready` 委派，已提交记录的 usage 随 State 保存和恢复，不会恢复出新的预算。

## 为什么这样设计

把 `outcome` 和 `delivery_status` 分开，既能把“调查失败”告诉父模型，又能保证每个 assistant tool call 都有对应的 tool result。它还允许重复 commit 做幂等处理，不会产生第二条消息。

预留账本让预算拒绝发生在第一次子 LLM 请求之前；按实际用量结算又不会永久吞掉未使用的额度。代价是服务商的真实 usage 往往要到响应后才知道，已经发生的超额请求不能撤销，只能阻止后续请求。

协作式取消保留了标准库 HTTP 客户端的简单边界，避免强行关闭 socket 后无法判断 provider 请求到底发生了什么。代价是网络调用不会总能立即停下，所以任务边界必须有界等待并保留旧任务。

## 设计边界

- v0.37 只有一个同步、单层、只读子代理，`max_concurrency=1`。
- 子代理不能写文件、运行 shell、管理进程、修改父计划、改变 generation、提供权威验证或决定父任务完成。
- 取消只在同步检查点生效；不会强行杀死进行中的 HTTP/socket。
- safe point 拒绝活动或 `result_ready` 委派；已提交 usage 会随 State 保存，不会重置预算。
- 本版不自动重跑未收束的子代理，也不持久化跨进程可恢复的原始子结果；这留给第 39 课。
- 第 38 课才增加多个子代理并行和按模型顺序交付。

## 本版特性、下一课与代码索引

本课完成单子代理的生命周期、协作式取消、父任务聚合预算、usage 结算、状态摘要和安全点边界。下一课会让多个只读子代理同时运行，同时仍保证父 `role=tool` 结果按模型调用顺序提交。

核心源码：

- [`src/mini_agent/state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.37/src/mini_agent/state.py)
- [`src/mini_agent/delegation.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.37/src/mini_agent/delegation.py)
- [`src/mini_agent/runtime.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.37/src/mini_agent/runtime.py)
- [`src/mini_agent/context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.37/src/mini_agent/context.py)
- [`src/mini_agent/session.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.37/src/mini_agent/session.py)
