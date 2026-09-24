# 第 47 课：具名子代理角色

上一课：[受限 HTTP MCP、文本 Resource 与 Prompt](46-mcp-http-resources-prompts.md) · [教程总览](README.md) · 下一课：阶段十三后续版本（规划中）

代码快照：`v0.47` · 相邻差异：`v0.46..v0.47`

本课命令使用 Bash/zsh。下面的代码链接和示例都对应 `v0.47`。

## 本课目标

上一版已经可以把只读调查交给同步子代理，但每个子代理使用同一套通用提示和工具范围。父 Agent 很难清楚表达“请专门找缺陷”“请分析测试覆盖”这样的分工。

本课给 `delegate_task` 增加可选的 `agent_profile`，让父 Agent 可以从几个固定角色中选择，也可以从本地配置加载自定义角色。角色会限制提示、工具和模型；可申请的 Skill 仍要由父侧逐个授权。读完后，你应能区分“角色配置”和“模型别名”，并说明为什么未指定角色的旧调用仍能按原样工作。

## 前置条件

只需要基础 Python、终端和 Git。建议先读第 34–39 课，了解子代理如何只读调查、怎样共享运行循环、受哪些预算限制，以及结果如何进入父会话。

查看相邻版本的变化并切换到本课代码：

```bash
git checkout v0.46
git diff --stat v0.46..v0.47
git checkout v0.47
```

阅读完毕后，用 `git checkout -` 返回切换前所在的分支。

## 上一版的问题

在 v0.46 中，`delegate_task` 的子代理可以使用 `calculate`、`read_file`、`list_dir` 和 `grep`。这些工具都只读，但每个子代理拿到相同的系统提示和固定工具视图。父 Agent 只能在任务文字里描述分工，运行时无法把角色选择变成明确的能力边界。

同时，本地 Skill 虽然能告诉父 Agent 怎样组合工具，但尚未有机制让特定只读子代理按需读取某个 Skill。直接把父 Skill Catalog 交给子代理会让它看到父侧全部条目和加载能力。因此 v0.47 要同时冻结角色能力，并在子任务启动前让父 PermissionGate 确认每个角色申请的 Skill ID。

## 新增与改动文件

角色定义、委派生命周期和现有 Skill 加载边界需要一起调整：

| 文件 | 变化 | 作用 |
|---|---|---|
| [agent_profiles.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/agent_profiles.py) | 新增 | 校验并冻结内置角色和本地自定义角色，生成不含提示正文的指纹。 |
| [delegation.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/delegation.py) | 修改 | 把角色和工具交集纳入委派合同；构建独立子提示、模型绑定、工具视图和可选 Skill 视图。 |
| [tools/delegation.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/tools/delegation.py) | 修改 | 为 `delegate_task` 增加可选 `agent_profile` 字段。 |
| [tools/base.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/tools/base.py) 与 [tools/skill.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/tools/skill.py) | 修改 | 只允许角色运行时使用受限 Skill 工具视图。 |
| [skills.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/skills.py) | 修改 | 从已冻结 Skill Catalog 构造只含父侧已授权 ID 的子视图。 |
| [state.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/state.py)、[session.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/session.py) 与 [trace.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/trace.py) | 修改 | 显式角色结果只保存角色 ID 和配置指纹，并校验它们与有序交付事实一致。 |
| [config.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/config.py) 与 [config_example.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/config_example.py) | 修改 | 增加空的 `AGENT_PROFILES` 默认值和仅含占位内容的自定义角色例子。 |

## 版本变更定位

图中的角色配置没有取代 v0.46 的同步工具回合；它在同一入口增加了一个可选身份，并决定子 Runtime 会看到哪些只读能力。

```text
[旧] v0.46：同步委派
父 AgentRuntime
    → delegate_task 参数校验
    → 父 PermissionGate 与 handler admission
    → DelegationManager.build_delegated_task
    → SubagentRunner（通用提示 + 固定只读工具）
    → 子 AgentRuntime
    → 结构化结果 → 父 Context 的 role=tool → 按序提交
```

```text
[~] v0.47：同一同步入口增加角色边界
父 AgentRuntime
    → delegate_task 参数校验 [+] agent_profile / model_profile 一致性
    → 父 PermissionGate 与 handler admission
    → DelegationManager.prepare_batch
       ├─ 冻结角色、模型和工具交集
       ├─ [C] 父 PermissionGate 逐个预授权角色 Skill ID
       └─ [B] 无效角色/模型或空工具交集 → 唯一有界失败结果
    → [~] SubagentRunner（角色提示 + 工具交集 + 已授权 Skill 子视图）
    → [旧] 独立子 AgentRuntime → 结构化结果
    → [~] State / schema 3 / Trace 记录角色 ID 与配置指纹 → 父侧按序提交
```

图例：`[旧]` v0.46 已有；`[+]` v0.47 新增；`[~]` v0.47 修改；`[C]` 主要消费者；`[B]` 本版边界。模型只提出角色 ID；父 Runtime 负责解析本地定义和授权，子 Runtime 只拿到冻结后的能力视图。

## 核心概念与数据结构

### 1. `agent_profile` 是角色，`model_profile` 是模型别名

`agent_profile` 选择一组本地规则，例如 `reviewer`；`model_profile` 选择 `ProviderCatalog` 里的一个模型别名。角色可以指定默认子模型，也可以沿用现有子代理默认值。父请求同时带两个字段时，运行时要求它们解析为同一个子模型；未知或超出子代理模型白名单的别名会在子 LLM 请求之前被拒绝，不会静默换成其他模型。

内置角色有 `explorer`、`reviewer`、`tester` 和 `general`。前三者的默认工具是 `read_file`、`list_dir`、`grep`；`general` 还包含没有副作用的 `calculate`。例如，父模型可以在旧委派参数末尾增加：

```json
"agent_profile": "reviewer"
```

运行时仍会检查角色工具、本次 `requested_tools` 和子代理总白名单的交集。交集中的文件工具还必须通过 ScopeGate，确保路径在委派范围里。

### 2. 本地角色配置在 Runtime 创建时冻结

自定义定义放在未跟踪的 `config_local.py` 的 `AGENT_PROFILES`。每项必须提供 `description`、`prompt` 和 `tools`；`model_profile`、`permissions`、`skills` 可选。角色 ID 以小写字母开头，最多 64 字符。内置名称不能被自定义值覆盖。受跟踪的 [配置模板](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/config_example.py) 仅展示占位角色：

```python
AGENT_PROFILES = {
    "api_reader": {
        "description": "追踪 API 的只读实现路径",
        "prompt": "指出入口、调用关系和能直接支持结论的文件证据。",
        "tools": ["read_file", "grep"],
        "permissions": {"grep": "allow"},
        "skills": [],
    },
}
```

这里的 `permissions` 只能对 `tools` 中的工具指定 `allow` 或 `deny`。`deny` 会从可用工具交集中移除该项；没有显式 `deny` 的角色工具仍可由本次 `requested_tools` 申请。子代理的 PermissionGate 是独立静态规则，不会向终端发起授权提问。

Catalog 会在父 Runtime 组装时检查角色字段、Skill ID、工具和模型别名，并冻结不可变定义。角色提示正文只用在子 system prompt，不进入 State、Trace 或 session。合同只携带角色 ID 和由角色配置生成的 SHA-256 指纹；指纹可区分角色配置变化，但不保存或显示提示正文。

### 3. Skill 仍要由父侧逐个授权

角色的 `skills` 列表只表示这些 Skill 可以被申请，并不自动等于授权。父侧在子 Runtime 启动前，用当前 PermissionGate 对每个精确 Skill ID 执行一次检查；用户拒绝或策略 deny 的 ID 不会进入子 Catalog、工具目录或 Context 元数据。若角色引用当前 Catalog 中不存在的 Skill，则直接拒绝此次委派，避免子代理在缺少预期资料时继续工作。

获准 Skill 只能通过 `skill(name)` 按需读取。子代理拿到的加载器只包含这次获准的 ID；即使模型猜出其他 Skill 名，也无法读取。正文仍是普通、不可信的 `role=tool` 结果，不授予 shell 或其他工具权限。它不会进入父 State、Trace 摘要或 verification evidence；若父会话开启 `/save`，子结果中也不会包含 Skill 正文。

没有 `agent_profile` 的旧 `delegate_task` 不会得到 Skill 能力。这里的授权边界是“父策略先准许精确 ID，子 Catalog 再限制可见范围”，而不是把父 PermissionGate 或整个 Skill Catalog 传给子代理。

### 4. `tester` 只能给验证建议

`tester` 角色没有 shell 或测试运行工具。它检查测试代码与被测实现，提出应该运行的命令或需要补的用例，但不能声称测试已经运行或通过。报告校验会拒绝常见的“tests passed / 测试通过”表述，并要求 `limitations` 明确说明本次没有执行测试。它的输出只是父 Agent 的调查资料；是否运行测试仍由父 Agent 走自己的计划和权限流程决定。

## 为什么这样设计

角色被实现为受信任的本地配置，任务合同与选中的父侧事实仍作为普通用户侧输入交给子代理。这样角色提示可以规定工作方式，而文件内容和工具结果仍不能借着合同覆盖 system 规则。

工具权限由多个集合求交，不允许角色配置扩大四工具白名单，也不允许父请求越过角色上限。Skill 的授权则留在父侧，因为只有父侧持有当前任务的实际 PermissionGate 和交互授权结果。子 Runtime 收到的只是已获准 ID 的独立只读视图。

身份指纹也只在明确选择角色时写入合同、结构化状态和持久结果。因此旧调用的合同哈希、结果 JSON 和 session 字段保持 v0.39 的形状；显式选择不同角色时，则可以在回放记录中辨认角色身份，而不保存角色提示正文。

## 设计边界

v0.47 仍然只支持同步、单层、只读委派。父 Agent 在子代理完成并提交本轮结果前不能进入下一次模型调用。角色不能运行 shell、启动进程、修改文件、操作 MCP、创建计划、恢复会话或决定父任务完成。

`tester` 的硬工具限制保证它不能运行测试；报告检查补充了对常见“测试已通过”说法的拦截。自然语言无法穷尽所有误导表达，因此父 Agent 仍需把子结果视作调查材料，并自行运行计划内验证。

角色 Skill 必须由当前父侧 Catalog 发现并逐个授权。配置里的未知 Skill ID 不会回退到其他来源，而是拒绝此次委派。`config_local.py` 和 session 敏感目录（包括实际使用的自定义 `SessionStore.root`）的路径检查会使用 realpath，覆盖直接路径和符号链接目标。

本版没有后台子代理、异步结果领取或子会话续接。后续版本计划可能研究这些能力，但它们不是本课已实现的接口。

## 关键流程

下面展示一次角色委派从参数到回传结果的主要过程。只有角色列出的 Skill 会进入“父侧授权”步骤；不带角色的调用绕过该步骤并保留原有合同。

```text
父模型提交 delegate_task
  → 冻结角色与模型别名
  → 计算白名单 ∩ 角色工具 ∩ requested_tools
  → 父 PermissionGate 逐个检查角色 Skill ID
  → 构造只含有效工具和已授权 Skill 的子 Runtime
  → 子模型调查并提交结构化报告
  → 父 State / durable boundary 校验角色身份后按序交付

角色未知、模型冲突、工具交集为空：在子 LLM 请求前形成失败结果
Skill 被拒绝或文件被替换：Skill 不可用或返回有界加载错误
```

## 运行与观察

使用已配置的 LLM 网关启动一次命令行首条任务，例如“只读审阅 `src/mini_agent` 中的 HTTP 重试边界”。父模型需要调用 `delegate_task` 并提供 `agent_profile: reviewer`。观察返回的结构化 JSON：当角色明确选择时会出现 `agent_profile` 和配置指纹；子代理仍同步返回，之后父 Agent 才继续处理。

如果配置了角色级 `skills`，授权提示会逐项显示 Skill ID 与来源。拒绝某个 ID 后，子 Context 的 Skill 目录不应再展示它；这说明父授权结果已经变成子 Runtime 的可见能力边界。

## 实现拆解

参数校验先从冻结的 `AgentProfileCatalog` 解析角色，并对显式模型别名做一致性比较；构建 `DelegatedTask` 时再将有效工具子集和角色指纹纳入合同哈希。[`validate_delegation_arguments` 与 `build_delegated_task`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/delegation.py) 仍保留无角色分支的旧字段集。

在批量预算预留之前，`DelegationManager` 会用父 `PermissionGate` 检查角色 Skill 列表。`SubagentRunner` 随后构造只包含获准 Skill ID 的目录、工具和子策略，并通过原有 `AgentRuntime.run()` 运行；它没有第二套 agent loop。结果通过原持久委派边界返回父侧。

在子任务 scope 内，`ScopeGate` 还对解析后的真实路径拒绝 `config_local.py`、工作区 session 敏感目录、当前持久化会话目录以及指向这些位置的符号链接。角色工具集合不能放宽这条检查。

## 本版特性、下一课与代码索引

本版新增了四种内置角色、本地自定义角色、角色工具/权限/模型绑定、父侧 Skill 预授权，以及可写入 State、schema 3 结果和 Trace 的可选角色身份摘要。未传角色的调用保持同步通用子代理行为。

阶段十三后续版本计划讨论当前进程内的后台子代理和子会话续接；在它们完成前，本文所有角色仍然同步返回，也没有 follow-up 工具。

核心实现索引：[角色 Catalog](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/agent_profiles.py)、[委派运行时](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/delegation.py)、[受限 Skill Catalog](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/skills.py)、[父子工具视图](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/tools/base.py)、[持久化结果校验](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.47/src/mini_agent/session.py)。
