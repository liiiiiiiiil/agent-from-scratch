# Mini Agent 上下文构成：一张动态视图

> 事实来源：`src/mini_agent/` 下 `__main__.py`、`agent.py`、`context.py`、`state.py`、`prompt.py`、`instructions.py`、`tools/base.py`。

## 0. 心智模型：一句话版本

**上下文不是一份存储，而是一个每轮重新计算的视图**。v0.20 还把 Repair Loop 阶段作为不可丢失的关键状态注入，v0.22 又把 active Plan Contract 的有界执行视图放入同一条语义轨道；v0.24 继续加入活动 replan trigger、修订预算和 LoopStagnationState；v0.33 再加入受保护的 crash recovery 摘要：模型看到的不是“最近一次文字说了什么”，而是当前计划、步骤依赖、真实触发来源、未结算 issue 以及是否必须只读调查、重规划或独立验证。完整 `plan_revisions`、`plan_progress_history` 和 crash recovery 记录保存在 State，Structured State 只显示有界摘要；v0.21 的 Trace 对缺失 `todo_revisions` 继续安全降级为空。

```text
view = Runtime Notice? + 只读底座(System Prompt) + [Structured State](语义轨道，含 Repair Loop 阶段)
     + [Historical Summary]? + Task + 历史轮次(协议轨道)     —— 再过一遍预算闸门
```

- **一个只读底座**：`protected_messages`，启动时构建一次，永不变化。
- **两条写入轨道**：同一次工具执行被记录两次——`history` 里的协议原文（给 LLM 直接读，可被裁剪）和 `AgentState` 里的语义事实（免疫裁剪，每轮重渲染）。
- **一个反馈环**：视图超预算时，改写的是视图本身（trim → compact），`history` 本体永远完整。

## 1. 主图：一轮的上下文生产线（闭环）

```text
启动（__main__.py main()，进程内只做一次）
│
├─ InstructionLoader.load()      root→cwd 逐级收集 AGENTS.md，合并截断 ≤12000 字符
├─ build_system_prompt()         header 身份 + core_rules 规范 + <env> 环境 + 项目级指令
└─ protected_messages            只读底座：永不变化，裁剪永不触碰


用户任务（run_task，一次会话可多次）
│
├─ state.begin_task(task)        语义轨道清零重开（history 不清，跨任务保留）
└─ history += {role:user}        进入协议轨道，成为永不删除的 Task 锚点


agent_loop 每一轮（agent.py:141，上限 MAX_ITERATIONS=50）
│
│  ① 汇合投影 —— prepare_messages()（context.py:405），上下文在这一刻诞生
│  │    view = [Runtime Notice]?        一次性提醒，插最顶，发出即消费
│  │         + System Prompt            来自 protected_messages（拷贝）
│  │         + [Structured State]       来自 state.snapshot()，本轮重新渲染
│  │         + [Historical Summary]?    来自 compact()，触发过压缩才存在
│  │         + Task                     首条 user 消息
│  │         + 历史轮次                  assistant + 其 tool result，原子成组
│  │    预算闸门：截断大 tool result → 删最老整轮 → compact 摘要老轮次
│  ▼
│  ② call_llm(view)              流式发送，收 assistant 回复
│  │
│  ③ 分流
│  ├─ 无 tool_calls：查完成条件（active plan 全部完成 + 最近修改后验证通过；无计划时走 Direct Path）
│  │     ├─ 满足 → 结束返回
│  │     └─ 不满足且未提醒过 → set_runtime_notice → 回 ①
│  │           （仅提醒一次；仍不满足则 status=blocked 后返回）
│  │
│  └─ 有 tool_calls：ThreadPoolExecutor 并发执行（含 PermissionGate 拦截）
│        │
│        │   ★ 同一次执行，写两条轨道；全部 role=tool 回灌后再观察停滞：
│        │
│        ├─ 协议轨迹：history += {role:tool}（完整结果原文）
│        │     LLM 下一轮直接读；受预算约束，可能被截断/折叠
│        │
│        └─ 语义事实：结构化 ExecutionResult → state.record_execution_result（兼容回调仍可写入 record_tool）
│              files_changed / errors / active plan / 验证证据
│              免疫裁剪，下一轮渲染进 [Structured State] 锚定事实
│
└──► 两条轨道在下一轮的 ① 重新汇合 —— 循环，直到纯文本收尾或轮次上限
```

恢复时还有一条不可裁剪的交接事实：`crash_recovery` issue 会插入 Structured State，原始参数和 stdin 正文不进入摘要。未结算 issue 存在时，agent loop 只允许只读调查；所有 issue 结算后必须经过新的计划和 verification 才能收口。

Repair Loop 的阶段约束也在这里重新渲染：`diagnosis_required` 要求只读调查、独占 `recover` 或独占 `request_replan`；failure trigger 进入 Explore 后只允许只读调查或独占 `commit_plan`；`verification_required` 要求下一回合只有一个独立 verification。v0.24 的停滞计数、短 hash、活动 trigger 来源和 gate 合法下一动作也来自同一份 State 快照。v0.33 未结算 crash issue 会进一步收窄 gate 到无副作用观察，不能被历史裁剪或摘要覆盖。上下文压缩只处理协议历史，不能删除计划执行视图、`repair_loop`、活动 failure/recovery、crash recovery、generation、预算或停滞状态。

## 2. 关键机制一：双轨记录（本架构的核心取舍）

为什么一次执行要写两份？因为**协议历史会被预算破坏，而事实必须存活**。

| | 协议轨迹 `history` | 语义事实 `AgentState` |
|---|---|---|
| 写入者 | `agent_loop` 回灌 `role=tool` 消息 | 结构化 `ExecutionResult → AgentState.record_execution_result`；兼容入口可用 `on_result → record_tool` |
| 内容 | 完整结果原文（命令输出、文件片段） | brief 截断 ≤200 字符 + 派生事实 |
| 预算下的命运 | 可被截断、整轮删除、折叠进摘要 | **免疫裁剪**，每轮完整重渲染 |
| 读出方式 | 作为历史轮次原文进入视图 | 渲染为 `[Structured State]`（context.py:312） |
| 生命周期 | 跨任务累积（会话级） | `begin_task` 清零重开（任务级） |

两个特例，同样服务于这个设计：

- `commit_plan` / `update_plan_progress`：计划意图不是环境执行事实，两个 state-bound 工具直接写入不可变 revision/event；它们不推进 generation，也不产生验证证据。Structured State 只显示从 State 推导的 active plan 执行视图。
- `run_shell(purpose="execution"/"verification")`：execution 视为改动环境 → 作废全部验证证据；verification 通过 → 记录证据并解除"需要验证"标记。证据失真比缺证据更危险，所以宁可作废重来。

## 3. 关键机制二：视图随轮次演化

同一任务里，第 1 / N / M 轮发送的消息长这样（自上而下即实际顺序）：

```text
第 1 轮（任务刚下达）
  System Prompt
  [Structured State]        Task=修复登录bug | Tools: 0 | Status: running
  user: 修复登录 bug
  （还没有历史）


第 N 轮（历史生长，事实累积）
  System Prompt
  [Structured State]        Plan revision=1 | step=inspect | Files: auth.py | Errors: 1
  user: 修复登录 bug
  assistant(tool_calls: read_file, update_plan_progress)
  tool: <文件内容>          ┐
  tool: ok                 ├─ 轮次 = assistant + 其全部 tool result
  assistant(edit_file)     │  原子成组，绝不产生孤儿 tool result
  tool: ok                 ┘
  …（历史逐轮变长，token 逼近预算）


第 M 轮（超预算，压缩已触发）
  System Prompt
  [Structured State]        语义事实无损：改了什么、验证是否通过都在
  [Historical Summary]      ← 老轮次折叠成结构化摘要（按已摘要边界增量更新）
  user: 修复登录 bug
  assistant(tool_calls: run_shell)
  tool: [exit=0] …          ← 只保留最近 keep_rounds=6 轮原文
```

注意两点：`[Structured State]` 的位置在 Task **之前**（首条 user 前插入）；压缩后历史的"中间"被摘要替换，但**头（Task）和尾（最近 6 轮）保真**——模型始终知道自己为什么来、刚刚做了什么。

## 4. 关键机制三：预算反馈环

```text
window = CONTEXT_WINDOW
├─ reserve   15%              输出预留，不发送
└─ input_limit 85%
   ├─ system / task / state   受保护区：裁剪永不触碰
   └─ history / tool_result   可压缩区：上限约 window × 45%（history_ratio）
```

超预算时按"损失从小到大"三步走，全部只作用于视图副本（`history` 本体不动）：

1. **截断 tool result**：只截超过 120 字符的 tool 内容，保头尾 + `[... omitted N characters ...]`，协议字段不变。
2. **整轮原子删除**：最老的轮次先走，assistant 与 tool result 同生共死。
3. **compact()**：把最近 6 轮之前的完整轮次交给 LLM 做无工具摘要（任务/已完成步骤/最后成功调用/改动文件/错误/进度/下一步）；失败降级为仅 trimming。

精确阈值：`message_limit = max(protected, min(input_limit, protected + history_limit))`（context.py:115）——受保护前缀再大也不误删，只是极端情况下放开上限。

## 5. 不变量

1. System Prompt 与首条 user 永不删除；轮次原子，无孤儿 tool result。
2. `history` 只追加不修改；trim/compact 只改副本——任何一轮的视图都可从完整 history 重建。
3. 每轮 tool results 全部回灌后才进下一轮（无 v0.10 的"半截状态"）。
4. 语义事实免疫裁剪：不管协议历史被削成什么样，`[Structured State]` 每轮完整重渲染。
5. Runtime Notice 只发一次，且在最终视图构建成功后才消费（context.py:429）——压缩重建消息不会吞掉提醒；v0.20 的阶段性 Notice 会明确指出下一步合法动作，v0.24 的停滞 Notice 还会显示计数、类别和同时满足 Planning / Repair gate 的下一动作。
6. Trace & Replay 读取独立的 `state.snapshot()` 视图，不进入 LLM 消息，不调用执行链，也不改变上下文或 State。
7. 可观测性与结果回调都是纯观察者，异常被吞（base.py:128、context.py:281），不破坏执行。

8. v0.33 的 `crash_recoveries`、`crash_issues` 和 `crash_decisions` 是不可裁剪的恢复事实；Structured State 只显示有界摘要，包含 issue ID、工具、分类、准入状态、公开原因和下一动作，不显示原始参数、shell 命令或 stdin。未结算 issue 存在时只允许获准的无副作用观察，所有 issue 结算后仍需新计划和独立 verification。

## 6. 代码速查

| 视图成分 | 产出位置 | 更新频率 |
|---|---|---|
| System Prompt | `prompt.py:112` + `instructions.py:48`，`__main__.py:30` 组装 | 进程级一次 |
| `[Runtime Notice]` | `context.py:414`（`set_runtime_notice` 设置） | 单次请求 |
| `[Structured State]` | `context.py:312 _render_state` ← `state.py:171 snapshot` | 每轮 |
| `[Historical Summary]` | `context.py:360 compact` ← `agent.py:136 summarize_messages` | 压缩后增量 |
| Task / 历史轮次 | `__main__.py:45`、`agent.py:236,304` 追加 | 事件驱动 |
| 语义事实 | `agent.py` 提交 `ExecutionResult` → `state.py record_execution_result` | 每次结构化工具执行 |
