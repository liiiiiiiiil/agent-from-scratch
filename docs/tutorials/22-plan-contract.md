# 第 22 课：Plan Contract——把 Todo 变成不可变的执行合同（v0.22）

上一课：[任务轨迹回放](21-trace-replay.md) · [教程总览](README.md) · 下一课：[先调查，再交付计划](23-plan-mode-handoff.md)

> 代码快照：`v0.22` · 相邻差异：`v0.21..v0.22` · 命令环境：Bash/zsh

> 运行要求：Python 3.10+；核心运行时只使用标准库。

## 本课目标

上一课把执行尝试、失败、恢复和验证事实按 generation 保存下来，但计划本身仍然是一个可以整体覆盖的 Todo 列表。本课结束后，读者应能解释三个问题：为什么“改计划结构”和“推进步骤状态”不能共用一个写入口；为什么历史 revision 不能被覆盖；以及为什么步骤完成仍然不等于验证通过。

本版把复杂任务的计划表示为 Plan Contract。它是一份由模型提交、由 Runtime 校验结构的执行合同：合同说明目标、限制、验收标准、步骤和依赖，但不替模型判断业务上哪份计划最聪明，也不把计划状态当成环境事实。

## 前置条件

需要基础 Python、Bash/zsh 和 Git 知识。先查看上一课的代码，再切换到本课快照：

```bash
git checkout v0.21
git checkout v0.22
git diff --stat v0.21..v0.22
```

第一条命令让你能对照上一版；第二条命令进入本课代码；最后一条命令用于观察本版改动规模。当前工作区如果还没有用户创建的 `v0.22` tag，可在 tag 建立后再运行这组对照命令。

## 新增与改动文件

本版的主线只有一条：模型写入计划协议，State 保存不可变事实，Context 每轮重新生成执行视图。

| 文件 | 作用 |
| --- | --- |
| `src/mini_agent/state.py` | 定义冻结的 `PlanStep`、`PlanRevision`、`PlanProgressEvent`、`PlanningState`，并在一把 State 锁内完成校验和提交。 |
| `src/mini_agent/tools/plan.py` | 暴露 `commit_plan` 和 `update_plan_progress` 两个 state-bound 工具。 |
| `src/mini_agent/tools/__init__.py` | 注册新的计划工具，不再注册模型侧 `update_todo`。 |
| `src/mini_agent/context.py` | 渲染 active revision、当前步骤、可开始步骤、阻塞依赖和数量摘要。 |
| `src/mini_agent/prompt.py` | 告诉模型何时提交完整计划、何时只推进步骤状态。 |
| `src/mini_agent/agent.py` | 让同一回合的计划写入按模型调用顺序串行执行。 |

下面的命令只列出差异统计；它的用途是帮助读者把这张表和真实变更对应起来，而不是把文件列表当成设计结论。

```bash
git diff --stat v0.21..v0.22
```

## 上一版的问题：一份 Todo 做了两种工作

旧 Todo 的提交参数同时包含内容和状态。模型想表达“开始调查”时，要提交整个列表；模型想表达“调查方向变了”时，也要提交整个列表。Runtime 只能看到新列表，不能稳定区分这是旧计划的进度，还是一份新计划。

v0.21 已用 `TodoRevision` 留下每次提交的审计快照，但当前 Todo 仍会被整份替换；快照也无法区分“同一步前进了”和“这一步换了含义”。步骤没有稳定 ID、依赖和成功标准；验证通过只说明某条命令在某个 generation 中通过，并不能说明 Todo 中某个自然语言条目已经完成。

v0.22 因此采用两个协议：

- `commit_plan` 写入完整计划结构，成功后创建一个新的 `PlanRevision`。
- `update_plan_progress` 只写当前 revision 的一步状态，成功后追加一个 `PlanProgressEvent`。

简单任务仍然直接执行，不必为了一个计算或一次读取创建计划。

## 版本变更定位

从上一版的真实入口看，模型每次提交 Todo 都经由工具执行器写入 State；执行结果再回到下一轮上下文。图中的 `[旧]` 是 v0.21 已有节点，`[+]` 是本版新增，`[~]` 是本版改动，`[C]` 是主要消费者，`[B]` 是本版边界。

```text
v0.21 基线
[旧] 用户任务 -> agent_loop -> LLM 的 tool_calls -> ToolExecutor
                                                 -> update_todo -> AgentState.update_todos()
                                                                   -> 当前 Todo + TodoRevision
                           <- 每个 call 的 role=tool 结果 <- ToolExecutor
                           -> ContextManager(当前 State) -> 下一轮 LLM
[旧] 文件/命令执行 -> ExecutionAttempt / generation -> 独立 verification
                                                -> completion_reminder / 最终回复
```

本版保留同一条工具结果回灌链，但将 Todo 写入口拆成“提交结构”和“推进状态”。实际操作通常是先把步骤设为 `in_progress`，执行工作，再设为 `completed`；需要验证时仍单独运行 verification。

```text
v0.22 变更
[旧] 用户任务 -> agent_loop -> LLM 的 tool_calls
                      -> [~] 按模型顺序执行计划写入 -> ToolExecutor
                           ├-> [+] commit_plan -> AgentState.commit_plan()
                           │                    -> PlanRevision 1 / 后续完整 Revision 2
                           └-> [+] update_plan_progress -> AgentState.update_plan_progress()
                                                        -> PlanProgressEvent
                      <- [旧] 每个 call 对应的 role=tool 结果
                      -> [C] ContextManager 从 State 快照生成当前计划执行视图
                      -> [旧] 下一轮 LLM
[+] 校验或阶段拒绝 -> plan_rejected -> role=tool -> 下一轮 LLM
                                            [B] 不创建 FailureEvent 或 generation
[旧] 文件/命令执行 -> ExecutionAttempt / generation -> 独立 verification
                                                -> [~] 计划步骤 + 验证共同决定能否结束
[B] 简单任务沿 Direct Path；计划写入本身不算环境变化或验证证据
```

`commit_plan` 成功后的新 revision 和 `update_plan_progress` 成功后的事件都保存在 State；ContextManager 是它们的主要消费者，每轮只取当前执行所需的视图。拒绝路径仍为每个 call 回灌工具结果，让模型能修正输入，但不会把计划校验错误记作执行失败。这个差别也是图中保留旧执行与验证链的原因。

## 关键流程

初次提交不带 `parent_revision_id`。Runtime 把所有步骤设为 `pending`，并将 `planning_state.phase` 从 `direct` 变为 `executing`。后续提交必须引用当前 active revision；旧 revision 或另一个分支都不能成为 parent。

一个最小的 `commit_plan` 输入如下。注意输入没有 `status`：状态由 Runtime 通过进度事件管理。

```json
{
  "goal": "完成目标",
  "constraints": [],
  "success_criteria": ["最终检查通过"],
  "steps": [
    {
      "step_id": "inspect",
      "content": "检查现有实现",
      "depends_on": [],
      "success_criteria": ["确认调用链和边界"],
      "replaces": []
    }
  ],
  "reason": "先固定调查和验收范围"
}
```

Runtime 会先校验全部输入，再一次性提交。步骤 ID 必须稳定且唯一；依赖必须指向同一 revision 的步骤，不能自依赖或成环；文本和数量有固定上限。任何失败都不能只提交半份 revision。

## 实现拆解

### 1. Revision 保存结构，Event 保存变化

`PlanRevision` 是冻结 dataclass，保存一次完整计划；`PlanProgressEvent` 也是冻结 dataclass，保存一次状态转换。旧 revision 不会因为后续进度更新而改变：当前 active plan 是把 revision 的初始状态和该 revision 的 events 重新推导出来的视图。

```python
@dataclass(frozen=True)
class PlanProgressEvent:
    progress_id: int
    revision_id: int
    generation_id: int
    step_id: str
    from_status: str
    to_status: str
    reason: str
```

这样，`pending -> in_progress -> completed` 的每一步都可追溯；跳级、回退、重复完成、未满足依赖和同时有两个进行中步骤都会被拒绝。

### 2. 修订继承状态，但不复用旧含义

结构修订提交的是一份完整新计划。保留的步骤 ID 继承父 revision 推导出的最新状态；新步骤或 replacement 步骤从 `pending` 开始。一个已经从计划中移除的 ID 不能重新出现，同一个 ID 的 `content` 和步骤级 `success_criteria` 也不能改变。需要改变步骤含义时，使用新 ID，并在 `replaces` 中引用父 revision 中被移除的步骤。

`parent_revision_id`、结构校验和 revision 编号在同一把锁内完成，因此两个并发提交不会产生同号 revision、分叉 active 指针或半份计划。

### 3. 计划工具的错误不会伪装成执行失败

计划参数错误或计划不变量错误会回灌一个 `plan_rejected` tool result。它不会创建 `FailureEvent`，不会进入 `diagnosis_required`，也不会推进 generation。真正的 handler 异常仍沿用工具层的异常边界，才会成为执行失败。

两个计划工具默认 `ALLOW` 且 `effect_class=none`。因此它们不会清除当前 verification evidence，也不能通过 `recover.retry`、`recover.adjust` 或 rollback 间接执行。若当前阶段要求独立 verification，计划工具会被拒绝，但不会绕过该阶段。

同一 assistant 回合中如果出现多个计划写入，Runtime 仍按模型调用顺序执行并为每个 call 回灌一个结果。它们不被伪装成 possible effect；计划写入不是环境变化。

### 4. Structured State 只展示执行所需的有界视图

完整的 `plan_revisions` 和 `plan_progress_history` 保存在 State。每轮上下文只重新生成一个有界视图：当前 revision 的目标、限制和任务级成功标准；当前 `in_progress` 步骤的内容、依赖和步骤级标准的有界摘要；最多五个可立即开始的步骤；最多十个被依赖阻塞的步骤；以及 completed、pending、in-progress 和省略数量。依赖或标准很长时，当前步骤行也可能被截断；需要核对完整合同时仍应读取 State 快照。

这意味着压缩历史消息不会删除计划合同。Context 降级时，计划关键行仍优先于普通工具摘要，并且最终长度继续受现有 6000 字符上限约束。

### 5. 完成条件仍由计划和验证共同决定

有 active plan 时，所有活动步骤都必须是 `completed`，并且最近一次可能修改后的独立 verification 仍需通过。步骤的 `success_criteria` 是计划要求，不是 `VerificationEvidence`；标记 completed 不会替代测试或检查。

下面用标准库命令走一遍这条链，观察计划工具的返回、generation 和完成提醒。示例在 Bash/zsh 的本课代码目录运行，不写文件；局部权限策略只允许计划工具和 `python` shell 命令。计划工具由 handler 原子写入 State；`record_execution_result()` 记录实际执行和验证的工具事实，模拟 agent loop 的提交步骤。

```bash
PYTHONPATH=src python - <<'PY'
import json
from mini_agent.permission import ALLOW, PermissionGate, PermissionPolicy
from mini_agent.state import AgentState
from mini_agent.tools import create_registry
from mini_agent.tools.base import ToolExecutor

state = AgentState()
state.begin_task("检查一个计算命令")
policy = PermissionPolicy({
    "commit_plan": ALLOW, "update_plan_progress": ALLOW,
    "run_shell": {"python *": ALLOW},
})
executor = ToolExecutor(create_registry(state), PermissionGate(policy))

def call(name, arguments):
    result = executor.execute_result(name, arguments, state)
    state.record_execution_result(result)
    return result

plan = {
    "goal": "检查一个计算命令", "constraints": [],
    "success_criteria": ["命令及独立验证通过"],
    "steps": [{"step_id": "check", "content": "运行计算命令",
               "depends_on": [], "success_criteria": ["输出 2"], "replaces": []}],
    "reason": "先记录任务与步骤",
}
print("提交:", json.loads(call("commit_plan", plan).output)["status"], state.current_generation_id)
for status in ("in_progress", "completed"):
    if status == "completed":
        result = call("run_shell", {"command": 'python -c "print(1 + 1)"', "purpose": "execution"})
        print("执行:", result.outcome, state.current_generation_id)
    result = call("update_plan_progress", {
        "revision_id": 1, "step_id": "check", "status": status, "reason": status,
    })
    print("进度:", json.loads(result.output)["to_status"])
print("仍需验证:", state.completion_reminder()["verification_required"])
call("run_shell", {"command": 'python -c "print(1 + 1)"', "purpose": "verification"})
print("验证:", state.snapshot()["verification_evidence"][-1]["outcome"])
print("完成提醒:", state.completion_reminder())
PY
```

预期依次看到 `提交: committed 0`、`进度: in_progress`、`执行: succeeded 1`、`进度: completed`、`仍需验证: True`、`验证: passed` 和 `完成提醒: None`。提交计划和两次进度更新都没有推进 generation；执行命令推进到 generation 1。即使步骤已经 completed，提醒仍要求独立验证；验证通过后提醒才消失。这里的 `None` 仅表示完成条件已满足，不是在替完整的 agent loop 宣告任务结束。

无 active plan 时，Direct Path 保持上一版的完成条件。完成提醒的 `progress_marker` 使用 active revision 和推导后的步骤状态；重复或被拒绝的结构提交不会制造假进展，真正的步骤事件才会允许新的提醒机会。

## 为什么这样设计

把结构和进度拆开，是为了让 Runtime 能够回答“这次是改变方案，还是继续执行原方案”。不可变 revision 保留了旧方案，事件则以很小的记录表达步骤变化；这比每次复制 Todo 列表更容易审计，也避免进度更新重写计划含义。

本版选择由模型提交完整新结构，而不是让 Runtime 自动猜测如何补步骤或重排依赖。好处是协议简单、校验确定；代价是模型必须在修订时重复提交完整计划。运行时只检查格式、引用和状态不变量，不评价业务计划是否充分。

计划没有被当作执行事实。可能副作用仍由现有 generation 规则管理，验证仍由独立 verification 产生，失败仍进入既有 Repair Loop。这样“计划说完成”和“环境已经正确”不会混为一谈。

## 设计边界

- 不要求简单任务创建计划，也不根据任务长度自动猜测复杂度。
- 本版没有单独的 plan mode、Explore gate、用户审批或 replan trigger。
- 没有通用停滞检测和计划修订预算；`PlanningState` 中的相关形状保持冻结，但本版不使用这些能力。
- `/trace` 继续只读回放 v0.21 的 generation、执行、失败、恢复和验证事实；完整 Plan 因果链不在本版 Trace 中展示。
- Plan Contract 不会放宽项目指令、PermissionGate、Repair Loop 或 verification 规则。

## 本版特性、下一课与代码索引

本课新增的可观察能力是：完整不可变 Plan Contract、独立 progress event、只读 Todo 兼容投影，以及包含计划执行视图的 Structured State。完成本版后，下一阶段会继续讨论更严格的规划边界；本课不提前实现那些运行时行为。

固定到本课 tag 的实现入口：

- [`state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.22/src/mini_agent/state.py)：冻结模型、原子校验、revision 与 progress 推导。
- [`plan.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.22/src/mini_agent/tools/plan.py)：两个模型可见的 state-bound 工具。
- [`context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.22/src/mini_agent/context.py)：有界 Structured State 计划视图。
- [`agent.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.22/src/mini_agent/agent.py)：回合内计划写入的顺序保证。

回到[教程索引](README.md)，再用[操作手册](../operation/manual.md)查看当前版本的运行边界。
