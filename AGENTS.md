# AGENTS.md

## 项目定位

`agent-from-scratch`（Python 包名 `mini_agent`）是一个**逐步生长的编程 agent**：从最小可用的 agent loop 起步，按需增加工具与能力，目标是能独立完成基础的编程任务（读写改文件、跑命令、跑测试、简单多步任务）。

> 本仓库为多阶段教学仓库，教程入口按能力阶段组织，具体实现按 git tag `v0.01` → `v0.X` 顺序学习（持续迭代，不设上限），见 `docs/tutorials/`。

设计原则：
- **零第三方依赖**（仅 Python 标准库），保持自包含、易部署。
- **渐进式生长**：每次只加刚好够用的能力，避免过度设计。新功能先在 `AGENTS.md` 记下意图，再落地代码。
- **核心 loop 保持清晰**：agent loop 不加 try/except 兜底，工具失败直接抛异常——保持主路径可读。复杂容错按需在工具层或执行器层引入。

## 当前架构（v0.12）

标准 Python `src/` 包布局：

```
mini_agent/
├── pyproject.toml          # 项目元数据（零第三方依赖）
├── README.md
├── .gitignore
├── src/mini_agent/
│   ├── __init__.py         # 包入口
│   ├── __main__.py         # CLI 入口：python -m mini_agent
│   ├── agent.py            # agent loop：call_llm + agent_loop（经 ContextManager 调 LLM）
│   ├── config.py           # 配置占位 + 自动加载 config_local.py（本地真实配置，不进 git）
│   ├── context.py          # ContextManager：预算检查 + tool result 截断 + 按轮次原子裁剪
│   ├── state.py            # AgentState：独立于 messages 的执行状态 + record_tool
│   ├── permission.py       # 权限闸门：二维权限 (tool_name, pattern) + allow/deny/ask 三态 + fnmatch 通配符匹配
│   ├── prompt.py           # system prompt 分层组装：header + core_rules + environment
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
        └── 12-token-budget-trimming.md
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
- 迭代上限 `MAX_ITERATIONS = 10`（硬编码，长任务可能静默截断，后续需调）。
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
- [ ] **v0.13 上下文压缩**（阶段四 4.3）：老历史 LLM 摘要 + Structured State 锚定 + `MAX_ITERATIONS` 调大
- [ ] **v0.14 plan 引导**：规划模式引导
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
- agent loop 无 try/except 兜底，工具失败会直接抛异常终止——这是有意为之，保持核心逻辑清晰。
- 迭代上限 `MAX_ITERATIONS = 10`（硬编码）。超限直接返回 `"达到最大迭代次数"`，不报错——长任务可能静默截断。

## 已知行为

- v0.01 无工具：LLM 回复不含 `tool_calls` 即视为完成，agent loop 结束。
- 结束条件：LLM 回复不含 `tool_calls` 即视为完成。
