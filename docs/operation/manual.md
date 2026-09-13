# mini_agent 操作手册

> 本手册跟随最新版本更新。当前对应版本：**v0.25**（计划轨迹回放与验收；含 v0.24 证据驱动重规划、v0.23 只读规划与用户交接、v0.22 Plan Contract、v0.21 Trace & Replay 和此前可靠执行能力）。

## v0.25 计划轨迹回放与验收

`AgentState.snapshot()` 的 `trace_events` 是任务内连续的只读轨迹索引。每条事件保存 `sequence_id`、发生时的 `generation_id` / `revision_id`、已有记录的类型和 ID；verification 使用 `verification_history` 的索引。事件不复制工具原始输出，不推进 generation，不改变预算，也不参与完成判定。

这三个字段承担不同职责：`generation` 表示环境副作用或恢复后的验证代次，`revision` 表示计划结构版本，`sequence_id` 连接跨列表的发生顺序。同一 generation 内提交多个 revision 后，调查、执行和验证的 revision 归属以事件为准，不能只按 generation 或数组位置推断。

Trace API 保留原调用方式，并增加 revision 查询：

```python
from mini_agent.trace import build_trace, render_trace

report = build_trace(state.snapshot())
revision_report = build_trace(state.snapshot(), revision_id=2)
print(render_trace(revision_report))
```

`generation_id` 与 `revision_id` 不能同时指定；非法 ID 抛 `TraceQueryError`。完整报告新增 `plan_revisions` 和有序 `plan_timeline`。每个 revision 视图包含 parent、trigger 前因、模型提交的 `reason`、Runtime 重算的结构差异、步骤及 progress、plan-only 用户决定，以及生效期间的调查、执行、failure、recovery、verification 和 generation。`causal_edges` 新增 parent、trigger、revision 相关边，并以 `resolved` / `UNRESOLVED` 标记完整性。

```text
/trace
/trace <generation_id>
/trace revision <revision_id>
```

revision 查询会把所选 revision 的 trigger 来源保留为前因；generation 查询展示该代提交或生效的 revision，并用 `generation_role` 区分两者。历史 generation 的结论依据按该代末的计划与进度事件重建，不借用后续 revision；没有顺序终态记录时，历史终态摘要标为 `not_recorded`。旧 snapshot 没有计划数据时沿用 v0.21 结果；有计划记录却没有 `trace_events` 时保留可直接验证的结构事实，并将跨记录先后和执行归属标记为不完整。Trace 不重新执行任务，也不把后续 generation 的通过证据倒推成旧方案正确。

## v0.24 证据驱动重规划与停滞收口

执行中的计划不能因为模型一句“需要调整”就被静默改写。模型只能独占调用 `request_replan(kind, source_id, reason)`：`failure` 必须精确引用当前 `active_failure_id`，`observation` 必须引用当前 active revision 提交之后成功且获准的只读 `ExecutionAttempt`。调用通过后进入 `exploring`，只允许只读调查或独占 `commit_plan`；后续修订必须引用当前 parent revision 和活动 trigger。

Direct Path 因 failure 或 blocked 恢复进入 Explore 时可能还没有 parent。此时首次 `commit_plan` 必须带活动 trigger、不能提供 `parent_revision_id`；普通任务的初始计划仍然不带 trigger 和 parent。有效后续 revision 消耗总 replan 预算，计划差异由 Runtime 保存为 `retained`、`added`、`cancelled`、`replaced`，并标出保留步骤的依赖变化及目标、约束和成功标准的变化。无变化提交只增加当前 trigger 的无进展计数，第二次达到上限后阻塞。

默认最多 3 次有效后续修订。第三次修订仍可以执行，请求第四次时进入 `blocked`。失败修订不会删除 FailureEvent、repair cycle、generation 或旧验证记录；提交新方案只表示诊断形成了新路径，实际修改与当前 generation 的独立 verification 仍必须发生。

模型不能自行恢复 `blocked` 或 `failed`。用户可对 blocked 任务输入 `/resume <反馈>`；该命令记录 `resume_blocked` 决定和恢复前终态原因，再进入 `running / exploring`。如果 blocked 来自 `recover(ask/block)`，Runtime 会保留后继 generation 的独立 verification 要求，并先恢复为 `diagnosis_required`，所以新修订提交后仍必须验证；其他 blocked 来源沿用普通恢复路径。如果已有 active plan，修订必须引用它；没有 active plan 时走带 trigger、无 parent 的首次提交。预算耗尽、failed 或已有活动恢复 trigger 时请使用 `/new <任务>`。

停滞检测在一个完整工具回合的所有调用执行或拒绝、State 按模型顺序更新、结果展示并回灌所有 `role=tool` 消息之后运行。默认 `MAX_STAGNANT_ROUNDS=3`：第二个连续无进展回合注入一次受保护 Runtime Notice，第三个记录 `repeated_action`、`no_new_observation`、`explore_without_commit` 或 `execute_without_progress` 并进入 blocked。它只保存动作、调查结果 hash 和短摘要，不复制完整工具输出；Planning gate 与 Repair gate 给出的合法下一动作优先于提醒文字。

本版新增配置：

| 配置项 | 默认值 | 说明 |
| --- | ---: | --- |
| `MAX_ATTEMPT_FINGERPRINTS` | `4` | 同一工具及参数指纹的执行上限；必须不小于 `MAX_STAGNANT_ROUNDS + 1`。 |
| `MAX_REPLAN_REVISIONS` | `3` | 单任务有效后续计划 revision 总数。 |
| `MAX_NO_PROGRESS_REPLANS` | `2` | 同一活动 trigger 的无结构变化提交次数。 |
| `MAX_STAGNANT_ROUNDS` | `3` | 连续无进展完整工具回合上限，必须大于 1。 |

## v0.23 只读规划与用户交接

普通任务仍从 `direct` 开始。模型认为任务需要先规划时，独占调用 `begin_plan` 进入 `exploring`；尚未提交计划时可用 `cancel_planning` 回到 Direct Path。命令行使用 `PYTHONPATH=src python -m mini_agent --plan "<任务>"` 时，首条任务从 `plan_only / exploring` 开始，不能取消强制规划。该命令处理首条任务后仍进入交互循环。

`exploring` 只允许无副作用调查、独占调用 `commit_plan`，以及普通模式尚无计划时的 `cancel_planning`。shell、文件写入、verification、恢复和进度更新会在 PermissionGate 之前被拒绝；同回合混合 `commit_plan` 与其他调用会整体拒绝。`--plan` 首次提交还要求至少一条成功、获准且已执行的只读调查记录，否则返回 `plan_rejected`。每个被拒绝的调用仍有对应工具结果，但不运行 handler、不推进 generation、不生成验证证据。

普通模式提交计划后进入 `executing`。`--plan` 模式提交后停在 `awaiting_approval`，CLI 从 State 中的 `active_plan` 打印完整待批计划及当前 revision ID，再等待用户决定；即使 `OUTPUT_MODE=quiet` 也会显示，模型不会重新复述计划或继续执行。继续调查后使用 `/review`，CLI 会再次打印同一份计划和决定命令。交接命令：

| 命令 | 作用 |
| --- | --- |
| `/approve <revision_id>` | 批准当前待批 revision，继续执行。 |
| `/reject <revision_id> <反馈>` | 驳回当前 revision，保存反馈并返回只读调查。 |
| `/continue <revision_id> <反馈>` | 保留当前 revision，带反馈继续只读调查。 |
| `/review <revision_id>` | 继续调查后若方案未变，将原 revision 重新交付审批。 |

驳回或继续调查后若提交修订，`commit_plan` 必须同时提供当前 `parent_revision_id` 和 `trigger_id`。用户决定、反馈触发记录和旧 revision 都保存在当前任务 State；`/reset` 或 `/new <任务>` 会清除它们。批准旧 revision、重复批准、无反馈驳回或驳回后直接 `/review` 都会拒绝。计划批准只改变规划阶段，不写入 PermissionGate 的 allow 规则；后续工具继续单独授权，修改后仍须独立 verification。失败/观察触发重规划、修订预算及停滞检测见本手册开头的 v0.24。

## v0.22 Plan Contract

复杂任务可以通过 `commit_plan` 提交结构化计划。计划包含 `goal`、`constraints`、任务级 `success_criteria` 和 1–50 个带稳定 `step_id` 的步骤；步骤可以声明 `depends_on`、步骤级 `success_criteria` 和 `replaces`。简单任务继续走 Direct Path，不需要创建计划。

普通任务的初次 `commit_plan` 不提供 `parent_revision_id` 或 trigger，成功后创建 revision 1；`--plan` 模式进入 `awaiting_approval`。结构发生变化时，必须先由用户反馈、失败或观察创建活动 trigger，再提交带当前 active revision 作为 parent 的完整新计划；Direct failure 或 blocked resume 没有 parent 时，首个修订计划只带 trigger。旧 revision 不会被覆盖。只改变步骤状态时使用 `update_plan_progress`，状态只能按 `pending → in_progress → completed` 推进，依赖未完成或已有其他进行中步骤时会被拒绝。

计划校验和 revision 提交在同一把 State 锁内完成。无效参数或违反计划不变量的请求会收到 `plan_rejected`，不会创建 `FailureEvent`、进入 Repair Loop、推进 generation 或产生验证证据。两个计划工具默认 `ALLOW`、`effect_class=none`，也不能成为 `recover` 的 retry、adjust 或 rollback 目标。计划写入不代表环境已经正确，步骤完成仍不能替代独立 verification。

`AgentState.snapshot()` 同时提供完整的 `plan_revisions`、append-only 的 `plan_progress_history`、当前推导出的 `active_plan` 和 `planning_state`。`current_goal`、`unfinished_todos()` 与 `snapshot()["todos"]` 只是 active plan 的只读兼容投影。Structured State 只显示有界的目标、当前步骤、最多五个 ready 步骤、最多十个阻塞步骤及数量摘要；压缩后仍从 State 重建，不从历史摘要恢复计划。

v0.21 的 `/trace` 继续只读回放 generation、执行、失败、恢复和验证事实；本版不把完整 Plan 因果链加入 Trace。

## v0.21 任务轨迹回放（Trace & Replay，只读）

`/trace` 回放当前进程、当前任务已经保存的结构化事实：Todo revision、generation、执行尝试、失败、恢复动作、验证证据和终态。`/trace 3` 只显示 generation 3；计划链和 revision 查询见上面的 v0.25 说明。命令在 `run_task()` 之前拦截，不追加 user history，不调用 LLM、工具 handler 或 PermissionGate，也不修改 State、预算或 generation。

回放也可通过标准库 Python API 使用：

```python
from mini_agent.trace import build_trace, render_trace

report = build_trace(state.snapshot(), generation_id=None)
print(render_trace(report))
```

`report["integrity"]` 为 `complete` 时，当前保存的引用可以完整验收；为 `incomplete` 时，`issues` 会说明断链、缺失的历史验证证据或跨 generation 证据，报告仍保留可确认的原始记录，不能据此推测缺失事实。失败诊断优先使用已有 `cause_hint`，否则显示关联恢复动作的 `reason`，两者都没有时显示“未记录诊断”。

v0.21 的 Trace 对缺失的 `todo_revisions` 会安全降级为空；v0.22 不再把 Todo revision 作为计划写入来源，计划历史改由本节开头的 `plan_revisions` 和 `plan_progress_history` 保存。

## v0.20 Repair Loop（修复循环）

失败后的执行不能直接跳回普通写入。`AgentState.snapshot()["repair_loop"]` 显示当前阶段、活动 failure/recovery 和周期预算：

| 阶段 | 允许的下一步 |
|---|---|
| `idle` | 正常调查、执行和计划推进 |
| `diagnosis_required` | 只读调查、独占调用 `recover` 处理当前 `active_failure_id`，或独占调用 `request_replan` |
| `verification_required` | 下一工具回合只能是单个 `run_shell(purpose="verification")` |

agent loop 在回合级检查批量调用，ToolExecutor 在权限和 handler 前再次检查；不合规调用会收到协议错误且不会运行 handler、询问权限或推进 generation。恢复目标携带受 State 锁保护的 reservation，是 verification 阶段唯一的受控执行例外；恢复结果回灌后仍必须有独立 verification。

`MAX_REPAIR_CYCLES` 默认是 3。初始失败、schema/参数拒绝、权限拒绝以及 `ask`/`block` 不消耗周期；`retry`、`adjust`、`rollback` 只有在目标授权并激活 successor generation 后才计入。验证失败会重新进入 `diagnosis_required`，而不是直接增加周期；需要第四次恢复时以明确的 `failed` 原因收口。恢复成功本身不代表任务完成，只有当前 generation 的验证通过且 active Plan Contract 的步骤完成，完成提醒才会消失；没有 active plan 时沿用 Direct Path 的验证条件。

Structured State 和上下文压缩后的 critical state 会保留 `repair_loop`、最近失败/恢复动作、generation 与预算。完成提醒会按阶段说明唯一合法的推进动作；相同 `progress_marker` 下再次只输出文本仍会进入既有 `blocked` 保护。v0.24 另外保留活动 trigger 的来源与理由、replan 剩余预算、`LoopStagnationState` 的计数和 gate 允许的下一动作。

## v0.19 检查点与回滚（Checkpoint / Rollback）

`write_file` 和 `edit_file` 在权限放行、attempt/generation 预留后，会为单个工作区内普通文件保存前镜像。前镜像最多 `MAX_CHECKPOINT_BYTES`（默认 1 MiB），不存在的文件记录为 `absent` tombstone；符号链接、工作区外路径、目录/特殊文件、无效父目录和读取失败只会让检查点变为 `unavailable`，不会改变原文件工具行为。

文件工具结果和 critical Structured State 会保留检查点 ID、相对路径、attempt、generation、状态和哈希；上下文压缩或超长状态降级后仍保留这些恢复元数据。若获准的文件 handler 抛错但前后镜像明确，任务保持可恢复并提示回滚；前后镜像不可用时才按未知副作用阻塞。internal 工具不能作为 retry/adjust 目标，原始 mode 为 `0` 也会按原值恢复。

模型通过 `recover(action="rollback", checkpoint_id=...)` 请求恢复。`rollback_checkpoint` 是内部工具，不出现在 LLM schema 中，也拒绝模型直接调用；RecoveryRuntime 会使用检查点保存的规范化相对路径经过同一 PermissionGate 授权。恢复前重新计算当前文件的类型和 SHA-256，发现外部修改就拒绝写入并进入 `blocked`。普通文件使用同目录临时文件、原 mode 和 `os.replace` 原子恢复；absent tombstone 只在后镜像仍匹配时删除目标。

恢复成功只证明恢复操作本身完成：它会打开新的 generation、清除旧 verification evidence，并要求下一轮独立 verification。检查点元数据保留在 Structured State 中，但不包含前镜像内容或绝对路径；`/reset` 和 `/new` 会清除本任务全部检查点及私有字节。

配置项：

| 配置项 | 默认值 | 说明 |
|---|---:|---|
| `MAX_CHECKPOINT_BYTES` | `1_048_576` | 单个前镜像的最大字节数，可在 `config_local.py` 覆盖 |

## v0.18 Recovery Policy

`recover` 支持 retry、adjust、ask、block。v0.18.1 修复了拒绝记录和预算边界：schema、引用、参数、预算或权限拒绝都会只记录一次带 `recovery_id` 的 rejected action，不推进 generation。目标权限检查前先在 State 锁内预留额度；获准后才打开后继 generation 和 attempt。权限拒绝释放未使用的目标执行及重试额度，但恢复申请仍计数，连续无效申请达到恢复动作上限也会阻塞。恢复目标复用当前会话的同一把 PermissionGate，不重复询问同一次授权；恢复后必须独立 verification。

`edit_file` 的没有匹配和多处匹配是确定性的参数前置条件失败（`error_kind=edit_no_match` / `edit_multiple_matches`），不会改文件或进入未知副作用阻塞；已获准进入 handler 的其他异常仍会使 possible-effect generation 保持推进。Structured State 在 `running` 时也显示恢复通知、最近失败/恢复动作和脱敏预算。进入 `blocked` 或 `failed` 后，Executor 不再询问权限或运行 handler，当前批次剩余调用以 `task_terminal` 结果逐一回灌。

## v0.17 失败模型

每个任务从 generation 0 开始。可能产生副作用的工具（文件写入、编辑和所有 shell 命令）在权限放行后、handler 前原子推进 generation；即使 handler 失败也不会回退。`purpose="verification"` 只指定命令结果用作验证证据，不证明 shell 命令只读，因此验证命令也会打开新 generation，证据绑定这一代。Executor 产出结构化 `ExecutionResult`，State 保存 `ExecutionAttempt`、`FailureEvent` 和绑定 generation 的 verification evidence。参数以 canonical JSON 的 SHA-256 指纹计数，Structured State 仅显示 hash/脱敏摘要。全只读回合可并发，包含 shell 的回合按模型顺序串行提交；verification 不得与其他可能有副作用的调用同轮。

## v0.16 计划驱动执行（Plan-driven Execution）

复杂任务通常按 Plan → Execute → Observe → Verify 推进；如果观察结果或验证结果暴露问题，模型再 Replan（重排 Todo）并继续执行。文件修改以及所有实际执行的 `run_shell` 都按可能改变环境处理，会使旧验证失效；使用 `run_shell` 的 `purpose="verification"` 且退出码为 0 的结果作为完成证据，建议将最终测试或检查作为最后一个 verification 调用。验证命令本身应只检查结果，不承担文件修改；Runtime 保守记账可能的副作用，但不提供通用 shell 沙箱。

v0.16.1 修复了完成提醒：当模型在 Todo 未完成或仍需验证时输出阶段性文本，运行时注入明确的 Runtime Notice，要求下一回复调用推进工具（更新 Todo、调查/操作或验证），而不是只口头描述下一步。提醒按进展状态最多一次：完整 Todo、非 Todo 工具结果、验证证据数量、generation 或 `verification_required` 发生变化后，可以再次提醒；相同标记下再次输出无工具文本才标记 `blocked`。没有 `progress_marker` 的旧式 State 保持一次提醒兼容行为。这种保守策略不依赖第三方库或命令解析。

## v0.15 任务清单与状态（Todo / Task State）

以下是历史版本行为；从 v0.22 起 `update_todo` 不再注册为模型可见工具，当前计划写入请看本手册开头的 Plan Contract。

模型可调用 `update_todo` 提交完整任务列表。状态为 `pending`、`in_progress` 或 `completed`，最多一个进行中项；更新失败时旧状态不变。Todo 属于 AgentState，Execution State（工具历史、文件、错误）仍由执行器维护；每轮请求通过 Structured State 注入，压缩后也会恢复。v0.15 不自动规划、持久化或阻断完成。

## v0.14 项目级指令（Project Instructions）

启动时 Agent 会从 Git 根目录到当前工作目录按顺序读取 `AGENTS.md`，并将带来源标记的内容作为受保护 system context 注入每次请求。非 Git 目录只检查当前目录；总长度上限为 12,000 字符。项目级指令不会放宽权限，也不会因 trimming 或 compaction 消失。

## 1. 环境准备

### 1.1 依赖
- Python 3.10+（项目统一使用 Python 3.10 及以上版本）
- 核心运行时零第三方依赖，仅 Python 标准库
- 可选 CLI 体验依赖：`prompt_toolkit>=3.0,<4`

### 1.2 安装方式

**方式一：开发模式安装（推荐）**
```bash
cd agent-from-scratch
pip install -e .
```
安装后可从任意目录运行 `python -m mini_agent`。

需要多行编辑、Shift+Enter 换行和多行粘贴时，额外安装：

```bash
pip install -e '.[interactive]'
```

未安装该 extra 时，CLI 自动回退到标准库 `input()`，核心功能不受影响。

**方式二：免安装，用 PYTHONPATH**
```bash
# Windows PowerShell
$env:PYTHONPATH="src"
python -m mini_agent
```
```bash
# Linux/macOS
PYTHONPATH=src python -m mini_agent
```

### 1.3 配置
配置分两层：`config.py`（占位模板，进 git）+ `config_local.py`（真实配置，不进 git）。

首次使用：复制 `src/mini_agent/config_example.py` 为 `src/mini_agent/config_local.py`，填入真实值。`config.py` 会自动 `import *` 加载 `config_local.py` 覆盖占位值。

| 配置项 | 占位值 | 说明 |
|---|---|---|
| `BASE_URL` | `http://your-gateway-host/v3/openai/model` | LLM 网关地址 |
| `API_KEY` | `sk-YOUR_API_KEY_HERE` | 网关密钥 |
| `MODEL` | `EB-GLM-5.2` | 模型名 |
| `MAX_ITERATIONS` | `50` | agent loop 最大轮数 |
| `CONTEXT_WINDOW` | `128000` | 模型上下文窗口的 token 估算值 |
| `OUTPUT_MODE` | `normal` | 终端输出级别：`quiet`、`normal` 或 `debug` |

> 真实配置写进 `config_local.py`（不进 git）；无 `config_local.py` 时回退到 `config.py` 占位值。

---

## 2. 运行

### 2.1 单次任务模式
```bash
python -m mini_agent "你好"
```
任务完成后进入交互模式，可继续追问。

### 2.2 交互模式
```bash
python -m mini_agent
```
启动后进入交互提示符。安装 `interactive` extra 后，Enter 提交、Shift+Enter 换行，粘贴多行文本后按 Enter 提交；未安装时使用标准库单行输入。输入 `exit` 或 `quit` 退出，或按 Ctrl+C/Ctrl+D。

普通后续输入默认继续当前任务。使用 `/new <任务>` 清空旧任务并开始新任务，使用
`/reset` 清空当前任务和任务级状态；会话内已经授予的权限和项目级指令不受影响。

---

## 3. 当前能力（v0.25，含 v0.18.1 完成提醒修复）

v0.13 在 v0.12 的预算与裁剪之上加入历史压缩和 Context Observability。完整 `history` 保留在本地；每次 LLM 调用前，`ContextManager` 都生成一个可发送的、协议合法的上下文副本。预算超限且存在旧轮次时，旧历史会先尝试压缩为摘要，摘要失败则退回 v0.12 的 trimming。终端默认使用 `OUTPUT_MODE = "normal"` 显示简短进度；设置为 `debug` 可查看 token 分桶、裁剪/压缩事件和有界工具细节，设置为 `quiet` 可隐藏过程输出。`CONTEXT_OBSERVABILITY = False` 仍可关闭默认 observer。

v0.14 在启动时加载适用的 `AGENTS.md`，并将项目级指令作为受保护 system context 注入每次请求。详情见[第 14 课](../tutorials/14-project-instructions.md)。

### 3.1 上下文架构

完整的上下文生产线、双轨记录、预算反馈环和运行时不变量见[上下文架构说明](context-architecture.md)。本节保留操作层面的字段和配置速查。

`AgentState` 保存任务执行事实，独立于会被 LLM 消费的 `messages`：

| State 字段 | 内容 |
|---|---|
| `task` / `current_goal` | 当前任务与目标 |
| `tool_history` | 工具名、参数、成功状态、结果摘要 |
| `files_changed` | 成功写入或编辑过的文件路径 |
| `errors` | 权限拒绝或工具失败记录 |
| `status` | `running` / `done` / `blocked` / `failed` |
| `todos` | active Plan Contract 的只读兼容投影 |
| `plan_revisions` / `plan_progress_history` | 不可变计划结构历史与独立步骤进度事件；完整历史不直接注入 LLM 上下文 |
| `planning_state` / `active_plan` | 当前计划阶段、active revision 和应用进度事件后的执行视图 |
| `replan_triggers` / `user_plan_decisions` | 活动 trigger、四类来源、用户决定及恢复前终态原因 |
| `loop_stagnation` | progress epoch、连续无进展回合数、短指纹、告警类型和最近原因 |
| `verification_evidence` | 最近 verification 命令、退出码与结果；只有当前 generation 的 `[exit=0]` 才算通过 |
| `verification_history` | append-only 的任务内 verification 审计记录；跨 generation 回放使用，不参与完成判定或 LLM 上下文 |
| `failures` / `recovery_actions` | 最近失败的工具、failure/attempt/generation、分类与可重试性，以及恢复动作状态和因果引用 |
| `repair_loop` | 当前修复阶段、活动 failure/recovery、已使用/剩余 repair cycle 和要求的下一动作 |
| `checkpoints` / `rollback_checkpoints` | 单文件前后镜像元数据；后者只列出当前可回滚的 `ready` 检查点，不含文件内容 |
| `budgets` / `recovery_notice` | replan、无进展、失败重试、参数指纹、恢复动作和 repair cycle 的剩余额度及当前恢复提示 |

所有 LLM 请求都经 `ContextManager.prepare_messages()`。它按 `len(text) // 3` 估算 token，保留输出空间，并在超限时先截断最老的 tool result、再删除最老的完整历史轮次。工具执行结果通过 `ToolExecutor(on_result=state.record_tool)` 更新 State，agent loop 不直接维护第二份状态。

```python
state = AgentState()
history = [{"role": "system", "content": build_system_prompt()}]
context = ContextManager(state, history)
tool_executor = ToolExecutor(registry, on_result=state.record_tool)
```

每轮带 `tool_calls` 的 assistant 消息，都会在进入下一轮或返回前追加全部对应的 `role=tool` 消息，避免达到迭代上限时留下协议不完整的消息序列。

### 3.2 上下文预算与裁剪

`CONTEXT_WINDOW` 可在 `config_local.py` 中按模型窗口覆盖。`ContextBudget` 默认保留 15% 给模型输出，历史层最多使用窗口的 45%；system 消息和首条 user task 是保底内容，永不删除。

裁剪顺序固定如下：

1. 旧 tool result 保留首尾并标记省略内容。
2. 仍超限时，从最老的完整轮次开始删除。
3. 一轮中的 `assistant(tool_calls)` 与所有对应 `role=tool` 结果始终成组，绝不拆散。

终端会输出 `[Context]` 日志，展示超限、截断和轮次删除的估算 token 节省量。保底内容本身超过预算时，agent 保留它们并继续请求，不会因裁剪逻辑崩溃。

### 3.3 上下文压缩

当预算超限且存在足够旧的历史轮次时，`ContextManager` 会调用一次不带工具 schema、也不向终端流式输出的摘要请求。摘要结果以 `[Historical Summary]` system 消息注入；近期轮次仍按完整 tool-calling 轮次保留。`AgentState.snapshot()` 每次重新渲染为 `[Structured State]`，用于锚定真实执行事实。

摘要允许有损，State 不依赖摘要推断。摘要请求失败、返回空内容或没有可压缩的旧轮次时，ContextManager 自动退回 trimming；原始 `history` 始终不被修改。

### 3.4 System Prompt

启动时由 `prompt.py` 的 `build_system_prompt()` 组装 `messages[0]`，分三层：

| 层 | 函数/常量 | 内容 |
|---|---|---|
| 身份 | `header(agent_name)` | 告诉模型是哪个 agent（当前只有 build，为多 agent 预留） |
| 行为规范 | `_CORE_RULES` | tone、专业客观性、工具用法、安全约束 |
| 环境信息 | `environment()` | 工作目录、git 状态、平台、日期（动态生成） |

查看当前 system prompt：
```bash
$env:PYTHONPATH="src"; python -c "from mini_agent.prompt import build_system_prompt; print(build_system_prompt())"
```

### 3.5 工具

| 工具 | 参数 | 权限 | 说明 |
|---|---|---|---|
| `calculate` | `expression: str` | allow | 计算数学表达式（仅数字与 `+-*/()` ） |
| `read_file` | `path: str, offset?: int, limit?: int` | allow | 读取文本文件，支持分段读取，输出带行号前缀 |
| `write_file` | `path: str, content: str` | **ASK** | 写文件（完整覆盖），每次执行前问用户 |
| `edit_file` | `path: str, old_string: str, new_string: str, replace_all?: bool` | **ASK** | 精确字符串替换，多匹配时需 replace_all 或更长上下文 |
| `list_dir` | `path?: str` | allow | 列出目录内容，目录加 `/` 后缀，上限 200 条 |
| `grep` | `pattern: str, path?: str, include?: str` | allow | 正则搜索文件内容，返回 `file:line: content`，上限 100 条 |
| `run_shell` | `command: str` | **按命令模式** | 执行 shell 命令，超时 30s，输出截断 2000 字符 |
| `rollback_checkpoint` | 内部 `checkpoint_id` | **仅 RecoveryRuntime** | 不进入模型 schema；恢复一个已授权且未冲突的单文件检查点 |

### 3.6 权限交互

v0.09 权限系统升级为二维匹配：`(tool_name, pattern) -> action`。`PermissionGate` 从工具参数中提取 pattern（文件工具提取 `path`，`run_shell` 提取 `command`，其他返回 `*`），用 `fnmatch` 做 wildcard 匹配。

**规则格式**（`permission.py` 的 `PERMISSION_RULES`）：

```python
# 简单格式（一维兼容，pattern 默认 "*"）
{"write_file": "ask", "read_file": "allow"}

# 复杂格式（二维，按 pattern 细控）
{"read_file": {"*": "allow", "*.env": "deny", "*.env.example": "allow"}}

# run_shell 二维权限（按命令前缀控制）
{"run_shell": {"git *": "allow", "python *": "allow", "*": "ask"}}
```

**匹配规则**：
- `findLast` 语义：从后往前找第一个匹配的规则，后出现的优先级更高
- 复杂格式中 `*` 自动排最前（优先级最低），具体模式排后面（优先级更高）
- 未匹配任何规则时默认 `ask`（安全优先）
- `always` 回复时存 `(tool_name, pattern)` 到 approved，后续同类操作免问

**run_shell 权限规则**（v0.10）：

| 命令模式 | 动作 | 说明 |
|---|---|---|
| `git *` | allow | git 操作放行 |
| `python *` | allow | python 脚本/测试放行 |
| `pip *` | allow | pip 安装放行 |
| `ls *` | allow | 只读命令放行 |
| `cat *` | allow | 只读命令放行 |
| `echo *` | allow | 只读命令放行 |
| `*` | ask | 其他命令每次问用户 |

`write_file`/`edit_file` 执行前会提示：
```
允许执行 write_file({...})? [once/always/reject]
```
- `once`：本次允许，下次再问
- `always`：本轮运行内总是允许该 pattern，不再问
- 其他输入：拒绝执行，工具返回拒绝原因给 LLM

> 二维权限示例：配置 `{"read_file": {"*": "allow", "*.env": "deny"}}` 后，读取 `.env` 文件会被拒绝，其他文件正常放行。

### 3.7 工具调用流程
1. LLM 返回 `tool_calls`（一轮可含多个，代码用线程池并发执行）
2. `ToolExecutor` 先过权限闸门（`PermissionGate.guard`）
3. 通过则调 handler，失败则捕获异常返回错误信息给 LLM
4. 结果作为 `role=tool` 消息回灌，进入下一轮；Executor 回调同时更新 AgentState

如果一批调用中途使任务进入 `blocked`/`failed`，剩余调用仍各自产生拒绝结果并全部回灌，下一轮模型只能解释终态原因；它们不会再次触发权限询问或 handler。

### 3.8 相对路径约定
工具的相对路径（如 `examples/input.txt`）按进程的**当前工作目录**解析，不会自动相对已安装的包目录。使用仓库示例时，建议先进入仓库根目录：
```bash
# 在 agent-from-scratch/ 目录下运行
python -m mini_agent "读取 examples/input.txt"
```

### 3.9 终端输出

CLI 的输入提示固定为 `你 › `。助手正文通过 SSE 流式到达时，只有收到第一个非空 chunk 才显示 `助手 › `，随后直接追加正文；因此空回复不会留下空标题。agent loop 将 `call_llm` 的 `on_content` 回调连接到这一层，最终正文不会再次整段重播。

输出模式由 `OUTPUT_MODE` 控制：

| 模式 | 显示内容 |
|---|---|
| `normal` | 助手正文、工具批次（按工具名计数）和按调用顺序排列的结构化结果 |
| `debug` | normal 内容，加上轮次、工具参数和工具结果；参数与结果各最多 1200 个字符 |
| `quiet` | 隐藏助手正文、工具进度和运行时状态；显式 CLI 通知及权限确认仍显示 |

normal 模式的成功工具结果只显示工具名和安全摘要，不展开结果正文。文件工具摘要只包含 `path`，`run_shell` 只包含 `command`，每个摘要最多 100 个字符；写入内容、grep pattern 等其他参数不显示。失败、拒绝、超时和无效结果显示最多 240 个字符的原因。debug 模式才显示有界的完整结果。上述限制只作用于终端，完整 tool message 仍会回灌模型。

`call_llm` 的 `stream_output` 和 `on_content` 语义如下：

- `stream_output=True` 且传入 `on_content` 时，每个正文 chunk 调用一次回调，不额外写标准输出；回调异常会被隔离。
- `stream_output=True` 且没有回调时，保留独立调用的标准输出兼容行为。
- `stream_output=False` 时不打印正文，也不调用回调，但仍累积并返回完整 assistant message。未显式指定时，quiet 模式默认为关闭流式观察，其余模式默认为开启。

终端呈现是观察层：输出流关闭、重定向或写入失败不会改变工具协议或让执行失败。权限询问仍由 `PermissionGate` 同步发出，即使在 quiet 模式也必须让用户看到提示并输入 `once`、`always` 或 `reject`。

---

## 4. 测试

### 4.1 运行 smoke test
```bash
# 需 PYTHONPATH=src（未 pip install 时）
$env:PYTHONPATH="src"; python tests/test_prompt.py   # system prompt
$env:PYTHONPATH="src"; python tests/test_loop.py      # import 链路
$env:PYTHONPATH="src"; python tests/test_tools.py     # 工具 + 权限
$env:PYTHONPATH="src"; python tests/test_state.py      # AgentState
$env:PYTHONPATH="src"; python tests/test_context.py    # 预算、裁剪与压缩
$env:PYTHONPATH="src"; python tests/test_executor.py   # Executor 结果回调
```
覆盖：system prompt 分层组装、import 链路、registry 注册、AgentState、ContextManager 预算/裁剪/压缩、Executor 结果回调、calculate 正常/非法输入、read_file 分段读取、读写文件、edit_file 精确替换/多匹配安全检查、list_dir、grep、run_shell 执行/退出码/二维权限、权限闸门。

### 4.2 快速验证 import 链路
```bash
$env:PYTHONPATH="src"
python -c "from mini_agent.tools import registry; print([t.name for t in registry.list_tools()])"
# 期望输出: ['calculate', 'read_file', 'write_file', 'edit_file', 'list_dir', 'grep', 'run_shell']
```

---

## 5. 常见问题

### Q1：运行报 502 / 连接网关失败
确认 `config_local.py` 的 `BASE_URL` 和 `API_KEY` 正确，且网络可达配置的网关。
> 注意：必须用 `http.client`（代码已如此），不能用 requests/urllib——网关对 `Accept-Encoding: gzip` 响应异常。`call_llm` 已显式设 `Accept-Encoding: identity` 绕过，并按 `BASE_URL` 的 scheme 选择 HTTP 或 HTTPS 连接。

### Q2：任务没完成就停了
可能触发 `MAX_ITERATIONS=50` 上限，agent 返回 `"达到最大迭代次数"`。可在 `config_local.py` 中调整，但注意长对话会累积上下文。

### Q3：工具失败直接报错退出
agent loop 不对 LLM 或 CLI 顶层异常做兜底；这是为了保持核心路径清晰。工具层（`ToolExecutor.execute`）会捕获 handler 异常并将错误结果回灌给 LLM，但 loop 本身的顶层异常仍会向上抛出。

### Q4：write_file 被拒绝
检查权限交互的输入。选 `reject` 或输错字符会拒绝。重新运行即可。

### Q5：中文乱码（Windows 控制台）
`__main__.py` 已对 win32 设 `sys.stdout.reconfigure(encoding="utf-8")`。若仍乱码，PowerShell 执行 `chcp 65001` 切到 UTF-8。
