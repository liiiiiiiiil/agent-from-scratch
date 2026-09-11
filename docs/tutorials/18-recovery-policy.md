# 第 18 课：受限恢复策略（Recovery Policy，v0.18）

上一课：[失败模型](17-failure-model.md) · [教程总览](README.md) · 下一课：[单文件检查点与回滚（Checkpoint / Rollback）](19-checkpoint-rollback.md)

> 代码快照：`v0.18` · 相邻差异：`v0.17..v0.18` · 命令环境：Bash/zsh

> 补丁代码快照：`v0.18.1` · 相邻差异：`v0.18..v0.18.1` · 命令环境：Bash/zsh

> 本课保留 v0.18 的概念和演进基线；恢复边界以 v0.18.1 修正后的实现为准。v0.18.1 没有增加新的恢复动作，只把既有承诺在拒绝、授权、预算和终态路径上落实一致。

> 运行要求：Python 3.10+；运行时只使用标准库。

## 本课目标

第 17 课已经能回答“哪次调用失败、失败属于什么类别、可能有没有副作用”，但还没有规定模型下一步能做什么。本课要解决的是：**失败后怎样做出有限、可审计、不会把一次成功调用误当成修复的选择。**

读完本课，你应能解释：

- `recover` 为什么是一个受 JSON Schema（描述输入形状和允许值的规则）约束的控制工具，而不是模型用文字宣称“已修复”；
- `retry`、`adjust`、`ask`、`block` 何时适用，以及它们如何经过参数校验、PermissionGate 和预算；
- 为什么接受恢复后要打开后继 generation（代次），清除旧验证，并在下一轮单独验证；
- v0.18.1 为什么把“预留额度”“确认目标权限”和“激活 generation”拆开。

主线只有一句话：**先根据失败事实选择有限动作，再用当前代次的新验证判断任务是否真的完成。**

## 上一版的问题

v0.17 建立了事实层，但模型仍可能只在自然语言中决定“再试一次”“换个参数”或“停止”。自然语言不能保证重试的是同一组原始参数，也不能阻止同一失败被无限重复。更重要的是，恢复调用返回成功时，不能因此宣称原任务已经修复；它只说明某个恢复 attempt（一次调用记录）执行完了。

v0.18 把这个选择放进已有工具协议：模型调用 `recover`，运行时校验动作和 failure 引用、预留预算，需要执行真实工具时仍走参数校验、PermissionGate（权限闸门）和 ToolExecutor（工具执行边界）。恢复结果只报告执行事实；是否修复，交给后续 verification（独立验证）。

## 前置条件与版本切换

建议先阅读第 17 课。命令均适用于 Bash/zsh：

```bash
git checkout v0.17
git diff --stat v0.17..v0.18
git diff v0.17..v0.18 -- src/mini_agent/recovery.py src/mini_agent/state.py src/mini_agent/tools/__init__.py src/mini_agent/agent.py
git diff --stat v0.18..v0.18.1
git diff v0.18..v0.18.1 -- src/mini_agent/recovery.py src/mini_agent/state.py src/mini_agent/tools/base.py src/mini_agent/tools/file.py src/mini_agent/context.py src/mini_agent/agent.py
git checkout v0.18.1
```

前四条命令让读者先看到 v0.18 新增了什么，再看到 v0.18.1 修正了哪些边界；最后一条切到补丁快照。查看差异时，先找 `recover` 从哪里注册、恢复如何复用 ToolExecutor，再看补丁如何把拒绝也写入 State。正文涉及 v0.18 原始行为的地方会明确标出，避免把补丁能力倒灌回主版本。

## 新增与改动文件

下表是本课教学主线直接涉及的文件；补丁还调整了文件工具、上下文渲染和终态调度。

| 文件 | 变化 | 作用 |
|---|---|---|
| `src/mini_agent/recovery.py` | v0.18 新增；v0.18.1 收紧拒绝和授权顺序 | 定义 `recover` schema 与动作调度 |
| `src/mini_agent/state.py` | 增加恢复记录、因果链接、额度和 generation 操作 | 原子保存恢复事实，避免并发绕过预算 |
| `src/mini_agent/tools/__init__.py` | 为每个 `AgentState` 注册 `recover` | 让控制工具与任务状态绑定，不共享任务数据 |
| `src/mini_agent/tools/base.py` | 复用预留 attempt/generation；补丁处理终态和 schema 拒绝 | 保持所有真实工具经过同一执行边界 |
| `src/mini_agent/agent.py` | 顺序处理 recover，并为每个调用回灌 tool result | 保持调用协议完整 |

## 版本变更定位

图例：`[旧]` 是上一版已有，`[+]` 是本版新增，`[~]` 是本版修改，`[C]` 是主要消费者，`[B]` 是本版边界。

先看 v0.17 的基线：失败会被记录和展示，但没有受控的恢复入口。

```text
[旧] agent loop -> ToolExecutor.execute_result()
  -> [旧] AgentState.record_execution_result()
  -> ExecutionAttempt / FailureEvent / generation
  -> role=tool 回灌 -> [C] Structured State
  -> [B] 没有受控的恢复入口
```

v0.18 在同一条调用链旁边加入控制工具。这个图展示的是概念主线；目标工具的权限预留顺序在 v0.18.1 由补丁进一步收紧：

```text
[旧] FailureEvent + Structured State
  -> [~] agent loop -> [+] recover JSON Schema
  -> [+] RecoveryRuntime.recover()
       -> 校验 failure、动作组合和预算
       -> [+] AgentState.reserve_recovery()
            -> retry：取直接失败 attempt 的私有原始参数
            -> adjust：先校验目标工具的新参数
            -> ask/block：不进入 handler，最终阻塞
       -> PermissionGate -> ToolExecutor.execute_result() -> handler
       -> [+] RecoveryAction / ExecutionAttempt
  -> role=tool 回灌
  -> 下一轮单独 verification

[B] rollback 明确拒绝；没有 checkpoint、自动 repair 调度、trace replay 或跨进程持久化。
```

v0.18 的入口是 `RecoveryRuntime.recover()`，它负责动作选择；`AgentState` 保存 failure、recovery、generation 和预算；ToolExecutor 仍是唯一的参数、权限和 handler 边界。v0.18.1 的重点不是另加一条路径，而是让这些节点在拒绝、终态和权限不通过时仍然保持一致。

## 核心概念与数据结构

### 1. `recover` 是可校验的恢复申请

要解决的问题是“模型说要修复”没有统一格式，也没有明确说明它针对哪次失败。`recover` 是一个控制工具：它不直接代表任务完成，而是提交一个带 failure 因果引用、理由和动作参数的申请。

下面的 schema 片段说明输入的骨架；阅读它前，先记住用途是限制模型能表达的恢复范围：

```python
parameters = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["retry", "adjust", "ask", "block"]},
        "caused_by_failure_id": {"type": "string"},
        "reason": {"type": "string", "minLength": 1, "maxLength": 500},
        "requested_attempt": {"type": "string"},
        "requested_tool": {"type": "string"},
        "requested_arguments": {"type": "object"},
    },
    "required": ["action", "caused_by_failure_id", "reason"],
    "additionalProperties": False,
}
```

这段规则通过后，还要检查 failure 是否存在、任务是否已经终态、动作组合是否合法、目标参数是否能通过工具 schema，以及预算是否还有余量。失败的申请不会静默消失：在 v0.18.1 中，schema、参数、failure 引用、动作组合、恢复预算、`recover` 自身权限和目标权限的拒绝都会生成唯一 `recovery_id`，并只记录一条 `status="rejected"` 的 `RecoveryAction`；generation 不变。

工具名严格匹配注册表。例如 `edit_file` 是合法目标名，`functions.edit_file` 不是别名。目标权限使用当前会话的同一把 PermissionGate；目标获准后，实际执行复用已经确认的授权，不重复询问同一次目标。

### 2. 四种动作把恢复限制在可解释的范围内

四种动作解决四个不同问题：

| action | 需要提供什么 | 运行时行为 | 重要边界 |
|---|---|---|---|
| `retry` | 直接失败的 `requested_attempt` | 取 State 私有保存的原始参数，重新执行原工具 | 不接受模型改写参数；同一 failure 默认最多 3 次，且 failure 必须可重试 |
| `adjust` | `requested_tool` 与 `requested_arguments` | 先按目标工具 schema 校验，再执行 | 记录 canonical JSON hash 和脱敏摘要；不能把 `recover` 作为目标 |
| `ask` | failure 引用与 reason | 不进入 handler，创建 state-only generation，进入 `blocked` | 表示等待外部条件，但本版不提供外部等待机制 |
| `block` | failure 引用与 reason | 不进入 handler，创建 state-only generation，进入 `blocked` | 表示按策略保守停止 |

`retry` 的直接引用关系可以用下面的真实逻辑理解。代码前先明确用途：它防止模型拿一个“相关但不是直接失败”的 attempt 来重试；代码后再看结果：参数从 State 的私有副本深拷贝，不从模型重新提交。

```python
if source_attempt is None or source_attempt.failure_id != caused_by_failure_id:
    return self._reject_recovery(
        action, caused_by_failure_id, reason,
        "retry 必须引用直接失败 attempt",
    ) + (None,)
requested_tool = source_attempt.tool
requested_arguments = deepcopy(
    self._original_attempt_arguments.get(source_attempt.attempt_id, {})
)
```

因此 retry 不能悄悄替换参数；adjust 才是显式提交新工具和新参数的入口。adjust 的无效参数不会进入权限闸门或 handler。两种真实执行都仍经过 ToolExecutor.execute_result()。

### 3. 接受恢复时，generation 是验证边界，不是快照

generation 的直观含义是“从哪一次环境边界开始重新验证”。它不是操作系统快照，也不能撤销文件、shell、网络或未知副作用。接受恢复后，旧 verification evidence 会被清空，并要求下一轮验证。

v0.18 原始实现把预留动作、generation 激活和目标执行连在一起。v0.18.1 将它们拆开，以避免目标权限还没通过就让旧证据失效。补丁后的顺序如下：

```text
recovery_target()
  -> 只读确认 failure、动作组合、目标和当前额度
reserve_recovery(..., defer_generation=True)
  -> 在 State 锁内创建 proposed action
  -> 预留 failure retry 与目标参数指纹额度
  -> generation 尚未改变，attempt 尚未创建
PermissionGate.guard(target)
  ├─拒绝 -> deny_reserved_recovery()
  │         proposed -> rejected
  │         释放未使用的 retry / 参数指纹额度
  │         不打开 generation
  └─通过 -> activate_recovery()
            打开后继 generation，清除旧 verification
            创建并执行预留 attempt
```

这里有两类额度，含义不要混淆：恢复动作额度记录“申请过几次”，所以目标权限拒绝后不退还；failure retry 和参数指纹额度限制“实际可执行几次”，目标没有获准执行时会退还。只有目标获准后才推进 generation，表示恢复边界已被接受，而不是一个尚未授权的意图。

### 4. 恢复结果不是修复证据

`RecoveryAction.result_attempt` 只表示恢复调用的执行事实，不表示任务已经修复。retry/adjust 的目标 attempt 使用已预留 generation；ask/block 没有 handler，却仍创建 state-only generation 并进入 `blocked`。

恢复结果先以对应的 `role=tool` 消息完整回灌。下一轮才能调用 `run_shell(purpose="verification")`，只有当前 generation 的 `[exit=0]` 才可能成为完成条件。如果验证失败，它是新的 `validation` failure，不能用“恢复工具返回 executed”来替代。

这条边界在状态判断中体现为“证据的代次必须等于当前代次”：

```python
return (
    bool(self.verification_evidence)
    and self._last_verified_generation == self._verification_generation
    and self.verification_evidence[-1].outcome == "passed"
)
```

因此恢复返回 `executed` 只说明恢复 attempt 已记录；只有后续验证把当前 generation 标记为通过，任务才有完成证据。

## 为什么这样设计

让模型读到失败文本后直接再调工具成本低，却不能保证重试同一调用，也难以限制循环；让恢复模块自行执行所有修复，则会重复工具逻辑，容易绕过参数校验和权限规则。

本版采用“控制工具 + 既有执行器”：recover 只选择受限动作、建立因果链接、占用预算；真实工具仍走唯一的参数和权限边界。这样做多了一次控制调用和一个 LLM 回合，换来可审计的记录，以及不会跨 generation 误用验证证据的约束。

本版不自动重写完整 Todo；需要改计划时，下一轮使用 `update_todo`。本版也不把 generation 当成 checkpoint，不提供自动 repair、rollback 或跨进程恢复。

## 设计边界

- `MAX_RECOVERY_ACTIONS` 默认为 8；恢复申请额度耗尽时保守进入 `blocked`。`MAX_FAILURE_RETRIES`、`MAX_ATTEMPT_FINGERPRINTS` 和 `MAX_REPAIR_CYCLES` 分别默认为 3、3、3。
- schema、参数、failure 引用、预算或权限拒绝的恢复不会打开 generation，但 v0.18.1 仍记录拒绝事实和 `recovery_id`。
- hash 用于关联和计数，脱敏摘要只用于展示；两者都不是授权凭证。
- recover 与 possible effect 同轮时按模型顺序串行提交；verification 不得与副作用同轮，恢复后必须下一轮验证。
- rollback 明确拒绝。v0.18 没有检查点，不承诺撤销文件、shell、网络或未知副作用，也不提供跨进程持久化。
- ask 和 block 都是 `blocked`；前者说明等待外部条件，后者说明策略停止，本版不会自动等待或继续执行。
- Structured State 会展示 recovery notice、最近三条失败与恢复动作、失败对应的工具/attempt/generation/分类/可重试性，以及 hash 形式的剩余预算；裁剪或压缩后这些字段仍从 State 快照重建。
- 一旦状态进入 `blocked` 或 `failed`，调度器和 Executor 都拒绝后续 handler 与权限询问。v0.18.1 仍按原顺序为批次中剩余的每个 tool call 追加 `task_terminal` 结果并全部回灌；终态原因不会被后续拒绝记录改写。

## 关键流程

下面先看一次 retry 的正常路径，再看两个重要拒绝点。它用于判断运行时观察到的字段，而不是把恢复调用当成完成信号：

```text
一次执行 attempt 失败，创建 failure event
  -> recover(retry,
       caused_by_failure_id=<该 failure event 的 ID>,
       requested_attempt=<直接失败的 attempt ID>)
  -> schema / failure 关联 / retry 预算
       ├─拒绝 -> status=rejected，generation 不变 -> role=tool
       └─预留额度 -> 取私有原始参数 -> PermissionGate
                    ├─拒绝：动作 rejected，实际执行额度释放，generation 不变
                    └─接受：打开 generation，清除旧 verification
                         -> handler -> 新 attempt
                    -> role=tool
  -> 下一轮单独 verification
       ├─[exit=0]：当前 generation 获得证据
       └─失败：新的 validation failure，不能宣称已修复
```

运行真实任务时，成功接受恢复应观察到 generation 增加、旧 verification evidence 消失，并出现要求独立验证的 recovery notice。若申请或目标权限被拒绝，应看到 `status=rejected`、一个 recovery_id，且 generation 不变；如果同一批次在某个 possible handler 异常后进入终态，后续调用只得到 `error_kind=task_terminal`，不会进入 handler。

## 实现拆解

`create_registry(state)` 为每个 `AgentState` 新建 RecoveryRuntime 和 `recover` 工具，避免不同任务共享恢复状态。agent loop 按模型顺序处理 recover；每个 tool call 都有对应的 `role=tool` 结果，全部回灌后才进入下一轮。

在 v0.18.1，`RecoveryRuntime.recover()` 的关键顺序是：检查 reason 与 rollback；adjust 校验目标参数；`recovery_target()` 做不改变状态的目标确认；`reserve_recovery(defer_generation=True)` 原子预留额度；用当前会话的 PermissionGate 检查目标；`activate_recovery()` 打开 generation；ToolExecutor 执行目标；`record_execution_result()` 写入 attempt/failure。目标权限拒绝只更新已预留动作，不增加第二条拒绝记录，也不打开 generation。

补丁还把 `edit_file` 的两个已知前置条件错误从普通 `ValueError` 分开：`EditNoMatchError` 表示 `old_string` 没有匹配，`EditMultipleMatchesError` 表示多处匹配但没有 `replace_all=true`。两种情况都发生在写入前，文件保持原样；Executor 用 `error_kind` 分别记录，State 将它们归为 `deterministic`，任务保持可继续状态。相反，已获准进入 handler 的普通异常仍可能已经产生未知副作用，继续归为 `unknown` 并阻塞；补丁不通过异常文字猜测是否写入。

## 本版特性、下一课与代码索引

v0.18 将失败后的选择限制为精确重试、显式调参、等待外部条件或保守停止。它不撤销副作用，也不把恢复 attempt 当作完成证据；每个已接受动作都要求当前 generation 的独立验证。v0.18.1 进一步让拒绝记录、预算预留、目标授权、文件前置条件和终态批次遵守同一套边界。

下一课将讨论检查点与有边界的回滚：只有保存明确文件前镜像的场景才可能恢复内容，shell 和外部副作用仍不承诺可回滚。

核心代码索引：

- v0.18：[recovery.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18/src/mini_agent/recovery.py)、[state.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18/src/mini_agent/state.py)、[tools/__init__.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18/src/mini_agent/tools/__init__.py)、[tools/base.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18/src/mini_agent/tools/base.py)、[agent.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18/src/mini_agent/agent.py)
- v0.18.1：[recovery.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18.1/src/mini_agent/recovery.py)、[state.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18.1/src/mini_agent/state.py)、[tools/base.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18.1/src/mini_agent/tools/base.py)、[tools/file.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18.1/src/mini_agent/tools/file.py)、[tools/file_errors.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18.1/src/mini_agent/tools/file_errors.py)、[context.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18.1/src/mini_agent/context.py)、[agent.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18.1/src/mini_agent/agent.py)

## 补丁附录：v0.18.1 修正了什么

这一节最后阅读即可：主线仍是“受限恢复后必须重新验证”。补丁没有增加新的动作，而是修正了 v0.18 原始实现中会让模型看到的事实、State 保存的事实和实际执行边界不一致的路径。

### 1. 拒绝也必须成为可追踪事实

v0.18 已经要求恢复可审计、预算在执行前生效、终态后不再执行工具，但原始实现没有在所有入口兑现：schema 或参数拒绝可能没有 `RecoveryAction`，目标权限确认前可能已经推进 generation，同一参数指纹预算没有统一覆盖普通执行与恢复执行，终态批次的后续调用还可能继续走执行路径。

v0.18.1 把这些入口统一到 `reject_recovery()` 或等价的 Executor 拒绝路径。下面的图先说明输入，再说明读者应观察到的结果：

```text
recover 申请
  ├─ schema / JSON 不合法
  ├─ failure 或 action 引用不合法
  ├─ adjust 目标参数不合法
  ├─ recover 自身权限拒绝
  ├─ 恢复或参数指纹预算耗尽
  └─ 目标工具权限拒绝
         v
一条 RecoveryAction(status="rejected")
  + 一个包含 recovery_id 的 role=tool 结果
  + generation 保持不变
```

只记录一次很重要：目标权限拒绝发生在已创建的 `proposed` 动作之后，运行时更新这条记录，而不是再追加第二条拒绝记录。恢复动作申请本身仍消耗 `MAX_RECOVERY_ACTIONS`，所以模型不能用无效参数或无权目标无限消耗回合；默认上限为 8，达到上限后任务进入 `blocked`。

### 2. 把额度预留、授权和 generation 激活拆开

补丁后的 `proposed` 不是执行结果。它只说明 State 已在锁内占用恢复申请额度和实际执行所需的 retry/指纹额度。目标没有获准时，后两类实际执行额度释放；恢复申请额度不释放。通过同一把 PermissionGate 后，`activate_recovery()` 才打开后继 generation，Executor 以 `permission_already_checked=True` 复用这次授权，避免同一目标被重复询问。

补丁还收紧动作组合：retry 只能引用可重试 failure 的直接失败 attempt，并复用 State 私有的原始参数；adjust 不接受 `requested_attempt`；ask/block 不接受目标参数；`recover` 本身不能成为恢复目标。目标工具名必须严格匹配注册表，例如 `edit_file`，而不是 `functions.edit_file`。

### 3. 区分“无法编辑”和“写入结果未知”

`edit_file` 先读取文件并检查 `old_string`。没有匹配，或多处匹配但没有 `replace_all=true` 时，写入尚未发生，这是确定性前置条件失败，不是未知副作用。v0.18.1 用专用异常和 `error_kind` 稳定归类，文件保持原样，generation 仍保留这次已获准的 possible-effect attempt 所打开的边界，但任务不会仅因这两个异常进入 `blocked`，模型可以改正参数后申请 adjust。

相反，只要调用已获准进入 handler，而运行时无法证明副作用没有发生，就仍用 `handler_exception` 和 `unknown` 分类，保留已推进的 generation 并进入 `blocked`。补丁没有通过异常文本猜测文件是否写入。

### 4. 终态既停止执行，也保持工具协议完整

一次 possible-effect handler 抛出未知异常后，State 可能立即进入 `blocked`。同一条模型消息中剩余的 tool call 不再询问权限，也不进入 handler；但它们不能从协议中消失。调度器按原顺序为每个 call 追加对应的 `role=tool`，并以 `error_kind=task_terminal` 说明拒绝原因。

这同时维护两个不变量：`blocked` 或 `failed` 之后不再进入后续 handler；每个 tool call 仍有且只有一个对应的 tool result，整批回灌后才进入下一轮。终态原因采用“首次终态获胜”，后续拒绝记录不会覆盖最初导致终态的 failure。

### 5. 如何观察补丁是否生效

切换到补丁 tag 后，可以先查看本课开头给出的真实 diff。运行一次“失败 → 被拒绝的恢复”时，应观察到带 failure 因果引用的 rejected recovery，`recovery_actions_remaining` 减少，而 generation 不变；若是目标权限拒绝，retry 与参数指纹的实际执行额度不减少。

运行 `edit_file` 无匹配场景时，应观察到 `error_kind=edit_no_match`、failure category 为 `deterministic`、文件内容不变，任务仍可继续。若让一个已获准的 possible-effect handler 抛出普通异常，则应观察到 category 为 `unknown`、任务进入 `blocked`，同批后续调用只收到 `task_terminal`，不会进入 handler。

v0.18.1 仍不提供检查点、回滚、自动 repair 调度或 trace replay；这些属于后续版本。补丁只让 v0.18 已声明的恢复协议在拒绝、预算、权限、异常与终态路径上保持一致。
