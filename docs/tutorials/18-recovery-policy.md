# 第 18 课：受限恢复策略（Recovery Policy，v0.18）

上一课：[失败模型](17-failure-model.md) · [教程总览](README.md) · 下一课：v0.19（规划中）

> 代码快照：`v0.18` · 相邻差异：`v0.17..v0.18` · 命令环境：Bash/zsh

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

## 前置条件与版本切换

建议先阅读第 17 课。以下命令适用于 Bash/zsh：

~~~bash
git checkout v0.17
git diff --stat v0.17..v0.18
git diff v0.17..v0.18 -- src/mini_agent/recovery.py src/mini_agent/state.py src/mini_agent/tools/__init__.py src/mini_agent/agent.py
git checkout v0.18
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
       -> [+] AgentState.reserve_recovery()
            -> 校验 failure、动作和预算，打开后继 generation，清除旧验证
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

schema 通过后还要确认 failure 存在、任务未终态、动作组合合法且预算有余量。被拒绝的动作记录为 status="rejected" 的 RecoveryAction，不创建后继 generation。完整实现见 [recovery.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18/src/mini_agent/recovery.py)。

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

完整状态实现见 [state.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18/src/mini_agent/state.py)。

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

## 关键流程

~~~text
一次执行 attempt 失败，创建 failure event
  -> recover(retry,
       caused_by_failure_id=<该 failure event 的 ID>,
       requested_attempt=<直接失败的 attempt ID>)
  -> schema / failure 关联 / retry 预算
       ├─拒绝 -> status=rejected，generation 不变 -> role=tool
       └─接受 -> recovery action 预留下一个 generation，清除旧 verification
                    -> 复用私有原始参数
                    -> PermissionGate -> handler -> 新 attempt
                    -> role=tool
  -> 下一轮单独 verification
       ├─[exit=0]：当前 generation 获得证据
       └─失败：新的 validation failure，不能宣称已修复
~~~

运行真实任务时，应观察到 Structured State 的 generation 增加、旧 verification evidence 消失和 recovery notice 的独立验证提示。若动作被拒绝，应看到 status=rejected 且 generation 不变。

## 实现拆解

create_registry(state) 为每个 AgentState 新建 RecoveryRuntime 和 recover 工具，避免任务间共享恢复状态。agent loop 将 recover 按顺序处理；每个 tool call 都有对应 role=tool 结果，全部回灌后才进入下一轮。

RecoveryRuntime.recover() 的顺序是：检查 reason 与 rollback；adjust 先校验目标参数；reserve_recovery() 原子校验并预留；retry/adjust 交给 ToolExecutor；record_execution_result() 写入 attempt/failure。这样无效 adjust 不会被误记为副作用，预留的 generation 也不会被重复推进。

相关源码：

- [recovery.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18/src/mini_agent/recovery.py)
- [tools/__init__.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18/src/mini_agent/tools/__init__.py)
- [tools/base.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18/src/mini_agent/tools/base.py)
- [agent.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18/src/mini_agent/agent.py)

## 本版特性、下一课与代码索引

v0.18 将失败后的选择限制为精确重试、显式调参、等待外部条件或保守停止。它不撤销副作用，也不把恢复 attempt 当作完成证据；每个已接受动作都要求当前 generation 的独立验证。

下一课将讨论 checkpoint 与有边界的 rollback：只有保存明确文件前镜像的场景才可能恢复内容，shell 和外部副作用仍不承诺可回滚。

- [recovery.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18/src/mini_agent/recovery.py)
- [state.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18/src/mini_agent/state.py)
- [tools/__init__.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18/src/mini_agent/tools/__init__.py)
- [tools/base.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18/src/mini_agent/tools/base.py)
- [agent.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.18/src/mini_agent/agent.py)
