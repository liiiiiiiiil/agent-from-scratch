# 第 33 课：崩溃恢复与不确定副作用交接（v0.33）

上一课：[持久化工具执行边界](32-durable-tool-boundaries.md) · [教程总览](README.md) · 下一课：按需追加

> 代码快照：`v0.33` · 相邻差异：`v0.32..v0.33` · 命令环境：Bash/zsh

> 运行要求：Python 3.10+。本课的 `v0.33` tag 由仓库维护者在交付后手动创建；助手不创建、移动或推送 tag。

## 本课目标

上一课已经把工具调用写成 durable boundary：磁盘能知道调用停在 pending、准入还是 committed。但 pending 只说明“结果没有完整落盘”，并不能回答 handler 有没有开始。比如文件可能已经写完，shell 可能已经启动子进程，管道 stdin 可能只写入了一部分。

本课建立一个保守的交接流程。新的 CLI 读取 `active + schema 3 + pending tool_boundary` 时，不覆盖源 session，也不重放调用；它派生新的 session，把调用分类为“未执行”或“不确定”，再让用户逐项决定是否调查、继续还是阻塞。

## 前置条件

需要理解第 32 课的 `tool_boundary`、`handler_admitted` 和 schema 3。先核对本课相对上一课的真实差异，再阅读代码：

```bash
git diff --stat v0.32..v0.33
git diff v0.32..v0.33 -- src/mini_agent/state.py src/mini_agent/session.py src/mini_agent/resume.py
git checkout v0.32
git checkout v0.33
PYTHONPATH=src python -m pytest -q tests/test_crash_recovery_v033.py
```

这些命令的重点不是切换分支本身，而是确认本课的行为来自固定快照。测试完成后回到自己的工作分支继续学习。

## 新增与改动文件

| 文件 | 变化 | 作用 |
|---|---|---|
| `state.py` | 增加 State format 2、恢复记录、issue、用户决定和 crash gate | 保存不确定事实，阻止错误的副作用调用和完成收口 |
| `resume.py` | 分流 clean safe point 与 pending crash recovery | 重建新运行时、生成恢复结果，并在 claim 前再次检查源提交和工作区 |
| `session.py` | 增加 claim sidecar 与派生 session 原子提交 | 保留源文件，确保同一源完整性只派生一次 |
| `context.py`、`prompt.py` | 增加受保护的恢复摘要和模型规则 | 让模型知道 issue 和下一步，但看不到原始敏感参数 |
| `__main__.py`、`tools/base.py` | 增加 `/resolve` 与执行前 gate | 用户决定只由 CLI 记录，调查阶段只放行真正的无副作用观察 |
| `trace.py` | 校验恢复因果链 | 只读回放 recovery → issue → decision → trigger |

## 版本变更定位

先看上一版的边界。图中 `[旧]` 表示 v0.32 已有节点，`[B]` 表示本课仍然拒绝自动继续的边界：

```text
[旧] assistant tool_calls
          │
          ▼
[旧] pending tool_boundary ──► [旧] handler admission
          │                              │
          │ 进程中断                       ▼
          └──────────────────────► [B] v0.32 只诊断，不恢复半轮
```

v0.33 在 pending 边界之后插入恢复分支。`[+]` 是新增节点，`[~]` 是重新接线的消费者，`[C]` 是主要消费者：

```text
[旧] active schema 3 pending boundary
          │
          ▼
[+] prepare_resume 分类 pending calls
          │
          ├── handler_admitted=false ──► [+] not_executed
          │                                  │
          │                                  └──► [+] interrupted_before_handler tool result
          │
          └── handler_admitted=true ─────► [+] uncertain_* issue
                                             │
                                             ▼
[+] 新 session claim + 新 generation + 清除当前 verification
          │
          ▼
[C] Structured State / Context 显示 issue
          │
          ├── [C] 只读调查 ──► /resolve issue investigate
          ├── [C] 用户决定 ──► /resolve issue continue
          └── [B] 阻塞任务 ──► /resolve issue block
                                      │
                                      ▼
[+] 所有 issue 结算 ──► crash_recovery replan trigger ──► 重新授权和独立 verification
```

这张图强调一个顺序：恢复分类不是 handler 的返回值，用户的 `continue` 也不是模型可以伪造的批准。没有独立 verification，恢复 generation 不能完成任务。

## 上一版的问题：没有结果不等于没有执行

“session 里没有 tool result”只是一个存储事实。它可能对应三种不同情形：

1. 进程在 handler 前就退出，调用确实没有进入 handler。
2. handler 已经开始，但它是读取或 State 操作，结果可能已经改变了当前任务状态。
3. handler 已经开始，文件、shell、进程或 stdin 可能产生外部副作用。

文件表面没有变化、旧 PID 不见了、命令超时，都不能把第 2 或第 3 种情况改写成“肯定没执行”。因此 v0.33 只对 `handler_admitted=false` 生成确定的未执行结果；其余调用成为一等不确定事实。

## 关键流程

### 1. 先保留源，再准备恢复候选

`--resume` 自动检查 session 类型。clean session 仍走第 31 课的安全恢复；只有 schema 3 的 active pending boundary 进入 crash recovery。准备阶段不会调用 LLM、PermissionGate 或 handler。

候选会记录源 session ID、完整性摘要、工作区观察结果和待处理调用。工作区变化报告只是调查线索，不会证明某个 handler 执行或未执行。

### 2. 派生新的 session

claim 在源 session 锁内再次校验完整性，并把 `source_session_id + source_integrity` 写进私有 `crash_recovery_claims.json`。sidecar 使用规范化 JSON、私有权限、`fsync` 和同目录原子替换，按 `preparing → committed` 两阶段提交；中途失败时，下一次恢复会沿用同一个 derived ID，并用重新检查后的 State、Context 和工作区事实原子重写尚未发布的派生文件。只有派生文件验证成功后才发布 `committed`。已完成的 claim 仍直接报告已有分支，拒绝重复派生。

派生文件包含恢复后的 State、Context 和合成的 `role=tool` 结果；源文件字节不变。这样即使之后的运行时判断有误，也仍有一份原始 durable boundary 可供诊断。

### 3. 分类并回灌结果

分类只使用耐久边界里的准入事实和 effect class：

| 条件 | 分类 | 回灌内容 |
|---|---|---|
| `handler_admitted=false` | `not_executed` | `interrupted_before_handler`，自动结算 |
| `handler_admitted=true, effect_class=none` | `uncertain_state_or_result` | 不声称 State 或观察结果可用 |
| `handler_admitted=true, effect_class=possible` | `uncertain_side_effect` | 不声称成功，也不重放 |

每个原始 call 仍有且只有一个按模型顺序排列的 `role=tool` 结果。恢复结果不是旧 handler 的成功返回值；`uncertain` 只在恢复结算旧 attempt 时出现。

### 4. 逐项调查和决定

恢复期间，普通输入、副作用工具、verification、计划提交和任务完成都被 gate 拦截。只读调查必须先通过：

```text
/resolve issue-1 investigate 读取相关文件，确认当前工作区事实
... 只读模型回合 ...
/resolve issue-1 continue 调查完成，允许进入新的规划
```

`continue` 要求当前恢复 generation 已有一次成功、获准且 `effect_class=none` 的调查 attempt。所有 issue 都 continue 后，State 创建 `crash_recovery` replan trigger；模型必须提交或复核新计划，之后仍须重新授权和独立 verification。任何一个 issue 使用 `block` 都会把任务留在不可恢复的终态。

## 实现拆解

`AgentState` 把恢复 ID、issue、用户决定和新 generation 作为权威事实导出为 State format 2；旧 format 1 读取时把新增集合视为空。`resume.py` 只负责从源边界构造恢复候选，`SessionStore.claim_crash_recovery()` 负责锁、claim、派生 envelope 和耐久提交，两层都在返回运行时之前拒绝不确定的存储结果。

`ContextManager` 把 issue ID、工具名、分类、准入状态、公开原因和合法下一动作放入 Structured State。原始参数、shell 命令正文和 stdin 正文不会因为恢复摘要而进入持久化事实。`tools/base.py` 在 planning、repair 和 permission 之前执行 crash gate，避免“先授权再发现恢复状态”的窗口。

`Trace` 仍只接收当前任务的 `snapshot()`。它不读取源 session、派生 session 或 claim sidecar，也不调用 LLM、工具或权限闸门。恢复事实之间的边通过源恢复记录、issue、decision 和 trigger 的 ID 逐一校验；断链时结论保持 incomplete。

## 为什么这样设计

派生 session 的代价是同一个任务会留下源文件、claim 和新分支三份可审计对象，用户需要理解源 ID 与新 ID 的关系；收益是恢复流程不会覆盖唯一的事故证据，重复执行也不会悄悄产生第二组不确定事件。

把“继续”设计成用户逐 issue 决定，而不是模型输出一个普通文本，是因为模型看不到崩溃时刻的外部世界，也不能把文件变化猜成因果。只读调查先建立当前事实，再把用户的风险判断写入 State；这会增加交互次数，但保留了正确的责任边界。

新 generation 和独立 verification 会使任务比崩溃前多走一步。代价是不能复用旧的通过证据；收益是不会把恢复前的环境状态误当作恢复后的结果。`effect_class=none` 也默认不自动重试，因为“无外部副作用”仍不等于“结果已产生”。

## 设计边界

本课刻意不做以下事情：

- 不重连旧 `Popen`、PID、管道或 stdin 写入线程；旧进程会以 `orphaned` 事实保留，不能重新控制，也不能据此宣称已经退出。
- 不根据工作区未变化、命令超时或进程消失推断调用没有发生。
- 不自动回滚文件、shell 或外部服务副作用，不把未提交结果伪装成 verification。
- 不跨进程继承旧 PermissionGate 的 `once` / `always` 运行时批准。
- 不把 claim sidecar 当成 Trace 输入；它只用于防止同一源提交重复派生。

## 运行与观察

在真实会话中，先使用 `/save` 让任务启用 schema 3 持久化，再在 active pending boundary 存在时运行：

```bash
PYTHONPATH=src python -m mini_agent --resume <source_session_id>
```

预期看到源 ID、派生 session ID、分类表、工作区变化和 `/resolve` 用法。使用 `investigate` 后只读回合可以运行；在调查证据出现前直接 `continue` 会被拒绝。所有 issue 结算后，下一步不是继续旧工具回合，而是提交或复核 `crash_recovery` 触发的新计划。

## 本版特性、下一课与代码索引

本版新增的是“崩溃后的可解释交接”，不是无限可靠的外部事务恢复。后续课程如继续扩展，应沿用源 session 不覆盖、不自动 replay、重新授权和独立 verification 这些边界。

- [State 恢复记录（v0.33）](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.33/src/mini_agent/state.py)
- [Session claim 与派生 session（v0.33）](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.33/src/mini_agent/session.py)
- [恢复候选分流（v0.33）](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.33/src/mini_agent/resume.py)
- [CLI `/resolve`（v0.33）](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.33/src/mini_agent/__main__.py)
- [Structured State 恢复摘要（v0.33）](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.33/src/mini_agent/context.py)
- [Trace 因果校验（v0.33）](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.33/src/mini_agent/trace.py)
