# 阶段十：受控子代理委派（Controlled Subagent Delegation）实施计划

> 状态：`v0.34` 已实现；`v0.35`–`v0.37` 待实施
> 建议版本范围：`v0.34`–`v0.37`
> 能力前置：阶段七结构化计划（`v0.22`–`v0.25`）、阶段八任务与进程边界（`v0.26`–`v0.29`）、阶段九的安全恢复与持久化工具边界（`v0.30`–`v0.32`）
> 关联计划：`adaptive-planning-plan.md`、`process-management-plan.md`、`session-persistence-resume-plan.md`

`v0.33 Crash Recovery` 不是只读 Subagent 的硬前置。`v0.34`–`v0.36` 的子代理不修改文件、不执行 shell、不启动或控制进程，也不调用其他子代理；父进程在子代理运行中崩溃时，最坏结果是未提交的调查结果丢失和一次额外的模型成本，而不是重复工作区副作用。`v0.37` 开始持久化委派生命周期时，才必须与 `v0.33` 的 incomplete invocation、恢复分类和用户交接语义对齐。若阶段九按路线先完成 `v0.33`，阶段十直接复用其边界；若并行推进，则 `v0.37` 在合并前以 `v0.33` 的最终协议为准。

## 1. 目标与定位

当前 `mini_agent` 只有一个执行上下文。它可以规划、调用多个工具并并发执行同轮的只读调用，但所有调查、判断、修改和验证仍由同一个模型上下文承担。复杂项目任务中，不相关的调查材料会共同占用父上下文，多个独立问题也无法被分配给有明确范围和预算的工作单元。

阶段十要回答的问题是：**怎样把认知调查分给多个受控子代理，同时继续由父 Agent 独占执行权、权限边界、主计划和完成判定？**

一句话目标：**把单 Agent Runtime 扩展为一棵有父子关系、能力边界、预算和结果合同的执行树；委派认知工作，但不分散工作区修改所有权。**

目标流程：

```text
用户任务
   ↓
父 Agent：维护主 Plan、PermissionGate、执行与完成判定
   ├── 委派调查 A ──→ 只读 Subagent A ──→ 结构化结果 A ──┐
   ├── 委派调查 B ──→ 只读 Subagent B ──→ 结构化结果 B ──┼→ 父 Agent 综合
   └── 委派审阅 C ──→ 只读 Subagent C ──→ 结构化结果 C ──┘       ↓
                                                        父 Agent 修改
                                                            ↓
                                                        独立验证
                                                            ↓
                                                        用户回复
```

四个版本依次解决最小委派合同、生命周期与预算、有界并行，以及可审计的持久交付。第一版即提供完整 Task / Result Contract，不先暴露一个只有自由文本参数的临时工具。

## 2. 范围与非目标

### 2.1 本阶段范围

- 父 Agent 通过显式 `delegate_task` 工具创建一个范围清晰的调查任务。
- 子代理拥有独立的 State、Context、循环控制和提示词，只接收项目指令、委派合同及父 Agent 明确选择的事实。
- 不复制工具定义；共享不可变工具定义，通过只读 `FilteredToolRegistryView` 暴露显式允许的能力子集。
- 定义可校验、有大小上限的 `DelegatedTask`、`SubagentResult`、`Finding`、`EvidenceRef` 和 `UsageRecord`。
- 建立单子代理预算和父任务聚合预算，约束轮次、模型调用、工具调用、token、结果大小、墙钟时间、子代理总数和并发数。
- 将执行结果与结果交付分开：子代理先产生 `result_ready`，只有结果已进入父 State 和对应父 Context 后才是 `committed`。
- `v0.36` 支持多个只读子代理有界并行；实际完成可以乱序，父工具结果仍按模型调用顺序提交。
- `v0.37` 将委派状态、结果摘要、Trace 和 session 恢复连接起来，并与 Crash Recovery 的不完整调用分类对齐。

### 2.2 本阶段不做

- 不允许子代理写文件、执行 shell、启动/观察/控制父任务进程、写 stdin、创建 checkpoint、执行 recovery 或 verification。
- 不允许子代理修改父 Plan、推进父步骤、创建父 FailureEvent、改变 generation、批准权限、解除终态或决定父任务完成。
- 不允许子代理继续委派；阶段十最大深度固定为 `1`。
- 不把父 Agent 的完整 system prompt、完整 history、PermissionGate 或运行时 `once` / `always` 授权复制给子代理。
- 不做多个子代理并行修改、工作树隔离、变更所有权、合并、冲突解决、跨代理 rollback 或分布式事务。
- 不做跨主机 worker、常驻代理池、消息总线、云端任务队列或无人值守后台调度。
- 不把“角色讨论”“投票”或多数意见当作正确性证明；父 Agent 必须自行综合并验证。
- 核心运行时不引入第三方依赖；线程、计时、取消、序列化和并发控制继续使用标准库。

## 3. 先冻结的架构决策

### D1：`v0.33` 是持久委派的协议依赖，不是只读委派的功能门槛

`v0.34`–`v0.36` 的子代理只有可重复的认知调查能力。父进程异常中断后，不尝试从内存恢复运行中的子代理，也不声称已收到其结果；重新调查会增加成本，但不会重复工作区副作用。因此这些版本可以建立在 `v0.31` Safe Resume 和 `v0.32` Durable Tool Boundaries 之上。

`v0.37` 必须处理“子代理已产生结果，但父 Context 尚未提交”的窗口，并识别运行中、结果待交付和已经提交三种事实。此时要么先完成 `v0.33`，要么在合并时显式采用它的 crash record、issue、decision 与 incomplete invocation 分类，不能另造一套恢复语义。

### D2：父 Agent 独占执行所有权和最终权威

父 Agent 是唯一面向用户、持有主 Plan、请求副作用权限、修改工作区、运行 authoritative verification 并决定任务是否完成的主体。子代理只提供调查发现和来源证据。

```text
Delegation = 分散认知工作
Execution ownership = 集中在父 Agent
```

子代理成功不能自动推进父计划。父 Agent 必须读取结果、判断其相关性，并通过现有计划工具明确更新步骤。子代理失败也不自动产生父 FailureEvent；只有父任务真实执行或验证失败时，才沿既有 Failure Model 和 Repair Loop 记录事实。

### D3：隔离可变运行时，共享不可变工具定义

每个子代理必须拥有独立的：

- `AgentState`
- `ContextManager`
- runtime notice、轮次计数和停滞计数
- 委派预算计数与取消信号
- 子代理专用 system prompt

工具的名称、schema、描述和无状态 handler 定义不需要复制。建议在 `ToolRegistry` 上增加只读过滤视图：

```python
subagent_registry = registry.filtered_for_subagent(
    allowed={"calculate", "read_file", "list_dir", "grep"},
)
```

过滤视图不能注册、替换或修改底层 Tool，也不能暴露绑定父 State、ProcessManager、CheckpointStore 或 RecoveryRuntime 的 handler。若现有 `Tool` 仍是可变对象，视图只能返回不可变 schema 快照并将执行解析到冻结的允许项；不能把底层可变映射直接交给子运行时。

### D4：可委派能力必须显式声明，不能由 `effect_class=none` 推断

`effect_class` 描述工具是否可能产生执行副作用，不描述工具是否可以交给子代理。计划控制工具和进程观察工具可能属于 `none`，但它们会接触父任务状态或资源，因此不能自动进入子代理白名单。

工具定义增加独立的能力元数据或由集中策略维护明确集合，例如：

```text
delegation_capability: unavailable | readonly_workspace | pure_compute
```

首版仅允许：

- `calculate`
- `read_file`
- `list_dir`
- `grep`

即使父模型在合同中请求其他工具，Runtime 也只取请求集合与系统白名单的交集；空集合、未知工具或越权请求在创建子代理前拒绝。项目指令和父计划都不能放宽该策略。

### D5：子代理使用专用身份与最小上下文，不复制父 Context

子代理 system prompt 明确其职责：调查被委派的问题、收集证据并按合同报告；禁止修改、继续委派、改变父计划、决定父任务完成或声称 authoritative verification。

子上下文由以下部分组成：

```text
Subagent Identity & Rules
+ 当前重新发现的 Project Instructions
+ Delegation Contract
+ Selected Parent Facts
+ 子代理自己的 Structured State
+ 子代理自己的 Recent Messages
```

`Selected Parent Facts` 只能包含父 Agent 显式提供且通过大小与敏感信息检查的任务事实，不默认复制完整 history、工具输出、运行时授权、隐藏提示词或其他子代理上下文。项目指令仍是受保护内容；工具返回和父 Agent 提供的材料都是不可信数据，不能覆盖 system rules。

### D6：Task / Result Contract 从 `v0.34` 起就是正式协议

`delegate_task` 不接受一个含义不明的自由文本字符串作为全部语义。模型提供 goal、scope、constraints、期望 findings 和预算请求；Runtime 分配 ID、裁剪预算并冻结最终合同。

子代理结果必须通过 schema 校验后才进入 `result_ready`。结果包含明确 outcome、摘要、结构化 findings、证据、限制和实际用量。缺字段、越界路径、悬空 evidence、超长结果或非法状态不会被当作成功结果；Runtime 可在剩余预算内给子代理一次受保护的格式修正机会，之后返回确定的失败结果。

### D7：子代理证据可以支持调查结论，但不是父任务的权威完成证据

子代理可以提供证据，例如某个定义的文件位置、某个测试覆盖的行为或一次只读工具观察。这些证据进入 `SubagentResult.findings[*].evidence`，帮助父 Agent 核查结论。

```text
Subagent evidence
        ≠
Parent authoritative completion evidence
```

子代理证据不写入父 `verification_evidence`，也不能满足修改后的完成条件。特别是子代理文本中声称“测试通过”不构成父任务 verification；阶段十的子代理根本不获得 `run_shell`。父 Agent 必须通过自己的只读复查或独立 verification 建立权威事实。

### D8：预算分为单子代理预算和父任务聚合预算

单子代理预算至少包含：

```text
max_rounds
max_llm_calls
max_tool_calls
max_tokens
max_result_bytes
timeout_seconds
```

父任务聚合预算至少包含：

```text
max_subagents
max_concurrency
max_total_llm_calls
max_total_tool_calls
max_total_tokens
```

所有预算必须为 Runtime 权威计数，模型只能请求不超过剩余额度的子预算。并行启动前原子预留额度，结束后按确定的实际用量结算并释放未使用的预留；失败、超时和取消同样计入已经消耗的调用与 token。不得通过重复失败委派、恢复 session 或拆分相同任务重置聚合预算。

token 统计优先使用服务商返回的 usage；服务商未提供时，使用 `ContextManager` 已有的保守估算方法记录 estimated usage，并在结果中标明来源。每次 LLM 请求前检查剩余额度，响应完成后结算；若服务商支持可靠的输出上限则同时下发剩余额度，但不能把服务商是否遵守上限作为唯一保护。

### D9：执行 outcome 与交付状态是两个正交维度

委派交付生命周期：

```text
created → running → result_ready → committed
              │            ↑
              └─ 任何执行 outcome 均形成有界结果 ─┘
```

结果的执行 outcome 独立表示：

```text
completed | failed | timed_out | cancelled | budget_exhausted
```

因此失败、超时、取消和预算耗尽也必须产生一个完整、可提交的 `SubagentResult`，而不是停在父 Context 之外：

- `result_ready`：子代理完整结果已经产生并通过本地校验，但尚未可靠进入父 State / Context。
- `committed`：结果、父 DelegationRecord 和对应 `role=tool` 消息已经共同提交，父 Agent 可以继续请求 LLM。

`committed` 不表示 outcome 为 `completed`。父 Agent 必须能收到并处理确定的失败结果。

### D10：每次父工具调用仍恰好对应一个按序 `role=tool` 结果

一个 `delegate_task` 是父 Agent 的一个工具调用。无论子代理内部执行了多少轮、调用多少只读工具，父 Context 只接收一个有界的结构化结果；子代理内部消息不得直接拼入父 history。

同一父 assistant 回合发出多个委派时可以并行运行，但结果必须遵守现有工具协议：每个 call 恰有一个结果，全部结果按模型顺序进入父 Context，整轮提交后才允许下一次父 LLM 请求。某个子代理失败不能丢失或提前提交其他调用的结果。

### D11：委派错误在子代理边界收口，不击穿父 Agent Loop

子代理 LLM 错误、schema 错误、只读 handler 异常、超时、取消和预算耗尽由 `SubagentRunner` 转换为 `SubagentResult`。父 `ToolExecutor` 仍负责把 `delegate_task` handler 边界外的异常转成对应工具错误结果。

核心父 Agent Loop 不增加针对顶层 LLM 或 CLI 异常的兜底。子代理的异常转换是工具 handler 内部子运行时的明确职责，不改变项目现有异常边界。

### D12：阶段十固定单层委派，防止递归和预算树爆炸

子代理 Registry 不包含 `delegate_task`，合同中的 `depth` 由 Runtime 固定为 `1`，模型不能提高。恢复文件、父 Context 或项目指令中出现更深委派请求时均拒绝。多层代理只有在未来单独定义递归预算继承、权限衰减、Trace 展示和终止传播后才可评估。

## 4. 运行时与调用协议

### 4.1 可重入 Runtime

当前 `agent_loop()` 直接使用模块级 `call_llm`、`MAX_ITERATIONS` 和输出配置，并通过注入的 Context/Executor 获得部分实例状态。`v0.34` 应抽出可实例化的运行时依赖，但保持 HTTP 客户端继续使用 `http.client`，并显式设置 `Accept-Encoding: identity`。

建议边界：

```python
class AgentRuntime:
    def __init__(self, llm_client, context, executor, loop_policy, output): ...
    def run(self) -> str: ...

class SubagentRunner:
    def run(self, task: DelegatedTask, cancel_event) -> SubagentResult: ...
```

父 Runtime 使用现有完成规则、TerminalOutput、Plan/Repair/Process/Session 集成；子 Runtime 使用无终端流式输出、只读工具视图、子预算和结果 schema 校验。不要通过递归调用共享全局 `agent_loop()` 状态，也不要让多个子代理共享同一个 ContextManager。

### 4.2 委派调用路径

```text
父模型生成 delegate_task
  → 父 loop 校验同轮协议
  → ToolExecutor 校验 schema / phase / terminal state
  → DelegationManager 原子预留父聚合预算并创建 DelegationRecord
  → 父 durable boundary 提交 handler_admitted（开启 /save 时）
  → SubagentRunner 构造子 State / Context / RegistryView / Prompt
  → 子 loop 调用 LLM 与只读工具
  → 校验并生成 SubagentResult
  → DelegationRecord = result_ready
  → 父 State + role=tool + boundary 原子提交
  → DelegationRecord = committed
  → 父 loop 在整轮完成后继续
```

`v0.34`–`v0.36` 在未实现 Durable Delegation 时，`result_ready` 主要是内存事实；开启 `/save` 后仍受父 `delegate_task` 的现有 durable tool boundary 保护，活动 handler 不能成为 clean safe point。`v0.37` 才承诺跨进程识别和协调子结果交付状态。

### 4.3 子代理完成协议

子代理仍遵守“无 `tool_calls` 才能结束”以及每个工具调用必须有对应 `role=tool` 结果的核心协议。最终纯文本必须满足 Subagent Result JSON schema；非法结果不会被静默当作 summary。

建议通过子 Runtime 的 completion policy 校验最终内容：第一次非法时注入一次受保护格式提醒并继续；第二次仍非法或预算不足时生成 `outcome=failed` 的 Runtime 结果。不能新增一个调用后立即中止、缺少 `role=tool` 回灌的特殊终止工具。

## 5. 数据模型

### 5.1 委派任务合同

```text
DelegatedTask
- delegation_id                 # Runtime 分配，父任务内唯一
- subagent_id                   # Runtime 分配，不与 task_id 混用
- parent_task_id
- parent_generation_id          # 创建时父 generation，仅作来源引用
- goal                          # 单一、可回答的调查目标
- scope                         # 允许调查的工作区相对路径或主题边界
- constraints                   # 有界字符串列表
- expected_findings             # 父 Agent 希望回答的问题列表
- requested_tools               # 模型请求；Runtime 与白名单取交集
- allowed_tools                 # Runtime 冻结后的实际能力
- selected_parent_facts         # 有界、脱敏、明确选择的父事实
- budget                        # Runtime 批准后的单子代理预算
- depth = 1
- created_at
```

约束：

- `goal` 必须非空且只表达一个主要调查目标。
- `scope` 规范化到当前 workspace；禁止 `..` 逃逸、符号链接越界和任意绝对路径。
- `constraints`、`expected_findings`、父事实、工具数量及单项长度均有上限。
- `allowed_tools` 只能由 Runtime 计算，不能从持久化输入直接覆盖。
- 合同创建后不可变；取消、结果和交付状态写入独立记录。

### 5.2 结构化证据与发现

```text
EvidenceRef
- evidence_id
- kind: file_location | tool_observation
- claim
- path?                         # workspace 相对路径
- line?                         # 正整数；只能表示观察位置，不保证内容未变化
- tool?                         # 仅允许子代理白名单工具
- observation_hash?             # 有界结果的稳定摘要，不复制大段输出

Finding
- finding_id
- claim
- evidence_ids: list[evidence_id]
- confidence: observed | inferred
- caveat?
```

`file_location` 在结果生成时校验路径位于 scope 内、行号合法，但父 Agent 后续仍应在需要时复读，因为工作区可能变化。`inferred` 必须带 caveat 或支持证据，不能伪装成直接观察。证据文本和 hash 不是 PermissionGate 或 verification 事实。

### 5.3 子代理结果合同

```text
SubagentResult
- result_id
- delegation_id
- subagent_id
- outcome: completed | failed | timed_out | cancelled | budget_exhausted
- summary
- findings: list[Finding]
- evidence: list[EvidenceRef]
- limitations: list[string]
- usage: UsageRecord
- started_at, finished_at
- error_kind?, error_detail?
```

```text
UsageRecord
- rounds
- llm_calls
- tool_calls
- input_tokens
- output_tokens
- token_accounting: provider | estimated | mixed
- elapsed_ms
- result_bytes
```

结果上限按 UTF-8 bytes 校验。超限时优先要求子代理在剩余预算内重新生成更短结果；仍超限则返回 `failed/result_too_large`，不能在 JSON 中间截断。错误详情必须有界且不包含 API key、完整 system prompt 或未选择的父 history。

### 5.4 父任务中的委派记录

```text
DelegationRecord
- delegation_id, subagent_id, parent_task_id, parent_generation_id
- task_contract_hash
- delivery_status: created | running | result_ready | committed
- outcome: pending | completed | failed | timed_out | cancelled | budget_exhausted
- result_id?
- result_hash?
- usage
- created_at, started_at?, result_ready_at?, committed_at?
- cancellation_reason?
- diagnostic_reason?
```

父 State 保存合同的有界摘要、结果摘要和引用，不保存完整子 Context。完整结果通过对应的父 `role=tool` 内容进入 Context，并受现有历史裁剪与大小限制；Trace 使用 State 中的结构化摘要，不反向解析自然语言消息。

不变量：

- `delegation_id`、`subagent_id` 和 `result_id` 在父任务内唯一。
- delivery status 只能单向推进；`committed` 必须有且只有一个 result。
- outcome 从 `pending` 变为最终值后不可改写。
- `result_ready` 必须引用通过 schema 校验的 result hash。
- `committed` 必须与父 Context 中对应 tool call/result、模型顺序和 durable commit 一致。
- 任意执行 outcome 都必须形成一个结果；失败路径不能没有父工具结果。
- 活动委派数量、并发数和实际 usage 不能超过父聚合预算。

## 6. 状态、上下文与所有权

### 6.1 字段所有权

| 信息 | 写入者 | 父 Agent 是否可直接覆盖 |
|---|---|---|
| 委派 goal、scope、constraints、期望发现 | 父模型经 `delegate_task` 提供，Runtime 校验 | 不能修改已冻结合同，只能新建委派 |
| ID、实际 allowed tools、预算预留、交付状态 | `DelegationManager` | 否 |
| 子工具执行事实与子 history | 子 `ToolExecutor` / `ContextManager` | 否，不合并到父 State |
| findings、evidence、limitations | 子模型提供，子 Runtime 校验 | 父可评价或复查，不能篡改原结果 |
| 父 Plan 进度、generation、verification | 父 Runtime 与现有工具 | 子代理无写入口 |
| 用户批准与 PermissionGate | CLI / 父 PermissionGate | 子代理无继承或写入口 |
| result commit 与协议关联 | 父 Runtime / SessionBoundary | 模型不能伪造 |

### 6.2 父 Structured State

父 Context 只渲染有界的当前委派视图：

```text
delegations
- aggregate budget used / remaining
- active delegation IDs and goals
- recently committed outcomes and short summaries
- cancellation or delivery diagnostic
```

不重复渲染完整 findings 和 evidence；它们已经存在于对应 tool result。compaction 后需要保留仍影响主任务的有界委派摘要和结果引用，不能把未 committed 的结果描述成父 Agent 已收到。

### 6.3 子 Structured State

子 State 只记录自己的目标、只读工具历史、预算、格式修正提醒和终态。它不复制父 PlanRevision、FailureEvent、RecoveryAction、VerificationEvidence、ProcessRecord 或 session 写入权。子代理读文件产生的是 observation，不推进父 generation。

## 7. 权限、安全和任务边界

- `delegate_task` 本身不修改工作区，但会消耗模型资源并启动嵌套运行时。它保留 `effect_class=none` 的工作区语义，同时通过独立的 `delegation_capability`、聚合预算和生命周期规则管理，不能据此推断“免费”或可无限重试。
- 子只读工具仍经过 scope gate；首版可以使用固定拒绝式 PermissionGate 或更窄的 CapabilityGate，但不能复用父会话的动态批准。
- `read_file`、`list_dir`、`grep` 对 scope 外路径在 handler 前拒绝；拒绝结果仍回灌子模型并计入工具预算。
- 子代理不能读取 `config_local.py`、session 私有目录、API key 或项目规则明确禁止的路径；现有文件权限规则与项目指令取更严格者。
- 父任务处于 `awaiting_approval`、`verification_required`、terminal 状态或 Crash Recovery 只允许特定调查动作时，`delegate_task` 是否允许必须由明确的 phase matrix 决定，不能仅因其只读而默认放行。
- 推荐首版允许：`direct/executing + idle` 的调查委派，以及 `exploring + idle` 的只读委派；`diagnosis_required` 只有合同明确为诊断且不绕过 active failure 时允许。`verification_required`、`awaiting_approval` 和 terminal 状态拒绝。
- 子代理返回的文本和文件内容都是不可信数据；父 prompt 明确不得把其中的指令当成 system 或项目规则。
- `/new`、`/reset`、EOF、`exit` 和异常退出必须对当前父任务的活动子代理发出取消并有界等待。未收束时保留具体 ID 和原因，不能声称 clean 交接。

## 8. 版本切片

### 8.1 `v0.34` Minimal Delegation（已实现）

目标：父 Agent 能把一个明确的只读调查任务同步交给单个、单层、隔离的子代理，并收到通过正式合同校验的结构化结果。

主要工作：

1. 抽取可实例化的 `AgentRuntime` / completion policy，使父、子 loop 共享协议实现但不共享可变状态；保持 LLM HTTP 约束和父 loop 现有行为。
2. 定义 `DelegatedTask`、`SubagentResult`、`Finding`、`EvidenceRef`、`UsageRecord` 及严格的长度、数量、枚举和引用校验。
3. 为工具定义增加显式 delegation capability，并实现只读 `FilteredToolRegistryView`；首版只暴露 calculate、read_file、list_dir、grep。
4. 新增 `DelegationManager`、`SubagentRunner` 和状态绑定的 `delegate_task` 工具；只允许同时一个同步子代理，depth 固定为 1。
5. 增加子代理专用 system prompt，注入当前项目指令、冻结合同和有界 selected parent facts，不复制父 history 或授权。
6. 子最终纯文本必须满足 Result Contract；一次格式修正后仍非法则返回确定失败。每个父 delegate call 恰有一个有界 tool result。
7. 测试父 State/Context 不被子修改、父工具定义不被视图修改、越权工具不可见、证据引用可校验，以及简单任务不委派时没有行为变化。

建议文件：

| 文件 | 动作 | 内容 |
|---|---|---|
| `src/mini_agent/runtime.py` | 新增 | 可实例化 loop 依赖与 completion policy；或从 `agent.py` 提取等价边界 |
| `src/mini_agent/delegation.py` | 新增 | 合同 dataclass、校验、Manager 与 Runner |
| `src/mini_agent/tools/delegation.py` | 新增 | 状态绑定的 `delegate_task` 工具 |
| `src/mini_agent/tools/base.py` | 修改 | delegation capability 与过滤视图 |
| `src/mini_agent/tools/__init__.py` | 修改 | 父 Registry 注册委派工具；子视图禁止递归 |
| `src/mini_agent/prompt.py` | 修改 | 父委派规则与子代理专用 prompt |
| `src/mini_agent/agent.py` | 修改 | 复用实例 Runtime，保持父协议兼容 |
| `tests/test_subagent_v034.py` | 新增 | 合同、隔离、只读能力和单次委派 E2E |

验收重点：子代理能回答一个跨文件调查问题并提供结构化位置证据；它看不到任何写、shell、进程、计划、恢复、验证或委派工具；其结果不自动推进父 Plan 或父 verification。

本次实现固定为单个同步子代理；可配置预算只允许请求更小额度，未实现父任务聚合预算、后台取消、多子代理并行、持久化 DelegationRecord 或跨 session 恢复。后续 `v0.35` 接手生命周期、可配置/聚合预算和取消，`v0.36` 接手有界并行，`v0.37` 接手持久化交付与 crash recovery 协调。

实施状态：

- [x] `v0.34`：单个、同步、单层、只读委派与 Task / Result Contract。
- [ ] `v0.35`：生命周期、取消和聚合预算。
- [ ] `v0.36`：有界并行和按父顺序提交。
- [ ] `v0.37`：持久委派、Trace、session 与 Crash Recovery 协调。

### 8.2 `v0.35` Lifecycle & Budget

目标：把一次可用委派升级为有任务身份、可取消、不会无限消耗资源的受控生命周期。

主要工作：

1. 在父 State 增加 `DelegationRecord`、聚合预算和原子状态转换；区分 delivery status 与 execution outcome。
2. 实现单子代理的 rounds、LLM calls、tool calls、tokens、result bytes 和 timeout 预算，并在每个边界前检查、返回明确的 `budget_exhausted`。
3. 实现父任务的 max subagents、total LLM calls、total tool calls、total tokens 预算；创建前预留，完成后按实际用量结算。
4. 增加标准库 `Event` 驱动的协作式取消和有界等待；取消不能杀死正在进行的 HTTP/socket 调用，但下一同步点必须停止，超时后保守报告未收束。
5. 活动委派阻止父任务 `done`、clean save、`/new`、`/reset` 和正常退出，直到结果提交或完成取消清理。
6. 将委派摘要和剩余预算注入父 Structured State；compaction 后保持准确且不复制完整子 history。
7. 将重复相同合同、无新 findings 的连续委派接入停滞判断；新 ID 或预算消耗本身不算进展。

建议新增或修改：`state.py`、`context.py`、`config.py`、`delegation.py`、`__main__.py`、`tests/test_subagent_lifecycle_v035.py`。

验收重点：超时、取消、预算耗尽、非法结果和 LLM 异常都形成唯一结果并到达父 Context；重复委派不能重置聚合预算或通过创建新 ID 制造进展。

### 8.3 `v0.36` Parallel Delegation

目标：允许多个互不依赖的只读调查并行执行，同时保持预算、隔离和父工具协议确定性。

主要工作：

1. 增加专用 `DelegationScheduler` 和固定 `max_concurrency`，不要直接让每个父 tool-call 线程各自创建无上限线程池。
2. 同一父 assistant 回合的多个 `delegate_task` 在原子预留聚合预算后并行启动；无法获得预算的调用在启动子 LLM 前返回确定拒绝结果。
3. 每个子代理拥有独立可变 Runtime；仅共享冻结工具定义、LLM client 配置和只读项目指令快照。
4. 完成顺序可以不同，父 State / Context / durable tool result 仍按模型 call 顺序提交；等待较早调用时继续有界收集后续结果，但不提前回灌。
5. 支持 partial failure：一个子代理失败、超时或取消不取消其他子代理，除非父任务整体取消或聚合预算/安全边界要求停止。
6. 聚合 usage 线程安全结算；预留与释放不会超发 token、工具或并发额度。
7. 父任务终止传播到全部活动子代理，并报告未在期限内收束的 subagent ID。

建议新增或修改：`delegation.py`、`agent.py`、`state.py`、`config.py`、`tests/test_parallel_subagents_v036.py`。

验收重点：至少三个调查任务乱序完成，父模型按原调用顺序收到三个唯一结果；部分失败不丢失成功结果；实测活动数和总 usage 从不超过配置上限。

### 8.4 `v0.37` Durable Delegation

目标：让委派创建、子结果产生和父结果提交成为可审计、可恢复协调的事实，并接入 Trace 与 session。

主要工作：

1. 持久化有界 DelegationRecord、冻结合同摘要、usage、result hash 和 `created/running/result_ready/committed` 状态；不保存完整子 Context、隐藏 prompt 或未选择父 history。
2. 在子结果通过校验后先提交 `result_ready`；随后将结果、父 State 和对应 `role=tool` 一起提交为 `committed`，保持父 round 的模型顺序。
3. 将活动或未交付委派纳入 safe-point 检查。`running` 不能保存 clean；`result_ready` 只有在能从持久结果重建准确父 tool result 时才可协调提交。
4. 与 `v0.33` 对齐恢复分类：运行中但无结果的只读子任务可标记为调查丢失，不伪造成功；重新运行需要重新结算成本和当前 phase/budget。已有 durable `result_ready` 时优先提交原结果，不再次调用子 LLM。
5. 恢复时重新计算 allowed tools 和当前项目指令；旧合同不能放宽新策略。若 scope、项目指令或工作区身份变化，保留旧结果供审计，但由父 Agent 决定复查。
6. Trace 只读展示父子关系、合同摘要、生命周期、outcome、usage 和结果提交引用；不加载完整子 history，不调用 LLM，不重放委派。
7. 在 `running → result_ready`、`result_ready → committed`、同轮多个结果提交和 session 替换前后注入崩溃，验证不会重复交付、遗漏结果或断开父 tool-call 协议。

建议新增或修改：`session.py`、`resume.py`、`state.py`、`context.py`、`trace.py`、`delegation.py`、`tests/test_durable_delegation_v037.py`。

验收重点：子代理已经形成结果但父进程在 Context 提交前崩溃时，恢复能够识别并提交同一 result；运行中崩溃不会伪造结果或自动重复计费调查；每个父 tool call 最终最多一个 committed result。

## 9. 与现有状态机的组合

### 9.1 Planning / Repair / Crash Recovery 准入矩阵

| 父状态 | 是否允许新委派 | 说明 |
|---|---|---|
| `direct` / `executing` + `idle` | 允许 | 普通调查或审阅；不能替代父执行和验证 |
| `exploring` + `idle` | 允许 | 只读调查符合 Explore 边界；`commit_plan` 仍须独占父工具回合 |
| `awaiting_approval` | 拒绝 | 模型应等待用户决定，不能继续启动工作 |
| `diagnosis_required` | 条件允许 | 合同必须是只读诊断；不能推进旧 Plan 或调用 recover |
| `verification_required` | 拒绝 | 下一父工具回合仍只能是现有单个独立 verification |
| Crash Recovery 有 unresolved issue | 条件允许 | 仅在现有恢复规则允许只读调查且委派结果可关联当前 issue 时允许；用户决定不能由子代理产生 |
| `blocked` / `failed` / `done` | 拒绝 | 子代理不能解除或覆盖父终态 |

整轮准入和单调用执行器都要检查上述边界。若同轮混有必须独占的计划、恢复或 verification 调用，则按现有更严格规则拒绝整轮相关调用；委派不能成为绕过独占性的包装器。

### 9.2 完成判定

父任务不能完成的新增条件：

- 存在 `created`、`running` 或 `result_ready` 但未 committed 的委派。
- 某个父 `delegate_task` 尚无对应 `role=tool` 结果。
- 聚合 usage 或交付状态尚未结算。
- 任务修改后缺少父 Runtime 产生的当前 generation authoritative verification。

全部子代理 outcome 为 `completed` 也不会自动满足父 Plan success criteria。相反，某个子代理失败也不必然阻止父任务完成：父 Agent 可以自行调查或使用其他证据完成目标，但必须先收到并处理该 committed 失败结果。

### 9.3 停滞检测

新委派只有在带来当前 progress epoch 内首次出现的有效 finding/evidence hash，或父 Agent 因结果产生真实计划/状态进展时，才可视为候选推进。以下不算进展：

- 只改变 delegation/subagent/result ID。
- 相同合同重复委派并返回相同 findings。
- 失败、拒绝、超时或无 findings 的结果。
- 仅消耗预算或增加 Trace 记录。

停滞规则不得把不同但无业务价值的随机 summary 文本当作新事实；hash 基于规范化的结构化 finding/evidence，而不是完整自由文本。

## 10. 测试与验收

### 10.1 单元与集成测试

- Task Contract 拒绝空 goal、越界 scope、未知工具、超长字段、非法预算和 depth 不为 1。
- Result Contract 拒绝重复/悬空 evidence ID、越界路径、非法行号、未知 outcome、超长 JSON 和 usage 倒退。
- `FilteredToolRegistryView` 不能注册或修改底层工具；父工具更新后视图行为按冻结策略明确，子代理永远看不到状态绑定或副作用工具。
- `effect_class=none` 的计划控制和进程观察工具不会因副作用分类被错误放行。
- 子 State、Context、停滞计数和 history 完全隔离；子代理不能改变父 Plan、generation、verification、PermissionGate 或终态。
- 子 prompt 只包含项目指令、合同和 selected facts，不包含父完整 history、API key、运行时授权或其他子 Context。
- 子每个 tool call 都有对应结果；最终只有无 tool_calls 且通过 Result Contract 才能结束。
- LLM 异常、工具异常、格式错误、超时、取消和各类预算耗尽均转换为唯一有界结果。
- 单子代理和聚合预算均在并发下原子预留与结算；恢复、失败和重复委派不能重置已消耗额度。
- provider usage 缺失时标记 estimated；估算路径也能在下一请求前停止超预算循环。
- 多个子代理乱序完成时，父 `role=tool` 结果保持模型顺序；每 call 恰有一个结果，半轮不能请求父 LLM。
- partial failure 不丢失其他结果；父取消会传播，未收束任务保留 ID 和原因。
- `result_ready` 与 `committed` 单向转换；重复 commit 幂等拒绝或返回同一提交，不产生第二个父 tool result。
- compaction 后父委派摘要、活动状态、聚合预算和结果引用准确，完整子 Context 不进入父摘要。
- 父任务有活动/待提交委派时 completion reminder、clean save 和任务切换均拒绝收口。
- Explore、awaiting approval、diagnosis、verification、Crash Recovery 和 terminal 状态的准入符合矩阵。
- 子证据不会进入父 `verification_evidence`；父独立 verification 仍是修改后完成的必要条件。
- Trace 只消费父公开 snapshot，不读取子 history、session 或工具，也不重判 findings。
- 崩溃注入覆盖 created、running、result_ready、单结果 committed 和多结果半轮提交位置。
- Direct Path、普通工具并发、Plan/Repair、进程管理、v0.32 durable boundary 和 v0.33 recovery（存在时）没有回归。

### 10.2 阶段级 E2E 场景

1. 父 Agent 委派“定位认证入口”，子代理只读多个文件，返回带路径、行号和 claim 的 evidence；父 Agent 复读关键位置后制定方案。
2. 子代理尝试请求 `write_file`、`run_shell`、计划工具、进程工具或再次委派，工具 schema 不可见且 Runtime 在执行前拒绝伪造调用。
3. 父 Agent 同时委派“查实现”“查测试”“查文档约束”，三者乱序结束，父 Context 按调用顺序收到结构化结果并综合。
4. 一个子代理超时、一个结果格式非法、一个成功；三个父工具结果均存在，成功结果未丢失，聚合 usage 正确。
5. 多个子代理的最大预算之和超过父剩余预算，Scheduler 在启动前确定性拒绝超额任务，不产生额外 LLM 请求。
6. 父任务在子代理运行中收到 `/new` 或退出，取消并有界等待；清理不完整时旧任务保留且不能保存 clean。
7. 子代理声称某行为正确，父 Agent 未独立复查或验证时不能据此完成；父验证通过后才收口。
8. 相同合同连续返回相同 findings，不因新 ID 或新 summary 绕过停滞护栏；改变调查范围获得新证据后可继续。
9. 开启 `/save` 后，在子结果生成与父 Context 提交之间杀掉运行时；恢复识别 durable `result_ready` 并只提交一次原结果。
10. 在子代理仍运行时崩溃；恢复不伪造成功、不继承旧线程，也不把重新调查当作零成本自动 replay。
11. 至少发生一次父 Context compaction 和一次 session resume 后，委派关系、预算、结果引用和父 Plan/verification 边界保持准确。
12. `/trace` 能展示“父委派—子结果 ready—父结果 committed—父决策—执行—验证”的因果链，并对断链记录显示 unresolved。

### 10.3 阶段完成定义

- [ ] `v0.34`–`v0.37` 各有独立教程、变更记录、测试和可复现验收场景。
- [ ] Task / Result Contract 从第一个版本起就是结构化、可校验且有界的协议。
- [ ] 可变 Runtime 完全隔离；不可变工具定义共享；子能力通过显式策略过滤。
- [ ] 子代理始终只读、单层，不能修改父状态、权限、计划、generation 或 verification。
- [ ] 单子代理预算与父聚合预算在串行、并行、失败、取消和恢复路径上都不超发。
- [ ] 所有 execution outcome 都形成结果，并经历可审计的 `result_ready → committed` 交付。
- [ ] 多子代理可以有界并行且按父模型顺序提交，每个 tool call 恰有一个结果。
- [ ] 子 evidence 与父 authoritative completion evidence 明确分离；父仍独占修改和验证。
- [ ] 活动或未提交委派阻止完成、clean save 和不安全任务切换。
- [ ] Durable Delegation 与 v0.33 Crash Recovery 使用同一不确定事实和恢复语义，不自动伪造、重放或重复提交。
- [ ] Trace 只读、Plan Contract、PermissionGate、Repair Loop、generation、session 和完整工具结果协议均未被绕过。
- [ ] 默认测试、教程检查、README 检查与阶段级 E2E 全部通过；核心运行时仍只依赖标准库。
- [ ] 各版本 tag 仅由用户手动创建，助手不执行任何 tag 操作。

## 11. 版本依赖关系

```text
v0.25 Plan Contract / Trace
  + v0.29 任务进程生命周期
  + v0.31 Safe Resume
  + v0.32 Durable Tool Boundaries
                ↓
v0.34 Minimal Delegation
单个同步只读子代理、Runtime 隔离、Task / Result Contract、depth=1
                ↓
v0.35 Lifecycle & Budget
父子身份、交付状态、取消/超时、单体与聚合预算
                ↓
v0.36 Parallel Delegation
专用调度器、有界并发、结果顺序、partial failure
                ↓
v0.37 Durable Delegation ←→ v0.33 Crash Recovery
result_ready / committed、session、Trace、崩溃协调
```

不要在 `v0.34` 同时加入并行，不要在 `v0.35` 提前持久化运行中的子 Context，不要在 `v0.36` 允许子代理修改工作区，也不要在 `v0.37` 自动恢复旧线程或无条件重跑调查。每版只增加一个主要概念，使相邻 tag 的行为差异可教学。

## 12. 文档与发布同步

每个版本实现时同步：

- `README.md` 与 `README_EN.md` 的阶段十学习路径和当前版本
- `docs/tutorials/README.md` 的阶段十导航
- 对应版本教程：`34-minimal-delegation.md`、`35-subagent-lifecycle-budget.md`、`36-parallel-delegation.md`、`37-durable-delegation.md`
- `docs/operation/manual.md` 的委派工具、配置、输出、取消和恢复说明
- `CHANGELOG.md`
- `pyproject.toml` 版本信息
- `docs/plans/README.md` 与本计划的实施状态
- 真正新增全仓运行、授权或修改硬约束时更新 `AGENTS.md`

教程先解释“工具并发”和“子代理委派”的区别，再解释为什么隔离 State/Context、共享工具定义、过滤能力视图；随后引入 Task / Result Contract、两级预算、结果顺序和 durable delivery。首次出现 Subagent、delegation、result_ready、committed、aggregate budget 和 authoritative verification 时必须就近用直观中文解释，不能只列字段。

每版交付前运行：

```bash
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python scripts/check_tutorials.py
PYTHONPATH=src python scripts/check_readme.py
```

教程完成且用户手动创建相应 tag 后，再运行依赖本地 Git 对象的事实检查。助手不得创建、移动、覆盖、删除或推送 tag。

## 13. 阶段完成后的能力边界

阶段十完成后，`mini_agent` 可以把多个独立调查问题交给只读、有预算、单层的子代理，在隔离上下文中并行收集结构化发现和证据，并以确定顺序、耐久边界交还父 Agent。父 Agent 仍是唯一修改者、权限请求者、主计划维护者和完成判定者。

这个阶段建立的是“受控认知委派”，不是多代理共同写代码。若后续需要让子代理执行修改，应另设阶段，先解决 workspace/worktree 隔离、变更所有权、权限继承与衰减、冲突合并、跨代理 verification、rollback 和 durable execution；不能通过扩大 `allowed_tools` 偷渡这些能力。
