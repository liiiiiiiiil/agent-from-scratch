# 第 16 课：Plan-driven Execution（v0.16）

> 当前开发版本 v0.16（尚未创建 tag） | [教程总览](README.md) | [上一课：Todo / Task State](15-task-state.md) | 下一课：规划中

## 本课目标

v0.15 的 Todo 表示意图，却不能阻止模型在写文件后直接宣布完成。本课建立 Plan → Execute → Observe → Replan → Verify 闭环：状态层只保存可观察事实，验证证据与最近一次成功修改绑定，并提供一次性纠正提醒。

## 前置条件与版本切换

Python 3.10+，运行时仅标准库。开发期没有 `v0.16` tag，先切换上一课并查看工作区差异；发布后将命令中的范围替换为 `v0.15..v0.16`。

```bash
git checkout v0.15
git diff --stat v0.15
```

## 新增与改动文件

| 文件 | 变化 | 作用 |
|---|---|---|
| `state.py` | `VerificationEvidence`、generation、完成条件、任务重置 | 保存验证事实与状态转换依据 |
| `tools/shell.py` | `purpose=execution|verification` | 区分执行命令和验证命令 |
| `context.py` | Runtime Notice、验证状态渲染 | 只向下一次请求注入提醒 |
| `agent.py` | reminder/blocked/failed 收口 | 防止过早结束或无限循环 |
| `prompt.py` | 闭环规则 | 引导计划、重排和独立验证 |
| `__main__.py`、`tests/test_state.py`、`test_loop.py`、`test_tools.py` | 运行实例与验收 | 覆盖 CLI 和边界 |

## 为什么需要本版

Todo 不是证据；一次成功写入后，旧测试结果也不再可信。每次成功 `write_file`/`edit_file` 以及所有实际进入执行阶段的 `run_shell(purpose="execution")` 都递增 generation 并清空旧证据，即使命令最终非零退出或超时也一样。只有 `run_shell` 的 `purpose="verification"`、明确解析到 `[exit=0]` 的结果，才满足当前 generation 的验证条件；建议把最终测试或检查作为最后一个 verification 调用。这是在零第三方依赖下的保守正确性选择。

## 关键流程

```text
begin_task -> Plan(update_todo) -> Execute(tools) -> Observe(state)
       -> Replan(update_todo) -> Verify(run_shell verification) -> done
无工具最终回复 + 缺口 -> 一次 Runtime Notice -> 继续；仍有缺口 -> blocked
```

## 实现拆解

`begin_task(task)` 清空上一任务状态但保留会话 `history`，状态回到 `running`。正常完成置 `done`，达到最大迭代或顶层异常置 `failed`，提醒后仍有缺口则置 `blocked`。简单只读任务没有 `files_changed`，可直接结束。

正常 shell 命令始终带 `[exit=N]`；超时只带 `[timeout]`。`purpose` 默认 `execution`，旧调用兼容。验证证据记录 command、outcome、exit_code 和 output。

`completion_reminder()` 只在存在未完成 Todo 或可能改变环境的操作后缺少验证时返回提醒。Runtime Notice 只注入下一次请求（即使该请求触发自动 compaction 也会保留），不写入 history，最多纠正一次；第二次仍有缺口就 blocked，不无限循环。

## 设计选择与边界

运行时不自动规划、重试或持久化，也不撤回已流式输出的草稿。验证只在发生成功写入后成为完成条件；证据代表退出状态，不保证业务正确性。

## 最小无网络示例与典型场景

```bash
PYTHONPATH=src python - <<'PY'
from mini_agent.state import AgentState
s = AgentState(); s.update_todos([{"content":"检查", "status":"in_progress"}])
s.record_tool("write_file", {"path":"a.py"}, True, "已写入")
print(s.completion_reminder()["verification_required"])
s.record_tool("run_shell", {"command":"pytest", "purpose":"verification"}, True, "[exit=0] ok")
print(s.has_verification_evidence())
PY
```

写入后提醒验证；验证通过后可 `done`；再次写入使旧证据失效并再次要求验证；两次最终回复仍有缺口则 `blocked`。

## 测试与验收

```bash
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python tests/test_state.py
PYTHONPATH=src python tests/test_loop.py
PYTHONPATH=src python tests/test_tools.py
```

重点检查 execution 不产生证据、verification 零/非零/超时格式、写入使证据失效、简单任务直接结束、提醒一次性和失败状态。

## 本版特性、下一课与代码索引

本版独有特性是 generation 绑定的验证闭环和明确的 done/blocked/failed 状态；下一版本规划中。

- [state.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/state.py)
- [agent.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/agent.py)
- [context.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/context.py)
- [shell.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/tools/shell.py)
- [__main__.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/__main__.py)
- [test_state.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/tests/test_state.py)
- [test_loop.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/tests/test_loop.py)
- [test_tools.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/tests/test_tools.py)
