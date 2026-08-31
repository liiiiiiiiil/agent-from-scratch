# AGENTS.md

## 项目定位

`agent-from-scratch`（Python 包名 `mini_agent`）是一个**逐步生长的编程 agent**：从最小可用的 agent loop 起步，按需增加工具与能力，目标是能独立完成基础的编程任务（读写改文件、跑命令、跑测试、简单多步任务）。

> 本仓库为多阶段教学仓库，教程入口按能力阶段组织，具体实现按 git tag `v0.01` → `v0.X` 顺序学习（持续迭代，不设上限），见 `docs/tutorials/`。

设计原则：
- **零第三方依赖**（仅 Python 标准库），保持自包含、易部署。
- **渐进式生长**：每次只加刚好够用的能力，避免过度设计。新功能先在 `AGENTS.md` 记下意图，再落地代码。
- **核心 loop 保持清晰**：agent loop 不对 LLM 或 CLI 顶层异常做兜底；工具执行异常由工具层/执行器转换为错误结果并回灌给 LLM。复杂容错按需在工具层或执行器层引入。

## 教程文档规则与标准

`docs/tutorials/` 不是版本更新摘要，而是读者可以按 git tag 独立学习、运行和验证的课程。新增或修改能力时，教程与代码、测试同属交付内容，必须满足以下规则：

### 内容原则

- **按版本差异教学**：每课只讲当前版本相对上一版本新增的核心概念和代码变化，并给出 `git checkout` / `git diff` 入口；不要把后续版本能力提前写成当前行为。
- **先解释问题，再解释实现**：必须说明上一版本遇到了什么限制、为什么需要本版设计，以及关键取舍；不能只罗列类名、函数名或最终结论。
- **以代码和测试为事实来源**：教程中的默认值、调用顺序、失败路径、返回值、配置项和命令必须与对应 tag 的实现一致。计划文档只说明意图，不能代替对实际代码的核对。
- **保留教学主线**：突出本版新增的一个核心概念，代码片段只展示理解该概念所需的最小部分；完整实现通过文件路径和 tag 引导读者查看，避免整文件粘贴。
- **同时讲清正常路径与边界**：涉及协议、安全、并发、预算、异常降级等行为时，必须写明不变量、失败路径和本版刻意不解决的问题。

### 每课必备结构

每篇版本教程原则上包含以下内容；确实不适用的章节可以合并，但不能让学习链路中断：

1. 版本号、上一课、教程总览和下一课导航。
2. 本课目标，以及读完后应能解释或完成什么。
3. 前置条件和切换到对应 git tag 的命令。
4. 新增/改动文件表，以及 `git diff --stat <上一版本>..<当前版本>`。
5. 上一版的问题、本版核心概念、关键执行流程和必要的数据结构。
6. 关键实现拆解：入口、主路径、重要辅助函数、失败或降级路径。
7. 设计选择及理由，并明确本版边界。
8. 至少一个可运行的最小示例；涉及运行时流程时，再给一个典型场景或可观察日志。
9. 与本版能力直接对应的测试命令和验收点。命令必须可执行，不写容易过期的测试数量。
10. 本版独有特性、下一课预告和完整代码索引。

### 同步与验收

- 完成一个版本时，同步 `README.md`、`README_EN.md`、`docs/tutorials/README.md`、`docs/operation/manual.md`、`CHANGELOG.md`、`pyproject.toml` 版本信息，以及本文件的当前架构与路线图；已完成的教程必须提供可点击链接，未完成内容明确标为规划中。
- 文档示例优先使用仓库现有 API 和标准库，遵守“零第三方运行时依赖”；若测试命令依赖开发环境中的工具，要同时给出仓库原生的直接运行方式（如适用）。
- 提交前逐项核对：链接有效、版本号一致、代码片段可对应到实现、命令可运行、协议描述无误、失败降级与测试覆盖一致。
- 不以篇幅衡量完整度，但读者只阅读当前课和引用的上一课后，应能回答“为什么改、改了什么、如何工作、如何验证、有什么边界”。缺少其中任一项都视为教程未完成。

## 版本状态

- **稳定基线**：`v0.13`（已创建 Git tag，可按教程复现）。
- **当前开发版本**：`v0.15`（Todo / Task State）。
- 进行中的版本不要在稳定版手册中标为已发布；创建 tag 前应完成本文件、README、中英文教程索引、操作手册和 CHANGELOG 的一致性检查。

## 当前架构（v0.15）

标准 Python `src/` 包布局：

```
agent-from-scratch/
├── pyproject.toml          # 项目元数据（零第三方运行时依赖）
├── README.md
├── README_EN.md
├── AGENTS.md
├── .gitignore
├── src/mini_agent/
│   ├── __init__.py         # 包入口
│   ├── __main__.py         # CLI 入口：python -m mini_agent
│   ├── agent.py            # agent loop：call_llm + agent_loop（经 ContextManager 调 LLM）
│   ├── config.py           # 配置占位 + 自动加载 config_local.py（本地真实配置，不进 git）
│   ├── config_example.py    # 配置模板
│   ├── context.py          # ContextManager：预算裁剪 + 历史摘要压缩 + Structured State 注入
│   ├── state.py            # AgentState：独立于 messages 的执行状态 + record_tool
│   ├── permission.py       # 权限闸门：二维权限 (tool_name, pattern) + allow/deny/ask 三态 + fnmatch 通配符匹配
│   ├── prompt.py           # system prompt 分层组装：header + core_rules + environment + project instructions
│   ├── instructions.py     # InstructionLoader：发现并合并 AGENTS.md
│   └── tools/
│       ├── __init__.py     # registry + executor 实例
│       ├── base.py         # Tool / ToolRegistry / ToolExecutor（含 on_result 回调）
│       ├── calc.py         # calculate 工具
│       ├── file.py         # read_file / write_file / edit_file / list_dir / grep 工具
│       └── shell.py        # run_shell 工具（subprocess + 超时 + 输出截断）
├── tests/
│   ├── test_loop.py        # agent loop 集成测试（mock LLM）
│   ├── test_prompt.py      # system prompt smoke test
│   ├── test_state.py       # AgentState 单测
│   ├── test_context.py     # ContextManager 单测
│   ├── test_executor.py    # Executor 结果回调单测
│   └── test_tools.py       # 工具 smoke test
├── examples/               # 示例 IO 文件
└── docs/
    ├── README.txt          # 文档目录说明
    ├── governance/         # 治理文档：约束、规范、决策记录
    ├── plans/               # 计划文档：路线图、功能计划、任务拆解
    ├── operation/          # 操作文档：运行手册、使用指南
    │   └── manual.md       # 操作手册
    └── tutorials/          # 教学文档：按阶段导航、按版本切片的教程
        ├── README.md       # 教学路径索引
        ├── 01-minimal-loop.md
        ├── 02-first-tool.md
        ├── 03-file-tools.md
        ├── 04-permission-gate.md
        ├── 05-streaming.md
        ├── 06-concurrent-tool-calls.md
        ├── 07-system-prompt.md
        ├── 08-file-operations.md
        ├── 09-permission-upgrade.md
        ├── 10-shell-execution.md
        ├── 11-context-architecture.md
        ├── 12-token-budget-trimming.md
        └── 13-context-compaction.md
```

- LLM 调用：`http.client` 流式，OpenAI function calling 协议（`tools` 参数）。
- 工具：`calculate`、`read_file`（支持 offset/limit）、`write_file`、`edit_file`（精确替换）、`list_dir`、`grep`、`run_shell`（shell 执行，超时 30s，输出截断 2000 字符）。通过 `ToolRegistry` 注册，`ToolExecutor` 执行。
- **v0.04 权限闸门**：`write_file`/`edit_file` 走 ASK（每次问用户），`read_file`/`calculate`/`list_dir`/`grep` 走 ALLOW。`PermissionGate` 在 Executor 里拦截。
- **v0.07 系统提示词工程化**：`prompt.py` 的 `build_system_prompt()` 分层组装 system prompt（header 身份 + core_rules 行为规范 + environment 环境信息），`__main__.py` 启动时调一次构造 `messages[0]`。为多 agent 预留 `agent_name` 参数。
- **v0.08 文件操作补全**：`read_file` 加 `offset`/`limit` 分段读取 + 行号前缀；新增 `edit_file`（精确字符串替换，多匹配安全检查）、`list_dir`（目录列举，200 条上限）、`grep`（正则搜索，100 条上限，纯标准库 `re`+`os.walk`+`fnmatch`）。
- **v0.09 权限系统升级**：`permission.py` 从一维 `tool_name -> action` 升级为二维 `(tool_name, pattern) -> action`。规则内部存扁平 `list[dict]`（Rule 三元组），`_from_config()` 兼容简单 dict 格式和复杂 dict 格式，复杂格式 `*` 排最前（优先级最低）。`check()` 用 `fnmatch` 做 wildcard 匹配，`findLast` 语义（后出现优先级更高），未匹配默认 `ask`。`approve()` 存 `(tool_name, pattern)` 实现"同类免问"。`_extract_pattern()` 从 args 提取 pattern（文件工具提取 path，run_shell 提取 command，其他返回 `*`）。
- **v0.10 shell 执行**：新增 `tools/shell.py`（`run_shell` 工具，`subprocess.run` + `shell=True` + 超时 30s + 输出截断 2000 字符 + 退出码前缀）。`PERMISSION_RULES` 加 `run_shell` 二维权限（`git *`/`python *`/`pip *`/`ls *`/`cat *`/`echo *` → allow，`*` → ask）。`prompt.py` header 能力描述更新为"读写改文件、跑命令、做数学计算"。不做 BashArity 命令泛化——fnmatch 通配符已够用。
- **v0.11 上下文架构**（纯重构，外部行为与 v0.10 一致）：新增 `state.py`（`AgentState`：task/current_goal/tool_history/files_changed/errors/status，`record_tool` 由 Executor 回调驱动，加锁 + `snapshot()` 深拷贝）与 `context.py`（`ContextManager.prepare_messages()` 作为 LLM 调用前统一入口）。`agent_loop` 签名改为 `(context_manager, tool_executor)`，`ToolExecutor` 加 `on_result` 回调（权限拒绝/异常/成功三路径都通知，State 更新 loop 不感知）。消除 v0.10"MAX_ITERATIONS 半截状态"契约：每轮 tool results 全部回灌后才进下一轮或返回。
- **v0.12 预算与裁剪**：`context.py` 新增 `count_tokens`（`len(text) // 3` 启发式）、`ContextBudget`（`CONTEXT_WINDOW` 比例预算）与 `TrimPolicy`。`prepare_messages()` 每次基于完整 history 生成副本；超限时先截断最老 tool result，再按完整轮次原子删除，system 与首条 user task 永不删除，无孤儿 tool result。
- **v0.13 上下文压缩**：老轮次通过无工具摘要请求压缩为 Historical Summary，近期轮次保留原文；`AgentState` 重新渲染为 Structured State 锚定事实，摘要失败降级为 trimming，`MAX_ITERATIONS = 50`。
- **v0.13.1 Context Observability 增强**：`ContextManager` 提供 `ContextStats`/`stats_snapshot()` 与 `ContextEvent` observer；默认输出每次请求的互斥 token 分桶及 trimming/compaction 事件，可由 `CONTEXT_OBSERVABILITY` 关闭；不新增独立教程。
- **v0.14 Project Instructions**：启动时按 root → cwd 加载 `AGENTS.md`，作为 protected context 注入每次请求；12,000 字符上限，不改变权限规则。
- **v0.15 Todo / Task State**：`AgentState` 管理最多 50 项 Todo，`update_todo` 通过实例 registry 更新并由 Structured State 每轮渲染；不自动规划或持久化。
- 迭代上限默认值为 `MAX_ITERATIONS = 50`，可由 `config_local.py` 覆盖；超限直接返回“达到最大迭代次数”。
- 包未 pip install 时需 `PYTHONPATH=src`；`pip install -e .` 后可免。

## 路线图

实现路线持续迭代，每次加一个概念，直到达到轻量编程 agent 的能力集。教程入口按阶段组织，版本表保留代码演进顺序：

- [x] **v0.01 最简 agent loop**：`call_llm`（非流式）+ `agent_loop`（无工具纯对话）+ `__main__` + `config.py`
- [x] **v0.02 第一个工具**：`Tool`/`ToolRegistry`/`ToolExecutor` + `calculate` + function calling 协议
- [x] **v0.03 文件读写工具**：`read_file`/`write_file`
- [x] **v0.04 权限闸门**：`permission.py`（allow/deny/ask 三态）+ `PermissionGate`
- [x] **v0.05 流式输出**：`call_llm` 改流式 + chunk 拼接 + 打字机效果
- [x] **v0.06 并发 tool_calls**：`ThreadPoolExecutor` 并发执行同一轮多个 tool_calls
- [x] **v0.07 系统提示词工程化**：`prompt.py`（`build_system_prompt` 分层组装：header 身份 + core_rules 行为规范 + environment 环境信息）
- [x] **v0.08 文件操作补全**：`list_dir`、`edit_file`、`grep`
- [x] **v0.09 权限系统升级**：二维权限 (tool_name, pattern) + once/always 回复区分 + fnmatch 通配符匹配
- [x] **v0.10 shell 执行**：`run_shell` 工具 + subprocess + 超时 + 输出截断 + 二维命令模式权限
- [x] **v0.11 上下文架构**（阶段四 4.1）：`AgentState`（状态独立于 messages）+ `ContextManager`（LLM 调用前统一入口，分层构建）
- [x] **v0.12 预算与裁剪**（阶段四 4.2）：token 启发式估算 + Context Budget（比例配置）+ 按轮次原子 trimming（无孤儿 tool result）
- [x] **v0.13 上下文压缩**（阶段四 4.3）：老历史 LLM 摘要 + Structured State 锚定 + `MAX_ITERATIONS` 调大
- [x] **v0.13.1 上下文可观测性增强**（阶段四维护版本）：ContextStats 分桶 + trimming/compaction 事件
- [x] **v0.14 Project Instructions**：自动发现并注入受保护的 `AGENTS.md` 项目规则
- [x] **v0.15 Todo / Task State**：显式动态任务状态与原子更新
- [ ] **v0.16 Plan-driven Execution**：计划驱动执行与验证闭环
- [ ] **...**（持续迭代，按需追加）

> 每加一项，在此打勾并在"当前架构"更新对应模块说明。详细方案见 `docs/plans/teaching-repo-plan.md`；阶段四细化方案见 `docs/plans/context-management-plan.md`。

> 教学阶段：阶段一为 Agent Loop（v0.01）；阶段二为工具与安全（v0.02–v0.04）；阶段三为 Mini Agent 里程碑（v0.05–v0.10）；阶段四为 Context Management（v0.11–v0.13）；阶段五为 v0.14 及以后的进阶能力。

## 运行

```bash
python -m mini_agent "你的任务"
# 或交互式输入
python -m mini_agent
```

包未安装时需设 `PYTHONPATH=src`（Windows 用 `$env:PYTHONPATH="src"`）；`pip install -e .` 后可从任意目录直接运行。

## 关键约束（踩坑备忘，勿违反）

- **必须用 `http.client`，不能用 `requests`/`urllib`**：`your-gateway-host` 网关对 `Accept-Encoding: gzip` 响应异常返回 502。`call_llm` 里显式设 `Accept-Encoding: identity` 绕过。换 HTTP 客户端会重新踩坑。
- 配置（`BASE_URL`/`API_KEY`/`MODEL`）写在 `config_local.py`（不进 git），由 `config.py` 自动 `import *` 加载；无 `config_local.py` 时回退到 `config.py` 里的占位值。
- agent loop 不对 LLM/CLI 顶层异常做兜底；`ToolExecutor` 会捕获 handler 异常并返回错误字符串，`agent_loop` 会把工具边界异常转换为对应的 `role=tool` 结果，保证协议序列完整。
- 迭代上限默认值为 `MAX_ITERATIONS = 50`，可由 `config_local.py` 覆盖。超限直接返回 `"达到最大迭代次数"`，不报错。

## 测试与验收

运行时不需要第三方依赖；完整测试套件使用开发环境中的 pytest：

```bash
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python scripts/check_tutorials.py
```

未安装 pytest 时，可运行各测试文件自带的脚本入口（这些入口覆盖核心 smoke test）：

```bash
PYTHONPATH=src python tests/test_prompt.py
PYTHONPATH=src python tests/test_loop.py
PYTHONPATH=src python tests/test_tools.py
PYTHONPATH=src python tests/test_state.py
PYTHONPATH=src python tests/test_context.py
PYTHONPATH=src python tests/test_executor.py
```

新增版本的验收至少应覆盖对应模块的测试、教程中的最小示例，以及稳定版文档中的版本号、默认配置和命令。
教程结构检查器默认校验教程索引中的最新版本，也可传入相对路径校验指定课程：`PYTHONPATH=src python scripts/check_tutorials.py docs/tutorials/15-task-state.md`。

## 已知行为

- v0.01 无工具：LLM 回复不含 `tool_calls` 即视为完成，agent loop 结束。
- 结束条件：LLM 回复不含 `tool_calls` 即视为完成。
