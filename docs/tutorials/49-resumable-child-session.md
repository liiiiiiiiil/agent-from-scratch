# 第 49 课：恢复并继续子会话

上一课：[让子代理在后台调查](48-background-subagents.md) · [教程总览](README.md) · 下一阶段：评估与回归

代码快照：`v0.49` · 相邻差异：`v0.48..v0.49`

本课命令使用 Bash/zsh；源码链接和示例都对应 `v0.49` 快照。

## 本课目标

第 48 课给每个后台调查一个 `child_session_id`。这个编号能让父 Agent 在当前程序里找到子任务，但它本身不包含子代理已经读过的对话。程序退出后，线程和内存中的对话都会消失；只保存编号仍无法继续调查。

本课允许父 Agent 在结果已完成并领取后保存子代理自己的对话快照。之后重启程序并恢复父会话，父 Agent 可以用原来的编号继续追问同一个子代理。

先区分两个编号：

- 父会话的 `session_id` 用来恢复用户的整个任务。
- 子会话的 `child_session_id` 用来继续其中一项独立调查。

读完后，你应能说明什么条件下子会话可以保存、恢复后哪些内容会沿用，以及为什么每轮追问都要重新说明调查范围。

## 上一版的问题

v0.48 只在当前命令行进程中保存活动的子代理对象和对话。父会话可以记录某次调查已领取，却没有足够资料让另一个进程恢复该调查。

另一个难点是保持资料一致：父会话写入了“结果已领取”，但子对话仍留在内存里时，程序退出会让两边记录不匹配。本版把父会话和已完成子会话的快照一起保存，并且只允许保存空闲、已领取的子会话。

## 前置条件与版本切换

建议先读第 47 课，了解角色和 Skill 授权，再读第 48 课，了解后台启动、结果领取与父侧保存边界。Skill 是 Agent 可按需读取的本地操作说明。

检查版本变化后切到本课快照：

```bash
git checkout v0.48
git diff --stat v0.48..v0.49
git diff v0.48..v0.49 -- src/mini_agent/delegation.py src/mini_agent/session.py src/mini_agent/resume.py
git checkout v0.49
```

读完后用 `git checkout -` 返回切换前的分支。

## 新增与改动文件

| 文件 | 变化 | 读者可从这里看到什么 |
|---|---|---|
| [tools/delegation.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.49/src/mini_agent/tools/delegation.py) | 修改 | 新工具 `followup_subagent` 的参数合同。 |
| [delegation.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.49/src/mini_agent/delegation.py) | 修改 | 怎样恢复子对话、开始新一轮并生成快照。 |
| [state.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.49/src/mini_agent/state.py) | 修改 | 如何分别记录每轮报告、领取情况和累计用量。 |
| [agent.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.49/src/mini_agent/agent.py) 与 [runtime.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.49/src/mini_agent/runtime.py) | 修改 | 初次启动与继续调查怎样遵守同一整轮提交规则。 |
| [session.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.49/src/mini_agent/session.py) | 修改 | schema 4 怎样把父会话与空闲子快照一起保存和检查。 |
| [resume.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.49/src/mini_agent/resume.py) | 修改 | 重启时怎样重建本地配置并判断哪些子会话还能继续。 |

这里的 `schema 4` 是 v0.49 使用的会话文件格式版本。与 v0.48 相比，变化不仅是增加一个追问工具，还包括保存格式和恢复过程。

## 版本变更定位

v0.48 的父 Agent 能在一个进程内启动子代理、收到结果并领取报告；父安全保存点不会带上子代理的完整对话。State 是父任务当前事实与进度的结构化记录；Context 是一次模型请求会看到的对话和资料：

```text
[旧] 父 Agent
  → spawn_subagent → 保存启动回执 → 启动子代理
  → 父线程收到完成结果 → get_subagent_result 领取
  → [B] 进程结束后：子对话和运行线程不恢复
```

v0.49 允许已领取的子会话继续一轮，并把空闲子会话快照与父会话原子保存。这里的子 Context 就是子代理自己的对话视图。

```text
[旧] 父 Agent 与后台子代理
  → [~] spawn_subagent / [+] followup_subagent
  → 父线程按顺序提交启动回执
  → 整轮保存成功 → 后台任务管理器启动子任务
  → 子 Context 继续调查 → 本轮结果
  → 领取结果 → 子快照变为空闲，可保存
  → [C] 一次保存父 State / Context 与空闲子快照
  → [C] 恢复时重建当前角色、模型、Skill 配置
  → [B] 配置身份不匹配：报告该子会话不能续接
```

图例：`[旧]` 为 v0.48 已有，`[+]` 为 v0.49 新增，`[~]` 为修改，`[C]` 为主要校验或使用方，`[B]` 为本版边界。箭头表示调用、控制或数据流。只要一轮还没完成并领取，子快照就不能作为空闲状态保存。

## 核心概念与数据结构

### 1. 每次追问都提交一份新调查合同

`followup_subagent` 中的 followup 意为“继续上一轮调查”。它使用原来的 `child_session_id`，并要求调用者重新写明这轮要做什么、可查看哪些文件、应交付什么发现，以及申请哪些只读工具。委派合同就是这组目标、范围、限制和工具申请。例如：

```json
{
  "child_session_id": "<上一轮的子会话编号>",
  "goal": "检查配置加载是否也需要恢复适配",
  "scope": ["src/mini_agent"],
  "constraints": ["只读调查，不修改文件"],
  "expected_findings": ["指出相关入口和调用关系"],
  "requested_tools": ["read_file", "grep"],
  "selected_parent_facts": ["父任务正在整理恢复流程"],
  "purpose": "investigation",
  "budget": {
    "max_rounds": 2,
    "max_llm_calls": 2,
    "max_tool_calls": 4,
    "max_tokens": 4000,
    "timeout_seconds": 20
  }
}
```

示例里的尖括号文字是说明占位符，实际调用时要换成上一轮返回的子会话编号。`selected_parent_facts` 用来明确告诉子代理哪些父任务背景与本轮有关。子代理不是自动接收父 Agent 的全部新对话，所以新合同要把本轮真正需要的背景说清楚。角色和模型沿用原设置，调用者不能趁追问时换成权限更大的角色或模型。每次需要读取角色配置中的 Skill 时，父侧都要重新检查该 Skill 的授权。

在发出子模型请求前，运行时会确认：这是当前父任务和工作区的子会话；上一轮已完成且已领取；角色、模型和 Skill 身份仍匹配；本轮合同没有越权；累计预算还有余量。未收束、未领取或身份不匹配的子会话不能继续。

### 2. 对话快照保存历史，预算仍然累计

Child Session Snapshot（子会话快照）是父会话文件里一份有界的子代理检查点。它包含子代理对话、角色和模型来源摘要、Skill 文件身份、最近领取结果的编号与摘要、轮次和累计用量等信息。它不包含 Python 工作线程，也不是一个能继续运行的后台进程。

下面摘出快照中几个关键字段；完整结构还会记录工作区、模型和 Skill 的身份摘要：

```python
@dataclass(frozen=True)
class ChildSessionSnapshot:
    child_session_id: str
    agent_profile: str
    round_index: int
    last_claimed_result_id: str
    cumulative_usage: UsageRecord
    context: dict[str, Any]
```

这些字段说明快照保存的是“哪个子会话、做到第几轮、上一份报告是什么、对话和用量到哪里”，而不是正在运行的线程。

恢复时，子代理可以基于原来的对话继续；但每次追问会重新建立本轮预算。累计上限仍包含之前各轮，不能通过保存和重启清零：

| 整个子会话的累计上限 | 含义 |
|---|---|
| 4 轮 | 包含最初的调查 |
| 16 次模型调用 | 所有轮次合计 |
| 48 次工具调用 | 只计算允许使用的子代理只读工具 |
| 64,000 tokens（模型处理文本的计量单位） | 所有轮次合计 |
| 240 秒 | 所有轮次已用时间与本轮预留一起检查 |

每轮有独立的委派结果和报告编号，所以领取新报告不会覆盖旧报告；重复领取同一轮也不会重复结算。父任务自己的总预算与并发限制仍然生效。继续旧子会话不会占用一个新的子代理名额。

### 3. 父会话与空闲子快照一起保存

用户必须先通过 `/save` 开启持久化。只有子代理已经完成、父 Agent 也已经领取结果后，系统才会把该轮快照标记为空闲。活动中的线程和待领取报告都不能作为可恢复快照保存。

负责写会话文件的保存模块，会把父任务状态、对话、当前工具回合的提交记录和空闲子快照放进同一个 schema 4 会话文件，再通过一次原子替换提交。原子替换意味着恢复时看到的是完整旧文件或完整新文件，不会只更新父会话而漏掉子快照。

保存模块先写完临时文件并确认写入，再调用标准库的文件替换操作：

```python
os.replace(temporary, target)
```

这一步让新文件整体替代旧文件；它不会先删掉旧会话，再逐段复制新内容。

每个父会话最多保存 3 个子快照；每个快照最多 256 KiB，总计最多 720 KiB。快照过大或校验失败时不会为了凑大小而截断对话或身份。单个子快照不能生成时，调查报告仍可领取并结算，但该子会话无法继续追问；总体快照超限时，保存会指出相关子会话 ID。

恢复时，程序会检查子快照是否与父记录中的领取结果、轮次和累计用量一致，然后从当前本地配置重新装配角色、模型和 Skill 目录。若角色、模型或 Skill 文件身份已经变化，该子会话会标为不能续接；系统不会偷偷切换到另一个模型。旧 schema 1/2/3 父会话仍可读取，但本来没有子快照，不能凭空恢复旧子对话。

### 4. 用原子保存和恢复命令走完一次续接

一条完整路径如下：

```text
1. 启动后台子代理
2. 等待收束，并调用 get_subagent_result 领取报告
3. 在命令行界面输入 /save，按提示记录父 session_id
4. 退出程序
5. 使用 --resume 恢复父会话
6. 让父 Agent 用原 child_session_id 调用 followup_subagent
```

恢复父会话的命令使用 Bash/zsh：

```bash
PYTHONPATH=src python -m mini_agent --resume <session_id>
```

这里的 `<session_id>` 是 `/save` 后命令行界面提供的父会话编号，不是子代理编号。恢复后，父 Agent 可以选用原来的 `child_session_id` 继续调查。若程序退出前子代理仍在运行，或报告尚未领取，保存和恢复都不会把它当作可续接的子会话。

## 为什么这样设计

只存编号无法还原子代理读过的对话；无限保存所有内容又会让会话文件和恢复校验没有上限。因此，本版只保存已成功完成、已领取的有限对话，并与父会话一起写入。

每轮重新提交合同，让新的目标和文件范围都能在续接点重新检查。角色、模型保持不变，Skill 权限重新获得确认；累计预算则跨轮保留。这样恢复提供的是同一受限调查的继续，而不是一份不受约束的新任务。

## 设计边界

只有成功完成并已领取的报告可以续接。失败、超时、取消、中断、运行中和待领取状态都不行。子会话也不能转移到另一父任务或工作区，不能更换角色、模型或继承旧权限。

父 State 和 Trace（供只读回放使用的事件记录）只保存子会话身份、结果摘要、轮次与用量等结构化资料；子对话正文存放在会话快照中。开启持久化后，这些内容会保存在本地 session 文件里，应按普通会话资料保护。

## 关键流程

```text
followup_subagent(child_session_id, 新合同)
  → 确认子会话属于当前父任务
  → 确认上一轮已完成并领取
  → 检查角色、模型、Skill、工具范围与累计预算
  → 顺序提交启动回执和工具结果
  → 整轮保存成功后，恢复子 Context 并运行新一轮
  → 父线程收集、结算；父 Agent 领取新报告
  → 更新空闲子快照
  → /save 将父会话与空闲快照原子写入

不符合条件 → 在子模型请求前拒绝继续
```

## 运行与观察

准备本地模型配置后，可在父任务中要求先做一次调查、领取报告，再要求保存并续接。关键观察点是：第一次与后续追问会用同一个 `child_session_id`；新一轮仍需明确目标和范围；没有领取报告时，命令行界面不会把子会话当作可保存的空闲快照。

父会话恢复后，观察父 Agent 是否能根据原子文件中的子快照发起 `followup_subagent`。如果本地角色、模型或 Skill 身份与保存时不同，系统会说明对应子会话不能继续，而不会把它静默切换成其他身份。

## 实现拆解

`followup_subagent` 通过父侧工具、权限和阶段检查，再交给后台任务管理器准备合同。它复用同一套子 Agent 运行循环；差别是先从快照恢复该子代理自己的 Context，再把新合同作为这一轮的新任务输入。

结果由父线程收集和结算。只有结果领取完成后，后台管理器才会提供可保存的空闲快照。`session.py` 将快照和父会话放进同一次提交；`resume.py` 恢复时重新读取当前本地配置并比对身份。线程句柄、运行中的任务和旧权限批准都不会从文件中复活。

## 本版特性、下一课与代码索引

v0.49 为已完成且已领取的后台调查增加可恢复的子对话快照，并增加 `followup_subagent`。父会话与空闲快照一起原子保存；累计预算、角色和模型边界跨轮保留。

第 49 课完成了后台子会话的续接链路。下一阶段转向 Evaluation & Regression，即评估现有功能的效果、成本和可靠性。

固定在 v0.49 的代码索引：[追问工具合同](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.49/src/mini_agent/tools/delegation.py)、[子代理运行循环与快照管理](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.49/src/mini_agent/delegation.py)、[父任务生命周期和累计预算](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.49/src/mini_agent/state.py)、[schema 4 保存与校验](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.49/src/mini_agent/session.py)、[恢复时的身份检查](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.49/src/mini_agent/resume.py)。
