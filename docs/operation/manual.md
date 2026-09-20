# mini_agent 操作手册

> 本手册跟随最新版本更新。当前对应版本：**v0.40**（轻量持久 Memory；含此前可靠执行能力）。

## v0.40 轻量持久 Memory

Memory 是按规范化工作区隔离的长期资料集合，与当前任务的 `AgentState`、Plan、verification evidence 和 `/save` session 分开。它不会自动从对话提炼，也不会自动注入每次模型请求；父 Agent 必须显式调用工具。`source` 只是用户或 Agent 提供的来源说明，不代表内容已经独立验证。

父侧提供五个工具：`list_memories(limit=20, offset=0)` 只返回 ID、revision、标题、标签、来源和时间，并附 `total` 与 `next_offset`；`read_memory(memory_id)` 才返回单条正文；`remember(title, body, tags, source)` 新建；`revise_memory(memory_id, expected_revision, title, body, tags, source)` 修订；`forget_memory(memory_id, expected_revision)` 遗忘。查看默认 `allow`，三个修改工具默认 `ask`；授权提示展示受 schema 上限约束的拟写内容、目标 ID 和 revision，并继续支持 `once/always/reject`。

默认存储根目录是 `~/.mini_agent/memory`，可在不提交的 `config_local.py` 中设置 `MEMORY_DIR`。`MEMORY_DIR` 按真实路径校验，不能位于当前工作区内，也不能通过符号链接指向工作区；这样子代理的工作区只读工具无法绕过 Memory 工具边界读取正文。工作区外的自定义目录继续可用。每个规范化工作区真实路径的 SHA-256 作为文件名，JSON 使用 `schema_version=1`。单个工作区最多 256 条；标题、正文、标签、来源上限分别是 120、2000、8×32、240 字符，整个 JSON 不超过 1 MiB。目录权限是 `0700`，文件是 `0600`；缺失文件表示空集合，损坏文件或未知 schema 只报错，不覆盖。

修改流程在工作区专属 lock 下执行“重读 → 校验 → 修改 → 临时文件写入并同步 → 原子替换 → 目录同步”。锁最多等待 2 秒，不会自动抢占遗留锁。若替换已发生但目录同步失败，工具返回独立的 `memory_commit_uncertain`，State 将其归为结果不确定、不可直接重试的失败；当前进程后续记忆写入停止，但只读查看仍可用于核查。工具结果、State 和恢复提示都会说明文件可能已经替换，不能据此断言未写入。`expected_revision` 冲突不会写盘。

记忆修改工具仍是 `effect_class=possible`，所以会预留 generation、使旧 verification 失效，并受只读规划和 crash recovery gate 约束。开启 `/save` 后，相关工具调用会像其他调用一样先提交 `handler_admitted`，再提交 State、对应 `role=tool` 和 boundary；如果 Memory 文件已写入但父结果未提交，恢复不会重放 handler，而是把调用标为不确定事实。Memory 文件不参与 session 回滚。

Memory 不加入子代理固定白名单；子代理仍只能使用 `calculate`、`read_file`、`list_dir`、`grep`。State 摘要和普通终端输出不展开正文，但工具参数仍属于当前模型 history；开启 `/save` 后，单次记忆正文可能随 Context 进入 session。因此本版只是不复制整份 Memory 快照，不承诺 session 绝无某次记忆正文。相关性检索和自动选择上下文候选属于 v0.41，References 属于 v0.42。

### v0.40 配置

```python
MEMORY_DIR = "~/.mini_agent/memory"
```

## v0.39 持久委派交付

v0.38 的调度器允许子代理乱序完成，但结果在父 Context 写入前只存在于当前进程。如果父进程在这个窗口退出，子代理可能已经消耗了模型额度，父 Agent 却没有足够事实重建同一个 `role=tool` 结果。v0.39 把这两个时刻明确分开：结果合同校验通过后先保存为 `result_ready`，再按模型调用顺序交付。

开启 `/save` 后，schema 3 的 `tool_boundary` 可以包含有界的 `pending_delegation_results`。每条记录保存 `invocation_id`、`delegation_id`、结果 ID、规范化结果原文、摘要和 hash；原文按 UTF-8 计数，单条和总区都有上限。它只覆盖尚未提交的 `delegate_task`，不保存子代理的完整 Context、history、隐藏提示词或凭据。

父侧提交的顺序是：先保存委派合同、预留额度和 `created/running` 状态；子结果完成后校验并保存 `result_ready`；轮到该父调用时，把父 attempt、State 中的 `committed` 生命周期、对应 `role=tool` 和 boundary 中的结果引用放入同一次原子 session 提交。前序调用仍在运行时，后序结果可以先变成 `result_ready`，但不会越过父模型调用顺序交付。

恢复 active pending boundary 时，已有且通过 hash、调用身份、State 状态和顺序校验的结果会在派生 session 中按父顺序直接交付，不会重新调用子 LLM。`created/running` 且没有持久结果的记录转为 `interrupted`：不生成子结果 ID/hash，也不标记为 `committed`；对应父工具调用仍由 v0.33 生成不确定结果和待用户处理的 issue。实际子用量未知，聚合预算按原预留上限保守占用，不会因派生新 session 重置，也不会显示为已确认的子实际 usage。

Trace 和 Structured State 只显示父调用、合同摘要、生命周期、usage 和结果引用，不读取子 history 或原始待交付结果。工作区和项目指令仍在恢复时重新检查；旧结果只能作为父 Agent 的调查材料，不能扩大新的委派权限或代替父验证。

## v0.38 有界并行子代理

v0.38 允许父模型在一个 assistant 回合提交多个彼此独立的 `delegate_task`。子代理仍是同步创建、depth=1、只读实例，但 `DelegationScheduler` 以固定 `MAX_CONCURRENCY` 运行已预留的合同。三个调用 A、B、C 在并发上限 2 时会先运行 A/B，B 完成后可启动 C；完成顺序可以乱序，父 `role=tool` 结果和 durable boundary 仍严格按 A、B、C 提交。

父任务聚合预算在子代理启动前按模型顺序批量预留，在结果生成后按实际 usage 结算。预留额度和运行槽位分开：排队的 `created` 合同占用总子代理数和请求额度，但只有已经运行的 worker 占用并发槽位。默认上限是 3 个子代理、并发 2、24 次 LLM 调用、72 次只读工具调用和 96,000 个 token。模型可以在 `delegate_task.budget` 中请求更小的单次额度，不能扩大父账本；provider usage 缺失时沿用保守估算并标记来源，实际 usage 超过预留时照实记账并阻止后续委派。

每个 worker 使用独立的 State、Context、模型绑定、连接、流解析缓冲、预算计数和取消事件，只共享不可变工具定义与只读项目指令快照。一个子代理失败、超时或取消不会取消同批其他任务；父任务整体取消时，Manager 会广播到全部活动和待启动任务。`/new`、`/reset`、EOF、`exit` 和异常退出先请求取消，再有界等待；若仍未收束，CLI 保留旧任务并报告所有未收束的 `subagent_id`。

用户中断子代理调用时，Runtime 先提交每个已完成或取消的父工具结果，再停止父循环；不会继续请求父模型。子代理的上下文压缩若需要额外的模型摘要请求，也必须先检查剩余调用数、token 和时间，并限制摘要输出；预算不足时退回裁剪并结束子任务。

活动或 `result_ready` 委派会阻止父任务进入 `done`，也会阻止 safe point。v0.39 为 `result_ready` 增加了有界原文保存；没有这份原文的旧 v0.38 session 仍按 v0.33 规则把已准入调用交给 issue，不自动重跑子代理。

### v0.38 配置

```python
MAX_SUBAGENTS = 3
MAX_CONCURRENCY = 2
MAX_TOTAL_LLM_CALLS = 24
MAX_TOTAL_TOOL_CALLS = 72
MAX_TOTAL_TOKENS = 96_000
```

`MAX_CONCURRENCY` 必须是正整数且不大于 `MAX_SUBAGENTS`。这些值可以由本地配置覆盖；核心运行时仍只使用标准库线程和锁。

## v0.36 多 provider 与统一协议适配

v0.36 将“模型服务配置”和“协议转换”从 Agent Runtime 中分离出来。`provider` 是服务配置身份，`protocol` 是请求/响应格式，`model profile` 是本地允许模型使用的别名。父 Agent 和 Subagent 在启动前分别冻结 `ModelBinding`，因此可以使用不同 provider/model，但仍进入同一个 `AgentRuntime.run()`。

### 配置格式

推荐在未跟踪的 `src/mini_agent/config_local.py` 中覆盖以下映射；仓库里的 `config_example.py` 只有占位值：

```python
PROVIDERS = {
    "openai-gateway": {
        "protocol": "openai_chat",
        "endpoint": "https://gateway.example.invalid/v1/chat/completions",
        "api_key": "sk-PLACEHOLDER",
    },
    "anthropic-gateway": {
        "protocol": "anthropic_messages",
        "endpoint": "https://gateway.example.invalid/v1/messages",
        "api_key": "key-PLACEHOLDER",
    },
}
MODEL_PROFILES = {
    "parent-default": {
        "provider_id": "openai-gateway",
        "model_id": "model-PLACEHOLDER",
        "context_window": 128000,
        "max_output_tokens": 8192,
    },
    "child-anthropic": {
        "provider_id": "anthropic-gateway",
        "model_id": "model-PLACEHOLDER",
        "context_window": 200000,
        "max_output_tokens": 4096,
    },
}
PARENT_MODEL_PROFILE = "parent-default"
SUBAGENT_MODEL_PROFILE = "child-anthropic"
SUBAGENT_ALLOWED_MODEL_PROFILES = ("child-anthropic",)
```

`openai_chat` 使用完整的 `/chat/completions` endpoint，`anthropic_messages` 使用完整的 `/v1/messages` endpoint。两个映射必须同时定义；只定义一半会在启动时失败。模型只能通过 `delegate_task(model_profile="child-anthropic")` 请求获准别名，不能传 endpoint、key、真实 model ID 或认证头。选择顺序是显式且获准的 profile、子默认 profile、父 profile；未知或越权别名不会回退。

只配置旧的 `BASE_URL`、`API_KEY`、`MODEL` 时，程序会生成 `legacy-default` provider/profile，并在 OpenAI-compatible base URL 后补 `/chat/completions`。新旧配置不能局部混用。真实值不得提交到版本库。

### 协议、请求和 usage

首批支持 OpenAI-compatible Chat Completions 与 Anthropic Messages。OpenAI 使用 Bearer 认证和工具 schema；Anthropic 使用 `x-api-key`、`anthropic-version`、顶层 system、`tool_use` 与 `tool_result` block。两种协议都归一为内部 assistant message 和按 call ID 对应的 `role=tool` 结果；Runtime、工具执行和完成判定不读取 provider 原生字段。

所有请求继续使用标准库 `http.client`，每次请求独立 connection，并发送 `Accept-Encoding: identity`。适配器不自动重试、不执行工具、不修改 State、不判断完成，也不做跨 provider fallback。流式 tool arguments 未闭合、流在结束标志前中断或收到 provider error 时，本次响应会被拒绝，不会进入 executor。

`ProviderUsage.source` 为 `provider`、`estimated` 或 `mixed`。服务方提供完整 usage 时优先使用；缺失字段由保守 token 估算补足。Context 摘要也使用当前父/子 binding，并计入同一个 `UsageMeter`，不会成为免费调用。父、子 Context window 和 output reserve 分别来自各自 profile。

### 脱敏和恢复

State、Context、session、Trace、工具结果和用户可见错误最多保存 profile、provider ID、protocol 和 fingerprint；不保存 API key、真实 endpoint、认证头或真实 model ID。fingerprint 只由非认证配置生成，不包含 API key 或额外请求头，因此轮换凭据不会改变它。v0.36 创建的带模型绑定记录的会话在恢复时重新加载本地 catalog 并比较父 binding；配置缺失或 fingerprint 变化时报告问题，不重发历史请求，也不自动切换 profile。旧版会话没有模型绑定记录，无法核对原模型来源，恢复前需由用户自行核对本地配置。

v0.36 是 v0.37 的 provider 基线：它仍只有一个同步、depth=1、只读 Subagent，但没有本课新增的父聚合预算、后台取消和生命周期记录。v0.38 才引入多个子代理并行，v0.39 才处理跨进程委派结果交付。

## v0.35 共享父子运行循环

父 Agent 和只读 Subagent 现在都通过同一个 `AgentRuntime.run()` 驱动“请求 LLM → 规范化 tool call → 执行或拒绝工具 → 按模型顺序回灌结果 → 判断完成”的循环。父侧使用 `ParentRuntimePolicy` 继续管理 Plan、Repair、进程、session、verification 和完成提醒；子侧使用 `SubagentRuntimePolicy` 管理固定预算、scope 观察、结果合同和一次格式修正。策略不能请求 LLM 或进入 handler。

这次收敛不改变 v0.34 的外部委派能力：仍然只能同步创建一个 depth=1 子代理，子代理只能使用 `calculate`、`read_file`、`list_dir`、`grep`，父 Agent 仍独占修改、权限、主计划、generation、权威 verification 和完成判定。非法 tool call 也会获得唯一的规范化 ID 和对应 `role=tool` 结果；持久化回合仍须先提交 `handler_admitted`，整轮提交完成前不会请求下一次 LLM。

## v0.34 最小受控子代理委派

父 Agent 可以通过 `delegate_task` 同步委派一个单层只读调查。子代理拥有独立的 State、Context、loop、项目提示词和固定 PermissionGate；它只看见 `calculate`、`read_file`、`list_dir`、`grep`，不能写文件、运行 shell、操作进程、调用计划/恢复/验证工具或再次委派。父 Agent 仍独占工作区修改、主 Plan、权限、generation、权威 verification 和完成判定。

工具参数为：`goal`（必填字符串）、`scope`（1–8 个工作区相对路径）、`constraints`、`expected_findings`、`requested_tools`（非空且只能是四个只读工具）、`selected_parent_facts`、`purpose`（`investigation` / `diagnosis` / `crash_investigation`），诊断或崩溃调查还必须带正确的 `source_id`；可选 `budget` 包含 `max_rounds`、`max_llm_calls`、`max_tool_calls`、`max_tokens`、`max_result_bytes`、`timeout_seconds`。

scope 会拒绝绝对路径、`..`、`config_local.py`、符号链接逃逸和工作区外路径；每一次子文件工具调用都会再次检查。selected facts 不得包含明显的 API key、Authorization、Bearer、token、secret 或 password 内容。父任务处于 `awaiting_approval`、`verification_required`、`blocked` 或 `failed` 时拒绝委派；诊断和 crash investigation 必须精确引用当前活动事实。

v0.34 固定护栏为 8 rounds、8 次 LLM calls、24 次 tool calls、32,000 个保守估算 token、12 KiB 最终 JSON 和 120 秒墙钟时间。超限返回 `budget_exhausted`；超时只在同步边界检查，不强制中断已经发出的 HTTP 请求。子代理的失败、超时或预算耗尽也会返回一个结构化 tool result，不自动创建父 FailureEvent。

子代理最终只允许输出以下报告体：

```json
{"summary":"...","findings":[],"evidence":[],"limitations":[]}
```

Runtime 会补充 UUID、父子 ID、`outcome`、usage、时间戳和合同 hash，并校验证据 ID 唯一、finding 引用不悬空、scope 内路径、正行号、合法工具名和 SHA-256 observation hash。非法报告只获得一次受保护格式修正；再次非法或修正阶段再次调用工具时返回 `failed/invalid_result`。子 evidence 不写入父 `verification_evidence`，只能作为父 Agent 的调查输入。

v0.34 每个父工具回合只允许一个 `delegate_task`，同步等待后父 Context 只收到一个按模型顺序提交的 `role=tool` JSON 结果。未实现多子代理并行、后台取消、父任务聚合预算、持久化 DelegationRecord 或跨 session 恢复；`/new`、`/reset` 和退出会在当前同步 handler 返回后才继续处理。

## v0.33 会话持久化、崩溃恢复与安全交接

### 开启、更新和关闭会话

`/save` 是明确的本地落盘选择。它只能在有活动任务时使用：第一次调用创建随机 `session_id` 并显示 ID，之后再次调用更新同一个文件。开启后，完整的 agent 回合返回 CLI、计划审批等状态决定完成时，Runtime 会自动保存一份 `active` 安全点；替换前保存失败不会覆盖旧文件，也不会显示“已保存”。文件已替换但最终同步或锁清理失败时，CLI 会单独报告提交状态未确认及 session ID，不能据此断言磁盘仍是旧版本或新版本已持久化。

```text
/save
已保存会话：<session_id>
```

`/new <任务>`、`/reset`、EOF、`exit` 和正常结束会先执行现有的有界后台进程清理，再把旧会话写成 `clean`。清理失败、stdin 写入在途、仍有活动进程、未结算 attempt 或异常退出都不会提交 `clean`；CLI 会保留旧任务或报告具体原因。任务切换后不会把旧 session 身份自动带到新任务，新任务需要再次输入 `/save`。

### 文件位置、格式和隐私边界

默认文件位于当前用户的 `~/.mini_agent/sessions/`，目录只允许当前用户访问，每个 session 是一个不超过 16 MiB 的 JSON 文件。写入使用同目录临时文件、文件 `fsync` 和 `os.replace`，并用同名独占锁文件拒绝并发写入；遗留锁不会被自动抢占。新写入 envelope 使用 `schema_version=3`，包含 `session_id`、包版本、规范化工作区根目录、保存时间、保存 generation、`active/clean`、State、Context、工作区清单、`tool_boundary` 和覆盖其他字段的 SHA-256 完整性值。schema 1 仍可读取诊断，schema 2 的 clean 安全点仍可恢复，并在恢复占用时升级为 schema 3。

State 导出的是权威计划、执行、失败、恢复、generation、预算私有计数、原始恢复参数和检查点**元数据**；不会写入锁、`ProcessManager`、`Popen`、线程或检查点前镜像字节。Context 导出普通任务历史、摘要、压缩标记、摘要轮数和待消费 Runtime Notice，但不导出受保护的 system prompt。assistant 工具调用必须与按序的 `role=tool` 结果一一配对。

会话可能包含普通对话、文件内容和工具输出，应按本地敏感数据看待。`write_process` 的 assistant 参数会保留合法 JSON 形状和 `tool_call_id`，但将 `function.arguments.input` 替换为明确的脱敏占位。如果非空正文也出现在普通历史文本、摘要或 Runtime Notice 中，本次保存会拒绝，而不是把正文写入文件或盲目替换其他文字；空输入发送 EOF 不改写摘要。通用历史文本不会被笼统地宣称“无敏感信息”，API key、`BASE_URL`、`MODEL` 和 `config_local.py` 不会被 SessionStore 读取或序列化。

### 读取校验与故障诊断

SessionStore 提供读取、大小检查、schema 检查、字段/引用检查、工具结果配对检查和 SHA-256 校验。使用下面的命令请求恢复：

```bash
PYTHONPATH=src python -m mini_agent --resume <session_id>
```

恢复只接受 schema 2/3 中 `schema_version`、`save_kind=safe_point`、`handoff_status=clean` 且工作区清单仍匹配的完整 committed 会话。CLI 会先构造新的 State、Context、工具注册表、ProcessManager 和 PermissionGate，再在 session 独占锁内复核原提交与工作区清单，把会话改写为 schema 3 的后继 generation `active` 版本；提交成功后显示原任务状态并等待输入，不自动请求 LLM。工作区变化、无法完整检查的路径、`active` 会话、pending tool boundary、损坏文件和锁竞争都会在调用 LLM 或工具前拒绝。恢复后的正常退出才重新写入 `clean`；异常退出留下 `active`，不能再次直接恢复。

v0.30 的 schema 1 会话仍可读取诊断，但不能续跑。旧验证资格在恢复时清空，`verification_history` 只用于审计和 Trace 回放；恢复后的任务需要独立验证。旧 PID、旧 `process_id` 和没有前镜像字节的旧 `ready` checkpoint 只保留审计记录，不能控制进程或执行回滚。

- `session 文件不存在`：检查 `~/.mini_agent/sessions/` 和显示的 `session_id`，不要手工拼接其他路径。
- `schema 1 会话只供诊断`：使用当前代码重新完成一个 `/save` 和正常 clean 交接，之后再用 `--resume`。
- `工作区检查失败`：恢复前还原任务涉及文件和目录；清单按结构化文件参数、`files_changed`、失败和 checkpoint 记录建立，递归 `grep` 覆盖搜索范围内未匹配的文件。任务路径含符号链接或搜索范围超限时只保留诊断用途；shell 命令文本不会被猜测为路径或副作用。
- `只有 clean 会话可以恢复`：active 会话可能对应进程清理或异常退出后的不确定窗口，先保留它用于诊断。
- `session SHA-256 校验失败`、`JSON` 或引用错误：文件可能损坏或被手工修改；保留原文件以便诊断，不能把它当作可继续任务。
- `session 文件超过上限`：历史或工具输出过大；先保留旧提交，不删除或截断半个工具回合。
- `session 已被其他写入者锁定`：另一个 CLI 可能正在写入，或存在需要人工检查的遗留锁；Runtime 不会自动抢锁。
- `会话保存失败`：若失败发生在替换前，上一份文件保持不变；CLI 不会伪造成功提示。若是清理失败，先处理报告中的 `process_id`、PID 或 stdin 原因。
- `会话提交状态未确认`：文件可能已经换成新内容，但目录同步或锁清理没有完成；用显示的 session ID 检查磁盘文件和独占锁，不要假定旧提交仍在。

## v0.30 会话持久化与安全点

v0.30 引入 `/save`、本地 schema 1 session、脱敏 history、原子安全点和 `active/clean` 生命周期。v0.31 增加 schema 2 和工作区清单；v0.32 增加 schema 3 的持久化工具边界；v0.33 在 active pending 边界上建立崩溃恢复交接。clean 安全点仍按原流程恢复，pending 半轮则派生新 session，不覆盖源文件，也不自动补做 handler。

### `--resume` 的两种分流

```bash
PYTHONPATH=src python -m mini_agent --resume <session_id>
```

- `clean + safe_point`：检查 workspace manifest，claim 原 session，创建新的 resume generation，等待用户输入。
- `active + schema 3 + pending tool_boundary`：显示源 session、派生 session、workspace 变化和 issue 分类；claim 后只在新 session 上运行。
- schema 1、schema 2 active、没有 pending 证据或损坏的 session：拒绝进入运行时，不调用 LLM、handler 或 PermissionGate。

崩溃恢复使用私有的 `crash_recovery_claims.json` sidecar，位置与 session 目录相同。它只保存源 session ID、源完整性摘要、派生 ID、恢复 ID 和 `preparing/committed` 阶段，不保存工具原始参数、shell 命令、stdin 或模型配置。先提交 `preparing` 意图，再写入并校验派生 session，最后提交 `committed`；中途失败可用同一派生 ID 重试。sidecar 或派生 session 的耐久性未确认时，不向 CLI 返回可运行对象；已 `committed` 的同一源完整性重复 claim 会报告已有的派生 session。

### `/resolve`

恢复报告中的每个 issue 都必须逐项处理，一次只接受一个 issue：

```text
/resolve <issue_id> investigate <必填反馈>
/resolve <issue_id> continue <必填反馈>
/resolve <issue_id> block <必填反馈>
```

`investigate` 只记录用户决定并允许只读调查；`continue` 要求本次恢复 generation 中已有成功、获准、`effect_class=none` 的调查 attempt；`block` 立即终止任务并保留其他 issue。所有 issue 都 continue 后会创建 `crash_recovery` replan trigger，必须重新提交或复核计划，再重新授权和独立 verification。未结算 issue 存在时，副作用工具、verification、计划提交、普通输入和完成状态均会被拒绝。

工作区变化不能证明 handler 执行或未执行，但会生成一个必须调查并由用户结算的 `workspace-drift` issue；准备和 claim 阶段比较路径、类型、可用性、目录成员及内容 hash 的结构化摘要。已准入调用不重放：`effect_class=none` 记录为 `uncertain_state_or_result`，可能有副作用的调用记录为 `uncertain_side_effect`；未进入 handler 的调用才会得到 `interrupted_before_handler` 的明确未执行结果。旧 PID、进程 ID 和 stdin 写入线程不跨进程继承，可能遗留的进程以 `orphaned` 事实保留，并通过独立 process issue 阻止任务完成；用户 `continue` 不会恢复 PID 控制权。

## v0.29 后台进程有界文本输入

`start_process(command, cwd?, stdin_mode?)` 的 `stdin_mode` 默认是 `closed`，保持 v0.28 及以前的 EOF 行为；只有显式设为 `pipe` 的进程才接受 `write_process(process_id, input, close_stdin?)`。输入必须是 UTF-8 文本，单次编码后最多 4096 字节；空输入只允许配合 `close_stdin=true`，用于单独发送 EOF。

`write_process` 的写入与关闭共用一次独立权限检查，默认 `ask`，作用类别为 `possible`。授权提示只显示 `process_id`、编码后的字节数和关闭标志，不显示正文；未知或跨任务 ID 在授权及 generation 预留前拒绝，参数错误也不进入授权。已启用但已经关闭、提前退出或已有在途写入的进程会在授权后、generation 预留前返回明确状态。每个进程串行处理写入，专用线程最多等待 2 秒；前一次写入仍在途时返回 `write_pending`，不能据此重试或把它当作已写入。成功结果的 `written_bytes` 是完成写入与 flush 后的字节数；管道错误时该字段为 0，`delivery_uncertain=true` 表示可能已投递部分字节，不能自动重试。结果只包含进程 ID、写入字节数、`stdin_state`、`closed` 及有界状态原因，不回灌正文。

成功写入不构成 verification。继续用 `read_process` 或 `wait_process` 观察响应；进程退出、控制和 `/new`、`/reset`、EOF、`exit` 或异常清理时都会有界回收写入线程和 stdin 管道。活动进程或在途写入都阻止任务进入 `done`；清理无法确认时保留旧任务登记并报告进程 ID、PID 和原因。此版只支持管道字节流，不支持 PTY、终端回显、控制字符、终端尺寸或交互式 shell 语义。

## v0.28 后台进程控制与任务收口

`terminate_process(process_id)` 向当前任务登记的进程请求正常终止；`kill_process(process_id)` 强制结束。两个工具独立授权，默认均为 `ask`，且只接受任务专属 ID，不接受系统 PID。未知、过期或跨任务 ID 在权限询问前返回 `unknown_process_id`，不预留执行 attempt 或 generation。有效调用获准后按可能有副作用的工具预留 generation；计划只读阶段、待批准计划和修复阶段的限制仍然适用。

正常终止和强制结束各最多等待 2 秒。`terminated` 或 `killed` 表示直接子进程、受管进程组及输出管道均已确认退出；`still_running` 表示本次未确认退出，进程仍阻止任务完成，可继续观察或尝试 `kill_process`。`already_exited` 表示调用时已自然退出。控制失败返回 `control_failed`，不会产生最终进程事件。Windows 只能确认直接子进程及管道，结果会注明无法确认任意 shell 派生进程树。

前台同步点为确认的主动退出提交一条 `terminated` 或 `killed` 事件，引用启动 attempt 和控制 attempt，并开启退出后的 generation。旧验证随之失效。只有进程稳定退出、计划步骤完成、修复义务解决，并在新 generation 独立执行 `run_shell(purpose="verification")` 后，任务才能完成。进程控制不能替代诊断阶段的合法 `recover` 或 `request_replan`。

## v0.27 后台进程观察与有界等待

`start_process` 返回的 `process_id` 可在同一任务的后续轮次使用。`get_process(process_id)` 查询状态、最终退出码和 stdout/stderr 的累计**字节**位置，不消费日志；`list_processes()` 列出本任务登记的进程元数据，结果过长时从最早记录开始省略并报告 `omitted_count`。未知、过期和跨任务 ID 返回 `unknown_process_id`，不能用系统 PID 查询其他进程。

`read_process(process_id, max_chars?)` 分别读取 stdout 和 stderr 自上次成功读取以来的新内容。默认合计最多 2000 字符，参数范围 1–4000；完整 JSON 最多 8000 字符。返回 `next_stdout_offset`、`next_stderr_offset`、逐流 `*_output_gap` 和 `*_lost_bytes`。位置按原始字节计，UTF-8 字符跨收集块时不会重复或丢失；非法字节显示为替代字符。每条流只保留最近 64 KiB，读取太晚时须检查缺口标记。日志正文只在本次工具结果中出现，不进入长期 State 或 Trace。

`wait_process(process_id, timeout_ms?)` 必须独占一个工具回合。默认等待 1000 毫秒，可设 0–30000 毫秒；遇到未读输出返回 `output_available`，确认退出返回 `exited`，二者都不消费日志。超时返回 `still_running`，完整工具结果回灌后任务进入 `awaiting_process` 交回 CLI；再次输入会先同步进程事实，再继续原任务。超时不是进程失败、验证证据或完成状态。自然非零退出单独记录一次 `FailureEvent`，引用启动 attempt 和退出事件，并进入现有诊断流程；退出还会使运行期间的旧验证失效。

## v0.26 后台进程启动与任务边界

长命令会让同步执行器一直等到命令退出；开发服务器、文件监听器或持续构建因此无法在同一个 Agent 任务中继续工作。v0.26 增加 `start_process(command, cwd?)`：它沿用 `run_shell` 的 shell 字符串语义，但创建进程后立即返回任务专属的 `process_id`。`run_shell` 仍保持同步执行、30 秒超时和原有 `[exit=N]` 返回格式。

后台进程的运行态由 CLI 生命周期内的 `ProcessManager` 持有。State 只保存可快照的 `ProcessRecord` 和 append-only `ProcessEvent`：任务 ID、启动 attempt、generation、PID、状态、时间、退出码、stdout/stderr 累计字节位置和最终事件 ID；不保存 `Popen`、管道、线程、完整日志或输入正文。每个任务最多有 4 个活动进程，每条输出流最多保留 64 KiB；v0.29 以前启动时 stdin 连接 `DEVNULL`，v0.29 只有显式 `stdin_mode="pipe"` 才建立 stdin 管道。

成功结果是有界 JSON，例如：

```json
{"process_id":"proc-1","pid":12345,"status":"running","start_attempt_id":"a-3"}
```

这只表示进程已经创建。自然零退出记录 `exited`，自然非零退出记录 `failed`，并引用启动 attempt；同步退出会使当前 verification 失效、开启一个新的 generation。进程状态、启动结果和缓冲位置都不是 verification evidence，退出后仍需用独立的 `run_shell(purpose="verification")` 验证最终状态。

模型在后台进程仍运行时给出无 `tool_calls` 的文本，Runtime 会把任务置为 `awaiting_process`，交回 CLI，不把它判为 `done`，也不额外消耗一轮 LLM。CLI 显示 task/process ID、累计输出位置和剩余计划、修复、验证义务；下一次用户输入会先同步退出事实，再恢复原任务。

`/new`、`/reset`、EOF、`exit`、`KeyboardInterrupt` 和异常退出都会在清空 State 前清理当前任务的进程：先请求正常终止并等待最多 2 秒，再强制结束并等待最多 2 秒；确认进程结束后有界等待收集线程读到 stdout/stderr 的 EOF，才关闭管道。已确认退出的进程只回收句柄，不会再次向旧进程组 ID 发信号，以免该 ID 被系统复用后误伤无关进程。POSIX 还须确认受管进程组已消失；外层 shell 先退出时，同组后代或仍持有管道的后代都会继续阻止任务完成和释放活动额度。Windows 只能确认直接子进程与两条管道，无法证明任意 shell 派生进程树已结束；因此有后台进程登记时报告清理不完整，`/new` 和 `/reset` 保留旧任务，用户须在不再需要该任务后退出 CLI。清理不完整时保留旧任务和 Manager 登记信息，报告 PID、process ID 和原因；POSIX 无法控制脱离进程组且关闭继承管道的后代。

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
| `BASE_URL` | `https://gateway.example.invalid/v1` | 旧配置兼容路径的 LLM 网关地址 |
| `API_KEY` | `sk-PLACEHOLDER_API_KEY` | 网关密钥占位值 |
| `MODEL` | `model-PLACEHOLDER` | 旧配置兼容路径的模型名占位值 |
| `PROVIDERS` / `MODEL_PROFILES` | `{}` / `{}` | 两者同时为空时启用旧三元组兼容；推荐填写 v0.36 多 provider 映射 |
| `PARENT_MODEL_PROFILE` | `default` | 新配置中的父 profile 别名 |
| `SUBAGENT_MODEL_PROFILE` | `None` | 子默认 profile；为空时按白名单回退到父 profile |
| `SUBAGENT_ALLOWED_MODEL_PROFILES` | `("default",)` | `delegate_task.model_profile` 可请求的 profile 白名单 |
| `MAX_ITERATIONS` | `50` | agent loop 最大轮数 |
| `CONTEXT_WINDOW` | `128000` | 模型上下文窗口的 token 估算值 |
| `OUTPUT_MODE` | `normal` | 终端输出级别：`quiet`、`normal` 或 `debug` |
| `MAX_SESSION_FILE_BYTES` | `16777216` | 单个 session JSON 文件上限；默认 16 MiB |
| `MAX_SUBAGENTS` | `3` | 一个父任务最多创建的子代理数 |
| `MAX_CONCURRENCY` | `2` | 同时运行的子代理数，必须不大于 `MAX_SUBAGENTS` |
| `MAX_TOTAL_LLM_CALLS` | `24` | 父任务委派子代理可消耗的 LLM 调用总数 |
| `MAX_TOTAL_TOOL_CALLS` | `72` | 父任务委派子代理可消耗的只读工具调用总数 |
| `MAX_TOTAL_TOKENS` | `96000` | 父任务委派子代理可消耗的输入和输出 token 总数 |

进程运行参数是固定实现默认值，不需要写入配置：每任务最多 4 个活动进程；每进程 stdout、stderr 各保留最多 64 KiB；任务边界正常终止和强制结束各等待最多 2 秒。v0.27 的读取和等待额度见本手册开头。

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
`/reset` 清空当前任务和任务级状态；输入 `/save` 可显式开启本地 session 保存。两个任务边界命令会先清理该任务登记的后台进程，清理不完整时保留旧任务并报告原因。会话内已经授予的权限和项目级指令不受影响。

---

## 3. 当前能力（v0.40，含 v0.18.1 完成提醒修复）

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
| `status` | `running` / `awaiting_process` / `done` / `blocked` / `failed` |
| `task_id` | 每次 `begin_task` 分配且不复用的任务 ID |
| `processes` / `process_events` | 当前任务的后台进程投影与 append-only 生命周期事件，含 stdin 能力、状态和在途标记 |
| `awaiting_process` | 进程仍运行或 stdin 写入未收束时的非终态 CLI 交接信息 |
| `todos` | active Plan Contract 的只读兼容投影 |
| `plan_revisions` / `plan_progress_history` | 不可变计划结构历史与独立步骤进度事件；完整历史不直接注入 LLM 上下文 |
| `planning_state` / `active_plan` | 当前计划阶段、active revision 和应用进度事件后的执行视图 |
| `replan_triggers` / `user_plan_decisions` | 活动 trigger、四类来源、用户决定及恢复前终态原因 |
| `loop_stagnation` | progress epoch、连续无进展回合数、短指纹、告警类型和最近原因 |
| `verification_evidence` | 最近 verification 命令、退出码与结果；只有当前 generation 的 `[exit=0]` 才算通过 |
| `verification_history` | append-only 的任务内 verification 审计记录；跨 generation 回放使用，不参与完成判定或 LLM 上下文 |
| `failures` / `recovery_actions` | 最近失败的工具、failure/attempt/generation、分类与可重试性，以及恢复动作状态和因果引用 |
| `repair_loop` | 当前修复阶段、活动 failure/recovery、已使用/剩余 repair cycle 和要求的下一动作 |
| `delegations` / `delegation_budget` | 委派交付状态、outcome、短摘要/hash、活动委派和父任务剩余额度；不含子 history |
| `checkpoints` / `rollback_checkpoints` | 单文件前后镜像元数据；后者只列出当前可回滚的 `ready` 检查点，不含文件内容 |
| `budgets` / `recovery_notice` | replan、无进展、失败重试、参数指纹、恢复动作和 repair cycle 的剩余额度及当前恢复提示 |

v0.31 另有一份独立的 session 导出，不改变上表的公开 `snapshot()` 或 Trace 投影；导出格式是保存和恢复协议，不是 Trace 回放视图。

所有 LLM 请求都经 `ContextManager.prepare_messages()`。它按 `len(text) // 3` 估算 token，保留输出空间，并在超限时先截断最老的 tool result、再删除最老的完整历史轮次。工具执行结果通过 `ToolExecutor(on_result=state.record_tool)` 更新 State，agent loop 不直接维护第二份状态。

```python
state = AgentState()
history = [{"role": "system", "content": build_system_prompt()}]
context = ContextManager(state, history)
tool_executor = ToolExecutor(registry, on_result=state.record_tool)
```

每轮带 `tool_calls` 的 assistant 消息，都会在进入下一轮或返回前追加全部对应的 `role=tool` 消息，避免达到迭代上限时留下协议不完整的消息序列。

### 3.2 持久化工具执行边界

输入 `/save` 后，session 文件升级为 schema 3。它仍然是单个原子 JSON 文件，同时保存 State、Context 和最后一个工具回合的 `tool_boundary`；不会额外创建 journal。`session_generation` 是每次提交递增的序号。

工具回合的持久化顺序是固定的：先写入 assistant 消息和按模型顺序排列的 pending call；通过权限、参数和计划前置检查后，在 handler 真正开始前提交 `handler_admitted`；handler 返回后，先记账 State，再追加对应的 `role=tool`，然后原子提交该 call 的结果。对 `delegate_task`，子结果先进入 boundary 的 `result_ready` 区；交付时再把父 attempt、State 的 `committed` 生命周期、`role=tool` 和边界状态一起提交。所有 call 都 committed 后，才允许再次请求 LLM。`run_shell` 包括 `purpose="verification"` 始终按 `possible` 记录；`recover` 可能进入另一个工具或执行回滚，外层边界也保守记录为 `possible`，实际恢复 attempt 和 generation 仍由 `RecoveryRuntime` 管理。

这条边界解决的是“工具已经执行，但结果还没有落盘”的窗口。串行副作用工具按模型顺序完成；effect class 为 `none` 的调用可以并发产生结果，但准入提交和主线程的结果提交仍按模型顺序进行。权限拒绝、参数错误、计划校验失败和 handler 异常都各自产生确定的工具结果；存储失败会停止后续 handler 与模型请求，不会被包装成普通工具失败。

进程自然退出属于异步 State 事实，会单独提交，不伪造新的工具回复。`write_process.input` 只在保存时保留占位符；如果正文出现在其他持久化文本中，保存会拒绝。

v0.33 增加了 pending 边界的崩溃恢复交接，v0.39 又能直接消费已持久化的委派原结果。`--resume` 仍保留 v0.31 的 clean safe point 路径；遇到 active pending boundary 时，新的 session 会保存按模型顺序合成的 `role=tool` 结果和恢复 State，但不会重放旧调用。恢复 generation 的当前 verification 资格会清除，append-only verification history、FailureEvent、RecoveryAction 和计划历史仍保留供审计。

### 3.3 上下文预算与裁剪

`CONTEXT_WINDOW` 可在 `config_local.py` 中按模型窗口覆盖。`ContextBudget` 默认保留 15% 给模型输出，历史层最多使用窗口的 45%；system 消息和首条 user task 是保底内容，永不删除。

裁剪顺序固定如下：

1. 旧 tool result 保留首尾并标记省略内容。
2. 仍超限时，从最老的完整轮次开始删除。
3. 一轮中的 `assistant(tool_calls)` 与所有对应 `role=tool` 结果始终成组，绝不拆散。

终端会输出 `[Context]` 日志，展示超限、截断和轮次删除的估算 token 节省量。保底内容本身超过预算时，agent 保留它们并继续请求，不会因裁剪逻辑崩溃。

### 3.4 上下文压缩

当预算超限且存在足够旧的历史轮次时，`ContextManager` 会调用一次不带工具 schema、也不向终端流式输出的摘要请求。摘要结果以 `[Historical Summary]` system 消息注入；近期轮次仍按完整 tool-calling 轮次保留。`AgentState.snapshot()` 每次重新渲染为 `[Structured State]`，用于锚定真实执行事实。

摘要允许有损，State 不依赖摘要推断。摘要请求失败、返回空内容或没有可压缩的旧轮次时，ContextManager 自动退回 trimming；原始 `history` 始终不被修改。

### 3.5 System Prompt

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

### 3.6 工具

| 工具 | 参数 | 权限 | 说明 |
|---|---|---|---|
| `calculate` | `expression: str` | allow | 计算数学表达式（仅数字与 `+-*/()` ） |
| `read_file` | `path: str, offset?: int, limit?: int` | allow | 读取文本文件，支持分段读取，输出带行号前缀 |
| `write_file` | `path: str, content: str` | **ASK** | 写文件（完整覆盖），每次执行前问用户 |
| `edit_file` | `path: str, old_string: str, new_string: str, replace_all?: bool` | **ASK** | 精确字符串替换，多匹配时需 replace_all 或更长上下文 |
| `list_dir` | `path?: str` | allow | 列出目录内容，目录加 `/` 后缀，上限 200 条 |
| `grep` | `pattern: str, path?: str, include?: str` | allow | 正则搜索文件内容，返回 `file:line: content`，上限 100 条 |
| `run_shell` | `command: str` | **按命令模式** | 执行 shell 命令，超时 30s，输出截断 2000 字符 |
| `start_process` | `command: str, cwd?: str, stdin_mode?: "closed" / "pipe"` | **独立按命令模式 ASK** | 启动后台 shell 命令，立即返回 `process_id`；默认 stdin 关闭，每任务最多 4 个活动进程 |
| `get_process` | `process_id: str` | allow | 查询本任务进程的状态、退出码与两流累计位置 |
| `read_process` | `process_id: str, max_chars?: int` | allow | 读取两流新增输出、下一字节位置与缓冲缺口 |
| `list_processes` | 无 | allow | 列出本任务登记的有界进程元数据 |
| `wait_process` | `process_id: str, timeout_ms?: int` | allow | 独占回合，有界等待未读输出或退出；超时交回 CLI |
| `write_process` | `process_id: str, input: str, close_stdin?: bool` | **ASK** | 向显式开启管道的进程写入最多 4096 字节 UTF-8 文本；可单独发送 EOF；单次最多等待 2 秒 |
| `rollback_checkpoint` | 内部 `checkpoint_id` | **仅 RecoveryRuntime** | 不进入模型 schema；恢复一个已授权且未冲突的单文件检查点 |

### 3.7 权限交互

v0.09 权限系统升级为二维匹配：`(tool_name, pattern) -> action`。`PermissionGate` 从工具参数中提取 pattern（文件工具提取 `path`，`run_shell` 和 `start_process` 各自提取 `command`，其他返回 `*`），用 `fnmatch` 做 wildcard 匹配。`start_process` 有自己的规则表，不继承 `run_shell` 已放行的命令。`write_process` 使用自己的 `ask` 规则，不继承启动命令或其他工具的授权。

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

`start_process` 默认对所有命令 ASK。用户选择 `always` 后只保存 `start_process` 对应的命令 pattern；它不会改变同一命令在 `run_shell` 中的授权。

`write_process` 的授权提示只显示安全元数据，例如：

```
允许执行 write_process(process_id=proc-1, bytes=7, close_stdin=True)? [once/always/reject]
```

输入正文不会出现在提示中。`write_pending` 表示两秒内尚不能确认投递完成；等待下一次 `get_process` 或 `wait_process` 的状态，不要重复投递同一输入。

`write_file`/`edit_file` 执行前会提示：
```
允许执行 write_file({...})? [once/always/reject]
```
- `once`：本次允许，下次再问
- `always`：本轮运行内总是允许该 pattern，不再问
- 其他输入：拒绝执行，工具返回拒绝原因给 LLM

> 二维权限示例：配置 `{"read_file": {"*": "allow", "*.env": "deny"}}` 后，读取 `.env` 文件会被拒绝，其他文件正常放行。

### 3.8 工具调用流程
1. LLM 返回 `tool_calls`（一轮可含多个，代码用线程池并发执行）
2. `ToolExecutor` 先过权限闸门（`PermissionGate.guard`）
3. 通过则调 handler，失败则捕获异常返回错误信息给 LLM
4. 结果作为 `role=tool` 消息回灌，进入下一轮；Executor 回调同时更新 AgentState

`start_process` 的成功结果只证明句柄已创建。Runtime 在每轮上下文、完整工具结果回灌、完成判断和用户恢复前同步进程；自然退出会记录最终事件并使旧 verification 失效。模型可用 v0.27 的观察工具查询状态和新增日志；重复空结果不算新事实。模型在仍有活动进程或 stdin 写入在途时只回复文本，或 `wait_process` 超时，都会进入 `awaiting_process`，CLI 显示继续方式而不把任务标为完成。v0.28 提供模型可调用的 terminate_process/kill_process；v0.29 增加显式管道的 write_process；任务边界清理仍由 Runtime 执行。写入成功后必须继续观察，不能替代独立 verification。

如果一批调用中途使任务进入 `blocked`/`failed`，剩余调用仍各自产生拒绝结果并全部回灌，下一轮模型只能解释终态原因；它们不会再次触发权限询问或 handler。

### 3.9 相对路径约定
工具的相对路径（如 `examples/input.txt`）按进程的**当前工作目录**解析，不会自动相对已安装的包目录。使用仓库示例时，建议先进入仓库根目录：
```bash
# 在 agent-from-scratch/ 目录下运行
python -m mini_agent "读取 examples/input.txt"
```

### 3.10 终端输出

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
PYTHONPATH=src python -c "from mini_agent.state import AgentState; from mini_agent.tools import create_registry; print([t.name for t in create_registry(AgentState()).list_tools()])"
# 期望输出包含: calculate, read_file, write_file, edit_file, list_dir, grep, run_shell, start_process, write_process
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

### Q6：后台进程显示 awaiting_process
这是非终态交接，表示模型已经暂停回复、`wait_process` 超时，或 stdin 写入仍在途，但任务登记的进程尚未完全收束。继续输入即可恢复原任务；恢复前 Runtime 会先同步进程。可用 `read_process` 读取日志、`get_process` 查看 stdin 状态；可用 terminate_process 或 kill_process 控制当前任务的进程；`/new`、`/reset` 和退出 CLI 时会清理当前任务的进程及写入线程。
