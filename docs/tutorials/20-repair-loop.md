# 第 20 课：修复循环（Repair Loop，v0.20）

上一课：[单文件检查点与回滚](19-checkpoint-rollback.md) · [教程总览](README.md) · 下一课：[任务轨迹回放](21-trace-replay.md)

> 代码快照：`v0.20` · 相邻差异：`v0.19..v0.20` · 命令环境：Bash/zsh

> 运行要求：Python 3.10+；核心运行时只使用标准库。

## 本课目标

前几课已经能记录失败、选择 `retry`/`adjust`/`rollback`，也能为新的 generation（验证边界）运行检查。但如果模型在失败后直接继续写文件，或者把恢复动作的“执行成功”当成任务已经修好，运行时就无法保证验证证据属于当前环境。v0.20 把这条异常路径固定成一个有上限的 Repair Loop（修复循环）：

```text
失败 → 诊断 → 受限恢复 → 新 generation → 独立 verification
                         ↘ 继续 / 再次修复 / blocked / failed
```

读完本课，你应能解释：

- `idle`、`diagnosis_required`、`verification_required` 三个阶段各自允许什么；
- 为什么初始失败不消耗 repair cycle，而真正激活的 `retry`、`adjust` 或 `rollback` 才消耗一个周期；
- 为什么恢复成功后下一工具回合必须是单个 `run_shell(purpose="verification")`；
- 为什么 executor 和 agent loop 都做准入检查，同时仍为每个模型 tool call 回灌结果。

## 为什么需要本版

v0.19 已经能把文件回滚到检查点，但“恢复动作完成”仍不是“任务正确”。此前 State 只有一个 `verification_required` 布尔信号：它无法表达“先诊断再恢复”的中间阶段，也无法阻止模型在待修复时直接执行新的副作用。另一个边界问题是 repair cycle 被错误地按验证失败次数增加，权限拒绝或恢复参数无效也可能混淆预算含义。

本版把阶段、活动 failure、活动 recovery 和周期预算放入 State。运行时仍不替模型选择业务修复方案；模型通过既有 `recover` 工具提供诊断理由和动作，运行时只保证顺序、引用、权限、generation 和上限。

## 前置条件

建议先阅读第 17、18、19 课。先查看版本差异，再切到本课快照：

```bash
git checkout v0.19
git diff --stat v0.19..v0.20
git diff v0.19..v0.20 -- src/mini_agent/state.py src/mini_agent/tools/base.py src/mini_agent/agent.py
git checkout v0.20
```

本课的 `v0.20` tag 由发布者在实现验收后手动创建；如果本地还没有该 tag，源码链接和差异命令应保留为发布说明，而不是把当前工作树误当作历史快照。

## 新增与改动文件

| 文件 | 变化 | 作用 |
|---|---|---|
| [`state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.20/src/mini_agent/state.py) | 新增 repair loop 状态与活动因果引用 | 保存阶段、failure/recovery、周期预算和完成条件 |
| [`tools/base.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.20/src/mini_agent/tools/base.py) | 增加执行前阶段准入 | 在 PermissionGate 和 handler 前拒绝不合规调用 |
| [`agent.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.20/src/mini_agent/agent.py) | 增加回合级准入 | diagnosis 下 recover 必须独占；verification 下只接受单调用 |
| [`context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.20/src/mini_agent/context.py) | 渲染 Repair Loop critical state | 压缩后仍显示阶段、活动引用和预算 |
| [`prompt.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.20/src/mini_agent/prompt.py) | 补充协议规则 | 让模型知道失败后先诊断，恢复后先独立验证 |

## 关键流程

```text
normal execution
  ├─ possible effect 成功 → phase 保持 idle，但使完成证据失效
  └─ 任意需处理的失败 → FailureEvent + diagnosis_required

diagnosis_required
  ├─ read-only / update_todo → 留在诊断阶段
  ├─ recover（独占回合）→ 校验活动 failure、权限和预算
  └─ possible effect / verification → 协议错误，不执行 handler

accepted retry / adjust / rollback
  ├─ cycles_used += 1
  ├─ 开启 successor generation
  └─ recovery attempt 结束 → verification_required

verification_required
  ├─ 单个独立 verification 通过 → idle，清除活动引用
  └─ verification 失败 → 新 FailureEvent + diagnosis_required
```

`ask` 和 `block` 仍然是保守终态 `blocked`。周期预算默认是 `MAX_REPAIR_CYCLES = 3`：初始失败、无效恢复、权限拒绝和 `ask`/`block` 不消耗；恢复动作只有在目标获得授权并激活新 generation 后才计入。达到上限后不能静默开始第四次恢复，运行时以明确的 `failed` 原因收口。

## 实现拆解

### 1. State 是阶段的唯一事实源

`AgentState.snapshot()` 新增 `repair_loop`：

```text
phase                 idle | diagnosis_required | verification_required
active_failure_id     当前必须处理的 FailureEvent
active_recovery_id    当前 generation 对应的 RecoveryAction
cycles_used           已激活的恢复周期
cycles_remaining      剩余额度
required_next_action  运行时要求的下一步
```

验证失败不会直接增加 `cycles_used`。只有 `reserve_recovery()` 经过目标校验、PermissionGate 放行并调用 `activate_recovery()` 时，`retry`、`adjust` 或 `rollback` 才会打开新 generation 并计数。活动 failure 引用也会防止模型恢复一个已经被更新失败取代的旧事件。

### 2. 两层准入保证顺序

agent loop 能看到一整批 tool calls，因此负责回合级约束：diagnosis 中 `recover` 必须独占；verification 中只能有一个正确用途的 shell 调用。ToolExecutor 仍做第二层检查，保护直接调用、嵌入式使用和并发入口。准入失败会返回结构化协议结果，handler、PermissionGate 和 generation 都不会被调用；agent loop 仍按原始顺序为每个 call 添加 `role=tool`。

恢复目标带着受 State 锁保护的 reservation 执行，是 verification 阶段中唯一的受控例外。它结束后不能绕过独立 verification。

### 3. 上下文与完成提醒

Structured State 和压缩后的 critical state 都保留 repair loop。失败后 Runtime Notice 会要求只读调查、更新 Todo 或独占 recover；恢复后要求下一回合独立 verification。相同 `progress_marker` 下继续输出纯文本仍会进入既有 `blocked` 规则，因此模型不能用文字跳过阶段。

## 为什么这样设计

- 阶段机把“失败事实”“模型诊断”和“验证证据”分开，避免一条自然语言成功消息改变执行状态。
- 活动 failure 是显式引用，不靠最近时间猜测因果关系；旧 failure 不能在新 failure 出现后继续驱动恢复。
- 两层准入分别覆盖批量协议和直接执行边界，但都只做安全约束，不引入第二个 planner。
- 以实际激活的恢复动作计周期，拒绝不会消耗修复机会；恢复 attempt 失败仍算已使用，因为副作用已经获准执行。

## 本版特性、下一课与代码索引

本版包含：阶段化 Repair Loop、活动 failure/recovery、恢复周期预算、恢复后独立 verification、agent loop 与 executor 双层准入、Structured State/Runtime Notice 同步，以及 `ask`/`block`/预算耗尽的明确终态。

本版不包含自动推断修复方案、自动重试、外部等待恢复、通用事务回滚或 Trace & Replay；下一课会只读消费 v0.17–v0.21 已记录的事实，不应重新执行工具。

代码索引：

- [`state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.20/src/mini_agent/state.py)：RepairPhase、活动引用、generation 与预算；
- [`recovery.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.20/src/mini_agent/recovery.py)：recover schema 和动作调度；
- [`tools/base.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.20/src/mini_agent/tools/base.py)：结构化执行与第二层准入；
- [`agent.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.20/src/mini_agent/agent.py)：回合调度和完整 tool result 回灌；
