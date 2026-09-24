# 第 48 课：让子代理在后台调查

上一课：[给子代理设定角色](47-agent-profiles.md) · [教程总览](README.md) · 下一课：[恢复并继续子会话](49-resumable-child-session.md)

代码快照：`v0.48` · 相邻差异：`v0.47..v0.48`

本课命令使用 Bash/zsh；代码链接和示例对应 `v0.48` 源码快照。

## 本课目标

上一课的 `delegate_task` 会一直等子代理交回报告。即使父 Agent 还有别的工作，等待期间也不能继续处理。本课加入后台调查：父 Agent 先得到一个“已接受”回执，然后可以继续工作，之后再查询子代理状态并领取报告。

这里的“后台”指调查在当前命令行程序中另一个工作线程里运行。它不是能在程序退出后继续工作的独立服务。正在处理用户任务的是父 Agent；负责只读调查的是子代理。

读完后，你应能区分“启动回执”和“调查报告”，并理解为什么系统要等整轮启动记录安全提交后才启动子代理，以及为什么活动任务或未领取报告会阻止保存和结束父任务。

## 前置条件与版本切换

只需要基础 Python、终端和 Git。建议先读第 47 课了解角色如何限制子代理；第 34–39 课介绍了委派、并发和持久化工具结果，可作扩展阅读。

检查相邻版本差异，再切到本课快照：

```bash
git checkout v0.47
git diff --stat v0.47..v0.48
git diff v0.47..v0.48 -- src/mini_agent/delegation.py src/mini_agent/runtime.py
git checkout v0.48
```

读完后用 `git checkout -` 返回切换前的分支。

## 上一版的问题

v0.47 的委派是同步的：父 Agent 发出 `delegate_task` 后，要等子代理完成才能继续下一轮。后台任务可以让父 Agent 在调查进行时继续处理其他事情。

但启动时机不能随意提前。如果子代理在线程里立刻开始，父程序可能在还没把“已接受启动”这一事实写入保存文件时退出。恢复时就分不清它有没有开始，也可能错误地重复启动。

因此本版规定：父线程先按顺序提交本轮所有启动回执；只有整轮写入成功后，才真正启动子代理。

## 新增与改动文件

| 文件 | 变化 | 读者可从这里看到什么 |
|---|---|---|
| [tools/delegation.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.48/src/mini_agent/tools/delegation.py) 与 [tools/__init__.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.48/src/mini_agent/tools/__init__.py) | 修改 | 四个后台工具如何注册到父 Agent。 |
| [delegation.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.48/src/mini_agent/delegation.py) | 修改 | 如何管理工作线程、排队、取消、并发和结果收集。 |
| [runtime.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.48/src/mini_agent/runtime.py) 与 [agent.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.48/src/mini_agent/agent.py) | 修改 | 为什么启动要等整轮保存成功，以及父任务何时可以结束。 |
| [state.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.48/src/mini_agent/state.py) 与 [trace.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.48/src/mini_agent/trace.py) | 修改 | 父任务怎样记录启动、完成、领取或中断。 |
| [session.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.48/src/mini_agent/session.py) 与 [resume.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.48/src/mini_agent/resume.py) | 修改 | 怎样保存启动事实，并在程序重启后记录旧工作已中断。 |
| [__main__.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.48/src/mini_agent/__main__.py) 与输入/输出模块 | 修改 | 等待用户输入时怎样显示通知，以及退出时怎样清理任务。 |

`git diff --stat v0.47..v0.48` 会显示这次变化横跨工具、运行循环、保存和命令行退出流程，不只是多了几个工具。

## 版本变更定位

v0.47 的父 Agent 必须等同步委派返回报告；下面是该版本的基本路径：

```text
[旧] 父 Agent
  → delegate_task
  → 启动只读子代理
  → 等待完整报告
  → 把报告交给父 Agent
```

v0.48 把启动确认和报告领取分成了两次操作。`schema 3` 是本项目保存会话时使用的一种数据格式；本版用它记录每个启动回执以及整轮是否提交成功。

```text
[旧] 父 Agent 的同步委派
  ↓
[+] spawn_subagent（只返回启动回执）
  → [~] 父 Agent 增加后台任务管理
  → [C] 工具权限与任务阶段检查
  → 按顺序保存启动回执
  → [B] 整轮保存失败：不启动任何子代理
  → 整轮保存成功：后台启动 worker
       ├─ 父 Agent 继续处理其他工作
       └─ 子代理完成 → 父线程收集结果
                            → 查状态 → 显式领取报告
```

图例：`[旧]` 为 v0.47 已有，`[+]` 为 v0.48 新增，`[~]` 为修改，`[C]` 为主要校验或使用方，`[B]` 为本版边界。`worker` 指执行子代理任务的工作线程。箭头表示调用、控制或数据流。完成先后的不同不会打乱父 Agent 中工具结果的顺序。

## 核心概念与数据结构

### 1. 启动、查询、领取和取消是四种操作

后台子代理的工具只放在父 Agent 一侧。它们各自做一件事：

| 工具 | 用途 | 是否包含调查报告 |
|---|---|---|
| `spawn_subagent` | 启动一项具名只读调查 | 否，只回报已接受及任务 ID |
| `get_subagent_status` | 查看任务当前状态 | 否，只返回有界状态和可用时的结果 ID |
| `get_subagent_result` | 领取已经收束的结果 | 是 |
| `cancel_subagent` | 请求子代理停止 | 否 |

`child_session_id` 是父任务内这项子任务的编号；`result_id` 标识完成后的报告。状态通知不会自动把报告正文塞进父 Agent 的对话。父 Agent 要阅读调查发现，必须显式调用 `get_subagent_result`。重复领取会返回同一份报告，父状态也只结算一次。

启动时必须指定第 47 课介绍的角色，并声明用途是只读调查。待用户批准的计划、验证、诊断和崩溃恢复阶段不允许启动后台任务；子代理不能因为被放到后台就取得额外能力。

下面只列出启动调用中与本课主题有关的两个字段；真实调用还要提供调查目标、范围和预算：

```json
{
  "agent_profile": "reviewer",
  "purpose": "investigation"
}
```

### 2. 整轮保存成功后，子代理才开始运行

“模型回合”是模型一次回答中发出的全部工具调用。一个含后台启动的回合可以启动一项或多项子任务，但不能夹带查询状态、读文件等其他工具调用。父线程按模型给出的顺序处理每个启动请求并写入对应回执，然后保存整轮边界。

关键顺序可以简化为：

```python
if self.session_boundary is not None:
    self.session_boundary.complete_round(state, context)
manager.commit_background_spawn_round()
manager.activate_background_tasks()
```

第一步将本轮工具结果和状态作为一个完整边界提交；后两步才让后台管理器启动工作线程。若保存某个回执或整轮边界失败，流程就不会走到启动步骤。这样，模型已经收到的启动回执和实际开始消耗资源的子任务保持一致。

### 3. 子线程只报告结果，父线程更新父任务

每个子代理有独立的任务状态、对话和只读工具范围。工作线程完成后，把有限大小的结果放进线程安全队列。父线程稍后读取队列，更新父任务记录、结算预算并显示短通知。父 State（当前任务事实与进度）、对话历史和 session 文件不会由多个工作线程同时修改。

同步与后台子代理共用预算：默认每个父任务最多创建 3 个子代理，同时最多运行 2 个；本地配置可以再设更小的并发限制。排队任务也会占用预留额度。`token` 是模型处理文本的计量单位，和模型调用数、工具调用数一样受预算限制。失败、超时和取消都会形成可查询的结果。

取消是协作式的：父 Agent 发出取消请求后，正在等待的模型请求可能要先返回；子代理会在接下来的安全检查点看到请求后停止。系统不强行杀掉线程。

### 4. 活动或未领取的任务不能算完成

safe point（安全保存点）是一个状态完整、没有未处理后台任务的会话快照。活动线程本身无法保存成可恢复的任务，所以手动 `/save` 会列出活动或未领取子任务并拒绝保存；自动保存则等待任务收束。

命令行界面退出、输入结束、`/new` 或 `/reset` 时会先请求取消并等待一段有限时间。若线程无法按时结束，界面保留旧任务并报告相关 ID。即使父模型已输出普通结束文本，只要子任务还在运行或结果还未领取，父任务就不会进入 `done`。子代理报告也不会自动证明父任务的修改已经验证通过。

## 为什么这样设计

把“启动”和“读取结果”分开，父 Agent 可以先发起调查，再决定什么时候查看。它也保留了工具协议的一条简单规则：每次工具调用只有一个对应结果。

父线程独占父状态和会话写入，避免两个线程同时改同一段对话或存档。整轮提交闸门则确保不会出现“保存文件说已启动，但工作线程其实没启动”的模糊状态。

代价是后台任务只在当前程序进程中运行。退出后不能接管原工作线程；如果程序在结果领取前退出，恢复会把无法确认的工作标为中断，而不会自动重跑。

## 设计边界

若父程序在启动回合完整提交后退出、而结果尚未安全保存，恢复流程会记录任务为 `interrupted`（中断），不会重启旧线程或伪造报告，并按已预留的上限谨慎结算未知用量。用户仍须逐项处理崩溃恢复问题。

如果启动回执已经逐项保存，但整轮还没提交成功，子代理从未开始，恢复流程会关闭尚未启动的预算预留。已经完成但尚未领取的结果不能作为可恢复子会话保存；清理时会先把它保留到父会话成功提交为止。

子代理始终是只读调查者：不能写文件、运行 shell、使用 MCP（将外部工具服务接入 Agent 的协议）、继续委派或替父 Agent 做验证。要在另一次命令行进程中继续同一段子代理对话，需要第 49 课的会话快照能力。

## 关键流程

```text
父模型提出一轮后台启动
  → 检查参数、角色、任务阶段与权限
  → 预留子代理数和模型/工具/token 预算
  → 按调用顺序提交每个启动回执
  → 整轮保存成功后，后台任务管理器启动可运行的工作线程
  → 工作线程将结果放入完成队列
  → 父线程收集结果并显示 ID/状态通知
  → 父模型查询状态，并显式领取报告

失败路径：
  任一启动回执或整轮保存失败 → 这一轮不启动 worker
  旧进程退出而结果未保存 → 标记 interrupted，不自动重跑
  结果仍在运行或尚未领取 → 父任务不能进入 done
```

## 运行与观察

配置好本地模型后，发起一条明确要求使用后台子代理的任务：

```bash
PYTHONPATH=src python -m mini_agent "请用 reviewer 角色启动一项只读调查，检查 src/mini_agent 的工具注册和参数校验；调查进行时继续查看父侧调用路径，之后查询状态并领取报告"
```

观察是否先出现带 `child_session_id` 的启动回执，之后父 Agent 继续工作。调查结束时，终端通知只显示任务 ID、状态和可用的 `result_id`；报告正文应在父模型调用 `get_subagent_result` 后才进入对话。

若父 Agent 尝试直接结束而任务仍活动或结果未领取，命令行界面会说明尚未收束的任务，父任务保持活动状态。这能帮助区分“子代理已启动”和“子代理报告已交回”。

## 实现拆解

后台任务管理器负责准备和管理子任务，但在整轮提交前不启动线程。父 Runtime 按调用顺序提交启动结果和 `schema 3` 边界，再通知管理器释放已预留任务。

每个工作线程复用项目同一套 Agent 运行循环（安排模型请求、工具调用和结果），但只写入自己的结果队列。实现中，工作线程把完成信息放进队列：

```python
self._background_events.put((selected.task.subagent_id, result))
```

父侧运行循环在安全边界收集结果、核对身份、结算使用量并通知模型。领取报告时，父状态还会核验子任务 ID、委派 ID、结果 ID 和结果摘要。

恢复时，恢复模块检查已提交的后台启动记录。它读取父会话中的结构化任务记录，不读取子对话，也不尝试恢复 Python 工作线程。

## 本版特性、下一课与代码索引

本版保留同步 `delegate_task`，同时提供四个后台工具。父 Agent 可以在子代理调查期间继续工作，但必须显式查询并领取结果；启动确认要在整轮安全提交后才会真正启动子任务。

下一课会让已完成、已领取的调查在保存后继续追问。v0.48 仍不会把工作线程或子代理对话保存到磁盘。

固定在 v0.48 的代码索引：[后台任务管理](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.48/src/mini_agent/delegation.py)、[启动提交顺序](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.48/src/mini_agent/runtime.py)、[父任务阶段策略](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.48/src/mini_agent/agent.py)、[生命周期记录](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.48/src/mini_agent/state.py)、[会话保存与恢复](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.48/src/mini_agent/session.py)。
