# 第 15 课：Todo / Task State（v0.15）

> 版本 v0.15 | [教程总览](README.md) | [上一课：Project Instructions](14-project-instructions.md) | [下一课：Plan-driven Execution](16-plan-driven-execution.md)

## 本课目标

v0.14 的项目规则受保护，但任务进度仍藏在自然语言中，裁剪或压缩后会丢失。本课把任务意图放入 `AgentState.todos`，由状态绑定的 `update_todo` 工具完整替换，并在每次请求重建 Structured State。读完后应能解释校验、原子替换、实例隔离及压缩后的恢复。

## 前置条件与版本切换

Python 3.10+，运行时仅标准库；先阅读[第 14 课](14-project-instructions.md)。

```bash
git checkout v0.15
git diff --stat v0.14..v0.15
git diff v0.14..v0.15 -- src/mini_agent/state.py src/mini_agent/tools/todo.py src/mini_agent/tools/__init__.py src/mini_agent/context.py src/mini_agent/__main__.py tests/test_state.py tests/test_tools.py tests/test_context.py
```

## 新增与改动文件

| 文件 | 变化 | 作用 |
|---|---|---|
| `state.py` | `TodoItem`、`update_todos()`、快照 | 校验并保存任务意图 |
| `tools/todo.py` | `make_update_todo_tool(state)` | 将工具绑定到运行实例 |
| `tools/__init__.py` | `create_registry(state)` | 注册实例专属工具 |
| `context.py` | Structured State 增加 Todos | 每轮从最新快照渲染 |
| `__main__.py` | 创建 state/registry/executor | 避免运行间泄漏 |
| `tests/test_state.py`、`test_tools.py`、`test_context.py` | 边界与隔离测试 | 验收不变量 |

## 为什么需要本版

Todo 是模型的计划意图，不是工具执行事实；后者仍由 Executor 回调记录到 `tool_history`、`files_changed` 和 `errors`。列表最多 50 项，每项内容去空白后必须为 1–240 字符，状态只能是 `pending`、`in_progress`、`completed`，且最多一个进行中项。全部校验通过后才替换，因此失败提交不会改变旧快照。

## 关键流程

```text
main -> AgentState -> create_registry(state) -> update_todo
LLM 参数(完整数组) -> update_todos 校验/原子替换
下一次 prepare_messages -> snapshot -> Structured State Todos
```

`prompt.py` 只在复杂任务启发式下要求先维护 Todo；`permission.py` 明确放行 `update_todo`。CLI 把运行实例的 registry schema 传给 `call_llm`，所以模型能看到该工具；全局 registry 仍兼容旧 smoke test，但不含实例 Todo。

## 实现拆解

### 状态与工具

工具成功返回 `Todo 已更新：共 N 项；当前目标：...`；类型或值错误返回 `Todo 更新失败: ...`。`record_tool()` 忽略 `update_todo`，保持意图与执行事实分离。

### 上下文与边界

`ContextManager._render_state()` 每轮读取 `snapshot()`，因此 trimming、compaction 后仍显示最新列表；上下文重建不会把 Todo 写入 history。两个 `AgentState` 各自传给 `create_registry()` 即可隔离。首版不自动规划、阻断或持久化。

## 设计选择与边界

完整替换消除增量歧义，代价是请求需携带完整数组；独立状态保证压缩安全，代价是每轮少量重复 token。Todo 不改变权限，文件和 shell 副作用仍由闸门决定。

## 最小示例与预期输出

```bash
PYTHONPATH=src python - <<'PY'
from mini_agent.state import AgentState
from mini_agent.tools.todo import make_update_todo_tool
s = AgentState(task="修复回归")
t = make_update_todo_tool(s)
print(t.handler(todos=[{"content":"定位失败测试"}, {"content":"修改实现", "status":"in_progress"}]))
print(s.snapshot()["current_goal"])
print(t.handler(todos=[{"content":""}]))
print(s.snapshot()["current_goal"])
PY
```

预期依次看到更新成功、`修改实现`、更新失败，以及仍为 `修改实现`（失败提交回滚）。

## 测试与验收

```bash
PYTHONPATH=src python -m pytest -q tests/test_state.py tests/test_tools.py tests/test_context.py
PYTHONPATH=src python tests/test_state.py
PYTHONPATH=src python tests/test_tools.py
PYTHONPATH=src python tests/test_context.py
```

验收非法类型、空内容、超限和重复进行中均保持旧快照；registry 实例隔离；Todo 在裁剪、压缩和再次更新后均由最新 Structured State 重建。

## 本版特性、下一课与代码索引

本版独有特性是显式、受校验、可恢复的任务状态，而非计划生成器。下一课加入 Plan → Execute → Observe → Replan → Verify 闭环。

- [state.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/state.py)
- [todo.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/tools/todo.py)
- [tools/__init__.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/tools/__init__.py)
- [context.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/context.py)
- [__main__.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/__main__.py)
- [test_state.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/tests/test_state.py)
- [test_tools.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/tests/test_tools.py)
- [test_context.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/tests/test_context.py)
