# 第 15 课：任务清单与状态（Todo / Task State，v0.15）

上一课：[项目级指令](14-project-instructions.md) · [教程总览](README.md) · 下一课：[计划驱动执行](16-plan-driven-execution.md)

> 代码快照：`v0.15` · 相邻差异：`v0.14..v0.15` · 命令环境：Bash/zsh

> 运行要求：Python 3.10+。运行时只使用标准库。

## 本课目标

第 14 课解决了“模型应遵守哪些项目规则”，但没有回答“这项任务还剩哪些步骤”。如果 Todo 只写在 assistant 消息中，长对话的 trimming 或 compaction 可能移除它；如果把工具结果也混入计划，模型的意图和环境事实又会混在一起。

v0.15 引入显式任务清单（Todo）和独立的 `AgentState`。读完本课，你应能：

- 解释 Todo 意图与工具执行事实为何分开保存；
- 说明完整替换、字段校验、最多一个 `in_progress` 和失败不改旧状态的原子性；
- 跟踪 state-bound registry 如何把 `update_todo` 绑定到单个运行实例；
- 说明每轮 Structured State 如何从快照重建，以及本版没有自动规划、持久化和完成校验。

本课的学习主线是：**Todo 记录可校验的任务意图，不是执行证据，也不是规划器。**

## 前置条件与版本切换

先阅读[第 14 课](14-project-instructions.md)，了解受保护项目指令和 `ContextManager`。下面命令均适用于 Bash/zsh；`git diff` 只查看本课涉及的实现：

```bash
git checkout v0.14
git diff --stat v0.14..v0.15
git diff v0.14..v0.15 -- src/mini_agent/state.py src/mini_agent/tools/todo.py src/mini_agent/tools/__init__.py src/mini_agent/context.py src/mini_agent/permission.py src/mini_agent/__main__.py
git checkout v0.15
```

## 新增与改动文件

| 文件 | 相对 v0.14 的变化 | 作用 |
|---|---|---|
| `src/mini_agent/state.py` | 新增 `TodoItem`、`todos` 和 `update_todos()` | 校验并原子替换任务意图，推导 `current_goal` |
| `src/mini_agent/tools/todo.py` | 新文件，新增 `make_update_todo_tool(state)` | 将 Todo 工具绑定到一个 `AgentState` |
| `src/mini_agent/tools/__init__.py` | 新增 `create_registry(state)` | 为每次运行创建实例专属 registry |
| `src/mini_agent/context.py` | Structured State 增加 Todos；每轮插入状态消息 | 让模型读取最新快照 |
| `src/mini_agent/permission.py` | `update_todo` 设为 `ALLOW` | 内存更新不触发副作用权限询问 |
| `src/mini_agent/__main__.py` | 创建 state、registry 和回调链 | 保证 CLI 运行实例隔离 |

## 版本变更定位

v0.14 已有 `AgentState`、工具执行回调和 ContextManager；v0.15 只在这条链上增加 Todo 意图，并让每轮请求重新渲染它：

```text
v0.14：工具调用 -> ToolExecutor -> AgentState（执行事实）
                         └-> ContextManager（任务/工具/错误状态）

v0.15：模型调用 update_todo
          -> state-bound handler
          -> AgentState.update_todos（校验后原子替换）
          -> state.snapshot()
          -> ContextManager._render_state()
          -> 下一轮请求中的 [Structured State] Todos

入口：update_todo                  主要消费者：ContextManager
本版不负责：自动生成计划、持久化 Todo、以 Todo 自动判定 done/blocked/failed
```

## 为什么这样设计

把计划留在消息历史中有两个问题：历史被裁剪后，模型看不到原列表；自由文本也无法稳定表达状态。`AgentState.update_todos()` 先把整个数组解析成不可变 `TodoItem`，检查通过后才替换旧列表，因此状态结构稳定，失败时旧快照仍可用。

另一种选择是 `add_todo`、`remove_todo` 等增量 API，但重试或乱序会产生重复和顺序歧义。完整替换更容易验证，代价是每次更新都要提交完整数组。Todo 仍只是模型意图：`record_tool()` 会跳过 `update_todo`，文件变更和错误继续由真实工具结果记录。v0.15 刻意不把两者合并，也不把 Todo 写入磁盘；验证闭环由下一课引入。

## 关键流程

```text
main()
  -> state = AgentState()
  -> run_registry = create_registry(state)
  -> ToolExecutor(run_registry, on_result=state.record_tool)
  -> agent_loop 调用 update_todo(todos=完整数组)
       -> update_todos：类型/数量/字段/唯一进行中项校验
       -> 成功：替换 todos，并由进行中项推导 current_goal
       -> 失败：工具返回“Todo 更新失败: ...”，旧状态不变
  -> 下一轮 prepare_messages()
       -> snapshot() -> [Structured State] 中的 Todos
```

`update_todo` 在权限表中是 `ALLOW`，因为它只修改内存。CLI 的命令行首条任务和交互输入共用同一个 `state`、registry 与 history；v0.15 没有 `begin_task()`，所以后续任务不会自动清空旧 Todo，调用者需要提交新的完整列表。

## 实现拆解

### 1. 状态结构与原子校验

`TodoItem` 是不可变数据对象；`AgentState` 最多保存 50 项，每项内容去首尾空白后为 1–240 字符，状态只能是 `pending`、`in_progress` 或 `completed`，且最多一个进行中项：

```python
def update_todos(self, todos: list[dict[str, Any]]) -> None:
    # 先构造 parsed 并完成所有检查
    ...
    if in_progress > 1:
        raise ValueError("最多只能有一个 in_progress Todo")
    with self._lock:
        self.todos = parsed
        self.current_goal = next(
            (t.content for t in parsed if t.status == "in_progress"), ""
        )
```

省略号前的校验包含数组类型、50 项上限、对象类型、非空 `content`、240 字符上限和状态枚举。锁只包住最终替换，避免半更新；`snapshot()` 再把 Todo 转成独立字典供读取。[完整实现](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/state.py)

### 2. 绑定运行实例的工具

工具工厂捕获传入的 `AgentState`。handler 把 `TypeError`/`ValueError` 转成工具结果，成功时返回项目数和当前目标；它不会直接改消息历史：[工具实现](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/tools/todo.py)。

`create_registry(state)` 注册普通工具后再注册这个绑定工具。全局 `registry` 仍用于旧版 smoke test，但不包含 Todo；CLI 明确使用 `create_registry(state)`，因此两个运行实例的 Todo 不会互相覆盖。[registry 实现](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/tools/__init__.py)

### 3. Structured State 重建

`ContextManager._render_state()` 每次从 `state.snapshot()` 生成 system 消息：

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

这条消息属于当前请求视图，不追加进 history。未压缩时它位于首条 user 消息之前；压缩后它仍会在受保护消息之后重新生成，所以 trimming/compaction 不会让 Todo 消失。[渲染逻辑](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/context.py)

### 4. 与执行事实的边界

`record_tool()` 对 `update_todo` 直接返回，不写入 `tool_history` 或 `errors`；`write_file`、`edit_file` 的成功结果才会进入 `files_changed`，失败结果才会进入 `errors`。这样“打算做什么”和“实际发生什么”不会互相伪装。[状态回调](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/state.py)

## 运行与观察（按需）

配置好 LLM 后，用一项包含多个步骤的真实任务启动 CLI。观察模型调用 `update_todo` 提交完整数组，再观察下一轮请求的 `[Structured State]` 显示新列表。命令行首条任务处理完后，程序仍进入交互循环；若继续输入新任务，Todo 会保留，直到模型再次完整提交列表。

## 本版特性、下一课与代码索引

v0.15 提供实例隔离、严格校验、原子替换和可重建的任务清单，但不自动规划、不持久化，也不因未完成 Todo 阻止最终回复。下一课将在此状态基础上加入 Plan → Execute → Observe → Replan → Verify，以及 `done`、`blocked`、`failed` 收口。

- [状态与快照](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/state.py)
- [Todo 工具](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/tools/todo.py)
- [工具 registry](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/tools/__init__.py)
- [Structured State](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/context.py)
- [权限规则](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/permission.py)
- [CLI 运行入口](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/src/mini_agent/__main__.py)
- [状态测试](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/tests/test_state.py)
- [工具隔离测试](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.15/tests/test_tools.py)
