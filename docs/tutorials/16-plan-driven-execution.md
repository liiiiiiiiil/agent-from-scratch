# 第 16 课：Plan-driven Execution（v0.16）

> 稳定版本 v0.16 | [教程总览](README.md) | [上一课：Todo / Task State](15-task-state.md) | 下一课：规划中

## 本课目标

第 15 课加入了 Todo，但 Todo 只是模型维护的任务意图：它不能证明文件已经修改，也不能证明修改后的代码仍然通过检查。v0.16 在不引入自动规划器或持久化数据库的前提下，把 Todo、工具执行事实和验证结果连成一个保守的完成闭环：

```text
Plan -> Execute -> Observe -> Replan -> Verify -> done
```

读完本课，你应该能够：

- 解释 `generation` 为什么会让旧验证证据失效。
- 区分 `run_shell` 的 `execution` 与 `verification`，以及它们对状态的不同影响。
- 看懂“最终文本回复”出现后，agent loop 如何提醒一次、阻止过早完成并最终收口为 `blocked`。
- 使用零网络示例和现有测试验证正常路径、失败验证、任务重置和最大迭代失败路径。

本课的核心原则是：**计划表达意图，状态记录事实，独立验证才提供完成证据。**

## 前置条件与版本切换

需要 Python 3.10+；运行时只有标准库。建议先阅读第 15 课，理解 Todo 的原子更新和 Structured State。在对应 tag 查看差异：

```bash
git checkout v0.15
git diff --stat v0.15..v0.16
git diff v0.15..v0.16 -- src/mini_agent/state.py src/mini_agent/agent.py src/mini_agent/context.py src/mini_agent/tools/shell.py
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

Todo 的状态变化并不等于环境变化。例如模型可以把“运行测试”标记为 completed，却从未调用测试命令；即使之前测试通过，随后一次写文件也可能破坏结果。因此 v0.16 引入两个不变量：

1. 成功的 `write_file`/`edit_file`，以及真正执行的 `run_shell(purpose="execution")`，都视为可能改变环境的操作。
2. 只有当前 generation 中、`run_shell(purpose="verification")` 返回明确 `[exit=0]` 且未超时的证据，才算验证通过。

执行命令非零退出或超时仍会使旧证据失效（因为命令已经运行）；权限拒绝则没有进入 handler，不会无谓地失效。验证失败和超时会保留失败证据并继续要求重试。

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

`VerificationEvidence` 保存 `command`、`outcome`（`passed`/`failed`）、解析出的 `exit_code` 和截断后的 `output`。`has_verification_evidence()` 只有在“证据非空、最后通过、generation 相等”时才返回 True；它不会判断业务语义是否正确。

`record_tool()` 由 `ToolExecutor(on_result=state.record_tool)` 回调驱动，因此 agent loop 不需要理解每个工具如何更新状态。`update_todo` 特意不进入 `tool_history` 或错误/文件列表：它是计划意图，不是执行事实。`begin_task(task)` 清空上一任务的 todos、文件、错误和证据，递增 generation，并保留会话 history 供后续对话使用。

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

`ContextManager.prepare_messages()` 每轮都会重新渲染 Structured State，所以压缩、裁剪或多轮对话不会把最新的验证状态“藏”在旧摘要里。Runtime Notice 不写入 `history`，只在下一次请求视图中出现；即使这次请求触发上下文压缩，也会保留到最终构建完成后再消费。

## 实现拆解

### `run_shell` 的 purpose 与输出协议

`purpose` 可取 `execution` 或 `verification`，缺省为 `execution`，因此兼容 v0.10 及更早调用。工具仍使用 `subprocess.run(shell=True)`，当前工作目录执行，超时 30 秒，stdout/stderr 合并并截断到 2000 字符：

- 正常结束始终带 `[exit=N]`，无输出时为 `[exit=N] (无输出)`。
- 超时返回 `[timeout] ...`，没有 exit code。

`purpose` 只影响状态记录，不改变命令本身的执行权限；权限仍由 PermissionGate 决定。

### generation 绑定验证

成功写入或 execution shell 调用 `_invalidate_verification()`：递增 generation、清空证据并设置 `verification_required=True`。verification shell 则解析结果：`[exit=0]` 且 `ok`、非超时才记录 passed，并把 `_last_verified_generation` 绑定到当前 generation；其余情况记录 failed。

因此“先验证、再执行、最后直接回复”仍会触发提醒，因为最后一次执行已经让证据过期。建议把最终测试或检查作为最后一个 verification 调用。

### 完成提醒与 loop 收口

`completion_reminder()` 在存在未完成 Todo，或存在待验证的可能变更时返回消息；简单只读任务没有这些缺口，可以直接结束。agent loop 收到没有 `tool_calls` 的 assistant 消息后：

1. 第一次发现 reminder：设置 Runtime Notice，继续下一轮；提醒最多纠正一次，不会自动替模型规划或重试。
2. 第二次仍有缺口：将 `state.status` 设为 `blocked`，返回当前文本，避免无限循环。
3. 有工具调用则继续正常执行；达到 `MAX_ITERATIONS`（默认 50）后返回“达到最大迭代次数”，并把状态设为 `failed`。

CLI 的 `run_task()` 在 loop 正常返回且状态仍为 `running` 时设为 `done`；顶层异常或最大迭代结果设为 `failed`。loop 不兜底 LLM/CLI 顶层异常，工具边界异常仍由 Executor/loop 转成可回灌的工具结果。

## 设计选择与边界

- **不自动规划**：模型负责调用 `update_todo`；运行时只验证状态，不替模型生成计划。
- **保守地把 execution 当作可能变更**：无法可靠判断任意 shell 命令是否修改环境，因此即使命令看起来是只读或退出非零，也会使证据失效。
- **证据不是业务正确性证明**：`[exit=0]` 只代表进程成功退出；测试覆盖不足、命令选错等问题仍需模型判断。
- **不持久化、不撤回流式输出**：任务状态只在当前进程内维护；已经打印的草稿不会被运行时收回。
- **提醒只一次**：这是防止过早结束的安全闸门，不是重试策略；持续缺口最终明确为 `blocked`。

## 最小无网络示例

下面直接驱动状态层，观察写入如何要求验证、验证如何恢复完成条件，以及新写入如何使旧证据失效：

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

典型场景是：模型先建立 Todo，执行写入后看到 `verification_required`，再运行 verification 命令；若命令返回 `[exit=1]` 或 `[timeout]`，状态保持待验证，模型应据输出调整 Todo 并重试。

## 测试与验收

### 阶段级 E2E 验收

`tests/test_stage5_e2e.py` 在临时 Git 项目中直接组装 runtime，并用脚本化模型响应驱动完整的“加载 AGENTS.md → 建立 Todo → 调查 → 错误修改 → 验证失败 → 重排 Todo → 修正 → 验证通过”流程。测试预置带工具结果的历史以触发 compaction，记录每次 `prepare_messages()` 快照，断言项目指令和 Structured State 在压缩后仍存在、失败验证发生在第二次修改前、最终文件与 `[exit=0]` 证据一致，并检查每个 tool call 都有对应的 `role=tool` 结果。

```bash
PYTHONPATH=src python -m pytest -q tests/test_stage5_e2e.py
```

完整测试和本课核心测试均可直接运行：

```bash
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python tests/test_state.py
PYTHONPATH=src python tests/test_loop.py
PYTHONPATH=src python tests/test_tools.py
PYTHONPATH=src python tests/test_context.py
```

重点验收点：

- execution 不产生通过证据；写入、execution 失败和超时都会使旧证据失效，权限拒绝不会。
- verification 的零退出码、非零退出码和超时分别得到正确 evidence。
- 新任务 `begin_task()` 清空运行态但不要求清空会话 history。
- 最终回复的提醒只注入一次；仍有缺口时状态为 `blocked`；达到迭代上限时为 `failed`。
- tool call 协议、并发执行和结果顺序保持第 11 课以来的不变量。

## 本版特性、下一课与代码索引

v0.16 的独有能力是“generation 绑定的验证证据 + 一次性完成提醒 + 明确的 done/blocked/failed 状态”。下一版本尚在规划中；运行时不会自动保存计划，也不会替用户决定验证命令。

- [state.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/state.py)
- [agent.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/agent.py)
- [context.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/context.py)
- [prompt.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/prompt.py)
- [shell.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/tools/shell.py)
- [base.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/tools/base.py)
- [__main__.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/__main__.py)
- [test_state.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/tests/test_state.py)
- [test_loop.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/tests/test_loop.py)
- [test_context.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/tests/test_context.py)
- [test_tools.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/tests/test_tools.py)
