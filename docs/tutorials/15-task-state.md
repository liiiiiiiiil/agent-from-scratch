# 第 15 课：把任务计划保存成可靠状态（Todo / Task State，v0.15）

上一课：[项目级指令](14-project-instructions.md) · [教程总览](README.md) · 下一课：[计划驱动执行](16-plan-driven-execution.md)

> 代码快照：`v0.15` · 相邻差异：`v0.14..v0.15` · 命令环境：Bash/zsh

> 运行要求：Python 3.10+。运行时只使用标准库。

## 上一版的问题

一个 Agent（能调用工具完成任务的模型程序）需要同时记住两类不同的东西：它打算做什么，以及已经实际做了什么。v0.14 只有消息历史可以承载这些信息。历史被裁剪或压缩后，计划可能消失；如果把工具结果也当成计划，模型的意图和执行事实又会混在一起。

v0.15 只解决“计划存在哪里、怎样稳定地呈现给模型”。它不会替模型生成计划，也不会因为 Todo 未完成就自动判断任务失败或阻止最终回复。

## 本课目标

本课把这两个概念分开：Todo（任务清单）记录模型的任务意图，工具记录实际执行结果。读完后，你应该能够：

- 说明为什么 Todo 每次提交完整列表，而不是逐项增删；
- 解释为什么一项非法时旧列表仍然保留；
- 说明为什么不同运行实例不会互相覆盖 Todo；
- 看懂 Todo 如何在每轮请求中重新出现在当前状态视图里。

本课主线是：**Todo 是可校验的计划，不是执行证据，也不是自动规划器。**

## 前置条件与版本切换

先阅读第 14 课，知道 `ContextManager`（上下文管理器）会为每轮请求准备消息，并知道受保护的 system prompt（系统提示）不会随普通历史一起丢失。下面的命令在 Bash/zsh 中执行；它先切到上一版，查看真实差异，再回到本课快照：

```bash
git checkout v0.14
git diff --stat v0.14..v0.15
git diff v0.14..v0.15 -- src/mini_agent/state.py src/mini_agent/tools/todo.py src/mini_agent/tools/__init__.py src/mini_agent/context.py src/mini_agent/agent.py src/mini_agent/__main__.py
git checkout v0.15
```

`git diff --stat` 先告诉你本版大致改了多少；第二条 `git diff` 只展开本课主线文件。最后的 `git checkout v0.15` 是为了让后续阅读与运行对应本课快照。

## 新增与改动文件

先用上面的差异确认范围，再把变化按“问题—位置”对应起来：

| 文件 | 本版变化 | 解决的问题 |
|---|---|---|
| `src/mini_agent/state.py` | 增加 `TodoItem`、`todos`、`update_todos()` 和快照字段 | 保存、校验并安全读取计划意图 |
| `src/mini_agent/tools/todo.py` | 增加绑定状态的 `update_todo` 工具 | 让模型提交完整 Todo 列表 |
| `src/mini_agent/tools/__init__.py` | 增加 `create_registry(state)` | 让每个运行实例拥有自己的工具 |
| `src/mini_agent/context.py` | 每轮把 Todo 渲染为 Structured State | 裁剪或压缩后仍能看到最新计划 |
| `src/mini_agent/agent.py`、`src/mini_agent/__main__.py` | 传递当前 registry 并复用同一份状态 | 保持 CLI、执行器和 LLM 使用同一实例 |

## 版本变更定位

下面先看 v0.14 的真实入口和收口方式，再看 v0.15 把 Todo 插入的位置。图例：`[旧]` v0.14 已有，`[+]` v0.15 新增，`[~]` v0.15 修改，`[C]` 主要消费者，`[B]` 本版边界。

v0.14 基线图：

```text
[旧] CLI main()
  -> [旧] AgentState + history + ContextManager
  -> [旧] 全局 registry -> ToolExecutor
  -> [旧] agent_loop -> call_llm
       -> 工具 handler -> state.record_tool()
       -> role=tool 结果回灌 history -> 下一轮 LLM
```

这条基线说明：上一版已有独立的执行状态和工具结果回灌，但没有保存 Todo 计划的字段，也没有给每个运行实例绑定 Todo 工具。

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

关键不在于“多了一个列表”，而在于同一个 `AgentState` 实例同时被 Todo 工具、工具执行器和上下文管理器使用。这样，模型提交的计划和下一轮请求看到的计划来自同一份状态。

## 核心概念与数据结构

### 1. 先分清计划和事实

如果模型说“准备修改文件”，这句话不能证明文件已经修改。v0.15 让 `update_todo` 只提交计划；文件修改、shell 执行和错误仍由普通工具结果记录到 `tool_history`、`files_changed` 和 `errors`。`record_tool()` 特意跳过 Todo：

```python
if name == "update_todo":
    return
```

这段判断的用途是保持两条记录分开：调用 Todo 后，执行日志不会多出一条“已经完成”的假事实。下一课会在这条边界上增加独立的验证证据。

### 2. 用完整列表做一次原子更新

逐项调用 `add_todo`、`remove_todo` 会让中间状态很难校验，也容易在重试时留下重复项目。因此 v0.15 规定每次 `update_todo` 都提交完整数组。运行时先把整份数组检查完，最后才替换旧列表：

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

代码前半段只构造 `parsed`，没有触碰旧状态；最后的锁内赋值才是替换点。合法状态是 `pending`（待处理）、`in_progress`（进行中）和 `completed`（已完成），最多一个 Todo 可以是 `in_progress`；每项内容最多 240 个字符，列表最多 50 项。任何一项非法时，异常在替换前抛出，工具层把它变成可回灌的工具错误，旧列表保持不变。

### 3. 让工具绑定当前运行实例

如果 Todo 工具使用全局可变状态，两个 Agent 运行实例可能互相覆盖计划。v0.15 让 `make_update_todo_tool(state)` 捕获一份明确的 `AgentState`；CLI 用 `create_registry(state)` 创建当前运行实例的 registry（工具注册表），再把同一个 registry 交给执行器和 LLM：

```python
state = AgentState()
run_registry = create_registry(state)
context = ContextManager(state, history)
tool_executor = ToolExecutor(run_registry, on_result=state.record_tool)
```

这段组装代码的关键是同一个 `state` 贯穿三处。于是两个运行实例各有自己的 Todo。`snapshot()` 在锁内复制列表并返回独立字典，读取方不会拿到正在被工具修改的内部对象。

### 4. 每轮重建当前请求视图

Structured State（结构化状态）是运行时根据状态快照临时生成的一条 system 消息；它不是追加到 `history` 的旧对话。每轮 `_render_state()` 会得到类似下面的视图：

```text
[Structured State]
Task: 修复回归
Current goal: 修改实现
Todos: [pending] 定位失败测试; [in_progress] 修改实现
Files changed: (none)
Status: running
```

这段输出的用途是说明“当前值从哪里来”：即使普通历史已经裁剪或压缩，模型仍应看到最新 Todo。输出中的 `Todos` 来自刚才的 `snapshot()`，所以成功更新后下一轮会看到新列表，非法更新不会留下半份计划。

## 为什么这样设计

把计划只放在消息历史中，容易在裁剪或压缩时消失；把计划放在独立状态、每轮重新渲染，则能同时保留结构和最新值。完整替换比增量 API 更容易校验，也不会因重试产生重复项目；代价是每次更新都要提交完整列表。

工具绑定运行实例而不是使用全局可变 handler，可以隔离不同 Agent 的状态；代价是 registry 必须显式传递。`update_todo` 只改内存，因此默认允许调用；写文件和 shell 等有副作用工具仍由权限闸门控制。

## 设计边界

- 合法列表会被原子替换，并在下一轮请求中显示。
- 非法类型、字段、长度或多个 `in_progress` 会返回错误，旧状态保持不变。
- 状态锁保护更新和快照读取；本版不支持跨进程共享或磁盘持久化。
- 每个工具调用仍必须回灌对应的 `role=tool` 结果。
- 本版不自动生成、重排或验证 Todo，也不会因为 Todo 未完成而阻止最终文本。

## 关键流程

下面的流程把“提交计划”和“执行工具”分开。成功更新后，下一轮请求中的 Structured State 会带上新列表；非法更新只产生工具错误，旧列表仍在。

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

## 实现拆解

`AgentState.update_todos()` 负责校验和原子替换；`make_update_todo_tool()` 把 `TypeError`/`ValueError` 转成模型可读的工具结果；`create_registry(state)` 注册绑定当前状态的工具；`ContextManager.prepare_messages()` 每轮读取快照。执行器负责工具异常边界，agent loop 负责按原顺序回灌所有工具结果；LLM 或 CLI 顶层异常仍不由核心 loop 吞掉。

## 运行与观察（按需）

配置本地 LLM 后，在 Bash/zsh 中运行一条包含多个步骤的命令行首条任务：

```bash
PYTHONPATH=src python -m mini_agent "实现一个包含多个步骤的任务，并维护 Todo"
```

“命令行首条任务”是传给 CLI 的第一项任务参数；处理后程序仍进入交互循环。观察模型是否调用 `update_todo` 提交完整数组，以及后续行动是否继续围绕最新清单展开。`[Structured State]` 是发给模型的内部请求视图，CLI 默认不会把它直接打印到终端。后续任务继续使用同一个状态和 history，直到模型再次提交完整列表。

## 本版特性、下一课与代码索引

v0.15 提供实例隔离、严格校验、原子替换和可重建的任务清单。它不自动规划、不持久化，也不因 Todo 未完成而阻止最终回复。下一课将在此基础上加入验证证据，并让模型根据观察结果或验证失败决定是否重排 Todo。

- [状态与快照](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/state.py)
- [Todo 工具](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/tools/todo.py)
- [工具注册表](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/tools/__init__.py)
- [上下文渲染](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/context.py)
- [Agent loop](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/agent.py)
- [CLI 入口](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/__main__.py)
