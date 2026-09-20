# 第 34 课：让 Agent 请一个只读助手查资料

上一课：[崩溃后的调用交接](33-crash-recovery.md) · [教程总览](README.md) · 下一课：[共享父子运行循环](35-shared-agent-runtime.md)

> 代码快照：`v0.34` · 相邻差异：`v0.33..v0.34` · 命令环境：Bash/zsh

本课对应阶段十“受控子代理委派”。代码链接固定到 `v0.34`；阅读和运行本课都不需要创建 Git tag。

## 本课目标

这里的 Agent 是一个能向模型发问、调用工具并完成任务的程序。到上一课为止，所有调查都由同一个 Agent 完成：它既要读文件、找定义，又要修改和验证。调查越多，主任务的上下文越拥挤。

本课让主 Agent 临时请一个 Subagent（子代理）做一件小而明确的只读调查。子代理有自己的消息历史和状态，只能查看工作区或计算；它把结构化报告交回主 Agent。读完后，你应能解释：

- 为什么子代理是“独立的只读助手”，不是拥有主任务权限的第二个主 Agent；
- `delegate_task` 怎样描述目标、范围和允许的工具；
- 为什么父子各有自己的 Context、State 和权限；
- 为什么子代理的报告只能作为调查材料，不能直接算作主任务的验证证据。

## 上一版的问题

v0.33 已经能在崩溃后安全交接不确定的工具调用，但主 Agent 仍要亲自完成所有只读调查。这样会带来两个问题：调查内容占用主 Context，而且读文件、改文件、跑验证这些不同性质的工作容易混在一起。

本版只把“收集材料”分出去。主 Agent 仍是唯一能修改工作区、请求权限、维护计划、执行权威验证和决定任务完成的人；子代理没有这些能力。

## 前置条件与版本切换

需要基础 Python、JSON、函数调用和相对路径知识，并先阅读第 33 课。下面命令使用 Bash/zsh；第一条切换到上一版，第二条只查看差异，最后一条进入本课快照。

```bash
git checkout v0.33
git diff --stat v0.33..v0.34
git checkout v0.34
```

本课的离线回归使用 fake LLM，不需要真实 API key。真实模型配置仍只应放在本地未跟踪的 `config_local.py`。

## 新增与改动文件

先用 `git diff --stat v0.33..v0.34` 看范围，再关注下面这条学习主线：主侧提出委派，子侧在受限工具视图中运行，结果回到主侧。

| 文件 | 作用 |
|---|---|
| [`src/mini_agent/delegation.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.34/src/mini_agent/delegation.py) | 保存委派合同、范围检查、预算、结果校验和子代理运行入口。 |
| [`src/mini_agent/tools/delegation.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.34/src/mini_agent/tools/delegation.py) | 提供父侧的 `delegate_task` 工具。 |
| [`src/mini_agent/tools/base.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.34/src/mini_agent/tools/base.py) | 用显式能力标记筛出子代理可见的工具。 |
| [`src/mini_agent/runtime.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.34/src/mini_agent/runtime.py) | 为父、子运行时提供本版的公共调用入口。 |
| [`src/mini_agent/prompt.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.34/src/mini_agent/prompt.py) | 分别告诉父 Agent 和子代理各自的身份与边界。 |

## 版本变更定位

图例：`[旧]` v0.33 已有，`[+]` v0.34 新增，`[~]` v0.34 修改，`[C]` 主要消费者，`[B]` 本课边界。

上一版的调查只能沿主 Agent 的路径进行：

```text
[旧][C] 父 Agent
      -> read_file / grep / calculate
      -> 父 Context 和父 State
      -> 修改、验证、完成判定
      [B] 没有独立调查助手
```

本版在父 Agent 发出一个 `delegate_task` 后插入独立的子运行时：

```text
[C] 父 Agent
      -> [+] delegate_task（目标 + 工作区范围 + 工具白名单）
      -> [+] ScopeGate / PermissionGate 检查
      -> [+] SubagentRunner
           -> [+] 独立子 Context / 子 State
           -> [+] calculate / read_file / list_dir / grep
           -> [+] 结构化 JSON 报告
      -> [C] 父 Context 收到一个 role=tool 结果

[B] 子代理不能写文件、运行 shell、操作进程、修改父计划或再次委派
```

## 核心概念与数据结构

### 1. 委派合同：先说清楚“查什么”

如果只把一句“帮我看看项目”交给另一个模型，它可能读太多文件，也可能回答一个无法复查的问题。因此 `delegate_task` 不是一段任意提示词，而是一份小合同：`goal` 说明目标，`scope` 限定相对路径，`constraints` 说明限制，`expected_findings` 说明希望得到哪类发现，`requested_tools` 说明需要哪些只读工具。

运行时会另外生成 `delegation_id`、`subagent_id` 和合同 hash。模型不能伪造这些身份，也不能把 `depth` 提高到 1 以上。一个父回合只能有一个独占的 `delegate_task`；若同回合混入普通工具，整轮会被拒绝，不会启动子模型。

### 2. 工具范围是两道门

子代理的 `scope` 必须是工作区内的 1–8 个相对路径。绝对路径、`..`、`config_local.py`，以及 realpath 后逃出工作区的链接都会被拒绝。每次读文件、列目录或搜索时还会再次检查实际路径，防止首次检查后路径发生变化。

工具是否能给子代理使用，不由“看起来只读”推断，而由显式的 `delegation_capability` 标记决定。v0.34 只开放 `calculate`、`read_file`、`list_dir` 和 `grep`；写文件、shell、进程、计划、恢复和 `delegate_task` 都不在子代理的工具视图里。

### 3. 子代理有自己的上下文和报告

Context 是会送给模型的消息历史，State 是程序维护的任务事实账本。父子两者都拥有自己的副本，但父 Context、父 State、父权限和父 verification 不会复制给子代理。子代理只看到自己的身份规则、委派合同、被选中的父侧事实和自己的工具结果。

子代理最后必须返回固定形状的 JSON。下面的例子说明“结论”和“证据”如何关联；它不是要求读者手写，而是帮助理解返回值为何可检查：

```json
{
  "summary": "配置从 src/example.py 读取",
  "findings": [
    {"id": "f1", "claim": "...", "evidence_ids": ["e1"], "confidence": "observed", "caveat": null}
  ],
  "evidence": [
    {"id": "e1", "kind": "tool_observation", "tool": "read_file", "path": "src/example.py", "observation_hash": "<sha256>"}
  ],
  "limitations": []
}
```

运行时会检查证据 ID 是否唯一、引用是否存在、路径是否在 scope 内、hash 是否为 SHA-256，并确认 hash 来自本次成功的只读观察。第一次格式不合格时只给一次修正提示；再次不合格或在修正阶段又调用工具，就以 `failed/invalid_result` 收口。

### 4. 一次父调用只对应一个父结果

无论子代理内部调用了多少次只读工具，父 Context 只接收一个对应的 `role=tool` 结果。`role=tool` 是对话协议中的“工具结果消息”，它必须与父模型发出的工具调用一一配对。子代理内部消息留在自己的 Context 中，不能直接拼进父 history。

子代理超时、预算耗尽或报告失败，也会返回一个结构化失败结果，而不是让父协议缺少消息。这个结果能告诉父 Agent “调查没有成功”，但不能把它变成主任务的验证证据。

## 关键流程

正常路径是：

```text
父模型提出 delegate_task
  -> 检查阶段、权限、合同和 scope
  -> 创建一个独立的子 Runtime
  -> 子模型使用四个只读工具调查
  -> 校验 JSON 报告
  -> 父侧写入唯一的 role=tool 结果
  -> 父模型决定下一步
```

重要失败路径是：

```text
父回合混入普通工具，或同时出现两个 delegate_task
  -> delegation_batch_gate
  -> 所有调用得到有界拒绝结果
  -> 不启动子模型，也不修改父任务
```

## 运行与观察

在已经配置本地模型后，用 Bash/zsh 启动交互式 CLI：

```bash
PYTHONPATH=src python -m mini_agent
```

观察一次委派时，父侧应看到一个 `delegate_task` 调用和一个对应的 JSON 工具结果；子代理的 `read_file` 等内部消息不会出现在父对话中。若子模型请求 `write_file` 或 `run_shell`，应得到拒绝结果，实际 handler 不会执行。命令行首条任务处理后，CLI 仍会进入交互循环。

## 实现拆解

`DelegationManager` 冻结合同并创建 `SubagentRunner`；`FilteredToolRegistryView` 只暴露被允许的四个工具；`ScopeGate` 在每次文件操作前复核路径。v0.34 的单个子代理默认最多 8 轮、8 次 LLM 调用、24 次工具调用、约 32,000 个估算 token、12 KiB 结果和 120 秒墙钟时间。模型只能请求更小预算，token 使用保守估算并标记为 `estimated`。

父侧仍按既有工具边界记录一次 `delegate_task` 调用。子代理结果不会推进父 Plan、generation 或 `verification_evidence`；v0.34 也不保存完整子 Context、不恢复运行中的子代理、不做多子代理并行或父任务聚合预算。

## 为什么这样设计

本版选择一个同步、单层、只读子代理，是因为它能带来独立调查的好处，同时让父侧仍保持清楚的“一次调用、一个结果”协议。显式能力和独立权限比“工具没有副作用所以安全”更可靠，因为某些工具虽然不写文件，仍可能绑定父任务状态或进程资源。

代价是调查必须等待子代理返回，且父 Agent 需要复查报告。子代理只能提供线索，不能替父 Agent 证明测试通过或决定任务完成。生命周期、聚合预算、并行和持久化交付会改变资源与恢复边界，留到后续课程。

## 设计边界

- 同一父任务同时最多一个子代理，`depth=1`，同步等待。
- 子代理只能使用 `calculate`、`read_file`、`list_dir`、`grep`，不能写文件、运行 shell、操作进程或再次委派。
- 子结果是父侧的不可信调查材料，不进入父 `verification_evidence`。
- 子失败仍有结构化父工具结果；父 loop 不把子失败偷偷改写成成功。
- v0.34 不提供多 provider、并行调度、后台取消、父聚合预算或跨进程恢复。

## 本版特性、下一课与代码索引

本课完成了最小受控委派：父 Agent 可以把范围清楚的只读调查交给一个独立子代理，并收到可复查的结构化结果。下一课会把父、子两套控制循环收敛为同一个 `AgentRuntime.run()`，但仍保持它们各自的状态和权限边界。

核心源码：

- [`src/mini_agent/delegation.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.34/src/mini_agent/delegation.py)
- [`src/mini_agent/runtime.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.34/src/mini_agent/runtime.py)
- [`src/mini_agent/tools/base.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.34/src/mini_agent/tools/base.py)
- [`src/mini_agent/tools/delegation.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.34/src/mini_agent/tools/delegation.py)

完整设计见 [`docs/plans/subagent-delegation-plan.md`](../plans/subagent-delegation-plan.md)，运行约束见 [`docs/operation/manual.md`](../operation/manual.md)。
