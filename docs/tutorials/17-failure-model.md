# 第 17 课：失败模型（v0.17）

上一课：[计划驱动执行](16-plan-driven-execution.md) · [教程总览](README.md) · 下一课：v0.18（规划中）

> 代码快照：`v0.17` · 相邻差异：`v0.16..v0.17` · 命令环境：Bash/zsh

> 本课只讲失败事实的记录与保守收口；自动恢复、重试和回滚属于后续版本。

## 本课目标

第 16 课已经用 `generation`（代次）绑定验证证据，但工具失败仍可能只剩一段文本。读完本课，你应能解释：

- `ExecutionResult`、`ExecutionAttempt`、`FailureEvent` 和 `ExecutionGeneration` 分别记录什么；
- 为什么参数错误、权限拒绝、handler 异常、超时、非零退出和验证失败不能混为一谈；
- 为什么获准的 `possible` 调用即使失败，也会让旧验证失效；
- 为什么全只读调用可以并发，而含潜在副作用的回合必须按顺序提交。

本课的主线是：**模型表达意图，Executor 观察事实，State 保存可追溯记录。**

## 上一版的问题

v0.16 的工具结果主要以文本进入 `AgentState`。文本能告诉模型“失败了”，却不能稳定回答 handler 是否进入、权限是否通过、环境是否可能被改变，以及失败对应哪一次调用。上下文裁剪后，单靠历史消息还可能丢掉这些关键信息。

v0.17 只建立事实层：它为每次调用分配 attempt，为失败生成带因果链接的 failure，并把验证证据绑定到当前 generation。它不替模型选择修复方案，也不声称能够恢复未知副作用。

## 前置条件与版本切换

需要 Python 3.10+。命令均为 Bash/zsh；运行时仍只使用标准库。建议先阅读第 16 课，然后查看相邻差异：

```bash
git checkout v0.16
git diff --stat v0.16..v0.17
git diff v0.16..v0.17 -- src/mini_agent/state.py src/mini_agent/tools/base.py src/mini_agent/agent.py src/mini_agent/context.py
git checkout v0.17
```

本文件是重写草稿；完整实现以声明的 v0.17 快照为准。

## 新增与改动文件

| 文件 | 变化 | 作用 |
|---|---|---|
| `src/mini_agent/state.py` | 增加 generation、attempt、failure、参数指纹和预算 | 保存不依赖消息历史的事实 |
| `src/mini_agent/tools/base.py` | `Tool.effect_class`、`ExecutionResult`、参数校验结果 | 在工具边界统一观察值 |
| `src/mini_agent/agent.py` | 按 effect class 选择并发/串行；拒绝混合 verification | 保持记录顺序和协议完整 |
| `src/mini_agent/context.py` | 渲染最近失败、代次和预算 | 裁剪或压缩后仍能诊断 |
| `src/mini_agent/config.py` | 失败、指纹和 repair cycle 上限 | 防止无界尝试 |

## 版本变更定位

图例：`[旧]` v0.16 已有，`[+]` v0.17 新增，`[~]` 修改，`[C]` 主要消费者，`[B]` 本版边界。

v0.16 的调用链是：

```text
[旧] agent loop -> [旧] ToolExecutor -> PermissionGate -> handler
                         -> 文本结果 -> [旧] AgentState.record_tool
                         -> role=tool 回灌 -> generation/verification 收口
```

v0.17 在工具边界和状态提交之间加入结构化事实：

```text
[旧] agent loop
  -> [~] 解析 tool calls
       -> 全 none effect：[+] 并发
       -> 含 possible：[+] 按原顺序串行
  -> [~] ToolExecutor.execute_result()
       -> 参数校验 -> 权限 -> [~] handler
       -> [+] ExecutionResult
       -> [+] AgentState.reserve_attempt()
       -> [+] record_execution_result()
            -> [+] ExecutionAttempt / FailureEvent / Generation
            -> [~] verification evidence 绑定 generation
  -> [旧] 每个调用回灌 role=tool
  -> [~] Structured State 展示最近失败和预算

[B] 不负责：recover 工具、自动 retry、参数修正、checkpoint、rollback、持久化。
```

入口是 `ToolExecutor.execute_result()`；主要消费者是 `AgentState`、`ContextManager` 和 agent loop。

## 核心概念与数据结构

### 1. `ExecutionResult`：工具边界的观察值

要解决的问题是“错误文本太晚、太粗”。`ExecutionResult` 同时保留 `permission`、`handler_admitted`、`outcome`、`effect_class`、耗时、退出码和 `error_kind`：

```python
@dataclass(frozen=True)
class ExecutionResult:
    tool: str
    arguments: dict[str, Any]
    permission: Literal["allowed", "denied", "not_checked"]
    handler_admitted: bool
    outcome: Literal["succeeded", "failed", "denied", "timeout", "invalid"]
    effect_class: EffectClass
    exit_code: int | None = None
    error_kind: str | None = None
```

顺序固定为“校验 -> 授权 -> 执行”。invalid 不询问权限，denied 不进入 handler；handler 异常在 Executor 边界转为 `failed`。每个结果仍须产生对应 `role=tool` 回灌。

### 2. `effect_class` 与 generation

`effect_class` 是“执行后环境是否可能变化”的声明，不是权限。`none` 表示只读；`possible` 表示不能安全证明没有副作用。`run_shell(purpose="verification")` 在本次调用中按 `none` 处理。

对获准的 `possible` 调用，handler 前原子预留 attempt，并开启下一个 generation：

```python
before = self._verification_generation
if effect_class == "possible":
    self._verification_generation += 1
    self.verification_evidence.clear()
return AttemptReservation(attempt_id, before, self._verification_generation)
```

handler 随后抛错也不会回退代次：它可能已经写入部分内容。参数错误或权限拒绝因 handler 未运行，不推进 generation。

### 3. Attempt、Failure 与因果链接

`ExecutionAttempt` 是一次调用的不可变记录，包含 canonical JSON 的 `arguments_hash`、脱敏参数摘要、generation、结果和 `failure_id`。`FailureEvent` 则描述失败事实：`category`、执行阶段、是否可重试、受影响文件和 `caused_by_attempt_id`。

基础分类如下：

| 观察结果 | category | retryable |
|---|---|---|
| 参数/协议不合法 | `protocol` | 否 |
| 权限明确拒绝 | `permission` | 否 |
| 超时 | `transient` | 是 |
| verification 非零或失败 | `validation` | 是 |
| possible handler 异常 | `unknown` | 否 |
| 其他执行失败（如 shell 非零） | `deterministic` | 否 |

因此 failure 可以沿 `failure_id -> caused_by_attempt_id -> generation_id` 回溯；不依赖时间顺序或自然语言猜测。敏感字段只进入脱敏摘要，原始参数不渲染到 Context。

### 4. 验证证据与终态

只有 `run_shell(purpose="verification")` 在当前 generation 返回 `[exit=0]`，才留下通过证据。任何获准的 possible 调用都会使旧证据失效。权限拒绝不会使证据失效，因为 handler 没有运行。

不可安全恢复的确定性、协议或权限错误收口为 `failed`；副作用范围未知收口为 `blocked`；可重试失败达到指纹预算，或验证失败耗尽 repair cycle，也会停止。`blocked` 表示当前运行不能安全继续，不等于成功。

## 为什么这样设计

把所有结果压成 `ok: bool` 最简单，但会丢失“是否获准”“是否进入 handler”和“是否可能改变环境”。本版选择结构化结果与不可变记录，代价是字段更多、只读 shell 也可能被保守地视为 `possible`。

并发只读调用可以提高吞吐；只要出现 possible，整回合串行则牺牲并发度，换取 generation 和 attempt 顺序稳定。混合 possible 与 verification 的回合直接把 verification 标为 invalid，避免测试在修改前或修改中运行。

本版刻意不实现自动重试、恢复动作、回滚和跨进程持久化；否则“记录事实”和“替用户决定如何修复”会混在同一层。

## 设计边界

- Executor 负责把 handler 异常转为错误结果；LLM 或 CLI 顶层异常仍由上层处理。
- 工具结果全部回灌后才进入下一轮；失败不会跳过协议消息。
- hash 用于稳定关联和计数，不是授权凭证；脱敏摘要也不能替代 PermissionGate。
- 本版无法判断 partial write 的实际影响，只能把未知副作用标为 `unknown` 并保守阻塞。
- 状态只存在当前进程；普通 history trimming 不会删除 State 中的 failure，但重启后不会自动恢复。

## 关键流程

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

`Tool.effect_for()` 将工具默认等级与 verification 特例分开；`ToolExecutor.execute_result()` 只负责观察和转换，不替 State 猜测原因；`AgentState.record_execution_result()` 才负责生成 attempt、failure、验证证据和终态。`ContextManager._render_state()` 读取 `snapshot()`，所以这些事实不依赖旧消息是否还在发送视图中。

完整实现：[`src/mini_agent/state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.17/src/mini_agent/state.py)、[`src/mini_agent/tools/base.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.17/src/mini_agent/tools/base.py)、[`src/mini_agent/agent.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.17/src/mini_agent/agent.py)、[`src/mini_agent/context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.17/src/mini_agent/context.py)。

## 本版特性、下一课与代码索引

v0.17 把“失败”从一段文本变成可观察、可计数、可追溯的事实，并让验证证据严格属于某个 generation。下一课将讨论如何在这些事实和预算约束下选择有限恢复动作。

- [`src/mini_agent/config.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.17/src/mini_agent/config.py)
- [`src/mini_agent/state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.17/src/mini_agent/state.py)
- [`src/mini_agent/tools/base.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.17/src/mini_agent/tools/base.py)
- [`src/mini_agent/agent.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.17/src/mini_agent/agent.py)
- [`src/mini_agent/context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.17/src/mini_agent/context.py)
