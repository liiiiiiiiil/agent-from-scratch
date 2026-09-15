# 第 31 课：从完整安全点恢复会话（v0.31）

上一课：[会话持久化与安全点](30-session-persistence.md) · [教程总览](README.md) · 下一课：[v0.32 持久化工具执行边界](32-durable-tool-boundaries.md)

> 代码快照：`v0.31` · 相邻差异：`v0.30..v0.31` · 命令环境：Bash/zsh

> 运行要求：Python 3.10+。本课的 `v0.31` tag 由仓库维护者在交付后手动创建；助手不创建、移动或推送 tag。

## 本课目标

上一课已经把 State、Context 和安全点写进 session 文件，但新进程仍然只能读取并诊断它。完成本课后，你应能解释为什么“文件内容完整”还不足以继续执行，为什么恢复必须重新检查工作区和重新建立运行时对象，以及为什么恢复后的任务必须重新验证。

本课还会带你观察 `python -m mini_agent --resume <session_id>` 的行为：它只接受经过校验的 schema 2、`safe_point`、`clean` 会话；成功后展示原任务状态并等待用户输入，不自动请求 LLM。恢复失败发生在当前 CLI 运行时替换之前，因此不会偷偷执行工具。

## 前置条件

需要基础 Python、命令行和第 30 课的 session 概念。下面的命令使用 Bash/zsh。先切换到相邻快照查看真实差异，再切换回本课快照；tag 只提供学习者手动执行的步骤。

```bash
git checkout v0.30
git diff --stat v0.30..v0.31
git diff v0.30..v0.31 -- src/mini_agent/session.py src/mini_agent/resume.py src/mini_agent/state.py src/mini_agent/context.py src/mini_agent/__main__.py
git checkout v0.31
```

## 新增与改动文件

先看 `git diff --stat`，可以确认这一课跨越的是保存格式、恢复准入、状态重建和 CLI 交接四条边界。下面只保留帮助理解主线的文件。

| 文件 | 变化 | 作用 |
|---|---|---|
| `src/mini_agent/session.py` | 新增 schema 2、工作区清单和独占占用 | 读取诊断、构造清单、复核原提交并原子写出后继 `active` generation |
| `src/mini_agent/resume.py` | 新增恢复准入与运行时组装 | 检查工作区，创建 State、Context、工具注册表、进程管理器和权限闸门 |
| `src/mini_agent/state.py` | 新增严格恢复和 resume generation | 重建权威事实，保留预算与计划决定，清空当前验证资格 |
| `src/mini_agent/context.py` | 新增历史恢复 | 恢复摘要、裁剪位置和完整 tool-call/result 顺序，重新接入 protected prompt |
| `src/mini_agent/checkpoint.py`、`processes.py` | 导入旧资源的审计信息 | 旧 checkpoint、PID 和 `process_id` 不再成为可操作句柄 |
| `src/mini_agent/__main__.py` | 新增 `--resume` 入口 | 在提交 active 占用后交给 CLI，等待用户决定或下一条输入 |
| `src/mini_agent/trace.py` | 识别 resume generation | 只从已加载 State 快照回放旧事实和恢复起点 |

## 版本变更定位

先看上一版的调用链。v0.30 可以把一个完整安全点写到磁盘，但退出后没有对象可以把 JSON 还原成可执行的运行时。

```text
[旧] v0.30：
CLI -> State / Context -> SessionStore.save() -> clean JSON
新 CLI -> SessionStore.load() -> 只能校验和诊断
```

本版把恢复插在“读取”和“交给 CLI”之间。`[C]` 表示主要消费者，`[B]` 表示本版边界。

```text
[旧] --resume 输入
  -> [~] SessionStore.load()
  -> [+] 工作区清单检查
  -> [+] restore_session(State, Context)
  -> [+] 新 Registry / ProcessManager / PermissionGate
  -> [+] 独占锁内复核 schema、generation、完整性
  -> [+] 原子写出后继 active 提交
  -> [C] CLI 展示任务状态并等待用户输入

[B] schema 1 / active / 工作区变化 / 无法完整检查
  -> 拒绝，不调用 LLM，不执行 handler，不替换当前运行时

[B] 旧 PID、旧 process_id、缺少前镜像字节的 ready checkpoint
  -> 仅保留审计记录，不提供控制或回滚能力
```

## 上一版的问题

一个 session 文件只说明某个时刻的 State 和 Context 已经一起保存。它不能证明当前工作区仍然是当时的样子，也不能把旧 Python 进程中的文件句柄、权限批准、线程或 `Popen` 对象带到新进程。若直接把旧验证结果当成今天的事实，任务可能在文件已变化时错误完成；若直接使用旧 PID，新的进程甚至可能控制到不属于当前任务的资源。

因此本版把恢复拆成两个问题：先证明“保存的任务事实仍与当前工作区相符”，再构造一套没有旧操作系统句柄的新运行时。最后在独占锁内重新读取原提交，只有提交成功才把候选运行时交给 CLI。

## 核心概念、流程与数据结构

### 1. schema 2 保存工作区清单

工作区清单是保存时从结构化任务事实建立的有限基线。文件工具的 `path`、`files_changed`、失败记录和 checkpoint 会进入清单；递归 `grep` 的搜索目录还会纳入未匹配的子目录和文件，避免恢复时漏掉后来变化的内容。shell 命令文本不会被解析来猜测它可能读写了什么。目录记录直接条目和其中普通文件的哈希；搜索范围超出数量上限或任务路径包含符号链接时，清单不可用于恢复。

清单不等于整个工作区的快照。它只覆盖 State 能明确声明或观察到的任务路径；如果路径在保存时无法完整建立基线，session 仍可用于诊断，但 `recoverable` 会是 `false`，恢复会明确拒绝。

下面的字段是恢复准入真正关心的内容：

```json
{
  "schema_version": 2,
  "save_kind": "safe_point",
  "handoff_status": "clean",
  "session_generation": 3,
  "workspace_manifest": {
    "root": "/work/project",
    "complete": true,
    "recoverable": true,
    "entries": [
      {"path": "src/app.py", "kind": "file", "available": true, "sha256": "..."}
    ]
  }
}
```

这里的 `session_generation` 是保存提交版本，不是执行 generation。前者用于在独占锁中防止恢复旧提交，后者记录任务执行和验证边界。两者都必须由读取到的原提交核对，不能由 CLI 根据文件名推测。

### 2. 恢复先构造候选对象，再占用 session

`prepare_resume()` 只做读取、验证和内存组装。它调用 `AgentState.restore_session()` 重建 dataclass 记录和私有计数，调用 `ContextManager.restore_session()` 恢复历史与摘要，再创建新的工具注册表、进程管理器和权限闸门。这个阶段没有 LLM 请求，也没有 handler 调用。

候选对象准备好以后，`ResumeCandidate.claim()` 在 session 独占锁内重新读取文件，核对原来的完整性值、`clean` 状态、`safe_point` 和 generation，并再次检查工作区清单，然后原子写出后继 `active` envelope。两次检查之间若有文件变化，恢复会拒绝；复核失败或锁清理状态不确定时不返回可运行对象。

### 3. resume generation 让旧验证失效

恢复会追加一个 `open_reason="resume"` 的执行 generation，清空当前 `verification_evidence`，保留 append-only 的 `verification_history`，并设置新的验证义务。这样 Trace 仍能回放旧事实，但任务完成判定不能把旧 generation 的成功命令当作本次恢复后的验证。

计划 revision、步骤进度、用户审批决定、修复阶段、任务级预算和失败因果引用会原样重建。恢复不会因为新进程启动而给任务增加预算，也不会自动批准 `awaiting_approval` 计划或解除 `blocked` / `failed` 状态。

### 4. 旧资源只有审计意义

State 中的进程记录是可序列化事实，不是进程句柄。新建的 `ProcessManager` 不接管旧 PID，且分配新 `process_id` 时跳过历史 ID。旧 checkpoint 只保存元数据；由于前镜像字节不在 session 中，导入时旧 `ready` 会变成 `unavailable`。恢复后的工具只能看到新 manager 持有的句柄。

## 为什么这样设计

本版选择工作区清单而不是把整个目录复制到 session。这样可以检查任务相关的文件内容和目录条目，同时保持 session 有界；代价是没有声明或观察到的 shell 外部效果不会被清单覆盖。按照计划，本版不从 shell 文本推断副作用，也不把“没有发现变化”解释成“外部系统没有变化”。

恢复提交采用“先构造、后占用”的顺序。若先把文件改成 `active`，随后发现 State 或 protected prompt 无法重建，用户会得到一个不能运行的占用状态；若先把候选对象交给 CLI，又可能在占用失败时执行下一轮。当前顺序让任何失败都停在当前运行时之外。

本版选择重新建立 PermissionGate，并重新发现当前工作区的 `AGENTS.md`。旧进程的 `once` / `always` 授权只存在于旧内存对象，不应跨进程继承；当前指令也可能已经改变，必须进入新 system prompt。代价是用户需要重新授权后续副作用工具。

## 设计边界

可恢复的是 schema 2、`safe_point`、`clean` 且工作区清单仍匹配的完整安全点。`exploring` 会继续只读调查，`awaiting_approval` 会继续等待 `/approve`、`/reject` 或 `/continue`，`blocked` 和 `failed` 继续受原 CLI 规则限制。恢复成功后 CLI 只等待用户输入；它不会因为 session 中有任务就自动调用 LLM。

不可恢复的是 schema 1、`active`、损坏或哈希不符的文件、工作区根路径变化、文件或目录内容变化、无法完整检查的清单、活动旧进程和在途 stdin。v0.31 也不恢复中断中的工具调用，不重连旧进程，不保存或恢复 checkpoint 前镜像字节。恢复后的异常退出会留下 `active`，因此同一会话不能再次直接恢复。

## 关键流程

```text
--resume <id>
  -> load + schema / size / field / reference / SHA-256 validation
  -> compare normalized workspace root and every manifest entry
  -> restore State / Context and rediscover protected instructions
  -> create fresh Registry / ProcessManager / PermissionGate
  -> invalidate current verification and append resume generation
  -> lock session and re-read expected clean safe point
  -> atomic write successor active generation
  -> CLI displays task / plan state and waits for user input

任何前置步骤失败
  -> no LLM, no handler, no current-runtime replacement

正常退出
  -> bounded cleanup -> save clean
异常退出或清理不完整
  -> keep/report active session; a later direct resume is rejected
```

为了观察“恢复不会自动请求 LLM”，可以先保存一个 clean session，再从新 Python 进程运行：

```bash
PYTHONPATH=src python -m mini_agent --resume <session_id>
```

预期先看到“会话已恢复”和原任务状态，然后出现输入提示；在你输入下一条任务或 `/approve` 等 CLI 决定之前，不会产生新的模型请求。若先修改清单中的文件，预期看到“工作区检查失败”，并且 session 不会被标记为可运行的 `active`。

## 实现拆解

`SessionStore` 仍负责大小、私有权限、锁、哈希和原子替换；schema 2 额外保存 `workspace_manifest`，`claim_resume()` 在锁内复核待恢复提交并写后继 generation。旧 schema 不会被转换成可恢复对象，只能继续被 `load()` 用于诊断。

[`resume.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.31/src/mini_agent/resume.py) 把准入和组装集中到 `prepare_resume()`。这使 CLI 不需要把“检查路径”“重建 dataclass”“创建工具注册表”和“提交 active 占用”拼在一个交互分支里，也让没有 LLM 配置的测试可以证明失败路径不会触发模型或工具。

[`state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.31/src/mini_agent/state.py) 的 `restore_session()` 先严格校验导出，再把 JSON 记录转换回 `PlanRevision`、`ExecutionAttempt`、`FailureEvent`、`RecoveryAction`、`VerificationEvidence` 和进程审计记录。`begin_resume()` 负责追加 resume generation 和清除当前验证资格；`snapshot()` 继续重新计算 `current_goal`、Todo 和 active plan 投影。

[`context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.31/src/mini_agent/context.py) 恢复历史、摘要和裁剪位置。每个 assistant `tool_calls` 仍必须紧跟按顺序的 `role=tool` 结果；脱敏的 `write_process.input` 只作为历史显示，不能变回可执行正文。当前项目的 protected system prompt 由 [`instructions.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.31/src/mini_agent/instructions.py) 重新发现后注入。

CLI 入口见 [`__main__.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.31/src/mini_agent/__main__.py)。它先完成候选构造和 `claim()`，再替换本地 State、Context、Registry、ProcessManager 和 ToolExecutor 引用；失败时仍保留原运行时引用。Trace 入口见 [`trace.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.31/src/mini_agent/trace.py)，它只消费已加载 State 的快照，不自行读取 session 或执行任何操作。

## 运行与观察

下面的命令仍使用 Bash/zsh。先在一个任务中输入 `/save`，正常输入 `exit` 完成 clean 交接，记录显示的 session ID；再启动新的 Python 进程运行 `--resume`。观察三件事：任务文本和计划状态仍在，启动时没有 LLM 请求，下一轮工具需要新的权限判断和新的验证 generation。这三点分别说明 State/Context 已恢复、恢复入口不是自动 agent loop，以及旧运行时授权和验证没有被带入。

如果任务处于 `awaiting_approval`，恢复后会重新显示当前 revision 和 `/approve`、`/reject`、`/continue`；如果任务是 `blocked` 或 `failed`，CLI 保留原限制。若会话含旧后台进程、在途 stdin 或旧 `awaiting_process` 状态，v0.31 会在工作区检查后拒绝恢复，旧记录仍可用于 Trace 和诊断。

## 本版特性、下一课与代码索引

本版新增的是从完整 clean 安全点安全地重建运行时：schema 2 工作区基线、独占占用、新 State/Context、重新发现的指令与权限、resume generation，以及旧资源的审计化处理。下一课 v0.32 将讨论如何把工具调用的准备执行和结果提交变成耐久边界；本课不提前恢复中断中的 handler。

核心代码索引：[`session.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.31/src/mini_agent/session.py)、[`resume.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.31/src/mini_agent/resume.py)、[`state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.31/src/mini_agent/state.py)、[`context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.31/src/mini_agent/context.py)、[`__main__.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.31/src/mini_agent/__main__.py)。
