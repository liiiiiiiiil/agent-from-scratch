# 第 15 课：Todo / Task State（v0.15）

上一课：[v0.14 Project Instructions](14-project-instructions.md) · [教程总览](README.md) · 下一课：v0.16 Plan-driven Execution（规划中）

复杂任务的进度若只存在模型自然语言中，会在上下文裁剪或压缩后丢失。v0.15 将任务意图存入 `AgentState`，并通过受控的 `update_todo` 工具完整替换列表；每次请求重新渲染 Structured State，因此压缩后仍可恢复。

## 开始

- Python 3.10+；仓库运行时仍然只有标准库依赖。

```bash
git checkout v0.14
git diff --stat v0.14..HEAD
PYTHONPATH=src python -m pytest -q
```

## 数据流与约束

`TodoItem(content, status)` 只允许 `pending`、`in_progress`、`completed`，最多 50 项、单项 240 字符，且最多一个进行中项。`update_todos()` 先校验完整列表，再一次性替换，失败时旧列表保持不变；`current_goal` 始终由进行中项派生。工具只更新 Todo，不触碰工具历史、文件和错误执行状态。

CLI 为每个运行实例创建 `create_registry(state)`，LLM 看到的 schema 与该实例绑定。`ContextManager.prepare_messages()` 每轮插入新的 `[Structured State]` 消息，消息不写入原始 history；trimming/compaction 后仍使用同一快照。

```python
from mini_agent.state import AgentState
from mini_agent.tools.todo import make_update_todo_tool

state = AgentState()
tool = make_update_todo_tool(state)
print(tool.handler(todos=[{"content": "运行测试", "status": "in_progress"}]))
assert state.snapshot()["current_goal"] == "运行测试"
```

非法状态、空内容、超限列表或重复 `in_progress` 会返回 `Todo 更新失败: ...`，不会部分写入。v0.15 不自动规划、不强制创建 Todo、不阻断未完成任务，也不做持久化。

## 验收与索引

```bash
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python tests/test_state.py
PYTHONPATH=src python tests/test_tools.py
PYTHONPATH=src python tests/test_context.py
```

实现索引：`src/mini_agent/state.py`、`src/mini_agent/tools/todo.py`、`src/mini_agent/tools/__init__.py`、`src/mini_agent/context.py`、`src/mini_agent/agent.py`、`src/mini_agent/__main__.py`。
