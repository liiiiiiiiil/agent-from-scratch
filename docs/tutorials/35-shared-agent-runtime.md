# 第 35 课：父 Agent 和子代理共用一条运行循环

上一课：[让 Agent 请一个只读助手查资料](34-minimal-delegation.md) · [教程总览](README.md) · 下一课：[多 provider 与统一协议适配](36-multi-provider.md)

> 代码快照：`v0.35` · 相邻差异：`v0.34..v0.35` · 命令环境：Bash/zsh

`v0.35` tag 和本课的固定源码链接由仓库维护者创建；本课不执行任何 tag 操作。

## 本课目标

第 34 课已经有了父 Agent 和只读子代理，但它们各自维护一套“请求模型 → 执行工具 → 回传结果”的循环。两套循环短期能工作，长期容易在错误处理和消息顺序上分叉。

本课把控制流程收进唯一的 `AgentRuntime.run()`。Runtime 可以理解为驱动一次任务的总控制循环；policy（策略）是注入其中的规则集合，负责说明某个运行时允许什么、何时暂停或结束。读完后，你应能解释：

- 为什么重复循环会造成协议不一致；
- Runtime 负责哪些固定步骤，policy 负责哪些父子差异；
- 为什么“共用代码”不等于“共用 State、权限或历史”。

## 上一版的问题

v0.34 有三处相似但不相同的循环：父 Agent 的 legacy loop、通用 Runtime 壳，以及 `SubagentRunner` 自己的循环。它们都能处理 tool call（模型请求工具的结构化指令），但没有天然保证同一套规则。

例如，一套循环可能在坏调用上直接抛异常，另一套却返回 `local-error-N`；一套可能先写 State，再追加 `role=tool`，另一套顺序相反。`role=tool` 是对话历史中的工具结果消息，顺序一旦错了，下一次模型请求和恢复 session 都可能看到不完整的半轮。

## 前置条件与版本切换

需要第 34 课、基础 Python 和 JSON 知识。命令使用 Bash/zsh；切换到上一版查看差异，再切回本课快照：

```bash
git checkout v0.34
git diff --stat v0.34..v0.35
git diff v0.34..v0.35 -- src/mini_agent/runtime.py src/mini_agent/agent.py src/mini_agent/delegation.py
git checkout v0.35
```

## 新增与改动文件

本版的学习重点不是增加了多少文件，而是把两条循环合并成一个控制入口。

| 文件 | 作用 |
|---|---|
| [`src/mini_agent/runtime.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.35/src/mini_agent/runtime.py) | 定义唯一的 `AgentRuntime.run()`、消息顺序和工具回合提交流程。 |
| [`src/mini_agent/agent.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.35/src/mini_agent/agent.py) | 保留父侧入口，只负责组装父 Runtime。 |
| [`src/mini_agent/delegation.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.35/src/mini_agent/delegation.py) | 组装隔离的子 Runtime，并保留第 34 课的合同与结果校验。 |
| [`src/mini_agent/context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.35/src/mini_agent/context.py) | 提供追加 assistant 消息和 tool 结果的统一入口。 |

## 版本变更定位

图例：`[旧]` v0.34 已有，`[+]` v0.35 新增，`[~]` v0.35 修改，`[C]` 主要消费者，`[B]` 本课边界。

v0.34 的三个循环分别收口：

```text
[C] agent_loop
      -> [旧] 父 legacy loop
           -> 父 LLM / 父工具 / 父 State / session boundary

[C] AgentRuntime
      -> [旧] 简化的通用循环

[C] SubagentRunner.run()
      -> [旧] 子预算 / 子工具 / 报告修正循环
```

v0.35 保留父子两个 Runtime 实例，但让它们进入同一个控制流程：

```text
[C] agent_loop ───────────────┐
                              ├─> [+] AgentRuntime.run()
[C] SubagentRunner.run() ────┘       -> 准备消息、请求模型
                                      -> 规范化 tool_calls
                                      -> 追加 assistant 消息
                                      -> policy 检查并执行工具
                                      -> 按模型顺序追加 tool 结果
                                      -> policy 决定继续或结束

[~] ParentRuntimePolicy：计划、权限、进程、session、完成判定
[~] SubagentRuntimePolicy：预算、scope、报告合同
[B] 不共享 State、Context、PermissionGate 或 verification
```

## 核心概念与数据结构

### 1. Runtime 是固定传送带，policy 是规则检查员

Runtime 负责所有父子都必须遵守的步骤：准备消息、请求一次模型、规范化响应、执行工具、回传结果、判断是否进入下一轮。它不应该知道“这是 Anthropic 还是 OpenAI”，也不应该把父任务计划塞进子代理。

policy 负责运行时的差异：父策略知道计划、进程、持久化和完成提醒；子策略知道只读工具、scope、预算和最终报告格式。它可以返回“继续”“完成”或“拒绝这次调用”，但不能自己请求模型、进入 handler 或另写一套循环。

最小的策略结果可以直观理解为：

```python
RuntimeDecision("continue", notice="请调用推进任务的工具")
RuntimeDecision("finish", content="完成", stop_reason="text")
ToolRoundPlan(serial=True, rejection_by_index={})
```

这些对象描述控制决定，不是新的 State 或 session schema。

### 2. 每次工具回合都按同一顺序闭合

模型返回工具调用时，Runtime 先保存规范化的 assistant 消息，再为每个调用准备一个且仅一个 `role=tool` 结果。所有结果提交完毕后，才允许请求下一次模型：

```text
准备消息
  -> 模型响应
  -> 规范化 tool_calls
  -> assistant(tool_calls)
  -> tool result[0], tool result[1], ...
  -> 策略判断继续或结束
  -> 下一次模型 / 完成
```

调用 handler 的完成顺序可以和模型顺序不同，但提交消息和 State 的顺序不能变。启用 durable boundary 时，还要先提交 `handler_admitted`，再进入 handler；任何关键提交失败，Runtime 都停止后续调用和下一次模型请求。

### 3. 坏调用也必须有结果

`NormalizedToolRound` 把模型响应转换成统一调用元组。缺少、空白或重复的 call ID 会生成不会与模型原 ID 冲突的 `local-error-N`。非法的调用类型、函数名或参数不会进入 handler，但仍会保留在规范化 assistant 消息中，并得到对应错误的 `role=tool` 结果。

父策略可以让同轮其他合法调用继续；子策略沿用第 34 课的保守规则，只要这一轮出现畸形调用，就拒绝整轮。两者不同，但都由同一个 Runtime 保证“每个 call 最终都有 result”。

### 4. 共用循环，不共用任务数据

父、子分别拥有自己的 Context、State、工具执行器、PermissionGate 和 policy。父侧提供终端输出和 session boundary；子侧使用静默输出，不接入父 session boundary。共享的是不可变的控制代码，不是可变的任务事实。

因此子代理仍不能修改父计划、父 generation、父权限或父 verification。它的结果也只是父 Agent 的调查材料。

## 关键流程

父回合和子回合都遵守相同的消息骨架：

```text
assistant(tool_calls=[A, B])
  -> 执行 A、B（B 可能先结束）
  -> 按 A、B 提交 State 与 role=tool
  -> 下一次模型看到完整的 assistant + tool(A) + tool(B)
```

子代理输出非法报告时，子策略会走自己的边界：

```text
非法报告
  -> 一次受保护的格式修正提示
  -> 合法 JSON：完成
      或再次非法 / 又调用工具：补齐结果后 failed/invalid_result
```

## 运行与观察

在已经配置本地模型后，用 Bash/zsh 启动 CLI：

```bash
PYTHONPATH=src python -m mini_agent
```

父侧仍显示工具进度，子侧保持静默；一次父工具回合结束后，下一次模型请求只会在所有 `role=tool` 结果提交后发生。命令行首条任务处理后，CLI 仍会进入交互循环。若启用 `/save`，观察父 session 仍按 `handler_admitted → 单 call 结果 → complete_round` 收口，说明控制入口变了，但协议边界没有被绕过。

## 实现拆解

[`agent_loop()`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.35/src/mini_agent/agent.py) 和 [`SubagentRunner.run()`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.35/src/mini_agent/delegation.py) 现在主要负责组装依赖和转换结果，不再拥有各自的 `while` 循环。`AgentRuntime.run()` 独占模型调用计数、消息追加、工具回合、按序提交和轮次上限。

父策略继续承接第 34 课以前的计划、修复、进程、session 和完成提醒；子策略继续承接固定预算、scope 观察、hash 和 Result Contract。LLM 异常在父 Runtime 顶层不被吞掉；子 Runner 在自己的结果边界转换 timeout 和其他失败，父任务不会继承子状态。

## 为什么这样设计

把协议不变量集中到一个循环中，意味着“一个 call 对应一个 result”“整轮结果收齐后才能请求模型”“坏调用也要闭合”只需维护一份。策略仍能表达父子差异，而不必让只读子代理接触父计划和 session。

代价是 Runtime 必须提供清楚的策略钩子，父侧的完成提醒和 durable 提交不能再依赖 legacy loop 的局部变量；并行执行时还要额外保存完成结果并按模型顺序提交。本版选择小型 `Protocol`，保持控制面可读、可审计。

## 设计边界

- 父子共享 `AgentRuntime.run()`，不共享 State、Context、权限、进程管理器、session 或 verification evidence。
- policy 只能提出决定和拒绝计划，不能请求模型、执行 handler、追加 tool 结果或另写循环。
- 子代理仍是同步、单层、depth=1，只读四工具；子结果不会自动推进父计划或验证。
- v0.35 只收敛运行控制权，不加入多 provider、生命周期取消、聚合预算、多子代理并行或跨进程委派恢复。

## 本版特性、下一课与代码索引

本课完成唯一的父子运行循环：父入口和子入口都进入 `AgentRuntime.run()`，统一的 `NormalizedToolRound` 保证坏调用也能得到配对结果，同时保留父子各自的安全边界。

下一课是[多 provider 与统一协议适配](36-multi-provider.md)：它会把“使用哪个模型服务”和“如何翻译消息格式”放到 Runtime 外部的绑定与适配层。

核心源码：

- [`src/mini_agent/runtime.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.35/src/mini_agent/runtime.py)
- [`src/mini_agent/agent.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.35/src/mini_agent/agent.py)
- [`src/mini_agent/delegation.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.35/src/mini_agent/delegation.py)
- [`src/mini_agent/context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.35/src/mini_agent/context.py)
