# 第 25 课：计划轨迹回放与验收

上一课：[证据驱动重规划与停滞收口](24-replanning-policy.md) · [教程总览](README.md) · 下一课：后续版本课程（待规划）

代码快照：`v0.25` · 相邻差异：`v0.24..v0.25`

本课示例命令适用于 Bash/zsh。`v0.25` tag 由仓库维护者在交付前手动创建；在 tag 尚未创建时，可以留在当前分支阅读和运行实现。

## 本课目标

上一课已经保存了计划 revision、重规划 trigger、执行失败、恢复动作和验证历史，但这些记录分散在不同列表中。只看 generation 能知道某一代发生了什么，却无法回答同一代提交两个 revision 后，一次调查属于旧方案还是新方案。

本课结束后，读者应能解释三个维度的分工：`generation` 表示环境经过一次副作用或恢复后的验证代次，`revision` 表示计划结构版本，顺序事件把两者以及 trigger、用户决定和事实记录连接起来。Trace 仍然只读，它展示证据链的完整性，不重新执行工具，也不替模型判断计划是否聪明。

## 前置条件

需要基础 Python、Bash/zsh，以及第 24 课中的 Plan Contract、Repair Loop 和重规划 trigger。先切换到本课代码并查看相邻差异：

```bash
git checkout v0.25
git diff --stat v0.24..v0.25
```

如果本地还没有 `v0.25` tag，可继续在当前开发分支运行下面的 API。固定源码链接和教程事实检查以维护者手动创建 tag 后的快照为准。

## 新增与改动文件

| 文件 | 本课作用 |
| --- | --- |
| `src/mini_agent/state.py` | 增加任务内连续的 `TraceEvent`，在事实成功写入的同一把 State 锁中记录顺序、generation、revision、阶段变化和停滞摘要。 |
| `src/mini_agent/trace.py` | 增加 revision 查询、计划时间线、计划归属、结构校验、计划因果边和结论依据，同时保留 v0.21 generation 查询。 |
| `src/mini_agent/__main__.py` | 增加只读命令 `/trace revision <revision_id>`。 |
| `tests/test_plan_trace_v025.py` | 覆盖失败—重规划—新 generation 验证链、用户交接、停滞恢复、查询互斥和损坏快照。 |
| `docs/operation/manual.md`、`README.md`、`README_EN.md`、`CHANGELOG.md`、`pyproject.toml` | 同步操作入口、版本导航和发布元数据。 |

## 关键流程

下面的流程保留第 24 课已有的执行和 Repair Loop 节点，只在事实写入处插入顺序事件。事件保存的是已有记录的类型和 ID，因此它不会复制工具原始输出：

```text
[旧] attempt / failure / verification 事实写入
       │
       ├── [+] TraceEvent(sequence_id, generation_id, revision_id, record pointer)
       │
[旧] failure -> request_replan -> exploring -> commit_plan
       │                                  │
       │                                  └── [+] parent / trigger / diff
       │
[旧] possible effect / recovery -> 新 generation -> 独立 verification
       │                                  │
       └── [+] revision -> attempt / recovery / verification 因果边
```

以“旧计划失败、引用失败重规划、新计划验证通过”为例，事件顺序会表达：旧 revision 的失败先发生；trigger 引用这个失败；新 revision 提交时引用旧 revision 和 trigger；随后新 revision 的执行事实和新 generation 验证通过。后一个通过证据不会被写回旧 revision，也不会被解释成旧计划已经正确。

`generation` 和 `revision` 的关系可以这样看：

```text
计划 revision 1 ──失败 attempt / failure──> trigger 1 ──> 计划 revision 2
       │                                         │                 │
       └── generation 0                          └── parent         └── generation 1 的执行与验证
```

## 上一版的问题

第 24 课的 `snapshot()` 已分别提供 `plan_revisions`、`plan_progress_history`、`replan_triggers`、`user_plan_decisions`、attempt、failure、recovery 和 verification history。Trace 可以验证每张表内部的引用，却不能可靠知道记录的先后。按数组位置会把实现细节误当成发生顺序，按 generation 又无法区分同一代中的多个 revision。

因此本课只增加连接信息。状态转换、模型工具和规划策略保持不变；事件也不参与完成判定、预算计算或验证证据计算。旧 snapshot 没有计划数据时仍按 v0.21 结果回放；有计划记录但没有新事件时，报告保留能直接验证的结构，并把跨记录顺序和执行归属标为 `unresolved`。

## 核心概念：顺序事件

`TraceEvent` 是一个很小的指针记录。它的 `sequence_id` 在任务内连续递增，`record_type` 和 `record_id` 指向已有记录；verification 的 ID 是 `verification_history` 的索引。事件还保存发生时的 generation 和 revision，以及确实发生变化时的 planning / repair phase 前后值。

```python
snapshot = state.snapshot()
for event in snapshot["trace_events"]:
    print(event["sequence_id"], event["kind"], event["revision_id"])
```

这段代码读取公开快照，不触发回放副作用。读者应看到计划提交、步骤进度、执行结果、失败和验证事件按事实写入顺序排列；一次非法 `commit_plan` 不会多出事件，因为它没有成功写入 Plan Contract。

任务开始和 `/reset` 会清除旧事件并把序号重新从 1 开始。停滞告警即使后来因为新观察被当前状态清除，也会留在事件列表中；事件中的停滞字段只有类型、次数和短指纹，不包含工具输出。

## 核心概念：revision 查询与计划时间线

默认的 `build_trace(snapshot)` 继续返回全部 generation。`build_trace(snapshot, generation_id=1)` 仍只把 generation 1 作为主要展示范围。新增的 revision 查询使用仅限关键字参数，避免把两个稳定维度混在一个位置参数中：

```python
from mini_agent.trace import build_trace, render_trace

report = build_trace(snapshot, revision_id=2)
print(render_trace(report))
```

revision 视图包含目标、成功标准、parent、trigger、模型提交的 `reason`、Runtime 计算的结构差异、步骤进度、用户决定以及生效期间的调查、执行、失败、恢复和验证事实。`plan_timeline` 按 `sequence_id` 排列这些事件；若查询的 revision 来自重规划，旧 revision 中的失败或用户反馈作为 trigger 前因显示，但不会被归入新 revision 的执行期间。

两个查询不能同时指定，非法 ID 抛出 `TraceQueryError`。记录损坏属于报告完整性问题：报告仍返回能定位的记录，并把相应因果边标成 `UNRESOLVED`。

CLI 对应入口是：

```text
/trace
/trace <generation_id>
/trace revision <revision_id>
```

三个入口都在执行新任务前拦截。Trace 不调用 LLM、工具 handler 或 PermissionGate，也不追加对话历史，不修改 State、预算和 generation。

## 实现拆解

`state.py` 在已有事实成功写入的位置追加事件。计划提交先写入 `plan_committed`，若解决活动 trigger，再追加 `trigger_resolved`；步骤进度、用户决定、恢复动作、执行结果、failure 和 verification 各自指向对应记录。一次执行结果派生出的 attempt、failure 和 verification 在同一把锁内按写入顺序追加，Trace 再用 ID 建立因果边。

`trace.py` 先运行原有 generation 完整性检查，再处理计划记录。它检查 parent 是否存在且早于子 revision，步骤依赖是否存在且无环，`replaces` 是否只引用 parent 中移除的步骤，保存的 diff 是否等于两份快照重算结果。它还检查 progress 的 revision / step、生效区间和状态连续性，trigger 来源和唯一消费，以及 plan-only 决定是否指向当时待审批的当前 revision。

计划事实的归属只接受顺序事件提供的 revision。没有事件时，报告可以显示 revision 自身的结构、parent 和 trigger 引用，但不会用 generation 或数组位置猜测一次 attempt 属于哪个 revision。事件序号缺口、悬空引用、错误 generation / revision 和跨 generation 验证都会进入 `integrity.issues`。

报告的计划因果边包括：

```text
parent revision -> revision
failure / attempt / user decision -> trigger -> revision
revision -> progress / decision / attempt / recovery / verification
```

每条边都带 `resolved` 和 `status`。最终结论沿用 Runtime 保存的 `done`、`blocked` 或 `failed`，并附活动 revision 的步骤状态、当前 generation 的独立验证记录、`verification_required`、末次 failure、停滞摘要和终态摘要。Trace 只检查这些证据能否连起来，不重新决定终态。

## 为什么这样设计

把顺序单独保存，代价是 State 多了一份很小的 append-only 记录，但换来了跨列表的确定性归属。事件只存索引而不复制输出，既能让报告找到事实，也不会扩大敏感数据暴露面。把 generation 查询和 revision 查询分开，则让“环境验证代次”和“方案版本”各自保持清晰。

报告对损坏数据选择不猜测，是因为补全一个看似合理的归属会把不确定性隐藏起来。相应的限制是，手工构造的旧计划 snapshot 如果没有事件，只能得到部分可验证报告；需要完整回放时必须从带 `trace_events` 的运行时快照开始。后续通过验证的 generation 也不会自动证明旧计划正确，旧 failure 是否真正解决仍须由已有事实和当前 Runtime 结论共同支持。

## 设计边界

- Trace 只消费 `AgentState.snapshot()`，不访问私有原始参数、checkpoint 内容、LLM 或执行器。
- 事件不推进 generation，不改变预算，不产生 verification evidence，也不替代 plan / attempt / failure / recovery 的写入来源。
- `reason`、反馈、计划文本、路径和停滞指纹在 State 或报告边界内有界、脱敏；Trace 不重新读取工具输出判断“是否有进展”。
- 没有计划数据的旧 snapshot 保持 v0.21 兼容；有计划数据但缺事件时返回不完整报告，并保留 unresolved 边。

## 本版特性、下一课与代码索引

本版新增按顺序回放计划链、revision 查询、计划因果边、损坏快照完整性报告和 `/trace revision`。简单 Direct Path、旧 generation 查询、Repair Loop 和独立 verification 的边界继续有效。

下一课尚未规划。固定代码快照索引如下，链接指向本课交付时的 `v0.25` tag：

- [`state.py` 的 TraceEvent 与事实写入](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.25/src/mini_agent/state.py)
- [`trace.py` 的只读回放与完整性检查](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.25/src/mini_agent/trace.py)
- [`__main__.py` 的 Trace CLI 入口](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.25/src/mini_agent/__main__.py)
- [`test_plan_trace_v025.py` 的验收链路](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.25/tests/test_plan_trace_v025.py)
