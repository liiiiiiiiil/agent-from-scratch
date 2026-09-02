# Changelog

## [Unreleased]

- Raised the current mainline minimum supported Python version from 3.9 to 3.10.
- Recreated the recommended development environment with Python 3.10.

## [v0.16] - 计划驱动执行（Plan-driven Execution）

- Added verification evidence, task reset, completion reminders, and blocked/failed task states.
- Added `run_shell` purpose enum with unified exit status output.
- Added stage-five end-to-end acceptance coverage for project instructions, Todo replanning, context compaction, and verification closure.

## [v0.15] - 任务清单与状态（Todo / Task State）

- Added state-bound `update_todo` with atomic validation and Structured State rendering.
- Added per-run registries to isolate Todo state between AgentState instances.
- v0.15 does not auto-plan, persist todos, or block completion.

## [v0.14] - 项目级指令（Project Instructions）

### Added
- `InstructionLoader`：发现并合并适用的 `AGENTS.md`，保留来源并限制长度。
- protected context：项目规则在 trimming/compaction 后仍会重新注入。
- `docs/tutorials/14-project-instructions.md` 与指令加载测试。

### Changed
- CLI 启动时加载项目级指令；`build_system_prompt()` 支持 `<project_instructions>` 区块。
- `ContextManager` 支持独立 protected messages，兼容旧 history 调用。

### Why
- 让 Agent 在执行任务前获得项目约束，同时避免把规则误当作可压缩的对话历史。

## [v0.13.1] - Context Observability 增强

### Added
- `ContextStats` 与 `ContextManager.stats_snapshot()`：记录实际请求的 token 总量、预算、输出预留及互斥分桶。
- `ContextEvent` observer：观察 prepared、trimmed、compacted 事件；默认终端日志可通过 `CONTEXT_OBSERVABILITY` 关闭。
- v0.13 教程追加 Context Observability 说明；本版本不新增独立教程。

### Changed
- trimming 日志记录 tool result 截断和完整轮次删除的对象与 token 节省量。
- compaction 日志记录压缩轮次、摘要大小和保留轮次；观测回调异常不影响上下文构建。

## [v0.13] - 上下文压缩

### Added
- `ContextManager.compact()`：老轮次摘要、近期轮次保留和 Structured State 注入
- 摘要请求复用 `http.client`，不携带工具 schema；摘要失败自动降级为 trimming
- `docs/tutorials/13-context-compaction.md` 与压缩流程单测

### Changed
- `MAX_ITERATIONS` 从 10 调整为 50
- `prepare_messages()` 在预算超限且存在老轮次时自动触发 compaction

## [v0.12] - 预算与裁剪

### Added
- `src/mini_agent/context.py`：`count_tokens`（`len(text) // 3` 启发式）、`ContextBudget`（窗口、输出预留和历史比例）与 `TrimPolicy`
- `CONTEXT_WINDOW = 128_000`：写入 `config.py` 与 `config_example.py`，可由 `config_local.py` 覆盖
- `docs/tutorials/12-token-budget-trimming.md`：第十二课教学文档
- `tests/test_context.py`：token 估算、预算、轮次原子性、tool result 截断、保底消息与预算收敛测试

### Changed
- `ContextManager.prepare_messages()` 从完整 history 构建独立副本：超限时先截断旧 tool result，仍超限时从最老轮次删除
- system 消息和首条 user task 不参与裁剪；带 `tool_calls` 的 assistant 消息与其连续 tool results 作为不可拆分轮次处理

### Why
- 上下文是有限资源，长任务不应因 token 累积而直接失败。
- OpenAI tool calling 要求 tool result 紧跟相应 tool call；按轮次原子删除避免产生会导致 400 的孤儿 `role=tool` 消息。
- 原始 history 与 AgentState 都不被裁剪，给 v0.13 历史摘要和 Structured State 锚定保留正确边界。

## [v0.11] - 上下文架构

### Added
- `src/mini_agent/state.py`：`AgentState` 数据类（task/current_goal/tool_history/files_changed/errors/status）+ `record_tool` 执行记录接口 + `snapshot` 线程安全快照
- `src/mini_agent/context.py`：`ContextManager`，`prepare_messages()` 作为 LLM 调用前统一入口（本版恒等返回，只立边界）
- `tests/test_state.py`：State 单测（10 个：记录/派生字段/深拷贝隔离/并发安全）
- `tests/test_context.py`：ContextManager 单测（5 个：引用保持/恒等返回/State 不被注入）
- `tests/test_executor.py`：Executor 回调单测（14 个：三路径回调/回调异常不影响执行/brief 截断/并发回调）
- `docs/tutorials/11-context-architecture.md`：第十一课教学文档

### Changed
- `src/mini_agent/agent.py`：`agent_loop(messages)` 改为 `agent_loop(context_manager, tool_executor)`，经 `cm.prepare_messages()` 调 LLM；消除 v0.10 的"MAX_ITERATIONS 半截状态"契约（每轮 tool results 全部回灌后才进下一轮或返回）
- `src/mini_agent/tools/base.py`：`ToolExecutor` 加 `on_result` 结果回调（权限拒绝/handler 异常/执行成功三路径都通知，brief 截断 200 字符，回调异常只打印不影响执行）
- `src/mini_agent/__main__.py`：组装 AgentState + ContextManager 注入 loop，`run_task` 统一 argv/交互两条路径并维护 `state.status`
- `src/mini_agent/agent.py`：`call_llm` 按 `BASE_URL` 的 scheme 选 `HTTPConnection`/`HTTPSConnection`（此前明文 HTTP 打 https 网关会收到 302，LLM 回复为空）
- `tests/test_tools.py`：修复 `test_run_shell_exit_code` 在 pytest 下读 stdin 挂掉的问题（放行策略绕过 ASK 交互）

### Why
- **Agent State ≠ LLM Context**（D1）：messages 是易耗品（迟早裁剪/压缩），State 是压缩后不失忆的锚。v0.09 的 PermissionGate always 状态已验证"运行时状态独立于 messages"可行。
- **纯重构版**：外部行为与 v0.10 完全一致，不裁剪、不压缩、不估算 token——先把"谁能碰 messages"（只有 ContextManager）、"状态放哪"（AgentState）立好边界，v0.12/v0.13 的工程实现才有落点。
- **State 由 Executor 结果回调更新，loop 不感知**（D5）：否则 State 会变成第二个无人维护的 messages。回调把记录收敛到 Executor 一处，且是观察者不是参与者（异常不影响执行结果）。
- **线程安全**：v0.06 起同一轮 tool_calls 并发执行，回调来自线程池工作线程，`record_tool` 加 Lock、读取走 `snapshot()` 深拷贝。
- **半截状态消除**：v0.10 文档已警告 messages 可能停在"有 tool_calls 无 tool 结果"的协议非法状态，本版从代码上根治——这也为 v0.12 按轮次原子裁剪铺路（轮次完整性从此有保证）。

## [v0.10] - shell 执行

### Added
- `src/mini_agent/tools/shell.py`：`run_shell` 工具（`subprocess.run` + `shell=True` + 超时 30s + 输出截断 2000 字符 + 退出码前缀）
- `docs/tutorials/10-shell-execution.md`：第十课教学文档

### Changed
- `src/mini_agent/tools/__init__.py`：注册 `run_shell_tool`（7 个工具）
- `src/mini_agent/permission.py`：`PERMISSION_RULES` 加 `run_shell` 二维权限规则（`git *`/`python *`/`pip *`/`ls *`/`cat *`/`echo *` → allow，`*` → ask）；`_from_config` 对复杂格式排序，`*` 排最前（优先级最低），与 findLast 语义配合
- `src/mini_agent/prompt.py`：`header()` 能力描述从"后续会扩展到跑命令"改为"当前能读写改文件、跑命令、做数学计算"
- `tests/test_tools.py`：新增 3 个 run_shell 测试（执行 echo、非零退出码、二维权限 git allow/rm deny）

### Why
- v0.09 的二维权限已为 `run_shell` 铺路（`_extract_pattern` 返回 command 字符串），v0.10 落地工具本身。
- 不做 BashArity 命令泛化：fnmatch 的 `git *` 通配符已能按命令前缀匹配，教学简洁性优先。后续如需按"命令+参数"分离匹配再引入。
- `shell=True` 让命令字符串直接执行，教学简洁；安全性由二维权限闸门兜底（安全命令 allow，其他 ask）。
- 超时 30s 硬编码：跑测试够用，长任务后续 Context Management 版本再调。
- 输出截断 2000 字符：防长输出爆上下文，与 `read_file` 的 limit 设计一致。
- `_from_config` 排序修复：findLast 从后往前找，`*` 会匹配一切，必须排最前（优先级最低），否则 `*` 永远先匹配返回 ask，具体模式被遮蔽。

## [v0.09] - 权限系统升级

### Changed
- `src/mini_agent/permission.py`：从一维 `tool_name -> action` 升级为二维 `(tool_name, pattern) -> action`
  - 规则内部存扁平 `list[dict]`（Rule 三元组：permission + pattern + action）
  - `_from_config()` 兼容旧版简单 dict 格式（`{"write_file": "ask"}`）和新版复杂格式（`{"run_shell": {"git *": "allow"}}`）
  - `check()` 用 `fnmatch` 做 wildcard 匹配，`findLast` 语义（后出现优先级更高），未匹配默认 `ask`
  - `approve()` 存 `(tool_name, pattern)` 而非只存 `tool_name`，实现"同类命令免问"
  - `_extract_pattern()` 从 args 提取 pattern（文件工具提取 path，run_shell 提取 command，其他返回 `*`）
- `tests/test_tools.py`：新增 4 个二维权限测试（pattern allow/deny 覆盖、always 存 pattern、findLast 优先级）

### Why
- v0.04 的一维权限只按工具名控制，无法区分 `git status`（安全）和 `rm -rf /`（危险）——所有 `run_shell` 共享同一个 action，粒度太粗。
- 二维权限按命令模式控制：`git *` 可以 allow，`rm *` 可以 deny，其他 ask。为 v0.10 `run_shell` 工具的命令模式权限铺路。
- `findLast` 语义让运行时 `approved` 规则（追加在末尾）自然覆盖前面的 `ask` 规则，无需显式删除旧规则。
- 未匹配默认 `ask` 而非 `allow`——安全优先，新工具默认需要用户确认。
- 借鉴 OpenCode `PermissionNext` 的 `evaluate` / `fromConfig` / `findLast` 设计，去掉事件总线、pending 队列、持久化——CLI 同步交互不需要。
- `_extract_pattern()` 对 `run_shell` 返回完整命令字符串作为占位，v0.10 直接用 fnmatch 通配符（如 `git *`）按命令前缀匹配，不做 BashArity 命令泛化——fnmatch 已够用。

## [v0.08] - 文件操作补全

### Added
- `src/mini_agent/tools/file.py`：新增 `edit_file`（精确字符串替换，多匹配安全检查）、`list_dir`（目录列举，200 条上限）、`grep`（正则搜索，100 条上限，纯标准库 `re`+`os.walk`+`fnmatch`）
- `docs/tutorials/08-file-operations.md`：第八课教学文档

### Changed
- `src/mini_agent/tools/file.py`：`read_file` 加 `offset`/`limit` 分段读取 + 行号前缀（`00001| `）+ 剩余行提示
- `src/mini_agent/permission.py`：`list_dir`/`grep` 走 ALLOW，`edit_file` 走 ASK
- `src/mini_agent/tools/__init__.py`：注册 6 个工具
- `tests/test_tools.py`：从 8 个测试扩到 15 个（新增 read_file offset/limit、edit_file 单次/无匹配/多匹配、list_dir、grep 有匹配/无匹配）

### Why
- v0.03 只有 read_file/write_file，agent 看不到目录结构、改文件只能整文件重写、找不到内容在哪——补齐 list_dir/edit_file/grep 形成完整操作链路。
- `edit_file` 用字符串匹配而非行号编辑：LLM 容易数错行号，从 read_file 输出复制原文更可靠。多匹配时报错而非静默替换第一处，防误改。
- `read_file` 加 offset/limit：大文件不爆上下文；行号前缀帮 LLM 定位 edit_file 的 old_string。
- `grep` 用纯标准库而非 ripgrep：零第三方依赖约定。性能差但教学场景够用，v0.10 加 run_shell 后 LLM 可自己调 rg。
- 不做 OpenCode 的 8 种模糊匹配策略、输出临时文件、文件锁、LSP 集成——对教学项目过度设计。

## [v0.07] - 系统提示词工程化

### Added
- `src/mini_agent/prompt.py`：`build_system_prompt(agent_name)` 分层组装（header 身份 + `_CORE_RULES` 行为规范 + `environment` 环境信息）；`_detect_git` 纯目录遍历判断 git 仓库
- `tests/test_prompt.py`：system prompt smoke test（组装/ header / 回退/ 环境字段）
- `docs/tutorials/07-system-prompt.md`：第七课教学文档

### Changed
- `src/mini_agent/__main__.py`：`messages[0]` 从一行硬编码字符串改为调用 `build_system_prompt()`

### Why
- 一行 system prompt 缺环境信息（工作目录/平台/日期），模型靠猜路径和命令易出错。
- 无行为规范，模型可能啰嗦、加 emoji、复述工具输出、主动总结——多轮迭代时污染上下文。
- 分层组装（header/core_rules/environment）借鉴 OpenCode 四层结构做减法：去掉 provider 适配（单模型）和 custom 加载（留 v0.08）。
- `header(agent_name)` 为多 agent/sub-agent 预留接口，当前只实现 build，后续加 explore/plan 只需在 dict 加一行。
- `agent.py` 不动——核心 loop 仍只认 messages 列表，prompt 构造是入口层职责，保持 loop 清晰。

## [v0.06] - 并发 tool_calls

### Changed
- `src/mini_agent/agent.py`：`agent_loop` 里 tool_calls 执行从串行 for 循环改为 `ThreadPoolExecutor` 并发；`pool.map` 保证结果按原序回灌

### Why
- 同一轮的多个 tool_calls 互不依赖，串行执行浪费时间。
- `ThreadPoolExecutor` 是"最小改动 + 足够好的并发"——不需要把整个调用链 async 化。
- `pool.map` 保证结果顺序与 tool_calls 原序一致，回灌顺序安全。
- v0.04 的 `_ask_lock` 在并发场景生效：防止多个 ASK 权限交互交错。

## [v0.05] - 流式输出

### Changed
- `src/mini_agent/agent.py`：`call_llm` 改流式（`stream=True` + SSE 解析 + chunk 拼接 + 打字机效果）；`agent_loop` 里 print 标记移到 `call_llm` 之前

### Why
- 非流式下 LLM 全部想完才返回，长回复时终端有明显等待。
- 流式边收边显示，打字机效果让用户体感更快。
- `http.client` + `Accept-Encoding: identity` 保证收到未压缩的原始文本流，逐行解析可靠。
- tool_calls 的 arguments 跨 chunk 拼接（`+=`），用 index 聚合——流式下结构化数据的处理方式。

## [v0.04] - 权限闸门

### Added
- `src/mini_agent/permission.py`：`PermissionPolicy`（allow/deny/ask 三态）+ `PermissionGate`（检查+交互+锁）
- `docs/tutorials/04-permission-gate.md`：第四课教学文档

### Changed
- `src/mini_agent/tools/base.py`：`ToolExecutor` 加 `gate` 参数，`execute` 里先过 `gate.guard` 再调 handler
- `src/mini_agent/tools/__init__.py`：import PermissionGate（executor 自动创建默认 gate）
- `tests/test_tools.py`：write_file 用放行策略绕过 ASK；新增 DENY 策略测试

### Why
- v0.03 的 write_file 直接执行不问人，有覆盖重要文件的风险。
- 三态（allow/deny/ask）覆盖"总是允许/总是禁止/看情况"三种现实需求，比两态更灵活。
- 拒绝不是报错而是工具结果，回灌给 LLM 让它调整策略——容错在工具层。
- `_ask_lock` 为 v0.06 并发 tool_calls 预留，防止多个权限提示交错。

## [v0.03] - 文件读写工具

### Added
- `src/mini_agent/tools/file.py`：`read_file`/`write_file` 工具
- `examples/input.txt`、`examples/input2.txt`：示例文件
- `docs/tutorials/03-file-tools.md`：第三课教学文档

### Changed
- `src/mini_agent/tools/__init__.py`：注册 read_file/write_file
- `tests/test_tools.py`：加 read_file/write_file 测试

### Why
- agent 不改文件没法做编程任务，文件读写是基础能力。
- 先让"能写文件"跑通，权限是独立概念，v0.04 专门讲。
- 加工具只需写 handler + 注册，不动 agent.py——验证三件套分离关注点的好处。

## [v0.02] - 第一个工具

### Added
- `src/mini_agent/tools/base.py`：`Tool`/`ToolRegistry`/`ToolExecutor` 三件套
- `src/mini_agent/tools/calc.py`：`calculate` 工具（数学表达式计算，正则白名单防注入）
- `src/mini_agent/tools/__init__.py`：注册中心，创建 registry + executor 并注册 calculate
- `tests/test_tools.py`：工具 smoke test（registry/calculate/异常处理）
- `docs/tutorials/02-first-tool.md`：第二课教学文档

### Changed
- `src/mini_agent/agent.py`：`call_llm` 加 `tools` 参数（function calling 协议）；`agent_loop` 加 tool_calls 执行 + `role=tool` 回灌
- `src/mini_agent/__main__.py`：system prompt 改为"你是一个助手，通过调用工具完成任务"

### Why
- 引入 OpenAI function calling 协议，让 LLM 能真正"做事"而不只是聊天。
- Tool 三件套（定义/注册/执行）分离关注点，后续加工具只需写 handler + 注册，不动 loop。
- Executor 捕获 handler 异常返回错误信息给 LLM，让 LLM 决定重试或告知用户——容错在工具层，不在 loop 层。

## [v0.01] - 最简 agent loop

### Added
- `src/mini_agent/agent.py`：`call_llm`（非流式）+ `agent_loop`（无工具纯对话循环）
- `src/mini_agent/__main__.py`：CLI 入口，支持单次任务模式和交互模式
- `src/mini_agent/config.py`：`BASE_URL`/`API_KEY`/`MODEL`/`MAX_ITERATIONS`（硬编码）
- `src/mini_agent/__init__.py`：包入口
- `tests/test_loop.py`：import 链路 smoke test
- `docs/tutorials/01-minimal-loop.md`：第一课教学文档
- `docs/tutorials/README.md`：教学路径索引
- `docs/plans/teaching-repo-plan.md`：多阶段教学仓库完整方案
- `docs/operation/manual.md`：操作手册（v0.01 版）

### Why
- 从最小可用的对话 loop 起步，先讲清"什么是 agent loop"：messages 列表、调 LLM、判断结束条件。
- 不引入工具、权限、流式、并发，让第一课的 loop 概念最干净。
- `http.client` + `Accept-Encoding: identity` 是踩坑后的选择（网关对 gzip 响应异常），从第一版就确立。
## [v0.17] - Failure Model

- Added auditable `ExecutionGeneration`, `ExecutionAttempt`, `FailureEvent`, and reserved `RecoveryAction` state records.
- Added structured executor results, effect classes, canonical argument fingerprints, bounded budgets, and blocked/failed terminal reasons.
- Possible-effect tool calls now reserve generations before handlers; read-only calls may remain concurrent while mixed verification calls are rejected.
- Structured State renders generation, latest failure, causal attempt, budgets, and recovery notices without raw sensitive arguments.
