# 第 49 课：可续接子会话

上一课：[进程内后台子代理](48-background-subagents.md) · [教程总览](README.md) · 下一阶段：评估与回归

代码快照：`v0.49` · 相邻差异：`v0.48..v0.49`

本课命令使用 Bash/zsh。源码链接和示例对应 v0.49 快照。

## 本课目标

上一课的 `child_session_id` 像一张查找编号：它能让父 Agent 找到当前进程里的后台任务，却不包含子代理已经读过什么、得出什么结论。进程退出后，编号还可能保存在父会话里，但负责保存子对话的内存已经消失。因此，只有保存 ID 不足以继续同一场调查。

本课为已完成并由父 Agent 领取的调查增加 `followup_subagent`。它为原来的子会话追加一份完整新合同，保留子代理自己的历史；父任务保存安全点时，也会把有界的空闲子会话快照一起写入。恢复后，父 Agent 可以用原 ID 继续调查。

## 前置条件

建议先读第 47 课了解具名角色和 Skill 授权，再读第 48 课了解启动确认、结果领取与后台 worker。Tag `v0.49` 由用户手动创建；在 tag 建立前，可在当前工作区阅读实现。建立后，在本地切换到固定快照并查看相邻变化：

```bash
git checkout v0.49
git diff --stat v0.48..v0.49
git diff v0.48..v0.49 -- src/mini_agent/delegation.py src/mini_agent/session.py src/mini_agent/resume.py
```

读完后运行 `git checkout -` 回到切换前的分支。

## 新增与改动文件

续接会经过父工具、共享子 Runtime、State、安全点保存和恢复，因此要同时修改这些边界：

| 文件 | 变化 | 作用 |
|---|---|---|
| [tools/delegation.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.49/src/mini_agent/tools/delegation.py)、`tools/__init__.py` | 新增/修改 | 注册 `followup_subagent`，复用后台工具组与父侧权限入口。 |
| [delegation.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.49/src/mini_agent/delegation.py) | 修改 | 校验 followup 合同、恢复子 Context、运行新一轮并生成有界快照。 |
| [state.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.49/src/mini_agent/state.py) | 修改 | 记录每轮独立的委派结果和每个子会话的累计预算、领取结果身份。 |
| [agent.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.49/src/mini_agent/agent.py)、[runtime.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.49/src/mini_agent/runtime.py) | 修改 | 将 spawn 与 followup 统一纳入纯后台启动回合，整轮提交后再启动 worker。 |
| [session.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.49/src/mini_agent/session.py) | 修改 | 用 schema 4 原子保存父状态与空闲子快照，并检查快照大小、历史和结果引用。 |
| [resume.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.49/src/mini_agent/resume.py)、`__main__.py` | 修改 | 重新绑定本地角色、模型和 Skill Catalog；装入兼容快照并显示不能续接的原因。 |

执行上面的 `git diff --stat` 后，重点观察变化不只在新增工具：schema、父侧生命周期和恢复装配也同时扩展。

## 关键流程

先回看 v0.48 的真实基线。后台 Manager 在当前进程中持有子 Runtime 和 Context；父 Agent 领取的是结果，父安全点没有可恢复的子历史。

```text
[旧] 父 AgentRuntime
  → spawn_subagent → [C] PermissionGate / 阶段闸门
  → 整轮 durable commit → [C] Manager 启动子 Runtime
  → 完成队列 → 父 State 更新 → get_subagent_result 领取
  → [B] 进程结束：结果和 child ID 可记账，子 Context 不随之恢复
```

v0.49 在“结果已领取”之后加入新一轮，但仍保留父线程控制启动时机。下面的图把上一版节点保留下来，并标出续接、保存、恢复及失败路径。

```text
[旧] 父 AgentRuntime
  → [~] spawn_subagent / [+] followup_subagent（只允许纯启动调用回合）
  → [C] ToolExecutor / PermissionGate / 阶段与合同校验
  → [~] 按模型顺序提交启动确认及 role=tool
  → [B] schema 4 整轮提交失败：不启动本轮 worker
  → 整轮 committed → [C] Manager 启动子 Runtime
                         ↓
       [旧] 首轮 Context 或 [+] 已领取快照 Context
          → [~] 追加完整新合同 → AgentRuntime.run()
          → 本轮结果 + 候选快照 → [C] 父线程收集和预算结算
          → get_subagent_result 领取 → [~] 快照变为 idle 可保存
                         ↓
 [C] SessionStore 原子保存父 State / Context / tool_boundary / idle child_sessions
                         ↓
 [C] /resume 重建当前 Catalog 并核验指纹 → [B] 不兼容项只报告，不回退模型
```

图例：`[旧]` v0.48 已有；`[+]` v0.49 新增；`[~]` v0.49 修改；`[C]` 主要消费者；`[B]` 本版边界。箭头代表调用、控制或数据流。工具结果在父历史中仍按模型顺序配对；子 worker 只产出结果候选，快照要等父线程确认领取后才可作为 idle 快照保存。

## 实现拆解

### 1. 每一轮都要重新提交完整调查合同

`followup_subagent` 接受原 `child_session_id` 和与 `spawn_subagent` 相同的调查字段。下面的片段展示合同的形状；作用域和工具仍需同时通过现有只读白名单、角色工具集和 ScopeGate。

```json
{
  "child_session_id": "<上一轮的 UUID>",
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

合同必须重新明确目标、范围、限制、预期发现和要请求的工具。调用方不能传新的角色或模型；Manager 按已冻结的子会话身份重建本轮任务。角色配置列出的 Skill 若要在新一轮读取，父侧仍要按该 Skill ID 再次获得授权。

工具先检查该子会话是否属于当前父任务、工作区和已冻结角色/模型，上一轮是否 `completed` 且已领取，以及工具和累计预算是否仍足够。同一进程内还会重新检查冻结的 Skill 文件身份。状态不是 idle、结果未领取、合同越权或身份变化时，拒绝在子 LLM 请求前返回。成功确认会带 `round_index`；完成后仍通过 `get_subagent_result(child_session_id)` 领取本轮结果。

### 2. 子历史恢复与本轮预算分开

保存的 `ChildSessionSnapshot` 是父 session 内一个私有、有界的子 Context 检查点。它记录父 task 和 workspace 身份、角色与模型来源摘要、Skill 文件身份、当前轮次、累计 usage、最近领取结果的 ID/hash、子 Context 导出及允许延续的只读观察事实。它不保存线程句柄或运行中的 worker。

续接时，`SubagentRunner` 从该 Context 继续，并把新合同追加为新的用户侧输入，然后交给共享的 `AgentRuntime.run()`。每次 followup 会新建本轮运行状态和 SubagentBudget 计数；累计预算则从快照累加。父 history、其他子会话、父 Plan、权限批准、generation 和 verification 不会复制给子代理。

一轮新结果有自己的 `delegation_id`、`result_id` 和 usage。领取旧轮结果仍返回旧身份，不会被新结果覆盖；重复领取同一轮也不会重复结算。子会话硬上限如下：

| 累计上限 | 说明 |
|---|---|
| 4 轮 | 包含初始调查轮 |
| 16 次 LLM 调用 | 各轮 SubagentBudget 之和不得超限 |
| 48 次工具调用 | 只统计获准的子只读工具 |
| 64,000 tokens | 按本地 usage 口径累计 |
| 240 秒 | 将各轮已用时间与本轮预留一起检查 |

父任务原有聚合预算和并发槽位仍生效；续接不会消耗新的 `created_subagents` 名额。达到任一累计上限后，已经交付的结果仍可读取，但不会再启动后续一轮。

### 3. 把可续接快照放进同一个安全点

只在每一轮完成结果被领取后，Manager 才把该轮候选快照提升为 idle 快照。`/save` 将父 State、Context、当前工具边界和所有 idle 子快照一起交给 `SessionStore`，再作为一个 schema 4 JSON 文件原子替换。快照数量最多 3 个；单项最多 256 KiB，合计最多 720 KiB。单项快照生成失败时，已完成的报告仍可领取、实际用量仍会结算，但该子会话不能继续追问；全部快照合计超限时，保存会报出相关子 ID。历史或身份不会为凑大小而被截断。

恢复前，SessionStore 会检查快照与父 lifecycle、结果 ID/hash、轮次、累计用量和 Context 中 assistant/tool 消息配对是否一致。恢复后再从当前本地配置重建角色、provider binding 与 Skill Catalog，核对冻结指纹和 Skill 文件身份。若一个子会话身份已变化，它会被单独标为不可续接并说明原因；父任务和其他有效子快照仍可继续恢复。

执行时可以在父 Agent 中输入 `/save`，等没有活动或待领取子任务后再正常退出。CLI 提示的 session ID 可用于后续启动：

```bash
PYTHONPATH=src python -m mini_agent --resume <session_id>
```

观察到父任务恢复后仍可看到原子会话里的 idle 子会话，并能让模型用原 `child_session_id` 发起 followup。若该轮在关闭进程前仍运行或结果还未领取，`/save` 不会把它伪装成可恢复快照；恢复也不会启动旧 worker。

## 为什么这样设计

仅保留 ID 无法还原对话；无界保存子 history 又会让一个父 session 的大小和恢复校验失去上限。因此，本版只保存已领取成功轮次的有界 Context，并把它与父安全点放进一次原子提交。这样恢复不会把父侧事实和子侧调查混在一起，也不会出现“父 session 已更新、子快照仍是旧文件”的半提交状态。

每轮采用完整新合同，是为了让目标、范围、预期结果和工具申请在续接点重新可见、重新校验。角色和模型固定，Skill 权限重新取得，避免把历史授权当作永久授权。累计计量则让通过新轮次或重启不会清零资源消耗。

## 设计边界

只有成功且已领取的结果可以续接。失败、超时、取消、中断、运行中和待领取状态都不可续接；不支持跨父 task 或 workspace 转移，不支持更换角色/模型，也不恢复旧进程、旧 worker 或旧权限。

schema 1/2/3 仍可读取，但它们没有 v0.49 子快照。新安全点采用 schema 4；父 State 和 Trace 只保留身份、hash、轮次与用量摘要。子 Context 正文保存在私有 session 快照里，仍应按普通会话敏感数据管理。

## 本版特性、下一课与代码索引

第 49 课完成阶段十三的后台子会话闭环。下一阶段转向 Evaluation & Regression：评估已有能力、成本和可靠性，不默认增加更复杂的多代理基础设施。

下面的源码链接固定在本课快照，便于将教学说明和具体实现对照：

- [Followup 工具合同与参数校验](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.49/src/mini_agent/tools/delegation.py)
- [子 Runtime、快照和后台 Manager](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.49/src/mini_agent/delegation.py)
- [父侧 lifecycle 和累计预算](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.49/src/mini_agent/state.py)
- [schema 4 安全点与快照验证](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.49/src/mini_agent/session.py)
- [恢复时重新装配与兼容性检查](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.49/src/mini_agent/resume.py)
