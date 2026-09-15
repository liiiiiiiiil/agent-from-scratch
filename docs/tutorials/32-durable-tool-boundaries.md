# 第 32 课：把工具调用的关键边界写下来（v0.32）

上一课：[从完整安全点恢复会话](31-safe-resume.md) · [教程总览](README.md) · 下一课：[崩溃后的调用交接](33-crash-recovery.md)

> 代码快照：`v0.32` · 相邻差异：`v0.31..v0.32` · 命令环境：Bash/zsh

> 运行要求：Python 3.10+。本课建立 schema 3 的工具边界，但仍拒绝直接恢复半轮工具调用；分类和用户交接留到 v0.33。

## 本课目标

想象一个工具回合：模型已经说“请写文件”，程序刚准备执行，或者 handler 已经返回但结果还没来得及写回 session，Python 进程就退出了。v0.31 的最后一个 clean 安全点无法告诉下一次启动“动作停在哪一步”。

本课把这个危险窗口拆成可检查的持久事实。读完本课，你应该能解释：

- `pending`、`handler_admitted` 和 `committed` 分别表示什么；
- 为什么必须在 handler 前先提交一次准入事实；
- 为什么 State、`role=tool` 结果和调用记录要一起提交；
- 为什么同一回合的所有调用完成提交后，才能再次请求 LLM；
- 为什么 v0.32 记录了中断位置，却仍不自动 replay（重放）旧调用。

## 上一版的问题

一次工具调用至少涉及三个不同事实：模型发出了 `tool_calls`；调用通过了参数、计划和权限检查；handler 执行后产生结果并回灌 `role=tool`。v0.31 只有回合结束后才保存安全点。

如果 handler 已经开始，随后保存失败，下一次读取旧文件会把“可能已经执行”和“完全没有执行”混在一起。如果 State 已经记账、但消息历史缺少 `role=tool`，下一次 LLM 也会看到一条不完整的协议。因此本版增加一个单文件 `tool_boundary`，专门记录工具回合的中间状态。

## 前置条件与版本切换

需要基础 Python、命令行和第 31 课的 session、State、Context 概念。命令使用 Bash/zsh；tag 是固定学习快照。

```bash
git checkout v0.31
git diff --stat v0.31..v0.32
git diff v0.31..v0.32 -- src/mini_agent/session.py src/mini_agent/context.py src/mini_agent/state.py src/mini_agent/tools/base.py src/mini_agent/agent.py
git checkout v0.32
```

## 新增与改动文件

本版的主线是“回合开始记录 → handler 前准入 → 结果和事实一起提交 → 整轮收口”。

| 文件 | 变化 | 作用 |
|---|---|---|
| `src/mini_agent/session.py` | 增加 schema 3 和 `tool_boundary` | 校验调用、结果、State 和顺序，并原子保存 |
| `src/mini_agent/context.py` | 增加最后一轮结果前缀导出 | 在边界保存时允许受控的半轮上下文 |
| `src/mini_agent/state.py` | 增加边界专用 pending 导出 | 保存 attempt、预算和 generation 的引用 |
| `src/mini_agent/tools/base.py` | 拆分 `admit()` 和 `execute_admitted()` | 把“允许进入 handler”与“真正执行”分开 |
| `src/mini_agent/agent.py` | 增加逐调用提交和整轮收口 | 保证 State、消息和边界记录同步推进 |
| `src/mini_agent/__main__.py`、`resume.py` | 接入 schema 3 | 区分保存失败，并继续拒绝半轮 safe resume |

## 版本变更定位

图例：`[旧]` v0.31 已有，`[+]` v0.32 新增，`[~]` v0.32 修改，`[C]` 主要消费者，`[B]` 本课边界。

旧流程只有一个完整收口：

```text
[旧] assistant tool_calls
  -> [旧] handler
  -> [旧] State + role=tool
  -> [旧] clean safe point
  [B] 中途退出时，磁盘没有 handler 是否已进入的事实
```

本版把工具回合变成一串受约束的 durable（已写入并校验到磁盘）提交：

```text
[旧] assistant tool_calls
  -> [+] pending round commit
  -> [+] 参数 / 计划 / 权限 / 预算检查
  -> [+] handler_admitted + attempt/generation commit
  -> [旧] handler
  -> [+] State + role=tool + call committed commit
  -> [+] 全部 call committed
  -> [C] 允许下一次 LLM

[B] 任一提交失败
  -> 不进入下一步，不请求下一次 LLM
[B] pending 或部分 committed
  -> v0.32 只保存中断事实，不自动恢复或 replay
```

## 核心概念与数据结构

### 1. `pending` 先记录“模型要求了什么”

工具回合开始时，agent loop 会把 assistant 消息和调用顺序写入 `tool_boundary`。每个调用得到稳定的 `invocation_id`，同时保留模型原始的 `tool_call_id`。这一步发生在任何 handler 之前。

简化后的记录如下：

```json
{
  "schema_version": 3,
  "save_kind": "tool_boundary",
  "tool_boundary": {
    "round_id": 4,
    "status": "pending",
    "calls": [{
      "invocation_id": "r-4-c-0",
      "tool_call_id": "call-7",
      "tool": "write_file",
      "effect_class": "possible",
      "permission": "not_checked",
      "handler_admitted": false,
      "attempt_id": null,
      "status": "pending"
    }]
  }
}
```

这里的 `pending` 只表示“边界记录已经落盘”。它不表示 handler 已经开始，也不表示文件写入成功。它只给崩溃后的诊断留下最后一个明确位置。

参数摘要会脱敏：`content`、`command` 和 `input` 等正文只保留类型、长度或占位信息，尤其不能把 `write_process.input` 变成可重放的 stdin。

### 2. handler 前先提交准入

handler 是真正执行工具的函数；准入（admission）是执行器确认“这个调用现在可以进入 handler”的结果。准入检查会涉及工具是否存在、参数是否符合 schema、当前计划/修复阶段是否允许、权限是否允许，以及是否需要预留 attempt 和 generation。

顺序必须是：

```text
admit()
  -> 写入 permission / handler_admitted / attempt / generation
  -> execute_admitted()
```

如果写入准入事实失败，就不能调用 `execute_admitted()`。这条规则尤其重要：保存失败时宁可留下 `pending`，也不能让一个已经改变外部世界的动作没有任何持久边界。

权限拒绝或参数错误不会进入 handler，但仍然会产生确定的工具结果；它们不是“副作用未知”。计划工具的校验失败也继续使用 `plan_rejected`，不因为新增持久化就改变原有计划语义。

### 3. 一个工具结果对应一个原子提交

handler 返回后，主线程按模型顺序完成一次提交：

1. State 记录 `ExecutionResult`，也就是这次执行的事实；
2. Context 追加对应的 `role=tool` 消息；
3. `tool_boundary` 把这个 call 标记为 `committed`，并写入结果引用；
4. 三者一起原子替换 session 文件。

`role=tool` 是模型协议中的工具结果消息，必须带回原始 `tool_call_id`。只有 State、消息和调用记录都可读，才算一个 call committed；同一回合每个 call 都 committed 后，整轮才 committed，agent loop 才能再次请求模型。

有些 `effect_class=none` 的只读调用可以并发等待，但提交仍按模型顺序进行。后一个结果即使先返回，也不能越过前一个尚未提交的 call。`effect_class=possible` 的调用继续按现有顺序执行，`run_shell` 仍属于 possible。

### 4. 不同失败也必须各有结果

工具不存在、参数不合法、权限拒绝、handler 异常、计划拒绝和进程异步退出，都不能让模型协议少一条结果。每个原始 call 恰好对应一个 `role=tool`；但结果可以表示错误或不确定。

这条不变量的意义是：模型下一轮看到的是一个完整回合，而不是“有两个调用却只回来了一个结果”。完整协议不等于所有工具都成功。

## 为什么这样设计

本版使用同一个原子 JSON 文件，而不是再引入一个 journal。这样 State、Context 和调用边界能在同一次替换中保持一致，读者也只需理解一个权威文件。代价是每个小提交都要写文件，磁盘写入更频繁；同时文件仍可能在 handler 返回前停在 pending。

本版把“准入事实”放在 handler 前，是为了回答“是否已经获得执行资格”，不是为了声称副作用已经发生。即使 `handler_admitted=true`，进程也可能在 handler 内部退出；如果 effect 是 possible，文件或 shell 的外部结果更无法从 session 直接推断。

因此 v0.32 只承诺精确记录中断窗口，不承诺自动重放。下一课才会把这些 pending 调用分类，并交给用户决定如何调查。

## 设计边界

支持的路径是：回合开始保存 pending；准入完成后保存 `handler_admitted`；handler 返回后把 State、`role=tool` 和 call 记录一起提交；全部 call committed 后才允许下一次 LLM。session 仍是单个 schema 3 JSON 文件，没有额外的 journal。

不支持的路径是：根据 pending 推断 handler 一定没有执行；把脱敏参数当成重试凭据；中断后自动重放 `write_file`、`run_shell`、进程控制或 stdin 写入；在缺少某个 `role=tool` 时继续请求模型。v0.33 才处理“不确定调用”的恢复交接。

## 关键流程

```text
assistant tool_calls
  -> pending round commit
  -> preflight / permission / reservation
  -> handler_admitted commit
  -> handler
  -> State + role=tool + call committed commit
  -> all calls committed
  -> complete round
  -> next LLM request

任一 durable commit 失败
  -> 停在当前边界
  -> 不进入后续 handler，不请求下一次 LLM
```

## 实现拆解

[`src/mini_agent/session.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.32/src/mini_agent/session.py) 定义 schema 3、调用引用、顺序校验和原子替换；[`src/mini_agent/context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.32/src/mini_agent/context.py) 只允许最后一轮按模型顺序导出结果前缀；[`src/mini_agent/state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.32/src/mini_agent/state.py) 只在边界保存时放行被引用的 pending attempt。

[`src/mini_agent/tools/base.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.32/src/mini_agent/tools/base.py) 的 `admit()` 与 `execute_admitted()` 划开准入和 handler；[`src/mini_agent/agent.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.32/src/mini_agent/agent.py) 负责回合开始、逐调用提交、按模型顺序回灌和整轮收口。`__main__.py` 和 `resume.py` 负责把保存失败与普通工具错误分开，并继续拒绝半轮 safe resume。

## 运行与观察

下面命令使用 Bash/zsh。命令行首条任务执行后，程序仍会进入交互循环。

```bash
PYTHONPATH=src python -m mini_agent "检查一个小改动"
```

在任务中输入 `/save` 开启持久化并记录 session ID。完成一轮工具调用后，打开 `~/.mini_agent/sessions/<session_id>.json`，应能看到 `schema_version: 3`；完整回合结束时，`tool_boundary.status` 为 `committed`，每个 call 也为 `committed`，历史中每个 assistant tool call 后都有对应 `role=tool`。

如果故障发生在回合中间，文件可能保留 `pending` 或部分调用已 committed。v0.32 的预期行为是保存这段事实并拒绝把它当作完整安全点；下一课会说明为什么不能直接继续。

## 本版特性、下一课与代码索引

本版新增的是 schema 3、handler 前的准入提交、每个 call 的原子结果提交、整轮 complete 约束和异步进程事实提交。它没有新增 journal，没有把 pending 当成“未执行”，也没有改变 `run_shell=possible`、PermissionGate 或计划拒绝的原有语义。

下一课 v0.33 会读取 active pending 边界，把调用分成确定未执行和不确定副作用两类，派生新的 session，并通过 `/resolve` 让用户逐项交接。

核心代码索引：[`session.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.32/src/mini_agent/session.py)、[`context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.32/src/mini_agent/context.py)、[`state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.32/src/mini_agent/state.py)、[`tools/base.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.32/src/mini_agent/tools/base.py)、[`agent.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.32/src/mini_agent/agent.py)。
