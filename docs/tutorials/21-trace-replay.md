# 第 21 课：Trace & Replay 只读回放（v0.21）

上一课：[修复循环](20-repair-loop.md) · [教程总览](README.md) · 下一课：按需追加

> 代码快照：`v0.21` · 相邻差异：`v0.20..v0.21` · 命令环境：Bash/zsh

> 运行要求：Python 3.10+；核心运行时只使用标准库。

## 本课目标

修复循环能让 Agent 从失败走向恢复，但运行结束后，读者仍需要回答一个问题：这次任务究竟经历了哪些状态转换？本课加入 Trace & Replay（回放），把当前进程、当前任务中已经保存的结构化事实按 generation（代次，验证证据的边界）重新组织出来。

读完本课，你应能解释：

- Todo 为什么需要保存每次成功提交的完整快照，而不只是当前列表；
- `ExecutionAttempt`、`FailureEvent`、`RecoveryAction` 和 verification evidence 如何组成一条因果链；
- 为什么断链和跨 generation 证据只能显示为 incomplete，不能由回放器猜测；
- 为什么 `/trace` 是诊断观察入口，不是重新执行、恢复或导入历史的入口。

## 上一版的问题

v0.20 已经保存执行尝试、失败、恢复、generation 和当前代的验证证据。可是当前证据会在新 generation 开启时清空，这是防止旧证据被用于完成判定的正确行为，却也意味着它不能单独承担跨代审计。与此同时，读者仍要把多个数组按 ID 手工拼接；Todo 也只保留当前版本，无法知道计划在哪一代发生了什么变化。

如果展示层只按列表顺序猜测因果，一条缺失引用就可能被误读成“恢复成功”或“验证通过”。因此 v0.21 的重点不是增加新的执行能力，而是建立一个保守的只读视图：能确认的边明确连起来，不能确认的边保留原记录并标记 unresolved（未解析）。

## 前置条件与版本切换

建议先阅读第 20 课。下面的命令在 Bash/zsh 中查看本版范围；`v0.21` tag 由发布者在实现验收后创建，若本地尚未创建，差异命令需在 tag 可用后运行。

```bash
git checkout v0.20
git diff --stat v0.20..v0.21
git diff v0.20..v0.21 -- src/mini_agent/state.py src/mini_agent/trace.py src/mini_agent/__main__.py
git checkout v0.21
```

## 新增与改动文件

本课的教学主线集中在审计快照、回放组装和显式 CLI 入口；完整范围以实际 `git diff --stat` 为准。

| 文件 | 变化 | 作用 |
|---|---|---|
| `src/mini_agent/state.py` | 新增 `TodoRevision` 审计快照 | 记录每次成功 Todo 提交及其 generation |
| `src/mini_agent/trace.py` | 新增只读 API 和渲染器 | 校验引用、构建 generation 视图、输出因果边 |
| `src/mini_agent/__main__.py` | 新增 `/trace` 分支 | 在 `run_task()` 前直接显示回放 |
| `tests/test_trace_replay_v021.py` | 新增回放回归覆盖 | 固定只读、安全、断链和真实工具链行为 |
| `docs/tutorials/21-trace-replay.md` | 新增本课 | 解释回放入口与边界 |

## 版本变更定位

先看 v0.20 的真实收口，再看 v0.21 插入的观察路径。图中 `generation` 是验证边界，不是文件系统快照。

```text
v0.20 基线：
[旧] LLM -> agent_loop -> ToolExecutor -> AgentState
                                      ├-> ExecutionAttempt
                                      ├-> FailureEvent -> RecoveryAction
                                      └-> verification evidence
                                      -> Structured State / 下一轮 LLM
                                      -> continue / done / blocked / failed

v0.21：
[旧] AgentState.snapshot()
        ├-> [旧] 执行事实数组
        ├-> [+] TodoRevision 数组（成功提交时追加）
        ├-> [+] verification_history（只增不删的审计证据）
        └-> [+] trace.build_trace() -> 按 generation 分组与完整性检查
                                      -> trace.render_trace() -> /trace CLI
                                      ├-> [B] 不调用 LLM、工具或 PermissionGate
                                      └-> [B] 断链只显示 unresolved，不猜测修复
```

关键变化是入口位置：`/trace` 在交互分支中被拦截，直接执行 `snapshot → build_trace → render_trace`。它不进入 `run_task()`，所以不会追加 user history，也不会触发 Agent Loop。

## 核心概念与数据结构

### 1. TodoRevision 是成功提交的审计事实

问题在于“当前 Todo”不能回答“计划什么时候改变”。`TodoRevision` 是不可变记录，每次 `update_todos()` 校验成功后，在同一把 State 锁内追加：

```text
TodoRevision
- revision_id: 1, 2, ...（任务内重新编号）
- generation_id: 提交时所在代次
- todos: 完整 TodoItem 快照
- current_goal: 提交时的进行中目标
```

验证失败不会进入锁内提交，因此不会产生 revision。`begin_task()` 和 `/reset` 会清空记录并重置编号；这些记录只出现在回放用的 `snapshot()` 中，不会被 `context.py` 注入 Structured State，避免把整段 Todo 历史扩大到 LLM 上下文。

这种设计把两个问题分开：当前 Todo 继续服务正常执行，`todo_revisions` 只服务事后观察。并发更新由实际获得 State 锁的顺序分配 revision 编号，不使用线程开始时间猜顺序。

### 2. 回放是结构化事实的重新编排

Python API 很小：

```python
from mini_agent.trace import build_trace, render_trace

report = build_trace(state.snapshot())
print(render_trace(report))
```

`build_trace()` 不接收 `AgentState`，而只接收快照。这是一个刻意的边界：它不能访问私有原始参数、retry 参数缓存或 checkpoint 前镜像，也不能借助对话摘要恢复事实。报告包括任务信息、查询范围、按 `generation_id` 排序的 generation，以及每代的 Todo revisions、attempts、failures、recovery actions 和 verification history。

State 同时维护两种验证视图：`verification_evidence` 只保留当前 generation，用于决定任务能否完成；`verification_history` 在任务内只增不删，用于回放先前失败或成功的验证。开启新 generation 仍会清空前者，但不会清空后者，因此回放不会改变旧证据隔离规则。

每个 failure 的诊断显示遵循事实优先顺序：有 `FailureEvent.cause_hint` 就显示它并标记来源；没有时查找关联 `RecoveryAction.reason`；两者都没有就显示“未记录诊断”。回放器不会把这个理由写回 `FailureEvent`。

### 3. 因果边和完整性

回放器不把“相邻出现”当成因果关系，而是使用 State 已保存的 ID：

```text
generation opener
attempt -> failure
failure -> recovery
recovery -> successor generation / result attempt
attempt -> verification evidence
```

它会检查重复 ID、generation 缺失或不连续、opener 类型和所属代次、attempt/failure 双向引用、accepted recovery 的后继和结果、verification attempt 是否有唯一同代历史证据，以及 Todo revision 是否引用存在的 generation。

检查结果放在 `report["integrity"]`：

```text
{"status": "complete", "issues": []}
{"status": "incomplete", "issues": ["..."]}
```

即使状态损坏，能定位的原始记录仍会展示；无法确认的边显示为 `UNRESOLVED`。只有查询参数非法或指定的 generation 不存在才抛出 `TraceQueryError`。这意味着 `incomplete` 是“这份证据不能完整验收”，不是“展示器替用户修好了链”。

## 关键流程

```text
交互输入 /trace [generation_id]
  -> 当前任务检查
  -> state.snapshot()
  -> build_trace(snapshot, generation_id?)
       ├-> 校验 ID、generation 和跨代引用
       ├-> 按 generation 分组原始记录
       └-> 生成 resolved / unresolved 因果边
  -> render_trace(report)
  -> 强制可见 CLI notice
```

如果有后继 generation，当前代的结论显示为 `continue`；最后一代按 State 显示 `continue`、`done`、`blocked` 或 `failed`。`blocked` 和 `failed` 还显示 `terminal_reason` 与最后一个可确认的 failure。

## 运行与观察

在 Bash/zsh 中启动交互模式：

```bash
PYTHONPATH=src python -m mini_agent
```

任务执行过至少一个回合后输入 `/trace`，可看到所有 generation；输入 `/trace 3` 只查看 generation 3。即使 `OUTPUT_MODE=quiet`，显式 `/trace` 仍会通过强制可见的 CLI notice 输出。没有活动任务、generation 参数不是非负整数，或 generation 不存在时，入口会给出明确提示。

观察回放时，先看 `完整性`，再看每一代的 `Attempts`、`Failures`、`Recovery actions` 和 `Verification evidence`。一个恢复 action 的执行结果只说明恢复工具运行过；只有后继 generation 中来源 attempt 明确的新 verification evidence 才能支持完成结论。

## 为什么这样设计

- 选择“快照 API + 纯函数式报告”而不是让 CLI 直接读取 State 私有字段，是为了让回放不会绕过锁、执行器或权限边界，也便于测试 snapshot 在前后完全相等。
- 选择显式 unresolved 而不是丢弃损坏记录，是为了让人工诊断能看到断点；代价是报告可能是不完整的，且不能替用户推导缺失因果。
- Todo revision 使用单次大小受限的完整快照，能解释计划变化，但仍然只保存在当前进程内；它不是数据库，也不提供跨任务历史。
- 渲染器展示 State 已保存的 attempt 结果、耗时、退出码、错误类型、参数 hash、脱敏参数和有界输出摘要；它不读取私有原始参数或 checkpoint 字节，并隐藏绝对路径。

## 设计边界

本版只回放 v0.21 升级后、当前进程和当前任务内产生的事实。`/reset`、`/new` 后旧任务事实随 State 清空；不支持导入、导出、跨进程持久化、重新执行、自动修复或恢复。

回放不调用 LLM，不执行工具，不请求权限，不修改 history、State、预算或 generation。它也不把“缺少证据”解释成失败原因：断链、跨 generation verification、缺失来源 attempt 和旧证据复用都只会降低 `integrity`，不会生成不存在的诊断。

## 实现拆解

`state.py` 在更新 Todo 时先完整校验，再在锁内替换当前列表、更新 `current_goal`、追加 `TodoRevision`；记录 verification 时同时更新当前证据和审计历史。恢复 generation 只通过 `opened_by_recovery_id` 连接 RecoveryAction，恢复 attempt 只通过 `caused_by_failure_id` 连接触发失败，避免一个节点出现多个直接因果前驱。`trace.py` 只复制允许展示的字段：attempt 使用参数 hash、脱敏参数和 `output_excerpt`，recovery 使用 reason、status、checkpoint ID 与结果引用，verification 使用命令、结果、退出码、输出和 generation。`__main__.py` 在 `run_task()` 之前处理 `/trace`，所以回放不会被“无 tool_calls 才结束”的 Agent Loop 条件影响。

正常的失败—恢复—验证链会得到 `complete`；如果删除一个来源 attempt，报告仍保留 failure 或 verification，但对应边为 unresolved，最终完整性为 `incomplete`。这正是回放器的验收价值：它能证明运行时保存的链条完整，也能明确指出当前快照不足以证明什么。

## 本版特性、下一课与代码索引

本版新增 Todo 提交审计、按 generation 的只读 Trace & Replay Python API、文本渲染器和 `/trace [generation_id]` CLI。阶段六至此可以把失败、诊断、恢复、后继 generation、验证和终态放在一份可检查的因果报告中。

下一课按路线图追加；后续能力不能把本版的只读回放入口变成自动执行或跨进程历史系统。

代码索引：

- [`state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.21/src/mini_agent/state.py)：`TodoRevision`、任务边界和快照；
- [`trace.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.21/src/mini_agent/trace.py)：`build_trace()`、完整性检查、因果边和 `render_trace()`；
- [`__main__.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.21/src/mini_agent/__main__.py)：交互式 `/trace` 入口；
- [`tests/test_trace_replay_v021.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.21/tests/test_trace_replay_v021.py)：回放、损坏快照、安全边界和真实 registry 工具链覆盖。
