# 多阶段教学仓库方案

> 状态：方案已确认，教程导航按单目录分阶段结构落地
> 决策记录：重置 main 到 v0.01 / 复用 docs/ 加 tutorials 子目录 / v0.01 为无工具纯对话 loop / 先出方案不动代码
> 更新：教学文档模板加入"使用指导"章节，随版本演进
> 更新：改为持续迭代模式，不设固定版本上限，每次加一个功能，直到达到轻量编程 agent 的能力集
> 更新：教程入口按阶段组织，具体课程仍按版本放在 `docs/tutorials/` 根目录，不新增阶段子目录
> 更新：v0.011 拆为 v0.11–v0.13"阶段四：Context Management"三个版本（tag 命名弃用 v0.011），plan 引导顺移至 v0.14，详见 `context-management-plan.md`

## 1. 目标

把 `mini_agent` 改造成**按能力阶段导航、按版本号切片的教学仓库**：学习者从阶段一开始，按 `v0.01 → v0.02 → ...` 顺序 checkout，每个版本只引入一个新概念，配套一份教学文档，最终能理解一个能读写改文件、跑命令、跑测试的编程 agent 是怎么一步步长出来的。

本仓库不限于固定版本数，改为持续迭代模式，每次加一个功能，直到达到轻量编程 agent 的能力集。后续版本按需追加，不设上限。

## 2. 版本切片表

每版相对上一版只加一个概念。tag 名 `v0.X`。

| 版本 | 主题 | 相对上一版只加什么 | 教学文档要点 |
|---|---|---|---|
| v0.01 | 最简 agent loop | `call_llm`（非流式）+ `agent_loop`（无工具纯对话）+ `__main__` + `config.py` | 什么是 agent loop；messages 列表；结束条件；http.client 调 LLM |
| v0.02 | 第一个工具 | `Tool`/`ToolRegistry`/`ToolExecutor` + `calculate` + function calling 协议 | 工具怎么接到 loop 里；`role=tool` 回灌；工具签名约定 |
| v0.03 | 文件读写工具 | `read_file`/`write_file` | agent 如何读写文件；副作用工具的风险 |
| v0.04 | 权限闸门 | `permission.py`（allow/deny/ask 三态）+ `PermissionGate` + `write_file` 改 ASK | 让 agent 改文件前先问人；三态权限模型 |
| v0.05 | 流式输出 | `call_llm` 改流式 + chunk 拼接 + 打字机效果 | 为什么用流式；`Accept-Encoding: identity` 踩坑 |
| v0.06 | 并发 tool_calls | `ThreadPoolExecutor` 并发执行同一轮多个 tool_calls | 一轮多工具的并发模型 |
| v0.07 | 系统提示词工程化 | system prompt 从一行扩到完整规范 | prompt 即配置 |
| v0.08 | 文件操作补全 | `list_dir`/`edit_file`（精准替换）/`grep` | 精准编辑而非整文件重写 |
| v0.09 | 权限系统升级 | 二维权限 (tool_name, pattern) + once/always 回复区分 + fnmatch 通配符匹配 | 从一维到二维；命令模式粒度控制 |
| v0.10 | shell 执行 | `run_shell` 工具 + subprocess 超时 + 输出截断 + 二维命令模式权限 | 跑测试/跑脚本；shell 权限从严 |
| v0.11 | 上下文架构 | `AgentState`（状态独立于 messages）+ `ContextManager`（LLM 调用前统一入口） | Agent State ≠ LLM Context；上下文构建与 loop 解耦 |
| v0.12 | 预算与裁剪 | token 估算 + Context Budget + 按轮次原子 trimming | Context 是有限资源；超限优先删低价值信息而非崩溃 |
| v0.13 | 上下文压缩 | 老历史 LLM 摘要 + Structured State 锚定 + `MAX_ITERATIONS` 调大 | 压缩不失忆；State 是锚，Summary 允许有损 |
| v0.13.1 | 上下文压缩增强 | Context Observability（无独立教程，追加到第 13 课） | 观察预算、trimming 与 compaction |
| v0.14 | plan 引导 | 规划模式引导 | 规划与执行分离 |
| ... | ... | ...（持续迭代，按需追加行） | ... |

> 切分原则：每版只引入一个新概念，代码差异控制在"一个文件或一个新模块"量级，让 `git diff v0.(X-1)..v0.X` 可读。

## 3. 目录结构

复用现有 `docs/` 三级结构，在其下加 `tutorials/` 子目录。代码仍在 `src/`，始终是最新版状态。

```
mini_agent/
├── AGENTS.md                # 更新：路线图改为版本切片表
├── CHANGELOG.md             # 新增：每版一段 Added/Changed/Why
├── README.md                # 更新：加"教学路径"章节
├── pyproject.toml           # version 跟随最新 tag
├── src/mini_agent/          # 代码（main 始终是最新版）
├── tests/
├── examples/
└── docs/
    ├── README.txt           # 更新：加 tutorials/ 说明
    ├── governance/          # 不变
    ├── plans/               # 不变（本方案文档放此）
    ├── operation/           # 不变
    └── tutorials/          # 教学文档：README 按阶段导航，课程按版本切片
        ├── README.md        # 教学路径索引
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
        ├── 13-context-compaction.md
        ├── 14-plan-guidance.md
        └── ...（持续迭代，按需追加，仍放在此目录）
```

### 3.1 各目录职责边界

| 目录 | 面向谁 | 内容性质 | 更新频率 |
|---|---|---|---|
| `docs/tutorials/` | 学习者 | 概念讲解 + 代码差异解读 + 使用指导 | 每版加一篇 |
| `docs/governance/` | 维护者 | 约束、规范、决策记录 | 偶尔 |
| `docs/plans/` | 维护者 | 方案、任务拆解（本文件所在目录） | 偶尔 |
| `docs/operation/` | 使用者 | 运行手册、使用指南 | 跟随代码变 |
| `AGENTS.md` | 维护者 + AI | 项目总纲、路线图、约束备忘 | 每版同步 |

## 4. 教学文档模板

每份 `docs/tutorials/0X-xxx.md` 用统一结构，降低学习者认知负担。文件名：两位序号 + 连字符主题，与 `docs/plans/` 约定一致。

```markdown
# 第 X 课：{主题}

> 版本 v0.X | [上一课](0Y-yyy.md) | [下一课](0Z-zzz.md)

## 本课目标
一句话：这版加了什么、解决什么问题。

## 前置
- 已读上一课文档
- `git checkout v0.X` 切到本版代码

## 新增/改动了什么
列出变动文件 + `git diff --stat v0.(X-1)..v0.X` 输出。

## 核心概念
### {概念名}
代码片段（从 src/ 摘关键部分）+ 解释。
重点用 `git diff v0.(X-1)..v0.X -- <file>` 的真实差异，让学习者看到"加了哪几行"。

## 为什么这样设计
记录踩坑/权衡。v0.05 要讲"为什么不用 requests"；v0.04 要讲"为什么三态不是两态"。

## 使用指导
### 本版可用的命令
列出本版可运行的命令（单次任务 / 交互模式 / 测试）。

### 本版典型示例
3-5 个能跑的示例，每个给命令 + 预期输出。
示例随版本变化：v0.01 只能对话，v0.02+ 能调工具，v0.04+ 有权限交互。

### 本版独有特性
本版新引入的、值得观察的现象。
例：v0.04 的权限交互 `[once/always/reject]`；v0.05 的流式打字机效果；v0.06 的并发 tool_calls 日志。

## 本版完整代码
指向 `src/mini_agent/` 对应文件，不贴大段代码。
```

### 4.1 文档写作约定

- **不贴大段完整代码**，只摘关键片段 + 指向文件路径，避免代码改了文档不同步
- **差异优先**：能用 `git diff` 说清的就用 diff，不重写一遍
- **每篇可独立读懂**：假设读者刚看完上一课，但不假设他记得本课细节
- **使用指导必须有**：每课给能跑的命令 + 预期输出，让学习者有反馈
- **示例随版本演进**：每版的典型示例要体现本版新能力，不重复上一版已覆盖的

## 5. 各版本"使用指导"差异要点

每版的使用指导会随工具集和能力变化。下表列出每版使用指导应包含的独有内容：

| 版本 | 使用指导独有内容 |
|---|---|
| v0.01 | 仅对话；交互模式 `你:` 提示符；`exit`/`quit` 退出；无工具可调 |
| v0.02 | 调 `calculate`；观察 `role=tool` 消息回灌；试 `"计算 3+5*2"` |
| v0.03 | 调 `read_file`/`write_file`；`write_file` 的 `"路径\n内容"` 参数格式；试 `"读取 examples/input.txt 并总结"` |
| v0.04 | `write_file` 触发权限提示；`once`/`always`/`reject` 三选项说明；观察拒绝后 LLM 如何反应 |
| v0.05 | 流式打字机效果（看终端逐字出现）；对比 v0.04 的"整段输出"差异 |
| v0.06 | 让模型一次发多个 tool_calls（如同时调 `read_file` ×2）；观察并发执行日志 |
| v0.07 | 对比 v0.06 vs v0.07 的 system prompt 让 LLM 行为差异；试长任务 |
| v0.08 | `edit_file` 精准替换示例；`grep` 搜索示例；`list_dir` 列目录 |
| v0.09 | 观察二维权限：按命令模式询问；`git *` 批准后同类免问；对比 v0.04 的一维权限差异 |
| v0.10 | `run_shell` 跑 `python tests/test_tools.py`；观察二维命令模式权限；试 "跑一下测试" |
| v0.11 | 对比 v0.10：loop 不再直接拼 messages；演示"同一 State 构建不同 Context" |
| v0.12 | 构造长任务观察预算检查与裁剪日志；验证裁剪后 agent 不崩、消息协议合法 |
| v0.13 | 长任务验证多次压缩后仍能继续原任务；观察 summary 与 Structured State 注入 |
| v0.14 | plan 模式引导示例；观察规划与执行的分离 |

### 5.1 与 `docs/operation/manual.md` 的分工

- `docs/operation/manual.md`：**最新版**完整使用手册，不按版本切片，每版完成时同步更新。涵盖所有当前可用工具、配置、权限交互、测试、FAQ。
- 教学文档的"使用指导"：只讲**本版**怎么用，是"学习者手册"，跟着版本走。不重复 manual.md 的所有细节，但告诉学习者"想看完整用法去翻 manual.md"。

## 6. `docs/tutorials/README.md` 内容要点

教程路径索引，给学习者一个入口。所有课程文件直接放在 `docs/tutorials/` 根目录，不新增阶段子目录；README 负责把版本课程分成清晰的学习阶段：

```markdown
# 教学路径

## 先从这里开始
说明完整学习路径、Mini Agent 里程碑和功能查找方式。

## 阶段一：理解 Agent Loop
v0.01

## 阶段二：工具与安全
v0.02 → v0.04

## 阶段三：Mini Agent 里程碑
v0.05 → v0.10

## 阶段四：Context Management
v0.11 → v0.13

## 阶段五：进阶能力（规划中）
v0.14 及以后

## 环境准备（一次性）
保留 Python、配置和安装说明。

## 版本与 Git tag
说明如何 checkout 版本和使用 git diff 查看演进。

## 完整使用手册
docs/operation/manual.md — 最新版的完整用法。
```

## 7. CHANGELOG.md 格式

```markdown
# Changelog

## [v0.01] - 最简 agent loop
### Added
- `src/mini_agent/agent.py`：call_llm（非流式）+ agent_loop
- `src/mini_agent/__main__.py`：CLI 入口
- `src/mini_agent/config.py`：BASE_URL/API_KEY/MODEL/MAX_ITERATIONS
### Why
- 从最小可用的对话 loop 起步，先讲清"什么是 agent loop"。

## [v0.02] - 第一个工具
...
```

每版一段，Added/Changed/Why 三段式。Why 段记录"为什么这版要加这个"，与教学文档的"为什么这样设计"呼应但更简短。

## 8. Git 操作清单

### 8.1 初始化：重置 main 到 v0.01

> 决策已确认：重置 main 到 v0.01，丢失现有 4 个 commit。

```bash
# 1. 备份现有代码（万一要参考）
git branch backup-pre-teaching

# 2. 创建无历史的新 main
git checkout --orphan main-new
git rm -rf .

# 3. 放入 v0.01 代码 + 文档（见第 9 节）
#    只放：agent.py(非流式版) / __main__.py / config.py / __init__.py
#    + pyproject.toml + README.md + AGENTS.md(精简版)
#    + docs/tutorials/01-minimal-loop.md + docs/tutorials/README.md
#    + CHANGELOG.md + .gitignore

git add -A
git commit -m "v0.01: 最简 agent loop"
git tag -a v0.01 -m "v0.01: 最简 agent loop"

# 4. 替换 main
git branch -D main
git branch -m main-new main
```

### 8.2 逐版往上加

```bash
# 1. 在 main 上开发到本版状态
# 2. 更新 CHANGELOG.md、docs/tutorials/0X-xxx.md、AGENTS.md 路线图打勾
# 3. 跑 tests
$env:PYTHONPATH="src"; python tests/test_tools.py
# 4. 提交 + 打 tag
git add -A
git commit -m "v0.X: {主题}"
git tag -a v0.X -m "v0.X: {主题}"
```

### 8.3 学习者使用方式

```bash
git clone <repo>
cd mini_agent
git tag                    # 看所有版本
git checkout v0.01          # 切到第一版
# 读 docs/tutorials/01-minimal-loop.md
# 跑：$env:PYTHONPATH="src"; python -m mini_agent "你好"
git checkout v0.02          # 看差异，读 02-first-tool.md
# ...依次到最新版
```

## 9. v0.01 代码清单

v0.01 只保留 4 个源文件，删掉所有工具/权限相关代码。

### 9.1 `src/mini_agent/agent.py`（非流式版，约 30 行）

```python
"""最简 Agent Loop：调 LLM -> 回复 -> 再调，循环到结束或上限。"""

import http.client
import json
from urllib.parse import urlparse

from mini_agent.config import BASE_URL, API_KEY, MODEL, MAX_ITERATIONS


def call_llm(messages):
    """非流式调用 LLM，返回 assistant message dict。"""
    p = urlparse(BASE_URL)
    conn = http.client.HTTPConnection(p.hostname, p.port or 80, timeout=120)
    body = json.dumps(
        {"model": MODEL, "messages": messages},
        ensure_ascii=False,
    ).encode()
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        "Accept-Encoding": "identity",
    }
    conn.request("POST", f"{p.path.rstrip('/')}/chat/completions", body=body, headers=headers)
    resp = conn.getresponse()
    data = json.loads(resp.read().decode("utf-8"))
    conn.close()
    return data["choices"][0]["message"]


def agent_loop(messages):
    """循环：调 LLM -> 打印回复 -> 无 tool_calls 则结束。"""
    for i in range(MAX_ITERATIONS):
        msg = call_llm(messages)
        messages.append(msg)
        print(f"\n=== [{i+1}] LLM 回复 ===")
        print(msg.get("content", ""))
        if not msg.get("tool_calls"):
            return msg.get("content", "")
    return "达到最大迭代次数"
```

> v0.01 的 `call_llm` 不带 `tools` 参数、不流式、不处理 tool_calls。
> `agent_loop` 不执行工具，只判断 `tool_calls` 为空就结束。第一课 loop 概念最干净。

### 9.2 其他文件

- `__main__.py`：与现有版基本一致，system prompt 保持一行 `"你是一个助手。"`
- `config.py`：与现有版一致
- `__init__.py`：空文件

### 9.3 删除的文件（v0.01 不要）

- `src/mini_agent/permission.py`（v0.04 才加）
- `src/mini_agent/tools/`（整个目录，v0.02 才加）
- `tests/test_tools.py`（v0.02 才有工具可测；v0.01 可加一个 `test_loop.py` 测 import 链路）

## 10. 后续版本代码差异要点

- **v0.02**：新增 `tools/base.py`+`calc.py`+`__init__.py`；`agent.py` 加 `tools` 参数 + tool_calls 执行 + `role=tool` 回灌；`__main__.py` system prompt 改"通过调用工具完成任务"；新增 `tests/test_tools.py`
- **v0.03**：新增 `tools/file.py`；`tools/__init__.py` 注册；tests 加 read_file/write_file
- **v0.04**：新增 `permission.py`；`tools/base.py` 的 Executor 加 gate；`tools/__init__.py` 传 gate；write_file 改 ASK
- **v0.05**：`agent.py` 的 `call_llm` 改流式，chunk 累积 content + tool_calls；文档重点讲 `Accept-Encoding: identity` 踩坑
- **v0.06**：`agent.py` 的 tool_calls 执行改 `ThreadPoolExecutor`
- **v0.07**：`__main__.py` system prompt 扩为完整规范
- **v0.08**：新增 `tools/file.py`；`tools/__init__.py` 注册；tests 加 read_file/write_file
- **v0.09**：`permission.py` 升级（`check()` 二维匹配 + `fnmatch` + `approve()` 存 pattern）；`tools/base.py` 的 `PermissionGate.guard()` 从 args 提取 pattern；`PERMISSION_RULES` 支持 dict 格式
- **v0.10**：新增 `tools/shell.py`（`run_shell` + `subprocess` 超时 + 输出截断）；`tools/__init__.py` 注册；`permission.py` 加 `run_shell` 二维权限规则 + `_from_config` 排序修复（`*` 排最前）；`prompt.py` 更新能力描述
- **v0.11**：新增 `state.py`（AgentState）+ `context.py`（ContextManager 统一入口）；`agent.py` 改经 `prepare_messages()` 调 LLM；`tools/base.py` Executor 加结果回调更新 State。纯重构，行为与 v0.10 一致
- **v0.12**：`context.py` 加 `count_tokens`（`len//3` 启发式）+ `ContextBudget`（比例配置）+ `TrimPolicy`（按轮次原子裁剪，无孤儿 tool result）；`config.py` 加 `CONTEXT_WINDOW`
- **v0.13**：`context.py` 加 `compact()`（LLM 摘要 + Structured State 注入 + 失败降级 trimming）；`config.py` 调大 `MAX_ITERATIONS`
- **v0.14**：`prompt.py` 加 plan 引导；`__main__.py` 加 plan 模式入口
- **v0.15+**：按需追加。Context Management 细化方案见 `context-management-plan.md`

## 11. AGENTS.md 更新要点

落地时需同步更新 AGENTS.md：
- "路线图"章节保留版本切片表，并补充教程阶段划分，每完成一版打勾
- "当前架构"章节跟随最新版更新
- "运行"章节不变
- "关键约束"章节不变（这些约束跨版本有效）
- 顶部加一行："本仓库为多阶段教学仓库，按 git tag v0.01→v0.X 顺序学习（持续迭代），见 `docs/tutorials/`"

## 12. 验收标准

方案落地后应满足：
- [ ] `git tag` 按顺序列出 v0.01 起递增的 tag，无固定上限
- [ ] `git checkout v0.X` 后代码可跑（`python -m mini_agent "你好"` 不报错）
- [ ] `docs/tutorials/` 下有与已落地版本对应数量的文档 + 1 份 README 索引
- [ ] 每份教学文档有"使用指导"章节且命令可跑通
- [ ] `CHANGELOG.md` 每个已落地版本一段，每段 Added/Changed/Why 齐全
- [ ] `git diff v0.(X-1)..v0.X --stat` 每版差异在"一两个文件"量级
- [ ] `AGENTS.md` 路线图与版本切片表一致

## 13. 执行顺序建议

1. **先落地 v0.01 全套**：重置 main + v0.01 代码 + 01 文档 + CHANGELOG + tag
2. **v0.02 → v0.06**：复用 backup 分支现有代码，按版本顺序重新提交 + 写文档（工作量在文档）
3. **v0.07 → v0.14**：需新写代码（system prompt 扩写、list_dir/edit_file/grep、权限升级、run_shell、上下文管理三版、plan 引导），工作量较大
4. **v0.15+**：持续迭代，按需追加新功能
5. **每版完成后**：跑 tests、更新 AGENTS.md 路线图打勾、打 tag

> v0.02-v0.06 的代码已存在于 backup-pre-teaching 分支，落地主要是拆分提交节奏 + 写文档。
> v0.07+ 需要新写代码，工作量较大。

## 14. 风险与权衡

- **重置 main 丢失 4 个 commit**：已用 `backup-pre-teaching` 分支备份，需要时可查
- **v0.01 代码与现有代码差异大**：是有意的，第一课必须最简。现有代码会在 v0.02-v0.06 逐步加回
- **教学文档维护成本**：代码改了文档要跟。用"指向文件 + diff"而非"贴完整代码"可降低同步成本
- **API_KEY 硬编码进教学仓库**：教学仓库若公开，需把 `config.py` 的 API_KEY 改为占位符 `"sk-YOUR_KEY_HERE"`，并在 01 文档说明学习者需自行填入
