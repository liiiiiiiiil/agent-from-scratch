# 第 24 课：证据驱动重规划与停滞收口

上一课：[只读规划与用户交接](23-plan-mode-handoff.md) · [教程总览](README.md) · 下一课：后续版本课程（待规划）

代码快照：`v0.24` · 相邻差异：`v0.23..v0.24`

本课示例命令适用于 Bash/zsh。运行前请在仓库根目录准备本地配置；命令行参数只是命令行首条任务，处理后进程仍会进入交互循环。

## 本课目标

上一课已经能让 Agent 先调查、提交计划，并在 `--plan` 模式等待用户决定。本课结束后，读者应能解释两件事：执行中的新证据如何要求模型引用真实来源来提交计划修订，以及重复工具回合为什么会先收到一次 Runtime Notice、随后进入有原因的 `blocked` 状态。

本课只增加一个单 Agent loop 中的运行时策略。它不增加独立 Planner，也不实现下一课的完整 Plan Trace。

## 前置条件

需要基础 Python、Bash/zsh 和上一课的 Plan Contract 概念。建议先阅读[第 23 课：只读规划与用户交接](23-plan-mode-handoff.md)，因为本课继续使用 `planning_state`、`PlanRevision`、`PermissionGate` 和 `Repair Loop`。

先查看上一课和本课的实际差异：

```bash
git checkout v0.24
git diff --stat v0.23..v0.24
```

若本地还没有 `v0.24` tag，可以留在当前分支阅读源码；固定链接和发布事实检查仍以该 tag 被用户创建后的快照为准。

## 新增与改动文件

| 文件 | 本课作用 |
| --- | --- |
| `src/mini_agent/state.py` | 保存四类 trigger、revision 差异、replan 预算、blocked 恢复决定和停滞状态，并在一把锁内完成校验与转换。 |
| `src/mini_agent/tools/plan.py` | 注册模型可用的 `request_replan` 工具。 |
| `src/mini_agent/tools/__init__.py`、`src/mini_agent/permission.py` | 注册计划控制工具，并让 `request_replan` 默认允许通过权限闸门。 |
| `src/mini_agent/tools/base.py` | 在单调用边界执行新的 planning/repair gate。 |
| `src/mini_agent/agent.py` | 拒绝非法混合回合，并在所有工具结果回灌后计算回合指纹、事实摘要和停滞状态。 |
| `src/mini_agent/__main__.py` | 增加 `/resume <反馈>`，防止普通文字静默恢复终态。 |
| `src/mini_agent/context.py`、`src/mini_agent/prompt.py` | 展示活动 trigger、预算、停滞计数和合法下一动作，向模型说明 Direct Path 的重规划例外。 |
| `src/mini_agent/config.py` | 增加三个有界推进和重规划预算，并在启动时校验。 |

## 版本变更定位

上一课的真实收口是 `commit_plan` 后进入 `executing`，失败则交给 Repair Loop；模型没有一个能引用执行事实的修订入口。下面的基线图保留这条调用链，便于把新增入口放回原来的单 Agent loop：

```text
[旧] agent_loop -> ToolExecutor -> PermissionGate -> handler
                                  -> AgentState.attempts / failures
[旧] commit_plan -> PlanRevision -> executing
[旧] verification -> generation evidence -> completion reminder
```

v0.24 在这条链上插入 trigger 校验和完整回合观察。箭头表示调用或事实流向；`[C]` 是主要消费者，`[B]` 是本版边界：

```text
[旧] agent_loop -> [~] Planning/Repair gate -> [旧] ToolExecutor -> [旧] PermissionGate -> handler
       |                    |                         |
       |                    +--> [C] request_replan -> [~] AgentState trigger / budget
       |                                              +--> [C] commit_plan -> PlanRevision.diff
       +--> [~] all tool results + role=tool history -> [C] observe_tool_round
                                                        +--> Runtime Notice -> blocked
[旧] verification -> [~] current generation evidence -> completion
[+] /resume -> blocked_resume trigger -> Explore -> commit_plan
[B] Plan Trace remains a later version capability
```

对应关系是：`state.py` 保存来源、预算、差异和停滞事实；`agent.py` 负责整轮独占准入与观察时机；`base.py` 负责单调用闸门；`__main__.py` 只允许 CLI 写入 blocked 恢复决定。这样可以区分“事实允许修订”“计划结构真的改变”和“工具回合持续没有进展”三个判断。

可以用下面的命令只看运行时骨架；这些入口说明了事实保存、工具注册和回合观察分别在哪里发生：

```bash
rg -n "class ReplanTrigger|class LoopStagnationState|def request_replan|def observe_tool_round" src/mini_agent/state.py
rg -n "request_replan|observe_tool_round|planning_batch_errors" src/mini_agent/agent.py src/mini_agent/tools
```

## 关键流程

上一课的流程在计划提交后进入 Execute。v0.24 在执行或诊断阶段插入了一个由真实 State 事实驱动的入口：

```text
[旧] executing / diagnosis_required
      -> 执行、失败或只读调查

[+] diagnosis_required + FailureEvent
      -> 独占 request_replan(failure, failure_id, reason)
      -> exploring -> 只读调查 -> commit_plan(parent + trigger)

[+] executing + 成功只读观察 attempt
      -> 独占 request_replan(observation, attempt_id, reason)
      -> exploring -> 只读调查 -> commit_plan(parent + trigger)

[+] blocked
      -> CLI /resume <反馈>
      -> running / exploring -> 由 blocked_resume trigger 引用的 commit_plan

[+] 每个完整工具回合
      -> 执行或拒绝全部 calls -> 按顺序写入 State -> 回灌全部 role=tool
      -> 观察持久进展和首次有效事实
      -> 第 2 个无进展回合 Runtime Notice；第 3 个进入 blocked
```

图中的“真实来源”是关键：failure trigger 必须指向当前活动 `FailureEvent`，observation trigger 必须指向当前 active revision 提交之后成功且获准的只读 `ExecutionAttempt`。如果任务走 Direct Path，因 failure 或 `/resume` 进入 Explore 时还没有 parent；这时首次 `commit_plan` 有 trigger 但没有 parent。普通任务的初始计划仍同时没有 trigger 和 parent。

## 上一版的问题

如果模型只修改计划文本，Runtime 无法判断它是在回应失败、采纳新观察，还是无理由地重写方案。旧方案也会因此失去可追溯的 parent 关系。另一类问题更隐蔽：工具调用可能每次都“成功”，但一直读取同一结果、重复同一动作或提交没有结构变化的计划，Agent loop 会继续消耗回合直到全局上限。

v0.24 把这两个问题分开处理。`request_replan` 只负责把一个真实事实变成活动 trigger；`commit_plan` 再根据 parent 和新快照计算差异。停滞检测只看完整工具回合和确定性摘要，不让模型自己声明“我有进展”。

## 核心概念：trigger 和 revision 差异

Trigger 是“为什么现在允许改变计划”的运行时记录。模型只能请求 `failure` 或 `observation`；用户驳回、继续调查和 blocked 恢复由 CLI 写入 `user_feedback` 或 `blocked_resume`。每条记录保存来源字段，其他来源字段为空；一个活动 trigger 只能被一个有效新 revision 解决。

模型调用入口很小，因为来源校验不应藏在自然语言里：

```python
request_replan(kind="observation", source_id="a-7",
               reason="调查结果显示原步骤依赖不存在")
```

Runtime 随后检查当前阶段、来源 attempt、总预算和是否已有活动 trigger。这个调用本身不产生 attempt、generation 或 verification evidence。失败诊断中的 trigger 会保留 `active_failure_id`；新 revision 提交后才把 repair phase 从 `diagnosis_required` 转回 `idle`，这只表示形成了新方案，不表示旧 failure 已被修复。

新 revision 的差异由两份已校验快照计算，而不是由模型提供的 `reason` 代替：

```text
retained: 仍存在的 step_id，以及 dependencies_changed
added:    新增 step_id
cancelled: parent 中消失且没有被替换的 step_id
replaced: 新步骤的 replaces 所引用的 parent step_id
goal_changed / constraints_changed / success_criteria_changed
```

只有结构真正变化才会追加 revision，并消耗一次 `MAX_REPLAN_REVISIONS`。同一 trigger 的结构无变化提交只增加 `trigger_no_progress_commits`；第二次无变化提交后任务进入 `blocked`，trigger 和 revision 都不被误消费。依赖变化可以出现在 `retained` 中，步骤内容和步骤级成功标准仍遵守上一课的稳定 ID 规则。

## 核心概念：预算和 blocked 恢复

默认 `MAX_REPLAN_REVISIONS=3`，它是整个任务共享的后续 revision 预算。第三次有效修订仍可以继续执行；请求第四次时，Runtime 进入 `blocked`。`MAX_NO_PROGRESS_REPLANS=2` 只属于当前 trigger，用来收口重复提交，不会把一个无效提交算成新 revision。

blocked 是运行时的终态，普通文字和模型工具都不能把它改回 `running`。用户可以输入：

```bash
PYTHONPATH=src python -m mini_agent "检查并修复项目问题"
# 交互中
/resume 外部依赖已经准备好，请重新调查入口
```

`/resume` 保存 `resume_blocked` 决定、恢复前的 `terminal_reason` 和活动 failure，然后创建 trigger 并回到 `running / exploring`。若 blocked 是 `recover(ask/block)` 留下的终态，Runtime 会保留 successor generation、repair budget 和独立 verification 要求，并把 repair phase 转回 `diagnosis_required`；因此新 revision 提交后仍要单独验证。如果当时没有计划，下一次 `commit_plan` 引用 trigger、不要提供 parent；如果已有计划，必须引用当前 parent。`failed`、重规划预算耗尽或已有活动恢复 trigger 的任务要使用 `/new <任务>`。

## 核心概念：确定性停滞检测

停滞检测的单位是完整工具回合。Runtime 先处理所有调用，按模型顺序提交 State 事实，展示结果并把每个 call 的结果写入 `history`；这之后才调用 `observe_tool_round`。因此第 3 个回合阻塞时，前面和当前回合的全部 `role=tool` 结果仍然存在。

它使用三类信号：

1. 动作指纹是按模型顺序连接的 `(tool, canonical_arguments_hash)`。换一个命令字符串或自然语言参数就会得到不同指纹，Runtime 不猜它们是否语义相同。
2. 持久进展标记包含计划结构和步骤状态、Planning/Repair phase、活动 trigger 来源、用户决定、已改变文件集合、`verification_required` 和当前 generation 的验证结论。attempt 数、自动 ID、generation 数值、工具历史长度和预算消耗不算进展。
3. 成功且获准的只读结果以及首次成功的 possible 动作各保存一个有界 hash。读到相同结果或重复动作不会再次取得“首次出现”信用；状态控制工具和 verification 只能通过持久标记变化清零。

`MAX_STAGNANT_ROUNDS` 默认是 3，且必须大于 1。第二个连续无进展回合会设置一次受保护 Runtime Notice，通知当前类别和同时满足两个 gate 的合法动作；第三个回合记录 `repeated_action`、`no_new_observation`、`explore_without_commit` 或 `execute_without_progress`，再进入 `blocked`。这不是 FailureEvent，因为工具可能没有失败。同一工具及参数指纹的执行上限 `MAX_ATTEMPT_FINGERPRINTS` 默认是 4，并且必须至少为 `MAX_STAGNANT_ROUNDS + 1`，这样一次新调查事实后仍保留三个完整的无进展回合用于提醒和收口。

## 实现拆解

`AgentState` 负责所有跨轮次事实和原子转换。`LoopStagnationState` 只保存 epoch、计数、最后回合指纹、最多 256 个观察 hash 和 possible 动作 hash，以及告警和最后原因。`begin_task`、`/new`、`/reset` 会清空这些任务级数据；持久进展标记变化会开启新的 epoch。

`ToolExecutor` 在 handler 和 `PermissionGate` 前执行 planning/repair gate。agent loop 另外查看完整 call 批次，因此 `request_replan` 与 `recover`、`commit_plan`、副作用或 verification 混合时，整轮每个 call 都返回协议结果而不执行。两层都拒绝非法调用，确保直接调用 Executor 也不能绕开 Explore 或 diagnosis 边界。

上下文每轮从 `snapshot()` 重建有界状态，只展示活动 trigger 的来源和理由、replan 剩余预算、`trigger_no_progress_commits`、停滞计数、短指纹和合法下一动作。完整工具输出仍属于可裁剪的协议历史，停滞状态只保存 hash；compaction 后这些关键字段仍由 State 快照重新渲染。

## 为什么这样设计

把“请求重规划”和“提交新计划”分成两次调用，可以让 Runtime 先验证证据来源，再验证 parent、步骤和差异。这样模型可以解释为什么改变方案，但不能用一句理由伪造失败或把用户反馈变成模型事实。把 failure trigger 与 Repair Loop 并列保留，也能让新方案形成后继续要求真实修改和当前 generation 的独立验证。

停滞检测选择 hash 和结构化状态，是为了让收口行为可测试、可回放，并且不需要额外 LLM 判断两个结果是否语义相同。代价是相同内容的不同展示格式可能被当作不同事实，超过 256 项的 hash 集合也不会继续提供首次出现信用；这些边界避免 Runtime 通过猜测扩大职责。

## 设计边界

- 本版不实现 Plan Trace；`PlanDifference`、trigger 和停滞摘要先进入公开 State，下一版再扩展只读回放。
- replan 不消耗 Repair cycle，也不打开 generation；实际修改、恢复和 verification 仍遵守阶段六规则。
- 失败修订不会删除 FailureEvent、旧 verification history 或旧 revision；计划提交也不能代替独立验证。
- `/resume` 是唯一的 blocked 恢复入口；模型不能请求 `user_feedback` 或 `blocked_resume`，普通文字不能恢复 `failed`。
- `MAX_ITERATIONS` 仍是全局最终保险；停滞护栏只处理完整工具回合，无 tool_calls 的阶段性文本继续使用上一版 completion reminder。

## 本版特性、下一课与代码索引

本版新增证据驱动的 `request_replan`、四类可追溯 trigger、有限 revision 和无进展预算、blocked 恢复入口，以及跨 Direct/Explore/Execute 的确定性停滞收口。后续版本课程再讨论其他只读分析能力，本课不包含那些实现。

固定到本课代码快照的入口：

- [`state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.24/src/mini_agent/state.py)：trigger、预算、差异、恢复和停滞状态。
- [`plan.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.24/src/mini_agent/tools/plan.py)：`request_replan` schema 与 handler。
- [`agent.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.24/src/mini_agent/agent.py)：整轮混合准入和回合观察位置。
- [`base.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.24/src/mini_agent/tools/base.py)：单调用 Planning/Repair gate。
- [`__main__.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.24/src/mini_agent/__main__.py)：`/resume` 用户恢复入口。
