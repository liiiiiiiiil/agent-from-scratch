# 教学路径（tutorials）

本目录是 `mini_agent` 教学仓库的核心：教程文件都放在同一个目录中，`README.md` 负责按阶段导航，具体课程负责解释对应版本的代码变化。

## 先从这里开始

- 第一次学习 Agent：从阶段一开始，按顺序读到阶段三。
- 想理解一个可工作的 Mini Agent：重点完成阶段一至阶段三，并完成阶段三的实践任务。
- 只想查某个功能：直接从下面的版本表进入对应课程。

## 学习阶段

### 阶段一：理解 Agent Loop

目标：理解 LLM 调用、`messages`、循环和结束条件，先写出最小的对话 Agent。

| 版本 | 主题 | 文档 |
|---|---|---|
| v0.01 | 最简 agent loop | [01-minimal-loop.md](01-minimal-loop.md) |

### 阶段二：工具与安全

目标：让 Agent 能调用工具，并在产生文件副作用前请求用户授权。

| 版本 | 主题 | 文档 |
|---|---|---|
| v0.02 | 第一个工具 | [02-first-tool.md](02-first-tool.md) |
| v0.03 | 文件读写工具 | [03-file-tools.md](03-file-tools.md) |
| v0.04 | 权限闸门 | [04-permission-gate.md](04-permission-gate.md) |

### 阶段三：Mini Agent 里程碑

目标：补齐交互、文件操作和命令执行能力，形成一个能完成基础编程任务的 Mini Agent。

| 版本 | 主题 | 文档 |
|---|---|---|
| v0.05 | 流式输出 | [05-streaming.md](05-streaming.md) |
| v0.06 | 并发 tool_calls | [06-concurrent-tool-calls.md](06-concurrent-tool-calls.md) |
| v0.07 | 系统提示词工程化 | [07-system-prompt.md](07-system-prompt.md) |
| v0.08 | 文件操作补全 | [08-file-operations.md](08-file-operations.md) |
| v0.09 | 权限系统升级 | [09-permission-upgrade.md](09-permission-upgrade.md) |
| v0.10 | shell 执行 | [10-shell-execution.md](10-shell-execution.md) |

完成 `v0.10` 后，Agent 已经能够读取项目、搜索和修改文件、执行命令、运行测试，并通过权限机制控制高风险操作。这是本仓库的第一个阶段性里程碑，但它仍然缺少长上下文管理、任务规划、失败恢复和安全隔离等进阶能力。

### 阶段四：Context Management

目标：让 Agent 在有限 Context Window 下持续稳定执行复杂任务——状态与上下文分离、预算感知、超限裁剪、历史压缩。

| 版本 | 主题 | 文档 |
|---|---|---|
| v0.11 | 上下文架构（AgentState + ContextManager） | [11-context-architecture.md](11-context-architecture.md) |
| v0.12 | 预算与裁剪（token 估算 + 按轮次原子 trimming） | [12-token-budget-trimming.md](12-token-budget-trimming.md) |
| v0.13 | 上下文压缩（LLM 摘要 + State 锚定） | 待落地 |

### 阶段五：进阶能力（规划中）

这一阶段继续沿用版本教程，学习重点回到"让 Agent 能够稳定完成更长、更复杂的任务"。

| 版本 | 主题 | 状态 |
|---|---|---|
| v0.14 | plan 引导 | 待落地 |
| 后续版本 | 失败恢复、验证闭环、可观测性、记忆、沙箱等 | 按需追加 |

## 版本与 Git tag

每个已完成版本仍然对应一个 Git tag。阶段是学习导航，版本是代码快照；两者互不替代。

```bash
git tag                    # 看所有版本
git checkout v0.01         # 切到指定版本
git diff v0.01..v0.02      # 查看两个版本之间的变化
```

学习某个版本时，先 checkout 对应 tag，再阅读同名课程中的“使用指导”和“核心概念”。

## 环境准备（一次性）

1. **Python 3.9+**（用了 `dict[str, ...]` 等新语法）
2. **克隆仓库**：
   ```bash
   git clone <repo>
   cd mini_agent
   ```
3. **配置 LLM 网关**：复制 `src/mini_agent/config_example.py` 为 `src/mini_agent/config_local.py`，填入你的 `BASE_URL`/`API_KEY`/`MODEL`（`config_local.py` 不进 git，由 `config.py` 自动加载）
4. **选择运行方式**：
   - 开发模式（推荐）：`pip install -e .`
   - 免安装：`$env:PYTHONPATH="src"` (PowerShell) / `PYTHONPATH=src` (bash)

## 如何切版本

```bash
git tag                    # 看所有版本
git checkout v0.01         # 切到第一版
# 读 01-minimal-loop.md，跑其中的"使用指导"
git checkout v0.02         # 看差异，读 02-first-tool.md
# 按上面的阶段和版本顺序继续学习
```

## 阶段三实践任务

完成 `v0.10` 后，可以让 Agent 完成一个完整的小任务：检查一个失败的测试，定位原因，修改代码，运行测试，并总结结果。这个任务可以验证它是否已经具备 Mini Agent 的基本工作闭环。

## 完整使用手册

[`docs/operation/manual.md`](../operation/manual.md) — 最新版的完整用法、配置、FAQ。
教学文档的"使用指导"只讲**本版**怎么用，想看全量用法去翻 manual。
