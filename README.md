<div align="center">

# agent-from-scratch

### 逐步生长的编程 Agent · 从零开始构建一个能干活的 AI Agent

A step-by-step tutorial for building a coding agent from scratch — in incremental versions (v0.01 → ongoing).

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/dependencies-zero-green)](#快速开始)
[![Versions](https://img.shields.io/badge/versions-v0.01%E2%86%92ongoing-orange)](#学习路径)
[![License](https://img.shields.io/badge/license-MIT-lightgrey)](./LICENSE)

**[English](./README_EN.md)** · **中文**

</div>

---

> **这是什么**：一个按 git tag 切片、按能力阶段组织的教学仓库。从最小可用的 agent loop（v0.01，无工具纯对话）起步，每个版本**只引入一个新概念**，一路长到能读写文件、跑命令、跑测试的 Mini Agent（持续迭代，不设上限）。
>
> **适合谁**：想搞清楚 LLM agent 到底怎么转起来的开发者。不调框架、不装 LangChain，只用 Python 标准库从零搭。

> **当前状态**：最新代码和教程已到 `v0.16`（Plan-driven Execution）。

> **阅读模型**：`docs/tutorials/` 以默认分支上的最新版本为准；每课声明要运行的代码 tag，并把源码索引固定到该 tag。请在主线/网页读教程，在本地 checkout tag 跑代码。历史 tag 不重打，`v0.01` 至 `v0.06` tag 内的旧教程路径只是历史副本。

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

> 需要 Python 3.10+（项目统一使用 Python 3.10 及以上版本）。

## 学习路径

教程按能力阶段组织，版本仍然是代码演进和 Git tag 的基本单位。建议从阶段一开始，按顺序学习到阶段三；完成 `v0.10` 后即可得到一个能完成基础编程任务的 Mini Agent，再继续学习阶段四的上下文管理。**这是本仓库的核心学习路径**。

<table>
  <thead>
    <tr><th>版本</th><th>主题</th><th>简介</th></tr>
  </thead>
  <tbody>
    <tr><th colspan="3">阶段一 · 理解 Agent Loop</th></tr>
    <tr><td><strong>v0.01</strong></td><td><a href="./docs/tutorials/01-minimal-loop.md">最简 agent loop</a></td><td>建立最小对话循环，理解请求、回复与结束条件。</td></tr>
    <tr><th colspan="3">阶段二 · 工具与安全</th></tr>
    <tr><td><strong>v0.02</strong></td><td><a href="./docs/tutorials/02-first-tool.md">第一个工具</a></td><td>接入 calculate 工具，跑通 function calling 的基本协议。</td></tr>
    <tr><td><strong>v0.03</strong></td><td><a href="./docs/tutorials/03-file-tools.md">文件读写工具</a></td><td>让 Agent 能读取和写入文件，开始处理真实项目内容。</td></tr>
    <tr><td><strong>v0.04</strong></td><td><a href="./docs/tutorials/04-permission-gate.md">权限闸门</a></td><td>为有副作用的工具加入 allow、deny、ask 三态授权。</td></tr>
    <tr><th colspan="3">阶段三 · Mini Agent 里程碑</th></tr>
    <tr><td><strong>v0.05</strong></td><td><a href="./docs/tutorials/05-streaming.md">流式输出</a></td><td>逐块接收并显示 LLM 回复，改善交互反馈。</td></tr>
    <tr><td><strong>v0.06</strong></td><td><a href="./docs/tutorials/06-concurrent-tool-calls.md">并发 tool_calls</a></td><td>并发执行同一轮的多个工具调用，减少等待时间。</td></tr>
    <tr><td><strong>v0.07</strong></td><td><a href="./docs/tutorials/07-system-prompt.md">系统提示词工程化</a></td><td>分层组织身份、规则和环境信息，稳定 Agent 行为。</td></tr>
    <tr><td><strong>v0.08</strong></td><td><a href="./docs/tutorials/08-file-operations.md">文件操作补全</a></td><td>补齐目录列举、精确编辑和正则搜索能力。</td></tr>
    <tr><td><strong>v0.09</strong></td><td><a href="./docs/tutorials/09-permission-upgrade.md">权限系统升级</a></td><td>按工具和路径或命令模式细粒度匹配权限规则。</td></tr>
    <tr><td><strong>v0.10</strong></td><td><a href="./docs/tutorials/10-shell-execution.md">shell 执行</a></td><td>执行命令并处理超时、输出截断和命令级授权。</td></tr>
    <tr><th colspan="3">阶段四 · Context Management</th></tr>
    <tr><td><strong>v0.11</strong></td><td><a href="./docs/tutorials/11-context-architecture.md">上下文架构</a></td><td>将持久执行状态与可裁剪的对话上下文分离。</td></tr>
    <tr><td><strong>v0.12</strong></td><td><a href="./docs/tutorials/12-token-budget-trimming.md">预算与裁剪</a></td><td>估算 token 并按完整对话轮次安全裁剪历史。</td></tr>
    <tr><td><strong>v0.13</strong></td><td><a href="./docs/tutorials/13-context-compaction.md">上下文压缩</a></td><td>用历史摘要和结构化状态降低长任务的遗忘。</td></tr>
    <tr><td><strong>v0.13.1</strong></td><td><a href="./docs/tutorials/13-context-compaction.md#v0131-增补context-observability">Context Observability 增补</a></td><td>提供上下文 token 统计、裁剪/压缩事件和结构化快照。</td></tr>
    <tr><th colspan="3">阶段五 · 项目感知与任务编排</th></tr>
    <tr><td><strong>v0.14</strong></td><td><a href="./docs/tutorials/14-project-instructions.md">Project Instructions</a></td><td>自动发现并注入项目级 AGENTS.md 指令。</td></tr>
    <tr><td><strong>v0.15</strong></td><td><a href="./docs/tutorials/15-task-state.md">Todo / Task State</a></td><td>用显式 Todo 状态跟踪多步任务进度。</td></tr>
    <tr><td><strong>v0.16</strong></td><td><a href="./docs/tutorials/16-plan-driven-execution.md">Plan-driven Execution</a></td><td>通过计划、验证证据和失败状态闭合执行流程。</td></tr>
    <tr><td>后续版本</td><td>按需追加</td><td>继续扩展恢复、记忆、沙箱等能力。</td></tr>
  </tbody>
</table>

完成 `v0.10` 后，Agent 已经能够读取项目、搜索和修改文件、执行命令、运行测试，并通过权限机制控制高风险操作。这是本仓库的第一个阶段性里程碑。

**如何切版本学习：**

```bash
git tag                    # 看所有版本
git checkout v0.01          # 切到第一版
# 先读 docs/tutorials/README.md，再读 01-minimal-loop.md
# 跑课程"使用指导"里的命令
git checkout v0.02          # 看差异：git diff v0.01..v0.02
# 按教程总览中的阶段和版本顺序继续学习
```

命令行参数是首条任务，不是一次性模式；处理后仍进入交互循环，可用空行、`exit`、`quit` 或 EOF 退出。教程命令默认使用 Bash/zsh；PowerShell 先设置 `$env:PYTHONPATH="src"`，再运行对应的 `python ...` 命令。

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

- **渐进式生长**：每次只加刚好够用的能力，避免过度设计。新功能意图先记录到对应计划文档；只有运行时约束变化才更新 `AGENTS.md`。
- **核心 loop 保持清晰**：agent loop 不对 LLM 或 CLI 顶层异常做兜底；工具层捕获 handler 异常并将错误结果回灌给 LLM。复杂容错按需在工具层引入。
- **零依赖**：只用标准库，保持自包含、易部署。HTTP 客户端约束与其他运行时契约见 [`AGENTS.md`](./AGENTS.md)。

## 文档

- [教学路径索引](./docs/tutorials/README.md) —— **从这里开始学**
- [完整使用手册](./docs/operation/manual.md) —— 最新版全量用法、配置、FAQ
- [上下文架构说明](./docs/operation/context-architecture.md) —— 当前上下文视图、状态与预算机制
- [路线图与计划](./docs/plans/teaching-repo-plan.md) —— 阶段导航与版本切分方案
- [AGENTS.md](./AGENTS.md) —— 运行时硬约束与精简架构索引
- [治理文档](./docs/governance/README.md) —— 详细规范与决策记录
- [CHANGELOG.md](./CHANGELOG.md) —— 按版本记录的变更

## 测试

开发环境安装 pytest 后运行完整测试：

```bash
PYTHONPATH=src python -m pytest -q
```

不安装 pytest 时，可直接运行各测试文件中的标准库 smoke test，详见[操作手册](./docs/operation/manual.md#4-测试)。

## 贡献

欢迎提 Issue / PR。如果是新增版本切片，请先读 `docs/plans/teaching-repo-plan.md`、[教程作者规范](./docs/governance/tutorial-authoring.md) 和 `AGENTS.md`，遵循“每版只加一个概念”的切分原则。

## License

MIT — 见 [LICENSE](./LICENSE)

---

<div align="center">

**如果这个仓库对你有帮助，欢迎 Star ⭐ 让更多人看到。**

</div>

<!-- 关键词 / Keywords: agent tutorial, agent 教程, LLM agent, coding agent, Python agent, function calling, 从零构建 agent, AI agent, 大模型 agent, agent loop, tool calling -->
