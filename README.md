<div align="center">

# agent-from-scratch

### 逐步生长的编程 Agent · 从零开始构建一个能干活的 AI Agent

A step-by-step tutorial for building a coding agent from scratch — in incremental versions (v0.01 → ongoing).

[![Python](https://img.shields.io/badge/Python-3.9%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/dependencies-zero-green)](#快速开始)
[![Versions](https://img.shields.io/badge/versions-v0.01%E2%86%92ongoing-orange)](#学习路径)
[![License](https://img.shields.io/badge/license-MIT-lightgrey)](./LICENSE)

**[English](./README_EN.md)** · **中文**

</div>

---

> **这是什么**：一个按 git tag 切片、按能力阶段组织的教学仓库。从最小可用的 agent loop（v0.01，无工具纯对话）起步，每个版本**只引入一个新概念**，一路长到能读写文件、跑命令、跑测试的 Mini Agent（持续迭代，不设上限）。
>
> **适合谁**：想搞清楚 LLM agent 到底怎么转起来的开发者。不调框架、不装 LangChain，只用 Python 标准库从零搭。

> **当前状态**：最新代码和教程已到 `v0.13.1`（上下文压缩 + Context Observability 增强）；`v0.14` 及以后仍在规划中。

## 为什么用这个仓库学 Agent

- **零第三方依赖** —— 全程只用 Python 标准库（`http.client` / `json` / `concurrent.futures`），不装 LangChain、不装 requests。代码自包含，每一行都能读懂。
- **版本切片，每版只加一个概念** —— `git diff v0.01..v0.02` 就是"加一个工具"的全部改动，diff 可读，学习负担低。版本持续递增，不设上限。
- **真实可跑的 agent** —— 不是玩具 demo：支持 function calling、流式输出、权限闸门、并发工具调用，能真的读写文件、跑命令。
- **配套中文教程** —— 教程按学习阶段导航，每个版本一份教学文档（`docs/tutorials/`），讲清"为什么这么设计"，不只是贴代码。
- **渐进式生长的设计哲学** —— 演示一个 agent 项目如何从最小 loop 长到可工作的 Mini Agent，每一步的取舍都有据可查。

## 快速开始

```bash
# 1. 克隆并进入仓库
git clone https://github.com/liiiiiiiiil/agent-from-scratch.git
cd agent-from-scratch

# 2. 配置 LLM 网关
cp src/mini_agent/config_example.py src/mini_agent/config_local.py
#    编辑 config_local.py 填入你的 BASE_URL / API_KEY / MODEL（此文件不进 git）

# 3. 安装（推荐）
pip install -e .

# 4. 跑起来
python -m mini_agent "帮我算一下 123 * 456"
python -m mini_agent             # 或交互式输入
```

不安装也可以运行。Linux/macOS 使用：

```bash
PYTHONPATH=src python -m mini_agent "帮我算一下 123 * 456"
```

PowerShell 使用：

```powershell
$env:PYTHONPATH="src"
python -m mini_agent "帮我算一下 123 * 456"
```

> 需要 Python 3.9+（用了 `dict[str, ...]` 等新语法）。

## 学习路径

教程按能力阶段组织，版本仍然是代码演进和 Git tag 的基本单位。建议从阶段一开始，按顺序学习到阶段三；完成 `v0.10` 后即可得到一个能完成基础编程任务的 Mini Agent，再继续学习阶段四的上下文管理。**这是本仓库的核心学习路径**。

### 阶段一：理解 Agent Loop

目标：理解 LLM 调用、`messages`、循环和结束条件。

| 版本 | 主题 | 本版新增 | 教程 |
|---|---|---|---|
| **v0.01** | 最简 agent loop | `call_llm` + `agent_loop`（无工具纯对话） | [01-minimal-loop.md](./docs/tutorials/01-minimal-loop.md) |

### 阶段二：工具与安全

目标：让 Agent 能调用工具，并控制文件修改等副作用。

| 版本 | 主题 | 本版新增 | 教程 |
|---|---|---|---|
| **v0.02** | 第一个工具 | `Tool`/`ToolRegistry` + `calculate` + function calling | [02-first-tool.md](./docs/tutorials/02-first-tool.md) |
| **v0.03** | 文件读写工具 | `read_file` / `write_file` | [03-file-tools.md](./docs/tutorials/03-file-tools.md) |
| **v0.04** | 权限闸门 | `permission.py`（allow/deny/ask 三态） | [04-permission-gate.md](./docs/tutorials/04-permission-gate.md) |

### 阶段三：Mini Agent 里程碑

目标：补齐交互、文件操作和命令执行能力，形成一个能完成基础编程任务的 Mini Agent。

| 版本 | 主题 | 本版新增 | 教程 |
|---|---|---|---|
| **v0.05** | 流式输出 | `call_llm` 改流式 + 打字机效果 | [05-streaming.md](./docs/tutorials/05-streaming.md) |
| **v0.06** | 并发 tool_calls | `ThreadPoolExecutor` 并发执行 | [06-concurrent-tool-calls.md](./docs/tutorials/06-concurrent-tool-calls.md) |
| **v0.07** | 系统提示词工程化 | system prompt 从一行扩到完整规范 | [07-system-prompt.md](./docs/tutorials/07-system-prompt.md) |
| **v0.08** | 文件操作补全 | `list_dir` / `edit_file` / `grep` | [08-file-operations.md](./docs/tutorials/08-file-operations.md) |
| **v0.09** | 权限系统升级 | 二维权限 (tool_name, pattern) + fnmatch 通配符匹配 | [09-permission-upgrade.md](./docs/tutorials/09-permission-upgrade.md) |
| **v0.10** | shell 执行 | `run_shell` 工具 + subprocess + 超时 + 输出截断 + 二维命令模式权限 | [10-shell-execution.md](./docs/tutorials/10-shell-execution.md) |

> `v0.06.1` 是 v0.06 的协议状态修订，没有独立课程；需要复现该修订时可直接 `git checkout v0.06.1`。

完成 `v0.10` 后，Agent 已经能够读取项目、搜索和修改文件、执行命令、运行测试，并通过权限机制控制高风险操作。这是本仓库的第一个阶段性里程碑。

### 阶段四：Context Management

目标：让 Agent 在有限 Context Window 下持续稳定执行复杂任务——状态与上下文分离、预算感知、超限裁剪、历史压缩。

| 版本 | 主题 | 本版新增 | 教程 |
|---|---|---|---|
| **v0.11** | 上下文架构 | `AgentState` + `ContextManager` + Executor 结果回调 | [11-context-architecture.md](./docs/tutorials/11-context-architecture.md) |
| **v0.12** | 预算与裁剪 | token 估算 + Context Budget + 按轮次原子 trimming | [12-token-budget-trimming.md](./docs/tutorials/12-token-budget-trimming.md) |
| **v0.13** | 上下文压缩 | LLM 摘要 + Structured State 锚定 | [13-context-compaction.md](./docs/tutorials/13-context-compaction.md) |
| **v0.13.1** | 上下文压缩增强 | ContextStats + trimming/compaction 可观测性 | [13-context-compaction.md](./docs/tutorials/13-context-compaction.md)（同一教程） |

### 阶段五：进阶能力（规划中）

| 版本 | 主题 | 状态 |
|---|---|---|
| v0.14 | plan 引导 | 待落地 |
| 后续版本 | 失败恢复、验证闭环、可观测性、记忆、沙箱等 | 按需追加 |

**如何切版本学习：**

```bash
git tag                    # 看所有版本
git checkout v0.01          # 切到第一版
# 先读 docs/tutorials/README.md，再读 01-minimal-loop.md
# 跑课程"使用指导"里的命令
git checkout v0.02          # 看差异：git diff v0.01..v0.02
# 按教程总览中的阶段和版本顺序继续学习
```

## 项目结构

```
agent-from-scratch/
├── src/mini_agent/
│   ├── agent.py            # agent loop：call_llm + agent_loop
│   ├── __main__.py         # CLI 入口
│   ├── config.py           # 配置占位 + 自动加载 config_local.py
│   ├── config_example.py   # 配置模板（复制为 config_local.py 使用）
│   ├── context.py          # ContextManager：LLM 调用前统一入口
│   ├── state.py            # AgentState：独立于 messages 的执行状态
│   ├── permission.py       # 权限闸门：allow/deny/ask 三态
│   ├── prompt.py           # 分层组装 system prompt
│   └── tools/
│       ├── base.py         # Tool / ToolRegistry / ToolExecutor（含结果回调）
│       ├── calc.py         # calculate 工具
│       ├── file.py         # read_file / write_file / edit_file / list_dir / grep 工具
│       └── shell.py        # run_shell 工具
├── tests/                  # smoke tests
├── docs/
│   ├── tutorials/          # 按阶段导航、按版本切片的教程（核心）
│   ├── plans/              # 路线图、功能计划
│   ├── operation/          # 运行手册、使用指南
│   └── governance/         # 治理文档、决策记录
└── examples/               # 示例 IO 文件
```

## 设计哲学

- **渐进式生长**：每次只加刚好够用的能力，避免过度设计。新功能先在 `AGENTS.md` 记下意图，再落地代码。
- **核心 loop 保持清晰**：agent loop 不对 LLM 或 CLI 顶层异常做兜底；工具层捕获 handler 异常并将错误结果回灌给 LLM。复杂容错按需在工具层引入。
- **零依赖**：只用标准库，保持自包含、易部署。换 HTTP 客户端会踩坑（见 `AGENTS.md` 关键约束）。

## 文档

- [教学路径索引](./docs/tutorials/README.md) —— **从这里开始学**
- [完整使用手册](./docs/operation/manual.md) —— 最新版全量用法、配置、FAQ
- [路线图与计划](./docs/plans/teaching-repo-plan.md) —— 阶段导航与版本切分方案
- [AGENTS.md](./AGENTS.md) —— 项目架构与约束备忘
- [CHANGELOG.md](./CHANGELOG.md) —— 按版本记录的变更

## 测试

开发环境安装 pytest 后运行完整测试：

```bash
PYTHONPATH=src python -m pytest -q
```

不安装 pytest 时，可直接运行各测试文件中的标准库 smoke test，详见[操作手册](./docs/operation/manual.md#4-测试)。

## 贡献

欢迎提 Issue / PR。如果是新增版本切片，请先读 `docs/plans/teaching-repo-plan.md` 和 `AGENTS.md`，遵循"每版只加一个概念"的切分原则。

## License

MIT — 见 [LICENSE](./LICENSE)

---

<div align="center">

**如果这个仓库对你有帮助，欢迎 Star ⭐ 让更多人看到。**

</div>

<!-- 关键词 / Keywords: agent tutorial, agent 教程, LLM agent, coding agent, Python agent, function calling, 从零构建 agent, AI agent, 大模型 agent, agent loop, tool calling -->
