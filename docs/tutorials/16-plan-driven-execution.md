# 第 16 课：计划驱动执行（Plan-driven Execution，v0.16）

上一课：[任务清单与状态](15-task-state.md) · [教程总览](README.md) · 下一课：[失败事实模型](17-failure-model.md)

> 代码快照：`v0.16` · 相邻差异：`v0.15..v0.16` · 命令环境：Bash/zsh

> 运行要求：Python 3.10+；运行时只使用标准库。

## 本课目标

第 15 课让 agent 能保存 Todo（任务清单），但 Todo 只表示模型的计划，不能证明任务真的完成。本课为复杂任务补上完成闭环：模型建立计划、执行工具、根据结果调整计划，最后用独立命令留下验证证据。

读完后，你应能解释下列规则：

```text
Plan -> Execute -> Observe -> Verify -> 最终回复
                         └─ 发现问题时 Replan -> Execute
```

这里的 `Replan`（重排计划）是条件分支，不是每一轮都必须经过的阶段。观察结果已经足够明确时可以直接验证；发现失败、遗漏或新工作时，模型才调整 Todo 并继续执行。

- 为什么 Todo 全部标为 `completed` 仍不一定能结束；
- 为什么一次可能改动环境的操作会让旧验证失效；
- 为什么最终回复前最多只提醒一次，第二次仍不满足条件时状态为 `blocked`。

本课的学习主线是：**计划记录意图，工具结果记录事实，当前代次的验证记录才是完成证据。**

## 上一版的问题

v0.15 已经把 Todo 放进独立的 `AgentState`，模型可以持续更新计划。但模型自己把“运行测试”改为完成，不代表测试实际运行过；即使测试刚通过，随后一次写文件也可能让结果过期。

因此，单靠消息历史或 Todo 状态无法回答“现在这份工作区是否已验证”。v0.16 不试图自动替模型制定计划或挑选测试命令，而是让运行时保存工具产生的事实，并在模型准备结束时检查完成条件。

## 前置条件与版本切换

建议先阅读第 15 课，了解 `AgentState`、Todo 的完整替换规则，以及状态为何不直接写入消息历史。以下命令适用于 Bash/zsh：

```bash
git checkout v0.15
git diff --stat v0.15..v0.16
git diff v0.15..v0.16 -- src/mini_agent/state.py src/mini_agent/agent.py src/mini_agent/context.py src/mini_agent/tools/shell.py src/mini_agent/prompt.py
git checkout v0.16
```

`git diff --stat` 先给出本版范围；第二条 diff 可直接定位本课的主要调用链。阅读和运行都应在 `v0.16` 快照中进行。

## 新增与改动文件

| 文件 | 变化 | 作用 |
|---|---|---|
| `src/mini_agent/state.py` | 增加验证证据、代次和完成条件 | 将计划和实际执行结果分开保存 |
| `src/mini_agent/tools/shell.py` | `run_shell` 增加 `purpose` | 区分执行命令与验证命令 |
| `src/mini_agent/agent.py` | 最终文本前检查完成条件 | 一次提醒后正常收口或标记阻塞 |
| `src/mini_agent/context.py` | 渲染验证状态与 Runtime Notice | 把最新事实和提醒交给下一轮模型 |
| `src/mini_agent/prompt.py` | 增加计划—验证规则 | 告知模型复杂任务的工作协议 |
| `src/mini_agent/__main__.py` | 每项任务先调用 `begin_task()` | 明确任务边界，重置本任务的运行事实 |

## 版本变更定位

图例：`[旧]` v0.15 已有，`[+]` v0.16 新增，`[~]` v0.16 修改，`[C]` 主要消费者，`[B]` 本版边界。

v0.15 的入口已经能执行工具、把结果回灌模型，并让 Todo 跟随会话存在；模型给出不带工具调用的文本后，CLI 会直接将任务收口。

```text
v0.15 基线：

[旧] CLI run_task
  -> [旧] state.task/status + history
  -> [旧] agent_loop
       -> [旧] call_llm
       -> [旧] ToolExecutor.execute
            -> [旧] PermissionGate -> tool handler
            -> [旧] state.record_tool
       -> [旧] 全部 role=tool 结果回灌
       -> [旧] 下一轮 LLM
  -> [旧] 无 tool_calls 的最终文本
  -> [旧] CLI 将 running 收口为 done
```

v0.16 在工具结果与最终收口之间插入“验证事实”和“完成检查”。正常路径是当前代次验证通过后结束；重要的降级路径是模型两次试图过早结束后标记为 `blocked`。重规划由模型根据观察或验证结果决定，运行时不强制阶段顺序。

```text
v0.16 变更：

[+] CLI run_task -> [C] state.begin_task(task)
                     └-> 清空 Todo、错误、文件和旧验证证据
  -> [旧] agent_loop -> [旧] call_llm
       -> [+] update_todo -> [C] state.update_todos()        (计划意图，可按需更新)
       -> [旧] ToolExecutor.execute -> handler
            -> [~] state.record_tool()                       (执行事实)
                 ├-> write_file/edit_file 或 run_shell(execution)
                 │    -> [+] generation 加一，旧证据失效
                 └-> run_shell(verification)
                      -> [+] VerificationEvidence
                      -> [C] 当前 generation 的通过/失败状态
       -> [旧] role=tool 全量回灌 -> 下一轮 LLM
       -> [C] ContextManager.prepare_messages()
            -> [~] Structured State
            -> [+] Runtime Notice（只供下一次请求）
  -> [~] 无 tool_calls 的最终文本
       ├-> 条件满足 -> CLI 收口为 done
       ├-> 首次有缺口 -> Runtime Notice -> 再请求一次 LLM
       └-> 仍有缺口 -> status=blocked                         (降级路径)

[B] 不自动生成 Todo、选择验证命令、重试、回滚或持久化计划。
```

## 核心概念与数据结构

### 1. Todo 不是完成证据

问题是：模型可以更新 Todo，却无法仅从这个动作证明任何文件或命令真的发生。

直观地说，Todo 像待办纸条；`record_tool()` 才像执行日志。v0.16 继续让 `update_todo` 只更新计划，不把它混入工具历史、错误或改动文件列表：

```python
# src/mini_agent/state.py（v0.16）
def record_tool(self, name, args, ok, brief):
    # Todo 是任务意图，不是执行事实。
    if name == "update_todo":
        return
    ...
```

因此，模型调用 `update_todo` 后仍须调用真实工具。工具执行器通过 `on_result=state.record_tool` 把每个工具结果交给状态；agent loop 不需要知道每种工具如何改变状态。工具 handler 的异常仍在工具边界转为可回灌结果，LLM 与 CLI 顶层异常不由 loop 吞掉。

### 2. 用 generation 让旧验证自动过期

问题是：一次验证成功后，后续修改可能破坏它。若只保存“测试曾通过”，就会把旧结果误当成当前工作区的结果。

`generation`（代次）是“可能改动环境”后的计数。成功的 `write_file`、`edit_file`，以及实际进入 handler 的 `run_shell(purpose="execution")` 都会调用 `_invalidate_verification()`：

```python
# src/mini_agent/state.py（v0.16）
def _invalidate_verification(self) -> None:
    self._verification_generation += 1
    self.verification_evidence.clear()
    self._last_verified_generation = -1
    self._verification_required = True

if name == "run_shell" and args_copy.get("purpose", "execution") == "execution":
    if "权限拒绝" not in brief:
        self._invalidate_verification()
```

运行时无法可靠判断任意 shell 命令是否只读，所以即使 execution 命令非零退出，也保守地使旧证据失效。权限拒绝意味着 handler 没有运行，因而不使证据失效。这样，“先测试、再改文件、直接回复”一定会被视为尚未验证。

`begin_task(task)` 则在每个 CLI 任务开始时清空 Todo、工具历史、错误和证据，重置为 `running`，并推进 generation。它保留会话 `history`，所以命令行首条任务结束后仍可在交互循环中追问。

### 3. `verification` 是可检查的独立证据

问题是：普通 shell 命令与“用来确认最终结果的命令”语义不同。v0.16 在工具 schema 中增加 `purpose`，默认是 `execution`，从而保持旧调用兼容：

```python
# src/mini_agent/tools/shell.py（v0.16）
def run_shell(command: str, purpose: str = "execution"):
    ...

"purpose": {
    "type": "string",
    "enum": ["execution", "verification"],
    "default": "execution",
}
```

当 `purpose="verification"` 时，`record_tool()` 从工具结果读取 `[exit=N]`。`ok` 为真、没有超时且退出码为 0，才生成当前 generation 的通过证据；其他结果都记录为失败并继续要求验证：

```python
passed = bool(ok and not timeout and code == 0)
evidence = VerificationEvidence(
    command=str(args_copy.get("command", "")),
    outcome="passed" if passed else "failed",
    exit_code=code,
    output=text,
)
```

`VerificationEvidence` 保存命令、`passed`/`failed`、退出码和截断后的输出。shell 工具超时返回 `[timeout]`；正常结束无论退出码是否为 0 都带 `[exit=N]`。证据只说明该进程成功退出，并不保证测试覆盖充分、验证命令正确或业务一定正确。

### 4. 完成提醒是一次纠正，不是自动重试器

问题是：模型可能在 Todo 未完成或验证仍需要时发出最终文本。完全相信文本会过早结束；无限阻止又会让状态忘记更新的模型永远循环。

状态把缺口集中成一个检查：

```python
# src/mini_agent/state.py（v0.16）
def completion_reminder(self) -> dict[str, object] | None:
    missing = [t.content for t in self.todos if t.status != "completed"]
    needs_verify = self._verification_required
    if not missing and not needs_verify:
        return None
    return {
        "unfinished_todos": missing,
        "verification_required": needs_verify,
        "message": "任务尚未满足完成条件，请继续执行并验证。",
    }
```

agent loop 首次看到缺口时调用 `set_runtime_notice()`，继续请求模型；第二次仍有缺口时设 `status="blocked"` 并返回该文本。这个提示不写入 `history`，只在下一次 `prepare_messages()` 构建的请求视图中出现；即使该次构建触发上下文压缩，提示也会在最终视图完成后才被消费。

## 为什么这样设计

可以把“验证通过”直接塞进 Todo 状态，或者由 Python 自动从命令文本猜测哪些命令是测试。v0.16 没有这样做：前者会再次把模型意图和运行事实混在一起，后者既容易误判，也会替用户选择项目特定的验证方式。

当前设计把责任分开：模型维护简短、可调整的计划并显式标注验证命令；运行时只做保守记录和完成检查。收益是证据与最近一次潜在环境变化绑定，且在裁剪、压缩消息历史后仍由 `AgentState` 提供。代价是简单但实际只读的 execution shell 也会要求再次验证，且系统不能判断验证是否足够全面。

一次提醒是另一项取舍。它给模型纠正遗漏的机会，但不把 Todo 的小错误变成无限循环。它刻意不是重试策略，更不会自行补建 Todo、执行测试或回滚修改。

## 设计边界

- 复杂任务由 prompt 建议先建 Todo；简单问答、一次读取或简单计算可直接完成，运行时不强制生成计划。
- `run_shell` 的 `purpose` 只改变状态记录方式，不改变权限。权限仍由 `PermissionGate` 决定。
- 验证失败、超时或没有退出码都会留下失败证据，并要求模型根据结果调整 Todo；运行时不自动选择下一条命令。
- 只有“所有 Todo 已完成”且没有待验证的潜在变化，最终文本才可直接收口。没有修改的只读任务通常不需要验证。
- 达到 `MAX_ITERATIONS`（默认 50）时 loop 返回“达到最大迭代次数”并设状态为 `failed`；CLI 正常返回且仍为 `running` 时才设为 `done`。
- 状态只存活于当前进程；流式输出过的草稿不会被撤回。

## 关键流程

下面是一项“修改并检查”的正常路径，以及验证失败时的回路。箭头表示当前 v0.16 的调用或数据流。

```text
CLI 收到任务
  -> state.begin_task()
  -> LLM 调用 update_todo：调查 / 修改 / 验证
  -> LLM 调用读写工具或 run_shell(execution)
  -> ToolExecutor 的 on_result -> state.record_tool()
       -> generation 变化，verification_required=true
  -> ContextManager 将最新 Structured State 注入下一轮
  -> LLM 按需更新 Todo，再调用 run_shell(verification)
       ├-> [exit=0]：当前 generation 的验证通过
       │    -> Todo 全完成 -> 最终文本 -> done
       └-> 非零 / timeout：失败证据
            -> Replan：更新 Todo
            -> Execute：修复
            -> 再次 verification
```

如果观察阶段已经发现计划需要变化，也可以在验证前先 Replan；这只是模型选择的执行路径，不是运行时硬编码的状态转换。

若模型在验证前给出最终文本，读者会先看到该文本已流式输出，然后下一轮请求收到 `[Runtime Notice]`。这证明提醒是请求视图中的纠正信息，而不是对已打印内容的撤回；第二次仍未满足条件才收口为 `blocked`。

## 实现拆解

`ContextManager._render_state()` 每轮基于 `state.snapshot()` 重新生成 Structured State，其中包括 `Verification` 和 `Verification required: true`。因此状态不是旧对话的一段文本：上下文被裁剪或压缩时，最新验证事实仍会再次注入。

prompt 负责告诉模型复杂任务采用 `Plan -> Execute -> Observe -> Verify`，并在观察或验证发现问题时调整 Todo；Python 不实现复杂任务分类器，也不强制 Replan 的调用时机。Todo 工具每次提交完整列表，最多一个条目可为 `in_progress`，这些 v0.15 规则继续有效。

最终收口的调用顺序也值得注意：带 `tool_calls` 的 assistant 消息必须先执行并按原顺序回灌全部 `role=tool` 结果，下一轮才可能看到更新后的 State。只有没有 `tool_calls` 的消息才进入 `completion_reminder()` 检查，所以验证结果不会在同一轮被跳过。

完整实现可在固定快照中阅读：[state.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.16/src/mini_agent/state.py)、[agent.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.16/src/mini_agent/agent.py)、[context.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.16/src/mini_agent/context.py)、[shell.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.16/src/mini_agent/tools/shell.py)。

## 运行与观察

配置本地 LLM 后，可在 Bash/zsh 中用一项需要修改和检查的真实任务启动程序：

```bash
PYTHONPATH=src python -m mini_agent "修改实现并运行检查，维护 Todo"
```

程序会把这段参数作为“命令行首条任务”，处理后仍进入交互循环。观察模型是否先更新 Todo；任何写入或 execution shell 后，Structured State 应出现 `Verification required: true`。只有最后一次 `run_shell(purpose="verification")` 返回 `[exit=0]` 后，这个字段才消失；这正是验证证据绑定当前 generation 的表现。

## 本版特性、下一课与代码索引

v0.16 新增了 generation 绑定的验证证据、一次性完成提醒，以及 `done`、`blocked`、`failed` 三种任务收口状态。它仍是单 agent 的轻量协议，不会自动规划或自动恢复失败。

下一课：[失败事实模型](17-failure-model.md) 会进一步区分“工具没有运行”“可能产生副作用”“验证失败”等不同失败事实。

- [src/mini_agent/state.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.16/src/mini_agent/state.py)
- [src/mini_agent/agent.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.16/src/mini_agent/agent.py)
- [src/mini_agent/context.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.16/src/mini_agent/context.py)
- [src/mini_agent/tools/shell.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.16/src/mini_agent/tools/shell.py)
- [src/mini_agent/prompt.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.16/src/mini_agent/prompt.py)
- [src/mini_agent/__main__.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.16/src/mini_agent/__main__.py)

## v0.16.1 补丁：完成提醒按进展状态重开

> 补丁快照：`v0.16.1` · 相邻差异：`v0.16..v0.16.1` · 本补丁不新增独立课程

### 为什么需要这个补丁

v0.16 用一个全局 `reminded` 布尔值控制完成提醒，整个任务最多提醒一次。这能阻止模型无限输出未完成总结，却也把正常的阶段性汇报误当成一次不可恢复的结束尝试：模型汇报调查结果、收到提醒并继续调用只读工具后，即使已经获得新事实，下一次阶段性汇报仍会直接进入 `blocked`。

v0.16.1 把规则收窄为“每个进展状态最多提醒一次”。它修复的是完成协议，不引入 `recover`、`FailureEvent`、planner 或新的控制工具；`MAX_ITERATIONS=50` 仍是整个 loop 的全局上限。

### 用已有完成事实构造进展标记

`AgentState.completion_reminder()` 在原有完成缺口之外返回内部字段 `progress_marker`：

```python
progress_marker = (
    tuple((todo.content, todo.status) for todo in self.todos),
    len(self.tool_history),
    len(self.verification_evidence),
    self._verification_generation,
    needs_verify,
)
```

它由五类事实组成：完整 Todo 的 `(content, status)`、已记录的非 Todo 工具结果数量、验证证据数量、当前验证 generation，以及 `verification_required` 状态。这里没有新增持久化的提醒计数器；标记完全从现有状态派生。

因此，相同内容和状态的 Todo 被重复提交时标记不变，不能借此无限刷新提醒。Todo 从 `pending` 或 `in_progress` 实质推进、读写或 shell 工具产生结果、验证证据增加、generation 改变，都会形成新标记。工具失败也属于新的观察事实，允许模型基于结果重新判断一次。

### agent loop 如何判定

`agent_loop()` 用 `reminded_progress_marker` 代替全局 `reminded`：

- 首次看到某个完成缺口时，保存当前标记并注入 Runtime Notice。
- 工具、Todo 或验证事实让标记发生变化后，可以针对新状态再次提醒。
- 标记没有变化而模型再次输出无工具文本时，任务才进入 `blocked`。
- 自定义或旧式 State 没有提供 `progress_marker` 时，仍沿用“整个任务只提醒一次”的兼容行为。

Runtime Notice 也改成可执行指令：任务未完成时，模型的下一回复必须携带更新 Todo、继续调查/操作或执行验证的工具调用，不能只口头描述“接下来执行”；确实无法继续时才说明具体阻塞原因。已经流式输出到终端的阶段性文本仍然保留。

典型的调查汇报流程变为：

```text
阶段性汇报
  -> Runtime Notice
  -> 调用只读工具（progress_marker 改变）
  -> 再次阶段性汇报
  -> 针对新标记再次收到 Runtime Notice
  -> 更新 Todo / 继续执行 / 验证
  -> 满足完成条件，或在下一种进展状态继续循环
```

无进展边界仍然明确：

```text
Runtime Notice
  -> 没有工具、Todo 或验证进展
  -> 再次只输出文本
  -> blocked
```

### 与 v0.16 保持不变的边界

正常完成条件没有改变：所有 Todo 都必须完成，并且最近一次可能改变环境的操作必须拥有当前 generation 的通过验证。带 `tool_calls` 的 assistant 消息仍须为每个调用回灌对应的 `role=tool` 结果；简单任务和无 Todo 任务仍可直接完成；50 轮上限保持不变。

Runtime Notice 仍只进入下一次目标请求。即使该次请求触发 trimming 或 compaction，它也在最终请求视图构建完成后才被消费，不会重复注入，也不会撤回已经打印的阶段性文本。

### 测试与验收

v0.16.1 使用脚本化 mock LLM 响应验证完成协议，不依赖网络。先运行补丁直接涉及的 State、loop 和 Context 测试，再运行完整测试和教程结构检查：

```bash
PYTHONPATH=src python -m pytest -q tests/test_state.py tests/test_loop.py tests/test_context.py
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python scripts/check_tutorials.py docs/tutorials/16-plan-driven-execution.md
```

验收覆盖以下边界：

- 首次未完成回复触发提醒；没有 Todo、工具或验证进展而再次输出文本时进入 `blocked`。
- 调查汇报后调用只读工具会改变标记；再次汇报可获得新的提醒，并能继续执行直至完成。
- Todo 从 `pending`/`in_progress` 实质推进会改变标记；重复提交完全相同的 Todo 不会。
- 工具失败也会形成新的观察事实并允许重新判断一次；之后持续只输出文本仍会阻塞。
- Todo 全部完成且当前 generation 有通过的验证证据时正常返回 `done`。
- 简单任务、无 Todo 任务、tool-call/result 一一对应、50 轮上限与 context compaction 行为不回归。
- Runtime Notice 在 trimming 或 compaction 后仍只注入目标请求一次。

补丁实现与回归测试可在固定快照中阅读：[state.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.16.1/src/mini_agent/state.py)、[agent.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.16.1/src/mini_agent/agent.py)、[prompt.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.16.1/src/mini_agent/prompt.py)、[test_state.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.16.1/tests/test_state.py)、[test_loop.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.16.1/tests/test_loop.py) 和 [test_context.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.16.1/tests/test_context.py)。
