# 第 17 课：失败模型（v0.17）

> 稳定版本 v0.17 | [教程总览](README.md) | [上一课：计划驱动执行（Plan-driven Execution）](16-plan-driven-execution.md) | 下一课：规划中
>
> 代码快照：`v0.17` · 相邻差异：`v0.16..v0.17` · 命令环境：Bash/zsh

## 本课目标

第 16 课已经能让 agent 在修改后运行验证，并用 `generation` 防止它拿旧测试结果冒充新结果。不过，当工具出错时，当时的状态仍然比较粗糙：一条错误文本很难回答下面这些问题：

- 这次调用是否真的进入了工具 handler？
- 它是在权限检查时被拒绝，还是执行到一半失败？
- 它可能已经改动环境了吗？之前的验证结果还能用吗？
- 这次失败和哪一次调用有关，运行时最后为什么停在 `blocked` 或 `failed`？

v0.17 为这些问题建立最小的“失败事实模型”（Failure Model）。它不替模型判断怎么修复，也不承诺能够恢复现场；它的工作是把已经发生的事情记录清楚，并在风险不明时保守停下。

读完本课，你应该能够：

- 区分 `ExecutionResult`、`ExecutionAttempt`、`FailureEvent` 和 `generation` 各自记录什么。
- 解释为什么允许执行的 `possible` 工具即使最终报错，也会先推进 generation。
- 看懂权限拒绝、参数错误、超时、非零退出和验证失败会如何分类。
- 解释为何全只读工具调用可以并发，而同一回合出现可能副作用时必须串行。
- 运行不需要 API Key 或网络的示例和测试，观察失败记录与终态。

本课的原则可以概括为：**模型给出意图，工具给出结果，State 保存可核对的事实。**

## 前置条件与版本切换

需要 Python 3.10+；运行时只使用标准库。建议先阅读第 16 课，了解 Todo、verification 和 generation 的基本含义。

可以用下面的命令查看本版相对上一版的改动。最后切回 v0.17 再运行示例：

```bash
git checkout v0.16
git diff --stat v0.16..v0.17
git diff v0.16..v0.17 -- src/mini_agent/state.py src/mini_agent/tools/base.py src/mini_agent/agent.py src/mini_agent/context.py
git checkout v0.17
```

## 新增与改动文件

| 文件 | 相对 v0.16 的变化 | 作用 |
|---|---|---|
| `src/mini_agent/state.py` | 增加 generation、attempt、failure、脱敏参数摘要和预算状态 | 保存不依赖消息历史的执行事实 |
| `src/mini_agent/tools/base.py` | Tool 增加 `effect_class`；Executor 返回 `ExecutionResult` 并校验参数 | 将权限、是否进入 handler、耗时、退出码和错误种类结构化 |
| `src/mini_agent/agent.py` | 按工具 effect class 选择串行或并发，并拒绝混合 verification | 保持 generation 和工具结果顺序确定 |
| `src/mini_agent/context.py` | Structured State 显示最近失败、generation、预算和恢复提示 | 历史裁剪或压缩后，关键事实仍可见 |
| `src/mini_agent/config.py` | 增加失败、重复调用和 repair cycle 的上限配置 | 为后续受限恢复保留可观察的预算边界 |
| `tests/test_executor.py`、`tests/test_loop.py`、`tests/test_context.py` | 增加结构化结果、调度和脱敏测试 | 覆盖本课的关键不变量 |

## 为什么需要本版

假设模型请求写文件，权限通过后工具开始工作，但写到一半抛出异常。返回给模型的文本可能只是“工具执行失败”。这条文本不能说明文件是否已经创建，也不能说明旧的测试是否仍然可信。

因此，本版不只保存“成功/失败”这个布尔值，而是把一次调用拆成几类事实：

| 名称 | 通俗理解 | 关键内容 |
|---|---|---|
| `ExecutionResult` | Executor 刚刚观察到的原始结果 | 权限是否允许、handler 是否已进入、结果、耗时、退出码、错误种类 |
| `ExecutionAttempt` | 写入 State 的一次调用记录 | 工具名、参数 hash、所属 generation、结果和对应 failure ID |
| `FailureEvent` | 从失败 attempt 推导出的失败事实 | 失败类别、阶段、是否可重试、直接原因 attempt |
| `ExecutionGeneration` | “环境可能已变化”的版本号 | 新版本由哪次 attempt 开启 |
| `VerificationEvidence` | 对某个版本的检查证据 | 命令、退出码、通过/失败和 generation ID |

它们的职责不同。`ExecutionResult` 是工具边界的观察值；State 把它固定为 `ExecutionAttempt`；失败时再生成 `FailureEvent`。模型可以阅读这些事实，但不能通过 `update_todo` 伪造一次执行成功、清除失败记录或修改参数 hash。

### generation：把“可能变了”当作一个新版本

generation 不是 Git commit，也不是文件版本号。它只是任务运行期间的一个递增编号，用来表示“从这里开始，之前的验证证据不能自动沿用”。每个任务从 generation 0 开始。

文件写入、文件编辑和 `run_shell(purpose="execution")` 都属于 `possible` effect：运行时无法可靠证明它们没有改变环境。只读工具和 `run_shell(purpose="verification")` 属于 `none` effect。

对一个已经通过权限检查的 `possible` 调用，运行时会在调用 handler **之前**完成三件事：

1. 预留稳定的 attempt ID。
2. 开启下一个 generation，并记录它由这次 attempt 开启。
3. 清空旧 verification evidence，标记需要重新验证。

即使 handler 随后异常，这个 generation 也不会回退。因为“handler 报错”不等于“它什么也没改”。这是一个刻意保守的选择：宁可要求多做一次验证，也不能把旧验证当作仍然有效。

相反，参数校验失败或权限拒绝时 handler 根本没有被调用，所以不会推进 generation。

## 关键流程

下面是一条可能有副作用的工具调用路径。箭头旁的词表示记录发生的位置，而不是模型需要手工填写的字段：

```text
模型发起 tool call
  -> 校验调用格式和参数 schema
       -> 不合法：ExecutionAttempt(outcome=invalid) -> FailureEvent(protocol)
  -> PermissionGate
       -> 拒绝：ExecutionAttempt(outcome=denied) -> FailureEvent(permission)
  -> 预留 attempt；若 effect=possible，先开启新的 generation
  -> 调用 handler
  -> ExecutionResult
  -> State 写入 ExecutionAttempt
       -> 失败时写入 FailureEvent
       -> verification 时写入 VerificationEvidence
  -> 以 role=tool 回灌本次结果给模型
```

无论成功还是失败，只要模型发起了有效的 tool call，agent loop 都会为它回灌对应的 `role=tool` 消息。这样模型能基于实际结果继续工作，也保持此前课程建立的工具调用协议。

### 同一回合的并发规则

模型可以在一个回复中发起多个工具调用。v0.17 的规则是：

- 如果所有调用都是 `none` effect，它们可以并发执行，提高读取、计算等任务的效率。
- 只要其中有一个 `possible` effect，整回合就按模型给出的顺序串行执行和记录。这样 generation 和 attempt ID 的顺序不会因为线程完成得快慢而变化。
- 同一回合不能同时做 `possible` effect 和 `run_shell(purpose="verification")`。verification 会收到 `invalid` 结果，handler 不会执行。

最后一条很重要。例如“写文件、运行测试”放在同一回合，测试可能在写入之前或中间运行，不能作为新 generation 的独立证据。模型应先完成写入，等待工具结果回灌后的下一轮，再单独请求 verification。

## 实现拆解

### 1. Tool 声明副作用等级

`Tool` 有一个 `effect_class` 字段，取值只有 `none` 和 `possible`。注册表会拒绝其他取值。`run_shell` 有一个特殊规则：当参数 `purpose` 是 `verification` 时，本次调用按 `none` 处理；其他情况按工具声明的等级处理。

这不是权限系统。`effect_class` 只描述“执行后环境是否可能变化”，PermissionGate 仍然单独决定是否允许执行。

### 2. 先校验，再授权，再执行

`ToolExecutor.execute_result()` 先做参数校验：必填字段、默认值、基本 JSON 类型、枚举值和 `maxItems`。它使用项目中工具 schema 所需的那个小型 JSON Schema 子集，不试图实现完整 JSON Schema。

校验失败会得到 `outcome="invalid"` 和 `error_kind="invalid_arguments"`，并且不会询问权限、更不会调用 handler。通过校验后才进入 PermissionGate：

- 被拒绝：`permission="denied"`、`handler_admitted=False`、`outcome="denied"`。
- 获准：`permission="allowed"`、`handler_admitted=True`；若是 `possible`，此时先预留 generation。

handler 返回后，Executor 会识别 `run_shell` 的两类特殊输出：`[timeout]` 记为 `timeout`，`[exit=N]` 且 `N != 0` 记为 `failed`，同时保留 `exit_code`。普通 handler 异常则记为 `failed` 和 `handler_exception`。

### 3. State 负责分类和因果链接

`AgentState.record_execution_result()` 是把 Executor 观察结果写入长期任务状态的唯一入口。它会计算 canonical JSON 的 SHA-256 `arguments_hash`：对象键的排列顺序不影响 hash，但数字 `1` 与字符串 `"1"` 不会被混为一谈。

State 不在 Structured State 中渲染原始参数。它只保留 hash 和脱敏摘要：名称含有 `key`、`token`、`secret`、`password`、`credential` 或 `authorization` 的字段会显示为 `<redacted>`；文件内容、编辑文本和命令只保留类型和长度。这可以让模型看到“这是哪一类调用”，又避免把敏感内容或很长的命令反复注入上下文。

一次失败会按下面的规则产生 `FailureEvent`：

| 观察到的结果 | `FailureEvent.category` | 是否标为可重试 |
|---|---|---|
| 调用格式或参数不合法 | `protocol` | 否 |
| PermissionGate 明确拒绝 | `permission` | 否 |
| 工具超时 | `transient` | 是 |
| verification 返回非零或失败 | `validation` | 是 |
| `possible` handler 抛异常，副作用范围不明 | `unknown` | 否 |
| 其他执行失败，例如 shell 非零退出 | `deterministic` | 否 |

每个 `FailureEvent` 都带有 `caused_by_attempt_id`，每个可能副作用开启的 generation 都带有 `opened_by_attempt_id`。因此不需要依赖日志时间或自然语言猜测“这条失败来自哪里”。

### 4. 验证证据仍然绑定 generation

v0.16 的验证规则仍然有效：只有 `run_shell(purpose="verification")` 返回 `[exit=0]`，才会留下 passed evidence；它必须属于当前 generation。任何已经获准执行的 `possible` 调用都会让旧 evidence 失效。

验证失败现在还会生成 `FailureEvent(category="validation", phase="verify")`。它说明“检查没有通过”，不代表运行时知道该怎样修复。模型需要阅读工具输出、调整 Todo 或下一步操作；v0.17 不会自动修改命令、重试或回滚。

### 5. 终态和预算

State 会保留最后一次 failure，并在以下情况下保守收口：

- `permission`、`protocol` 或 `deterministic`：状态为 `failed`，因为当前运行时没有安全的自动恢复路径。
- `unknown`：状态为 `blocked`，因为可能副作用的范围不清楚，需要外部诊断。
- 对同一工具和参数 hash 的可重试失败，达到 fingerprint 尝试上限：状态为 `blocked`。
- verification 连续失败并耗尽 repair cycle 上限：状态为 `failed`。

这些上限来自 `config.py`：`MAX_ATTEMPT_FINGERPRINTS`、`MAX_REPAIR_CYCLES` 等。Structured State 会显示当前 generation、最近失败、终止原因和预算信息，因而即使普通 history 被 trimming 或 compaction，模型仍能看到关键事实。

`RecoveryAction` 数据结构在本版已经预留，是为了让后续版本能沿用相同的因果链；v0.17 不提供 `recover` 工具，不自动 retry，也不做 checkpoint 或 rollback。

## 最小离线示例

下面的示例人为制造一个“可能写入后抛异常”的工具。它不访问网络，也不会写文件。重点是观察 generation 在 handler 运行前已经从 0 变成 1，以及失败被保存为 `unknown`：

```bash
PYTHONPATH=src python - <<'PY'
from mini_agent.permission import ALLOW, PermissionGate, PermissionPolicy
from mini_agent.state import AgentState
from mini_agent.tools.base import Tool, ToolExecutor, ToolRegistry

state = AgentState()
state.begin_task("演示失败记录")

def partial_write(path):
    print("handler 看到的 generation:", state.current_generation_id)
    raise RuntimeError("写入中断")

registry = ToolRegistry()
registry.register(Tool(
    "demo_write", "模拟写文件",
    {"type": "object", "properties": {"path": {"type": "string"}},
     "required": ["path"]},
    partial_write,
    effect_class="possible",
))
executor = ToolExecutor(
    registry,
    PermissionGate(PermissionPolicy({"demo_write": ALLOW})),
)

result = executor.execute_result("demo_write", {"path": "demo.txt"}, state)
state.record_execution_result(result)
snapshot = state.snapshot()

print("outcome:", result.outcome)
print("generation:", snapshot["current_generation_id"])
print("attempt:", snapshot["attempts"][0]["attempt_id"])
print("failure:", snapshot["latest_failure"])
print("status:", snapshot["status"])
PY
```

输出中的关键部分应当类似：

```text
handler 看到的 generation: 1
outcome: failed
generation: 1
attempt: a-1
failure: {... 'category': 'unknown', 'caused_by_attempt_id': 'a-1' ...}
status: blocked
```

再看一个不会推进 generation 的例子。这里工具因缺少必填参数而在 schema 校验阶段失败，handler 没有机会执行：

```bash
PYTHONPATH=src python - <<'PY'
from mini_agent.permission import ALLOW, PermissionGate, PermissionPolicy
from mini_agent.state import AgentState
from mini_agent.tools.base import Tool, ToolExecutor, ToolRegistry

state = AgentState()
state.begin_task("演示参数错误")
registry = ToolRegistry()
registry.register(Tool(
    "demo_write", "模拟写文件",
    {"type": "object", "properties": {"path": {"type": "string"}},
     "required": ["path"]},
    lambda path: "不会被调用",
    effect_class="possible",
))
executor = ToolExecutor(
    registry,
    PermissionGate(PermissionPolicy({"demo_write": ALLOW})),
)

result = executor.execute_result("demo_write", {}, state)
state.record_execution_result(result)
print(result.outcome, result.handler_admitted)
print(state.snapshot()["current_generation_id"])
print(state.snapshot()["latest_failure"]["category"])
PY
```

它会输出 `invalid False`、`0` 和 `protocol`。同样地，权限拒绝会记录为 `permission`，但不会开启新 generation，因为 handler 从未进入。

## 设计选择与边界

- **记录事实，不假装理解原因**：例如 handler 异常后的副作用范围通常无法从异常文本推断，因此归为 `unknown` 并 `blocked`，而不是武断地说“可以重试”。
- **先推进再执行**：对可能副作用的调用，即使失败也让旧验证过期。代价是部分纯只读 shell 命令会被保守对待，但不会误用旧证据。
- **参数有指纹但不裸露**：hash 用于稳定关联和重复尝试计数，脱敏摘要用于上下文展示；它们不是密码学授权，也不能替代权限检查。
- **并发服从确定性**：只读调用可以并发；含可能副作用的回合牺牲一点并发度，换来稳定的因果顺序和可复现的状态。
- **没有自动恢复**：v0.17 不会自动重试超时、修正参数、询问用户、保存检查点或回滚文件。这些都属于后续版本要在明确规则下处理的行为。
- **状态只在当前进程内**：失败模型让一次运行中的事实可审计，但本版尚不持久化，也不能跨进程恢复任务。

## 测试与验收

先运行本课直接相关的离线测试：

```bash
PYTHONPATH=src python -m pytest -q tests/test_executor.py tests/test_loop.py tests/test_context.py
PYTHONPATH=src python tests/test_executor.py
PYTHONPATH=src python tests/test_loop.py
PYTHONPATH=src python tests/test_context.py
```

然后按项目约定运行完整验证：

```bash
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python scripts/check_tutorials.py
```

验收时重点确认：

- 获准的 `possible` 调用在 handler 前推进 generation；handler 异常后 attempt、failure 和 `blocked` 状态都存在。
- 参数不合法和权限拒绝不调用 handler，也不推进 generation。
- 同一份参数对象无论键顺序如何，hash 相同；数字和字符串等不同 JSON 类型不会被混淆。
- 全 `none` 回合可以并发，但 State 按模型顺序提交结果；混入 `possible` 时整回合串行。
- 与 `possible` 同回合的 verification 只得到 `invalid` 工具结果，不执行 verification handler。
- Structured State 会显示最近 failure、generation 和预算；在 compaction 后仍不泄露敏感参数。

## 本版特性、下一课与代码索引

v0.17 的独有能力是把每次工具执行记录成有因果关系的 attempt 和 failure，同时把验证证据绑定到可能变化的环境版本。它为后续的受限恢复提供数据基础，但当前版本只负责记录、展示和保守收口。

- [`src/mini_agent/state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.17/src/mini_agent/state.py)
- [`src/mini_agent/tools/base.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.17/src/mini_agent/tools/base.py)
- [`src/mini_agent/agent.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.17/src/mini_agent/agent.py)
- [`src/mini_agent/context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.17/src/mini_agent/context.py)
- [`tests/test_executor.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.17/tests/test_executor.py)
- [`tests/test_loop.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.17/tests/test_loop.py)
- [`tests/test_context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.17/tests/test_context.py)
