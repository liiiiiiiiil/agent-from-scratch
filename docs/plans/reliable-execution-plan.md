# 阶段六：Reliable Execution 实施计划

> 状态：规划中
> 当前基线：`v0.16`（计划驱动执行，Plan-driven Execution）
> 前置阶段：阶段五项目感知与任务编排（Project-Aware Task Orchestration，`v0.14`–`v0.16`）
> 版本范围：`v0.17`–`v0.21`

## 1. 目标与定位

阶段五已经建立了 Plan → Execute → Observe → Replan → Verify 的正常执行闭环。阶段六处理这个闭环中的异常分支：**发现执行失败，基于事实判断原因，采取受约束的恢复动作，再用新的验证证据决定继续、阻塞或失败；并能回放这条决策链，验证可靠性机制确实按约束工作。**

阶段六的设计原则是：

```text
事实依据 + 原因判断 + 受限动作 + 新验证证据 + 可回放验收
```

目标流程：

```text
Execute
  -> Observe failure
  -> Classify / Diagnose
  -> Recover
  -> Verify
  -> Continue / Block / Fail
  -> Trace / Replay
```

阶段五与阶段六的边界如下：

| 能力 | 阶段五 | 阶段六 |
|---|---|---|
| 任务计划与 Todo | 建立、推进和完成 Todo | 失败时要求模型重新评估当前 Todo |
| 正常执行 | 调用工具并记录事实 | 失败后的动作选择与预算控制 |
| 验证 | 记录当前 generation 的完成证据 | 验证失败后驱动诊断、修复和再次验证 |
| 重新规划 | 模型维护完整计划 | 不新增独立 planner；通过正常模型回合触发计划调整 |
| 回滚 | 不提供 | `v0.19` 起提供有边界的 checkpoint/rollback |
| 可靠性验收 | 不提供失败恢复的因果回放 | `v0.21` 只读回放已有执行事实与证据 |

## 2. 范围与非目标

### 2.1 本阶段范围

- 统一记录工具尝试、失败事件和恢复动作
- 为所有执行事实绑定 `generation_id`
- 标准化失败分类、可恢复性和阻塞/失败收口规则
- 对重试和恢复动作施加计数预算
- 支持 `retry`、`adjust`、`ask`、`block` 四类基础恢复动作
- 在明确边界内为文件修改建立 checkpoint 并回滚
- 将诊断、修复、验证串成有上限的 Repair Loop
- 按 generation 查询和回放已有的失败、恢复与验证证据，以验收可靠性约束

### 2.2 本阶段不做

- `v0.18` 不暴露或执行 `rollback`；需要回滚的场景只能 `block`，直到 `v0.19` 的能力可用
- 不比较或推断“环境是否完全相同”；预算约束只使用稳定的计数和参数指纹
- 不引入独立的 Diagnose Agent、Planner Agent 或多 agent 协议
- 不把任意 shell 副作用伪装成可回滚操作；未知副作用只能检查后继续或阻塞
- 不绕过现有 `PermissionGate`，不因失败自动提升权限
- 不引入第三方依赖、跨进程持久化或事务数据库
- 不让运行时替模型判断业务正确性；任务级结论仍以 Verify 证据为准
- `v0.21` 不引入任何新的执行、恢复、诊断或状态转换逻辑；它只查询和展示 `v0.17`–`v0.20` 已记录的数据
- `v0.21` 不属于阶段七的规划系统范畴。阶段七（Planning & Replanning）保持独立阶段；其重规划触发依赖阶段六的 `blocked` 状态，并可使用 `v0.21` 的回放能力进行调试，但不合并代码或阶段编号

## 3. 先冻结的架构决策

### D1：`generation_id` 与执行调度共同定义证据边界

`generation_id` 是任务内单调递增的验证边界，而不是对真实操作系统快照的声称：它表示“在此编号之前获准执行的所有可能副作用，都必须有此编号或更新编号上的新验证”。

- `begin_task()` 创建初始 generation。
- 每个可执行工具在注册时声明 `effect_class`：`none` 或 `possible`。现有 `write_file`、`edit_file`、`run_shell(purpose="execution")` 和 rollback 为 `possible`；只读工具为 `none`。运行时不从命令文本猜测副作用。
- PermissionGate 放行后、调用 handler 前，Runtime 必须在同一把 State 锁内为 `possible` 调用预留下一个 generation，并立即使旧验证失效。handler 随后超时、异常、非零退出或只写入部分内容也不回退该 generation。权限拒绝或参数在 handler 前被拒绝时不预留 generation。
- `none` 调用与 verification 调用不推进 generation；verification 只能绑定到已经提交的当前 generation。已接受的 recovery action 是例外：Runtime 必须在同一把 State 锁内预留其后继 generation；`retry`/`adjust` 的实际工具调用复用这个 generation，`ask`/`block` 则创建无 handler 的状态转换 generation，不能重复预留。
- 同一 LLM 回合只有全部为 `effect_class=none` 的调用才可以并发。含 `possible` 调用或 rollback 的回合必须按 tool-call 原始顺序串行执行、串行提交 State。verification 不得和 `possible` 调用处在同一回合；违反时该 verification call 产生 `invalid` 的工具结果且不进入 handler。恢复后的 Verify 也必须在恢复结果已回灌模型后的下一回合执行。
- `ExecutionAttempt` 记录 `pre_generation_id` 和预留后的 `generation_id`；`FailureEvent`、`RecoveryAction`、`ExecutionGeneration` 和验证证据都带其所属的 `generation_id`。每个已接受的恢复动作都必须开启一个新的 `ExecutionGeneration`，即使它本身不产生外部副作用；恢复前的验证证据不能跨 generation 复用于 `done` 判定。被 schema、预算或权限拒绝的动作不是已接受的 recovery，仍必须记录拒绝事实但不创建后继 generation。

因此，即使一个获准的文件写入在中途失败，旧验证证据也不能继续证明当前环境；同一批并发结果的到达顺序也不能改变 generation 的含义。

### D2：阶段六不拥有完整重新规划能力

阶段六的 Recovery Policy 只能决定当前失败的下一步动作，并可要求模型重新诊断。它可以建议调整单个工具调用的参数、命令或顺序，但不直接生成或替换完整 Todo。

当需要改变任务方案时，运行时发出结构化 recovery notice，下一次普通 LLM 回合负责调用已有的 `update_todo` 并提交新计划。这样“完整计划”仍由阶段五的 Task State 协议负责，阶段六只提供触发条件和事实。

### D3：RecoveryAction 的执行结果引用不代表任务修复

`RecoveryAction.result_attempt` 只引用恢复动作实际产生的 `ExecutionAttempt`；该 attempt 记录权限结果、退出码或异常等执行层事实。例如“重试关联 attempt `a-7`，其退出码为 1”。它不得声称任务已经修复。

任务级别的正确性、Todo 是否完成以及修改后环境是否满足目标，全部由后续 Verify 调用和阶段五的完成条件判断。Recovery 与 Verify 不能互相代替。

### D4：重试只受计数和预算约束

不要求判断两次尝试之间的环境是否相同。运行时至少维护以下硬上限（具体默认值可在实现时配置并写入教程）：

- 单个 `failure_id` 的恢复/重试次数不超过 `MAX_FAILURE_RETRIES`
- 同一 `(tool, arguments_hash)` 组合的执行次数不超过 `MAX_ATTEMPT_FINGERPRINTS`
- 单任务的恢复动作总数不超过 `MAX_RECOVERY_ACTIONS`
- Repair Loop 的诊断—修复—验证周期不超过 `MAX_REPAIR_CYCLES`

达到上限后不得静默重复；必须依据下表收口为 `blocked` 或 `failed`。

`arguments_hash` 固定为校验后的参数以 canonical JSON（键排序、稳定分隔符、类型不丢失）编码后计算的 SHA-256；State 和 Structured State 只显示 hash 与脱敏参数摘要，不显示凭据原文。计数检查和额度预留必须在实际恢复调用前、同一把 State 锁内完成；未预留到额度的调用不得进入 PermissionGate 或 handler。

### D5：`blocked` 与 `failed` 的判定规则

| 条件 | 状态 | 含义 |
|---|---|---|
| 瞬时/可重试失败，但对应预算已耗尽 | `blocked` | 可能仍可恢复，但当前运行不能继续安全尝试 |
| 需要用户输入、授权或外部资源，且当前通道不可用 | `blocked` | 等待外部条件，不宣称任务不可完成 |
| 诊断信息不足，继续动作会有未知副作用 | `blocked` | 保守停止，保留失败事实供人工或下一次运行处理 |
| 权限被明确拒绝且没有可用的 ask/授权路径 | `failed` | 当前任务在既定权限边界内不可执行 |
| 协议、参数或状态违反运行时不变量，且无法由调整动作修复 | `failed` | 当前执行路径确定无效 |
| 明确不可恢复的外部错误或 Repair Loop 预算耗尽且没有待等待条件 | `failed` | 已无受支持的恢复路径 |

`blocked` 不是成功的别名，`failed` 也不是异常字符串的别名；两者都必须携带最后一个 `FailureEvent` 和可读原因。

## 4. 数据模型（`v0.17` 冻结的最小 schema）

运行时事实与模型判断分开保存，四类记录都不可由 `update_todo` 直接修改。除初始 generation 外，所有失败/恢复相关记录必须同时保存 `generation_id` 和明确的因果链接字段，链接到直接触发它的前一个 `FailureEvent` 或 `ExecutionAttempt`；不得仅靠时间顺序或自然语言推断关联关系。这个约束在 `v0.17` 冻结，以保证 `v0.21` 无需迁移或扩展底层模型即可回放。

```text
ExecutionGeneration
- generation_id
- opened_by_attempt_id: optional (initial generation is empty)
- opened_by_failure_id: optional
- opened_by_recovery_id: optional
- open_reason: task_start | possible_effect | recovery

ExecutionAttempt
- attempt_id
- pre_generation_id
- generation_id
- caused_by_failure_id: optional
- caused_by_attempt_id: optional
- tool
- arguments_hash
- outcome: succeeded | failed | denied | timeout | invalid
- duration_ms
- effect_class: none | possible
- handler_admitted: bool
- exit_code: optional
- error_kind: optional
- output_excerpt: bounded executor output
- failure_id: optional

FailureEvent
- failure_id
- generation_id
- phase: execute | verify | recover
- category: protocol | permission | transient | deterministic | validation | unknown
- cause_hint: optional model diagnosis
- retryable: bool
- affected_files
- caused_by_attempt_id: required

RecoveryAction
- recovery_id
- generation_id
- action: retry | adjust | ask | block (v0.19 adds rollback)
- reason
- caused_by_failure_id: required
- requested_attempt: optional (retry only)
- requested_tool / requested_arguments_hash / redacted_arguments: optional (adjust only)
- checkpoint_id: optional (required by v0.19 rollback)
- status: proposed | reserved | executed | rejected | terminal
- result_generation_id: optional (required once the action is accepted)
- result_attempt: optional execution-level result reference
```

除任务启动的初始 generation 外，`ExecutionGeneration` 必须有且只有一个 `opened_by_*` 因果前驱；由失败恢复开启的 generation 以 `opened_by_recovery_id` 为准。因失败或恢复触发的 `ExecutionAttempt` 必须有且只有一个 `caused_by_*` 因果前驱；普通初始执行 attempt 可为空。

字段所有权：

- ToolRegistry 声明 `effect_class`；Executor 产生结构化 `ExecutionResult`（权限决定、handler 是否获准、结果、耗时、退出码、超时和错误种类），不能只把 `ok` 布尔值或格式化字符串回调给 State。Runtime 将其写成 `ExecutionAttempt`，并在 handler 前完成 generation 预留。
- Runtime 根据结果产生 `FailureEvent` 的基础分类、计数和因果关联关系，并创建不可变的 `ExecutionGeneration` 开启记录。`retry` 或 `adjust` 所产生的 attempt 必须回链到触发它的 failure；恢复 action 必须回链到触发它的 failure，不能只保留展示性文字。
- 模型可以提供 `cause_hint`、`reason` 和 `adjust` 的参数，但不能伪造执行结果、选择 `result_attempt` 或清除失败记录。原始已校验参数仅保留在当前任务的私有 attempt 存储中，供 retry 使用，不渲染到 Context。
- Verify 继续由现有验证证据机制负责；其证据必须绑定 `generation_id`，通过后才能解除 `verification_required`。

## 5. 版本切片

### 5.1 `v0.17` Failure Model

目标：让每次失败都可观察、可关联、可计数。

主要工作：

1. 在 `AgentState` 中加入 generation、失败事件、执行尝试和恢复计数的结构化状态。
2. 为 ToolRegistry 加入 `effect_class`，并把 Executor 回调升级为结构化 `ExecutionResult`；保留完整工具回灌内容的既有协议，但 State 只能依据结构化字段分类。
3. 在 handler 前原子预留 `possible` 调用的 generation，限制并发只用于全只读回合，并拒绝与可能副作用同回合的 verification call。
4. 把权限拒绝、超时、非零退出、参数/协议错误和验证失败映射为基础分类。
5. 用 canonical arguments hash 为 `failure_id`、参数 fingerprint 和任务级动作维护原子预算计数。
6. 固定 `blocked` / `failed` 判定与状态转换，不实现自动恢复动作。
7. 在 Context 的 Structured State 中渲染最近失败、当前 generation、剩余预算和待处理 recovery notice；参数只显示 hash 与脱敏摘要。

验收重点：同一失败可以被稳定关联；失败与恢复可沿显式因果链接回溯；失败不会被普通 history trimming 丢失；获准但失败的可能副作用同样会使旧验证证据失效；并发的只读回合不会改变记录顺序或 generation 语义。

### 5.2 `v0.18` Recovery Policy

目标：在不提供 rollback 的前提下，支持有限且可解释的恢复选择。

动作集合只有：`retry`、`adjust`、`ask`、`block`。

- 模型通过新增、受 JSON Schema 校验的 `recover` 控制工具提交动作；它不是独立 planner，也不绕过普通工具调用。`recover` 必须带 `caused_by_failure_id`、`reason` 和 action 所需引用或参数。
- 每个通过 schema、预算和权限检查而被接受的 action，都在 State 锁内预留一个后继 generation 并写入 `result_generation_id`。`retry`/`adjust` 的实际工具调用复用该 generation；`ask`/`block` 也保留该 state-only generation，使旧验证证据不能被复用。
- `retry`：仅用于可重试失败，必须精确引用原 `attempt_id`，并在 State 锁内预留对应预算后，使用当前任务私有 attempt 存储中的原始参数重新经 PermissionGate 执行。
- `adjust`：模型基于失败事实提交目标工具和新参数；Runtime 校验参数、记录其 canonical hash 与脱敏摘要、原子预留预算后，再将该调用经 PermissionGate 执行。必要时通过 recovery notice 触发 Todo 调整。
- `ask`：需要用户授权、缺失信息或外部条件时暂停执行。
- `block`：没有安全恢复路径时保守收口。

`v0.18` 明确不接受 `rollback` 作为 action 值。验证失败且模型判断必须撤销已有副作用时，系统只能记录原因并进入 `blocked`，不能模拟回滚成功。

### 5.3 `v0.19` Checkpoint / Rollback

目标：为可追踪的文件修改提供有限、可审计的恢复能力。

- checkpoint 的作用域固定为**一次成功或失败的单文件 `write_file`/`edit_file` 调用**，不尝试提供多文件事务。创建 checkpoint 时在 handler 前捕获同一真实路径的前镜像：规范化后的工作区内相对路径、`absent | regular-file` 类型、原始字节、mode、前镜像 SHA-256、创建 generation 与 attempt ID。路径解析到工作区外、符号链接、非普通文件、超过 `MAX_CHECKPOINT_BYTES` 的文件一律不支持 checkpoint，调用仍可执行但不可 rollback。
- 每个 checkpoint 只绑定一个文件和一次写入；写入结束后记录预期的后镜像 SHA-256。原文件不存在时以 `absent` tombstone 表示，rollback 的目标是删除这次创建的文件。
- `v0.19` 将 `rollback` 加入 `recover` action schema，且该 action 必须引用一个 checkpoint ID。Runtime 据此调用不直接暴露给模型的 `rollback_checkpoint` 工具；该工具仍必须经 PermissionGate 按目标路径授权，模型不能提供替换内容。执行前必须验证当前文件类型和 digest 仍等于该 checkpoint 的后镜像，否则判为外部改变并拒绝写入。
- 恢复使用同目录临时文件加原子 replace；对于 `absent` tombstone，只能删除 digest 仍匹配后镜像的普通文件。restore 的任一步骤失败都记录为副作用未知、保留 checkpoint，并进入 `blocked`，不能声称已经回滚。
- rollback 仅对上述 checkpoint 文件承诺恢复；任意 shell、网络或外部服务副作用不承诺可回滚。每次 rollback 都是 `possible` 调用：在 handler 前预留新 generation、清除旧验证证据，并要求下一 LLM 回合的独立 Verify。
- checkpoint 缺失、文件已被外部改变或副作用范围未知时，动作结果为失败并按 `blocked` / `failed` 规则收口。

### 5.4 `v0.20` Repair Loop

目标：把失败分类、诊断、恢复和验证串成完整的有上限循环。

```text
failure
  -> FailureEvent
  -> model diagnosis / RecoveryAction
  -> execute recovery
  -> new generation
  -> independent verification
  -> continue, next failure, blocked, or failed
```

该版本只复用阶段五已有的 LLM 回合和 Todo 工具，不创建第二个 planner。每个周期必须能回答：失败事实是什么、选择动作的理由是什么、动作执行结果是什么、验证证据是否属于当前 generation。任何已接受的恢复动作都必须进入新 generation；旧 generation 的验证证据不得复用于 `done` 判定。

### 5.5 `v0.21` Trace & Replay

目标：支持按 generation 回放一次任务的完整决策链：计划 → 执行 → 失败 → 分类/诊断 → 恢复 → 再验证 → 终态。

本版是阶段六的验收和调试收尾，不引入新的执行、恢复、诊断或状态转换逻辑。它只查询并展示 `v0.17`–`v0.20` 已产生的 `ExecutionGeneration`、`ExecutionAttempt`、`FailureEvent`、`RecoveryAction`、Todo/计划变更、验证证据和终态；回放不得补写、推测或修复缺失的运行时事实。

回放视图按 generation 顺序呈现状态转换，并以 schema 中冻结的因果链接连接每条记录到其触发的 attempt 或 failure。每个节点必须能显示原始、已脱敏的执行/验证证据及其来源，且明确标出证据所属 generation；终态必须显示为 `continue`、`done`、`blocked` 或 `failed` 及其最后依据。

验收重点：对 `v0.17`–`v0.20` 产生的至少一次真实失败—恢复案例，能够完整回放每个 generation 的状态转换，以及每个分类、诊断、恢复和终态所依据的原始证据。该案例应能直接用于核验 `v0.18` 的恢复策略与预算、`v0.19` 的 rollback 边界和 `v0.20` 的验证证据隔离是否按预期生效。

## 6. 测试与验收

### 6.1 单元测试

- generation 在写文件、execution shell、验证、权限拒绝后的变化符合 D1。
- 获准但抛出异常、超时或只写入部分内容的 `possible` 调用仍预留 generation 并使验证失效；拒绝和 handler 前参数错误不会。
- 含副作用的同轮 tool calls 按原顺序串行提交；混入 verification 的调用被回灌为 `invalid`，不执行 handler。
- `ExecutionGeneration`、attempt、failure、recovery 和验证证据始终带 generation_id；除初始 generation 外，失败/恢复记录的因果链接完整且可机器追溯。
- Executor 的结构化结果能无字符串解析地表达权限拒绝、handler 异常、超时、退出码和耗时；canonical arguments hash 对键序稳定且 Context 不泄露原始敏感参数。
- failure_id、参数 fingerprint、动作总数和 repair cycle 超限时不再执行隐藏重试。
- `retry` 精确复用其引用 attempt 的参数；`adjust`、预算预留、PermissionGate 执行和 `result_attempt` 关联均可独立测试。
- `RecoveryAction.result_attempt` 不会被当作 Verify 证据；验证失败仍保持 `verification_required`。
- `blocked` / `failed` 判定表覆盖每种输入，状态转换可重复测试。
- `v0.18` 的 action schema 拒绝 `rollback`。
- checkpoint 覆盖既有文件、新建文件、外部修改、符号链接/工作区外路径、大小上限和 restore 中断；失败恢复不会把任务表述为已回滚。
- Trace & Replay 能仅依赖冻结 schema 的 generation 与因果链接重建一条失败—恢复—验证链；缺失链接或跨 generation 复用验证证据会被明确标为不可验收，而不是由展示层猜测补全。

### 6.2 阶段级 E2E 场景

1. 命令临时失败，有限重试后成功并通过最终验证。
2. 参数错误，模型调整参数后成功。
3. 修改后测试失败，模型根据输出二次修复并重新验证通过。
4. 权限拒绝或连续失败，最终以明确原因进入 `blocked` 或 `failed`，没有死循环。
5. `v0.19` 中单文件 checkpoint 回滚后，旧验证证据失效；外部修改或 restore 中断时进入 blocked；新验证通过才能继续。
6. 至少一次上下文压缩后，generation、失败事件、剩余预算和 recovery notice 仍然准确。
7. 对一次真实失败—恢复案例，按 generation 回放计划、attempt、failure、诊断、recovery、验证证据和终态；每一状态转换都能定位其原始、已脱敏证据与因果前驱。

### 6.3 阶段完成定义

- [ ] `v0.17`–`v0.21` 各有独立教程、变更记录和可运行测试。
- [ ] 所有失败都能关联到执行尝试和 generation；没有仅靠自然语言字符串驱动的隐藏状态。
- [ ] 每次恢复动作都受 PermissionGate 和计数预算约束，并开启新的 Execution Generation；旧 generation 的验证证据不可复用于 `done` 判定。
- [ ] rollback 只在 `v0.19` 及以后对明确 checkpoint 的副作用可用。
- [ ] Repair Loop 能在成功、继续修复、阻塞和失败四种结果间正确收口。
- [ ] v0.21 能用冻结的结构化数据回放至少一个真实失败—恢复案例，展示每个 generation 的状态转换与原始证据。
- [ ] 默认测试套件、教程检查和阶段级 E2E 全部通过，运行时仍只有标准库。

阶段六完成后，mini_agent 的完成标准不再只是“工具调用过且测试曾经通过”，而是：**失败有结构化事实，恢复有明确边界，结果有属于当前 generation 的新验证证据；这些事实还能按 generation 完整回放，以验证可靠性机制真的按预期工作；无法继续时也能准确说明是 blocked 还是 failed。**
