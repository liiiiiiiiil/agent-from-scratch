# 第 44 课：让 Agent 调用 MCP Tool

上一课：[用 stdio 连接一个 MCP Server](43-stdio-mcp-client.md) · [教程总览](README.md) · 下一课：[本地 Skills](45-local-skills.md)

代码快照：`v0.44` · 相邻差异：`v0.43..v0.44`

本课命令使用 Bash/zsh。下面的代码链接和示例都对应 `v0.44`。

## 本课目标

上一课中，你在命令行里手动确认后，Client 才会调用 MCP Server。Agent 还看不到这个工具。本课解决的问题是：怎样让模型请求外部工具，同时让 Agent 保持对执行过程的控制？

先认识 Agent 的一轮工作：它把任务交给模型；模型可以回复文字，也可以提交一个结构化的工具请求；Agent 决定是否执行请求，把结果交回模型，然后继续下一轮。MCP Tool 接入后，外部调用要走同一条路。模型能提出请求，但不能越过本地配置和权限检查直接控制 Server。

读完后，你应能解释 `agent_enabled` 为什么默认关闭、`readonly_tools` 实际允许什么，以及 MCP 调用怎样经过现有的 Agent 执行流程。

## 前置条件

只需要基础 Python、终端和 Git；建议先读第 43 课，了解 MCP Client 如何连接 Server。这里用到几个名称：Tool 是 Agent 可请求的一项操作；Tool Registry 是 Agent 提供给模型看的工具目录；ToolExecutor 是 Agent 实际检查并执行工具请求的部分。

先查看本版相对 v0.43 的改动，再切换到对应源码：

```bash
git checkout v0.43
git diff --stat v0.43..v0.44
git checkout v0.44
```

阅读完毕后，用 `git checkout -` 返回切换前所在的分支。

## 新增与改动文件

本版保留第 43 课的 MCP 通信方式，新增“把外部工具接入 Agent 工具目录”的部分：

| 文件 | 负责什么 |
|---|---|
| [mcp/adapter.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.44/src/mini_agent/mcp/adapter.py) | 把 Server 工具转换成 Agent 能识别的 Tool，并持有连接。 |
| [mcp/schema.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.44/src/mini_agent/mcp/schema.py) | 检查 Server 给出的参数说明，以及模型实际提交的参数。 |
| [tools/__init__.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.44/src/mini_agent/tools/__init__.py) | 创建父 Agent 的工具目录时，装入已启用的 MCP Tool。 |
| [tools/base.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.44/src/mini_agent/tools/base.py) | 让外部工具结果回到项目统一的执行结果格式。 |
| [permission.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.44/src/mini_agent/permission.py) | 在调用前向用户说明并询问是否授权。 |
| [runtime.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.44/src/mini_agent/runtime.py) | 运行模型请求、工具执行、结果回传组成的循环。 |

## 版本变更定位

v0.43 只有独立 MCP 命令；它不经过 Agent。下面两张图先标出这条旧路径，再显示 v0.44 把 MCP 工具接入的位置。

```text
[旧] v0.43 已有：
[旧] 命令行 → [旧] MCP Client → [旧] Server
                └─ [旧] 你在命令行确认后直接调用
```

```text
[+] v0.44 新增：
[+] 本地配置（agent_enabled=True）
  → [C] create_registry 连接 Server 并冻结工具目录
  → [+] Adapter 把目录翻译成 Agent Tool
  → [C] Tool Registry 把它提供给模型
  → [旧] 模型提出工具请求
  → [旧] ToolExecutor 检查授权并执行
  → [旧] 结果以 role=tool 消息交回模型
       └─ [B] 装配失败会关闭已连接的 Server；调用失败不重试
```

图例：`[旧]` v0.43 已有；`[+]` v0.44 新增；`[~]` v0.44 修改；`[C]` 主要消费者；`[B]` 本版边界。这里的 `role=tool` 是对话协议里表示“工具执行结果”的消息类别。它不是模型的回复，也不是用户的新问题。模型拿到结果后，Runtime 才会开始下一轮。

## 关键流程

一次调用从模型请求到 MCP Server 的过程如下：

```text
模型请求 mcp_demo_echo
  → Agent 找到注册的工具并检查参数
  → 检查当前任务计划是否允许这一步
  → PermissionGate（权限检查）询问你是否允许
  → 通过后，Adapter 用原始名称调用 MCP Server
  → 结果交回模型，模型据此继续回答
```

`Adapter` 是两种接口之间的转换层：模型看到项目统一的工具名和参数；Adapter 记住这个工具来自哪个 Server、Server 原本叫什么，再把请求翻译回 MCP 格式。比如 Server 的 `read-only` 会显示为 `mcp_demo_read_only`，但发给 Server 时仍使用原名 `read-only`。

如果你拒绝授权，或参数不符合工具声明，Server 不会收到调用。若请求已发送但 Server 超时或断线，Agent 会记录失败并把结果交回模型，不会猜测 Server 是否完成后再自动重试。

## 实现拆解

### 1. 配置明确选择哪些 Server 能进入 Agent

`MCP_SERVERS` 中的配置只描述本机 Server 怎样启动。v0.44 新增 `agent_enabled`，默认是 `False`：只有你显式启用后，Server 的工具才会进入父 Agent 的工具目录。旧配置因此不会在升级后自动启动。

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

`readonly_tools` 必须写 Server 返回的原始工具名，并按大小写和连字符精确匹配。列入后只表示本地配置把该工具归为“不会修改外部状态”；默认权限仍是 `ask`，用户仍需批准。Server 自己声称工具只读，不足以改变这个分类。

### 2. 在交给模型前检查外部工具说明

Server 返回的工具名和参数说明属于外部数据。Adapter 在把它们放进 Registry 前检查格式、大小和重名；模型实际调用时还会再次检查参数。无法支持的参数格式会拒绝整次装配，避免把不理解的约束悄悄交给模型。

### 3. 通过统一的执行器处理请求和结果

ToolExecutor 是内置工具和 MCP 工具共用的执行边界：它检查参数、确认你是否授权、遵守当前任务计划的限制，再记录执行结果并按调用 ID 把 `role=tool` 消息交给模型。成功时，有限文本可供模型继续工作；State（Agent 的结构化任务记录）和 Trace（按顺序保存的执行事件）只记录来源、结果类别和字节数等摘要，不复制整个远端正文。

如果启用了 `/save`（把当前任务保存为可恢复会话的命令），工具开始执行前还要先提交 `handler_admitted` 边界记录。它表示“本地已准许进入处理函数”。若这次提交失败，就不会调用 Server；若之后发生崩溃，已经准入的 MCP 调用会被当成结果不确定的事实，不会因恢复连接而重放。

### 4. 连接属于当前父 Agent

Runtime 启动时根据当前本地配置连接 Server、读取完整工具目录并冻结结果。新任务或恢复任务会重新连接；连接本身不从保存的 session 复用。任务结束、切换或退出时，Runtime 负责关闭连接。

Subagent（为父 Agent 做只读调查的子运行实例）继续只有固定的四个基础工具，不会继承 MCP Tool、父权限或父任务状态。

## 为什么这样设计

把 MCP 工具转换成普通 Tool，可以继续使用项目已经有的授权、计划检查、结果记录和恢复规则。如果 MCP 另走一套执行路径，拒绝授权、回传结果和崩溃恢复就容易与内置工具产生不同规则。

启动本地 Server 也属于需要明确控制的动作，所以默认关闭父 Agent 接入。另一个取舍是只接受受限的参数 Schema 和有界文本结果；图片、二进制或无法解释的结构化返回会明确失败，不会被伪装成普通文本。

## 运行与观察

若本机已有 LLM 配置，可在未跟踪的 `src/mini_agent/config_local.py` 中加入上面的 `demo` 配置，然后启动父 CLI：

```bash
PYTHONPATH=src python -m mini_agent
```

向 Agent 提出一个需要 `echo` 的请求。终端应先显示包含 alias 和原始工具名的授权提示。选 `once` 后，fixture Server 才会收到一次调用；选 `reject` 后，Agent 会收到拒绝结果，Server 不会收到调用。这个现象说明模型只是提出请求，执行权仍由父 Agent 的权限检查控制。

若从配置中删掉 `agent_enabled` 或设为 `False`，父 Agent 不会连接该 Server；第 43 课的独立命令仍可单独使用它。

## 设计边界

- 只接入 stdio MCP Tool；Resource、Prompt 和 HTTP 在后续版本讲解。
- 工具目录建立后不热刷新。连接失效后不自动重连，也不重试 `tools/call`。
- `readonly_tools` 只是精确的副作用分类，不能跳过默认授权，也不自动成为验证证据。
- MCP 工具只进入父 Agent。Server 名称、说明、参数和结果均按不可信外部数据处理。

## 本版特性、下一课与代码索引

v0.44 让父 Agent 能请求已由本地配置启用的 MCP Tool。请求经过 Agent 自己的参数、权限和执行边界，结果再回到模型。下一课会介绍一种不同能力：只提供工作步骤说明、不直接执行动作的本地 Skill。

固定代码索引：[adapter.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.44/src/mini_agent/mcp/adapter.py)、[schema.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.44/src/mini_agent/mcp/schema.py)、[tools/__init__.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.44/src/mini_agent/tools/__init__.py)、[tools/base.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.44/src/mini_agent/tools/base.py)、[permission.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.44/src/mini_agent/permission.py)。
