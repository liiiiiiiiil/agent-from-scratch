# 第 33 课：崩溃后的调用交接（v0.33）

上一课：[把工具调用的关键边界写下来](32-durable-tool-boundaries.md) · [教程总览](README.md) · 下一课：[第 34 课：最小受控子代理委派](34-minimal-delegation.md)

> 代码快照：`v0.33` · 相邻差异：`v0.32..v0.33` · 命令环境：Bash/zsh

> 运行要求：Python 3.10+。本课处理 `active + schema 3 + pending tool_boundary`；它不承诺自动恢复外部副作用。

## 本课目标

上一课让磁盘知道工具调用停在什么边界，但一个 pending call 仍然可能有两种完全不同的含义：程序可能还没进入 handler，也可能已经开始写文件、启动 shell 或向 stdin 写了一部分内容。

崩溃后最危险的做法，是把“没有结果”当成“没有执行”，然后自动再做一次。这样可能重复写文件、重复启动进程或重复发送输入。本课建立一条保守的交接流程。读完本课，你应该能解释：

- `not_executed` 和 `uncertain_side_effect` 的区别；
- 为什么源 session 保持不变，并且要派生新的 session；
- 为什么不确定调用不能自动 replay；
- `/resolve ... investigate|continue|block` 各自做什么；
- 为什么恢复后还要重新规划、重新授权和独立验证。

## 上一版的问题：没有结果不等于没有执行

v0.32 已经记录了 `handler_admitted`。但如果进程在 handler 运行期间崩溃，session 可能只有“准入已提交”，没有成功或失败结果。对于 `write_file`、`run_shell`、启动/控制进程和 `write_process`，外部世界可能已经改变；即使文件表面没变、旧 PID 看起来消失，也不能从这些现象推出“肯定没有执行”。

因此 v0.33 不尝试猜测过去发生了什么，而是把不确定性写成任务事实，交给新的 CLI 和用户逐项处理。只有明确知道 handler 在崩溃前没有被准入，才能生成确定的“未执行”结果。

## 前置条件与版本切换

需要基础 Python、命令行和第 32 课的 `tool_boundary`、`handler_admitted`、`effect_class` 概念。命令使用 Bash/zsh。

```bash
git checkout v0.32
git diff --stat v0.32..v0.33
git diff v0.32..v0.33 -- src/mini_agent/state.py src/mini_agent/session.py src/mini_agent/resume.py src/mini_agent/context.py src/mini_agent/prompt.py src/mini_agent/tools/base.py src/mini_agent/trace.py src/mini_agent/__main__.py
git checkout v0.33
```

## 新增与改动文件

本版增加的不是一个“重试按钮”，而是一套识别、分支、交接和重新验证的流程。

| 文件 | 变化 | 作用 |
|---|---|---|
| `src/mini_agent/state.py` | 增加 State format 2、recovery、issue 和决定 | 保存不确定事实，并阻止错误的完成收口 |
| `src/mini_agent/resume.py` | 分流 clean resume 和 crash recovery | 生成恢复候选、分类调用并派生 session |
| `src/mini_agent/session.py` | 增加 claim sidecar 和派生提交 | 保留源文件，并确保同一源只派生一次 |
| `src/mini_agent/context.py`、`prompt.py` | 增加恢复摘要和模型规则 | 说明 issue 与下一动作，但不泄露敏感参数 |
| `src/mini_agent/__main__.py`、`tools/base.py` | 增加 `/resolve` 和 crash gate | 用户决定只由 CLI 记录，调查阶段只允许无副作用观察 |
| `src/mini_agent/trace.py` | 增加恢复因果校验 | 只读检查 recovery → issue → decision → trigger 链 |

## 版本变更定位

图例：`[旧]` v0.32 已有，`[+]` v0.33 新增，`[~]` v0.33 修改，`[C]` 主要消费者，`[B]` 本课边界。

v0.32 遇到半轮时只能停在这里：

```text
[旧] assistant tool_calls
  -> [旧] pending tool_boundary
  -> [旧] handler admission（可能已经发生）
  -> [B] 只记录中断，不知道能否安全交接
```

v0.33 在 pending 边界后加入不覆盖源文件的恢复分支：

```text
[旧] active schema 3 + pending boundary
  -> [+] prepare_resume 分类 pending calls
       |
       +-- handler_admitted=false
       |      -> [+] not_executed
       |      -> [+] interrupted_before_handler tool result
       |
       +-- handler_admitted=true
              -> [+] uncertain_state_or_result / uncertain_side_effect issue
  -> [+] 保留源 session，claim 一次并派生新 session
  -> [+] 新 generation，清除当前 verification
  -> [C] Structured State / Context 显示待处理 issue
       |
       +-- [C] 只读调查
       +-- [C] 用户 continue / block
  -> [+] 全部 issue 结算后触发 crash_recovery replan
  -> [C] 重新授权和独立 verification

[B] 不重放旧 handler，不把用户普通文本当作 continue，不用旧 PID 控制进程
```

## 核心概念与数据结构

### 1. 先区分“确定没进 handler”和“不知道发生了什么”

恢复分类只使用 durable boundary 中已经写下的准入事实和 effect class：

| 条件 | 分类 | 新 session 中的结果 |
|---|---|---|
| `handler_admitted=false` | `not_executed` | `interrupted_before_handler`，明确记为未执行 |
| `handler_admitted=true` 且 `effect_class=none` | `uncertain_state_or_result` | 不声称结果或 State 变化可用 |
| `handler_admitted=true` 且 `effect_class=possible` | `uncertain_side_effect` | 不声称成功，也不重放 |

这里的 issue 是“必须被处理的未决事实”。例如 `write_file` 已准入但没有结果，就形成一个不确定副作用 issue；`read_file` 已准入但结果没提交，也不能直接把读到的内容猜回来。

未进入 handler 的调用可以安全地合成一条工具结果：

```json
{
  "status": "interrupted_before_handler",
  "message": "调用在崩溃前未进入 handler，恢复时明确记为未执行。"
}
```

不确定调用的结果则明确写成 `status: "uncertain"`，它不是旧 handler 的成功返回值。

### 2. 恢复时保留源 session，派生一个新分支

`--resume` 先判断 session 类型：完整 `clean` 安全点走 v0.31 的 safe resume；只有 schema 3 的 active pending boundary 进入 crash recovery。准备阶段不调用 LLM、PermissionGate 或 handler。

恢复不会覆盖事故现场。源 session 保持只读，新的 session 保存恢复后的 State、Context、合成的 tool result 和恢复记录。`source_session_id` 与 `source_integrity` 会写入恢复事实；私有的 claim sidecar 记录 `preparing` / `committed` 阶段，防止同一个源完整性被重复派生。

如果恢复前发现工作区清单发生变化，或 session 记录了旧后台进程、未完成的 stdin 写入，也会把这些事实纳入恢复交接；它们是待调查线索，不是“已经退出”或“没有副作用”的证明。

代码层面，候选和正式派生仍是两个动作：

```python
candidate = prepare_resume(store, source_session_id, workspace_root)
runtime = candidate.claim()
```

如果派生文件写入中途失败，下一次尝试沿用同一个 derived ID 继续；如果 claim 已经 `committed`，再次处理同一个源会报告已经存在的派生分支，而不是再创建一套事件。

### 3. `/resolve` 把风险判断交给用户

恢复有未结算 issue 时，普通输入、副作用工具、verification 和计划提交都会被 gate 拦截。CLI 提供三种明确决定：

```text
/resolve <issue_id> investigate <反馈>
/resolve <issue_id> continue <反馈>
/resolve <issue_id> block <反馈>
```

`investigate` 只记录用户决定，然后允许一次受限的只读调查；调查可以读取文件、列目录、搜索或计算，但不能写文件、运行 shell、控制进程或做 verification。`continue` 不是“重放旧调用”，而是用户在调查后接受继续处理该 issue；它要求当前恢复 generation 已有成功、获准、`effect_class=none` 的调查 attempt。`block` 则把任务保留在用户决定的阻塞终态。

用户的 `continue` 必须由 CLI 记录，模型不能通过普通文本伪造。所有 issue 都 continue 后，任务仍不能沿着旧工具回合直接跑，而是进入 `crash_recovery` replan：先提交或复核新计划，再重新走 PermissionGate 和独立 verification。

### 4. 恢复后的验证必须重新开始

恢复会开启新的 generation，清除当前 `verification_evidence`；旧的 `verification_history`、FailureEvent、RecoveryAction 和计划历史仍保留供审计。这样 Trace 可以回答“崩溃前发生过什么”，而完成判定只使用恢复后的新证据。

恢复摘要会显示 issue ID、工具、分类、准入状态、公开原因和合法下一步，但不会把原始工具参数、shell 命令正文或 stdin 正文重新放进 State、Trace 或提示词。

## 为什么这样设计

“没有结果就重试”看起来简单，却无法区分 handler 尚未开始和副作用已经发生。尤其是写文件、shell、进程和 stdin，重复一次可能比漏做一次更危险。保守分类会增加一次调查和用户决定，但不会伪造成功，也不会悄悄重复外部动作。

派生 session 会留下源文件、claim 和新分支三个可审计对象，存储成本更高；收益是事故证据不被覆盖，重复恢复不会产生第二组不确定事件。用户的 `continue` 也不直接恢复旧调用，而是触发新计划和新验证，因为用户接受风险不等于旧外部动作的结果已经知道。

## 设计边界

本版支持：读取 active schema 3 的 pending boundary；确定未准入的调用记为 `not_executed`；已准入调用记为相应的 uncertain issue；源 session 保持不变；逐 issue 调查和用户决定；所有 issue 结算后重新规划和验证。

本版不支持：重连旧 `Popen`、PID、管道或 stdin 线程；根据文件未变化、命令超时或 PID 消失推断“肯定没执行”；自动回滚或 replay 副作用；继承旧 PermissionGate 的运行时授权；把 claim sidecar 当作 Trace 输入。无法证明安全时，任务停在可解释的交接状态。

## 关键流程

```text
--resume <source_session_id>
  -> 校验 session 和 pending boundary
  -> 观察工作区（只作为线索，不证明因果）
  -> 按 handler_admitted / effect_class 创建 issue
  -> 派生新 session，源文件保持不变
  -> 未执行调用合成明确结果；不确定调用合成 uncertain 结果
  -> 显示 issue 和 /resolve 用法

/resolve issue-1 investigate 读取相关文件
  -> 只读调查回合
  -> /resolve issue-1 continue 调查后确认可以继续

所有 issue 都 continue
  -> crash_recovery replan
  -> 重新授权
  -> 独立 verification

任一 issue block
  -> 任务保持 blocked，不自动恢复旧调用
```

## 实现拆解

[`src/mini_agent/resume.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.33/src/mini_agent/resume.py) 在 `prepare_resume()` 中分流 safe point 和 crash recovery，并在 `claim()` 前复核工作区观察；它根据 pending call 的准入状态构造恢复结果。[`src/mini_agent/session.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.33/src/mini_agent/session.py) 负责 claim、sidecar、派生 envelope 和两阶段耐久提交。

[`src/mini_agent/state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.33/src/mini_agent/state.py) 保存 recovery、issue、decision、新 generation 和 `crash_recovery` trigger；`crash_recovery_gate()` 在 issue 未结算时把工具能力收窄到只读观察。[`src/mini_agent/tools/base.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.33/src/mini_agent/tools/base.py) 在 planning、repair 和 permission 之前执行这个 gate。

[`src/mini_agent/context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.33/src/mini_agent/context.py) 和 [`src/mini_agent/prompt.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.33/src/mini_agent/prompt.py) 给模型注入受保护的恢复摘要；[`src/mini_agent/__main__.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.33/src/mini_agent/__main__.py) 只由 CLI 解析和记录 `/resolve`；[`src/mini_agent/trace.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.33/src/mini_agent/trace.py) 只读校验 recovery → issue → decision → trigger 因果链。

## 运行与观察

下面命令使用 Bash/zsh。只有在已有 active pending boundary 时，才会进入本课的 crash recovery 分支：

```bash
PYTHONPATH=src python -m mini_agent --resume <source_session_id>
```

预期看到源 session ID、派生 session ID、调用分类、工作区观察和 `/resolve` 用法。`not_executed` 调用会自动补入明确的 `interrupted_before_handler` 结果；`uncertain_side_effect` 会阻止普通输入和副作用工具。

执行 `/resolve <issue_id> investigate <反馈>` 后，只读回合可以运行；在调查成功前直接执行 `continue` 会被拒绝。所有 issue 结算后，下一步应是引用 `crash_recovery` trigger 的新计划和独立 verification，而不是继续旧的工具调用。源 session 的文件内容应保持不变。

## 本版特性、下一课与代码索引

本版新增的是崩溃后的可解释交接：分类不完整调用、派生 session、逐 issue 用户决定、只读调查 gate、新 generation 和重新验证。它不是无限可靠的外部事务恢复，也不承诺无人值守地修复文件、shell、进程或 stdin 的副作用。

下一课：[第 34 课：最小受控子代理委派](34-minimal-delegation.md)。

核心代码索引：[`state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.33/src/mini_agent/state.py)、[`session.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.33/src/mini_agent/session.py)、[`resume.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.33/src/mini_agent/resume.py)、[`__main__.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.33/src/mini_agent/__main__.py)。
