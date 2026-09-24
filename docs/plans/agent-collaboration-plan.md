# 阶段十三：轻量 Agent Collaboration 实施计划

> 状态：`v0.47`、`v0.48` 与 `v0.49` 已实现
> 建议版本范围：`v0.47` Agent Profiles / Roles、`v0.48` Background Subagent、`v0.49` Resumable Child Session
> 能力前置：阶段十受控子代理委派（`v0.34`–`v0.39`）、阶段九会话与崩溃恢复（`v0.30`–`v0.33`）、阶段十二本地 Skills（`v0.45`）
> 关联计划：`subagent-delegation-plan.md`、`session-persistence-resume-plan.md`、`mcp-skills-plan.md`

## 1. 目标与定位

阶段十已有单层只读 Subagent、独立 Context、共享 `AgentRuntime.run()`、多 provider、预算、取消、有界并行和持久结果交付。但现有 `delegate_task` 是同步工具：同一父工具回合内的子任务可以并行，父 Agent 仍要等全部结果按序提交后才能请求下一轮模型。`v0.39` 保存的是结果交付事实，不保存可继续对话的完整子 Context。

本阶段回答的问题是：**怎样让父 Agent 选择合适的只读角色，边做自己的工作边等待子任务，并在以后继续同一个子会话，同时保持现有工具协议、授权和恢复边界？**

当前阶段的目标流程：

```text
spawn(profile, task) → child_session_id + 已接受的启动结果
父 Agent 继续自己的工具回合和模型轮次
子任务完成 → CLI 通知 / 可查询的完成事件
get_result(child_session_id) → 本轮有界结构化结果
followup(child_session_id, task) → 同一子 Context 的下一轮工作
```

`v0.48` 已将交互语义冻结为 `spawn_subagent`、`get_subagent_status`、`get_subagent_result`、
`cancel_subagent`。保留现有 `delegate_task` 的同步行为作为兼容入口；不把它悄悄改成后台调用。
阶段十三只扩展认知协作，父 Agent 仍独占工作区修改、主 Plan、权限交互、权威验证和完成判定。
`v0.49` 已完成：父 Agent 只能在成功结果已领取后提交完整的新调查合同；空闲快照与父任务安全点原子保存，并在恢复时重新核对当前角色、模型与 Skill 身份。完成本阶段后，新增能力路线收口，阶段十四专注 Evaluation & Regression。

## 2. 范围与非目标

### 2.1 本阶段范围

- `v0.47` 增加具名子代理 profile：`explorer`、`reviewer`、`tester`、`general`。每个 profile 冻结角色提示、获准的本地模型别名、四工具白名单内的工具子集、权限策略和可用 Skill ID；允许受限的本地自定义 profile。
- `v0.48` 增加当前 CLI 进程内的后台子任务。父 Agent 在所有启动调用的唯一工具结果和整轮边界提交后继续工作；子任务在有界并发、预算和取消边界内运行。
- 后台任务完成时产生可审计的状态事件和 CLI 通知。父 Agent 通过显式工具查询状态并领取有界结果；通知只携带身份和状态，不隐式注入完整子结果。
- `v0.49` 为已收束的子任务保存有界、可校验的子会话快照；父 Agent 可按 `child_session_id` 在同一父任务中追加后续任务，跨一次安全保存与恢复仍能续接。
- 同一子会话可有多个顺序执行的回合；每回合独立结果和 usage，累计预算与父任务聚合预算持续生效。

### 2.2 本阶段不做

- 不给子代理写文件、运行 shell、操作进程、执行测试、操作父 Plan/State、创建 verification evidence、调用 MCP Tool/Resource/Prompt 或再次委派的能力；深度固定为 1。
- 不做 Agent 之间的 message bus、共享 Task Graph、自主抢任务、递归 Team、协商投票、分布式调度或跨任务常驻 worker。
- 不做父进程退出后继续运行的后台 worker，也不自动重启中断的子模型请求。`v0.49` 的跨进程续接只针对已收束、已保存的子会话。
- 不把 profile 提示词或 Skill 正文当成授权；不让子结果代替父 Agent 的独立复查与验证。
- 不改变现有 `delegate_task` 的一调用一结果、同轮按模型顺序提交行为。

## 3. 先冻结的架构决策

### D1：Profile 是受限配置，不是新的权限主体

Profile 用稳定 ID 描述角色、用途、提示词、模型别名、工具子集、权限和 Skill ID。内置 profile 的语义必须与真实能力相符：`explorer` 查位置和关联，`reviewer` 只读审阅，`tester` 分析测试覆盖与建议验证命令但不能声称已运行测试，`general` 做未专门化的只读调查。

有效能力是 **系统子代理白名单 ∩ profile 工具集合 ∩ 本次合同请求集合 ∩ scope gate**。不认识的 profile、模型别名、工具或 Skill ID 在创建子 Runtime 前拒绝；不能静默降级到 `general`、父模型或另一 provider。Profile 不能通过配置打开 `run_shell`、写入工具、MCP、父状态工具或递归委派。

Profile 的本地定义只使用受限字段和有界文本，不接受可执行代码或任意文件引用；启动前冻结配置指纹，恢复时从当前本地配置重新解析并检查兼容性。`delegate_task` 可继续采用原有参数和默认只读行为；新入口将 profile 作为显式字段，不把旧 `model_profile` 混同于角色 profile。

### D2：Skill 能提供角色指导，不能扩大子代理权限

当前阶段十二硬约束是 Skills 仅供父 Agent 使用。为了满足本阶段的 profile 绑定 Skills，`v0.47` 必须显式修订 `AGENTS.md` 对子代理 Skills 的运行时硬约束，并同步更新操作手册；在修订完成前，任何 profile 都不能向子代理加载 Skill。

修订后的范围仍应很窄：profile 只列 Skill ID；父侧在 spawn/followup 准入时对每个 ID 完成独立 PermissionGate 授权并冻结本回合可访问集合。子 Runtime 仅可按需读取已授权 ID，沿用现有 Skill Catalog 的身份、路径、大小和变更复核；正文作为不可信的普通工具结果进入子 history，不进入 system、父 State/Trace 或 verification。后台线程不得弹出交互授权；未预授权的 Skill 调用直接拒绝。Skill 提到的命令仍不能执行。

### D3：后台启动调用只交付启动事实

一个 `spawn` 工具调用只能产生一个对应的 `role=tool` 结果，内容是 `child_session_id`、profile、已接受/拒绝状态和有界预算摘要。只有这个结果与同轮其他工具结果按序提交后，父 Runtime 才能请求下一轮 LLM。子任务完成不能再给原调用补第二个 `role=tool` 结果。

后台完成事件写入结构化状态，并向 CLI 发一条有界通知；父模型在下一安全边界只看到由 Runtime 生成的 ID/状态提示。完整结果通过显式 `get_result(child_session_id)` 返回其自身对应的 `role=tool` 结果。查询和领取必须可重复且按结果 ID 幂等：重复查询不生成新子结果，也不重复计费。子结果保持低信任资料身份。

### D4：后台工作跨父轮次，提交仍由父线程串行收口

工作线程只运行子 Runtime 并产出通过合同校验的结果，不直接改写父 Context、父 Plan、父 generation 或父 session。父 CLI/Runtime 在明确的安全边界收集完成事件，按固定顺序更新父侧生命周期和预算账本；父模型的工具结果仍按其调用顺序提交。完成顺序可以乱序，通知顺序有确定规则，并可按 ID 查询，不因通知丢失而丢结果。

后台 worker 的数量、正在运行数、等待队列、单回合及累计模型调用/token/墙钟预算均有硬上限。
`MAX_SUBAGENTS` 与 `MAX_CONCURRENCY` 由 State 账本和同步/后台共用的并发槽位执行；启动前原子预留。
只有每个启动确认按序提交且整个父工具回合 committed 后，worker 才可开始。失败、取消、超时与模型调用异常都必须结算实际消耗并形成有界结果。父模型结束文本不能绕过“仍有活动或未领取子任务”的完成门槛。

### D5：`v0.48` 的后台性限定在当前进程

`v0.48` 允许父 Agent 在子任务运行时继续处理其他工作，但运行中的线程和请求不能成为 safe point。此时手动 `/save` 明确拒绝并说明活动 ID；CLI 的自动安全点保存应推迟到任务收束后，不能因暂时无法保存而中断父 Agent 的正常工作，也不能把活动线程伪装成可恢复的持久会话。`/new`、`/reset`、EOF、退出及异常清理沿用有界取消与等待：未收束时保留旧任务并报告 ID/原因。异常进程退出后的恢复记录为 interrupted，不自动重放，也不宣称已收到未持久化结果。

若已开启 `/save`，启动确认的提交仍遵守 schema 3 的 handler 前 `handler_admitted`、逐 call 原子提交和整轮 committed 规则。State 与 boundary 会保存接受的合同身份和预留预算，不保存线程句柄；worker 必须等完整回合提交后才启动。若启动结果或整轮提交失败，不启动 worker；若完整启动回合已保存而结果未安全领取便崩溃，恢复标记 `interrupted`、按预留上限结算且不恢复 worker。未完整提交的回合中已确认但未启动的请求释放预留，不伪造已执行结果。

### D6：`v0.49` 保存的是子会话，不只是一个 ID

`child_session_id` 是由 Runtime 分配的不可猜测稳定标识，绑定 workspace、父 task、角色 profile 和模型绑定摘要。续接必须使用同一子 Context/history、子 State 中允许延续的只读事实、已消耗预算和回合序号。每次 followup 生成新合同与新结果 ID；旧结果不可改写。

首版只在子会话空闲、上一结果已交付、父任务处于安全保存点时，把有界子快照与父 session 一起原子保存。建议升级 session schema，并保持旧 schema 1/2/3 读取兼容；不能只在父文件中保存 ID 而把 history 留在内存。子 history、Skill 正文等可能进入私有 session，继续执行现有大小、权限和敏感值限制；超限时明确拒绝续接或保存，不静默裁剪关键身份与预算事实。

跨进程 `/resume` 重新从本地配置构造模型绑定、profile 与 Skill Catalog。来源指纹或角色能力不兼容时暂停该子会话续接并报告原因；不得自动换模型、扩大工具集或重放上一回合。已收束的其他子会话与父任务仍可按既有恢复语义处理。正在运行的子任务始终不能保存为可续接快照。

### D7：Followup 保留身份，重新校验本轮授权

只有创建该子会话的父 task 可请求 followup；旧任务结束、`/new` 或 `/reset` 后的 ID 不可跨任务使用。上一回合必须空闲且结果已领取；并发 followup 或边运行边修改 profile/模型/授权均拒绝。新请求仍需校验 scope、selected facts、当前阶段、profile、Skill grant 和预算；不能靠旧的“允许一次”权限通过新调用。

累计预算不会因 followup 或父 session 恢复而清零。子会话可用轮次、LLM/tool/token 与墙钟限额需区分“本回合上限”和“会话累计上限”；父聚合预算对新建与续接统一结算，`max_subagents` 只在新建时计数。结果中的子证据仍不能成为父 verification evidence。

### D8：保留单一 Runtime 和现有异常边界

每个子回合仍使用独立 `AgentRuntime.run()` 实例，`SubagentRunner` 只负责组装或恢复子 Context、调用公共循环、收口子错误和校验结果；不得另写一条后台专用 agent loop。工具层/执行器继续将 handler 错误转成对应工具结果；父顶层 LLM 与 CLI 异常不新增兜底。

`exploring`、`--plan` 待批准、Crash Recovery、repair/verification 和 terminal 等父任务阶段必须有显式准入矩阵。尤其只读子结果不等于授权的验证；后台运行不能让父任务在仍有未收束结果时进入 `done`。Trace 只读消费父结构化快照，不主动检查子线程、加载 Skill 或打开子 session。

## 4. 版本切片与实施任务

### 4.1 `v0.47`：Agent Profiles / Roles

1. 定义受限 `AgentProfile` 与 Catalog：内置四角色、可选本地自定义、稳定 ID、描述、冻结模型别名、提示词、工具子集、权限和 Skill ID。区分角色 profile 与现有 provider `model_profile`。
2. 扩展子 Runtime 组装，将 profile 提示纳入受保护角色规则，但合同和 selected facts 仍在不可信用户侧；工具与 scope 取交集，拒绝越权配置。保留旧 `delegate_task` 默认行为。
3. 按 D2 设计子 Skill 预授权与加载边界，修订 `AGENTS.md` 中对应硬约束和操作手册；不为子代理开放 MCP 或 shell。若不能完成授权与身份复核，则 v0.47 不能宣称“profile 绑定 Skills”已交付。
4. 测试四角色能力、未知/越权 profile、模型别名失效、工具交集、Skill 未授权与替换、父子 Context 隔离、旧委派兼容。

验收：相同任务选不同角色会得到可观察且受限的提示/工具/模型配置；`tester` 不能执行测试，Skill 不会授予工具权限；旧同步委派结果与预算合同保持兼容。

### 4.2 `v0.48`：Background Subagent（已实现）

1. 已增加 `spawn_subagent`、`get_subagent_status`、`get_subagent_result`、`cancel_subagent` 四个父侧工具；启动返回唯一确认，完成通知只给 ID/状态，结果按 ID 幂等领取。
2. 已将 Manager 扩为跨父轮次任务管理器；worker 只写线程安全完成队列，父侧安全边界收集并结算。同步 `delegate_task` 路径保持原样，两种模式共享并发槽位和父聚合预算。
3. 已冻结阶段准入、纯 spawn 回合闸门、整轮 durable commit 后启动、完成门槛、safe point 拒绝、有界取消、退出和崩溃语义；schema 3 只保存身份、预留预算和生命周期，不保存线程句柄。
4. 已覆盖父 Agent 在子任务运行时继续模型轮次、乱序完成、状态查询、重复领取、预算竞争、取消、超时、异常、结果收集故障、safe point 拒绝、回合提交失败、崩溃不重放和同步委派兼容。

验收：已通过。父 Agent 能在后台子任务未完成时继续独立工作；原启动 call 恰有一个结果，后续结果可重复领取但只结算一次；活动任务不能被保存为 clean 或让父任务进入 `done`。

### 4.3 `v0.49`：Resumable Child Session（已实现）

1. 已增加 `followup_subagent`，稳定复用 `child_session_id`，每轮生成新的 `delegation_id`、`result_id` 和 usage；只接受同一父 task/workspace 下已领取的成功结果，并要求完整的新调查合同。
2. `SubagentRunner` 从 bounded `ChildSessionSnapshot` 恢复子 Context，追加本轮合同输入并调用公共 `AgentRuntime.run()`；角色和模型冻结不变，每轮 Skill 权限重新申请。子历史不进入父 State 或 Trace。
3. 已把新写入 session schema 升至 4，与父 State、Context 和工具边界在同一原子提交中保存 idle 快照；校验结果引用、消息配对、轮次连续性、身份、用量和 256 KiB/720 KiB 快照上限，同时保留 schema 1/2/3 读取。
4. 恢复时从当前本地配置重建并复核角色、模型绑定和 Skill 文件身份；不兼容快照单独报告。只恢复空闲快照，不恢复 worker 或重放请求。
5. 已覆盖同一子会话两轮、父子历史隔离、领取幂等、安全点恢复、累计预算、纯启动回合、整轮提交故障、快照与 State 不匹配及 schema 3 读取兼容。

验收：已通过。已领取的子会话经 `/save`、进程重建和 `/resume` 后可用原 ID 续接；子模型看到自己的历史但不看到父完整 history；累计用量不回退，旧结果不重算、不重复结算。失败、超时、取消、中断、活动和未领取回合均不可续接。

## 5. 文件与边界映射

| 位置 | 预计变化 | 责任 |
|---|---|---|
| `src/mini_agent/delegation.py` | 已更新 | Profile、后台生命周期、受限子 Context 快照、followup、累计预算与身份复核；不复制 agent loop |
| `src/mini_agent/tools/delegation.py`、`tools/__init__.py` | 已更新 | 后台启动、followup、状态/结果查询、取消与同步工具兼容 |
| `src/mini_agent/runtime.py`、`agent.py`、`tools/base.py` | 已更新 | 纯启动回合、整轮提交后启动、父线程安全边界结算 |
| `src/mini_agent/state.py`、`session.py`、`resume.py` | 已更新 | 结构化多轮生命周期、schema 4 快照、预算、身份校验、旧 schema 兼容 |
| `src/mini_agent/permission.py`、`skills.py`、`prompt.py` | 定点修改 | Profile 权限交集、Skill 预授权和子身份提示 |
| `src/mini_agent/__main__.py`、`config.py` | 增量 | 本地配置、完成通知、`/save`、恢复和任务边界清理 |
| `tests/`、`docs/tutorials/`、操作手册、`README.md`、`CHANGELOG.md` | 每版同步 | 离线并发/故障测试、一版一个教学主线和导航 |
| `AGENTS.md` | v0.47–v0.49 已更新 | 子 Skill 合同、多轮续接、后台生命周期、safe point、持久化与恢复硬约束 |

## 6. 横向验收与交付

1. 保持父子共用 canonical `AgentRuntime.run()`；子仍单层、只读，父独占修改、Plan、PermissionGate 用户交互、generation、verification 和完成判定。
2. 每个模型工具调用都有唯一 `role=tool` 结果，同轮结果按模型顺序提交；后台完成通知不伪造工具结果或用户消息。
3. Profile/Skill/模型绑定在联网前校验，来源摘要不泄露真实 endpoint、model ID、API key 或认证头；Skill、文件和子结果始终是不可信资料。
4. 新建、后台执行、取消、失败、恢复和 followup 都受单回合及累计预算约束；无重复子结果、重复结算或凭新 ID 清零用量。
5. `v0.48` 崩溃不会自动重放活动子任务；`v0.49` 只恢复安全点中的空闲子会话。损坏或不兼容子快照明确拒绝，不猜测补全。
6. 覆盖同步委派、Plan/Repair、v0.33 Crash Recovery、v0.39 Durable Delegation、进程管理与阶段十二 Skills 的回归；核心运行时仍只依赖标准库。
7. 实施前复核阶段十计划中尚未核销的子代理敏感路径问题；profile、后台和续接不得扩大其影响面。各版本交付时运行相关测试及 `PYTHONPATH=src python -m pytest -q`、`PYTHONPATH=src python scripts/check_tutorials.py`、`PYTHONPATH=src python scripts/check_readme.py`。
8. 文档、教程、版本信息随实际实现逐版更新；tag 仅由用户手动操作。

## 7. 阶段级端到端场景

1. 父 Agent 用 `explorer` 和 `reviewer` 启动两个只读任务，获得两个 ID，继续处理主任务；两个子任务乱序完成，父收到通知并分别领取结果。
2. `tester` 分析测试覆盖并提出父 Agent 可运行的验证建议；父 Agent 自行运行验证，子报告不进入父 `verification_evidence`。
3. 后台子任务运行时父尝试 `/save`、`/new` 或退出：保存明确拒绝，任务切换先有界取消；无法收束时保留旧任务和 ID。
4. 开启 `/save` 后在启动 ack、子结果生成、通知及领取各边界注入故障；恢复不重复启动或交付，也不把未保存的结果伪造成成功。
5. 已领取结果的子会话保存并恢复后，父用原 ID 追加问题；子保留自己的历史，不能看到父完整历史、其他子会话或旧权限。
6. Profile/模型配置改变、Skill 文件替换、子快照损坏或预算耗尽时，followup 在下一次模型请求前明确拒绝。

## 8. 完成定义与后续阶段

- [x] `v0.47` 四个角色与受限自定义 profile 可复现；模型、提示、工具、权限、Skills 绑定均经运行时校验，旧 `delegate_task` 兼容。
- [x] `v0.48` 父 Agent 可跨轮次继续工作，子完成可通知和查询；活动任务有界取消，保存与崩溃语义明确。
- [x] `v0.49` 同一子会话可安全 followup，空闲快照可跨进程恢复，累计预算与权限不倒退。
- [x] 同步委派、工具协议、持久边界、恢复、Plan、verification 和阶段十二外部能力隔离无回归。
- [x] 版本文档、操作手册、教程、README、CHANGELOG 与包版本信息同步；阶段级离线故障覆盖已加入。
- [x] 全量 pytest、教程结构检查和 README 检查通过；代码编译及 `git diff --check` 通过。
- [ ] 教程固定 tag 事实检查已运行；除 `v0.49` tag 尚未由用户创建外无其他问题，tag 建立后需重跑该检查。

满足上述条件后，阶段十四只做能力、可靠性、成本和版本演进的 Evaluation & Regression；发现的缺陷作为修复处理，不默认开启更重的多代理基础设施。
