# 第 31 课：从完整安全点恢复会话（v0.31）

上一课：[把 Agent 会话保存到磁盘](30-session-persistence.md) · [教程总览](README.md) · 下一课：[持久化工具执行边界](32-durable-tool-boundaries.md)

> 代码快照：`v0.31` · 相邻差异：`v0.30..v0.31` · 命令环境：Bash/zsh

> 运行要求：Python 3.10+。本课只恢复完整的 `clean` 安全点，不恢复中断中的工具调用。

## 本课目标

上一课留下了一个可信的 session 文件，但“文件完整”不等于“现在可以安全继续”。保存以后，工作区可能变了；旧 Python 进程里的权限、文件句柄和后台进程也不可能凭 JSON 自动回来。

读完本课，你应该能解释：

- 为什么恢复前要检查工作区清单；
- 为什么要先构造一个候选运行时，再占用 session；
- 为什么恢复后要重新发现 `AGENTS.md`、重新建立权限闸门，并让旧验证失效；
- 为什么旧 PID 和 checkpoint 元数据只能用于审计，不能继续控制资源。

你还会看到 `--resume <session_id>` 的真实行为：成功时它显示已恢复的任务并等待输入，不会自动向 LLM 发请求。

## 上一版的问题

v0.30 可以保存 State 和 Context，但读取 session 只做格式校验和诊断。若新进程直接把 JSON 当成“现在的事实”，它可能在文件已经变化的工作区上继续；若它直接使用保存的 PID，则可能控制到另一个任务的进程。

本版把恢复定义成一次新的交接：先确认保存时记录的工作区仍然匹配，再从磁盘事实重新创建 State、Context、工具注册表、进程管理器和 PermissionGate。只有这些对象准备好，并且原 session 在独占锁内再次核对通过，CLI 才会接手。

## 前置条件与版本切换

需要基础 Python、命令行和第 30 课的 session 概念。下面命令使用 Bash/zsh。tag 是固定的学习快照；切换后请按自己的 Git 工作流回到开发分支。

```bash
git checkout v0.30
git diff --stat v0.30..v0.31
git diff v0.30..v0.31 -- src/mini_agent/session.py src/mini_agent/resume.py src/mini_agent/state.py src/mini_agent/context.py src/mini_agent/__main__.py
git checkout v0.31
```

## 新增与改动文件

本版跨越四个环节：检查能否恢复、把数据变回运行时对象、提交新的活动代次、把结果交给 CLI。这里的“运行时”指当前进程里真正持有工具和资源的对象。

| 文件 | 变化 | 作用 |
|---|---|---|
| `src/mini_agent/session.py` | 增加 schema 2、工作区清单和恢复占用 | 复核原提交并写出后继 `active` session |
| `src/mini_agent/resume.py` | 增加恢复准入与运行时组装 | 先读取检查，再创建候选运行时 |
| `src/mini_agent/state.py` | 增加严格恢复和 resume generation | 恢复权威事实，并清空旧验证资格 |
| `src/mini_agent/context.py` | 增加历史恢复 | 恢复消息顺序、摘要并重新接入受保护消息 |
| `src/mini_agent/instructions.py` | 重新发现项目指令 | 用当前工作区的 `AGENTS.md` 构造 system prompt |
| `src/mini_agent/__main__.py` | 增加 `--resume` | 只在占用提交成功后替换 CLI 的运行时 |
| `src/mini_agent/checkpoint.py`、`processes.py` | 导入旧资源的审计信息 | 明确旧 PID 和旧 checkpoint 不可操作 |

## 版本变更定位

图例：`[旧]` v0.30 已有，`[+]` v0.31 新增，`[~]` v0.31 修改，`[C]` 主要消费者，`[B]` 本课边界。

v0.30 的入口很短：

```text
[旧] --resume 输入
  -> [旧] SessionStore.load()
  -> [旧] 校验 JSON / 哈希
  -> [旧] 只能显示诊断
  [B] 没有 State、Context、工具和权限对象可供继续运行
```

v0.31 在读取和交给 CLI 之间插入恢复准入：

```text
[旧] --resume <session_id>
  -> [~] load + schema / 字段 / 引用 / SHA-256 校验
  -> [+] 比较工作区清单
  -> [+] restore State / Context
  -> [+] 重新发现 AGENTS.md，创建 Registry / ProcessManager / PermissionGate
  -> [+] 开启 resume generation，清空当前 verification
  -> [+] 独占锁内再次读取原 clean 提交
  -> [+] 原子写出后继 active session
  -> [C] CLI 展示任务状态并等待输入

[B] schema 1、active、工作区变化、无法完整检查、活动旧进程
  -> 拒绝；不调用 LLM，不执行 handler，不替换当前运行时
[B] 旧 PID / process_id / 缺少前镜像字节的 checkpoint
  -> 只保留审计信息，不提供控制或回滚
```

## 核心概念与数据结构

### 1. 工作区清单是“恢复前的对照表”

工作区指项目所在的目录。工作区清单（workspace manifest）不是整个目录的备份，而是保存时从任务事实中整理出的有限基线：任务涉及的文件会记录类型和 SHA-256，涉及的目录会记录目录条目；无法完整检查的路径会明确标记为不可恢复。

恢复准入真正关心的字段可以简化成这样：

```json
{
  "schema_version": 2,
  "save_kind": "safe_point",
  "handoff_status": "clean",
  "workspace_manifest": {
    "complete": true,
    "recoverable": true,
    "entries": [
      {"path": "src/app.py", "kind": "file", "sha256": "..."}
    ]
  }
}
```

恢复时会比较工作区根路径、文件类型、文件内容和目录成员。清单不覆盖 shell 可能触及的所有外部系统，所以“清单没变化”只说明已记录范围没有变化，不是整个世界都没有变化。

### 2. 恢复分为“准备候选”和“正式占用”

`prepare_resume()` 做读取、校验和内存组装，不调用 LLM，也不执行工具。它先把 JSON 还原成 State 和 Context，再创建新的工具注册表、进程管理器和权限闸门。

候选对象准备好后，`claim()` 在 session 独占锁内重新读文件，复核原来的哈希、`clean`、`safe_point` 和提交代次，并再次检查工作区。成功后才原子写出后继 `active` session，最后把候选运行时交给 CLI。准备和占用之间如果文件或工作区发生变化，恢复会拒绝。

代码中的入口关系如下；关键点是“任何一步失败都没有执行能力”：

```python
candidate = prepare_resume(store, session_id, workspace_root)
runtime = candidate.claim()
```

### 3. 恢复要开启新的执行代次

generation 可以理解为任务执行边界的编号。恢复会追加一个 `open_reason="resume"` 的新 generation，清空当前 `verification_evidence`，但保留只追加的 `verification_history`。

这样做是为了区分两件事：旧记录仍可供 Trace 审计，而“当前工作区在恢复后通过了什么验证”必须重新产生。计划 revision、进度、用户的计划决定、预算和失败因果仍会恢复；恢复不会自动批准计划、增加预算或解除 `blocked` / `failed`。

对应的实现先还原 State，再显式开启恢复代次：

```python
state = AgentState.restore_session(raw_state, root)
state.begin_resume()
```

### 4. 旧资源是记录，不是句柄

`Popen` 对象、管道、线程和 PermissionGate 的运行时授权都只存在旧进程里。session 中的进程记录只能说明“过去登记过一个 PID”，不能让新进程重新取得控制权。新 `ProcessManager` 会跳过历史 `process_id`，不接管旧 PID。

checkpoint 也一样：v0.31 只导入元数据，没有保存前镜像字节，因此旧的 `ready` checkpoint 会标记为 `unavailable`，不能假装可以 rollback。

创建新进程管理器时只把历史 ID 用作“避让清单”，而不是传入旧句柄：

```python
historical_process_ids = [item.process_id for item in state.processes]
process_manager = ProcessManager(historical_process_ids=historical_process_ids)
```

## 为什么这样设计

本版用工作区清单而不是复制整个目录。这样 session 有界，且能检查任务明确涉及的文件；代价是未声明的 shell 外部效果不在清单覆盖范围内。本版也不从 shell 文本猜测命令会改什么，更不把“没观察到变化”当成“肯定没有变化”。

本版采用“先构造、后占用”。如果先把 session 改成 `active`，随后 State 或当前项目指令无法重建，用户会得到一个已经被占用却不能运行的会话；如果先把候选交给 CLI，又可能在占用失败时开始下一轮。当前顺序让失败停在当前运行时之外。

重新建立 PermissionGate 和项目指令也有代价：旧进程的 `once` / `always` 授权不会继承，后续副作用工具可能需要重新询问。收益是恢复不会悄悄沿用旧环境的授权或旧规则。

## 设计边界

可以恢复的条件是：schema 2、`save_kind=safe_point`、`handoff_status=clean`、完整性校验通过，且工作区清单仍匹配。恢复成功后只显示任务并等待用户输入；它不会因为文件里存在任务就自动请求 LLM。

不能恢复的情况包括：schema 1、`active` session、哈希/引用损坏、工作区根路径变化、文件或目录变化、清单不完整、旧后台进程仍活动、stdin 写入在途和中断中的工具调用。异常退出会留下 `active`，同一会话不能再次直接走 safe resume。

## 关键流程

```text
--resume <id>
  -> 限大小、解析、校验 schema / 字段 / 引用 / SHA-256
  -> 比较工作区根路径和清单条目
  -> 恢复 State / Context，重新发现项目指令
  -> 创建新的 Registry / ProcessManager / PermissionGate
  -> 清空当前验证，追加 resume generation
  -> 锁内复核原 clean safe point
  -> 原子写出后继 active
  -> CLI 显示任务状态，等待用户输入

任一步失败
  -> 不调用 LLM，不执行 handler，不替换当前运行时

正常退出
  -> 有界清理 -> 写 clean
异常退出或清理失败
  -> 保留 / 报告 active；后续直接 resume 会被拒绝
```

## 实现拆解

`SessionStore.claim_resume()` 负责锁内复核和后继文件提交；`resume.py` 的 `prepare_resume()` 负责恢复准入和对象组装。两者分开后，CLI 不需要把路径检查、JSON 还原和运行时替换揉在一个交互分支里。

[`src/mini_agent/state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.31/src/mini_agent/state.py) 把 JSON 还原为计划、attempt、failure、recovery、verification 和进程审计记录，并由 `begin_resume()` 开启新 generation。`snapshot()` 仍会重新计算 Todo 和 active plan 投影，不直接信任文件里可能重复的派生字段。

[`src/mini_agent/context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.31/src/mini_agent/context.py) 恢复消息历史和摘要，要求每个 assistant `tool_calls` 后紧跟按顺序的 `role=tool` 结果；脱敏输入仍只是显示占位符，不能变回 stdin 正文。

[`src/mini_agent/instructions.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.31/src/mini_agent/instructions.py) 重新发现当前项目指令；[`src/mini_agent/__main__.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.31/src/mini_agent/__main__.py) 只有在 `claim()` 成功后才替换本地 State、Context、Registry、ProcessManager 和 ToolExecutor。Trace 仍只读取已经加载的 State 快照，入口见 [`src/mini_agent/trace.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.31/src/mini_agent/trace.py)。

## 运行与观察

下面命令使用 Bash/zsh。命令行首条任务执行后，程序仍会进入交互循环。

先用 v0.31 启动一个任务，输入 `/save`，再正常输入 `exit`，记下终端显示的 session ID。然后关闭旧进程，在新的 Python 进程中运行：

```bash
PYTHONPATH=src python -m mini_agent --resume <session_id>
```

预期现象是：先看到“会话已恢复”和原任务/计划状态，再看到输入提示；在你输入下一条任务或 `/approve` 等 CLI 决定前，不会产生新的模型请求。若在恢复前修改清单中的文件，预期是“工作区检查失败”，且不会得到可运行的后继 session。

这些现象分别证明了：Context/State 已读回、恢复入口不是自动 agent loop、恢复前确实检查了工作区。

## 本版特性、下一课与代码索引

本版新增的是从完整 `clean` 安全点重建运行时：schema 2 工作区基线、锁内占用、当前指令和权限重建、新 resume generation，以及旧资源的审计化处理。下一课 v0.32 会缩小“工具开始后、结果落盘前”的危险窗口；本课不恢复中断中的 handler。

核心代码索引：[`session.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.31/src/mini_agent/session.py)、[`resume.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.31/src/mini_agent/resume.py)、[`state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.31/src/mini_agent/state.py)、[`context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.31/src/mini_agent/context.py)、[`__main__.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.31/src/mini_agent/__main__.py)。
