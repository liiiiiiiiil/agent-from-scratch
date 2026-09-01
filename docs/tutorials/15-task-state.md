# 第 15 课：Todo / Task State（v0.15）

> 版本 v0.15 | [教程总览](README.md) | [上一课：Project Instructions](14-project-instructions.md) | [下一课：Plan-driven Execution](16-plan-driven-execution.md)

## 本课目标

第 14 课让模型读到项目规则，但“还要做什么”仍散落在自然语言和可能被裁剪的消息里。对话一长，计划可能不见，模型也可能给出彼此矛盾的下一步。

v0.15 因此引入显式的 Todo / Task State（待办与任务状态）。模型用 `update_todo` 提交完整列表，运行实例先校验，再存入 `AgentState`。每次请求都会从最新快照生成 Structured State（结构化状态），所以计划不依赖旧消息是否还在。

读完本课，你应该能够：

- 解释 Todo 意图与 Executor 记录的执行事实为什么必须分离。
- 说明完整替换、字段校验、“最多一个进行中项”和失败不改旧状态的原子性。
- 看懂 state-bound registry 如何避免多个运行实例互相污染。
- 验证 trimming/compaction 后 Todo 仍从状态重新渲染，以及本版本刻意不提供的自动规划和持久化能力。

本课的核心原则是：**Todo 记录可校验的任务意图，不是执行证据，也不是自动规划器。**

## 前置条件与版本切换

需要 Python 3.10+，运行时仍只使用标准库。请先阅读[第 14 课](14-project-instructions.md)，了解受保护项目指令和 ContextManager。使用已发布 tag 学习时：

```bash
git checkout v0.15
git diff --stat v0.14..v0.15
git diff v0.14..v0.15 -- src/mini_agent/state.py src/mini_agent/tools/todo.py src/mini_agent/tools/__init__.py src/mini_agent/context.py src/mini_agent/__main__.py tests/test_state.py tests/test_tools.py tests/test_context.py
```

## 新增与改动文件

| 文件 | 相对 v0.14 的变化 | 作用 |
|---|---|---|
| `src/mini_agent/state.py` | `TodoItem`、`update_todos()`、快照中的 todos/current goal | 校验并保存任务意图 |
| `src/mini_agent/tools/todo.py` | 新增 `make_update_todo_tool(state)` | 将工具绑定到某个 AgentState 实例 |
| `src/mini_agent/tools/__init__.py` | 新增 `create_registry(state)` | 为每次运行注册实例专属 Todo 工具 |
| `src/mini_agent/context.py` | Structured State 渲染 Todos | 每轮从 snapshot 重建上下文 |
| `src/mini_agent/__main__.py` | 创建 state、registry、executor 并连接回调 | 保证 CLI 运行实例隔离 |
| `tests/test_state.py`、`test_tools.py`、`test_context.py` | 校验、隔离和重渲染测试 | 覆盖本版本不变量 |

## 为什么需要本版

只用 assistant 文本记录“还剩哪些步骤”会遇到两个实际问题：trimming/compaction 可能删掉旧计划，模型也可能同时写出多个进行中步骤。把任务意图单独保存后，每轮都能重新生成状态，不必赌摘要保留了计划。

但 Todo 不能当作事实。文件是否真的改变、工具是否失败，仍由 `ToolExecutor` 的 `on_result` 回调写入 `tool_history`、`files_changed` 和 `errors`。因此本课只让任务意图可见且有固定结构；第 16 课才处理验证闭环。

## 数据结构与校验不变量

每项 Todo 对应不可变的 `TodoItem`：

```text
content: str                 # 去除首尾空白后 1–240 字符
status: pending | in_progress | completed
```

`AgentState.todos` 最多保存 50 项，列表中最多只能有一个 `in_progress`。因为当前目标只能有一个，所以 `current_goal` 从这一项自动得出；没有进行中项时它一定是空字符串。`snapshot()` 返回普通字典和深拷贝列表，ContextManager 可以安全读取。

`update_todos(todos)` 先检查整个列表，再一次替换旧值。参数必须是 list，每项必须是 dict；`content` 必须是 1 到 240 字符的非空字符串；状态只能是三种枚举之一，最后还会检查进行中项的数量。任何一项不合格都会抛出 `ValueError`。因为替换发生在全部检查之后，旧的 todos 和 current_goal 不会被半途改坏。

## 关键流程

```text
main()
  -> state = AgentState()
  -> run_registry = create_registry(state)
  -> ToolExecutor(run_registry, on_result=state.record_tool)
  -> call_llm 使用 run_registry.schemas() 看见 update_todo
  -> update_todo(todos=完整数组)
       -> state.update_todos 校验并原子替换
  -> 下一轮 ContextManager.prepare_messages()
       -> state.snapshot() -> [Structured State] Todos
```

`permission.py` 把 `update_todo` 设为 `ALLOW`，所以更新计划不会触发文件写入类询问。CLI 的一次 `run_task` 使用同一个运行实例的 registry。交互式后续任务会调用 `begin_task()`（在后续版本实现）重置运行状态，但 Todo 不会被序列化进 `history`。

## 实现拆解

### State-bound 工具与完整替换

`make_update_todo_tool(state)` 返回一个 `Tool`，其 handler 捕获传入的 `state`：

```python
def update_todo(todos=None):
    try:
        state.update_todos(todos)
    except (TypeError, ValueError) as error:
        return f"Todo 更新失败: {error}"
    snapshot = state.snapshot()
    return f"Todo 已更新：共 {len(snapshot['todos'])} 项；当前目标：..."
```

模型每次都必须提交完整数组，而不是使用 `add`/`remove` 增量操作。因为完整替换没有顺序和重复应用的歧义，所以状态更容易判断；代价是每次请求都要传递当前计划。handler 会把校验异常变成工具结果字符串，旧列表不会被半更新。

`create_registry(state)` 先注册 calculate、文件和 shell 工具，再注册绑定这个 state 的 `update_todo`。不传 state 的全局 `registry` 仍供旧版 smoke test 使用，但不会提供实例 Todo；生产 CLI 一定传入当前运行实例的 registry。

### Structured State 的重建

`ContextManager._render_state()` 每次调用 `state.snapshot()`，把 Todo 渲染成一条 system 消息：

```text
[Structured State]
Task: 修复回归
Current goal: 修改实现
Todos: [pending] 定位失败测试; [in_progress] 修改实现
Files changed: (none)
Errors: (none)
Status: running
Tools executed: 0
```

这条消息只属于当前请求视图，不会追加到 `history`。即使发生 trimming 或 compaction，下一次 `_build_messages()` 仍会从最新 snapshot 生成它。因此模型更新 Todo 后，不必再修改旧消息。摘要可能改写计划的措辞，但 Structured State 中的当前列表仍准确。

### 与工具执行事实的边界

`record_tool()` 遇到 `update_todo` 会直接返回，不把它写入 `tool_history` 或 `errors`。这是有意的职责分工：Todo 表示“模型打算做什么”，`write_file`、`run_shell` 等结果表示“环境实际发生了什么”。第 16 课会在同一个 State 上增加 generation 和验证证据，再把两者连成完成条件。

## 设计选择与边界

- **完整替换而非增量 API**：每次都提交完整列表，状态才不会因顺序或重复产生歧义；代价是请求更长。
- **严格限制规模和状态**：50 项、240 字符和单个进行中项限制上下文大小，也避免同时存在多个当前目标。
- **状态与消息分离**：压缩后仍能恢复，多个实例也能隔离；代价是每轮都会重复占用少量 token 来放 Structured State。
- **不自动规划、不阻断完成、不持久化**：v0.15 不会替模型生成 Todo，不会因未完成项自动阻止最终回复，也不会写入磁盘。验证和 blocked/failed 的收口留给下一课。
- **权限保持独立**：`update_todo` 只更新内存；文件和 shell 的副作用仍走原有 PermissionGate。

## 最小可运行示例

下面演示成功更新、自动得出的当前目标，以及非法提交后旧快照仍保持不变：

```bash
PYTHONPATH=src python - <<'PY'
from mini_agent.state import AgentState
from mini_agent.tools.todo import make_update_todo_tool

state = AgentState(task="修复回归")
tool = make_update_todo_tool(state)
print(tool.handler(todos=[
    {"content": "定位失败测试"},
    {"content": "修改实现", "status": "in_progress"},
]))
print(state.snapshot()["current_goal"])
before = state.snapshot()
print(tool.handler(todos=[{"content": ""}]))
print(state.snapshot() == before)
PY
```

输出会包含更新成功、`修改实现`、`Todo 更新失败: ...` 和 `True`。最后的 `True` 表示失败提交没有覆盖原列表。

## 实例隔离示例

同一进程创建两个 registry 时，每个工具只会捕获自己的 state：

```python
from mini_agent.state import AgentState
from mini_agent.tools import create_registry

first, second = AgentState(), AgentState()
create_registry(first).get("update_todo").handler(todos=[{"content": "first"}])
assert first.snapshot()["todos"][0]["content"] == "first"
assert second.snapshot()["todos"] == []
```

这也是 `tests/test_tools.py` 的核心验收点：不同任务绝不能通过模块级可变 Todo 共享进度。

## 测试与验收

```bash
PYTHONPATH=src python -m pytest -q tests/test_state.py tests/test_tools.py tests/test_context.py
PYTHONPATH=src python tests/test_state.py
PYTHONPATH=src python tests/test_tools.py
PYTHONPATH=src python tests/test_context.py
```

重点检查以下行为：

- 非 list、空内容、超长内容、非法状态、超过 50 项和重复 `in_progress` 都拒绝，且旧快照不变。
- `current_goal` 始终由唯一的 `in_progress` 项派生；Todo 不进入执行历史。
- 两个 state 的 registry 互不影响，工具 schema 包含 `update_todo` 且权限放行。
- 更新 Todo 后，`prepare_messages()` 立即显示新列表；trimming/compaction 后仍从最新 Structured State 重建。

## 本版特性、下一课与代码索引

v0.15 提供显式、可校验、可恢复的任务状态，但它不是计划生成器。下一课会在此基础上加入 Plan → Execute → Observe → Replan → Verify、验证证据和 `done/blocked/failed` 收口。

- [state.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/state.py)
- [todo.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/tools/todo.py)
- [tools/__init__.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/tools/__init__.py)
- [context.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/context.py)
- [permission.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/permission.py)
- [__main__.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/__main__.py)
- [test_state.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/tests/test_state.py)
- [test_tools.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/tests/test_tools.py)
- [test_context.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/tests/test_context.py)
