# 阶段九：会话持久化与恢复（Session Persistence & Resume）实施计划

> 状态：v0.30、v0.31 已完成；v0.32–v0.33 待实施
> 前置阶段：阶段六可靠执行（`v0.17`–`v0.21`）、阶段七结构化计划（`v0.22`–`v0.25`）与阶段八进程管理（`v0.26`–`v0.29`）
> 建议版本范围：`v0.30`–`v0.33`

v0.30 首次写入 schema 1；v0.31 的新写入格式为 `schema_version=2`，保留 `writer_version`、规范化 `workspace_root`、UTC `saved_at`、`save_kind`、`handoff_status`、`state`、`context`、工作区清单和 `integrity={"algorithm":"sha256","sha256":"..."}`。schema 1 仍可读取诊断，但只能恢复 schema 2。用户在当前任务输入 `/save` 后显式开启持久化，首次生成随机 `session_id`，文件写入 `~/.mini_agent/sessions/<session_id>.json`；后续安全点自动更新同一文件。`active` 表示最后一次保存是可诊断的完整安全点，`clean` 只在正常退出或任务切换完成有界进程清理后提交。v0.31 增加 `--resume`，但只接受 `clean` 安全点并在提交 active 占用后等待用户输入，不自动调用 LLM。

## 1. 目标与定位

当前任务事实保存在 `AgentState`，消息历史和摘要由 `ContextManager` 持有，权限的运行时批准、文件检查点的前镜像字节、后台进程句柄则分别留在各自的内存对象中。CLI 退出后，新 Python 进程无法继续同一任务。`AgentState.snapshot()` 虽然能供 Trace 只读回放，却不是完整的恢复格式：它不含全部私有计数、原始恢复参数、消息历史或检查点字节。

阶段九要回答的问题是：**在不重复执行不确定副作用、不断开工具调用协议、也不沿用过期验证证据的前提下，Agent 能否跨 Python 进程继续同一任务？**

目标流程：

```text
当前任务 → 完整安全点 → 持久化 session → CLI 退出
                                     ↓
新 CLI → 校验 session / 工作区 → 重建 State 与 Context → 重新授权 → 继续任务

工具调用 → 持久记录 pending → 执行 → 持久提交结果与 State / history
                       ↓ 异常中断
                检测未完成调用 → 判断副作用是否不确定 → 调查 / 验证 / 用户决定
```

四个版本依次解决数据表示、安全恢复、工具边界和异常中断。核心实现仅使用标准库，不把存储文件当成模型可修改的事实来源。

## 2. 范围与非目标

### 2.1 本阶段范围

- 为单个本地任务建立带版本号、完整性信息和工作区身份的私有 session 格式；持久化任务事实、协议消息、上下文摘要及恢复所需的内部状态。
- 从完整安全点跨进程恢复，重新发现项目指令、构造当前 system prompt 和 PermissionGate，并在继续前检查工作区变化。
- 在每个模型工具调用的执行边界记录 durable pending / committed 事实；同一 assistant 回合的全部 `role=tool` 结果仍按原顺序回灌。
- 对崩溃留下的 incomplete invocation 区分“尚未进入 handler”与“可能已发生副作用”，禁止自动重放后者。
- 将恢复、状态不确定和验证失效的来源保留为可审计的 Runtime 事实；Trace 仍只读消费已加载的当前任务 State。
- 保存、加载和异常报告不得泄露 `write_process.input` 正文或本地 LLM 配置密钥。

### 2.2 本阶段不做

- 不跨进程重连 `Popen`、管道、输出收集器或 stdin 写入线程；不凭保存的 PID 重新获得进程控制权。
- 不保证在崩溃、断电或 `SIGKILL` 后自动清理外部子进程；发现可能遗留时明确报告并阻止直接完成。
- 不自动重放 `write_file`、`edit_file`、`run_shell`、进程启动/控制/写入或恢复动作；也不承诺 shell 副作用回滚。
- 不持久化 `once` / `always` 的运行时授权作为下一进程的预批准；计划 revision 的用户批准是任务事实，不能替代工具授权。
- 不做云同步、多设备共享、多 CLI 同时写一个 session、后台守护调度、通用事务数据库或跨版本任意迁移。
- 不使用 `pickle` 等可执行反序列化格式，不把 `config_local.py`、API key、进程 stdin 正文或检查点私有字节复制进公开 State / Trace。

## 3. 先冻结的设计边界

### D1：session 是恢复格式，`snapshot()` 仍是回放视图

新增独立的 `SessionStore` 与显式的 `AgentState.export_session()` / `restore_session()` 一类边界。恢复格式包含 State 的权威记录、计数器、预算、修复阶段、当前 generation、计划审批事实和协议消息；`active_plan`、`todos`、`current_goal`、剩余额度等投影应重算并交叉校验，不能从存储文件直接覆盖只读投影。锁、handler、PermissionGate、`ProcessManager`、`Popen` 和线程不属于可序列化数据。

建议的顶层 envelope：

```text
schema_version             # 格式版本，不等同于包版本
session_id                 # 随机、不复用；与任务内 task_id 分开
writer_version
workspace_identity         # 规范化工作区位置及必要身份信息
saved_at, save_kind        # safe_point | tool_boundary
handoff_status            # active | clean；v0.31 只加载 clean
state                     # 权威任务事实及恢复用私有计数
context                   # 经脱敏的消息历史、摘要、裁剪位置
tool_boundary?            # v0.32 起的未完成 / 已提交调用记录
integrity                 # 覆盖规范化 payload 的校验值
```

`session_id` 标识磁盘上的会话，`task_id` 继续标识任务内进程及执行事实；不能因新 CLI 的本地计数从 1 开始而复用旧会话身份。读入时先限大小、解析并校验 schema、字段类型、枚举值、ID 唯一性、引用关系、计数器单调性、工具调用与结果配对，再构造运行时对象。未知 schema 版本、内容损坏或断链时拒绝进入执行；可以显示诊断，但不猜测缺失事实，也不把损坏文件加载成“近似可继续”的任务。完整性校验用于发现意外损坏，不把普通哈希当作防恶意篡改的认证。

### D2：存储提交原子化，单写者且默认私有

Session 文件置于用户私有的数据目录，路径不得由模型工具调用直接指定；不写进仓库、Git 索引或 `config_local.py`。目录与文件尽可能使用仅当前用户可读写权限，无法满足最小私有性时拒绝持久化并解释原因。写入采用同目录临时文件、写入并 `fsync`、`os.replace`，在平台允许时同步目录；恢复只承认完整提交的文件。保存失败不能覆盖上一份可用提交，也不能把失败的保存标记为已完成。

每个 session 同时只允许一个 CLI 写入，使用标准库能实现的独占锁或创建标记，并处理异常遗留锁的人工核查路径；不能因为时间久就擅自抢占另一个可能仍运行的进程。对单份 session 设置大小上限，限制历史消息和日志增长；不能通过截断半个工具回合来满足上限。配置值、任意用户文本和工具输出可能含敏感信息，应在操作手册明确本地文件权限与保留位置。

### D3：v0.30 / v0.31 只认完整安全点

安全点至少满足：本轮 assistant 的每个 tool call 均已有对应 `role=tool` 结果，State 与 history 已一起提交；没有正在执行的 handler、预留未结算的 attempt、在途 stdin 写入或活动后台进程；进程清理结果已经同步进 State。`awaiting_approval`、`exploring`、`blocked` 和尚待验证的执行任务可作为安全点，但恢复后仍受原状态机约束。`awaiting_process` 且进程仍运行不是可安全恢复点。安全点本身还不证明 CLI 后来正常退出：打开会话后应先持久标记为 `active`，只有正常交接完成才标记为 `clean`。v0.31 只恢复 `clean` 会话。

正常退出时先按阶段八规则有界清理本任务进程，再保存清理后的状态并提交 `clean` 标记；清理不完整则保留旧任务并报告具体进程，不能保存为“可直接继续”。崩溃发生在两个安全点之间时，v0.30 / v0.31 可检查最后一份完整安全点，但必须拒绝以它直接续跑：它之后可能有未记录操作，退回文件版本不会撤销工作区变化。v0.32 / v0.33 才用 durable tool boundary 缩小并处理这段不确定窗口。

### D4：恢复重建运行时，不复用旧权限和旧验证

新进程从当前工作区重新发现 `AGENTS.md`，重建受保护 system prompt、ToolRegistry、PermissionGate 和 ContextManager；持久化的旧 system prompt 不能覆盖新发现的项目指令。用户通过 CLI 做过的计划批准、驳回和 blocked 恢复保留为 State 事实，但当前调用仍须经过计划阶段、Repair Loop 与新 PermissionGate。运行时 `always` 批准默认不跨进程继承，`once` 从不继承。

恢复前检查规范化工作区身份及任务涉及路径的文件类型、内容哈希和可用性；Git 状态只能作为补充，不能代替对未跟踪文件和工作区外路径的检查。改变或无法检查的文件应明确列出，不推断原因。即使检查结果一致，进程退出到恢复之间的外部变化也无法穷举；恢复总是开启后继 generation，使旧 `verification_evidence` 不再用于完成判定，`verification_history` 和 Trace 仍保留旧证据供审计。变化需要诊断或重规划时沿现有受限入口处理，不能直接重置预算或绕过 `exploring` 只读边界。

文件检查点目前只在内存 `CheckpointStore` 中保存前镜像字节，公开 `snapshot()` 只有元数据。首版不持久化这些私有字节；恢复后相关 checkpoint 必须标记为不可回滚并保留原因，不能从元数据伪造 `ready`。若将来需要跨进程 rollback，应另设私有字节存储、冲突检查和独立验收。

### D5：工具边界先持久记录，再允许 handler 运行

v0.32 为每个 tool call 建立稳定 `invocation_id`，关联 assistant 回合、原始 `tool_call_id`、工具名、有效参数摘要、`effect_class`、权限结果、handler 准入状态、attempt / generation、提交序号。工具参数及结果的存储遵循脱敏规则；尤其 `write_process.input` 不能进入 session、journal、摘要、错误诊断或终端。读取历史中的该调用时保留 `tool_call_id` 和合法 JSON 参数形状，用明确占位说明输入已脱敏，不得把占位文本重新送到 stdin。

权限拒绝和 handler 前的参数/schema 拒绝已有确定结果；不能把它们归为“副作用可能发生”。`commit_plan` 等计划控制工具的校验可能在 handler 内完成，失败虽只回灌 `plan_rejected` 且不推进 generation，也必须记录其 handler 边界。在授权与全部前置校验完成、即将进入 handler 之前，必须把 `handler_admitted` / `pending` 持久提交；若提交失败就不得进入 handler。获准的 `run_shell` 无论 `purpose` 为 execution 还是 verification 都按 `possible`，计划控制工具虽不改变工作区也会改变任务 State，不能被“只读命令”分类掩盖。副作用调用按现有顺序执行；可并发的 `none` 调用仍在模型顺序提交结果。

handler 返回后，将 `ExecutionResult`、对应 State 事实和该 call 的 `role=tool` 内容作为同一 durable commit，不能出现“磁盘上结果已成功、State 未记账”或“State 已记账、模型协议缺结果”。下一次 LLM 请求前还须确认同轮全部 call 均已提交并保持原顺序；拒绝、异常和终态中的其余 call 也必须各有结果。进程自然退出等异步事实在同步点单独持久提交，不伪造成第二个工具回复。对无法在单文件提交中原子覆盖的 journal 与 session，应通过提交序号和校验链证明哪一份是权威记录。

### D6：崩溃后的不确定性是一等事实

v0.33 扫描最后提交序号后的 pending invocation。若明确尚未进入 handler，可安全生成拒绝/未执行结果或重新走正常授权入口；若已进入 `effect_class=none` 的 handler，只有能证明结果未产生副作用且调用仍被当前状态允许时才考虑重试，默认先调查。凡已进入 `possible` handler、进程可能仍活着、stdin 可能部分写入或 rollback 可能部分生效，均标为 `uncertain_side_effect`，不自动重放，也不把超时、进程消失或文件看似未变当作“肯定没执行”。

不确定事件引用原 invocation / attempt / generation，并使旧验证失效；运行时给出具体下一步：只读调查、必要的计划修订或恢复入口、独立 verification，以及无法确定外部效果时的用户决定。既有 FailureEvent / RecoveryAction 不得被虚构或覆盖。若当前 Repair Loop 与新的不确定事实冲突，保留两边因果，按现有保守阻塞路径交给用户；不得悄悄清除 active failure 或跨代套用验证证据。

### D7：存储层不改变 Trace、Plan 与权限语义

`/trace` 继续只读消费当前进程已加载的 State 快照；打开 session 是 CLI 的显式恢复动作，不属于 Trace 查询。恢复事件可进入 append-only 轨迹供回放，但 Trace 自身不得打开 session 文件、调用 LLM、执行工具、授权、修改 history 或推进 generation。`commit_plan` 的校验失败仍只返回 `plan_rejected`；保存协议不能让无效计划获得 generation 或验证证据。用户的计划决定只由 CLI 记录，模型不能借加载文件伪造批准。

## 4. Session 协议与状态模型

### 4.1 CLI 与文件入口

建议 `v0.30` 提供显式保存与可发现的 `session_id`，`v0.31` 提供 `--resume <session_id>`（或等价的 CLI 入口）；具体命令名在实现前以现有 `--plan`、`/new`、`/reset` 交互方式冻结。默认启动新任务不能暗中加载上一份 session。保存失败、版本不兼容、工作区不一致、非 `clean` 会话和未完成调用分别显示不同诊断；失败的加载不修改当前运行时。

存储内容分三类：

| 类别 | 例子 | 恢复规则 |
|---|---|---|
| 权威任务事实 | PlanRevision、进度、generation、attempt、failure、审批决定、预算计数 | 严格校验引用并恢复；派生投影重算。 |
| 协议上下文 | user / assistant / tool 消息、摘要、裁剪位置 | 保留完整 tool-call/result 配对；敏感正文先脱敏。 |
| 易失资源 | 权限运行时批准、`Popen`、线程、管道、检查点前镜像字节、当前验证通过资格 | 重新创建或明确失效；不得假装已恢复。 |

### 4.2 工具调用持久状态

```text
InvocationRecord
- invocation_id, session_id, task_id, round_id, tool_call_id, model_order
- tool, effect_class, permission, handler_admitted
- attempt_id?, generation_id?, status: pending | committed | uncertain
- result_commit_id?, redacted_arguments, diagnostic_reason?

RoundCommit
- round_id, assistant_message_id, ordered_invocation_ids
- ordered_tool_result_ids, state_commit_id
- status: incomplete | complete
```

`pending` 只表示持久化边界已建立，不表示 handler 一定开始；必须另有准入事实才能判定不确定范围。`committed` 要求结果、State 和 history 一起可读；整轮 `complete` 要求每个调用都有唯一结果，才允许下一次模型请求。持久文件中的脱敏参数不能充当重试所需的原始参数；用户明确继续时也应重新提供或由当前受限恢复协议取得必要输入。

## 5. 版本切片

### 5.1 `v0.30` Session Persistence（已完成）

目标：建立可验证、可原子保存的会话表示；此版不跨进程继续执行。

主要工作：

1. 定义 schema 1 的 session envelope、私有目录、随机 ID、大小限制、原子写入、`active/clean` 交接标记与完整性校验；明确不支持的旧/未来版本如何拒绝。
2. 为 State 增加显式导出与校验接口，涵盖恢复必须的私有计数和因果记录；公开 `snapshot()` 与 Trace 行为保持不变。
3. 持久化脱敏后的消息历史、摘要、裁剪位置；保留每个 assistant tool call 与 `role=tool` 的协议关联，绝不保存 `write_process.input` 正文。
4. 只在完整安全点保存；对活动进程、在途写入、半轮工具结果和保存失败给出明确反馈。正常退出先执行既有进程清理，再写最后安全点与 `clean` 标记。
5. 添加往返、损坏文件、非法引用、大小上限、写入中断与隐私边界测试；此版读取仅作校验和检查，不启动 Agent 续跑。

验收重点：同一安全点保存后读回的权威事实与协议关联等价；临时写入中断后旧提交仍可读；无法保存时不声称 session 已持久化；敏感 stdin 文本不出现在文件字节中。

### 5.2 `v0.31` Safe Resume

状态：已完成。

目标：从完整安全点在新 Python 进程中继续原任务，仍不承诺恢复中断中的工具调用。

主要工作：

1. 增加显式 resume 入口；只接受 `clean` 安全点，先校验文件和工作区，再构造 State、Context、Registry、ProcessManager 与 PermissionGate。加载失败不得污染当前任务。
2. 重新发现项目指令，重建 protected messages；不继承上个进程的 `once` / `always` 工具授权，保留 CLI 已记录的计划决定。
3. 对任务涉及路径做工作区一致性检查，报告外部变化和无法检查的路径；恢复后开启后继 generation，保留旧 verification history，要求新的独立验证。
4. 将没有前镜像字节的 checkpoint 标记为不可回滚；对旧进程元数据标记不可控制，不接受旧 `process_id` 或 PID 的操作。活动进程会话不得直接进入 safe resume。
5. 测试 Direct Path、`exploring`、`awaiting_approval`、待验证计划和 blocked 任务的恢复准入；计划批准后仍须经 PermissionGate。

验收重点：正常退出 CLI、启动新 CLI、显式加载并继续同一任务；原计划、预算和消息协议未丢失，旧验证不能完成任务；工作区变化、`active` 或不完整 session 不会触发 LLM 或工具。

### 5.3 `v0.32` Durable Tool Boundaries

目标：把模型工具调用的“准备执行”和“结果已进入协议”分别变成耐久事实。

主要工作：

1. 为 assistant 回合及每个 tool call 分配稳定 ID，在 handler 前提交 pending / handler-admitted 边界；提交失败阻止 handler。
2. 将每个 `ExecutionResult`、State 更新和对应的 `role=tool` 消息一起提交，并以整轮 complete 标记约束下一次 LLM 请求。
3. 保持现有 `effect_class=none/possible`、`run_shell=possible`、计划工具校验失败和副作用调用顺序；并发只读调用按模型顺序形成 durable 结果。
4. 对权限拒绝、参数错误、handler 异常、进程异步退出和整轮中途异常分别测试落盘状态；所有路径保持每 call 一个结果。
5. 验证脱敏 journal 不保存 stdin 正文，且损坏或缺一条结果时不会继续请求模型。

验收重点：在各提交位置模拟进程中断，能准确区分最后完整回合、未入 handler 和已入 handler 的调用；任何持久状态都不会声称半轮结果是完整协议。

### 5.4 `v0.33` Crash Recovery

目标：识别崩溃后的不完整调用，并以保守、可解释的路径恢复工作。

主要工作：

1. 加载 journal 并核对 session 的提交序号；将 incomplete invocation 分类为未执行、确定结果待补齐或效果不确定，保留证据与原因。
2. 对已进入可能副作用 handler 的调用禁止自动 replay；为工作区和外部进程状态提供只读调查提示，不把 PID 当成控制权。
3. 把不确定效果接入 generation、验证失效、Repair Loop、Plan Contract 与 CLI 用户交接；必要时阻止完成或标记 blocked，保留原 failure / recovery 因果链。
4. 在用户决定继续后重新走当前计划与 PermissionGate；独立 verification 通过前不能使用旧证据收口。
5. 用受控子进程在 handler 前、文件写入后未提交结果、shell 命令中、进程启动后、stdin 写入途中等位置模拟崩溃；确认没有盲目重试或重复副作用。

验收重点：同一崩溃只生成一次不确定事件；模型不会收到伪造的成功工具结果；用户能看出哪些事实已确认、哪些仍待调查，以及为什么暂不能完成。

## 6. 与现有状态机的组合

| 原状态 / 场景 | 恢复后的入口 | 不变量 |
|---|---|---|
| Direct Path，完整安全点 | 原任务继续 | 沿用原完成条件；恢复后的旧验证不算通过。 |
| `exploring` 或 `--plan` | 继续只读调查 | 不允许因恢复而运行副作用或 verification。 |
| `awaiting_approval` | 等待用户对当前 revision 决定 | 不能因文件中存在计划而自动批准。 |
| `diagnosis_required` / `verification_required` | 原修复阶段加恢复后的验证义务 | 不重置 repair cycle 或伪造 RecoveryAction。 |
| `awaiting_process` 且仍有活动资源 | v0.31 拒绝 safe resume；v0.33 标记可能遗留 | 不把旧 PID 当作已登记句柄，不直接 `done`。 |
| incomplete `possible` invocation | `uncertain_side_effect` 调查 / 用户交接 | 不自动重放，不复用旧验证。 |
| `done` / `blocked` / `failed` | 保留终态供查看；只能按既有 CLI 规则开启后续动作 | 加载不是解除终态的权限捷径。 |

恢复本身是 Runtime 事件，不是模型工具调用；只有下一次真实模型回复及工具结果才能推进任务。`MAX_ITERATIONS` 仍限制一次 agent loop，不应因为加载历史轮数而立刻耗尽，也不能通过反复 resume 重置已持久化的任务级预算。计划 revision、进度事件与用户决定按原有不可变/追加语义恢复。

## 7. 测试与验收

### 7.1 单元与集成测试

- **格式与原子性**：schema 升级/拒绝、规范化 JSON、哈希不符、截断文件、重复 ID、断链、大小上限、临时文件中断、旧提交保留、同时写入拒绝。
- **完整性**：State 权威字段、私有计数、计划投影、预算、verification history、Trace 引用及消息摘要往返等价；损坏数据不会部分加载。
- **协议**：多个并发 `none` 调用及串行 `possible` 调用都按模型顺序提交；每个 tool call 恰有一个结果；半轮不能发起下一次 LLM 请求。
- **恢复准入**：重新加载指令与权限；工作区身份、未跟踪文件及任务涉及路径变化；旧验证、旧 checkpoint 和旧进程控制权均按规则失效。
- **隐私**：真实格式的 `write_process.input` 出现在原始 assistant tool call 中时，session 与 journal 的原始字节、错误消息和终端输出均不含正文；`config_local.py` 密钥不进入会话文件。
- **崩溃矩阵**：pending 之前、pending 之后但 handler 之前、handler 中、结果已产生但尚未提交、部分工具结果提交、完整回合后分别中断；只在有确定事实时恢复确定状态。
- **状态机**：Plan Mode、Repair Loop、进程清理、完成提醒、verification generation、Trace 只读性质及 CLI 显式用户决定保持原约束。

用标准库测试子进程和临时工作区模拟真正的新 Python 进程；故障注入点由测试控制，不依赖定时碰运气。平台文件同步和进程清理差异须用能力检测及明确的测试预期表达。

### 7.2 阶段级 E2E

1. 执行含计划与文件修改的任务，在完整安全点退出；新 CLI 显式 resume，看到原计划、已完成步骤和上下文摘要，重新验证后完成。
2. 在 `--plan` 提交后退出；新 CLI 仍停在当前 revision 审批，不自动执行工具；批准后还需单独授权写入。
3. 保存含 stdin 写入调用的历史；检查文件字节无输入正文，恢复后工具结果仍与 call ID 正确配对。
4. 在文件 handler 修改完成、结果提交之前杀掉运行时；恢复显示不确定副作用，先调查文件，再由既有修复/重规划入口决定后续动作，不重复写入。
5. 在 `run_shell(purpose="verification")` 中断；恢复不能把它当只读或通过证据，必须重新独立验证。
6. 在后台进程或 stdin 写入在途时异常退出；恢复不重连旧 PID、不宣称清理成功，显示具体遗留风险并阻止直接 `done`。
7. 修改工作区的已跟踪与未跟踪任务文件后恢复；显示差异，旧验证失效，Trace 仍能回放旧事实但不推测变化原因。

### 7.3 阶段完成定义

- [ ] `v0.30`–`v0.33` 各有独立、可教学的行为与测试，且保存/恢复入口在操作手册中可复现。
- [ ] 完整安全点可跨进程恢复任务、计划、消息协议和任务级预算；旧权限与验证资格不会被继承。
- [ ] 中断中的工具调用有耐久、可核对的边界；任何未确认副作用都不会被自动重放或伪装成成功。
- [ ] 后台进程、stdin、checkpoint 和工作区外部变化的不可恢复部分均有明确状态与用户可执行的后续路径。
- [ ] Trace 继续只读，Plan Contract、PermissionGate、Repair Loop、generation 和完整 `role=tool` 回灌规则不被持久化层绕过。
- [ ] 完整测试、教程检查、README 检查与阶段级 E2E 通过；核心运行时仅依赖标准库。
- [ ] 各版本 tag 仅由用户手动创建，助手不执行任何 tag 操作。

## 8. 版本依赖关系

```text
v0.29 后台进程与有界管道 stdin
  ↓
v0.30 schema、私有存储、原子安全点、数据校验
  ↓
v0.31 显式加载、运行时重建、工作区检查、旧证据失效
  ↓
v0.32 handler 前后持久边界、单 call 提交、整轮协议提交
  ↓
v0.33 incomplete invocation、未知副作用、保守恢复与用户交接
```

`v0.30` 不把“能读文件”宣传为“能继续任务”；`v0.31` 只接受完整安全点；`v0.32` 不把落盘 pending 当成 handler 已执行；`v0.33` 不根据命令文本或文件表面状态猜测副作用不存在。每版新增一个主要概念，并单独验收其能力边界。

## 9. 文档与发布同步

实现各版时同步对应教程、`docs/tutorials/README.md`、`docs/operation/manual.md`、`CHANGELOG.md`、`README.md` 学习路径、`pyproject.toml` 版本信息和本计划状态。教程先解释“为什么上一进程的记忆不等于可继续执行”，再介绍 schema、保存点、工具边界和未知副作用；术语首次出现时按教程作者规范就近解释。主 README 的中文阶段名与版本主题遵守主 README 编写规范。只有新增的运行时硬约束需要全仓遵守时才更新 `AGENTS.md`。

每版交付前运行：

```bash
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python scripts/check_tutorials.py
PYTHONPATH=src python scripts/check_readme.py
```

教程完成且用户手动创建对应 tag 后，再运行依赖本地 Git 对象的事实检查。助手不得创建、移动、覆盖、删除或推送 tag。

## 10. 阶段完成后的能力边界

阶段九完成后，mini_agent 可以从已验证的本地 session 继续任务，并能识别崩溃发生在工具调用边界时哪些结果确定、哪些副作用仍需调查。它不提供进程重连、无损恢复任意外部副作用或无人值守的自动重试；无法证明安全时，任务停在可解释、可审计的交接状态。
