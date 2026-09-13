# 阶段七：自适应规划与重规划（Adaptive Planning & Replanning）实施计划

> 状态：`v0.22` Plan Contract、`v0.23` Plan Mode & Handoff、`v0.24` Replanning Policy 与 `v0.25` Plan Trace & Evaluation 已实现
> 当前基线：`v0.25`（Plan Trace & Evaluation；`v0.24` Replanning Policy 为前一版基线）
> 前置阶段：阶段五项目感知与任务编排（`v0.14`–`v0.16`）与阶段六可靠执行（`v0.17`–`v0.21`）
> 版本范围：`v0.22`–`v0.25`

## 1. 目标与定位

阶段五已经让模型用 Todo 维护动态工作清单，阶段六又让失败、恢复、验证和终态成为可审计的运行时事实。但现有 Todo 仍主要回答“接下来做什么”：它没有稳定步骤 ID、依赖、步骤完成标准和计划修订原因，也没有由 Runtime 强制的只读规划边界。当新证据证明原方案不再成立时，模型可以重写 Todo，却无法结构化说明哪部分计划被保留、替换或取消，以及为什么需要改变方案。

阶段七解决的是更高一层的问题：**先基于只读调查提交可验证计划；执行中出现足以推翻原方案的新事实时，显式触发重规划并形成新的不可变修订；当普通调查、执行或重规划没有产生新事实和任务进展时及时收口；最后能够回放从触发证据、计划变化到执行和验证的完整因果链。**

阶段七的设计原则是：

```text
明确目标 + 只读调查 + 可验证计划 + 证据驱动修订 + 有界推进 + 可回放验收
```

目标流程：

```text
Task
  -> Direct execution                         # 简单任务保持短路径
  -> Explore -> Commit plan -> Execute        # 复杂任务
                         -> Observe / Verify
                         -> Continue / Done
                         -> Replan trigger
                              -> Explore
                              -> Commit revision
                              -> Execute / Block / Fail
  -> Plan Trace
```

阶段五至阶段七的边界如下：

| 能力 | 阶段五 | 阶段六 | 阶段七 |
|---|---|---|---|
| 任务表示 | 可整体替换的 Todo 列表 | 为 Todo 快照保留只读修订历史 | 带稳定步骤 ID、依赖和完成标准的 Plan Contract |
| 正常执行 | Plan → Execute → Observe → Verify | 为执行和验证建立 generation 事实 | 用已提交计划约束复杂任务的执行入口 |
| 局部失败 | 模型自行调整 Todo | 分类并执行 retry / adjust / rollback 等受限恢复 | 原方案失效时转入显式 replan，不把 replan 当 retry |
| 规划边界 | 依靠 prompt 提醒先调查 | Repair Loop 只限制失败后的工具准入 | Runtime 强制 Explore 阶段只允许无副作用调查 |
| 人机交接 | 无独立 Plan Mode | ask 只表示等待外部条件 | `--plan` 提交计划后等待用户批准或驳回 |
| 审计 | 当前 Todo 可见 | 按 generation 回放执行因果链 | 回放计划触发、修订差异、批准与执行证据 |

## 2. 范围与非目标

### 2.1 本阶段范围

- 用结构化 Plan Contract 表达任务目标、成功标准、步骤、依赖和步骤级验证要求
- 为计划步骤分配任务内稳定 ID，为每次结构变化创建不可变 `PlanRevision`
- 区分计划结构变更与步骤进度更新，禁止用进度更新偷偷改写目标或步骤内容
- 支持模型显式进入 Explore，并由 Runtime 阻止该阶段的可能副作用和 verification
- 提供强制只读的 `--plan` 模式，以及计划提交后的用户批准、驳回和继续调查交接
- 根据明确的失败或观察事实创建 `ReplanTrigger`，再提交引用该触发器的新计划修订
- 对计划修订次数和无进展重规划施加稳定计数预算
- 对 Direct、Explore、Execute 和 Replan 共用的 Agent Loop 增加确定性的停滞检测，在连续工具回合既无任务进展也无新有效事实时提醒并有界收口
- 将计划修订、阶段转换和批准记录接入阶段六的 Trace & Replay
- 保留简单任务无需建计划的最短路径

### 2.2 本阶段不做

- 不引入独立 Planner Agent、Explore Agent、Reviewer Agent 或多 agent 协议
- 不让 Runtime 从自然语言任务中推断“唯一正确”的计划、业务约束或成功标准
- 不做候选计划树搜索、自我打分、投票或自动选择最优方案
- 不调用额外 LLM 或 Agent 判断两个动作、两段输出是否“语义等价”，也不尝试评价调查事实的业务价值
- 不让计划声明改变工具的 `effect_class`，也不让模型把有副作用工具声明为只读
- 不把计划批准视为工具授权；执行时仍逐次经过 `PermissionGate`
- 不因进入新计划修订而清除 FailureEvent、RecoveryAction、verification history 或旧 PlanRevision
- 不把计划修订本身视为环境变化或验证证据；只有实际副作用沿用 generation 规则
- 不提供跨进程计划持久化、断点续跑、事务数据库或分布式调度
- 不实现通用沙箱、工作树隔离、多文件事务或 shell 副作用回滚
- 不引入第三方运行时依赖

## 3. 先冻结的架构决策

### D1：Plan 是模型意图，不是 Runtime 事实

Plan Contract 描述模型准备怎样完成任务。`ExecutionAttempt`、`FailureEvent`、`RecoveryAction` 和 `VerificationEvidence` 仍描述实际发生了什么。两类信息可以互相引用，但不能互相替代：

- 步骤标记为 `completed` 不证明对应修改正确
- `success_criteria` 不会自动成为通过的验证证据
- 实际工具成功也不会自动完成某个计划步骤
- 最终 `done` 继续要求当前 generation 上的新验证证据，并满足计划完成条件

Runtime 只校验 Plan Contract 的结构、不变量和引用完整性，不判断计划在业务上是否聪明或充分。

### D2：简单任务保持 Direct Path，复杂任务由显式动作进入规划

Runtime 不根据任务长度、关键词或模型评分隐藏地分类复杂度。普通模式下，模型认为任务需要规划时调用 `begin_plan`；调用被接受后才进入 `exploring`。未调用 `begin_plan` 的简单读取、计算或单步操作继续走现有短路径。

`--plan` 是用户显式要求的例外：任务开始时 Runtime 直接进入 `exploring`，不允许 Direct Path。这样“是否强制先规划”有可观察来源，而不是依靠启发式猜测。

### D3：结构修订与进度更新使用不同协议

阶段五的 `update_todo` 同时承担“改计划内容”和“改执行进度”，无法判断一次更新是在推进旧计划还是替换方案。阶段七将两类写入分开：

- `commit_plan`：提交完整 Plan Contract；初次提交或改变目标、成功标准、步骤、依赖时创建新 `PlanRevision`
- `update_plan_progress`：只更新当前 revision 中已有步骤的状态，不得改变内容、依赖或成功标准

从 `v0.22` 起，模型可见工具以这两个协议为准；旧 `update_todo` 不再作为模型可调用工具注册。内部可以保留兼容读取或迁移辅助，但不得形成第二条可写计划来源。

### D4：PlanRevision 是 append-only 修订，不覆盖历史

每次 `commit_plan` 创建一个任务内单调递增的 revision。模型提交任务内步骤 ID，Runtime 校验格式、唯一性和跨 revision 一致性：同一 ID 的内容与步骤级成功标准不得改变；改变步骤含义必须创建新 ID，不能复用旧 ID 改写历史。依赖关系可以随 revision 调整，因为它描述的是当前执行顺序，不会重写步骤本身。

Runtime 根据相邻 revision 计算 `retained`、`added`、`cancelled` 和 `replaced` 差异。模型提供修订理由和显式 `replaces` 引用，但差异以两份已校验快照为准，不从自然语言理由推断。已完成且不再被活动步骤依赖的步骤可以不出现在新 revision 中，但其历史状态必须保留在旧 revision 和 trace 中。

### D5：Explore 是工具准入边界，不只是 prompt 建议

`planning_phase=exploring` 时：

- 允许 `effect_class=none` 的调查工具
- 允许独占调用 `commit_plan`
- 允许普通模式下、尚无失败触发器时调用 `cancel_planning`
- 拒绝所有 `effect_class=possible` 工具
- 拒绝 verification，因为尚无新执行结果需要验收
- 拒绝同一回合混合 `commit_plan` 与其他调用

该检查必须同时存在于 agent loop 的整轮准入和 ToolExecutor 的单调用准入中。被拒绝的每个 tool call 仍按原始顺序获得对应 `role=tool` 错误结果，但不进入 PermissionGate 或 handler，也不推进 generation。

### D6：计划批准不等于副作用授权

`--plan` 模式提交 revision 后进入 `awaiting_approval`：

- 用户通过 CLI 明确批准指定 revision，才进入 `executing`
- 驳回必须带反馈并产生新的用户反馈型 `ReplanTrigger`，随后回到 `exploring`
- 继续调查也必须带反馈；保留当前 revision，创建用户反馈型 trigger 并回到 `exploring`。调查后可引用 trigger 提交新 revision，或由用户用 `/review <revision_id>` 将未改变的当前 revision 重新交付审批
- 批准过期 revision、重复批准或批准后被新 revision 替代的请求必须拒绝
- 批准只表示“可以按该方案继续”，不向 `PermissionGate` 写入 allow 规则
- 之后每个写文件、execution shell 或恢复动作仍按原有权限策略询问或拒绝

普通模式的计划提交可直接进入 `executing`；是否需要人工批准只由任务启动模式决定，模型不能自行关闭强制批准。

### D7：Replan 必须引用触发事实，并受预算约束

重规划不是“想换一种写法”的自然语言声明。`request_replan` 必须创建 `ReplanTrigger`，引用以下一种来源：

- 当前活动 `FailureEvent`
- 已记录的只读 `ExecutionAttempt`，其输出带来了改变方案的新事实
- `--plan` 模式中的用户驳回或继续调查记录，或用户对 blocked 任务的明确恢复决定

除首次计划外，每个 `commit_plan` 必须引用当前活动 trigger。提交成功后 trigger 才变为 `resolved`；schema 错误、引用错误或预算拒绝不能消费 trigger。

至少维护两个硬上限：

- 单任务重规划次数不超过 `MAX_REPLAN_REVISIONS`
- 同一触发事实连续提交但没有结构差异的次数不超过 `MAX_NO_PROGRESS_REPLANS`

达到上限后不得继续生成同义 revision；有待用户或外部条件时进入 `blocked`，确定没有支持的执行路径时进入 `failed`。

### D8：Replan 与阶段六 Repair Loop 显式衔接

`retry`、`adjust` 和 `rollback` 处理的是当前方案内的局部恢复；`request_replan` 表示当前方案需要改变。两者不能在同一回合提交。

当 `repair_phase=diagnosis_required` 且模型选择重规划时：

1. `request_replan` 必须引用 `active_failure_id`
2. Repair Loop 保留该 failure，不伪装成已经恢复
3. planning phase 进入 `exploring`，只允许只读调查和 `commit_plan`
4. 新 revision 提交后，trigger 记录 `result_revision_id`，`repair_phase` 从 `diagnosis_required` 回到 `idle`，并清除 active failure 指针；原 FailureEvent 仍保留且可由 trigger 回溯
5. planning phase 回到 `executing`，后续实际修复工具仍按阶段六规则创建 attempt 和 generation
6. 修改后的独立 verification 通过后，原 failure 才可在 Plan Trace 中显示为“由新 revision 后的验证闭环解决”

计划修订只解决“下一步采用哪套方案”的诊断状态，不宣称原失败已经修复。它本身不创建 generation，也不消耗 repair cycle；真正获准的恢复或可能副作用仍按阶段六规则预留 generation 和预算。若已有 Repair Loop 要求下一回合独立 verification，则不得绕过它发起 replan。

阶段六已经进入 `blocked` 的任务不能由模型自行重开。只有用户明确要求继续、补充缺失条件或通过 CLI 发起 replan 时，Runtime 才记录 `resume_blocked` 类型的 `UserPlanDecision`，创建对应 trigger，将任务恢复为 `running / exploring`。旧 terminal reason、最后一个 failure 和全部执行事实继续保留。`failed` 表示既定边界内已无支持路径，不能原地恢复；用户若要改变目标或权限边界，应开始新任务。

### D9：计划约束不能放宽更高优先级规则

用户任务原文、项目级指令和 PermissionGate 始终高于模型提交的 Plan Contract。`constraints` 只是模型对已知约束的工作摘要：遗漏某条规则不代表该规则失效，写入冲突内容也不能覆盖受保护指令。

### D10：通用停滞检测是 Agent Loop 的确定性护栏

`MAX_REPLAN_REVISIONS` 和 `MAX_NO_PROGRESS_REPLANS` 只约束计划修订，不能把普通 Agent Loop 从“调用成功但原地打转”中收口。阶段七在现有单 Agent loop 中增加一个轻量、任务内的 `LoopStagnationState`；它不是新的 planner、phase 或失败恢复子系统，也不调用模型评价自己的进展。

检测单位是一个完整工具回合：assistant 一次回复中的全部 `tool_calls` 都按既有规则执行或拒绝、写入 State，并回灌对应 `role=tool` 结果后，Runtime 才观察这一回合。这样检测不会破坏批次顺序、并发只读提交顺序或“每个 call 都有结果”的协议。无 `tool_calls` 的阶段性文本继续由现有 completion reminder 处理，不建立第二套文本回复循环规则。

Runtime 使用三类确定性信号：

- **动作指纹**：复用阶段六的 canonical JSON 和 SHA-256，按模型顺序组成 `(tool, arguments_hash)` 回合指纹。这里的“等价动作”只指工具名和校验后参数完全相同；不通过改写 shell 文本、路径或自然语言参数猜测语义等价。
- **持久进展标记**：只包含会改变任务含义或可完成性的事实，例如 Plan Contract 的结构差异、步骤状态、planning / repair phase、active trigger、用户决定、文件集合变化和当前 generation 的验证结论。attempt 数量、tool history 长度、自动分配 ID、generation 自增、预算消耗和无结构差异的 revision 都不算进展，避免“制造记录”绕过检测。
- **有效新事实**：已通过 phase gate、PermissionGate 和 handler 且 outcome 为 `succeeded` 的 `effect_class=none` 调查调用，其工具结果去除 attempt ID、耗时等易变展示字段后，以稳定摘要计算 hash，并且该 hash 在当前进展 epoch 中首次出现；只保存 hash，不把完整输出复制进 State。`begin_plan`、`commit_plan`、`update_plan_progress`、`request_replan`、`recover` 等状态控制工具以及 verification 不走这条“调查事实”捷径，它们只能通过持久进展标记证明推进。重复读取同一内容、重复得到同一搜索结果、schema / phase / permission 拒绝和重复错误都不算新事实。成功的 `possible` 调用不伪装成调查事实，但一个此前未执行过的动作指纹可在当前 epoch 中记作一次候选推进；相同指纹的后续成功不会再次记作推进。

现有 `completion_reminder.progress_marker` 不能直接复用：它为“未满足完成条件却输出最终文本”的兼容提醒服务，包含 tool history 长度、generation 等机械变化；停滞检测必须使用上述更窄的持久进展标记，否则每次重复调用都会因新记录而错误清零。

持久进展标记发生变化时开启新的 progress epoch，并清零停滞计数；epoch 内首次出现的只读事实或执行动作也会清零当前连续计数，但其 hash 继续保留到下一次持久进展，防止在两组旧结果之间交替即可规避检测。若一个完整工具回合既没有改变持久进展标记，也没有产生 epoch 内首次出现的有效事实或动作，则 `consecutive_no_progress_rounds += 1`。

唯一新增的行为上限是 `MAX_STAGNANT_ROUNDS`，默认值为 `3`，配置必须大于 `1`：

1. 达到上限前一回合时，Runtime 通过受保护的 Runtime Notice 显示停滞类型、连续回合数和当前 phase 下合法的脱离方式，例如改变调查方向、`commit_plan`、更新步骤进度、引用真实触发事实 `request_replan`，或明确说明外部阻塞。
2. 下一回合仍无进展时，State 进入 `blocked`，`terminal_reason` 记录 `repeated_action`、`no_new_observation`、`explore_without_commit` 或 `execute_without_progress` 及相关脱敏指纹；停滞本身不伪造 `FailureEvent`，因为工具未必失败。
3. 终态前已产生的全部 tool result 必须先回灌 history；随后 loop 直接返回明确的 blocked 结果，不再让模型用另一次调用覆盖终态。

`LoopStagnationState` 只保存计数、hash、告警和最后原因，并进入 Structured State 与 compaction 的关键快照；hash 集合必须有固定内存上限，且只向模型渲染计数和最近的脱敏短指纹。Planning gate 与 Repair gate 始终优先，停滞提醒不能放宽它们的下一动作限制。`MAX_ITERATIONS` 继续作为全局最终保险，但不替代更早、可解释的停滞收口。

## 4. 状态模型（Plan core 于 `v0.22` 冻结；停滞护栏于 `v0.24` 追加）

```text
PlanStep
- step_id
- content
- status: pending | in_progress | completed
- depends_on: list[step_id]
- success_criteria: list[string]
- replaces: list[step_id]                 # 只引用 parent revision 中被替代的步骤

PlanRevision
- revision_id
- generation_id
- parent_revision_id: optional
- trigger_id: optional                 # 普通初始计划为空；Direct failure / blocked resume 的首个 revision 有 trigger 但无 parent
- goal
- constraints: list[string]
- success_criteria: list[string]
- steps: tuple[PlanStep]
- reason
- diff: optional PlanDifference           # retained / added / cancelled / replaced 与变化标记

PlanProgressEvent
- progress_id
- revision_id
- generation_id
- step_id
- from_status
- to_status
- reason

ReplanTrigger
- trigger_id
- generation_id
- kind: failure | observation | user_feedback | blocked_resume
- reason
- caused_by_failure_id: optional
- caused_by_attempt_id: optional
- caused_by_decision_id: optional         # 指向 rejected / resume_blocked 用户决定
- status: active | resolved | rejected
- result_revision_id: optional

UserPlanDecision
- decision_id
- revision_id: optional
- decision: approved | rejected | continue_exploring | resume_blocked
- feedback: optional
- generation_id
- caused_by_failure_id: optional
- previous_terminal_reason: optional

PlanningState
- mode: auto | plan_only
- phase: direct | exploring | awaiting_approval | executing
- active_revision_id: optional
- active_trigger_id: optional
- replans_used
- replans_remaining
- trigger_no_progress_commits            # 只属于当前活动 trigger

LoopStagnationState
- progress_epoch
- consecutive_no_progress_rounds
- last_round_fingerprint: optional
- seen_observation_hashes: bounded set[hash]
- seen_effect_action_hashes: bounded set[hash]
- warning_kind: optional
- last_reason: optional
```

不变量：

- 除初始 revision 外，每个 `PlanRevision` 有且只有一个存在的 parent
- replan revision 必须引用一个 active trigger，trigger 只能解决一次
- 每个 revision 内 `step_id` 唯一，依赖只能指向同一 revision 中的步骤，且依赖图无环；`replaces` 只能指向 parent revision
- 最多一个步骤为 `in_progress`；存在未完成依赖时，该步骤不能进入 `in_progress` 或 `completed`
- `update_plan_progress` 只能产生合法的 `pending -> in_progress -> completed` 转换，不能回退；失效或替换通过新 revision 表达
- 结构相同但仅步骤状态变化时不得创建新 revision，应产生 `PlanProgressEvent`
- revision、progress、trigger 和 user decision 都绑定提交时的 `generation_id`，但这些记录本身不推进 generation
- `UserPlanDecision` 不包含权限规则，也不能作为 VerificationEvidence
- 停滞计数只能在一个完整工具回合的所有结果已提交后更新；同一回合不能部分判定、提前终止或遗漏 tool result
- progress epoch 只由持久进展标记变化开启；新的 attempt ID、generation ID、计数预算消耗或重复事实不能单独开启 epoch

字段所有权：

- 模型提供 goal、constraints、success criteria、步骤 ID 与内容、依赖、替代关系、修订理由和 replan 请求
- Runtime 分配 revision / progress / trigger 等事实记录 ID，校验步骤 ID、依赖图与状态转换，计算 revision 差异和预算，并维护 planning phase
- Agent loop 在整轮结果提交后计算动作指纹、有效事实 hash 和持久进展标记，原子更新停滞计数；模型不能声明某回合“有进展”或清零计数
- Executor 继续独占工具执行事实；PermissionGate 继续独占授权决定
- 用户通过 CLI 或明确的后续输入产生 approve / reject / resume_blocked 决定；模型不能伪造这些记录
- Trace 只消费公开 snapshot，不读取模型私有参数或修改 State

## 5. 版本切片

### 5.1 `v0.22` Plan Contract

目标：把阶段五的可变 Todo 升级为结构化、可引用、可验证的计划协议。

主要工作：

1. 新增 `PlanStep`、`PlanRevision`、`PlanProgressEvent` 和 `PlanningState`。
2. 新增 `commit_plan`、`update_plan_progress`，停止向模型暴露旧 `update_todo`；`begin_plan` 与强制 Explore gate 留到 `v0.23`。
3. 为步骤 ID、依赖存在性、无环图、唯一进行中步骤和状态单向转换建立原子校验。
4. 初始 `commit_plan` 保存完整目标、约束摘要、任务级成功标准和步骤级成功标准。
5. Structured State 渲染当前 revision、当前步骤、未满足依赖和有界计划摘要；compaction 后无损重建。
6. completion reminder 同时检查当前计划是否仍有未完成步骤，但不改变阶段六的验证证据要求。

验收重点：计划结构与进度来源分开；非法依赖或进度回退不会部分更新 State；简单任务不建计划时行为与 `v0.21` 一致。

### 5.2 `v0.23` Plan Mode & Handoff

目标：让“先调查、再交付计划、经用户确认后执行”成为 Runtime 可强制的工作流。

主要工作：

1. 新增普通模式下由模型调用的 `begin_plan`，并为 CLI 增加 `--plan` 任务启动模式；两条入口都将 planning phase 置为 `exploring`。
2. 实现双层 Explore gate，按 D5 拒绝副作用、verification 和非法混合调用。
3. plan-only 任务提交 revision 后进入 `awaiting_approval`，agent loop 不继续执行计划。
4. 增加用户侧批准、驳回和继续调查入口；批准必须精确引用当前 revision。
   继续调查保留当前 revision；可提交引用用户反馈 trigger 的新 revision，或将未改变的 revision 重新交付审批。
5. 驳回反馈进入受保护的当前任务上下文，并创建可供下一 revision 引用的 trigger。
6. 明确展示“计划批准”和“工具授权”是两个不同事件。

验收重点：即使模型请求写文件或 execution shell，`exploring` 和 `awaiting_approval` 也不会产生副作用；批准旧 revision 不会错误启动执行；批准后高风险工具仍触发 PermissionGate。

### 5.3 `v0.24` Replanning Policy（已实现）

目标：在新事实推翻原方案时，以明确触发器和有限预算修订计划，而不是静默覆盖 Todo 或无限重写方案。

主要工作：

1. 新增 `ReplanTrigger`、`request_replan` 与重规划计数预算。
2. 支持 failure、只读 observation、用户驳回和 blocked 恢复四类触发来源，并校验来源记录真实存在。
3. `commit_plan` 对 replan 强制要求 active trigger 与 parent revision，提交后原子解决 trigger。
4. Runtime 计算相邻 revision 的 retained / added / cancelled / replaced 差异；结构无变化时记为无进展提交并受单独预算约束。
5. 按 D8 接入 `diagnosis_required`，保持 FailureEvent、repair budget、generation 与 verification 边界不被重规划绕过。
6. 按 D10 在现有 agent loop 增加跨 Direct / Explore / Execute / Replan 的停滞计数与单一 `MAX_STAGNANT_ROUNDS` 上限；该护栏复用结构化工具结果和计划状态，不新增 Agent 或规划分支。
7. Structured State 和 Runtime Notice 显示当前 replan 原因、来源、剩余预算、停滞计数和唯一合法下一动作。

验收重点：retry、adjust 与 replan 的入口和计数相互独立；每次 replan 都能定位触发事实；无变化重规划、重复工具回合、无新事实的调查或执行不推进都能在各自预算内明确收口，不形成隐藏循环。

### 5.4 `v0.25` Plan Trace & Evaluation（已实现）

目标：只读回放一次任务的规划链：调查 → 初始计划 → 批准 → 执行 → 触发事实 → 新 revision → 验证 → 终态。

本版不引入新的规划策略、执行动作或状态转换。它扩展 `v0.21` 的 Trace & Replay，按 generation 和 revision 两个稳定维度展示：

- revision 的 parent 与 trigger
- 相邻 revision 的结构差异
- progress event 与对应步骤
- plan-only 的批准或驳回
- trigger 引用的 attempt / failure / feedback
- revision 生效后的执行、恢复和 verification 事实
- 最后一次停滞告警或停滞终态的计数、类型和脱敏指纹
- 最终 `done`、`blocked` 或 `failed` 的依据

回放不得推测模型没有记录的修订理由，也不得把“后续验证通过”倒推成旧计划正确。Trace 只能展示 Runtime 已保存的停滞摘要，不能重新读取工具输出或用语义规则重判进展。缺失 parent、悬空 step dependency、重复 trigger 消费、无来源批准或跨 revision 进度事件必须标记为 unresolved，而不是由展示层修补。

验收重点：对至少一次“初始方案失败—证据触发重规划—新方案验证通过”的真实案例，能够证明计划为何改变、改变了什么、哪一代执行事实和验证证据支持最终结论。

## 6. 与阶段六状态机的组合

Planning State 与 Repair Loop 是正交状态，不能合并成一个不断扩张的枚举。工具准入同时检查两者，并采用更严格的限制：

| Planning phase | Repair phase | 允许的下一动作 |
|---|---|---|
| direct / executing | idle | 按现有规则执行、更新进度或验证 |
| exploring | idle | 只读调查，或独占 `commit_plan` |
| awaiting_approval | idle | 等待用户批准、驳回或结束；模型工具调用全部拒绝 |
| direct / executing | diagnosis_required | 只读诊断、独占 `recover`，或独占 `request_replan` |
| exploring | diagnosis_required | 只读调查，或独占 `commit_plan`；active failure 保留 |
| 任意 | verification_required | 仅允许阶段六要求的单个独立 verification，不得发起或提交 replan |

若两个状态组合不在表内，Runtime 应拒绝动作并返回结构化协议错误，不能自行选择较宽松分支。

通用停滞检测不加入上表的状态笛卡尔积。它只在每个完整工具回合结束后观察“该回合是否推进”，不会创造第三套 phase，也不会改变允许动作集合。处于 `awaiting_approval` 时没有模型工具回合，因此不累计停滞；等待用户不是 doom loop。进入 `blocked` 或 `failed` 后停止计数。

## 7. 测试与验收

### 7.1 单元测试

- Plan Contract 拒绝重复 ID、缺失依赖、自依赖、依赖环和超过长度/数量上限的字段
- 每个 revision 的 parent、trigger 与 generation 引用完整，失败提交不会部分写入
- 进度更新不能改变步骤结构、跳过依赖、产生多个 in-progress 或回退 completed
- 结构修订与纯进度更新分别写入 revision 和 progress history
- Explore gate 在 loop 与 executor 两层都拒绝 possible effect、verification 和混合调用
- 被 phase gate 拒绝的调用不进入 PermissionGate / handler，不推进 generation，且每个 call 都有 tool result
- `--plan` 只批准当前 revision；驳回反馈不能被模型伪造，批准不能写入权限 allow 规则
- replan trigger 的 failure / attempt / rejected decision / blocked resume 引用与唯一消费规则可重复测试
- blocked 只有在用户明确产生 resume decision 后才能回到 running / exploring；failed 不能原地恢复
- replan revision 预算和无进展预算在同一把 State 锁内检查并预留
- 相同 tool 与 canonical arguments 连续返回相同结果时不会因 attempt / generation 自增而被视为进展，并在 `MAX_STAGNANT_ROUNDS` 内收口
- Explore 中重复读取、重复搜索或在旧结果之间交替不会产生新有效事实；真正不同的只读结果只在当前 progress epoch 首次出现时重置连续计数
- Explore 连续无新事实且不 `commit_plan`、Execute 连续执行相同动作但不更新计划/任务事实、Direct Path 普通调用原地循环，都会得到对应的停滞原因
- phase / permission / schema 拒绝和重复错误不算进展；达到停滞终态前仍为每个调用回灌一个 tool result
- commit 新结构、合法步骤进度、真实 phase 转换、新用户决定或新验证结论会开启 progress epoch；纯 ID、generation、预算或历史长度变化不会
- 停滞告警经过 compaction 后仍保留计数和脱敏 hash；Planning / Repair gate 比告警建议更严格时以 gate 为准
- diagnosis → replan → execute → verification 的组合不清除原 failure，不复用旧验证证据
- compaction 后当前 revision、active trigger、planning phase、预算和步骤进度保持准确
- Plan Trace 只依赖公开 snapshot；断链、环、重复消费和损坏记录显示 unresolved
- 简单任务的 Direct Path、阶段六 Repair Loop 和旧 trace 查询没有回归

### 7.2 阶段级 E2E 场景

1. 简单只读任务不创建计划，直接完成。
2. 复杂修改任务先只读调查，提交含依赖与成功标准的计划，执行后独立验证通过。
3. `--plan` 下模型尝试写文件被拒绝；提交计划后等待，用户批准后才开始执行，执行仍触发工具权限。
4. 用户驳回初始计划并提供反馈，模型基于该反馈提交新 revision，旧 revision 保留。
5. 初始方案导致确定性失败或验证失败；模型引用真实 failure 发起 replan，新方案执行并在新 generation 验证通过。
6. 同一触发事实重复提交无结构变化的计划，预算耗尽后明确进入 blocked 或 failed，不死循环。
7. 普通模式分别复现“相同工具与参数重复调用”“Explore 重复读取无新事实”“Execute 重复动作但步骤不推进”，在告警后仍无变化时进入带明确原因的 blocked；改为新调查或提交真实进度时计数正确恢复。
8. 至少发生一次 compaction 后，步骤依赖、active revision / trigger、Repair Loop、停滞计数和 verification 边界仍准确。
9. Plan Trace 能把上述重规划案例与阶段六 attempt / failure / recovery / verification 因果链连接起来，并显示停滞终态依据。

### 7.3 阶段完成定义

- [x] `v0.22` Plan Contract 已有独立教程、变更记录和可运行测试
- [x] `v0.23` 已有独立教程、变更记录和可运行测试（tag 事实检查待用户手动创建 tag）
- [x] `v0.24` 已有独立教程、变更记录和可运行测试（tag 事实检查待用户手动创建 tag）
- [x] `v0.25` 有独立教程、变更记录和可运行测试
- [x] 计划结构、步骤进度、执行事实和验证证据有单一且不同的写入来源
- [ ] 简单任务保持短路径，复杂任务可以显式进入受 Runtime 约束的 Explore → Commit → Execute 流程
- [ ] `--plan` 在用户批准前不产生副作用，批准也不会绕过 PermissionGate
- [ ] 所有 replan 都引用真实触发事实，并保留 parent revision 和结构差异
- [ ] 失败触发重规划时，阶段六的 failure、generation、repair budget 和验证隔离仍然成立
- [ ] 重规划次数和无进展提交有硬上限，超限后不会隐藏循环
- [ ] Direct、Explore、Execute 与 Replan 共用确定性停滞护栏；重复动作、无新事实调查和无状态推进执行会先收到一次告警，再以明确 blocked 原因收口
- [x] Plan Trace 能回放至少一个失败—重规划—执行—验证案例，并明确标记损坏引用
- [ ] 默认测试套件、教程检查和阶段级 E2E 全部通过，核心运行时仍只有标准库
- [ ] 各版本 tag 由用户手动创建后，教程事实检查通过并完成发布验收

## 8. 版本依赖关系

```text
v0.21 Trace & Replay
    ↓ 提供 generation、TodoRevision 与失败/验证因果事实
v0.22 Plan Contract
    ↓ 提供稳定 revision、step 和进度协议
v0.23 Plan Mode & Handoff
    ↓ 提供 Explore 边界与用户批准事实
v0.24 Replanning Policy
    ↓ 用触发证据产生有界的新 revision，并为整个单 Agent loop 补齐通用停滞护栏
v0.25 Plan Trace & Evaluation
    ↓ 只读验收完整规划与执行链
```

不要在 `v0.22` 提前加入强制只读 Plan Mode，不要在 `v0.23` 用 prompt 模拟 replan trigger，也不要在 `v0.24` 同时引入独立 Planner Agent。通用停滞检测作为 `v0.24`“有界推进”策略的横切护栏落地，不改变 Explore → Commit → Execute → Replan 主线，也不派生新的规划角色或状态机。每版保持一个清晰主题，使相邻 tag 的代码 diff 可教学。

## 9. 文档与发布同步

每个版本完成时同步：

- `README.md` 的教学路径与当前状态
- `docs/tutorials/README.md` 的阶段七导航
- 对应版本教程
- `docs/operation/manual.md` 的最新版使用和状态协议
- `CHANGELOG.md`
- `pyproject.toml` 版本信息
- 本计划的实现状态与勾选项
- 必要时更新 `AGENTS.md` 中真正影响运行、授权和修改行为的硬约束

提交前运行：

```bash
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python scripts/check_tutorials.py
PYTHONPATH=src python scripts/check_readme.py
```

教程完成且用户手动创建对应 tag 后，再运行依赖本地 Git 对象的教程事实检查。助手不得创建、移动、覆盖、删除或推送 tag。

## 10. 阶段完成后的能力边界

阶段七完成后，mini_agent 不只是“有一份会变化的 Todo”，而是：**复杂任务可以先在无副作用边界内调查并提交可验证计划；计划执行中出现新事实时，能够说明为什么必须重规划、保留旧方案并提交有界的新 revision；普通调查、执行和重规划若持续没有新事实或任务进展，也会被确定性护栏提醒并收口；用户批准、工具授权、实际执行和最终验证各有独立事实来源，整条链可以只读回放。**

仍未解决的问题包括跨进程恢复、长期记忆、通用沙箱和多 agent 协作。下一阶段应从真实使用中的主要失败模式选择其中一个，而不是在阶段七提前混入。
