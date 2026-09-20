# 第 39 课：持久委派交付

上一课：[第 38 课：有界并行子代理](38-parallel-delegation.md) · [教程总览](README.md) · 下一课：阶段十一（规划中）

> 代码快照：`v0.39` · 相邻差异：`v0.38..v0.39` · 命令环境：Bash/zsh

> 运行要求：Python 3.10+。本课的示例使用本地 session 和测试代码，不需要真实 API key。

## 本课目标

本课解决一个很具体的问题：子代理已经完成调查并产生了结果，但父 Agent 还没有把结果写入自己的上下文时，父进程发生了崩溃。读者完成本课后，应能解释以下事实：结果为什么先进入 `result_ready`，父调用为什么仍要按模型顺序交付，以及恢复为什么可以交付原结果却不能重跑子代理。

## 上一版的问题

v0.38 已经支持多个只读子代理并行。假设父模型提交 A、B、C 三个调用，B 可能先完成，C 也可能在 A 仍运行时完成。调度器会把这些结果留在当前进程的内存中，等待 A 后再按 A、B、C 写入父 Context 和 `role=tool` 消息。

这段等待会形成一个恢复缺口：B 的子 LLM 可能已经消耗额度，结果也已经通过合同校验，但 session 里只有“调用已经准入”或“子代理正在运行”的事实。恢复进程既不能声称已经收到 B，也不能安全地自动重跑 B。v0.39 保存可以重建父工具结果的原文，把“结果产生”和“结果交付”变成两个可审计时点。

## 前置条件与版本切换

读者需要基础 Python、Bash/zsh 和 Git 知识，并先阅读[第 38 课：有界并行子代理](38-parallel-delegation.md)。本课使用的代码快照是 `v0.39`；下面的命令只用于查看代码，不代表程序执行后会立即退出。

```bash
git checkout v0.38
git diff --stat v0.38..v0.39
git diff v0.38..v0.39 -- src/mini_agent/session.py src/mini_agent/state.py src/mini_agent/delegation.py src/mini_agent/runtime.py src/mini_agent/resume.py
git checkout v0.39
```

`v0.39` tag 由项目维护者创建。本课源码链接固定到这个 tag；如果本地还没有该 Git 对象，可以先阅读当前工作区的对应文件，或等待维护者完成 tag 操作。

## 新增与改动文件

本版的变化跨过了子代理调度、父 State、schema 3 boundary 和恢复入口。下面的文件表先说明每个位置解决哪一段问题，后面的流程再把它们连起来。

| 文件 | 变化 | 作用 |
| --- | --- | --- |
| `src/mini_agent/session.py` | 新增有界待交付结果区和原子交付入口 | 保存可重建的结果原文，校验调用身份、hash、State 状态和大小上限。 |
| `src/mini_agent/state.py` | 补充持久结果结算与中断审计 | 让预算、父 attempt 和 `DelegationRecord` 在恢复时沿用已有事实。 |
| `src/mini_agent/delegation.py` | 分开完成回调和父交付回调 | 子任务完成即可持久化，父结果仍按模型顺序交付。 |
| `src/mini_agent/runtime.py` | 接入启动、`result_ready` 和 `committed` 时点 | 启动 worker 前保存合同；交付时合并 State、Context 和 boundary 提交。 |
| `src/mini_agent/resume.py` | 消费持久结果并派生恢复 session | 按父调用顺序交付原文，不调用子 LLM；无结果调用生成 v0.33 issue。 |
| `src/mini_agent/context.py`、`src/mini_agent/trace.py` | 增加结果引用和父侧生命周期投影 | 让压缩后的上下文与只读 Trace 仍显示准确状态，但不加载子 history。 |

## 版本变更定位

图例：

```text
[旧] 上一版已有    [+] 本版新增    [~] 本版修改
[C] 主要消费者    [B] 本版边界/不负责
```

v0.38 的基线是：调度器只把完成结果留在当前进程，父线程按 index 依次交付。

```text
[旧][C] 父 Runtime
       -> [旧] DelegationScheduler worker
       -> [旧] 内存结果缓冲 B、C
       -> [旧] 等待 A
       -> [旧] State / role=tool / tool_boundary 按 A、B、C 提交
       -> [旧][C] 下一次父 LLM

[B] 进程在“内存结果缓冲”处退出时，旧 session 没有 B、C 的可重建原文
```

v0.39 在 worker 完成和父交付之间加入了耐久边界。`result_ready` 是“结果已校验并已保存”的状态；`committed` 是“父 State、父消息和工具边界都已提交”的状态。

```text
[C] 父 Runtime
       -> [~] 批量保存合同、预留额度和 running
       -> [~] DelegationScheduler worker
       -> [+] 完成回调：校验结果
       -> [+] State result_ready
       -> [+] schema 3 pending_delegation_results 保存原文、摘要、hash
       -> [~] 父交付回调按 A、B、C
       -> [+] attempt + State committed + role=tool + boundary 原子提交
       -> [C] 下一次父 LLM

[+] active pending boundary -> [C] resume 派生新 session
       -> [~] 按父调用顺序读取 result_ready 原文
       -> [~] 直接构造 role=tool
       -> [B] 不调用子 LLM、不重放旧 worker

[B] created/running 且没有持久原文 -> v0.33 issue，等待用户决策
```

## 核心概念与数据结构

### 1. `result_ready` 保存什么

`result_ready` 不是一段“任务完成”的文字标签，而是可以重新构造父工具结果的完整 `SubagentResult` JSON。session 的 `pending_delegation_results` 条目保存调用 ID、委派 ID、结果 ID、结果原文、短摘要和 SHA-256 hash。原文使用 canonical JSON，恢复时重新序列化后必须得到同一个字节序列和 hash。

边界只保存恢复所必需的父侧材料。子代理的完整 history、隐藏提示词和连接状态仍然不会进入父 session：

```json
{
  "invocation_id": "r-3-c-1",
  "delegation_id": "d-2",
  "result_id": "result-2",
  "result_hash": "<64 个十六进制字符>",
  "result_json": "{...规范化的 SubagentResult...}",
  "result_summary": "发现配置读取路径"
}
```

单条和总区都有大小上限；结果只能属于一个仍处于 `result_ready` 的 `delegate_task`。这些约束避免 session 变成无限增长的子代理日志，也让恢复能够明确知道原文对应哪个父调用。

### 2. 两个回调表示两个时刻

v0.38 的“完成”回调同时承担了两个责任。v0.39 把它拆开：完成回调只说明某个 worker 已经产生了经过合同校验的结果；父交付回调等前序 index 可交付时才追加父消息并提交 `committed`。

```python
def on_result_ready(index, result):
    state.delegation_result_ready(task.delegation_id, result)
    boundary.record_delegation_result_ready(invocation_id, result, state, context)

def on_result(index, result):
    # 只会按父模型的调用顺序进入这里
    commit_parent_tool_result(index, result)
```

这样 B 可以在 A 运行时安全地落盘，C 也可以继续占用已释放的并行槽位；但 B、C 不会提前出现在父 Context 中。模型看到的消息顺序仍是调用顺序。

### 3. 原子交付和恢复结算

父交付必须同时完成三类事实：父执行尝试已经记录，`DelegationRecord` 已从 `result_ready` 变成 `committed`，对应的 `role=tool` 和 boundary 已经落盘。保存失败后 Runtime 停止后续调度和父 LLM 请求，避免内存状态继续向前走。

恢复时，已有持久原文会先校验调用身份、结果 ID、hash、State 生命周期和父顺序，再创建恢复 generation 的父 attempt，并按原文追加 `role=tool`。这一步不会请求子模型。预算使用源 session 已记录的预留和实际 usage；派生新 session 只是恢复载体，不会重置账本。

## 为什么这样设计

只保存 `result_hash` 可以证明某个结果曾经存在，却不能重建父模型需要的 `role=tool` 内容。保存完整结果原文能让恢复保持逐字一致，代价是 session 需要受控地承载一段结构化 JSON，因此 v0.39 同时设置单条和总字节上限。

把 `result_ready` 和父交付分开，是并行调度与确定性协议之间的折中。结果可以乱序产生和落盘，父消息仍然按模型调用顺序提交。代价是 session 在整轮完成前会保留待交付原文；这段内容只存在于 active pending boundary，不能被当作 clean safe point。

恢复选择“交付已保存结果、把无结果调用交给用户”，是因为只读调查没有工作区副作用，但重新调用仍会产生模型成本和新的调查事实。没有持久原文时，系统无法证明旧子代理到底返回了什么，因此把委派记为 `interrupted`（调查中断的审计状态），由父工具得到“不确定”结果，不给委派伪造结果 ID 或 `committed` 状态。旧进程的实际用量未知，聚合预算按原预留上限保守占用，不自动重跑。

## 设计边界

- `/save` 仍是启用 session 持久化的唯一入口；不新增后台结果文件或公开工具参数。
- 子代理仍是 depth=1、只读实例，只能使用既有四个观察工具；v0.39 不扩大权限，也不恢复旧线程。
- 只有能通过原文、hash、调用身份、State 状态和顺序检查的结果可以直接恢复；篡改任何一项都会拒绝 session。
- 旧 v0.38 schema 3 boundary 没有待交付原文时继续可读，但按 v0.33 将已准入且无结果的调用分类为不确定或调查中断事实。
- 子代理的 findings 和 evidence 仍是父侧调查材料，不进入父 `verification_evidence`，不能代替父 Agent 的独立验证。
- Trace 只读取父 State 快照和结果引用；它不读取完整子 history，不调用 LLM，也不通过回放重建缺失事实。

## 关键流程

正常路径可以压缩为下面的顺序。注意 worker 启动发生在合同、预留和运行状态保存之后。

```text
父 assistant: delegate_task A, B, C
  -> admission + 批量预留
  -> 保存 created/running
  -> 启动有界 worker
  -> B 完成：保存 B result_ready
  -> C 完成：保存 C result_ready
  -> A 完成：保存 A result_ready
  -> 按 A、B、C 交付父 tool result
  -> 每次同时保存 attempt、committed、role=tool 和 boundary
  -> 整轮 committed 后请求父 LLM
```

如果进程在 C 的 `result_ready` 保存之后退出，恢复会读取 A、B、C 中已经保存的原文，并按 A、B、C 追加父结果。若 B 只有 `running` 事实而没有原文，恢复会为 B 生成明确 issue；它不会把 B 的结果猜成成功，也不会偷偷重跑 B。

读者可以用以下 Bash 命令运行本地回归，观察结果区和恢复测试都在当前代码中通过：

```bash
PYTHONPATH=src python -m pytest -q tests/test_durable_delegation_v039.py
```

命令完成后应看到 v0.39 测试通过。它验证的关键现象是：父交付提交被注入失败时，源 boundary 仍保留 `result_ready` 原文；恢复后父 Context 收到相同 JSON，子模型调用没有增加。

## 实现拆解

`DurableToolBoundary.persist_delegation_batch()` 在调度器启动第一个 worker 前写入合同摘要、预留额度和委派引用。`record_delegation_result_ready()` 只接受已经进入 State `result_ready` 的结果，并在保存前重新计算规范化原文的 hash。

父 Runtime 交付时先构造本次 `ExecutionResult`，再调用 State 的 `commit_delegation_tool_result()`。随后 boundary 以当前 Context 和 State 导出一次 session；如果这次原子替换失败，Runtime 不会进入下一次父模型请求。提交成功后 pending 原文从 boundary 移除，State 继续保留结果 ID、hash、摘要和 usage 供 Trace 查看。

恢复入口 `ResumeCandidate.prepare_resume()` 将待交付结果按 boundary 中的父 call 顺序读取。每条原文先与源 State 的 `result_ready` 记录交叉校验，再写入恢复 State 和新的父 Context；没有 ready 原文的调用沿用 v0.33 的 issue 分类。源 session 保持只读，恢复只 claim 一次，也不继承旧 PID、stdin 或 verification 资格。

## 本版特性、下一课与代码索引

本版新增了跨进程可恢复的委派结果交付：子结果先持久化为 `result_ready`，父侧再按模型顺序把 State、attempt、`role=tool` 和 durable boundary 一起提交为 `committed`。恢复使用同一份原文，不重跑子代理；没有原文的调用继续由 v0.33 的用户决策流程处理。

下一阶段会继续处理更高层的任务编排问题；本课没有引入可写子代理、递归委派或跨主机 worker。

核心实现入口：

- [`src/mini_agent/session.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.39/src/mini_agent/session.py)
- [`src/mini_agent/state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.39/src/mini_agent/state.py)
- [`src/mini_agent/delegation.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.39/src/mini_agent/delegation.py)
- [`src/mini_agent/runtime.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.39/src/mini_agent/runtime.py)
- [`src/mini_agent/resume.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.39/src/mini_agent/resume.py)
- [`src/mini_agent/context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.39/src/mini_agent/context.py)
- [`src/mini_agent/trace.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.39/src/mini_agent/trace.py)
- [`tests/test_durable_delegation_v039.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.39/tests/test_durable_delegation_v039.py)

完整运行配置见[操作手册](../operation/manual.md)，版本意图和验收边界见[受控子代理委派实施计划](../plans/subagent-delegation-plan.md)。
