# 第 48 课：进程内后台子代理

上一课：[具名子代理角色](47-agent-profiles.md) · [教程总览](README.md) · 下一课：可续接子会话（规划中）

代码快照：`v0.48` · 相邻差异：`v0.47..v0.48`

本课命令使用 Bash/zsh。代码链接和示例对应 v0.48 源码快照。

## 本课目标

上一版的 `delegate_task` 会等子代理完成后才返回结果。即使父 Agent 已经有别的工作可做，它仍得停在原地等调查结束。本课增加进程内后台子代理：父 Agent 启动具名只读调查后，可以继续请求模型和使用工具，之后再按 ID 查询状态并领取结果。

读完后，你应能解释四个后台工具各自做什么，为什么两个并行启动要等整轮工具结果都提交后才真正启动，以及为什么正在运行或尚未领取的子任务会阻止 `/save` 和任务完成。

## 前置条件

只需要基础 Python、终端和 Git。建议先读第 34–39 课了解同步委派、预算、并行调度和 schema 3 持久工具边界，再读第 47 课了解具名角色和角色工具限制。

检查相邻版本差异。`v0.48` tag 由用户手动创建；在该 tag 出现前，可在当前工作分支阅读相同源码。tag 建立后可切换到固定快照：

```bash
git checkout v0.48
git diff --stat v0.47..v0.48
git diff v0.47..HEAD -- src/mini_agent/delegation.py src/mini_agent/runtime.py
```

读完后用 `git checkout -` 回到原分支。在 tag 建立前，可将上面的固定版本差异命令替换为 `git diff --stat v0.47..HEAD`。

## 上一版的问题

v0.47 已经可以选择 `explorer`、`reviewer`、`tester` 或本地角色，但 `delegate_task` 是同步调用。父 Agent 在同一个模型回合启动多个调查时，子任务内部可以并行，父 Agent 仍需要等这些结果后才能进入下一轮。

如果把工作线程直接放到工具 handler 里，handler 返回后线程就可能在 schema 3 的整轮提交之前运行。父进程若此时崩溃，磁盘上会留下“已经接受启动”的结果，但无法确定子代理是否真正开始。这一版因此同时处理两个问题：父模型可以边做边等；启动确认必须在父线程按序提交，整轮成功后才释放 worker。

## 新增与改动文件

后台任务跨越工具、Runtime、State 和 CLI 生命周期，需要这些部分共同工作：

| 文件 | 变化 | 作用 |
|---|---|---|
| [tools/delegation.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.48/src/mini_agent/tools/delegation.py)、[tools/__init__.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.48/src/mini_agent/tools/__init__.py) | 修改 | 注册后台启动、状态、结果和取消工具，只放进父侧 Registry。 |
| [delegation.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.48/src/mini_agent/delegation.py) | 修改 | 管理排队任务、worker、取消事件、并发槽位和父线程结果收集；与同步委派共用预算。 |
| [runtime.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.48/src/mini_agent/runtime.py)、[agent.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.48/src/mini_agent/agent.py) | 修改 | 只允许纯启动回合，整轮提交后运行子任务；未领取时不把父任务判为完成。 |
| [state.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.48/src/mini_agent/state.py)、[trace.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.48/src/mini_agent/trace.py) | 修改 | 记录启动、收束、领取、放弃和中断事实，只保留有界摘要。 |
| [session.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.48/src/mini_agent/session.py)、[resume.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.48/src/mini_agent/resume.py) | 修改 | 校验 schema 3 的启动/领取身份；恢复时中断丢失的进程内任务，不重启 worker。 |
| [__main__.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.48/src/mini_agent/__main__.py)、[input_session.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.48/src/mini_agent/input_session.py)、[output.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.48/src/mini_agent/output.py) | 修改 | 等待输入时继续收集短通知和推进队列；清理任务边界时保留结果直到 clean 保存成功。 |

用 `git diff --stat v0.47..v0.48` 可看到这不是只加四个工具的改动：线程启动时机、持久边界和 CLI 任务清理也一起改变。

## 版本变更定位

先看 v0.47 的真实基线：父 Runtime 调用同步工具，Manager 等待结果，父 Runtime 再把结果作为 `role=tool` 消息提交。

```text
[旧] 父 AgentRuntime
  → delegate_task 参数校验与父 PermissionGate
  → DelegationManager.run / SubagentRunner.run
  → 等待只读子 AgentRuntime 完成
  → 一个 SubagentResult → 父 role=tool → 下一次父模型请求
```

v0.48 新增一条异步生命周期。worker 只把有界结果放进完成队列；父线程在安全边界取出结果并更新 State。

```text
[~] 父 AgentRuntime
  → [+] spawn_subagent（一轮只能包含一个或多个纯启动调用）
  → [C] ToolExecutor / PermissionGate / 阶段闸门
  → [~] 按模型顺序提交启动确认和 role=tool
  → [B] 整轮 schema 3 提交失败 → 不启动任何 worker
  → [+] 整轮提交成功 → Manager 启动 worker → 父 Agent 继续模型轮次
                                      ↓
          worker 执行子 Runtime → 有界完成队列 → [C] 父线程 State/预算结算
                                      ↓
      [+] ID/状态通知 → get_subagent_status → get_subagent_result → role=tool
```

图例：`[旧]` v0.47 已有；`[+]` v0.48 新增；`[~]` v0.48 修改；`[C]` 主要消费者；`[B]` 本版边界。箭头分别表示调用、数据流和控制流。父 Agent 的工具顺序仍决定 `role=tool` 消息顺序，子任务的完成先后不会改变它。

## 核心概念与数据结构

### 1. 启动确认和子任务结果是两次不同的工具调用

`spawn_subagent` 只返回一次启动确认，其中包括 `child_session_id`、`delegation_id`、角色、接受状态和预算摘要。它不会在原工具调用里追加第二个结果。子任务结束后，父模型用另一个调用领取结果：

| 工具 | 用途 | 是否返回调查正文 |
|---|---|---|
| `spawn_subagent` | 启动具名只读调查 | 否，只返回启动确认 |
| `get_subagent_status` | 查询有界状态和 `result_id` | 否 |
| `get_subagent_result` | 领取 `SubagentResult` | 是，收束后才返回 |
| `cancel_subagent` | 发出协作式取消请求 | 否 |

状态查询不会把长正文带入模型上下文；完整内容只有在明确调用 `get_subagent_result` 后才作为普通工具结果进入父 history。重复领取返回同一个 `result_id` 和同一结果，父 State 只结算一次。

### 2. 工具回合先完整提交，再启动 worker

一个工具回合是模型一次性发出的所有工具调用及其对应结果。v0.48 为含启动请求的回合加了专用路径：同一回合可以有多个 `spawn_subagent`，但不能混入文件读取、状态查询或其他工具。父 Runtime 按调用顺序完成准入、写入唯一 `role=tool` 确认，并提交整轮边界，最后才告诉 Manager 启动 worker。

关键控制顺序如下；整轮边界提交前，子代理还没有运行：

```python
if self.session_boundary is not None:
    self.session_boundary.complete_round(state, context)
manager.commit_background_spawn_round()
manager.activate_background_tasks()
```

如果 schema 3 在任一启动确认或整轮提交时失败，控制不会走到 worker 启动。这个闸门把“父模型收到接受确认”和“子代理开始消耗预算”连接成一个可恢复的顺序。

### 3. worker 只产出结果，父线程负责结算

子 Runtime 使用自己的 State、Context、角色和只读工具视图。worker 完成时只把有界 `SubagentResult` 放入线程安全完成队列，不改父 Context 或父 session。父 Runtime 在下一安全边界收集队列，更新委派记录、结算 usage，并显示只包含 ID、状态和结果 ID 的通知。CLI 等待输入时，主线程仍会收集完成项并启动队列中的下一个任务。

同步和后台子代理共用父任务预算与并发槽位。默认最多创建 3 个子代理，同时运行最多 2 个；本地设置的更小并发上限也会生效。排队合同先占用总预算；worker 真正运行后占用并发槽位。失败、超时、异常和取消仍形成可领取的有界结果。

### 4. 进程内任务不能伪装成安全点

普通 safe point 要求任务状态完整且没有活动或未领取子代理。活动线程无法序列化，也不会保存为可续接子会话，因此 `/save` 明确拒绝并列出 ID；自动保存则等待子任务收束，不打断父 Agent。CLI 的 `/new`、`/reset`、EOF 和退出会先请求取消并有界等待。若线程没在期限内退出，旧任务保留，CLI 报告相关 ID。

父模型即使返回普通文本，也不能在活动子代理或未领取结果时宣布父任务完成。CLI 会把任务保持为活动状态；子结果也不自动成为父侧验证证据。

## 为什么这样设计

启动确认和最终结果分成两次工具调用，父模型可以先启动调查，再自主决定何时查询和领取；这也保持了每个工具调用只有一个 `role=tool` 结果的协议。

worker 与父线程分工，是为了避免多个线程同时改父 State、history 或 session。子任务可以并发计算，但父侧事实按固定顺序提交。schema 3 只保存合同身份、预留预算和生命周期，不保存 Python 线程句柄。

本版用协作式取消：正在执行的 LLM 请求需要先返回，子 Runtime 才能在下一个边界观察取消事件。它不使用强制线程终止，因为那无法保证子任务和父账本停在一致状态。

## 设计边界

后台只在当前 CLI 进程内运行。若完整启动回合已经安全写入，但父进程在结果领取前退出，恢复分支会把子任务记为 `interrupted`，不重启 worker，不伪造结果 ID，并按预留上限保守结算未知用量；用户仍需逐项处理 crash recovery issue。

如果启动 call 的逐项确认已经保存，但整轮尚未 committed，worker 从未启动，恢复会关闭未启动的预留。活动 worker 或未领取结果不会被保存为 clean safe point；用户结束任务时，已经收束但未领取的结果暂记为 `abandoned`。若 clean 保存失败，旧任务和内存结果仍可领取；成功提交后才释放结果正文。

角色仍只决定只读提示和工具范围。子代理不能写文件、运行 shell、操作 MCP、再委派或执行父侧验证。v0.49 才计划讨论保存已收束的子 Context 并续接同一个子会话。

## 关键流程

```text
父模型提出 spawn_subagent
  → schema / role / stage / PermissionGate 校验
  → 父 State 预留子代理数、LLM/tool/token 预算
  → handler 返回唯一启动确认
  → 对应 role=tool 与 schema 3 call 按模型顺序提交
  → 整轮 committed 后，Manager 启动可用并发槽位中的 worker
  → worker 子 Runtime 完成并只写完成队列
  → 父线程收集、结算 usage、显示 ID/状态通知
  → 模型显式查询状态并领取结果

失败路径：
  启动/整轮 session 提交失败 → 该轮不启动 worker
  旧进程退出且无安全结果 → interrupted + 预留额度保守结算 + crash issue
  结果未收束 → status 工具只返回状态；父任务不能进入 done
```

## 运行与观察

准备好本地模型配置后，用 Bash/zsh 启动命令行首条任务：

```bash
PYTHONPATH=src python -m mini_agent "只读调查工具注册表与执行器的关系，并继续检查一次父侧参数校验"
```

观察父模型是否先得到一个带 `child_session_id` 的 `spawn_subagent` 工具结果，然后继续发出自己的工具调用或模型轮次。子代理收束时，终端通知只显示 ID、状态和 `result_id`。父模型要看到摘要和 findings，必须再调用 `get_subagent_result`。若它直接输出结束文本但没有领取结果，CLI 会说明后台任务仍活动或结果待领取，任务不会变成 `done`。

这几个现象分别说明启动确认不是最终报告、通知不隐式注入结果，以及父任务的完成判定会检查未领取任务。

## 实现拆解

`tools/delegation.py` 提供严格的合同与 UUID 参数校验；`state.py` 预留聚合预算并保存 lifecycle。`DelegationManager.spawn_background()` 将合同放入待启动队列，但不启动线程。`AgentRuntime._run_background_spawn_round()` 在每个启动 call 的 `role=tool` 提交后确认启动事实，并仅在整轮 boundary committed 后释放 worker。

worker 复用 `SubagentRunner.run()` 与同一 `AgentRuntime.run()`，完成后只写 Manager 的线程安全队列。`AgentRuntime.collect_background_events()` 在父侧安全边界收集并通知。领取时，父 Runtime 校验结果身份，State 对 result ID/hash 进行一次性结算，`session.py` 再验证工具边界中的 `child_session_id`、`delegation_id` 和结果摘要。

崩溃恢复由 `resume.py` 检测 active schema 3 tool boundary 中的后台合同。它不恢复线程；未安全保存的结果会中断，并将未知用量按预留上限结算。Trace 仅读取 State 结构化生命周期，不观察线程，也不读取子 history。

## 本版特性、下一课与代码索引

本版新增四个后台工具，保留同步 `delegate_task`；父 Agent 能跨模型轮次继续工作，子结果通过显式状态查询和领取进入 history。整轮持久化、父侧用量结算、safe point 与崩溃恢复继续由现有父 Runtime 和 schema 3 控制。

下一课规划让父 Agent 继续追问一个已经收束并安全保存的子会话。v0.48 不保存运行线程或跨进程子 Context。

核心实现索引：[后台任务管理](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.48/src/mini_agent/delegation.py)、[工具回合启动闸门](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.48/src/mini_agent/runtime.py)、[父侧阶段策略](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.48/src/mini_agent/agent.py)、[生命周期状态](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.48/src/mini_agent/state.py)、[schema 3 边界](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.48/src/mini_agent/session.py)、[崩溃恢复](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.48/src/mini_agent/resume.py)。
