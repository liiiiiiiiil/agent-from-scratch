# 阶段十二：MCP 与 Skills 实施计划

> 状态：`v0.45` 已实现；`v0.46` 仍在规划中
> 建议版本范围：`v0.43`–`v0.46`
> 能力前置：统一 Tool / PermissionGate / AgentRuntime、阶段九的持久工具边界与崩溃恢复、阶段十的父子能力隔离、阶段十一的 Context 与不可信资料边界
> 关联计划：`reliable-execution-plan.md`、`session-persistence-resume-plan.md`、`subagent-delegation-plan.md`、`memory-retrieval-references-plan.md`

本计划参考 [MCP `2025-11-25` 生命周期](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)、[Tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)、[Resources](https://modelcontextprotocol.io/specification/2025-11-25/server/resources)、[Prompts](https://modelcontextprotocol.io/specification/2025-11-25/server/prompts) 与 [Streamable HTTP](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)，以及 [OpenCode MCP](https://opencode.ai/v2/docs/mcp-servers) 和 [OpenCode Skills](https://opencode.ai/v2/docs/skills) 的按需能力组织方式。它是有意裁剪的教学实现，不声称兼容所有 MCP Server 或复刻 OpenCode。

## 1. 目标与定位

阶段十一后，`mini_agent` 已能按需读取工作区 Memory 和具名本地 References，但外部标准能力仍不能通过统一 Tool 边界接入；重复的多步工作流也只能依赖普通项目指令或用户临时提示。

阶段十二要回答的问题是：**怎样接入标准外部能力、按需加载可复用工作流，同时不绕过已有的权限、执行、恢复和 Context 边界？**

一句话目标：**让父 Agent 通过 MCP 接入受控外部能力，通过 Skills 按需加载本地工作流说明，并继续复用已有 Tool、PermissionGate、Context 和 AgentRuntime。**

两条主线必须分开：

```text
MCP Server → MCP Client / Adapter → Tool Registry → ToolExecutor / PermissionGate → AgentRuntime

Skill 目录 → name + description 发现提示 → skill(name) → SKILL.md 正文
                                                    ↓
                                         普通 tool history / Context
                                                    ↓
                                         现有 Tools / MCP Tools 执行
```

**MCP 给 Agent 新能力；Skill 教 Agent 怎样组合已获准的能力。** Skill 正文不是新的执行权限，也不升级为 system instruction。

四个版本分别讲一个问题：`v0.43` 认识 MCP 协议和 stdio 生命周期；`v0.44` 已把外部 Tool 安全接入父 Runtime；`v0.45` 已实现 Skill 与 Tool 的区别及按需加载；`v0.46` 计划在严格限定范围内补远程传输、Resource 和 Prompt。

## 2. 范围与非目标

### 2.1 本阶段范围

`v0.43` 只包含独立的 stdio Client 和演示 CLI；`v0.44` 在此基础上把显式启用的
本地 MCP Tool 接入父 Runtime；`v0.45` 增加本地 Skills 的固定目录发现和按需加载。远程
HTTP、Resources 和 Prompts 仍属于后续版本，不由当前代码宣称支持。

- 固定 MCP `2025-11-25` 握手式协议，先实现本地 stdio Server 的 `initialize → notifications/initialized → tools/list → tools/call`，拒绝不受支持的协商版本。
- 仅由本地配置启用 MCP Server；父 Runtime 创建时冻结工具目录，外部 Tool 通过既有 Registry、Executor、PermissionGate、Plan gate、Failure、Trace 和持久工具边界执行。
- 外部 MCP Tool 默认 `effect_class="possible"`；只有本地配置精确指认的工具可标为 `none`。服务端名称、描述、schema、annotations 和结果不决定安全等级。
- 从项目级和全局本地目录发现 `SKILL.md`；向父模型有界展示 `name + description`，通过普通只读 `skill(name)` Tool 按需加载正文。
- 为 Skill 加载定义确定性的同名优先级、大小上限、路径边界和权限规则；正文只作为不可信工作流材料进入普通 tool history，并由现有 Context 裁剪。
- `v0.46` 增加仅适用于返回 JSON 响应的 Streamable HTTP Server 的教学子集，以及有界 UTF-8 文本 Resource 和文本 Prompt 的显式应用侧/用户侧入口。

### 2.2 本阶段不做

- 不开发 MCP Server 框架、Marketplace、插件系统、OAuth 管理、Skill 商店、在线 Skill 目录、自动生成 Skill 或 embedding 检索。
- 不实现 `2026-07-28` modern lifecycle、自动协议探测或多版本 fallback；不实现服务端发起的 sampling、elicitation、roots、tasks 等交互。
- 不实现 MCP 目录热刷新、订阅、变化通知处理、resource templates、补全、二进制/图片/音频内容，或 Streamable HTTP 的 SSE 响应、GET 监听、断线恢复与重投递。
- 不把 MCP Resource / Prompt 伪装成可由模型自主调用的普通 Tool；不把 MCP server instructions 自动注入受保护 system prompt。
- 不让 Skill 自动执行脚本、隐式授予工具权限或直接推进 Plan / verification；不默认向 Subagent 开放 MCP 或 Skills。
- 不引入第三方核心依赖。MCP stdio、JSON-RPC、HTTP 客户端、frontmatter 的受限解析和生命周期管理均使用标准库；LLM 传输继续遵守现有 `http.client` 与 `Accept-Encoding: identity` 约束。

## 3. 先冻结的架构决策

### D1：协议版本固定，完整实现已选择的经典握手

客户端只请求 `2025-11-25`。`initialize` 响应必须有可接受的版本、server 信息和能力声明；版本不符、协议响应不合法或缺少所需能力时明确失败，不隐式回退。收到成功响应后必须发送 `notifications/initialized`，然后才能列工具或发调用。`tools/list` 的分页必须读取到终点，且受页数、工具数和响应字节上限约束；不得把第一页误当完整目录。

`v0.43` 可用独立 client 与测试用本地 Server 演示 JSON-RPC 请求 ID、响应配对、通知、错误与关闭，不把尚未受权限管理的 MCP Tool 暴露给模型。stdio 的 stdout 只承载协议消息；stderr 有界排空并与协议输出隔离。无响应、超大行/消息、坏 JSON、错误 ID、提前退出和超时均产生明确失败，不无限等待。

### D2：Tool Adapter 只连接既有执行链，不复制 Agent Loop

`v0.44` 将已发现的 MCP Tool 注册为普通 `Tool`，并由现有 `AgentRuntime.run()` 触发。建议外显名为 `mcp_<server>_<tool>`；名称规范化、长度限制及碰撞检查在注册前完成，任何两个外部工具或外部/内置工具规范化后同名都拒绝，绝不静默覆盖。内部保留精确的原始 server/tool 名，用于真正的 `tools/call`，不把外显名反向猜回原始名。

MCP `inputSchema` 是外部 JSON Schema，而当前 `validate_arguments()` 只支持一个子集。Adapter 必须显式列出支持的字段与关键字；不能正确校验的 schema 或参数在注册/准入阶段拒绝，不能悄悄丢弃约束后直传远端。转换结果须保留有界文本与可解释的来源；不支持的内容类型明确标记或拒绝，不把二进制当文本。JSON-RPC error、`tools/call` 的 `isError=true`、断连、超时和协议错误都应转成可区分的 `ExecutionResult` 失败事实，且每个模型 call 仍收到对应 `role=tool` 结果。

### D3：外部 Tool 默认可能有副作用，本地配置才可降级

所有 MCP Tool 默认 `effect_class="possible"`，默认权限为 `ask`。服务端 `readOnlyHint` 等 annotations、名称、描述或 `outputSchema` 只是外部元数据，不能改变本地副作用等级或授权；MCP 规范也要求将非可信服务端的 annotations 视为不可信。仅本地配置中对**精确 server/tool 身份**的显式声明可将其设为 `none`，不能用服务端提供的自由文本或宽泛通配推断只读。

降级为 `none` 只影响既有 planning/generation 分类，不等于无风险、免权限或自动成为验证证据。默认 `possible` 的 MCP 调用在 `exploring` 中拒绝；若确需只读规划调查，须先精确配置且仍经过 PermissionGate。权限规则可按外显 server/tool 名配置；授权提示必须同时显示 server、原始 tool 名和有界、脱敏的参数摘要，避免仅凭规范化名称误认调用。`always` 不得因外部参数内的通配符扩大授权范围。

### D4：MCP 连接是 Runtime 基础设施，不是任务后台 Process

stdio Server 由本地显式配置启动，不由模型通过 `start_process` 创建，也不登记为当前任务的后台进程；连接本身不能被模型复用来写 stdin 或控制 PID。进程启动、环境变量、工作目录和远程 URL/header 的配置必须有明确边界；敏感值只从本地配置或环境读取，不进入 State、Context、session、Trace、工具结果、授权提示或终端错误。可以记录无凭据的 server alias、transport、协议版本和配置指纹。

Server 连接、PID、管道、HTTP session ID 不写入 session。Runtime/CLI 正常退出、异常退出、`/new`、`/reset` 与 EOF 时有界关闭 stdio Server；清理失败报告具体 server 与原因，不能悄悄遗留进程。重新创建 Runtime 或恢复任务时，从当前本地配置重新连接并发现目录；旧 session 中的配置或目录只可作历史事实，不可作为执行授权。配置启动本身可能运行外部代码，因此只允许用户明确配置并启用的 Server，不能根据模型或 Skill 文本动态启动。

### D5：副作用、持久工具边界与崩溃恢复继续由父 Runtime 管

MCP `tools/call` 与其他 Tool 一样，在 handler 前完成参数、阶段和权限校验。开启 `/save` 后，`possible` 调用必须先提交 schema 3 的 `handler_admitted`，再向 Server 发请求；每个 call 的 State、对应 `role=tool` 和工具边界仍按模型顺序原子提交，整轮 committed 前不得请求下一轮 LLM。提交失败不得发远程请求、进入后续 call 或请求 LLM。

请求已发出却未持久记录结果时，恢复分类为「已准入、结果不确定」，不能因重新连接成功而重放、补发或猜测成功。连接断开和超时也不能证明远端没有执行副作用。恢复中的调查、逐项 `/resolve`、重新规划、重新授权与独立验证沿用 v0.33 合同。外部 Tool 结果、Resource 和 Skill 内容都不能自行创建 verification evidence 或推进 Plan。

### D6：Skill 以元数据发现，以普通 Tool 结果加载

首版只发现固定的项目级和全局本地 `skills/<name>/SKILL.md`，不递归扫描任意目录，不从网络下载。目录 ID、frontmatter `name` 与调用参数采用同一稳定、受限的名称合同；同名时项目级优先于全局，重复来源、无效名称或不一致元数据必须可观察，不依赖文件遍历顺序。v0.45 的受限 frontmatter 仅解析 `name` 与 `description` 两个单行字符串，不声称支持完整 YAML；解析规则、未知字段处理及 UTF-8/大小上限在实现时固定并测试。

父模型平时只看到有界的 ID、`name` 和 `description` 目录提示；不得把全部正文常驻 system prompt。`skill(name)` 是父侧只读 Tool，权限 pattern 是 Skill ID，默认 `ask`；`deny` 的 Skill 不向模型展示，也拒绝直接调用。授权后重新核查目标文件的规范路径、符号链接终点、文件身份、编码与大小，再将正文作为该次调用的普通 `role=tool` 结果。加载结果受 Context 裁剪，可在后续轮次再次按需加载；不新增权威 `active_skills` State，也不把正文当成持久 system 指令。开启 `/save` 时，已加载正文可能随普通 history 进入 session，因此只承诺不额外复制整份 Skill 目录，不承诺 session 绝无正文。

Skill 内引用的脚本、模板和其他文件首版不自动执行或读取；如要访问，仍须走现有工具和权限。Skill 不能覆盖 system、`AGENTS.md`、PermissionGate、Plan 或用户明确要求；它只提供低优先级的工作流建议。

### D7：Resources 与 Prompts 保持各自的控制权

MCP Tools 是模型控制的动作；Resources 是应用选择、附加的资料；Prompts 是用户选择的模板。`v0.46` 的 Resource 列表/读取与 Prompt 列表/获取由 CLI/Host 显式入口触发，不注册成模型可自行调用的 Tool。资源正文作为有界、不可信资料进入当前 Context；用户选定并审阅的 Prompt 作为用户侧输入进入会话，但仍不能替代 system/项目指令或批准任何工具调用。可见的来源为 server alias 加 URI/Prompt 名，不将敏感 endpoint 或认证头带入 Context。

只接收文本 Resource 与文本 Prompt；URI、参数、内容长度、返回消息角色和数量均受限制。服务端返回的 Prompt 消息不能伪造 system、tool 或授权事件；不支持的内容类型或角色明确拒绝。资源读取与 Prompt 获取同样经过本地权限策略和来源校验，不因 Server 已连接而自动授权。Server instructions 不自动注入受保护 system prompt。

### D8：远程 HTTP 是显式限定的教学子集，不冒称完整传输兼容

`v0.46` 继续使用固定 `2025-11-25` 握手。在标准库 `http.client` 上实现受限的 Streamable HTTP POST：逐请求 JSON-RPC、必要的 `Accept` 和协议版本 header、初始化后的会话 header、认证 header 的本地注入、超时与响应大小限制。仅支持返回 `application/json` 的 Server；若 Server 返回 `text/event-stream`，明确报告不支持，不尝试把 SSE 当 JSON，也不声称兼容一般 Streamable HTTP Server。HTTP 认证失败、重定向、会话失效和网络中断不得触发会重放 `tools/call` 的自动重试。

远程 URL 需由用户本地配置显式列出，默认 HTTPS；若允许回环地址 HTTP，必须单独配置并记录其安全取舍。认证 token、真实 endpoint、HTTP session ID 均不进入持久化摘要。Remote 只是 Transport 变化，不改变 Tool 的默认 `possible`、权限、持久边界和子代理隔离。

### D9：子代理与 Context 预算不因外部能力自动放宽

阶段十二只给父 Runtime 注册 MCP、Skills 和用户/应用侧内容入口；子代理仍维持现有四工具白名单，不继承父 MCP 连接、Skill 目录、授权、Resource 或 Prompt 内容。父代理若要传递外部调查事实，只能通过既有有界 selected facts；子结果不升级为父验证证据。

MCP 工具目录、描述、Skill 目录提示、普通工具结果和显式 Resource/Prompt 输入均须有独立的长度/数量上限，并纳入现有 Context 预算。目录过大时采取确定性裁剪或明确拒绝，不悄悄挤掉受保护指令；所有外部正文都是不可信材料。Trace & Replay 只消费当时记录的结构化事实，不重新连接 Server、加载 Skill 或读取 Resource。

## 4. 版本切片与实施任务

### 4.1 `v0.43`：最小 stdio MCP Client（已实现）

目标是独立跑通一次受限、本地、可关闭的 MCP 会话，而不是立即给 Agent 加工具。

1. 新增 MCP client / stdio transport，使用标准库启动单个明确配置的 Server，完成 JSON-RPC 请求 ID 管理、响应/通知区分、stderr 排空、超时和有界关闭。
2. 固定 `2025-11-25`：按 `initialize → notifications/initialized → tools/list → tools/call` 运行，校验能力与版本；工具列表处理分页并冻结快照。
3. 提供测试用最小本地 Server fixture，不引入 MCP SDK 作为运行时依赖；覆盖正常调用、服务端错误、`isError`、坏协议、错 ID、未响应、分页、提前退出和清理。
4. 仅以测试/手动演示入口观察协议结果，不将 Tool 注册到父模型，也不为服务端调用开权限旁路。

验收已完成：本地 stdio Server 可以完成握手、列出所有分页工具、调用一次文本工具并可靠退出；不受支持版本与异常消息明确失败，且能力没有进入模型工具目录。

### 4.2 `v0.44`：MCP Tool 接入 Agent Runtime（已实现）

目标是让外部 Tool 对 Agent 来说仍是普通 Tool，同时复用所有现有执行边界。实现已
完成，核心落点是 `mcp/adapter.py`、`mcp/schema.py`、`tools/base.py`、
`tools/__init__.py`、`permission.py` 与 CLI/恢复生命周期。

1. 在父 Registry 装配阶段接入已冻结的 MCP 目录；建立命名、原始身份映射、schema 子集校验和冲突拒绝。不给模型暴露未经验证的工具定义。
2. 将 `tools/call` handler 经过 ToolExecutor、PermissionGate、Plan gate 和 Runtime；默认 `possible` / `ask`，精确本地配置才允许只读降级。区分协议错误、服务端 `isError`、超时和断连，并保持完整的 `role=tool` 回灌。
3. 接入 CLI 生命周期与 schema 3 持久工具边界；连接与 PID 不写 session，恢复时重新装配，已准入调用不自动重放。外部结果的 State/Trace excerpt 默认只留有界、脱敏摘要，不复制大段远端正文。
4. 覆盖权限拒绝、`exploring` gate、并列工具调用顺序、handler 前提交失败、远端已执行但结果提交失败、恢复交接、子代理不可见和进程清理；离线验收见 `tests/test_mcp_v044.py`。

验收已完成：父模型可像使用 `read_file` 一样调用一个获准的 `mcp_<server>_<tool>`；拒绝或提交失败时远端没有收到调用；已发出而结果不确定的调用在崩溃恢复后不重放。

### 4.3 `v0.45`：本地 Skills 的发现与按需加载（已实现）

目标是让模型按元数据选择工作流，而不是把所有 Skill 正文塞进提示词。已按本节合同实现。

1. 新增项目级/全局 Skill catalog：固定目录、受限 frontmatter、确定性优先级、同名处理、文件身份和大小校验；只发布有界的 ID、名称、描述。
2. 注册父侧 `skill(name)` 只读 Tool；加载前按 Skill ID 走 PermissionGate，拒绝项不展示，正文以普通 Tool 结果进入 history/Context。明确 Skill 的来源、信任等级和可裁剪行为。
3. 覆盖同名覆盖、描述缺失、无效 frontmatter、符号链接逃逸、目录变化、读取竞态、超大正文、拒绝加载、session 恢复和子代理不可见。
4. 用一个只指导现有 Tools / MCP Tools 的示例 Skill 演示“工作流指导不等于执行授权”，不让 Skill 自动执行脚本或跳过批准。

验收：模型在未加载时只见元数据；调用 `skill(name)` 后才见完整正文；Skill 中即使写有执行命令，后续副作用 Tool 仍独立请求权限，且 Skill 正文不能进入受保护 system 指令。

### 4.4 `v0.46`：MCP 能力扩展与阶段收口（规划中）

目标是展示同一 MCP Client 在远程传输、资料和模板上的扩展，而不追求生产级远程 MCP 平台。

1. 增加显式配置的远程 HTTP transport，仅处理 `application/json` 响应；复用握手、请求配对、会话和 Tool adapter，SSE 响应明确报不支持。测试使用本地 HTTP fixture，不依赖公网服务。
2. 增加有界文本 `resources/list` / `resources/read` 与 `prompts/list` / `prompts/get`；分别提供应用侧选择和用户侧选择入口，实施来源、内容类型、角色与权限校验，不注册为模型自主 Tool。
3. 把 Skill 目录提示、MCP Tool 目录和显式 Resource/Prompt 内容纳入现有 Context 预算与安全提示；核查远程认证、session、Trace、恢复及不可信内容边界。
4. 完成四课的协议范围说明和跨版本端到端示例；明确 JSON-only 远程子集、未实现能力及与 OpenCode/MCP 完整实现的差异。

验收：可连接一个返回 JSON 的受控远程测试 Server，使用其 Tool，并由用户/应用显式选取文本 Resource/Prompt；返回 SSE、不支持的内容或需要 OAuth 的 Server 给出明确能力边界，不自动 fallback 或重试副作用调用。

## 5. 文件与边界映射

| 位置 | 预计变化 | 责任 |
|---|---|---|
| `src/mini_agent/mcp/` | `v0.43`–`v0.46` 新增 | `v0.43` 的 JSON-RPC、stdio transport、协议 client、目录快照与有界错误；后续版本再扩展 |
| `src/mini_agent/tools/` | `v0.44`–`v0.45` 增量 | MCP Tool adapter 与 `skill(name)`；继续使用统一 Executor |
| `src/mini_agent/skills.py` | `v0.45` 新增 | 本地发现、受限 frontmatter、优先级与安全读取 |
| `src/mini_agent/permission.py` | 增量修改 | MCP server/tool 与 Skill ID 权限 pattern、脱敏提示 |
| `src/mini_agent/context.py`、`prompt.py` | 增量修改 | 有界目录提示、低优先级 Skill/Resource 材料、预算与信任说明 |
| `src/mini_agent/__main__.py`、`config.py` | 增量修改 | 父 Runtime 装配、显式本地配置、CLI 内容入口与连接清理 |
| `src/mini_agent/state.py`、`session.py`、`runtime.py` | 尽量不改协议 | 复用既有工具边界、完成判定与恢复；如必须改 schema，先证明旧会话兼容 |
| `tests/`、`docs/tutorials/`、操作手册、`README.md`、`CHANGELOG.md` | 每版同步 | 协议 fixture、失效/安全验收、一版一个教学主线 |

## 6. 横向验收与交付

- 每版均有不依赖外网的协议/行为测试；除成功路径外，覆盖错误 ID、异常输出、分页上限、超时、断连、命名冲突、schema 不支持、权限拒绝、版本不符和清理失败。
- MCP Tool 的默认副作用等级、`handler_admitted` 提交、顺序回灌、恢复不重放、Plan 只读阶段与子代理白名单在整个阶段保持一致；任何外部结果都不能冒充 verification evidence。
- Skill、Resource、Prompt 和 Server instructions 不得覆盖受保护指令或 PermissionGate；认证 header、真实 endpoint、PID、HTTP session ID 不进入 State、session、Trace 或用户可见错误。普通 tool history 中的 Skill/Resource 正文可能随 `/save` 保存，须明确披露并控制大小。
- 默认安装与核心运行继续仅依赖标准库；LLM HTTP 仍使用 `http.client` 并显式设置 `Accept-Encoding: identity`，MCP HTTP 实现不改变该约束。
- `v0.43`–`v0.46` 每版有独立教程、变更记录、操作手册说明和可复现验收场景；完成相应版本时同步 README、教程索引和版本信息。Git tag 仅由用户本人手动操作。
- 修改实现后至少运行相关测试；交付前运行 `PYTHONPATH=src python -m pytest -q`、`PYTHONPATH=src python scripts/check_tutorials.py`、`PYTHONPATH=src python scripts/check_readme.py`，或说明无法运行的原因。

v0.45 完成后，父 Agent 能使用经本地授权的 MCP Tool，并按需加载本地 Skill 指导工作流；v0.46 仍负责在明确限定范围内增加远程文本 Resource 和用户选择的 Prompt。MCP 与 Skills 都不改变谁有权执行、何时必须授权、什么事实可用于恢复和验证。
