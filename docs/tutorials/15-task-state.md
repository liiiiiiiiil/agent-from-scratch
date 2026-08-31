# 第 15 课：Todo / Task State（v0.15）

上一课：[第 14 课：Project Instructions](14-project-instructions.md) · [教程总览](README.md) · 下一课：v0.16 Plan-driven Execution（规划中）

## 本课目标

v0.14 已能保护项目规则，但任务进度仍藏在自然语言和 history 中，裁剪/压缩后可能丢失。v0.15 将任务意图存入 `AgentState`，由状态绑定的 `update_todo` 工具更新，并在每次 LLM 请求中重新渲染 Structured State。

读完后应能解释版本差异、Todo 校验与原子替换、CLI 到上下文的完整数据流，并验证隔离、失败回滚和压缩后保留。

## 前置条件与版本切换

Python 3.10+，运行时仅标准库；已读[第 14 课](14-project-instructions.md)。

```bash
git checkout v0.14
git diff --stat v0.14..v0.15
git diff v0.14..v0.15 -- src/mini_agent/state.py src/mini_agent/tools/todo.py src/mini_agent/tools/__init__.py src/mini_agent/context.py src/mini_agent/__main__.py tests/test_state.py tests/test_tools.py
```

## 新增与改动文件

| 文件 | 变化 | 作用 |
|---|---|---|
| `state.py` | `TodoItem`、`todos`、`update_todos()` | 校验任务意图并派生 `current_goal` |
| `tools/todo.py` | `make_update_todo_tool(state)` | 状态绑定的工具闭包 |
| `tools/__init__.py` | `create_registry(state)` | 每个运行实例注册 Todo |
| `context.py` | Structured State 增加 Todos | 每轮注入最新快照 |
| `__main__.py` | 创建独立 state/registry/executor | 防止跨运行泄漏 |
| `tests/test_state.py`、`test_tools.py` | 新增测试 | 原子性、隔离和边界 |

## 为什么需要本版

把 Todo 追加进 `messages` 会被预算裁剪；增量修改还可能留下半份列表。本版不变量是：`AgentState.todos` 是唯一真相；完整列表先全部校验、再一次替换；Todo 不计入 `tool_history`、`errors` 或 `files_changed`。

`TodoItem` 的 `content` 必须是非空字符串（去首尾空白，最长 240 字符），`status` 只能为 `pending`、`in_progress`、`completed`；列表最多 50 项，最多一个 `in_progress`。`current_goal` 自动取进行中项，否则为空。`snapshot()` 返回独立字典，修改快照不会改状态。

## 关键流程

```text
main -> AgentState() -> create_registry(state)
     -> make_update_todo_tool(state)
LLM -> update_todo(完整列表) -> state.update_todos()
下一轮 -> ContextManager.prepare_messages()
        -> snapshot() -> [Structured State] Todos: ...
```

工具成功返回 `Todo 已更新：共 N 项；当前目标：...`，失败返回 `Todo 更新失败: ...`。Executor 回调仍执行，但 `record_tool()` 忽略 `update_todo`，因此任务意图和执行事实分离。

## 实现拆解

### 状态层：校验后原子替换

`update_todos()` 先检查类型、数量、内容长度、枚举和进行中数量，全部通过后才在锁内写入并派生目标。非法提交不会部分生效：

```python
from mini_agent.state import AgentState
state = AgentState()
state.update_todos([
    {"content": "检查代码"},
    {"content": "运行测试", "status": "in_progress"},
])
before = state.snapshot()
try:
    state.update_todos([
        {"content": "新步骤", "status": "in_progress"},
        {"content": "重复进行中", "status": "in_progress"},
    ])
except ValueError:
    pass
assert state.snapshot() == before
assert state.snapshot()["current_goal"] == "运行测试"
```

工具捕获 `TypeError`/`ValueError` 返回错误文本，模型可修正参数；直接调用状态层则保留异常，便于测试。

### 工具与 registry：按实例绑定

`make_update_todo_tool(state)` 返回闭包；CLI 每次调用 `create_registry(state)`。因此两个实例互不影响：

```python
from mini_agent.state import AgentState
from mini_agent.tools import create_registry
from mini_agent.tools.base import ToolExecutor
from mini_agent.permission import PermissionGate, PermissionPolicy, ALLOW
a, b = AgentState(), AgentState()
e = ToolExecutor(create_registry(a),
    gate=PermissionGate(PermissionPolicy({"update_todo": ALLOW})))
e.execute("update_todo", {"todos": [{"content": "first"}]})
assert a.snapshot()["todos"][0]["content"] == "first"
assert b.snapshot()["todos"] == []
```

全局 `registry` 仍供旧 smoke test 使用；只有传入 state 的 registry 才含 `update_todo`。

### 上下文层：每轮重建

`ContextManager._render_state()` 从 `snapshot()` 生成含 Task、Current goal、Todos、文件、错误和状态的 system 消息。`prepare_messages()` 在压缩前后都插入它且不修改 `history`，所以 Todo 不随旧轮次摘要丢失。代价是每轮少量输入 token。

## 最小示例与典型场景

```bash
PYTHONPATH=src python - <<'PY'
from mini_agent.state import AgentState
from mini_agent.tools.todo import make_update_todo_tool
s = AgentState(task="修复回归")
t = make_update_todo_tool(s)
print(t.handler(todos=[{"content":"定位失败测试"},
                       {"content":"修改实现","status":"in_progress"}]))
print(s.snapshot())
print(t.handler(todos=[{"content":""}]))
print(s.snapshot()["current_goal"])  # 仍为“修改实现”
PY
```

典型日志：先出现 `Todo 已更新...`，下一次请求的 `[Structured State]` 出现同一列表；压缩后仍存在，且 `Tools executed` 不计入更新调用。

## 设计选择与边界

- 完整替换避免增量操作歧义，代价是每次提交完整数组。
- 状态与 history 分离，裁剪/摘要不影响 Todo，代价是重复渲染。
- 不自动规划、不自动阻断未完成任务、不持久化。
- 不改变权限；文件和 shell 副作用仍由 `PermissionGate` 决定。

## 测试与验收

```bash
PYTHONPATH=src python -m pytest -q tests/test_state.py tests/test_tools.py tests/test_context.py
PYTHONPATH=src python tests/test_state.py
PYTHONPATH=src python tests/test_tools.py
PYTHONPATH=src python tests/test_context.py
```

验收：默认 state 独立且快照深拷贝；非法状态/空内容/超 50 项/超 240 字符/重复进行中均拒绝且旧值不变；更新不进入执行历史；registry 实例隔离；Structured State 含 Todo 且 trimming/compaction 后保留。

## 本版特性、下一课与代码索引

本版独有特性是显式、受校验、可恢复的任务状态，不是计划生成器。下一课 v0.16 将加入 Plan-driven Execution 与验证闭环。

- [state.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/state.py)
- [todo.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/tools/todo.py)
- [tools/__init__.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/tools/__init__.py)
- [context.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/context.py)
- [__main__.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/__main__.py)
- [test_state.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/tests/test_state.py)
- [test_tools.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/tests/test_tools.py)
- [test_context.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/tests/test_context.py)
