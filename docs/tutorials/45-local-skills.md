# 第 45 课：给 Agent 一份本地 Skill

上一课：[让 Agent 调用 MCP Tool](44-mcp-tools-runtime.md) · [教程总览](README.md) · 下一课：[MCP HTTP、Resource 与 Prompt](46-mcp-http-resources-prompts.md)

代码快照：`v0.45` · 相邻差异：`v0.44..v0.45`

本课命令使用 Bash/zsh。下面的代码链接和示例都对应 `v0.45`。

## 本课目标

有时我们希望 Agent 遵循一套做事步骤，例如“先读文件，再修改，最后验证”。把步骤写进可执行脚本会让脚本越过 Agent 的工具授权；每次都把所有说明塞进模型提示，也会让无关内容占用上下文。

本课用 Skill 解决这个问题。Skill 是一份本地工作说明，不是可执行代码，也不会增加新的操作权限。Agent 先看到它的名称和简短介绍；需要正文时，再通过 `skill(name)` 请求读取，并经过权限检查。读完后，你应能说明 Skill 怎样进入模型的工作上下文，以及为什么它不能替 Agent 授权运行命令。

## 前置条件

只需要基础 Python、终端和 Git。建议先读第 44 课，了解 Agent 怎样接收模型的工具请求并检查授权。

这里会用到两个概念：Context 是 Agent 为当前一次模型请求准备的背景信息和对话内容；Context 有长度上限，超出时会裁剪。PermissionGate 是执行工具前的授权检查；Skill 默认也要经过这项检查，不能因为它只是文字就绕过去。

查看相邻版本变化并切换到本课代码：

```bash
git checkout v0.44
git diff --stat v0.44..v0.45
git checkout v0.45
```

阅读完毕后，用 `git checkout -` 返回切换前所在的分支。

## 新增与改动文件

Skill 的流程分为发现、展示说明和按需读取正文：

| 文件 | 负责什么 |
|---|---|
| [skills.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.45/src/mini_agent/skills.py) | 从两个固定本地目录发现 Skill，并安全读取文件。 |
| [tools/skill.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.45/src/mini_agent/tools/skill.py) | 注册 `skill(name)` 读取入口。 |
| [context.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.45/src/mini_agent/context.py) | 在模型请求中加入符合当前权限规则的 Skill 简介。 |
| [permission.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.45/src/mini_agent/permission.py) | 对每个 Skill 的正文读取单独询问授权。 |
| [tools/base.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.45/src/mini_agent/tools/base.py) | 把正文交给模型，同时限制 State 和 Trace 中记录的摘要。 |

## 版本变更定位

v0.44 已有的 Agent 循环会把可用 Tool 告诉模型，并在模型请求后执行工具。本版保留这条执行路径，再增加一份供 Context 展示的 Skill 简介，以及一个按需读取正文的工具。

```text
[旧] v0.44 已有：
[旧] Registry 中的 Tool → [旧] Context 准备模型请求 → [旧] AgentRuntime
                                                  → [旧] ToolExecutor → [旧] role=tool 结果

[+] v0.45 新增：
[+] skills/<name>/SKILL.md
  → [+] SkillCatalog 发现并检查文件
  → [~] Context 加入 Skill 简介（只放名称、来源、说明）
  → [C] 模型请求 skill(name)
  → [C] PermissionGate 授权后读取正文
  → [旧] ToolExecutor 把正文作为 role=tool 资料交给模型
       ├─ [B] 拒绝授权或文件变化：不提供正文
       └─ [B] 正文不授权后续工具，也不进入 Subagent
```

图例：`[旧]` v0.44 已有；`[+]` v0.45 新增；`[~]` v0.45 修改；`[C]` 主要消费者；`[B]` 本版边界。`SkillCatalog` 可以理解为当前 Runtime 的 Skill 名录。它启动时读取固定目录并冻结发现结果；正文不会因为“被发现”就自动读入模型请求。

## 关键流程

### 1. 发现时只读取简介

Skill 放在工作区根目录的 `skills/<名称>/SKILL.md`，或用户目录下的 `~/.mini_agent/skills/<名称>/SKILL.md`。文件开头需要有如下格式的简单说明区，称为 frontmatter：

```markdown
---
name: verify-change
description: 先阅读，再修改，最后验证
---
# 正文从这里开始
1. 阅读相关文件。
2. 完成修改。
3. 说明如何独立验证。
```

`name` 必须和文件夹名一致。v0.45 只接受 `name`、`description` 两个单行字段，不解析完整 YAML，也不会执行正文里的代码。

Agent 准备模型请求时，Context 会把允许展示的 Skill 名称、来源（项目级或用户级）和简介放进普通资料消息。这样模型能判断某个 Skill 可能有用，但不必每轮都接收所有正文。被权限规则禁止的 Skill 不会出现在简介中。

### 2. 读取正文时再询问

如果模型决定使用 `verify-change`，它会提交类似这样的工具请求：

```json
{"name":"skill","arguments":{"name":"verify-change"}}
```

PermissionGate 默认询问是否允许读取这个 Skill。选择 `always` 也只记住这个 Skill 的确切名称，不会替其他 Skill 授权。通过检查后，程序会重新安全打开对应的 `SKILL.md`，确认文件仍是发现时看到的那个文件，再把正文作为普通 `role=tool` 结果交给模型。

模型能看到正文，是因为它需要根据说明安排工作。Agent 的结构化任务记录和执行事件只保存 Skill 名称、来源和长度等摘要，不把整段正文复制进去。若启用了 `/save`（保存当前会话的命令），正文作为普通工具历史的一部分仍可能随会话保存。

### 3. 说明步骤不等于批准步骤

Skill 可以建议“完成修改后运行检查”，但这句话不等于运行命令的授权。真正的命令仍是一个独立工具请求，要经过自己的参数、计划和权限检查。Skill 也不能直接修改文件、替用户推进计划、产生验证证据，或让子代理继承它的内容。

## 实现拆解

项目级目录优先于用户级目录。同名项目 Skill 有效时会覆盖用户级版本；如果项目级同名项格式无效，程序会阻止回退到用户级的同名项，避免读者以为正在用项目里的说明，实际却读到了另一份。

目录、名称和说明都有格式与大小上限。发现时保存目录和文件身份；读取时会拒绝符号链接、文件替换、编码错误和大小变化。Skill 只在创建 Runtime 时发现，因此编辑文件后需开始新任务才能获得新的目录。

Subagent（父 Agent 派出的只读调查实例）没有 Skill 工具、目录提示或正文。父 Agent 的工具、权限和对话也不会传给它。

## 为什么这样设计

Skill 与 Tool 分开，能把“建议怎么做”和“允许做什么”保留为两件事。把正文放在受保护的 system prompt 中会模糊信任边界；把说明写成自动执行脚本，则可能越过现有工具授权。按需加载也减少了模型每轮都要处理的背景内容。

固定目录、受限格式和 Runtime 启动时冻结目录，牺牲了热更新与复杂的 Skill 格式，换来稳定、容易检查的读取行为。本版不扫描附属文件，不执行脚本，不支持远程 Skill 或完整 YAML。

## 运行与观察

本课版本仓库自带示例 `skills/verify-change/SKILL.md`。先看文件内容：

```bash
sed -n '1,80p' skills/verify-change/SKILL.md
```

如果已经配置好本地模型，可以在父 CLI 中明确要求 Agent 读取 `verify-change`。预期现象是：Agent 先请求读取该 Skill，终端出现 `skill` 权限提示；授权后，模型才收到正文。随后若 Agent 请求 `run_shell` 或写文件，仍会出现对应工具自己的授权流程。前后两次授权正说明 Skill 提供的是工作说明，不是操作许可。

## 设计边界

- 只扫描工作区和用户目录下指定的 `skills` 直属目录，不递归搜索，也不读取 Skill 的附属文件。
- Skill 简介和正文都是不可信资料：模型可以把它们当作参考信息，但它们不能覆盖用户要求、项目指令、计划或权限规则。
- Skill 正文进入普通工具历史；开启 `/save` 后它可能保存在会话中。
- Skill 只提供给父 Agent，不成为验证证据，也不传给 Subagent。

## 本版特性、下一课与代码索引

v0.45 加入本地 Skill 的发现、简介展示和经授权后的安全读取。下一课会区分 MCP 的三种用途：模型可以请求的 Tool、由应用选择的 Resource，以及由用户预览后提交的 Prompt。

固定代码索引：[skills.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.45/src/mini_agent/skills.py)、[tools/skill.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.45/src/mini_agent/tools/skill.py)、[context.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.45/src/mini_agent/context.py)、[permission.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.45/src/mini_agent/permission.py)。
