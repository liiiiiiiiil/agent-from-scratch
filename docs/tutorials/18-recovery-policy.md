# 第 18 课：受限恢复策略（Recovery Policy，v0.18）

上一课：[失败模型](17-failure-model.md) · [教程总览](README.md) · 下一课：v0.19（规划中）

> 代码快照：`v0.18` · 相邻差异：`v0.17..v0.18` · 命令环境：Bash/zsh

> 补丁代码快照：`v0.18.1` · 相邻差异：`v0.18..v0.18.1` · 命令环境：Bash/zsh

本文保留 v0.18 的概念与演进基线；恢复边界说明以 v0.18.1 修正后的实现为准。

> 运行要求：Python 3.10+，运行时只使用标准库。

## 本课目标

第 17 课把失败变成了可追溯的 State 事实，但它只会记录，不能决定下一步。读完本课，你应能解释：

- recover 为什么是受 JSON Schema 约束的控制工具，而不是模型用文字宣称“已修复”；
- retry、adjust、ask、block 何时适用，以及它们如何受参数校验、权限闸门和预算限制；
- 为什么每个被接受的恢复动作都要打开后继 generation（代次），并要求下一轮独立验证。

主线是：先依据失败事实选择有限动作，再用当前代次的新验证判断任务是否真的完成。

## 上一版的问题

v0.17 能回答“哪次调用失败、失败属于什么类别、是否可能有副作用”，但模型仍只能在自然语言中自行决定是否重试、改参数或停止。同一失败可能被无限重试，重试参数也可能被悄悄替换；恢复调用看似成功时，还可能被误当成任务已经修复。

v0.18 把恢复选择放进已有工具协议：模型调用 recover，运行时校验动作和引用、预留预算，并在需要执行时继续使用参数校验、PermissionGate 和工具执行器。恢复结果只报告执行 attempt 的事实；是否修复仍由后续 verification 决定。

v0.18.1 补丁修正了两类容易误导恢复判断的边界。`edit_file` 的没有匹配和多处匹配现在是继承 `ValueError` 的前置条件异常，Executor 以 `error_kind=edit_no_match` 或 `edit_multiple_matches` 标记，State 将其归为 `deterministic`；它们不会改文件，也不会因为异常文本被当成未知副作用。相反，已获准进入 handler 的其他异常仍按副作用范围未知处理，可能进入 `blocked`，并且已经预留的 generation 不回退。

## v0.18.1 勘误与修复

原始 [v0.18 恢复实现](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18/src/mini_agent/recovery.py) 在部分拒绝路径漏记恢复动作，编辑前置条件失败也可能被误判为未知写入。补丁统一拒绝记录、修正分类，并落实已有终态约束；副作用未知时会真正停止后续工具执行。

预算和 generation 分两步预留：先在 State 锁内预留动作、重试与目标参数指纹额度（`proposed`），再询问目标权限。获准后 `activate_recovery()` 才打开 generation 并使旧验证失效。拒绝时保留同一条动作记录并改为 `rejected`，释放未使用的目标执行和重试额度；动作申请仍计数。连续无效申请达到 8 次也会阻塞，不再进入目标权限或 handler。本补丁不增加后续课程的能力。

## 前置条件与版本切换

建议先阅读第 17 课。以下命令适用于 Bash/zsh：

~~~bash
git checkout v0.17
git diff --stat v0.17..v0.18
git diff v0.17..v0.18 -- src/mini_agent/recovery.py src/mini_agent/state.py src/mini_agent/tools/__init__.py src/mini_agent/agent.py
git diff --stat v0.18..v0.18.1
git checkout v0.18.1
~~~

## 新增与改动文件

| 文件 | 变化 | 作用 |
|---|---|---|
| src/mini_agent/recovery.py | 新增 | recover schema 与动作调度 |
| src/mini_agent/state.py | 修改 | 原子预留恢复、因果链接、预算和终态 |
| src/mini_agent/tools/__init__.py | 修改 | 为每个 AgentState 注册 recover |
| src/mini_agent/agent.py | 修改 | 顺序处理 recover，并回灌 tool result |
| src/mini_agent/tools/base.py | 修改 | 复用预留的 attempt/generation 执行工具 |

## 版本变更定位

图例：旧表示 v0.17 已有，+ 表示 v0.18 新增，~ 表示本版修改，C 表示消费者，B 表示边界。

~~~text
v0.17 基线：
[旧] agent loop -> ToolExecutor.execute_result()
  -> [旧] AgentState.record_execution_result()
  -> ExecutionAttempt / FailureEvent / generation
  -> role=tool 回灌 -> [C] Structured State
  -> [B] 没有受控的恢复入口

v0.18：
[旧] FailureEvent + Structured State
  -> [~] agent loop -> [+] recover JSON Schema
  -> [+] RecoveryRuntime.recover()
       -> [+] AgentState.reserve_recovery(defer_generation=True)
            -> 校验 failure、动作和预算，预留额度
            -> PermissionGate -> activate_recovery()
            -> 打开后继 generation，清除旧验证
            ├─ retry：复用直接失败 attempt 的私有原始参数
            ├─ adjust：先校验目标工具的新参数
            ├─ ask/block：state-only generation -> [B] blocked
            └─ rollback -> [B] 明确拒绝
       -> [旧] ToolExecutor.execute_result() -> PermissionGate -> handler
       -> [~] RecoveryAction / ExecutionAttempt
  -> role=tool 回灌
  -> 下一轮独立 verification
~~~

RecoveryRuntime 调度动作；AgentState 保存事实、generation 和预算；ToolExecutor 仍是唯一的权限与 handler 边界。

## 核心概念与数据结构

### 1. recover 是可校验的恢复申请

recover 必须说明触发它的 failure、原因，以及动作所需的引用或参数。schema 只公开四种 action，并拒绝额外字段：

~~~python
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
~~~

schema 通过后还要确认 failure 存在、任务未终态、动作组合合法且预算有余量。被拒绝的动作记录为 status="rejected" 的 RecoveryAction，不创建后继 generation。完整实现见 [recovery.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18.1/src/mini_agent/recovery.py)。

schema 本身、控制工具权限、failure 引用、目标工具参数、恢复预算和目标工具权限的拒绝都会生成一个带 `recovery_id` 的拒绝结果，并且每个申请只记录一次。目标权限检查使用当前会话同一把 PermissionGate；通过后才预留 generation 和 attempt，实际目标调用复用这次预留，不重复询问同一次授权。工具名严格匹配注册表，`functions.edit_file` 不是合法别名。

### 2. 四种动作限制恢复范围

| action | 输入 | 运行时行为 | 边界 |
|---|---|---|---|
| retry | 直接失败的 requested_attempt | 深拷贝 State 私有原始参数，执行原工具 | 不接受模型改写参数；同一 failure 默认最多 3 次 |
| adjust | requested_tool 与 requested_arguments | 先按目标工具 schema 校验，再执行 | 记录 canonical JSON hash 和脱敏摘要 |
| ask | failure 与 reason | 创建无 handler 的 generation，进入 blocked | 等待外部条件 |
| block | failure 与 reason | 创建无 handler 的 generation，进入 blocked | 保守停止 |

retry 只能引用直接产生该 failure 的 attempt：

~~~python
if source_attempt is None or source_attempt.failure_id != caused_by_failure_id:
    return self._reject_recovery(
        action, caused_by_failure_id, reason,
        "retry 必须引用直接失败 attempt",
    ) + (None,)
requested_tool = source_attempt.tool
requested_arguments = deepcopy(
    self._original_attempt_arguments.get(source_attempt.attempt_id, {})
)
~~~

原始参数不渲染到 Structured State。adjust 必须明确提交新参数并先调用 validate_arguments()；无效参数不会进入权限闸门或 handler。两种动作的实际执行都继续经过 ToolExecutor.execute_result()。

### 3. 后继 generation 不等于修复证据

generation 是验证证据的边界，不是操作系统快照。接受恢复时，State 在同一把锁内推进代次、清空旧证据并要求验证：

~~~python
gid = self._verification_generation + 1
self._verification_generation = gid
self.verification_evidence.clear()
self._last_verified_generation = -1
self._verification_required = True
self.generations.append(ExecutionGeneration(
    gid, opened_by_failure_id=caused_by_failure_id,
    opened_by_recovery_id=rid, open_reason="recovery",
))
~~~

retry/adjust 复用该预留 generation 创建 ExecutionAttempt；ask/block 虽无 handler，也创建 state-only generation。RecoveryAction.result_attempt 只表示恢复调用的执行事实，不表示任务已修复。恢复结果先完整回灌 role=tool，下一轮才能调用 run_shell(purpose="verification")。只有当前 generation 的 [exit=0] 才可能成为完成条件。

完整状态实现见 [state.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18.1/src/mini_agent/state.py)。

## 为什么这样设计

让模型读到失败文本后直接再调工具成本低，却不能保证重试同一调用，也难以限制循环。让恢复模块执行所有修复则会重复工具逻辑，容易绕过参数校验和权限规则。

本版采用“控制工具 + 既有执行器”：动作有 schema、因果链接和预算，真实工具仍走唯一执行边界。代价是多一次控制调用和 LLM 回合；收益是记录可审计，验证证据不会跨代次误用。本版不自动重写完整 Todo；需要改计划时，下一轮使用 update_todo。

## 设计边界

- MAX_RECOVERY_ACTIONS 默认为 8；预算耗尽保守进入 blocked。
- schema、预算或权限拒绝的恢复不打开 generation，但仍记录拒绝事实。
- hash 只用于关联和计数，脱敏摘要只用于展示；两者都不是授权凭证。
- recover 与 possible effect 同轮时按模型顺序串行提交；verification 不得与副作用同轮，恢复后必须下一轮验证。
- rollback 明确拒绝。v0.18 没有 checkpoint，不承诺撤销文件、shell、网络或未知副作用，也不提供跨进程持久化。
- ask 和 block 都是 blocked；前者等待外部条件，后者表示策略停止。
- 运行中的 Structured State 也会展示 recovery notice、最近三条失败与恢复动作、失败对应的工具/attempt/generation/分类/可重试性，以及 hash 形式的剩余预算；失败调用不会再被标作“已完成，不要重复”。裁剪或压缩后这些字段仍从 State 快照重建。
- 一旦状态进入 `blocked` 或 `failed`，调度和 Executor 都拒绝后续 handler 与权限询问。批次中剩余的每个 tool call 仍会逐一得到 `task_terminal` 结果并全部回灌；终态原因不会被后续记录改写。

## 关键流程

~~~text
一次执行 attempt 失败，创建 failure event
  -> recover(retry,
       caused_by_failure_id=<该 failure event 的 ID>,
       requested_attempt=<直接失败的 attempt ID>)
  -> schema / failure 关联 / retry 预算
       ├─拒绝 -> status=rejected，generation 不变 -> role=tool
       └─额度预留 -> 复用私有原始参数 -> PermissionGate
                    ├─拒绝：记录 rejected，generation 不变
                    └─接受：打开 generation，清除旧 verification
                         -> handler -> 新 attempt
                    -> role=tool
  -> 下一轮单独 verification
       ├─[exit=0]：当前 generation 获得证据
       └─失败：新的 validation failure，不能宣称已修复
~~~

运行真实任务时，应观察到 Structured State 的 generation 增加、旧 verification evidence 消失和 recovery notice 的独立验证提示。若动作被拒绝，应看到 status=rejected 且 generation 不变。

## 实现拆解

create_registry(state) 为每个 AgentState 新建 RecoveryRuntime 和 recover 工具，避免任务间共享恢复状态。agent loop 将 recover 按顺序处理；每个 tool call 都有对应 role=tool 结果，全部回灌后才进入下一轮。

RecoveryRuntime.recover() 的顺序是：检查 reason 与 rollback；adjust 校验目标参数；reserve_recovery(defer_generation=True) 原子预留额度；检查目标权限；activate_recovery() 打开 generation；ToolExecutor 执行目标；record_execution_result() 写入 attempt/failure。权限拒绝只更新已预留动作，不增加第二条拒绝记录，也不打开 generation。

相关源码：

- [recovery.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18.1/src/mini_agent/recovery.py)
- [tools/__init__.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18.1/src/mini_agent/tools/__init__.py)
- [tools/base.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18.1/src/mini_agent/tools/base.py)
- [agent.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18.1/src/mini_agent/agent.py)

## 本版特性、下一课与代码索引

v0.18 将失败后的选择限制为精确重试、显式调参、等待外部条件或保守停止。它不撤销副作用，也不把恢复 attempt 当作完成证据；每个已接受动作都要求当前 generation 的独立验证。

下一课将讨论 checkpoint 与有边界的 rollback：只有保存明确文件前镜像的场景才可能恢复内容，shell 和外部副作用仍不承诺可回滚。

- [recovery.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18.1/src/mini_agent/recovery.py)
- [state.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18.1/src/mini_agent/state.py)
- [tools/__init__.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18.1/src/mini_agent/tools/__init__.py)
- [tools/base.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18.1/src/mini_agent/tools/base.py)
- [agent.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18.1/src/mini_agent/agent.py)
