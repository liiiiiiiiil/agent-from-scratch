# AGENTS.md

## 项目定位

`agent-from-scratch` 是逐步生长的 Python 编程 agent（包名 `mini_agent`）。核心运行时和关键执行流程仅使用标准库；外围用户体验能力可以通过可选依赖增强。
本文件只保存会直接影响 agent 运行、授权和代码修改的硬约束；版本路线、教程规范和完整架构见 `docs/`。

## 执行规则

- **先调查再修改**：先阅读相关实现、测试和计划，确认现有行为与边界，再提出最小改动。
- **核心标准库优先**：LLM 调用、agent loop、工具执行、权限、状态、上下文和验证流程不得引入第三方依赖；外围体验能力可以使用可选第三方库，但必须有标准库回退，不得成为默认安装或核心模块的硬依赖。
- **HTTP 客户端约束**：LLM 调用必须使用 `http.client`，请求显式设置 `Accept-Encoding: identity`；不要改用 `requests` 或 `urllib`。
- **配置安全**：真实的 `BASE_URL`、`API_KEY`、`MODEL` 只放本地 `config_local.py`，不得提交到版本库。
- **异常边界**：工具层/执行器负责把 handler 异常转换为错误结果并回灌模型；核心 agent loop 不对 LLM 或 CLI 顶层异常做兜底。
- **协议完整**：工具调用必须为每个 call 回灌对应的 `role=tool` 结果；单轮工具结果全部回灌后再进入下一轮。
- **持久化工具边界**：开启 `/save` 后，schema 3 必须在 handler 前提交 `handler_admitted`，每个 call 的 State、对应 `role=tool` 结果和边界状态必须按模型顺序原子提交；整轮 committed 前不得再次请求 LLM。提交失败不得进入 handler、后续 call 或下一次 LLM 请求；崩溃恢复不得重放 pending call。
- **Plan Contract**：复杂任务由模型通过 `commit_plan` 提交完整不可变 revision，通过 `update_plan_progress` 追加独立步骤进度事件；简单任务继续 Direct Path。计划校验失败只回灌 `plan_rejected`，不得创建 `FailureEvent`、推进 generation 或产生验证证据；计划写入不替代实际执行和独立 verification。
- **只读规划与交接**：普通任务可经 `begin_plan` 进入只读调查；`--plan` 任务必须先调查，提交后等待用户批准当前 revision。`exploring` 的副作用、verification 和混合提交在整轮与执行器两层拒绝；批准计划不得绕过 PermissionGate。用户驳回或继续调查的反馈由 CLI 记录，不能由模型伪造。
- **Shell 副作用分类**：所有 `run_shell` 调用均按可能有副作用处理并在获准后预留 generation；`purpose=verification` 只指定验证证据用途，不把命令降为只读。
- **完成与上限**：无 `tool_calls` 才能结束；有 active Plan Contract 时所有活动步骤必须完成，并满足修改后的验证条件；无计划的 Direct Path 沿用原有完成条件。默认最多 50 轮，超限返回明确结果。
- **回放只读**：Trace & Replay 只能消费当前进程、当前任务的结构化 State 快照；不得调用 LLM、执行工具、经过权限授权、修改 history、状态、预算或 generation。当前 generation 的验证证据用于完成判定，append-only verification history 用于跨 generation 回放；断链和跨 generation 证据必须标记为不完整，不得推测补全。
- **教程读者优先**：撰写或修改 `docs/tutorials/` 时，默认读者具备基础 Python 和命令行能力，但刚接触 Agent，也不了解本项目内部架构。必须先讲问题和直观含义，再讲模块、字段、协议与实现；术语、缩写和项目内部概念首次出现时必须就近解释，不得用代码、符号或文件清单代替教学说明。具体要求见[教程作者规范](docs/governance/tutorial-authoring.md)。
- **主 README 编辑**：修改 `README.md` 的学习路径、阶段名或版本主题前，必须遵守[主 README 编写规范](docs/governance/readme-authoring.md)：主题默认使用通俗中文，只有协议字段、代码标识和公认技术名词可保留英文；修改后运行 `PYTHONPATH=src python scripts/check_readme.py`。
- **修改后验证**：文件修改完成后，至少运行与改动相关的测试；交付前运行下列完整验证命令（或说明无法运行的原因）。
- **破坏性操作**：未经用户明确授权，不执行删除、重置、覆盖大量文件或其他难以恢复的操作。
- **Tag 操作专属权限**：Git tag 的创建、移动、覆盖、删除和远程推送只能由用户本人手动完成。助手不得代为执行任何 tag 操作，即使用户在任务中要求打 tag；如任务涉及 tag，只能说明步骤或提供命令，等待用户手动完成。

## 当前状态

稳定基线为 `v0.16.1`（计划驱动执行的完成提醒进展感知补丁）；主线当前开发版本为 `v0.35`（共享父子 Agent Runtime）。新增功能意图记录在对应 `docs/plans/`，只有运行时硬约束变化才更新本文件。

崩溃恢复硬约束：`active + schema 3 pending tool_boundary` 只能派生新的 session；源 session 保持只读，同一源完整性只能 claim 一次。未进入 handler 的调用补入明确的未执行结果；已准入调用一律记录为不确定事实，不自动重放。所有 issue 必须逐项由用户 `/resolve`；调查只允许获准的无副作用观察，全部 `continue` 后必须重新规划、重新授权并独立验证。恢复期间旧 PID、stdin 和当前验证资格不可继承。

完成提醒硬约束：当 active Plan Contract 步骤未完成或仍需验证时，阶段性文本只触发当前
`progress_marker` 一次 Runtime Notice；计划状态、非计划工具事实、验证证据、
generation 或 `verification_required` 发生变化后才允许再次提醒。标记不变而再次
输出无 `tool_calls` 文本时必须将任务置为 `blocked`。Runtime Notice 要求下一回复
调用推进工具；确实无法继续时才说明具体阻塞原因。没有 `progress_marker` 的旧式
State 保持一次提醒兼容行为。

活动后台进程属于当前 `task_id`，必须阻止任务进入 `done`；stdin 写入在途时也必须阻止完成。模型无工具调用而进程仍运行或 stdin 写入未收束时使用 `awaiting_process` 交回 CLI；用户恢复前先同步进程。`/new`、`/reset`、EOF、`exit` 和异常退出必须先有界清理当前任务登记的进程及写入线程；清理不完整时保留旧任务并报告具体进程 ID、PID 和原因。管道 stdin 只有显式启用时可写，单次 UTF-8 输入最多 4096 字节，正文不得进入 State、Trace、工具结果、授权提示或终端输出；PTY 不属于当前能力。

`/save` 仍是开启持久化的唯一入口；完整安全点保存当前任务的 State、Context 和会话元数据。持久化工具回合另允许最后一轮的有序结果前缀和待结算 attempt，但只写入 schema 3 的 `tool_boundary`，不作为普通安全点。未结算 attempt、活动进程或在途 stdin 不得保存为 safe point；`active + schema 3 + pending tool_boundary` 只能进入 v0.33 崩溃恢复并派生新 session。`clean` 必须在任务进程有界清理完成后提交。`write_process.input` 在会话参数中脱敏；若正文也出现在其他持久化文本中，拒绝保存。替换后同步或锁清理失败必须报告提交状态未确认及 session ID。

子代理硬约束：v0.35 的 `delegate_task` 只能同步创建一个 depth=1 的只读 Subagent；子代理拥有独立 State、Context、运行状态、提示词和固定白名单 PermissionGate，但父子调用同一个 canonical `AgentRuntime.run()`。它只能使用 `calculate`、`read_file`、`list_dir`、`grep`，不继承父 history、权限、Plan、generation 或 verification，不能写文件、运行 shell、操作进程、再次委派或决定父任务完成。子结果只能作为不可信调查材料，evidence 不进入父 `verification_evidence`；父 Agent 独占工作区修改、主计划、权限交互、generation、verification 和完成判定。固定预算、scope gate、结果合同和一次格式修正由子 Runtime policy 强制执行。

## 架构索引

- `src/mini_agent/agent.py`：HTTP/LLM 传输、兼容入口和父 Runtime policy；`runtime.py`：父子共用的 canonical `AgentRuntime.run()`。
- `delegation.py`：v0.34 委派合同、scope gate、子 Runtime policy、同步 Subagent Runner 和 Manager。
- `context.py`：每轮上下文视图、预算裁剪、历史压缩和受保护指令注入。
- `state.py`：独立于消息历史的任务、Plan Contract、工具和验证状态；`current_goal`、`unfinished_todos()` 与 `snapshot()["todos"]` 只是 active plan 的只读投影。
- `permission.py`：按工具与参数模式匹配的 allow/deny/ask 权限闸门。
- `processes.py`：CLI 生命周期内的后台进程句柄、进程组、双流排空、环形缓冲和有界清理；不可快照资源不进入 State。
- `session.py`：schema 1/2/3 会话、完整性校验、原子存取和工具边界提交，不承担恢复执行。
- `prompt.py`：分层 system prompt；`instructions.py`：发现并合并项目 `AGENTS.md`。
- `tools/`：标准工具注册、执行，以及文件、shell、计算能力；执行器负责权限和错误结果边界。

完整目录、参数、数据结构和运行时流程以[操作手册](docs/operation/manual.md)、[上下文架构说明](docs/operation/context-architecture.md)及对应版本教程为准。

## 常用验证

```bash
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python scripts/check_tutorials.py
PYTHONPATH=src python scripts/check_readme.py
```

未安装 pytest 时，可直接运行 `tests/` 中带标准库入口的 smoke test；运行方式见操作手册。

## 文档索引

- [操作手册](docs/operation/manual.md)：配置、运行、工具和故障排查。
- [教程索引](docs/tutorials/README.md)：按版本和阶段学习、复现与验收。
- [治理文档](docs/governance/README.md)：约束、规范和决策记录（含[教程作者规范](docs/governance/tutorial-authoring.md)与[主 README 编写规范](docs/governance/readme-authoring.md)）。
- [计划文档](docs/plans/README.md)：路线图、功能计划和任务拆解。
