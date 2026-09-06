# 第 15 课：任务清单与状态（Todo / Task State，v0.15）

上一课：[项目级指令](14-project-instructions.md) · [教程总览](README.md) · 下一课：[计划驱动执行](16-plan-driven-execution.md)

> 代码快照：`v0.15` · 相邻差异：`v0.14..v0.15` · 命令环境：Bash/zsh

> 运行要求：Python 3.10+。运行时只使用标准库。

## 本课目标

v0.14 让 Agent 能看到项目规则，但没有一个独立的位置记录“准备做什么”和“当前做到哪一步”。本课引入 Todo（任务清单）和 `AgentState` 中的任务状态。读完本课，你应能：

- 区分 Todo 意图与工具执行事实；
- 解释完整替换、字段校验、唯一 `in_progress` 和失败不改旧状态；
- 跟踪 state-bound registry（绑定运行状态的工具注册表）如何隔离不同运行实例；
- 说明 Structured State（结构化状态）如何从快照重建，以及本版为什么不自动规划、不持久化、也不据此阻止完成。

本课主线是：**Todo 记录可校验的任务意图，不是执行证据，也不是规划器。**

## 上一版的问题

v0.14 的 `AgentState` 只记录工具历史、文件变化和错误；如果把 Todo 写进 assistant 消息，历史裁剪或压缩后它可能消失。如果把工具结果也写进计划，模型的意图和运行时事实又会混在一起。v0.15 要解决的是“计划存在哪里、如何在每轮请求中可靠呈现”，而不是替模型生成计划或判断任务是否完成。

## 前置条件与版本切换

先阅读[第 14 课](14-project-instructions.md)，了解受保护 system prompt 和 `ContextManager`。下面命令均适用于 Bash/zsh；最后一条把工作树切回当前课程的快照：

```bash
git checkout v0.14
git diff --stat v0.14..v0.15
git diff v0.14..v0.15 -- src/mini_agent/state.py src/mini_agent/tools/todo.py src/mini_agent/tools/__init__.py src/mini_agent/agent.py src/mini_agent/context.py src/mini_agent/permission.py src/mini_agent/prompt.py src/mini_agent/__main__.py
git checkout v0.15
```

## 新增与改动文件

先用 `git diff --stat v0.14..v0.15` 确认本版范围。下表只列教学主线直接涉及的文件。

| 文件 | 变化 | 作用 |
|---|---|---|
| `src/mini_agent/state.py` | 新增 `TodoItem`、`todos`、`update_todos()` 和快照字段 | 校验并原子替换任务意图，推导 `current_goal` |
| `src/mini_agent/tools/todo.py` | 新增 `make_update_todo_tool(state)` | 把 Todo 工具绑定到一个运行实例 |
| `src/mini_agent/tools/__init__.py` | 新增 `create_registry(state)` | 创建包含 state-bound 工具的独立 registry |
| `src/mini_agent/agent.py` | `call_llm()` 接收运行实例 registry | 让模型看到并调用当前实例的工具集合 |
| `src/mini_agent/context.py` | Structured State 渲染 Todos；每轮重新插入 | 让最新快照进入请求视图，不写入 history |
| `src/mini_agent/permission.py` | `update_todo` 设为 `ALLOW` | 内存状态更新不触发副作用权限询问 |
| `src/mini_agent/prompt.py` | 增加 Todo 使用规则 | 告诉模型何时建立计划及其边界 |
| `src/mini_agent/__main__.py` | 创建并复用 state、registry、executor | 保证 CLI 的一次运行使用同一份状态 |

## 版本变更定位

图例：`[旧]` v0.14 已有；`[+]` v0.15 新增；`[~]` v0.15 修改；`[C]` 主要消费者；`[B]` 本版边界/不负责。

### v0.14 基线图

```text
[旧] CLI main()
  -> [旧] AgentState + history + ContextManager
  -> [旧] 全局 registry -> ToolExecutor
  -> [旧] agent_loop
       -> [旧] call_llm(history/Structured State)
       -> [旧] ToolExecutor.execute()
            -> [旧] PermissionGate -> tool.handler
            -> [旧] state.record_tool()
       -> [旧] role=tool 结果回灌 history
       -> [C] 下一轮 LLM 或最终文本
```

这张图只说明 v0.14 的真实入口、工具调用链和收口方式：计划没有独立字段，模型只能从消息历史中保留自己的计划。

### v0.15 变更图

```text
[旧] CLI main()
  -> [~] AgentState + history + ContextManager
  -> [+] create_registry(state) -> [~] ToolExecutor
  -> [旧] agent_loop
       -> [~] call_llm(..., tool_registry=run_registry)
       -> [+] update_todo(todos=完整列表)
            -> [+] state-bound handler
            -> [+] AgentState.update_todos()
                 -> 成功：替换 todos，推导 current_goal
                 -> 失败：返回错误，旧状态不变
       -> [旧] 其他工具 -> [旧] state.record_tool()
       -> [~] ContextManager.prepare_messages()
            -> [C] state.snapshot()
            -> [C] [Structured State] Todos
       -> [旧] role=tool 结果回灌 history -> 下一轮 LLM

[B] 不自动生成计划、不持久化 Todo、不用 Todo 自动判定 done/blocked/failed
```

变更映射如下；它对应的是调用和数据流，不是改动文件清单的重复绘制。

| 图中节点/边 | 对应实现 | 作用 |
|---|---|---|
| `create_registry(state)` -> `update_todo` | `tools/__init__.py`, `tools/todo.py` | 每个 `AgentState` 拥有自己的 Todo handler |
| `call_llm(..., tool_registry=...)` | `agent.py` | 请求携带当前 registry 的 schema |
| `update_todos()` -> `snapshot()` | `state.py` | 校验、原子替换并输出独立快照 |
| `snapshot()` -> `Structured State` | `context.py` | 每轮重建最新 Todos，跨裁剪/压缩保留 |
| 非法 Todo -> 工具错误结果 | `todo.py`, `ToolExecutor` | 错误回灌模型，旧状态继续可用 |

## 核心概念与数据结构

### Todo 意图与执行事实

Todo 是模型提交的任务意图；`tool_history`、`files_changed` 和 `errors` 是工具执行后的事实。`update_todos()` 只更新前者，`record_tool()` 只记录后者；`record_tool()` 对 `update_todo` 直接返回，因此“打算做什么”不会伪装成“已经发生什么”。

### 可校验的完整列表

`TodoItem` 是不可变数据对象，状态只能是 `pending`、`in_progress` 或 `completed`。每次工具调用提交完整数组，最多 50 项，每项 `content` 去首尾空白后为 1–240 个字符，且最多一个 `in_progress`：

```python
parsed: list[TodoItem] = []
for item in todos:
    content = item.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Todo content 必须是非空字符串")
    status = item.get("status", "pending")
    if status not in ("pending", "in_progress", "completed"):
        raise ValueError("Todo status 非法")
    parsed.append(TodoItem(content.strip(), status))
if sum(item.status == "in_progress" for item in parsed) > 1:
    raise ValueError("最多只能有一个 in_progress Todo")
```

所有检查完成后才进入锁内替换，因此非法列表不会部分写入：

```python
with self._lock:
    self.todos = parsed
    self.current_goal = next(
        (item.content for item in parsed if item.status == "in_progress"), ""
    )
```

### state-bound registry 与快照

`make_update_todo_tool(state)` 捕获一个明确的 `AgentState`。CLI 用 `create_registry(state)` 创建运行实例专属 registry，再把同一个 registry 交给 `ToolExecutor` 和 `call_llm()`；两个运行实例不会共享 Todo。`snapshot()` 在锁内复制列表，返回值可安全交给上下文渲染。

### 可重建的 Structured State

`ContextManager._render_state()` 每轮从 `snapshot()` 生成 system 消息：

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

它是当前请求视图，不追加到 `history`。未压缩时插在首条 user 消息前；压缩后也会重新生成，所以 Todo 不依赖历史原文。

## 为什么这样设计

把计划放在消息历史中会随 trimming/compaction 消失，自由文本也难以稳定校验。把它放进独立状态并在每轮重建，能同时保留结构和最新值。完整替换比 `add_todo`、`remove_todo` 等增量 API 更容易验证，也避免重试造成重复；代价是每次更新都要提交完整列表。

Todo 工具绑定运行实例而不是使用全局可变 handler，是为了防止多个 Agent 运行相互覆盖；代价是 registry 和调用链需要显式传递。`update_todo` 只改内存，所以默认放行；写文件、执行 shell 等有副作用的工具仍由权限闸门处理。

## 设计边界

- **正常路径**：模型提交合法完整列表，状态原子替换，下一轮 Structured State 显示新 Todo。
- **失败路径**：数组、字段、长度或进行中项不合法时，handler 返回 `Todo 更新失败: ...`；错误作为对应 `role=tool` 回灌，旧 Todo 和 `current_goal` 保持不变。
- **并发边界**：状态替换和快照读取受同一把锁保护；本版不提供跨进程共享或磁盘持久化。
- **协议不变量**：每个 `tool_call` 都有对应 `role=tool` 结果；Todo 更新不会写入执行事实列表。
- **刻意不解决**：运行时不自动生成计划、不自动重排、不验证 Todo 是否真的完成，也不因未完成 Todo 阻止最终文本。下一课才把计划、执行和验证连成收口条件。

## 关键流程

```text
main()
  -> state = AgentState()
  -> run_registry = create_registry(state)
  -> ToolExecutor(run_registry, on_result=state.record_tool)
  -> agent_loop
       -> prepare_messages(): snapshot -> Structured State
       -> LLM 调用 update_todo(完整数组)
       -> Executor: 权限放行 -> handler -> update_todos()
          ├─ 成功：返回更新摘要
          └─ 失败：返回错误摘要，旧状态不变
       -> assistant + 全部 role=tool 结果写入 history
       -> 下一轮重新渲染 Structured State
```

CLI 的命令行首条任务和交互输入共用同一个 state、registry、executor 与 history；v0.15 没有 `begin_task()`，因此后续任务不会自动清空旧 Todo。

## 实现拆解

### 1. 状态校验和原子替换

入口是 [`AgentState.update_todos()`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/state.py)。它先在锁外解析所有项，再在锁内一次性替换；`snapshot()` 返回独立字典，避免调用方直接修改内部列表。

### 2. 绑定工具与权限

[`make_update_todo_tool()`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/tools/todo.py) 把 `TypeError`/`ValueError` 转成字符串结果，成功时返回项目数和当前目标。`create_registry(state)` 注册普通工具后注册它；[`update_todo`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/permission.py) 的规则为 `ALLOW`，但这不改变其他工具的权限策略。

### 3. 请求中注入当前状态

[`ContextManager._render_state()`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/context.py) 每次调用 `prepare_messages()` 都重新读取快照。[`agent_loop()`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/agent.py) 将 `ToolExecutor.registry` 传给 `call_llm()`，因此模型收到的是当前运行实例的工具 schema，而不是不含 Todo 的全局 registry。

### 4. 工具结果和异常边界

Executor 负责把 handler 异常转成工具结果；loop 负责为每个 call 回灌结果并继续下一轮。非法 Todo 因此能被模型看到并修正，但 LLM 请求异常和 CLI 顶层异常仍不会被核心 loop 吞掉。`record_tool()` 只接收普通工具的执行事实，`update_todo` 不会增加工具计数、文件列表或错误列表。

## 运行与观察

配置好本地 LLM 后，用一个包含多个步骤的真实任务启动 CLI（命令环境：Bash/zsh）：

```bash
PYTHONPATH=src python -m mini_agent "实现一个包含多个步骤的任务，并维护 Todo"
```

观察模型调用 `update_todo` 提交完整数组，以及下一轮请求中的 `[Structured State]` 显示新列表。命令行首条任务处理后，程序仍进入交互循环；继续输入任务时，旧 Todo 会保留，直到模型再次提交完整列表。

## 本版特性、下一课与代码索引

v0.15 提供实例隔离、严格校验、原子替换和可重建的任务清单。它不自动规划、不持久化，也不因 Todo 未完成而阻止最终回复。下一课将在此基础上加入计划驱动执行和验证收口。

- [状态与快照](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/state.py)
- [Todo 工具](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/tools/todo.py)
- [工具 registry](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/tools/__init__.py)
- [Agent loop 与 LLM registry](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/agent.py)
- [Structured State](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/context.py)
- [Prompt 与权限规则](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/prompt.py)
- [CLI 运行入口](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/__main__.py)
- [状态测试](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/tests/test_state.py)
- [工具与隔离测试](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/tests/test_tools.py)
- [上下文测试](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/tests/test_context.py)
