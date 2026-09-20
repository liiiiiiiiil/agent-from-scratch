# 第 37 课：子代理生命周期与聚合预算（v0.37）

上一课：[多 provider 与统一协议适配](36-multi-provider.md) · [教程总览](README.md) · 下一课：v0.38 有界并行（规划中）

> 代码快照：`v0.37` · 相邻差异：`v0.36..v0.37` · 命令环境：Bash/zsh

> 运行要求：Python 3.10+。本课继续使用标准库实现核心运行时；模型连接仍从本地 `config_local.py` 读取。

## 本课目标

上一课已经能同步启动一个只读子代理，但父任务只知道“工具调用返回了结果”。它不知道这个子代理正在创建、运行、等待交付还是已经进入父上下文，也没有办法把多次委派的模型调用和 token 统一限制在父任务的额度内。本课把这次调用变成一条有身份、有预算、有取消边界的生命周期。

读完本课后，读者应能解释三件事：为什么执行结果的 `outcome` 不能代替交付状态；为什么预算必须在子代理启动前预留、结束后按实际用量结算；以及为什么取消只能在同步边界生效。

## 上一版的问题

v0.36 的 `DelegationManager` 用一个布尔值表示是否有子代理运行。它能防止同一时刻重复启动，但不能回答“是哪一个父任务在运行”“结果是否已经进入父 State”“取消是否已经发出”。子代理超时或 LLM 报错虽然会返回结构化结果，父任务没有聚合账本，重复失败委派仍可能重新获得一套完整额度。

这会影响任务边界。用户输入 `/new`、`/reset` 或退出时，如果子代理还在同步调用中，CLI 必须先取消并等待；如果等待超时，旧任务不能被清空，也不能写成 `clean` safe point。v0.37 先解决单个同步子代理的生命周期，多个子代理并行留到 v0.38。

## 前置条件与版本切换

先阅读第 36 课，理解 `DelegatedTask`、`SubagentResult`、`AgentRuntime.run()` 和父子模型 binding。下面的命令用于查看本课相对上一课的真实范围。

```bash
git checkout v0.36
git diff --stat v0.36..v0.37
git diff v0.36..v0.37 -- src/mini_agent/config.py src/mini_agent/state.py src/mini_agent/delegation.py src/mini_agent/runtime.py src/mini_agent/agent.py src/mini_agent/context.py src/mini_agent/session.py src/mini_agent/__main__.py
git checkout v0.37
```

`config_local.py` 仍然只放在本地。若要运行 CLI，先按第 36 课配置模型；本课的核心状态和取消测试不需要访问真实服务。

## 新增与改动文件

下面的文件按教学主线列出。完整范围以 `git diff --stat v0.36..v0.37` 为准。

| 文件 | 变化 | 作用 |
|---|---|---|
| [`src/mini_agent/config.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.37/src/mini_agent/config.py) | 修改 | 定义父任务的子代理数量、并发、LLM、工具和 token 聚合上限。 |
| [`src/mini_agent/state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.37/src/mini_agent/state.py) | 修改 | 保存 `DelegationRecord`、预算账本、原子状态转换、快照和安全点校验。 |
| [`src/mini_agent/delegation.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.37/src/mini_agent/delegation.py) | 修改 | 用当前任务 ID、`Event` 和预留信息管理同步子代理，并在子边界检查取消和预算。 |
| [`src/mini_agent/runtime.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.37/src/mini_agent/runtime.py) | 修改 | 在父 `role=tool` 结果完成提交后推进委派记录。 |
| [`src/mini_agent/context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.37/src/mini_agent/context.py) | 修改 | 向父模型显示剩余额度、活动委派和近期提交摘要。 |
| [`src/mini_agent/session.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.37/src/mini_agent/session.py) | 修改 | safe point 拒绝活动或待提交委派。 |
| [`src/mini_agent/__main__.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.37/src/mini_agent/__main__.py) | 修改 | `/new`、`/reset`、EOF、`exit` 和异常退出先取消并有界等待。 |
| [`tests/test_subagent_lifecycle_v037.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.37/tests/test_subagent_lifecycle_v037.py) | 新增 | 覆盖预留结算、取消、预算拒绝、异常、停滞和 State 恢复。 |

## 版本变更定位

图中的“交付”指父 State、父上下文中的 `role=tool` 消息和必要的 durable boundary 已经共同记录；它不是子代理自己输出文本的时刻。

```text
[旧] 上一版已有    [+] 本版新增    [~] 本版修改
[C] 主要消费者     [B] 本版边界/不负责

v0.36 基线：
[C] Parent Agent
      -> [~] delegate_task
      -> [~] DelegationManager(_active: bool)
      -> [旧] SubagentRunner
      -> [旧] SubagentResult
      -> [C] 父 role=tool 结果
```

v0.37：

```text
[C] Parent Agent
      -> [~] delegate_task
      -> [+] AgentState.reserve_delegation()
      -> [+] DelegationRecord: created -> running
      -> [~] DelegationManager(Event, reservation)
      -> [~] SubagentRunner / SubagentRuntimePolicy
           | 取消或预算边界 -> [+] cancelled / timed_out / budget_exhausted 结果
           v
      [+] result_ready + 实际 usage 结算
      -> [~] Runtime 追加 role=tool
      -> [+] DelegationRecord: committed
      -> [C] 下一次父 LLM 请求

      [B] v0.37：同步单子代理、max_concurrency=1
      [B] v0.38：多个子代理并行
      [B] v0.39：跨进程结果持久交付
```

## 核心概念与数据结构

### 1. 交付状态和执行结果是两条轴

子代理可能已经失败，但父 Agent 仍必须收到一个结果。比如 LLM 超时的 `outcome` 是 `timed_out`，它的交付状态仍要从 `running` 进入 `result_ready`，再随父工具结果进入 `committed`。如果把“失败”当作“没有结果”，父 history 就会缺少与 assistant tool call 对应的 `role=tool` 消息，下一次请求会违反协议。

`DelegationRecord` 保存父侧有界事实：父子 ID、合同 hash、交付状态、独立 outcome、结果 ID/hash、用量、时间和取消或诊断原因。它只保存合同摘要和短结果摘要，不保存子代理完整 history。

```text
created -> running -> result_ready -> committed
                         |
                         +-- outcome = completed | failed | timed_out
                                      | cancelled | budget_exhausted
```

`result_ready` 由 Manager 在 `SubagentResult` 通过校验后写入。`runtime.py` 先追加对应的父 `role=tool`，在启用 durable boundary 时先完成该边界的结果保存，然后才调用 `commit_delegation`。所以 `committed` 是父侧可以继续请求模型的协议事实。

### 2. 预算先预留，再按实际用量结算

父任务的聚合预算包含 `max_subagents`、`max_concurrency`、`max_total_llm_calls`、`max_total_tool_calls` 和 `max_total_tokens`。v0.37 将 `max_concurrency` 固定为 1。每次创建委派时，State 锁内先检查剩余额度，并把子代理请求的单次上限放入 reserved 账本；预算不足时，子 LLM 请求不会发生。

子代理结束时，父账本释放未使用的预留额度，增加真实 usage。服务商没有 usage 时，Runtime 使用已有的保守 token 估算并标记 `estimated`。如果服务商返回的真实 usage 超过预留，账本保留真实超额，后续委派会因剩余额度不足被拒绝；本次响应不会被假设成“没有消耗”。

配置示例使用提交到仓库的占位值：

```python
MAX_SUBAGENTS = 1
MAX_CONCURRENCY = 1
MAX_TOTAL_LLM_CALLS = 8
MAX_TOTAL_TOOL_CALLS = 24
MAX_TOTAL_TOKENS = 32_000
```

模型在 `delegate_task.budget` 中只能请求更小的单次上限。父聚合预算由 Runtime 权威控制，不能因为重新生成合同或再次委派而重置。

### 3. 取消是协作式的

Manager 保存一个属于当前父任务的 `threading.Event`。CLI 的任务边界调用 `cancel(task_id, reason)` 只设置这个事件；它不会强行关闭正在进行的 HTTP/socket 调用。子 Runtime 在请求 LLM 前、收到响应后和工具回合准入前检查事件，工具结果边界之后还会再检查一次。

这解释了两个现象：如果 HTTP 请求很快返回，子代理会在 `after_llm` 变成 `cancelled`；如果连接本身没有及时返回，CLI 的有界等待可能先结束并报告“取消未收束”，旧任务会被保留。后者是安全交接事实，不能被打印成 clean。

用户中断发生在子调用中时，父 Runtime 会先给这次 `delegate_task` 提交对应的取消工具结果，再把中断交回 CLI；父模型不会继续下一次请求。子代理需要压缩历史时，摘要也是一次模型请求，所以必须在发送前检查剩余预算和取消状态。

## 关键流程

正常路径是一个父工具调用对应一个父工具结果：

```text
父 assistant: delegate_task
  -> schema / phase gate
  -> State 锁内预留聚合预算，记录 created
  -> Manager 标记 running，Runner 进入同一个 AgentRuntime.run()
  -> 子请求前检查取消/预算，响应后结算本轮 usage
  -> 生成并校验 SubagentResult
  -> State 标记 result_ready，释放预留并结算实际 usage
  -> 父 Context 追加恰好一个 role=tool
  -> State 标记 committed
  -> 父 Runtime 才能请求下一轮 LLM
```

失败路径仍然闭合协议：

```text
子 LLM 异常 / timeout / cancel / budget_exhausted
  -> 生成唯一、有界的 SubagentResult
  -> result_ready(outcome=失败类)
  -> 父 role=tool 仍然提交
  -> committed
```

如果父任务在子调用未收束时收到 `/new`、`/reset`、EOF、`exit` 或异常退出，CLI 先发取消并等待最多一个有界时间。等待失败时保留旧 State、委派 ID 和原因，跳过 clean 保存和任务切换。

## 为什么这样设计

把 `outcome` 和 `delivery_status` 分开，可以同时满足错误传播和消息协议。父模型必须知道“调查失败了”，而 Runtime 必须知道“这次父工具调用已经有结果可以回灌”。这也让结果提交可以做幂等保护：重复 commit 只返回同一个记录，不生成第二条 tool 消息。

预留账本解决了“模型同时请求多个工作”时的超发问题，虽然 v0.37 的并发槽只有一个。先预留再执行让预算拒绝发生在第一个子 LLM 请求之前；结束后按实际 usage 结算则不会因为保守预留而永久吞掉未使用额度。代价是服务商 usage 延迟到响应后才知道，真实超额只能停止后续请求，不能撤销已经发生的请求。

协作式取消保留了标准库 HTTP 客户端的简单边界。强行关闭 socket 可能留下不确定的 provider 请求状态，也无法保证子工具和父提交的顺序；在同步点停止更容易审计。代价是正在阻塞的网络调用无法被立即杀死，因此 CLI 必须有界等待并保留旧任务。

## 设计边界

- v0.37 仍只有一个同步、单层、只读子代理；`max_concurrency` 固定为 1，不启动并行调度器。
- 子代理不能写文件、运行 shell、管理进程、修改父 Plan、改变 generation、提供 authoritative verification 或决定父任务完成。
- 取消不会杀死进行中的 HTTP/socket 调用；取消请求只在下一同步边界生效，等待不收束时任务边界保持失败交接状态。
- safe point 拒绝活动或 `result_ready` 委派；已提交记录的聚合用量会随 State 保存和恢复，不会恢复出新的预算。
- schema 3 的 pending tool boundary 只保守保留可能的聚合预留并沿用 v0.33 crash recovery；本版不自动重跑子代理，也不承诺恢复一个未提交的原始子结果。
- findings/evidence 的规范化 hash 用于停滞判断。新 delegation ID、预算消耗或随机 summary 文本不会单独制造进展。
- v0.38 才处理多个子代理的并行完成和父模型顺序提交；v0.39 才处理跨进程委派结果交付。

## 运行与观察

使用 Bash/zsh 启动 CLI 后，可以在父任务仍处于活动状态时观察 Structured State 中的 `Delegation budget`、`Active delegations` 和 `Recent committed delegations`。这些行只显示剩余额度、委派 ID、目标和短摘要；完整子 history 不会进入父上下文。

```bash
PYTHONPATH=src python -m mini_agent
```

当子代理正常结束时，父模型下一轮看到的是一个结构化 `role=tool` JSON；当子代理超时、取消或预算耗尽时，仍会看到一个 `outcome` 为对应失败类的 JSON。输入 `/new <任务>` 或 `/reset` 时，CLI 会先执行委派取消和有界等待；如果等待没有收束，终端会显示 delegation ID 和原因，旧任务不会被清空。

## 实现拆解

`AgentState.reserve_delegation()`、`delegation_result_ready()` 和 `commit_delegation()` 在同一把 State 锁内完成预算和状态转换。`snapshot()` 与 session export 只输出无凭据的合同摘要、结果摘要/hash 和账本计数；`ContextManager._render_state()` 将这些有限视图注入父请求。

`DelegationManager` 不再使用布尔 `_active`。它保存当前父任务 ID、当前 delegation ID、取消 `Event`、预留记录和完成事件。`SubagentRunner` 仍然只组装子 State、Context、工具视图和策略，然后调用唯一的 `AgentRuntime.run()`；`SubagentRuntimePolicy` 把取消、墙钟时间、调用数、token 和工具回合边界转换为受控的 RuntimeDecision。

`AgentRuntime._commit_one()` 在追加 `role=tool` 后解析有限的委派结果引用并推进父记录。这样失败结果也经过同一条提交路径。`ParentRuntimePolicy` 和 CLI 的完成判断都检查未提交委派，避免父任务在结果交付前进入 `done`。

## 本版特性、下一课与代码索引

本课完成单子代理的生命周期、协作式取消、父任务聚合预算、实际 usage 结算、父上下文摘要、停滞 hash 和 safe-point 边界。下一课将把多个只读子代理并行运行，并保持父 `role=tool` 结果按模型调用顺序提交。

核心实现入口：

- [`src/mini_agent/state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.37/src/mini_agent/state.py)
- [`src/mini_agent/delegation.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.37/src/mini_agent/delegation.py)
- [`src/mini_agent/runtime.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.37/src/mini_agent/runtime.py)
- [`src/mini_agent/context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.37/src/mini_agent/context.py)
- [`src/mini_agent/session.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.37/src/mini_agent/session.py)
- [`src/mini_agent/__main__.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.37/src/mini_agent/__main__.py)

设计意图和后续版本边界见 [`docs/plans/subagent-delegation-plan.md`](../plans/subagent-delegation-plan.md)；当前运行约束见 [`docs/operation/manual.md`](../operation/manual.md)。
