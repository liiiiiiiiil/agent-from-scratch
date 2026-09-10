# mini_agent 操作手册

> 本手册跟随最新版本更新。当前对应版本：**v0.19.1**（Checkpoint / Rollback 审阅修复；含 v0.18.1 Recovery Policy 修复）。

## v0.19 Checkpoint / Rollback

v0.19.1 修复了四个边界：checkpoint 元数据会出现在 critical Structured State 和文件工具结果中；形成明确前后镜像的文件 handler 异常仍可请求 rollback；所有 internal 工具都会在恢复额度和 generation 激活前被拒绝为 retry/adjust 目标；原始 mode 为 `0` 时按 `0` 恢复。

`write_file` 和 `edit_file` 在权限放行、attempt/generation 预留后，会为单个工作区内普通文件保存前镜像。前镜像最多 `MAX_CHECKPOINT_BYTES`（默认 1 MiB），不存在的文件记录为 `absent` tombstone；符号链接、工作区外路径、目录/特殊文件、无效父目录和读取失败只会让 checkpoint 变为 `unavailable`，不会改变原文件工具行为。

模型通过 `recover(action="rollback", checkpoint_id=...)` 请求恢复。`rollback_checkpoint` 是内部工具，不出现在 LLM schema 中，也拒绝模型直接调用；RecoveryRuntime 会使用 checkpoint 保存的规范化相对路径经过同一 PermissionGate 授权。恢复前重新计算当前文件的类型和 SHA-256，发现外部修改就拒绝写入并进入 `blocked`。普通文件使用同目录临时文件、原 mode 和 `os.replace` 原子恢复；absent tombstone 只在后镜像仍匹配时删除目标。

恢复成功只证明恢复操作本身完成：它会打开新的 generation、清除旧 verification evidence，并要求下一轮独立 verification。checkpoint 元数据保留在 Structured State 中，但不包含前镜像内容或绝对路径；`/reset` 和 `/new` 会清除本任务全部 checkpoint 及私有字节。

配置项：

| 配置项 | 默认值 | 说明 |
|---|---:|---|
| `MAX_CHECKPOINT_BYTES` | `1_048_576` | 单个前镜像的最大字节数，可在 `config_local.py` 覆盖 |

## v0.18 Recovery Policy

`recover` 支持 retry、adjust、ask、block。v0.18.1 修复了拒绝记录和预算边界：schema、引用、参数、预算或权限拒绝都会只记录一次带 `recovery_id` 的 rejected action，不推进 generation。目标权限检查前先在 State 锁内预留额度；获准后才打开后继 generation 和 attempt。权限拒绝释放未使用的目标执行及重试额度，但恢复申请仍计数，连续无效申请达到恢复动作上限也会阻塞。恢复目标复用当前会话的同一把 PermissionGate，不重复询问同一次授权；恢复后必须独立 verification。

`edit_file` 的没有匹配和多处匹配是确定性的参数前置条件失败（`error_kind=edit_no_match` / `edit_multiple_matches`），不会改文件或进入未知副作用阻塞；已获准进入 handler 的其他异常仍会使 possible-effect generation 保持推进。Structured State 在 `running` 时也显示恢复通知、最近失败/恢复动作和脱敏预算。进入 `blocked` 或 `failed` 后，Executor 不再询问权限或运行 handler，当前批次剩余调用以 `task_terminal` 结果逐一回灌。

## v0.17 失败模型

每个任务从 generation 0 开始。可能产生副作用的工具（文件写入、编辑和 execution shell）在权限放行后、handler 前原子推进 generation；即使 handler 失败也不会回退。Executor 产出结构化 `ExecutionResult`，State 保存 `ExecutionAttempt`、`FailureEvent` 和绑定 generation 的 verification evidence。参数以 canonical JSON 的 SHA-256 指纹计数，Structured State 仅显示 hash/脱敏摘要。全只读回合可并发，包含副作用的回合按模型顺序串行提交；verification 不得与副作用同轮。

## v0.16 计划驱动执行（Plan-driven Execution）

复杂任务通常按 Plan → Execute → Observe → Verify 推进；如果观察结果或验证结果暴露问题，模型再 Replan（重排 Todo）并继续执行。文件修改以及所有实际执行的 `run_shell(purpose="execution")` 都按可能改变环境处理，会使旧验证失效；使用 `run_shell` 的 `purpose="verification"` 且退出码为 0 的结果作为完成证据，建议将最终测试或检查作为最后一个 verification 调用。

v0.16.1 修复了完成提醒：当模型在 Todo 未完成或仍需验证时输出阶段性文本，运行时注入明确的 Runtime Notice，要求下一回复调用推进工具（更新 Todo、调查/操作或验证），而不是只口头描述下一步。提醒按进展状态最多一次：完整 Todo、非 Todo 工具结果、验证证据数量、generation 或 `verification_required` 发生变化后，可以再次提醒；相同标记下再次输出无工具文本才标记 `blocked`。没有 `progress_marker` 的旧式 State 保持一次提醒兼容行为。这种保守策略不依赖第三方库或命令解析。

## v0.15 任务清单与状态（Todo / Task State）

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

## 3. 当前能力（v0.19.1，含 v0.18.1 完成提醒修复）

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
| `todos` | 动态计划步骤及其 `pending` / `in_progress` / `completed` 状态 |
| `verification_evidence` | 最近 verification 命令、退出码与结果；只有当前 generation 的 `[exit=0]` 才算通过 |
| `failures` / `recovery_actions` | 最近失败的工具、failure/attempt/generation、分类与可重试性，以及恢复动作状态和因果引用 |
| `checkpoints` / `rollback_checkpoints` | 单文件前后镜像元数据；后者只列出当前可 rollback 的 `ready` checkpoint，不含文件内容 |
| `budgets` / `recovery_notice` | 失败重试、参数指纹、恢复动作和 repair cycle 的剩余额度及当前恢复提示 |

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
| `rollback_checkpoint` | 内部 `checkpoint_id` | **仅 RecoveryRuntime** | 不进入模型 schema；恢复一个已授权且未冲突的单文件 checkpoint |

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
