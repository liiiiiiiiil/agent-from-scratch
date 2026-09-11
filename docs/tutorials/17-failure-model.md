# 第 17 课：失败模型（v0.17）

上一课：[计划驱动执行](16-plan-driven-execution.md) · [教程总览](README.md) · 下一课：[受限恢复策略](18-recovery-policy.md)

> 代码快照：`v0.17` · 相邻差异：`v0.16..v0.17` · 命令环境：Bash/zsh

> 本课只讲“失败发生了什么”的记录与保守收口。自动恢复、重试、检查点和回滚属于后续版本。

## 本课目标

当一个工具失败时，“失败”并不是一个足够精确的答案。我们至少需要知道：参数有没有通过校验、权限有没有通过、处理函数（handler，即真正执行工具工作的函数）有没有开始运行，以及环境是否可能已经改变。

读完本课，你应能用自己的话说明：

- `ExecutionResult` 是工具边界观察到的结果；`ExecutionAttempt` 是写入 State 的一次调用记录；`FailureEvent` 是从调用记录中抽出的失败事实；`ExecutionGeneration` 是验证证据所属的代次。
- 参数错误、权限拒绝、handler 异常、超时、shell 非零退出和验证失败为什么必须分开记录。
- 为什么一个已经获准、但可能改变环境的 `possible` 调用，即使最后失败，也会让旧验证证据失效。
- 为什么一回合全是只读调用时可以并发，而只要有潜在副作用就按模型原顺序执行和提交。

本课的主线是：**模型表达意图，Executor（工具执行边界）观察事实，State（任务状态）保存可追溯记录。**

## 上一版的问题

v0.16 的工具结果主要以文本进入 `AgentState`。文本可以说“失败了”，却不能稳定回答“handler 是否进入”“权限是否通过”“环境是否可能被改变”“这是哪一次调用”。上下文被裁剪或压缩后，只看历史消息还可能找不到这些答案。

v0.17 先解决记录问题，而不是替模型选择修复方案：每次调用都有 attempt（一次不可变的调用记录），每次失败都有带因果链接的 failure（失败事件），验证证据绑定到当前 generation（代次）。因此后续版本可以基于事实选择恢复，但本版不声称能恢复未知副作用。

## 前置条件与版本切换

需要 Python 3.10+；运行时仍只使用标准库。命令均适用于 Bash/zsh。建议先阅读第 16 课，再用下面的命令把差异和本版源码范围对照起来：

```bash
git checkout v0.16
git diff --stat v0.16..v0.17
git diff v0.16..v0.17 -- src/mini_agent/state.py src/mini_agent/tools/base.py src/mini_agent/agent.py src/mini_agent/context.py
git checkout v0.17
```

前两条命令分别回答“改动有多大”和“改了哪些相关文件”。最后一条切到本课快照，后面的源码链接和行为都以这个 tag 为准。查看差异时，应特别留意：工具边界开始返回结构化结果，State 开始保存 attempt/failure/generation；这正是本课要理解的变化。

## 新增与改动文件

`git diff --stat v0.16..v0.17` 显示本版还有其他配套改动。下表只列本课主线直接使用的部分：

| 文件 | 变化 | 作用 |
|---|---|---|
| `src/mini_agent/state.py` | 增加 generation、attempt、failure、参数指纹和预算 | 保存不依赖消息历史的执行事实 |
| `src/mini_agent/tools/base.py` | 增加 `Tool.effect_class`、`ExecutionResult` 和参数校验结果 | 在工具边界统一观察值 |
| `src/mini_agent/agent.py` | 按 effect class 选择并发/串行；拒绝混合 verification | 保持记录顺序和协议完整 |
| `src/mini_agent/context.py` | 渲染最近失败、代次和预算 | 裁剪或压缩后仍能诊断 |
| `src/mini_agent/config.py` | 设置失败、指纹和 repair cycle 上限 | 防止尝试无界增长 |

## 版本变更定位

图例：`[旧]` 是 v0.16 已有，`[+]` 是 v0.17 新增，`[~]` 是 v0.17 修改，`[C]` 是主要消费者，`[B]` 是本版边界。

先看上一版的入口和收口方式，读者可以把它当作“没有结构化失败事实时会发生什么”的基线：

```text
[旧] agent loop -> [旧] ToolExecutor -> PermissionGate -> handler
                         -> 文本结果 -> [旧] AgentState.record_tool
                         -> role=tool 回灌 -> generation/verification 收口
```

v0.17 把“观察工具结果”和“把事实写入状态”分开，并在调用调度处加入副作用分类：

```text
[旧] agent loop
  -> [~] 解析 tool calls
       -> 全 none effect：[+] 并发执行，按模型顺序提交事实
       -> 含 possible：[+] 按模型顺序串行执行和提交
       -> 混合 verification 与 possible：[+] verification 记为 invalid
  -> [~] ToolExecutor.execute_result()
       -> 参数校验 -> PermissionGate -> [~] handler
       -> [+] ExecutionResult
       -> [+] AgentState.reserve_attempt()（possible 在 handler 前推进代次）
       -> [+] record_execution_result()
            -> [+] ExecutionAttempt / FailureEvent / ExecutionGeneration
            -> [~] verification evidence 绑定 generation
  -> [旧] 每个调用回灌 role=tool
  -> [~] ContextManager 展示最近失败和预算

[B] 不负责：recover 工具、自动 retry、参数修正、checkpoint、rollback、跨进程持久化。
```

这张图中的入口是 `ToolExecutor.execute_result()`，主要消费者是 `AgentState`、`ContextManager` 和 agent loop。图后半段的关键是不论 handler 成功还是失败，结构化结果都会被记录并回灌；“可能发生了副作用”会先改变代次，不能等 handler 返回成功才决定。

## 核心概念与数据结构

### 1. 先把一次调用看成“观察值”

要解决的问题是：错误文本太晚、太粗。工具执行器（Executor）是所有工具调用经过的边界，它按固定顺序做三件事：**参数校验 → 权限检查 → handler 执行**。`ExecutionResult` 把这三个阶段的观察结果放在一起，供 State 和上下文使用。

下面的片段只展示最重要的字段；`duration_ms`、输出摘要、退出码和内部预留信息也属于 v0.17 的完整结果结构。看这段代码的目的，是理解“结果不是一个 `ok: bool`”这一点：

```python
@dataclass(frozen=True)
class ExecutionResult:
    tool: str
    arguments: dict[str, Any]
    permission: Literal["allowed", "denied", "not_checked"]
    handler_admitted: bool
    outcome: Literal["succeeded", "failed", "denied", "timeout", "invalid"]
    effect_class: EffectClass
    error_kind: str | None = None
```

这段定义带来的现象是：参数无效时 `outcome="invalid"`，不会询问权限；权限拒绝时 `outcome="denied"`，handler 不会进入；handler 抛异常时由 Executor 转成 `outcome="failed"`。每个结果仍然对应一个 `role=tool` 消息，不能因为失败就跳过协议回灌。

### 2. `effect_class` 说明“环境是否可能变化”

`effect_class` 是副作用分类，不是权限等级。`none` 表示可以按只读处理；`possible` 表示运行时不能安全证明没有副作用。它回答的是“执行后环境可能变了吗”，而 PermissionGate 回答的是“这次调用获准了吗”。例如 `run_shell(purpose="verification")` 在本次调用中按 `none` 处理。

对已通过权限的 `possible` 调用，State 在 handler 之前原子地预留 attempt，并推进 generation、清空旧验证证据：

```python
before = self._verification_generation
if effect_class == "possible":
    self._verification_generation += 1
    self.verification_evidence.clear()
return AttemptReservation(attempt_id, before, self._verification_generation)
```

这个顺序解释了一个容易误解的现象：handler 之后抛错，generation 也不会回退，因为它可能已经写入部分内容；参数错误和权限拒绝由于 handler 没有运行，不推进 generation。

### 3. Attempt、Failure 和因果链接

要解决的问题是“下一轮模型如何知道失败对应哪一次调用”。`ExecutionAttempt` 保存一次调用的不可变事实，包括工具名、参数的 canonical JSON hash（把 JSON 按稳定顺序编码后计算的指纹）、脱敏参数摘要、generation、结果和 `failure_id`。敏感值不会原样渲染到 Context。

`FailureEvent` 从 attempt 中描述失败本身：失败类别、发生阶段、是否可重试、受影响文件，以及 `caused_by_attempt_id`。因此可以沿着
`failure_id -> caused_by_attempt_id -> generation_id` 回溯，不必靠时间顺序或自然语言猜测。

下面这两个字段是最短的因果链示意。代码前看它是为了理解“failure 不会凭空出现”；代码后可以看到，State 保存的是 ID 关系，而不是把整段自然语言错误重新猜一遍：

```python
ExecutionAttempt(..., generation_id=generation_id, failure_id=failure_id)
FailureEvent(..., generation_id=generation_id,
             caused_by_attempt_id=attempt_id)
```

v0.17 的基础分类和默认可重试性如下：

| 观察到的结果 | category（类别） | retryable（可重试） |
|---|---|---|
| 参数或协议不合法 | `protocol` | 否 |
| 权限明确拒绝 | `permission` | 否 |
| 超时 | `transient` | 是 |
| verification 非零或失败 | `validation` | 是 |
| 已获准的 possible handler 异常 | `unknown` | 否 |
| 其他执行失败，例如普通 shell 非零退出 | `deterministic` | 否 |

表格的用途不是让读者背分类，而是说明同一句“失败了”会走不同的后续边界：超时和验证失败可以计入有限的重试/修复预算；未知副作用不能安全自动重试。

### 4. 验证证据只属于当前代次

验证证据是一次明确的检查结果，不是“最近一次工具调用成功”的同义词。只有 `run_shell(purpose="verification")` 在当前 generation 返回 `[exit=0]`，才会留下通过证据。任何获准的 `possible` 调用都会清空旧证据；权限拒绝不会清空，因为 handler 没有运行。

在 v0.17 中，确定性、协议或权限错误会收口为 `failed`；未知副作用会收口为 `blocked`；可重试失败达到同一参数指纹预算，或验证失败耗尽 repair cycle，也会停止。`blocked` 表示当前运行不能安全继续，不表示任务成功。

验证收口的关键判断可以压缩成下面的条件：

```python
passed = result.outcome == "succeeded" and result.exit_code == 0
self._last_verified_generation = generation_id if passed else -1
```

它说明“命令返回成功”还不够：证据必须和当前 generation 一起保存；下一次获准的 `possible` 调用会先清空它。

## 为什么这样设计

把结果压成 `ok: bool` 最简单，但会丢失“是否获准”“是否进入 handler”和“是否可能改变环境”。结构化结果和不可变记录多了一些字段，却让后续恢复有可靠事实可依赖；代价是只读 shell 也可能因为无法证明安全而被保守归为 `possible`。

全是 `none` 的调用可以并发，提高吞吐；只要出现 `possible`，整回合就按模型顺序执行和提交，牺牲并发度换取 generation 和 attempt 顺序稳定。混合 `possible` 与 verification 的回合把 verification 标为 invalid，避免检查在修改前或修改中运行。

本版刻意把“记录事实”和“决定如何修复”分开：没有自动重试、恢复动作、回滚或跨进程持久化。这样下一课可以在明确的失败事实和预算上实现受限恢复，而不会把修复策略藏在记录层里。

## 设计边界

- Executor 负责把 handler 异常转成错误结果；LLM 或 CLI 顶层异常仍由上层处理。
- 工具结果全部回灌后才进入下一轮；失败不会跳过对应的协议消息。
- hash 用于稳定关联和计数，不是授权凭证；脱敏摘要也不能替代 PermissionGate。
- 本版无法判断 partial write 的实际影响，只能把未知副作用归为 `unknown` 并保守阻塞。
- 状态只存在当前进程。普通 history trimming 不会删除 State 中的 failure，但进程重启后不会自动恢复。

## 关键流程

下面的流程用于观察一条调用从意图到状态的完整路径；读者不需要先记住函数名，只要先看每个分支发生在哪个边界：

```text
tool call
  -> 参数/schema 校验
       └─失败 -> invalid attempt -> protocol failure
  -> PermissionGate
       └─拒绝 -> denied attempt -> permission failure
  -> possible? 预留 attempt、推进 generation、清空旧验证
  -> handler
       ├─timeout / 非零 / 异常 -> ExecutionResult -> FailureEvent
       └─成功 -> attempt；verification 另写 evidence
  -> role=tool 回灌 -> 下一轮模型读取 Structured State
```

运行真实任务时，应观察到：权限拒绝和参数错误不改变 generation；获准的文件写入或 execution shell 即使失败也要求重新验证；上下文压缩后最近 failure、generation 和剩余预算仍可见。命令行首条任务处理后，程序仍进入交互循环。

## 实现拆解

`Tool.effect_for()` 把工具默认等级和 verification 特例分开。`ToolExecutor.execute_result()` 只负责校验、授权、执行并转换成观察值，不替 State 猜测原因。`AgentState.record_execution_result()` 再依据结果生成 attempt、failure、验证证据和终态。`ContextManager._render_state()` 读取 State 的 `snapshot()`，所以这些事实不依赖旧消息是否仍在发送视图中。

## 本版特性、下一课与代码索引

v0.17 把“失败”从一段文本变成可观察、可计数、可追溯的事实，并让验证证据严格属于某个 generation。下一课将在这些事实和预算约束下选择有限的恢复动作。

- [`src/mini_agent/config.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.17/src/mini_agent/config.py)
- [`src/mini_agent/state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.17/src/mini_agent/state.py)
- [`src/mini_agent/tools/base.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.17/src/mini_agent/tools/base.py)
- [`src/mini_agent/agent.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.17/src/mini_agent/agent.py)
- [`src/mini_agent/context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.17/src/mini_agent/context.py)
