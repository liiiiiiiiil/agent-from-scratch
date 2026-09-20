# 第 35 课：共享父子运行循环

上一课：[最小受控子代理委派](34-minimal-delegation.md) · [教程总览](README.md) · 下一课：[v0.36 多 provider 与统一协议适配](36-multi-provider.md)

> 代码快照：`v0.35` · 相邻差异：`v0.34..v0.35` · 命令环境：Bash/zsh

> `v0.35` tag 和本课使用的固定源码链接由仓库维护者手动创建；本课不执行任何 tag 操作。

## 本课目标

上一课已经能同步启动一个隔离的只读子代理，但父 Agent、通用 Runtime 和 SubagentRunner 仍各自维护一套循环。本课把它们收敛到唯一的 `AgentRuntime.run()`：父子都经过相同的“请求模型、处理工具调用、回灌观察、判断是否继续”协议骨架，差异只由策略和实例依赖表达。

读完本课后，你应能解释三件事：为什么重复循环会造成协议漂移；Runtime 与 policy 分别负责什么；为什么共享循环不会让子代理获得父 Agent 的计划、权限或修改能力。

## 上一版的问题

v0.34 的父 Agent 还在自己的 legacy loop 中处理计划、进程、持久化和完成提醒。Runtime 壳有一个简化循环，子代理又在 `SubagentRunner.run()` 中单独维护预算、LLM 请求、工具执行和格式修正。三套代码看起来都在做 tool call，但边界并不天然一致。

例如，某一套循环可能在坏调用上直接抛异常，另一套循环却生成 `local-error-N`；如果一套循环先写 State 再追加 `role=tool`，另一套循环改变顺序，下一次模型请求就会看到不同的协议。对于普通工具这是行为差异，对于 durable session 则可能变成无法恢复的半轮。

## 前置条件

前置条件是第 34 课、基础 Python 和 JSON。先在 Bash/zsh 中查看相邻差异：

```bash
git checkout v0.34
git diff --stat v0.34..v0.35
git diff v0.34..v0.35 -- src/mini_agent/runtime.py src/mini_agent/agent.py src/mini_agent/delegation.py
git checkout v0.35
```

第一条切换命令用于对照上一版，最后一条才进入本课快照；`git checkout` 不会创建或移动 tag。

## 新增与改动文件

下面的文件表只列出理解共享循环所需的入口。完整变化应以 `git diff --stat v0.34..v0.35` 为准。

| 文件 | 变化 | 作用 |
|---|---|---|
| [`src/mini_agent/runtime.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.35/src/mini_agent/runtime.py) | 重构 | 定义唯一 Runtime、策略协议、结果类型、tool-call 规范化和统一提交流程。 |
| [`src/mini_agent/agent.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.35/src/mini_agent/agent.py) | 重构 | 保留 HTTP/LLM 入口，新增父策略，`agent_loop()` 只负责组装 Runtime。 |
| [`src/mini_agent/delegation.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.35/src/mini_agent/delegation.py) | 重构 | 保留 v0.34 合同与校验，新增子策略，Runner 只组装隔离实例并转换结果。 |
| [`src/mini_agent/context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.35/src/mini_agent/context.py) | 小改 | 提供 `append_assistant()` 和 `append_tool_result()` 两个协议消息入口。 |
| [`tests/test_shared_runtime_v035.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.35/tests/test_shared_runtime_v035.py) | 新增 | 固定唯一循环、消息顺序、坏调用闭合和子预算边界。 |

## 版本变更定位

图例：

```text
[旧] 上一版已有    [+] 本版新增    [~] 本版修改
[C] 主要消费者     [B] 本版边界或刻意不负责
```

v0.34 的真实调用链有三个收口：

```text
v0.34 基线

[C] agent_loop
      └─> [旧] _legacy_agent_loop
             ├─> 请求父 LLM
             ├─> 规范化并执行父工具
             └─> 父 State / Context / session boundary

[C] AgentRuntime
      └─> [旧] _run_common

[C] SubagentRunner.run
      └─> [旧] 自有 while 循环
             ├─> 子预算与格式修正
             └─> 请求子 LLM、执行只读工具、回灌结果
```

v0.35 保留这些消费者，但把控制权收进一个入口：

```text
v0.35 变更

[C] agent_loop ───────────────┐
                              ├─> [+] AgentRuntime.run()
[C] SubagentRunner.run ──────┘       ├─> prepare Context
                                     ├─> 请求注入的 LLM
                                     ├─> 规范化 tool_calls
                                     ├─> 追加 assistant 消息
                                     ├─> policy 准备执行/拒绝计划
                                     ├─> 准入、执行、按模型顺序提交 tool 结果
                                     ├─> policy 判断完成或继续
                                     └─> 下一轮或退出

[~] ParentRuntimePolicy ──> Plan / Repair / process / session / completion
[~] SubagentRuntimePolicy ─> budget / scope observation / Result Contract
[B] v0.35 ─────────────────> 不加入多 provider、生命周期取消、跨进程委派恢复
```

这里的箭头表示真实控制流，不表示父子共享 State。两个 Runtime 实例各自持有 Context、State、工具执行器和 policy；共享的是循环实现，而不是可变任务数据。

## 核心概念与数据结构

### 1. 共享循环和策略分工

要解决的问题是：怎样复用控制流程，同时保留父子不同的安全边界？直观地说，Runtime 像一条固定的传送带，负责把模型消息送到工具，再把每个工具结果按原顺序送回模型；policy 是放在传送带旁的检查员，只能决定“这一轮能否继续、某个调用应如何拒绝、结果应该附带什么提示”。检查员不能自己去请求模型，也不能替 Runtime 进入 handler。

最小接口在 [`runtime.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.35/src/mini_agent/runtime.py) 中用标准库 `typing.Protocol` 表达。三个返回类型分别表示一次 Runtime 结果、一次策略决定和一次工具回合计划：

```python
RuntimeDecision("continue", notice="请调用推进任务的工具")
RuntimeDecision("finish", content="完成", stop_reason="text")
ToolRoundPlan(serial=True, rejection_by_index={})
```

`RuntimeResult` 还记录 `rounds`、`llm_calls`、`tool_calls` 和 `estimated_tokens`。这些是一次运行的统计，不是新的 State 或 session schema。Runtime 的构造边界是：

```python
AgentRuntime(
    llm_client=..., context=..., executor=..., policy=...,
    max_rounds=..., output=..., session_boundary=...
).run()
```

父侧提供 `output` 和 `session_boundary`；子侧把输出设为静默且不提供父 session boundary。这样，父子都进入同一个 `run()`，但不会共享终端输出对象或持久化状态。

### 2. 相同的协议骨架

Runtime 对每个模型响应都做同样的事情。模型给出纯文本时，交给 policy 决定是否完成；模型给出工具调用时，先写规范化后的 assistant 消息，再为每个调用产生一个且仅一个 `role=tool` 结果。所有结果提交完毕后，才会准备下一次 LLM 请求。

```text
prepare_messages
  → LLM response
  → NormalizedToolRound
  → assistant(tool_calls)
  → tool 结果：call[0], call[1], ...
  → after_tool_round
  → 下一次 prepare_messages 或 finish
```

`ToolRoundPlan.serial` 只影响 handler 的执行方式。纯只读回合可以并行进入 handler；提交仍按模型顺序。需要串行的父回合按模型顺序执行和提交。配置了 durable boundary 时，Runtime 会先持久化 `handler_admitted`，再进入 handler；某个串行准入或提交失败时，不会继续进入后续调用。

### 3. 坏调用也必须闭合协议

tool call 是模型请求工具的结构化消息。`NormalizedToolRound` 把它变成统一的调用元组，并把错误挂在 `errors_by_call_id` 上。

缺少、空白或重复的 call ID 会得到不与模型原 ID 冲突的 `local-error-N`。非法 `type`、`function.name` 或 `function.arguments` 不会进入 handler；它们仍会写进规范化 assistant 消息，并得到对应的错误 tool result。父 Runtime 允许同轮其他合法调用继续执行；子策略沿用 v0.34 的收口方式，只要同轮存在畸形 call，就拒绝整轮调用并返回 `failed/invalid_tool_call`。

这条规则解决的是协议完整性，而不是替模型修复参数。工具层仍负责未知工具、权限和参数校验；Runtime 只保证模型下一轮能看到完整的 call/result 配对。

## 父子差异如何表达

父策略承接 v0.34 以前已经存在的状态机规则：初始 `awaiting_approval` 不请求 LLM；活动进程或 stdin 未收束时不能宣称完成；相同 `progress_marker` 的第二次无工具文本会把任务置为 `blocked`；Plan、Repair、verification、process 和 delegation 的同轮准入继续由父侧判断。父工具的 State 事实、tool 消息和 durable 边界由 Runtime 统一提交。

子策略不拥有这些父能力。它只检查墙钟、round、LLM、token 和 tool-call 预算，记录成功只读调用产生的 observation hash，解析最终 JSON Result Contract，并在第一次非法报告后设置一次格式修正提示。修正阶段若再次调用工具，Runtime 先为所有 call 补齐拒绝结果，再以 `failed/invalid_result` 结束。`SubagentRunner.run()` 只创建子 State、过滤 Registry、固定 PermissionGate、子 Context 和 policy，然后调用同一个 `AgentRuntime.run()`。

因此，父子差异可以画成一张依赖表：

| 维度 | 父 Runtime | 子 Runtime |
|---|---|---|
| Context | 父 history、Structured State、Runtime Notice | 独立 history、子提示词和合同 |
| 工具面 | 完整父 Registry 与动态权限 | 仅 `calculate/read_file/list_dir/grep` 的冻结 view |
| policy | Plan、Repair、进程、session、完成判定 | 预算、scope observation、结果合同 |
| 输出 | `TerminalOutput` 流式回调 | 静默 |
| 所有权 | 修改、权限、verification、完成判定 | 只读调查材料，不改变父 State |

## 关键流程

正常父回合和子回合的消息顺序相同：

```text
assistant(tool_calls=[A, B])
  ├─> 执行 B 可能先完成
  ├─> 提交 A 的 State / tool result
  ├─> 提交 B 的 State / tool result
  └─> 下一次 LLM 看到 assistant, tool(A), tool(B)
```

如果 A 的 durable result 提交失败，Runtime 不会发下一次 LLM 请求；在串行计划中也不会进入 B。并行只允许 handler 完成顺序与模型顺序不同，不能改变 Context 和 session 的提交顺序。

子代理的另一条失败路径是：

```text
子模型输出非法报告
  → 一次受保护格式修正 Notice
  → 再次输出合法 JSON：完成
      或再次非法 / 发起工具调用：闭合工具结果后 failed/invalid_result
```

运行当前命令行入口时，命令行首条任务处理后仍进入交互循环；终端模式会显示父侧工具进度，子侧调用保持静默。若使用 `--save`，应观察到父 session 的 durable tool boundary 仍按 `handler_admitted → 单 call 结果 → complete_round` 收口；这说明共享 Runtime 改变了控制入口，没有改变 v0.32/v0.33 的持久化协议。

## 实现拆解

### Runtime 的唯一控制点

完整实现见 [`AgentRuntime.run()`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.35/src/mini_agent/runtime.py)。它独占 LLM 调用计数、消息追加、规范化、工具回合、按序提交和轮次上限。`invoke_llm_once()` 仍通过签名检查适配 provider 参数；provider 自己抛出的 `TypeError` 不会触发隐式重试。

### 父入口只是组装器

[`agent_loop()`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.35/src/mini_agent/agent.py) 保留原签名和字符串返回值。它通过延迟 lambda 引用模块级 `call_llm`，所以现有测试对 `mini_agent.agent.call_llm` 的 patch 路径仍然有效；真正的父循环不再留在 `agent.py`。

### 子入口只转换结果

[`SubagentRunner.run()`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.35/src/mini_agent/delegation.py) 保留 `last_state` 和 `last_context` 诊断能力，但不再维护 `while` 循环，也不直接驱动 `runtime.invoke()`。子 LLM timeout 和其他异常在 Runner 边界转换成既有的 `timed_out` 或 `failed` 结果；父 Agent 不会因此继承子状态或证据。

## 为什么这样设计

把循环集中到一个地方，收益是协议不变量只需维护一次：每个 call 有一个 result，tool 回合完整后才进入下一次 LLM，坏调用也不会留下孤立 assistant 消息。父子策略仍能清晰看到自己的状态边界，不必把父计划和 session 规则塞进只读子代理。

代价是 Runtime 需要提供明确的策略钩子，父策略的完成提醒、process sync 和 repair gate 不能再偷偷依赖 legacy loop 的局部变量；并行 handler 的结果也必须额外保存并按模型顺序提交。这里选择小型 `Protocol`，而不是通用插件系统，是为了让 v0.35 的控制面保持可读、可审计。

本版刻意不加入多 provider、模型选择、生命周期与聚合预算、取消、多子代理并行、持久化 `DelegationRecord` 或跨进程委派恢复。它们会改变依赖和资源所有权；多 provider 留给下一课，其他能力仍留给 v0.37–v0.39。

## 设计边界

- 父子共享的是 `AgentRuntime.run()` 的控制流程，不是 State、Context、PermissionGate、ProcessManager、SessionStore 或 verification evidence。
- policy 可以返回拒绝结果、提示和完成决定，但不能调用 LLM、执行 handler、追加 `role=tool` 或另写循环。
- 子代理继续是单个、同步、depth=1，只读四工具；子结果是父 Agent 的不可信调查材料，不自动推进父计划或 verification。
- LLM 异常在 Runtime 不被吞掉；父顶层仍按原边界上抛，子 Runner 在自己的结果合同边界转换 timeout 和其他异常。
- session、State 和工具 schema 没有改变。`v0.35` 只收敛运行控制权，v0.34 委派合同保持兼容。

## 本版特性、下一课与代码索引

本版完成唯一的父子 Agent Loop：父 `agent_loop()` 和子 `SubagentRunner.run()` 都实际进入 `AgentRuntime.run()`；统一的 `NormalizedToolRound` 保证坏调用也有唯一 ID 和配对结果；父 Plan/Repair/process/session 语义、子固定预算和格式修正继续有效。

下一课是 [v0.36 多 provider 与统一协议适配](36-multi-provider.md)。当前课涉及的固定源码入口如下：

- [`src/mini_agent/runtime.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.35/src/mini_agent/runtime.py)
- [`src/mini_agent/agent.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.35/src/mini_agent/agent.py)
- [`src/mini_agent/delegation.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.35/src/mini_agent/delegation.py)
- [`src/mini_agent/context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.35/src/mini_agent/context.py)

设计意图和后续版本边界见 [`docs/plans/subagent-delegation-plan.md`](../plans/subagent-delegation-plan.md)；当前运行约束见 [`docs/operation/manual.md`](../operation/manual.md)。
