# 第 16 课：计划驱动执行（Plan-driven Execution，v0.16）

上一课：[任务清单与状态](15-task-state.md) · [教程总览](README.md) · 下一课：规划中

> 代码快照：`v0.16` · 相邻差异：`v0.15..v0.16` · 命令环境：Bash/zsh

## 本课目标

v0.16 不加入自动规划器或持久化数据库，而是把 Todo、真实工具结果和验证结果连成一个保守的完成闭环：

```text
Plan -> Execute -> Observe -> Replan -> Verify -> done
```

读完本课，你应该能够：

- 解释 `generation` 为什么会让旧验证证据失效。
- 区分 `run_shell` 的 `execution` 与 `verification`，以及它们对状态的不同影响。
- 看懂“最终文本回复”出现后，agent loop 如何提醒一次、阻止过早完成并最终收口为 `blocked`。
- 能沿着正常路径解释失败验证、任务重置和最大迭代收口。

本课的核心原则是：**计划表达意图，状态记录事实，独立验证才算完成证据。**

## 上一版的问题

第 15 课的 Todo 能记录计划，但“标记完成”不代表文件真的改过，也不代表改完后检查仍会通过。模型可能把“运行测试”设为 `completed`，却从未执行测试。v0.16 要把 Todo、真实工具结果和验证结果连成一个保守的完成闭环，同时不把验证命令或计划交给运行时自动生成。

## 前置条件与版本切换

需要 Python 3.10+，运行时只有标准库。建议先阅读第 15 课，了解 Todo 的完整替换和 Structured State。在对应 tag 查看差异：

```bash
git checkout v0.15
git diff --stat v0.15..v0.16
git diff v0.15..v0.16 -- src/mini_agent/state.py src/mini_agent/agent.py src/mini_agent/context.py src/mini_agent/tools/shell.py
git checkout v0.16
```

切换回工作区版本后再运行本课示例。

## 新增与改动文件

先用 `git diff --stat v0.15..v0.16` 确认本版范围；下表只列与本课主线直接相关的文件。

| 文件 | 相对 v0.15 的变化 | 作用 |
|---|---|---|
| `src/mini_agent/state.py` | 增加 `VerificationEvidence`、generation、完成条件、`begin_task()` | 保存验证事实并使状态转换有依据 |
| `src/mini_agent/tools/shell.py` | `run_shell(command, purpose=...)` | 区分可能改变环境的执行命令和验证命令 |
| `src/mini_agent/context.py` | Structured State 增加验证字段；增加 Runtime Notice | 将缺口只注入下一次 LLM 请求 |
| `src/mini_agent/agent.py` | 最终回复检查 reminder；支持 `blocked`/`failed` 收口 | 防止模型未经验证就结束，达到上限时明确失败 |
| `src/mini_agent/prompt.py` | core rules 增加 Plan → Verify 规则 | 让模型知道何时建立 Todo、何时验证 |
| `src/mini_agent/__main__.py` | 每项任务调用 `begin_task()`；结束时设置 `done`/`failed` | CLI 任务边界和状态生命周期 |
| `tests/test_state.py`、`test_loop.py`、`test_context.py`、`test_tools.py` | 覆盖 generation、提醒、协议和 shell schema | 本版本的可执行验收 |

## 版本变更定位

图例：`[旧]` v0.15 已有，`[+]` v0.16 新增，`[~]` v0.16 修改，`[C]` 主要消费者，`[B]` 本版边界。

v0.15 的相关基线是“工具执行后记录摘要，最终文本直接交给 CLI 收口”；v0.16 在同一条链上加入任务重置、验证证据生命周期和最终完成闸门。

v0.15 基线图：

```text
[旧] CLI run_task
  -> [旧] AgentState.task/status
  -> [旧] history: user message
  -> [旧] agent_loop
       -> [旧] call_llm
       -> [旧] ToolExecutor.execute
            -> [旧] PermissionGate
            -> [旧] tool.handler
            -> [旧] state.record_tool
       -> [旧] history: role=tool
       -> [旧] 下一轮 LLM
  -> [旧] 最终文本
  -> [旧] CLI 直接收口为 done
```

v0.16 变更图：

```text
[+] CLI run_task
  -> [+] state.begin_task(task)
       清空上一任务的 Todo、文件、错误和证据
       初始化当前任务 generation
  -> [旧] agent_loop
       -> [旧] call_llm
       -> [+] update_todo
            -> [+] state-bound handler
            -> [+] AgentState.update_todos()
            -> [C] ContextManager._render_state()
       -> [旧] ToolExecutor.execute
            -> [旧] PermissionGate
            -> [旧] tool.handler
            -> [~] state.record_tool()
                 ├─ [~] write_file/edit_file 或 run_shell(execution)
                 │    -> generation 递增
                 │    -> 旧 verification evidence 失效
                 └─ [+] run_shell(verification)
                      -> VerificationEvidence
                      -> 当前 generation 的通过/失败证据
       -> [旧] role=tool 回灌
       -> [C] ContextManager.prepare_messages()
            ├─ [~] Structured State 增加验证字段
            └─ [+] Runtime Notice（只进入下一次请求视图）
       -> [+] 最终文本 completion_reminder()
            ├─ 无缺口 -> [C] CLI 收口为 done
            ├─ 首次有缺口 -> [+] set_runtime_notice()
            │                 -> 下一轮 LLM
            └─ 再次仍有缺口 -> [+] status=blocked

[B] 本版不负责：自动生成 Todo、自动选择验证命令、自动重试、回滚或持久化计划。
```

变更映射：

| 图中节点/边 | 类型 | 对应代码 | 主要消费者/作用 |
|---|---|---|---|
| `run_task -> begin_task()` | `[+]` | `src/mini_agent/__main__.py` | `AgentState`；明确任务边界并重置运行事实 |
| `update_todo -> update_todos()` | `[+]` | `src/mini_agent/state.py`、`src/mini_agent/tools/todo.py` | Structured State；保存模型计划意图 |
| `run_shell(purpose=...)` | `[+]` | `src/mini_agent/tools/shell.py` | `state.record_tool()`；区分执行和验证 |
| `record_tool()` 的 generation/evidence 分支 | `[~]` | `src/mini_agent/state.py` | `completion_reminder()`；维护证据生命周期 |
| Structured State 与 Runtime Notice | `[~]/[+]` | `src/mini_agent/context.py` | 下一次 LLM 请求；保留最新验证事实并传递一次提醒 |
| 最终文本 `completion_reminder()` | `[+]` | `src/mini_agent/agent.py` | `done`、`blocked` 收口；防止未经验证直接完成 |

入口是 `run_task()` 和模型发出的 `update_todo`/`run_shell` 调用；主要消费者是 `AgentState`、`ContextManager` 和 agent loop 的收口逻辑。本课不自动规划、重试、回滚或持久化计划；`ExecutionResult`、`effect_class` 等后续失败模型能力不属于 v0.16。

## 为什么这样设计

Todo 的状态变化不等于环境变化。即使测试曾经通过，后面一次写文件也可能破坏结果。因此运行时必须区分“命令已经执行”和“当前代码已经验证”。v0.16 用下面两条规则实现这个区分：

1. 成功的 `write_file`/`edit_file`，以及真正执行的 `run_shell(purpose="execution")`，都视为可能改变环境的操作。
2. 只有当前 generation 中、`run_shell(purpose="verification")` 返回明确 `[exit=0]` 且未超时的证据，才算验证通过。

执行命令即使非零退出或超时，也会让旧证据失效，因为环境已经可能变化。权限拒绝没有进入 handler，所以不会无故使证据失效。验证失败或超时会保留失败证据，并继续要求重试。

## 核心概念与数据结构

### 计划、执行与验证

Todo 表达模型的任务意图；工具回调记录执行事实；只有绑定当前 generation（代次）的验证证据，才可能满足完成条件。三者互相独立，避免把“计划已完成”误当成“环境已验证”。

### 状态不变量与证据生命周期

`AgentState` 在 messages 之外维护运行事实：

```text
task / current_goal / todos
files_changed / errors / tool_history
status: running | done | blocked | failed
verification_evidence[]
_verification_generation
_last_verified_generation
_verification_required
```

`VerificationEvidence` 保存 `command`、`outcome`（`passed`/`failed`）、解析出的 `exit_code` 和截断后的 `output`。`has_verification_evidence()` 只有在证据非空、最后一次验证通过且 generation 相等时才返回 True。它不会判断测试是否覆盖了正确业务场景。

`record_tool()` 由 `ToolExecutor(on_result=state.record_tool)` 回调触发，所以 agent loop 不必理解每个工具怎样改状态。`update_todo` 有意不进入 `tool_history`、错误或文件列表，因为它只是计划意图。`begin_task(task)` 会清空上一任务的 todos、文件、错误和证据，递增 generation，但会保留会话 history 供后续对话使用。

## 关键流程

一次典型任务的消息和状态变化如下：

```text
CLI run_task
  -> state.begin_task(task)
  -> LLM: update_todo（Plan）
  -> LLM: read/edit/write/run_shell execution（Execute）
       -> Executor on_result -> record_tool（Observe）
  -> LLM 根据结果更新 Todo（Replan）
  -> LLM: run_shell verification
       -> [exit=0] 才设置 last_verified_generation
  -> LLM 最终文本
       -> 无缺口：返回；有缺口：Runtime Notice 后再请求一次
```

`ContextManager.prepare_messages()` 每轮都会重新生成 Structured State。因此压缩、裁剪或长对话都不会把最新验证状态留在旧摘要里。Runtime Notice（运行时提示）不写入 `history`，只出现在下一次请求视图中；即使这次请求触发压缩，它也会保留到最终构建完成后再消费。

## 实现拆解

### `run_shell` 的 purpose 与输出协议

`purpose` 可以是 `execution` 或 `verification`，省略时一定是 `execution`，所以兼容 v0.10 及更早调用。工具仍通过 `subprocess.run(shell=True)` 在当前工作目录执行，超时为 30 秒，stdout/stderr 合并后最多保留 2000 字符：

- 正常结束始终带 `[exit=N]`，无输出时为 `[exit=N] (无输出)`。
- 超时返回 `[timeout] ...`，没有 exit code。

`purpose` 只影响状态怎样记录，不改变命令的执行权限；权限仍由 PermissionGate 决定。

### generation 绑定验证

成功写入或 execution shell 都会调用 `_invalidate_verification()`。它会递增 generation、清空旧证据，并设置 `verification_required=True`。verification shell 会解析结果：只有 `[exit=0]`、`ok` 且未超时时才记为 passed，并把 `_last_verified_generation` 绑定到当前 generation；其他情况都记为 failed。

所以“先验证、再执行、最后直接回复”一定仍会触发提醒，因为最后一次执行已经让证据过期。最终测试或检查应作为最后一个 verification 调用。

### 完成提醒与 loop 收口

`completion_reminder()` 遇到未完成 Todo 或待验证的可能变更时会返回消息。简单的只读任务没有这些缺口，所以可以直接结束。agent loop 收到不含 `tool_calls` 的 assistant 消息后会按以下规则处理：

1. 第一次发现 reminder：设置 Runtime Notice，继续下一轮；提醒最多纠正一次，不会自动替模型规划或重试。
2. 第二次仍有缺口：将 `state.status` 设为 `blocked`，返回当前文本，避免无限循环。
3. 有工具调用则继续正常执行；达到 `MAX_ITERATIONS`（默认 50）后返回“达到最大迭代次数”，并把状态设为 `failed`。

CLI 的 `run_task()` 正常返回且状态仍为 `running` 时会设为 `done`；顶层异常或达到最大迭代结果时设为 `failed`。loop 不会兜底 LLM 或 CLI 顶层异常；工具边界的异常仍由 Executor/loop 转成可回灌的工具结果。

## 设计边界

- **不自动规划**：模型负责调用 `update_todo`。运行时只检查状态，不替模型生成计划。
- **保守地把 execution 当作可能变更**：运行时无法可靠判断任意 shell 命令是否改了环境，所以即使命令看起来只读或非零退出，也会让证据失效。
- **证据不是业务正确性证明**：`[exit=0]` 只代表进程成功退出；测试覆盖不足、命令选错仍需要模型判断。
- **不持久化、不撤回流式输出**：任务状态只在当前进程中保存；已经打印的草稿不会被运行时收回。
- **提醒只一次**：这是防止过早结束的闸门，不是重试策略；缺口持续存在时，状态最终明确为 `blocked`。

## 运行与观察

配置好本地 LLM 后，用包含修改和检查的真实任务启动 CLI（命令环境：Bash/zsh）：

```bash
PYTHONPATH=src python -m mini_agent "修改实现并运行检查，维护 Todo"
```

写入或 execution shell 后，Agent 会把旧验证标为过期；只有针对当前 generation 的 verification 通过，最终回复才不会收到提醒。提醒最多纠正一次，仍有缺口时进入 `blocked`；命令行首条任务处理后仍继续交互。

## 本版特性、下一课与代码索引

v0.16 计划驱动执行（Plan-driven Execution）的独有能力是“generation 绑定的验证证据、一次性完成提醒，以及明确的 done/blocked/failed 状态”。下一版本仍在规划中；运行时不会自动保存计划，也不会替用户决定验证命令。

- [`src/mini_agent/state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.16/src/mini_agent/state.py)
- [`src/mini_agent/agent.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.16/src/mini_agent/agent.py)
- [`src/mini_agent/context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.16/src/mini_agent/context.py)
- [`src/mini_agent/tools/shell.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.16/src/mini_agent/tools/shell.py)
- [`src/mini_agent/prompt.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.16/src/mini_agent/prompt.py)
- [`src/mini_agent/permission.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.16/src/mini_agent/permission.py)
- [`src/mini_agent/__main__.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.16/src/mini_agent/__main__.py)
- [`tests/test_stage5_e2e.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.16/tests/test_stage5_e2e.py)
- [`tests/test_state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.16/tests/test_state.py)
- [`tests/test_loop.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.16/tests/test_loop.py)
- [`tests/test_context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.16/tests/test_context.py)
- [`tests/test_tools.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.16/tests/test_tools.py)
