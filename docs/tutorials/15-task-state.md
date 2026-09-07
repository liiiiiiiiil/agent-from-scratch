# 第 15 课：把任务计划保存成可靠状态（Todo / Task State，v0.15）

上一课：[项目级指令](14-project-instructions.md) · [教程总览](README.md) · 下一课：[计划驱动执行](16-plan-driven-execution.md)

> 代码快照：`v0.15` · 相邻差异：`v0.14..v0.15` · 命令环境：Bash/zsh

> 运行要求：Python 3.10+。运行时只使用标准库。

## 本课目标

v0.14 能把项目规则放进 system prompt（系统提示），但没有独立位置保存“准备做什么”和“现在做到哪一步”。v0.15 增加 Todo（任务清单）和 `AgentState`（运行状态），让模型提交的计划在每轮请求中都能被重新读取。

读完本课，你应该能够：

- 区分 Todo 计划和工具执行事实；
- 解释为什么 Todo 每次都提交完整列表；
- 看懂非法列表为何不会部分写入；
- 说明状态绑定的工具注册表如何避免不同运行实例互相覆盖。

本课主线是：**Todo 记录可校验的任务意图，不是执行证据，也不是自动规划器。**

## 上一版的问题

如果把计划只写在 assistant 消息里，历史裁剪或压缩后计划可能消失；如果把工具结果也写进计划，模型的意图和已经发生的事实就会混在一起。v0.15 要解决的是“计划存在哪里、怎样稳定地呈现给模型”，不是替模型生成计划，也不根据 Todo 判定任务完成。

## 前置条件与版本切换

先阅读第 14 课，了解 `ContextManager`（上下文管理器）和受保护 system prompt。在 Bash/zsh 中查看相邻版本：

```bash
git checkout v0.14
git diff --stat v0.14..v0.15
git diff v0.14..v0.15 -- src/mini_agent/state.py src/mini_agent/tools/todo.py src/mini_agent/tools/__init__.py src/mini_agent/context.py src/mini_agent/agent.py src/mini_agent/__main__.py
git checkout v0.15
```

## 新增与改动文件

只列本课主线直接涉及的文件：

| 文件 | 本版变化 | 解决的问题 |
|---|---|---|
| `state.py` | 增加 `TodoItem`、`todos`、`update_todos()` 和快照字段 | 保存并校验计划意图 |
| `tools/todo.py` | 增加绑定状态的 `update_todo` 工具 | 让模型提交完整 Todo 列表 |
| `tools/__init__.py` | 增加 `create_registry(state)` | 每个运行实例拥有自己的工具 |
| `context.py` | 每轮把 Todo 渲染为 Structured State | 裁剪或压缩后仍能看到最新计划 |
| `agent.py`、`__main__.py` | 传递当前 registry 并复用同一份状态 | 保持 CLI、执行器和 LLM 使用同一实例 |

## 版本变更定位

图例：`[旧]` v0.14 已有，`[+]` v0.15 新增，`[~]` v0.15 修改，`[C]` 主要消费者，`[B]` 本版边界。

v0.14 基线图：

```text
[旧] CLI main()
  -> [旧] AgentState + history + ContextManager
  -> [旧] 全局 registry -> ToolExecutor
  -> [旧] agent_loop -> call_llm
       -> 工具 handler -> state.record_tool()
       -> role=tool 结果回灌 history -> 下一轮 LLM
```

v0.15 变更图：

```text
[旧] CLI main()
  -> [~] AgentState + history + ContextManager
  -> [+] create_registry(state) -> [~] ToolExecutor
  -> [旧] agent_loop -> [~] call_llm(..., tool_registry=run_registry)
       -> [+] update_todo(完整列表)
            -> [C] AgentState.update_todos()
                 ├─ 合法：原子替换 todos，更新 current_goal
                 └─ 非法：返回工具错误，旧状态不变
       -> [~] prepare_messages()
            -> snapshot() -> [C] Structured State（当前请求视图）

[B] 不自动生成计划、不持久化 Todo、不用 Todo 自动判定完成
```

这里的关键变化不是增加了一个列表，而是把列表放到独立状态中，并让同一个状态实例同时被工具、执行器和上下文管理器使用。

## 核心概念一：Todo 是计划，不是事实

`update_todo` 只提交模型的计划。文件修改、shell 执行和错误仍由普通工具回调记录到 `tool_history`、`files_changed` 和 `errors`。

`record_tool()` 会特意跳过 `update_todo`：

```python
if name == "update_todo":
    return
```

这样，“准备修改文件”不会被误记成“文件已经修改”。下一课会在此基础上增加独立验证证据。

## 核心概念二：完整列表 + 原子替换

每次更新都提交完整数组，而不是调用 `add_todo` 或 `remove_todo`。运行时先检查全部项目，检查通过后才一次性替换：

```python
if len(todos) > 50:
    raise ValueError("Todo 数量不能超过 50")

parsed = []
for item in todos:
    content = item.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Todo content 必须是非空字符串")
    status = item.get("status", "pending")
    if status not in ("pending", "in_progress", "completed"):
        raise ValueError("Todo status 非法")
    parsed.append(TodoItem(content.strip(), status))

with self._lock:
    self.todos = parsed
```

合法状态只有 `pending`（待处理）、`in_progress`（进行中）和 `completed`（已完成）；最多一个 Todo 可以处于 `in_progress`。任何一项非法时，异常在替换前抛出，所以旧列表保持不变。

## 核心概念三：状态绑定的工具与快照

`make_update_todo_tool(state)` 捕获一份明确的 `AgentState`。CLI 通过 `create_registry(state)` 创建当前运行实例的 registry（工具注册表），再把它交给 `ToolExecutor` 和 `call_llm()`。

```python
state = AgentState()
run_registry = create_registry(state)
context = ContextManager(state, history)
tool_executor = ToolExecutor(run_registry, on_result=state.record_tool)
```

因此，两个 Agent 运行实例不会共享 Todo。`snapshot()` 在锁内复制列表，返回独立字典，供上下文安全读取。

## 核心概念四：每轮重建 Structured State

`ContextManager._render_state()` 每轮从 `snapshot()` 生成一条 system 消息：

```text
[Structured State]
Task: 修复回归
Current goal: 修改实现
Todos: [pending] 定位失败测试; [in_progress] 修改实现
Files changed: (none)
Status: running
```

它是当前请求视图，不追加到 `history`。即使历史被裁剪或压缩，下一轮仍会从状态快照生成最新 Todo。

## 关键流程

```text
main()
  -> AgentState()
  -> create_registry(state)
  -> ToolExecutor(registry, on_result=state.record_tool)
  -> agent_loop
       -> prepare_messages(): snapshot -> Structured State
       -> LLM 调用 update_todo(完整数组)
          ├─ 成功：替换列表，返回更新摘要
          └─ 失败：返回工具错误，旧列表不变
       -> assistant + 对应 role=tool 结果回灌 history
       -> 下一轮重新渲染 Structured State
```

观察重点是：Todo 更新成功后，下一轮请求中的 Structured State 会出现新列表；非法更新只会产生工具错误，不会留下半份新计划。

## 为什么这样设计

把计划放在消息历史中，容易随裁剪或压缩消失；把计划放在独立状态并每轮重建，可以同时保留结构和最新值。完整替换比增量 API 更容易校验，也不会因重试产生重复项目；代价是每次更新都要提交完整列表。

工具绑定运行实例而不是使用全局可变 handler，可以隔离不同 Agent 的状态；代价是 registry 必须显式传递。`update_todo` 只改内存，因此默认允许调用；写文件和 shell 等有副作用工具仍由权限闸门控制。

## 设计边界

- 合法列表会被原子替换，并在下一轮请求中显示。
- 非法类型、字段、长度或多个 `in_progress` 会返回错误，旧状态保持不变。
- 状态锁保护更新和快照读取；本版不支持跨进程共享或磁盘持久化。
- 每个工具调用仍必须回灌对应的 `role=tool` 结果。
- 本版不自动生成、重排或验证 Todo，也不会因为 Todo 未完成而阻止最终文本。

## 实现拆解

`AgentState.update_todos()` 负责校验和原子替换；`make_update_todo_tool()` 把异常转成模型可读的工具结果；`create_registry(state)` 注册绑定当前状态的工具；`ContextManager.prepare_messages()` 每轮读取快照。执行器负责工具异常边界，agent loop 负责按原顺序回灌所有工具结果；LLM 或 CLI 顶层异常仍不由核心 loop 吞掉。

## 运行与观察（按需）

配置本地 LLM 后，在 Bash/zsh 中运行一条包含多个步骤的命令行首条任务：

```bash
PYTHONPATH=src python -m mini_agent "实现一个包含多个步骤的任务，并维护 Todo"
```

观察模型调用 `update_todo` 提交完整数组，以及下一轮请求中的 `[Structured State]` 显示新列表。命令行首条任务处理后，程序仍进入交互循环；后续任务继续使用同一个状态和 history，直到模型再次提交完整列表。

## 本版特性、下一课与代码索引

v0.15 提供实例隔离、严格校验、原子替换和可重建的任务清单。它不自动规划、不持久化，也不因 Todo 未完成而阻止最终回复。下一课将在此基础上加入验证证据，并让模型根据观察结果或验证失败决定是否重排 Todo。

- [状态与快照](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/state.py)
- [Todo 工具](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/tools/todo.py)
- [工具注册表](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/tools/__init__.py)
- [上下文渲染](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/context.py)
- [Agent loop](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/agent.py)
- [CLI 入口](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/__main__.py)
