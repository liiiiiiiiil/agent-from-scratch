# 第 46 课：受限 HTTP MCP、文本 Resource 与 Prompt

上一课：[本地 Skills 发现与按需加载](45-local-skills.md) · [教程总览](README.md) · 下一课：阶段十二收口

代码快照：`v0.46` · 相邻差异：`v0.45..v0.46`

本课示例命令使用 Bash/zsh。`v0.46` tag 由维护者在交付后固定；阅读者切换前请确认本地已有该 tag。

## 本课目标

上一课的父 Agent 已经能够连接显式启用的本地 stdio MCP Server，也能按权限读取本地 Skill。它仍有两个实际限制：远程 MCP 只能停留在计划里，MCP Resource 和 Prompt 也还没有用户可以选择的入口。

本课把这三件事放进一个明确的边界中：HTTP 只发送和接收 JSON，Resource 是应用选择的资料，Prompt 是用户选择的模板。三者都复用既有的配置、连接生命周期、权限和 Context 规则；Resource 与 Prompt 不会变成模型可以自行调用的 Tool。

## 前置条件

需要 Python 3.10+、基础 Python、Bash/zsh 和 Git 知识。建议先阅读第 45 课，因为本课沿用它的父 Runtime、Context、PermissionGate、session 和 Subagent 隔离边界。

查看相邻版本时，可以执行：

~~~bash
git checkout v0.45
git diff --stat v0.45..v0.46
git checkout v0.46
~~~

第一条命令切到没有本版能力的基线，第二条命令显示本课改动范围，最后一条命令切到本课源码。阅读结束后回到原来的分支：

~~~bash
git checkout -
~~~

## 新增与改动文件

本版的关键插入点是 MCP Client 和父 CLI。Client 负责把不同传输方式收敛成同一个 JSON-RPC 会话；连接管理器只把有 Tool 能力的目录交给 Tool Registry；CLI 单独使用 Resource 和 Prompt 能力。

| 文件 | 作用 |
|---|---|
| [mcp/http.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.46/src/mini_agent/mcp/http.py) | 用 `http.client` 发送逐条 JSON 请求，检查状态、`application/json`、会话 Header、响应大小和有界关闭。 |
| [mcp/client.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.46/src/mini_agent/mcp/client.py) | 复用握手和请求配对，冻结分页的 Tools、Resources、Prompts 目录，并校验文本内容、身份和参数。 |
| [mcp/adapter.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.46/src/mini_agent/mcp/adapter.py) | 管理 stdio/HTTP Client；Resource/Prompt 专用 Server 可以连接，但不会注册模型 Tool。 |
| [config.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.46/src/mini_agent/config.py) | 验证 transport、HTTPS 默认、回环 HTTP 开关、URL 和本地 Header。 |
| [permission.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.46/src/mini_agent/permission.py) | 增加应用侧 `mcp_resource` 与 `mcp_prompt` 权限，按 `alias:uri` 或 `alias:name` 精确记住授权。 |
| [__main__.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.46/src/mini_agent/__main__.py) | 实现四个主 CLI 命令、Resource history 消息、Prompt 完整预览和用户确认。 |
| [context.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.46/src/mini_agent/context.py) | 让 Resource 正文参与普通 Context 预算，同时排除它对自动 Memory 查询的干扰。 |
| [test_mcp_v046.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.46/tests/test_mcp_v046.py) | 用离线 HTTP fixture 验证会话 Header、分页、内容拒绝、错误 ID 和无重试。 |

本课完成后可以用下面的命令检查当前工作树改了哪些位置：

~~~bash
git diff --stat v0.45..HEAD
~~~

## 版本变更定位

上一版的主要路径只有本地 stdio MCP Tool：

~~~text
[旧] MCP_SERVERS
  → [旧] StdioTransport
  → [旧] McpClient.initialize / tools/list
  → [旧] MCP Tool Adapter
  → [旧] Tool Registry → ToolExecutor → AgentRuntime
~~~

本版在传输层加入 HTTP，在同一个 Client 中加入两个应用侧目录，并把它们从模型 Tool 路径旁路出去：

~~~text
[~] MCP_SERVERS(transport=stdio|http)
  → [~] McpClient：固定握手、请求配对、目录冻结
      ├─ [+] HttpTransport：POST JSON、session header、只收 JSON
      ├─ [C] Tool Adapter → Tool Registry → ToolExecutor → AgentRuntime
      └─ [+] resources/list/read → [C] 主 CLI → 普通不可信 history
          └─ [B] 非文本、URI 不一致、超限：拒绝
      └─ [+] prompts/list/get → [C] 主 CLI → 预览 → 用户确认 → run_task
          └─ [B] 非法角色、非文本、取消：不进 history，不请求 LLM
~~~

图例：[旧] v0.45 已有，[+] v0.46 新增，[~] v0.46 修改，[C] 主要消费者，[B] 本版边界。HTTP 会话、真实 URL、认证 Header 和 session ID 只留在当前 Runtime；它们不进入 State、Trace 或 session。

## 上一版的问题

MCP Tool 适合模型主动执行的动作。Resource 和 Prompt 的控制权不同：Resource 需要应用先选资料，Prompt 需要用户先看模板。把它们注册成模型 Tool 会让模型绕过用户选择，也会把服务端返回的 `assistant` 文本误解成真正的助手消息。

远程传输还有一个教学取舍。完整 Streamable HTTP 需要处理 SSE 等能力，本项目本版只实现逐请求 JSON 响应。因此一个返回 `text/event-stream`、发生重定向、需要 OAuth 或在请求发出后断开的 Server，都会得到明确失败，不会被自动转换或重试。

## 核心概念

### 1. Transport 变化不改变 Tool 边界

`McpClient` 仍然先发送 `initialize`，再发送 `notifications/initialized`，然后冻结目录。stdio 用逐行 JSON，HTTP 用 `http.client` 的 POST；上层看到的仍是同一个请求配对接口。HTTP 初始化后在内存中携带 `MCP-Session-Id`，后续请求增加 `MCP-Protocol-Version` 和会话 Header。

HTTP 请求按规范声明 `application/json, text/event-stream`，但本课只接受 `application/json` 响应。通知必须返回无正文的 202；普通请求必须返回带 JSON 正文的 200。代码不会跟随 3xx，也不会把 SSE 当作 JSON 读取。工具调用已经发出后，超时或断开只产生失败事实，不能自动再次发送。

### 2. 完整分页后才允许读取

Tools、Resources 和 Prompts 都可能分页。Client 会一直读取 `nextCursor` 到终点，限制页数、项目数、目录大小并拒绝重复游标；读取 Resource 或获取 Prompt 前，目标必须来自已经冻结的目录。

Resource 目录项只保存有界元数据。`resources/read` 返回的每个内容必须带与请求相同的 URI，并且只能包含 UTF-8 文本；blob、图片、音频、嵌入资源和超限正文都会被拒绝。

Prompt 目录冻结名称和参数定义。`prompts/get` 的参数必须是定义中的字符串，必需参数不能缺失；返回消息只能是 `user` 或 `assistant` 角色的文本。这里的角色是引用资料的原始标签，不是当前会话的消息角色。

### 3. CLI 保留 Resource 和 Prompt 的控制权

主 CLI 提供四个入口：

~~~text
/mcp-resources <alias>
/mcp-resource <alias> <uri>
/mcp-prompts <alias>
/mcp-prompt <alias> <name> <JSON参数对象>
~~~

列表默认允许，读取和获取默认询问。`always` 只批准一个精确的 `alias:uri` 或 `alias:name`。Resource 读取成功后加入一条带 `name=mcp_resource` 的 `role=user` history 消息，消息正文包含来源和低信任提示；它不会触发当前轮 LLM 请求。Context 每次准备请求时会按普通历史预算裁剪它，并且自动 Memory 查询会跳过这条消息。

Prompt 获取成功后先完整预览，使用 `original_role=user` 或 `original_role=assistant` 标签呈现。用户取消时既不改 history，也不请求 LLM；用户确认后，整个预览作为一次 `role=user` 输入交给现有 `run_task`。即使预览里有 `original_role=assistant`，也不会在 history 中写入 `role=assistant`。

## 关键流程

下面的流程显示一次 Prompt 选择如何收口。箭头表示真实的控制顺序：

~~~text
用户输入 /mcp-prompt
  → 解析 JSON 参数
  → resources/prompts 目录已冻结？否 → 先完成 prompts/list
  → alias:name 命中？否 → 结束，不发 prompts/get
  → PermissionGate( mcp_prompt, alias:name )
  → prompts/get
  → 校验文本、角色、大小
  → 完整预览
      ├─ 取消 → history 不变，不请求 LLM
      └─ 确认 → 作为 role=user 输入 → run_task → AgentRuntime
~~~

HTTP 失败会在 Client 这一层结束连接；Tool 调用仍由已有 ToolExecutor 负责 handler 前准入和 schema 3 边界。Resource 和 Prompt 不走 ToolExecutor，因此不会创建工具 attempt、verification evidence 或 Subagent 能力。

## 实现拆解

### 配置和会话

旧配置省略 `transport` 时仍使用 stdio。HTTP 配置只允许 `url`、`headers` 和 `allow_loopback_http` 等 HTTP 字段；真实 URL 与认证值来自本地 `config_local.py`。`McpConnectionManager` 在每个父 Runtime 中拥有连接，`/new`、`/reset`、恢复和退出仍通过现有生命周期关闭它们。

### 内容校验和预算

Client 负责协议级身份和类型校验，CLI 负责展示和当前任务 history。两层都使用有界字符串；HTTP 响应、目录、单条文本和累计文本都有上限。Resource 正文属于普通历史，所以会服从 Context 裁剪；受保护 system 指令、State、Trace 摘要和 verification evidence 不会被外部正文替换。

### 子代理和持久化

父 Runtime 的 registry 仍只向 Subagent 提供固定的 `calculate`、`read_file`、`list_dir` 和 `grep`。连接、Resource、Prompt 和本地 Skills 都不进入子代理。开启 `/save` 后，Resource history 可能随普通 history 保存；Prompt 只有在确认并通过 `run_task` 后才会进入 history，取消不会写入 session。

## 为什么这样设计

把 HTTP 放进独立 Transport 可以复用固定 MCP 生命周期和工具适配，同时让 stdio 的进程清理规则不污染 HTTP。JSON-only 子集容易观察、容易离线测试，也明确承认它不兼容需要 SSE 的完整 Streamable HTTP Server。

把 Resource 和 Prompt 放在父 CLI 是为了让应用和用户决定何时引入外部内容。代价是模型不能自主选择这些能力，用户也需要先运行列表命令。这个版本刻意不实现资源模板、订阅、Prompt completion、OAuth、SSE、服务端主动请求和二进制内容。

保留原始角色标签而不写入真正的 `role=assistant`，可以让用户看到模板的结构，同时防止外部 Server 伪造会话历史。Resource 使用普通 history 让它能参与后续任务，但低信任标记、Context 预算和 Memory 查询排除共同限制了它对运行时决策的影响。

## 设计边界

- HTTP 默认只接受 HTTPS；回环 HTTP 需要显式开关。URL、认证 Header 和 HTTP session ID 不显示给用户，也不持久化。
- Server 能力必须在 `initialize` 中声明；只有有 `tools` 能力的 Server 才会生成模型 Tool，Resource/Prompt 专用 Server 仍可由连接管理器服务 CLI。
- 目录必须完整分页并冻结；运行期间不刷新，也不接受资源模板。URI 和 Prompt 名称必须来自冻结目录。
- 外部文本是不可信资料。它不能覆盖 system/project instructions、PermissionGate、Plan 或 verification，也不会进入 Subagent。
- 本课使用的 HTTP fixture 监听本机回环地址，生产环境配置应使用 HTTPS；教程不依赖公网 Server。

## 本版特性、下一课与代码索引

本课完成后，父 Runtime 可以连接配置中的 stdio 或 JSON-only HTTP MCP Server；主 CLI 可以列出和读取有界文本 Resource，也可以预览并确认文本 Prompt。完整代码索引见本课“新增与改动文件”表，重点入口是 [McpClient](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.46/src/mini_agent/mcp/client.py)、[HttpTransport](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.46/src/mini_agent/mcp/http.py)、[CLI](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.46/src/mini_agent/__main__.py) 和 [HTTP fixture](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.46/tests/fixtures/mcp_http_server.py)。

阶段十二到这里收口：MCP Tool、Resource、Prompt 和本地 Skill 都有各自的控制入口，但共享配置冻结、权限、Context、不可信资料和子代理隔离边界。Git tag 只由维护者手动创建；本课不执行 tag 操作。
