# 第 47 课：给子代理设定角色

上一课：[受限 HTTP MCP、文本 Resource 与 Prompt](46-mcp-http-resources-prompts.md) · [教程总览](README.md) · 下一课：[进程内后台子代理](48-background-subagents.md)

代码快照：`v0.47` · 相邻差异：`v0.46..v0.47`

本课命令使用 Bash/zsh；命令、代码链接和示例都对应 `v0.47`。

## 本课目标

Agent 是一种会向语言模型提问、按需调用工具来完成任务的程序。正在处理用户任务的 Agent 可以把一部分工作交给另一个独立的 Agent；前者叫“父 Agent”，受限执行调查的那个叫“子代理”。

上一版的子代理都使用同一套通用指令和只读工具。父 Agent 虽然能在任务文字里说“请审查代码”，运行时却不能据此限制子代理的职责。本课给 `delegate_task` 增加 `agent_profile`：父 Agent 可以选择 `reviewer` 等内置角色，或从本地配置加载自定义角色。

读完后，你应能用自己的话说明：角色决定子代理怎样工作、可以用什么工具；`model_profile` 则选择它使用哪个已配置模型。没有选择角色的旧调用仍按原有方式运行。

## 前置条件与版本切换

只需要基础 Python、终端和 Git。无需先理解 Agent 内部实现；先把“子代理”理解成一个权限更窄、专门做只读调查的独立助手即可。想看它最初怎样出现，可再读[第 34 课：最小委派](34-minimal-delegation.md)。

先查看上一版到本版的文件变化，再切到固定代码快照：

```bash
git checkout v0.46
git diff --stat v0.46..v0.47
git checkout v0.47
```

读完后运行 `git checkout -` 回到切换前的分支。

## 上一版的问题

v0.46 的 `delegate_task` 是一次同步委派：父 Agent 发出调用后要等子代理完成，再收到结果。它把一份固定的只读工具清单交给每个子代理，因此无法通过一个明确的角色设置来区分“找实现入口”“审查缺陷”或“分析测试”。

如果把一整套父 Agent 的工具和授权直接交给子代理，它就可能做父任务没有委派的事。v0.47 让父侧配置明确列出每个角色的职责和工具，并在创建子代理前检查这些限制。

## 新增与改动文件

| 文件 | 变化 | 读者可从这里看到什么 |
|---|---|---|
| [agent_profiles.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/agent_profiles.py) | 新增 | 内置角色和本地角色如何校验、冻结。 |
| [delegation.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/delegation.py) | 修改 | 怎样把角色、工具范围、模型和授权结果交给子代理。 |
| [tools/delegation.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/tools/delegation.py) | 修改 | `delegate_task` 怎样接收可选角色。 |
| [skills.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/skills.py) 与 [tools/skill.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/tools/skill.py) | 修改 | 子代理如何只看到获准的 Skill。Skill 是一份供 Agent 按需读取的本地操作说明。 |
| [state.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/state.py)、[session.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/session.py)、[trace.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/trace.py) | 修改 | State（任务事实与进度）、session（保存任务的会话文件）和 Trace（只读回放用的事件记录）如何核对角色身份。 |
| [config_example.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/config_example.py) | 修改 | 本地角色配置应采用什么形状。 |

## 版本变更定位

先看 v0.46 已有的同步调用路径。它说明本课是在原有委派入口上增加角色限制：

```text
[旧] 父 Agent
  → delegate_task 参数检查与授权
  → 子代理使用通用提示和固定只读工具
  → 等待调查完成
  → 把结果交回父 Agent
```

v0.47 在这条路径中加入角色选择、工具交集和 Skill 授权。图中的“工具交集”意思是：子代理最终可用的工具，必须同时出现在项目白名单、角色允许列表和这次请求的工具列表中。

```text
[旧] 父 Agent
  → [~] delegate_task（可选 agent_profile）
  → [+] agent_profile：选择内置或本地角色
  → [C] 父侧校验角色、模型和工具范围
  → [C] 父侧逐个检查角色申请的 Skill
  → [~] 建立受限子代理 → 调用独立子 Agent → 返回报告
  → [B] 角色/模型无效或没有共同工具 → 在子模型请求前拒绝
```

图例：`[旧]` 为 v0.46 已有，`[+]` 为 v0.47 新增，`[~]` 为修改，`[C]` 为主要校验或使用方，`[B]` 为本版边界。箭头表示调用或数据流。这里最重要的变化是角色在父侧先变成明确的限制，然后子代理才开始工作。

## 核心概念与数据结构

### 1. 角色和模型回答不同的问题

角色回答“这次调查要怎样做、能用哪些工具”；模型别名回答“由哪个本地配置的模型来执行”。例如，`reviewer` 是角色，`model_profile` 则从本地模型配置中选择一个别名。角色若指定模型就使用该模型；否则沿用原有子代理默认模型。调用者若另传模型别名，必须与这个最终选择一致，否则调用会在发出子模型请求前被拒绝。

内置角色有 `explorer`、`reviewer`、`tester` 和 `general`。它们都只能使用子代理只读白名单中的工具。下面是调用中的关键字段示意；真实调用还需要任务目标、范围和限制等信息：

```json
{
  "agent_profile": "reviewer",
  "requested_tools": ["read_file", "grep"]
}
```

`requested_tools` 是父 Agent 为这一次调查提出的工具申请。运行时还会检查路径是否在任务范围内；把工具写进请求本身不会绕过这项检查。

### 2. 自定义角色在任务开始时冻结

自定义角色写在本机未跟踪的 `config_local.py` 中，不应把真实密钥或私人配置放进示例文件。每个角色至少需要职责说明、子代理指令和工具列表；模型、权限和 Skills 都是可选项：

```python
AGENT_PROFILES = {
    "api_reader": {
        "description": "追踪 API 的只读实现路径",
        "prompt": "指出入口、调用关系和支持结论的文件证据。",
        "tools": ["read_file", "grep"],
        "permissions": {"grep": "allow"},
        "skills": [],
    },
}
```

`permissions` 只能为角色自己的工具设置 `allow` 或 `deny`。`deny` 会从本次可用工具中移除对应工具；角色不能借配置扩大子代理的总白名单。Agent Runtime（负责安排模型请求、工具和结果的运行循环）创建时会检查并冻结本地角色定义，因此运行期间修改配置不会悄悄改变正在进行的任务。

只保存角色 ID 和配置指纹供 State、session 和 Trace 核对。指纹是用于识别配置是否改变的摘要；角色提示正文不会因此进入这些摘要。

### 3. Skill 仍由父 Agent 逐项授权

角色列出某个 Skill，只表示子代理“可以申请读取它”，不代表已经获得许可。父 Agent 会在子代理启动前，按每个准确的 Skill ID 询问当前权限策略。拒绝或被策略禁止的 Skill 不会出现在子代理的目录和工具中；配置引用了不存在的 Skill 时，此次委派会失败。

如果获准，子代理通过 `skill(name)` 按需读取正文。Skill 正文只是普通工具返回的资料，不能授予 shell 等新工具权限，也不能覆盖受保护的系统指令。Skill 正文不会进入 State 或 Trace 摘要，也不能证明父任务已经通过验证；开启 `/save` 后，已加载正文仍可能作为普通工具历史保存在 session 中，所以会话文件应按任务资料妥善保护。

没有传 `agent_profile` 的旧委派不会获得角色 Skill 能力。

例如，下面这个字段表示该角色可以申请读取名为 `api-contracts` 的 Skill；它本身还没有批准这次读取：

```json
{
  "skills": ["api-contracts"]
}
```

### 4. tester 只能分析测试，不能运行测试

下面是选择 `tester` 时的关键字段示意；真实调用还要包括目标和文件范围：

```json
{
  "agent_profile": "tester",
  "requested_tools": ["read_file", "grep"]
}
```

`tester` 可以阅读实现与测试代码，建议要运行哪些命令或补充哪些用例；它没有运行测试的工具，不能把建议写成“测试已经通过”。是否执行测试仍由父 Agent 根据自己的任务流程决定。子代理的报告只是调查材料，不能代替父 Agent 执行验证。

## 为什么这样设计

把角色放在本地配置中，父 Agent 就能用一个简短 ID 选择一套固定职责和工具边界。把模型别名单独保留，则避免把“做什么”和“由哪个模型做”混成同一个设置。

代价是配置更严格：错误的角色、模型或 Skill 不会自动猜测替代项；角色给出的工具清单也只能收窄既有权限。这样父侧授权仍是唯一入口，子代理不会继承父 Agent 的全部能力。

## 设计边界

v0.47 的子代理仍然是同步、单层和只读的。父 Agent 必须等待本轮子代理完成；子代理不能改文件、运行 shell、访问外部工具服务、创建计划、做验证或决定父任务已经完成。`tester` 只能建议测试，父 Agent 仍须自己运行并检查验证结果。

本版还没有后台启动、稍后领取结果或跨进程继续同一子会话的能力。前者在下一课介绍，后者要到第 49 课。

## 关键流程

读下面的步骤时，可以把它理解为一次“先检查权限，再开始委派”的过程：

```text
父模型提出 delegate_task
  → 父侧解析角色和模型
  → 取项目白名单、角色工具和本次请求的交集
  → 父侧逐个授权角色申请的 Skill
  → 建立只有获准工具/Skill 的子代理
  → 子代理调查并返回结构化报告
  → 父侧记录角色摘要并接收报告

角色、模型或工具范围无效：子模型请求前拒绝
Skill 被拒绝：该 Skill 不会交给子代理
```

## 运行与观察

配置好本地模型后，用 Bash/zsh 启动一条只读审查任务：

```bash
PYTHONPATH=src python -m mini_agent "只读审阅 src/mini_agent 中的 HTTP 重试边界"
```

观察父模型是否调用 `delegate_task` 并选择 `reviewer`。如果调用成功，子代理仍会先完成调查，父 Agent 随后才继续；结构化结果中会出现角色 ID 和配置指纹。若使用了角色 Skill，授权提示会逐项显示 Skill ID；拒绝某个 ID 后，子代理就看不到它。这些现象分别说明角色身份已记录，Skill 访问仍由父侧控制。

## 实现拆解

参数检查会从冻结的角色目录解析角色，并核对显式模型别名；委派合同（记录这次子任务的目标、范围和限制）再带上有效工具子集与角色指纹。没有角色的调用保留旧合同形状。

在子任务启动前，委派管理器会通过父侧 PermissionGate（决定工具是否允许使用的权限检查）逐项检查 Skill。之后，子代理收到只含获准工具和 Skill 的独立视图，并运行项目共用的 Agent Runtime。子代理不会获得父 State、父权限或父验证证据。

需要对照实现时，查看 [角色配置校验](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/agent_profiles.py)、[委派运行流程](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/delegation.py)和[角色工具注册](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/tools/delegation.py)。

## 本版特性、下一课与代码索引

本版让父 Agent 能以固定内置角色或本地自定义角色开展同步只读调查，并由父侧逐项批准角色可申请的 Skill。未指定角色的旧调用继续使用原行为。

下一课会处理“父 Agent 不必等子代理完成”的问题：子代理先在当前进程里后台运行，父 Agent 之后查询状态并领取结果。

固定在 v0.47 的代码索引：[角色目录](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/agent_profiles.py)、[委派执行](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/delegation.py)、[Skills 目录](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/skills.py)、[父子工具视图](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/tools/base.py)、[持久结果校验](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/session.py)。
