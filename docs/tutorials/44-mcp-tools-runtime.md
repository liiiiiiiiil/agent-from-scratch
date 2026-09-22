# 第 44 课：MCP Tool 接入父 Agent Runtime

上一课：[最小 stdio MCP 客户端](43-stdio-mcp-client.md) · [教程总览](README.md) · 下一课：v0.45 本地 Skills（规划中）

代码快照：`v0.44` · 相邻差异：`v0.43..v0.44`

本课示例命令使用 Bash/zsh。`v0.44` tag 由维护者在交付后固定；阅读者切换前请确认本地已有该 tag。

## 本课目标

第 43 课已经能连接本地 MCP Server、完整读取工具目录并完成一次人工确认的调用，但这些工具还没有进入 Agent。Agent 也就没有办法用自己的 tool call 请求 MCP 能力，或者让 MCP 调用遵守项目已有的权限、计划、持久化和恢复规则。

本课要解决的是“把一个外部工具接进 Agent 后，谁负责什么”。读者会看到：本地配置决定哪些 Server 可以进入父 Agent；Adapter 把冻结的 MCP 目录转换成普通 `Tool`；ToolExecutor 负责调用前检查和调用后记录；MCP 连接只属于当前父 Runtime，Subagent 看不到它。

## 前置条件

需要 Python 3.10+、基础 Python、Bash/zsh 和 Git 知识。建议先阅读第 43 课，因为本课沿用它的 JSON-RPC、stdio、握手、分页和关闭实现。本课的离线示例只使用标准库，不访问网络，也不需要真实 LLM。

查看相邻版本时，可以执行：

```bash
git checkout v0.43
git diff --stat v0.43..v0.44
git checkout v0.44
```

第一条命令切到父侧还没有 MCP Tool 的基线，第二条命令显示本版的改动范围，最后一条命令让下面的代码和实现保持一致。阅读结束后回到原来的分支：

```bash
git checkout -
```

## 新增与改动文件

本版的关键变化发生在“冻结目录之后、模型请求之前”。第 43 课的 Client 仍然负责协议和子进程；本版新增的 Adapter、schema 校验和连接管理把它接到现有父侧执行链。

| 文件 | 作用 |
|---|---|
| [`mcp/adapter.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.44/src/mini_agent/mcp/adapter.py) | 将冻结的 MCP 工具转换成普通 `Tool`，保存精确的 alias/原始工具名，并统一关闭父 Runtime 持有的 Client。 |
| [`mcp/schema.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.44/src/mini_agent/mcp/schema.py) | 校验受支持的 MCP input schema，并在工具调用发送前校验参数。 |
| [`tools/base.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.44/src/mini_agent/tools/base.py) | 增加受控 handler 结果入口，使 Adapter 可以提供明确的 outcome、error_kind 和 State/Trace 摘要。 |
| `tools/__init__.py` | 在父侧 `create_registry()` 装配启用的 MCP Tools；模块级 smoke registry 和 Subagent 视图保持不接入。 |
| `config.py`、`config_example.py` | 增加默认关闭的 `agent_enabled` 和精确的 `readonly_tools`。 |
| `permission.py` | 为 MCP 授权提示展示 alias/原始工具名，并隐藏疑似凭据和长参数值。 |
| `__main__.py`、`resume.py` | 在新任务、恢复、任务切换和退出时重新装配或有界关闭 MCP 连接。 |
| `tests/test_mcp_v044.py`、`tests/fixtures/mcp_stdio_server.py` | 用本地 fixture 观察父侧调用、失败结果、超时、断连、目录冲突和清理。 |

## 版本变更定位

先看上一版的真实入口，再看本版插入的位置。图中的 MCP Server 是独立子进程，`McpClient` 只负责 MCP 协议，不负责 Agent 权限。

```text
[旧] python -m mini_agent.mcp
  → [旧] McpClient / StdioTransport
  → [旧] initialize → initialized → tools/list → tools/call
  → [旧] 独立 CLI 人工确认并关闭
```

```text
[+] MCP_SERVERS(agent_enabled=True, readonly_tools)
  → [旧] McpClient / StdioTransport
  → [旧] 冻结 tools/list 目录
  → [+] mcp/schema.py 严格校验 + mcp/adapter.py 命名和原始身份映射
  → [+] Tool Registry
  → [C] ToolExecutor → PermissionGate → Plan gate → schema 3 handler_admitted
  → [C] AgentRuntime.run() → role=tool → 下一轮模型请求
       ├─ [B] schema、名称、连接或清理失败：拒绝整次装配并关闭已启动 Server
       └─ [B] 超时/断连/协议错误：失败结果回灌，不重试 tools/call
```

图例：`[旧]` v0.43 已有，`[+]` v0.44 新增，`[C]` 主要消费者，`[B]` 本版边界。父侧的连接管理对象不会进入 `State`、session 或 Subagent；它只由当前 Runtime 持有。

## 关键流程

模型看到的名字和 Server 收到的名字不是同一个字符串。这样做是为了避免从一个可能被规范化的外显名反推出原始身份。Adapter 在建立闭包时直接保存 alias 和原始工具名：

```text
Server 原始工具名: read-only
             ↓ lower + '-' → '_'
父 Agent 外显名:  mcp_demo_read_only
             ↓ handler 闭包保存原始身份
MCP tools/call:  name="read-only"
```

一次成功调用的大致顺序如下：

```text
模型提交 mcp_demo_echo
  → Runtime 解析 tool call
  → Registry 校验 MCP 参数
  → PermissionGate 请求一次/始终/拒绝
  → possible 调用预留 generation；若启用 /save，提交 handler_admitted
  → Adapter 调用冻结 Client 的原始工具名
  → Client 发送 tools/call
  → Adapter 只接受有界 UTF-8 text 内容
  → Executor 生成 ExecutionResult
  → State/Trace 只记录 metadata excerpt
  → role=tool 回灌有限正文
```

如果权限被拒绝、参数不合法或当前阶段是只读 Explore，流程会在 `tools/call` 之前结束。若调用已经准入而远端超时或连接断开，结果会标记为不确定的外部事实边界，恢复流程不会因为重新连接成功而重放调用。

## 实现拆解

### 1. 用配置明确区分独立 Client 和父 Runtime

配置中已有的 Server 不能因为存在于列表里就自动启动。`agent_enabled` 的默认值是 `False`，所以升级到 v0.44 后旧配置仍然只影响独立命令。只有明确打开的 Server 才会在父 `create_registry(state=...)` 时连接。

```python
MCP_SERVERS = [{
    "alias": "demo",
    "command": ["python", "../../tests/fixtures/mcp_stdio_server.py"],
    "cwd": ".",
    "environment": {},
    "agent_enabled": True,
    "readonly_tools": ["echo"],
}]
```

`readonly_tools` 匹配同一 Server 的原始名称，大小写和连字符都按原文匹配。它只改变副作用分类：`echo` 的 `effect_class` 是 `none`，但默认权限仍是 `ask`。服务端的 `readOnlyHint` 等 annotations 不参与这个决定。

### 2. 在进入模型目录前校验 schema 和名称

MCP 的 `inputSchema` 是外部数据，而项目原来的通用参数校验器支持的字段比本版 MCP 合同更宽松。`mcp/schema.py` 因此只接受根 object、标量属性、标量数组、必要字段、布尔 `additionalProperties`、`enum`、字符串和数值边界、数组长度边界。嵌套 object、未知关键字、重复 `required` 和超限 schema 都会拒绝。

名称校验同样发生在注册前。原始工具名必须是有限的 ASCII 字符，规范化后的外显名必须不超过 64 个字符；和内置工具或其他 MCP Tool 碰撞时，已经启动的所有 Client 都会被关闭，Registry 不会留下半套目录。目录总大小和父模型可见的 MCP Tool 数量也有上限。

### 3. 把远端结果放回普通 Executor 边界

Adapter 的 handler 不直接修改 State，也不直接写对话 history。它返回一个受控结果，Executor 再把这个结果变成 `ExecutionResult`。因此 JSON-RPC `error`、`isError=true`、超时、断连、协议错误和图片/二进制/结构化内容都有独立 `error_kind`；超时还使用 `outcome="timeout"`。

成功结果只接受有界 UTF-8 文本。文本正文可以作为对应 `role=tool` 内容让模型继续工作，但 `output_excerpt` 只保存类似来源 alias、原始工具名、结果类别和字节数的元数据。State 和 Trace 因而不会把远端大段正文当成事实摘要，也不会保存 MCP 配置中的命令、环境值或 PID。

### 4. 复用持久工具边界和恢复规则

开启 `/save` 时，MCP Tool 与普通 possible Tool 共用 schema 3 的 `handler_admitted` 记录。提交失败，Adapter 的闭包不会进入 Client；每轮的工具结果仍按模型的 call 顺序提交。已经发送远端请求但在结果提交前崩溃的调用，在恢复时按既有 durable boundary 分类为未确定事实，不会自动重放。

新任务和恢复任务会从当前本地配置重新连接、重新分页和重新校验目录。连接不从 session 恢复，旧 Runtime 的 handler 也不会被新任务复用。`/new`、`/reset`、EOF、正常退出和异常退出会关闭 Runtime 持有的 Client；关闭失败会报告 alias 和有界原因。

## 为什么这样设计

本版选择把 MCP Tool 做成普通 `Tool`，这样已有的 Registry、PermissionGate、Plan gate、ToolExecutor、Runtime 和 schema 3 边界继续只有一套行为。另一种做法是给 MCP 单独写一条调用路径，但那会让权限拒绝、回灌顺序和恢复语义分叉，远端调用更难审计。

本版把 `agent_enabled` 放在 Server 配置上，而不是让模型或工具描述决定是否启动。启动本地命令本身就是副作用，必须来自用户维护的本地配置；省略字段继续保持 v0.43 行为，升级时不会意外拉起 Server。

本版只实现 MCP 参数和结果的严格子集。拒绝未知 schema 关键字会减少兼容面，却避免把未实现的约束静默丢掉。图片、二进制和结构化结果也明确失败，因为把它们强行转成文本会改变模型看到的事实。

## 设计边界

- 只支持 v0.43 已有的本地 stdio、固定 MCP `2025-11-25`、Tools 和有界文本结果；不接入远程 HTTP、Resources、Prompts 或 Skills。
- MCP Tool 只注册到父 Runtime；Subagent 仍固定使用 `calculate`、`read_file`、`list_dir`、`grep` 四工具视图。
- `readonly_tools` 是精确原始工具名列表，不接受通配符；降为 `none` 不等于免授权，也不自动提供 verification 证据。
- 工具目录冻结后不会热刷新；Client 失效后不自动重连，不重试 `tools/call`。
- Server 名称、描述、annotations、schema 和结果都是不可信输入，不能覆盖 system/project instructions、PermissionGate、Plan 或恢复决定。
- 连接、命令 argv、环境值、PID 和认证信息只保留在当前进程内；State、Trace、session、授权提示和用户可见错误只使用无凭据的有界摘要。

## 运行与观察

在未跟踪的 `src/mini_agent/config_local.py` 中显式打开 fixture Server：

```python
MCP_SERVERS = [{
    "alias": "demo",
    "command": ["python", "../../tests/fixtures/mcp_stdio_server.py"],
    "cwd": ".",
    "environment": {},
    "agent_enabled": True,
    "readonly_tools": ["echo"],
}]
```

然后运行父 CLI：

```bash
PYTHONPATH=src python -m mini_agent
```

当模型请求 `mcp_demo_echo` 时，终端会先显示父侧 PermissionGate 的授权提示，其中包含 `alias=demo` 和原始工具名 `echo`。输入 `once` 后，fixture 会记录一次 `tools/call`；输入 `reject` 时，模型仍会收到一个拒绝结果，而 fixture 不会记录调用。把 `agent_enabled` 删除后再次启动父 CLI，Server 不会被连接；独立命令仍可以使用同一 alias：

```bash
PYTHONPATH=src python -m mini_agent.mcp demo list
```

离线父侧示例使用同一个 fixture，能够观察到成功结果、`isError=true`、JSON-RPC error、超时、断连、图片/结构化结果拒绝，以及目录冲突时的清理。完整实现和测试索引见本课末尾的 tag 固定链接。

## 本版特性、下一课与代码索引

v0.44 让父 Agent 可以像调用内置 Tool 一样调用一个经本地配置启用的 MCP Tool。调用路径统一经过权限、阶段、持久化和恢复边界；结果和失败会按 `role=tool` 回灌，外部资料不会升级成 verification evidence。旧的独立 MCP CLI 仍保持 v0.43 的逐次人工确认行为。

下一课 v0.45 计划介绍本地 Skills。Skill 是帮助模型组合已有能力的工作流说明，不是新的 Tool 权限；它不会改变本课建立的 MCP 执行边界。

固定代码索引：[`adapter.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.44/src/mini_agent/mcp/adapter.py)、[`schema.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.44/src/mini_agent/mcp/schema.py)、[`tools/base.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.44/src/mini_agent/tools/base.py)、[`tools/__init__.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.44/src/mini_agent/tools/__init__.py)、[`test_mcp_v044.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.44/tests/test_mcp_v044.py)。
