# 第 15 课：Todo / Task State（v0.15）

> 版本 v0.15 | [教程总览](README.md) | [上一课：Project Instructions](14-project-instructions.md) | [下一课：Plan-driven Execution](16-plan-driven-execution.md)

## 本课目标

第 14 课解决了“项目规则如何进入上下文”，但任务进度仍混在自然语言和可被裁剪的消息历史里。v0.15 引入显式的 Todo / Task State：模型通过 `update_todo` 提交完整列表，运行实例把它校验后保存到 `AgentState`，每次 LLM 请求再从最新快照渲染为 Structured State。

读完本课，你应该能够：

- 解释 Todo 意图与 Executor 记录的执行事实为什么必须分离。
- 说明完整替换、字段校验、“最多一个进行中项”和失败不改旧状态的原子性。
- 看懂 state-bound registry 如何避免多个运行实例互相污染。
- 验证 trimming/compaction 后 Todo 仍从状态重新渲染，以及本版本刻意不提供的自动规划和持久化能力。

本课的核心原则是：**Todo 是可校验的任务意图，不是执行证据，也不是自动规划器。**

## 前置条件与版本切换

需要 Python 3.10+，运行时仅标准库；先阅读[第 14 课](14-project-instructions.md)关于受保护项目指令和 ContextManager 的内容。使用已发布 tag 学习时：

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

仅靠 assistant 文本记录“还剩哪些步骤”有两个问题：上下文 trimming/compaction 可能删掉旧计划；模型也可能输出互相矛盾的多个进行中步骤。把意图独立存储后，状态可以在每轮重建，不依赖摘要是否完整。

但 Todo 不能冒充事实。文件是否真的改变、工具是否失败，仍由 `ToolExecutor` 的 `on_result` 回调写入 `tool_history`、`files_changed` 和 `errors`。因此本课只解决“任务意图可见且结构化”，不解决第 16 课才加入的验证闭环。

## 数据结构与校验不变量

每项 Todo 对应不可变的 `TodoItem`：

```text
content: str                 # 去除首尾空白后 1–240 字符
status: pending | in_progress | completed
```

`AgentState.todos` 最多 50 项；列表中最多一个 `in_progress`。`current_goal` 从该项自动派生，没有进行中项时为空字符串。`snapshot()` 返回普通字典和深拷贝列表，供 ContextManager 安全读取。

`update_todos(todos)` 的校验顺序是“先解析全部、再一次性替换”：参数必须是 list；每项必须是 dict；`content` 必须是非空字符串且不超过 240 字符；状态必须在三种枚举中；最后检查进行中项数量。任何一项失败都抛出 `ValueError`，旧的 todos/current_goal 保持不变。

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

`permission.py` 将 `update_todo` 设为 `ALLOW`，更新计划不会触发文件写入类询问。CLI 每次 `run_task` 使用同一个运行实例的 registry；交互式后续任务会调用 `begin_task()`（后续版本实现）重置运行态，但不会把 Todo 序列化进 history。

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

模型每次必须提交完整数组，而不是 `add`/`remove` 增量操作。这样可以避免顺序和重复应用的歧义；代价是每次请求都要传递当前计划。handler 将校验异常转换为工具结果字符串，旧列表不会被半更新。

`create_registry(state)` 先注册 calculate、文件和 shell 工具，再注册绑定该 state 的 `update_todo`。不传 state 的全局 `registry` 仍用于旧版 smoke test，但不会提供实例 Todo；生产 CLI 始终传入运行实例 registry。

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

这条消息属于请求视图，不会追加到 `history`。即使发生 trimming 或 compaction，下一次 `_build_messages()` 仍会从最新 snapshot 生成，因此模型更新 Todo 后无需手动修改旧消息。压缩摘要可以丢失计划措辞，但 Structured State 中的当前列表仍准确。

### 与工具执行事实的边界

`record_tool()` 对 `update_todo` 直接返回，不将其放进 `tool_history` 或 `errors`。这是刻意的职责分离：Todo 表示“模型打算做什么”，而 `write_file`、`run_shell` 等工具结果表示“环境实际发生了什么”。第 16 课会在同一 State 上增加 generation 和验证证据，把两者连接成完成条件。

## 设计选择与边界

- **完整替换而非增量 API**：语义简单且原子，代价是请求必须携带完整列表。
- **严格限制规模和状态**：50 项、240 字符和单个进行中项避免上下文膨胀与并行目标歧义。
- **状态与消息分离**：压缩安全、实例可隔离；代价是每轮 Structured State 会重复占用少量 token。
- **不自动规划、不阻断完成、不持久化**：v0.15 不会替模型生成 Todo，不会因为未完成项自动阻止最终回复，也不会写入磁盘。验证和 blocked/failed 收口属于下一课。
- **权限保持独立**：`update_todo` 仅更新内存；文件和 shell 副作用仍走原有 PermissionGate。

## 最小可运行示例

下面演示成功更新、当前目标派生，以及非法提交保持旧快照：

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

预期输出包含更新成功、`修改实现`、`Todo 更新失败: ...` 和 `True`。最后一个 `True` 说明失败提交没有覆盖原列表。

## 实例隔离示例

同一进程创建两个 registry 时，工具分别捕获自己的 state：

```python
from mini_agent.state import AgentState
from mini_agent.tools import create_registry

first, second = AgentState(), AgentState()
create_registry(first).get("update_todo").handler(todos=[{"content": "first"}])
assert first.snapshot()["todos"][0]["content"] == "first"
assert second.snapshot()["todos"] == []
```

这也是 `tests/test_tools.py` 的核心验收点：不能使用模块级可变 Todo 共享不同任务的进度。

## 测试与验收

```bash
PYTHONPATH=src python -m pytest -q tests/test_state.py tests/test_tools.py tests/test_context.py
PYTHONPATH=src python tests/test_state.py
PYTHONPATH=src python tests/test_tools.py
PYTHONPATH=src python tests/test_context.py
```

重点检查：

- 非 list、空内容、超长内容、非法状态、超过 50 项和重复 `in_progress` 都拒绝，且旧快照不变。
- `current_goal` 始终由唯一的 `in_progress` 项派生；Todo 不进入执行历史。
- 两个 state 的 registry 互不影响，工具 schema 包含 `update_todo` 且权限放行。
- 更新 Todo 后，`prepare_messages()` 立即显示新列表；trimming/compaction 后仍从最新 Structured State 重建。

## 本版特性、下一课与代码索引

v0.15 的独有能力是显式、受校验、可恢复的任务状态，而非计划生成器。下一课将在此基础上加入 Plan → Execute → Observe → Replan → Verify、验证证据和 `done/blocked/failed` 收口。

- [state.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/state.py)
- [todo.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/tools/todo.py)
- [tools/__init__.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/tools/__init__.py)
- [context.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/context.py)
- [permission.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/permission.py)
- [__main__.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/__main__.py)
- [test_state.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/tests/test_state.py)
- [test_tools.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/tests/test_tools.py)
- [test_context.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/tests/test_context.py)
