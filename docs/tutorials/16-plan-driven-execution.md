# 第 16 课：计划驱动执行（Plan-driven Execution，v0.16）

> 稳定版本 v0.16 | [教程总览](README.md) | [上一课：任务清单与状态（Todo / Task State）](15-task-state.md) | 下一课：规划中

> 代码快照：`v0.16` · 相邻差异：`v0.15..v0.16` · 命令环境：Bash/zsh

## 本课目标

第 15 课的 Todo 能记录计划，但“标记完成”不代表文件真的改过，也不代表改完后检查仍会通过。比如模型可能把“运行测试”设为 completed，却从未执行测试。

v0.16 不加入自动规划器或持久化数据库，而是把 Todo、真实工具结果和验证结果连成一个保守的完成闭环：

```text
Plan -> Execute -> Observe -> Replan -> Verify -> done
```

读完本课，你应该能够：

- 解释 `generation` 为什么会让旧验证证据失效。
- 区分 `run_shell` 的 `execution` 与 `verification`，以及它们对状态的不同影响。
- 看懂“最终文本回复”出现后，agent loop 如何提醒一次、阻止过早完成并最终收口为 `blocked`。
- 使用零网络示例和现有测试验证正常路径、失败验证、任务重置和最大迭代失败路径。

本课的核心原则是：**计划表达意图，状态记录事实，独立验证才算完成证据。**

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

| 文件 | 相对 v0.15 的变化 | 作用 |
|---|---|---|
| `src/mini_agent/state.py` | 增加 `VerificationEvidence`、generation、完成条件、`begin_task()` | 保存验证事实并使状态转换有依据 |
| `src/mini_agent/tools/shell.py` | `run_shell(command, purpose=...)` | 区分可能改变环境的执行命令和验证命令 |
| `src/mini_agent/context.py` | Structured State 增加验证字段；增加 Runtime Notice | 将缺口只注入下一次 LLM 请求 |
| `src/mini_agent/agent.py` | 最终回复检查 reminder；支持 `blocked`/`failed` 收口 | 防止模型未经验证就结束，达到上限时明确失败 |
| `src/mini_agent/prompt.py` | core rules 增加 Plan → Verify 规则 | 让模型知道何时建立 Todo、何时验证 |
| `src/mini_agent/__main__.py` | 每项任务调用 `begin_task()`；结束时设置 `done`/`failed` | CLI 任务边界和状态生命周期 |
| `tests/test_state.py`、`test_loop.py`、`test_context.py`、`test_tools.py` | 覆盖 generation、提醒、协议和 shell schema | 本版本的可执行验收 |

## 为什么需要本版

Todo 的状态变化不等于环境变化。即使测试曾经通过，后面一次写文件也可能破坏结果。因此运行时必须区分“命令已经执行”和“当前代码已经验证”。v0.16 用下面两条规则实现这个区分：

1. 成功的 `write_file`/`edit_file`，以及真正执行的 `run_shell(purpose="execution")`，都视为可能改变环境的操作。
2. 只有当前 generation 中、`run_shell(purpose="verification")` 返回明确 `[exit=0]` 且未超时的证据，才算验证通过。

执行命令即使非零退出或超时，也会让旧证据失效，因为环境已经可能变化。权限拒绝没有进入 handler，所以不会无故使证据失效。验证失败或超时会保留失败证据，并继续要求重试。

## 数据结构与状态不变量

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

## 设计选择与边界

- **不自动规划**：模型负责调用 `update_todo`。运行时只检查状态，不替模型生成计划。
- **保守地把 execution 当作可能变更**：运行时无法可靠判断任意 shell 命令是否改了环境，所以即使命令看起来只读或非零退出，也会让证据失效。
- **证据不是业务正确性证明**：`[exit=0]` 只代表进程成功退出；测试覆盖不足、命令选错仍需要模型判断。
- **不持久化、不撤回流式输出**：任务状态只在当前进程中保存；已经打印的草稿不会被运行时收回。
- **提醒只一次**：这是防止过早结束的闸门，不是重试策略；缺口持续存在时，状态最终明确为 `blocked`。

## 最小无网络示例

下面直接操作状态层，观察写入为何要求验证、验证如何恢复完成条件，以及新写入如何让旧证据失效：

```bash
PYTHONPATH=src python - <<'PY'
from mini_agent.state import AgentState

s = AgentState(task="修改并检查 a.py")
s.update_todos([{"content": "运行检查", "status": "in_progress"}])
s.record_tool("write_file", {"path": "a.py"}, True, "written")
print(s.completion_reminder())                 # verification_required=True
s.record_tool("run_shell", {"command": "python -m py_compile a.py",
                             "purpose": "verification"}, True, "[exit=0] ok")
print(s.has_verification_evidence())            # True
s.record_tool("edit_file", {"path": "a.py"}, True, "edited")
print(s.has_verification_evidence())            # False（generation 已改变）
PY
```

典型过程是：模型先建立 Todo，写入后看到 `verification_required`，再运行 verification 命令。若命令返回 `[exit=1]` 或 `[timeout]`，状态仍是待验证；模型应根据输出调整 Todo 并重试。

## 测试与验收

### 阶段级 E2E 验收

`tests/test_stage5_e2e.py` 在临时 Git 项目中直接组装 runtime，再用脚本化模型响应完成“加载 AGENTS.md → 建立 Todo → 调查 → 错误修改 → 验证失败 → 重排 Todo → 修正 → 验证通过”。测试预置带工具结果的历史来触发 compaction，并记录每次 `prepare_messages()` 快照。它检查项目级指令和 Structured State 在压缩后仍存在，失败验证发生在第二次修改前，最终文件与 `[exit=0]` 证据一致，并且每个 tool call 都有对应的 `role=tool` 结果。

```bash
PYTHONPATH=src python -m pytest -q tests/test_stage5_e2e.py
```

完整测试和本课核心测试都可直接运行：

```bash
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python tests/test_state.py
PYTHONPATH=src python tests/test_loop.py
PYTHONPATH=src python tests/test_tools.py
PYTHONPATH=src python tests/test_context.py
```

重点验收以下行为：

- execution 不产生通过证据；写入、execution 失败和超时都会使旧证据失效，权限拒绝不会。
- verification 的零退出码、非零退出码和超时分别得到正确 evidence。
- 新任务 `begin_task()` 清空运行态但不要求清空会话 history。
- 最终回复的提醒只注入一次；仍有缺口时状态为 `blocked`；达到迭代上限时为 `failed`。
- tool call 协议、并发执行和结果顺序保持第 11 课以来的不变量。

## 本版特性、下一课与代码索引

v0.16 计划驱动执行（Plan-driven Execution）的独有能力是“generation 绑定的验证证据、一次性完成提醒，以及明确的 done/blocked/failed 状态”。下一版本仍在规划中；运行时不会自动保存计划，也不会替用户决定验证命令。

- [`src/mini_agent/state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.16/src/mini_agent/state.py)
- [`src/mini_agent/agent.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.16/src/mini_agent/agent.py)
- [`src/mini_agent/context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.16/src/mini_agent/context.py)
- [`src/mini_agent/tools/shell.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.16/src/mini_agent/tools/shell.py)
- [`tests/test_stage5_e2e.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.16/tests/test_stage5_e2e.py)
