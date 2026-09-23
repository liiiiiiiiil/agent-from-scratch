# 第 46 课：受限 HTTP MCP、Resource 与 Prompt

上一课：[本地 Skills](45-local-skills.md) · [教程总览](README.md) · 下一课：阶段十二收口

代码快照：`v0.46` · 相邻差异：`v0.45..v0.46`

本课命令使用 Bash/zsh。下面的代码链接和示例都对应 `v0.46`。

## 本课目标

MCP Server 除了提供模型可以请求的工具，还可以提供资料和提示模板。控制权因此有三种不同情况：模型请求 Tool；用户在 CLI 里选 Resource；用户查看并确认 Prompt。本课会说明这三种入口各自怎样工作，以及为什么 Resource 和 Prompt 不直接交给模型自行调用。

本版还让 MCP Client 可以通过 HTTP 连接 Server。HTTP 是通过网络发送请求和接收回复的方式；本课只支持每次请求返回一条 JSON 消息的受限形式。读完后，你应能判断什么时候内容会进入后续对话、什么时候需要用户确认，以及这种 HTTP 支持没有覆盖什么。

## 前置条件

只需要基础 Python、终端和 Git。建议先读第 44、45 课，了解 Agent 的 Tool 授权、本地 Skill 与 Context。Context 是每轮发给模型的背景和对话；CLI 是用户和 Agent 交互的命令行界面。

查看相邻版本的变化并切换到本课代码：

```bash
git checkout v0.45
git diff --stat v0.45..v0.46
git checkout v0.46
```

阅读完毕后，用 `git checkout -` 返回切换前所在的分支。

## 新增与改动文件

本版有两类改动：增加一种 HTTP 传输方式；增加两个由应用和用户控制的内容入口。

| 文件 | 负责什么 |
|---|---|
| [mcp/http.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.46/src/mini_agent/mcp/http.py) | 用标准库 `http.client` 发送和接收有界 JSON 请求。 |
| [mcp/client.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.46/src/mini_agent/mcp/client.py) | 管理请求配对，并读取、冻结 Tools、Resources 和 Prompts 目录。 |
| [mcp/adapter.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.46/src/mini_agent/mcp/adapter.py) | 管理连接；只把 Tools 接入 Agent 的模型工具目录。 |
| [config.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.46/src/mini_agent/config.py) | 检查 stdio/HTTP 配置、HTTPS 默认值和回环 HTTP 开关。 |
| [permission.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.46/src/mini_agent/permission.py) | 询问 Resource 读取与 Prompt 获取的权限。 |
| [__main__.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.46/src/mini_agent/__main__.py) | 提供四个 CLI 命令、资料加入对话和 Prompt 预览确认。 |
| [context.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.46/src/mini_agent/context.py) | 让 Resource 正文参与对话长度控制，并排除自动记忆检索。 |

## 版本变更定位

上一版已有 stdio MCP Tool 路径。本版在同一个 Client 上增加 HTTP，同时提供两条不经过模型 Tool 目录的路径：

```text
[旧] v0.45 已有：
[旧] stdio Server → [旧] MCP Client → [旧] Adapter → [旧] Tool Registry
                                                      → [旧] AgentRuntime → [旧] ToolExecutor
```

```text
[~] v0.46 修改并扩展：
[~] stdio 或 HTTP Server → [~] MCP Client（目录读完后冻结）
                               ├─ [旧] Tools → [C] Adapter → [旧] AgentRuntime → [旧] ToolExecutor
                               ├─ [+] Resources → [C] 用户 CLI 选择 → 后续对话的资料
                               │                    └─ [B] 只接受目录内的有界文本
                               └─ [+] Prompts → [C] 用户预览确认 → 一次新的用户输入
                                                    └─ [B] 取消时不进入对话
```

图例：`[旧]` v0.45 已有；`[+]` v0.46 新增；`[~]` v0.46 修改；`[C]` 主要消费者；`[B]` 本版边界。Tool、Resource 和 Prompt 的差别在于“谁决定使用它”。Tool 是模型提出的操作请求，仍经过 Agent 的权限检查；Resource 由用户在 CLI 选择，读入后作为不可信资料参与后续对话；Prompt 也由用户选择，完整预览并确认后才作为新的用户输入运行。

## 关键流程

### 1. HTTP 只更换连接方式，不改变 MCP 会话顺序

stdio Client 和 HTTP Client 都遵循同一顺序：初始化、通知初始化完成、读取能力目录、再按需要发出请求。HTTP 使用 Python 标准库的 `http.client` 发送 POST，并明确要求不压缩响应（`Accept-Encoding: identity`）；初始化后 Server 可能分配一个 session ID（会话标记，用来识别同一条连接），Client 只在当前内存连接中携带它。

本版只接收逐请求的 `application/json` 回复。即使请求头声明可以接受其他传输形式，Server 返回 SSE、重定向、需要 OAuth，或请求发出后断开时，Client 都会失败关闭，不会改用另一种方式或重试可能已执行的调用。默认使用 HTTPS；只有显式允许时，回环地址才可用明文 HTTP。

### 2. Resource 是用户加入对话的资料

主 CLI 用以下命令列出和读取 Resource：

```text
/mcp-resources <alias>
/mcp-resource <alias> <uri>
```

读取前，Resource 必须已经出现在完整读取并冻结的目录中。列出 Resource 和 Prompt 默认允许；读取 Resource、获取 Prompt 默认询问；`always` 只记住对应的精确 `alias:uri` 或 `alias:name`。正文必须是有界 UTF-8 文本，并且返回 URI 必须与请求相同。图片、音频、二进制或超限内容都会被拒绝。

读取后，正文会成为当前对话中的普通用户侧资料消息。代码用 `name` 标出来源，避免把它误当作用户亲自输入的普通文本：

```python
context.history.append({"role": "user", "name": "mcp_resource", "content": content})
```

它不会立刻触发一次模型请求；你下一次提交任务时，模型才会在 Context 预算允许范围内看到它。正文不会复制进结构化 State 或 Trace 摘要，也不会作为验证证据；来源等摘要仍可被记录。

Resource 正文属于普通对话历史，启用 `/save` 后可能随会话一起保存。State 和 Trace 只保留来源摘要。

### 3. Prompt 是用户审阅后提交的一次输入

CLI 提供两个 Prompt 命令：

```text
/mcp-prompts <alias>
/mcp-prompt <alias> <name> <JSON参数对象>
```

获取前必须先列出并冻结 Prompt 目录。程序检查名称和字符串参数后，请求 Server 返回模板消息；返回内容只能是有界文本，角色标签只能是原始 `user` 或 `assistant`。`assistant` 在这里表示“模板原先标注的角色”，不表示 Agent 已经说过这段话。

程序会完整预览模板。取消时，内容不进入对话，也不请求模型；确认后，整个预览作为一次 `role=user` 输入交给现有任务流程。这样保留了模板结构，也避免 Server 把文字伪装成真正的助手历史。确认后的输入属于普通任务历史，启用 `/save` 后可能随会话保存。

一次获取的顺序可以概括为：

```text
/mcp-prompt → 检查已冻结目录 → 检查权限 → 获取并验证文本
            → 完整预览 → 用户取消（结束）
                       → 用户确认 → 作为用户输入运行 Agent
```

## 实现拆解

HTTP 响应、目录、单条文本和累计文本都有大小上限。Tools、Resources、Prompts 都要读取完所有分页才会冻结；后续 Resource URI 和 Prompt 名称只能从各自的冻结目录选择。运行中不会因 Server 通知而热刷新。

只有 Server 在初始化时声明了 `tools` 能力，它的工具才可能进入 Agent 工具目录。Resource 和 Prompt 专用的 Server 可以由连接管理器连接，但它们不会因此获得模型可调用的 Tool。MCP 连接在当前父 Runtime 中创建，任务切换、恢复和退出时有界关闭。

Resource 会参与普通对话的 Context 长度裁剪，但不会扩展自动 Memory 检索的查询。Prompt 只有在用户确认后才进入任务 history。两者都不进入 Subagent，也不生成工具调用记录或验证证据。

## 为什么这样设计

Resource 与 Prompt 由应用侧选择，可以让用户决定何时把外部内容带入任务。代价是用户要先列出目录并选择条目，模型不能自行调用它们。把 Prompt 的原始角色保留为预览标签而非真实对话角色，可以防止外部 Server 伪造历史。

HTTP 只实现逐请求 JSON，代码更容易有界地检查状态、内容类型和回复大小；它因此不兼容要求 SSE 的 Server。本版也不实现 OAuth、重定向、资源模板、订阅、Prompt completion、二进制内容或 Server 主动请求。

## 运行与观察

如果已在本机配置一个 alias 为 `demo` 的 MCP Server，并且目录里有 Resource 和 Prompt，可以先查看可选项：

```text
/mcp-resources demo
/mcp-prompts demo
```

再把示例 URI 和名称替换成列表中实际出现的值：

```text
/mcp-resource demo <列表中的 URI>
/mcp-prompt demo <列表中的名称> {"topic":"example"}
```

读取 Resource 时，应看到权限询问；获准后它进入对话，但当前时刻不会立刻请求模型。获取 Prompt 时，应先看到完整预览；取消后对话不变，确认后才会启动一次任务。后一种操作需要本机已配置模型。

## 设计边界

- HTTP 默认要求 HTTPS；明文 HTTP 仅可对显式允许的回环地址使用。URL、认证 Header 和 session ID 不进入 State、Trace 或 session。
- 只接受 JSON 回复，不接受 SSE、重定向或 OAuth；请求超时和断开后不自动重试。
- Resource 和 Prompt 必须来自已冻结目录，只支持有界文本，不支持模板目录、二进制内容或热刷新。
- 外部正文按不可信资料处理，不会覆盖项目指令、授权、计划或验证规则，也不会传给 Subagent。

## 本版特性、下一课与代码索引

v0.46 为 MCP 增加了受限 HTTP Client，并让用户能从 CLI 选择文本 Resource、预览并确认 Prompt。三种能力各自有控制入口：Tool 由模型提出、Resource 由用户读取、Prompt 由用户审阅后提交。

固定代码索引：[client.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.46/src/mini_agent/mcp/client.py)、[http.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.46/src/mini_agent/mcp/http.py)、[adapter.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.46/src/mini_agent/mcp/adapter.py)、[CLI](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.46/src/mini_agent/__main__.py)、[context.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.46/src/mini_agent/context.py)。
