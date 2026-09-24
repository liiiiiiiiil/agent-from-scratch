# 第 43 课：用 stdio 连接一个 MCP Server

上一课：[具名本地 References](42-local-references.md) · [教程总览](README.md) · 下一课：[把 MCP Tool 接入 Agent](44-mcp-tools-runtime.md)

代码快照：`v0.43` · 相邻差异：`v0.42..v0.43`

本课命令使用 Bash/zsh。下面的代码链接和示例都对应 `v0.43`。

## 本课目标

想让程序使用另一个程序提供的能力，双方需要约定消息怎么写、先说什么、怎样知道对方回应的是哪次请求。MCP（Model Context Protocol）就是一套这样的约定；MCP Server 提供工具，MCP Client 负责连接并发出请求。

本课先写一个独立 Client：它启动本机 Server，列出工具，并在你确认后调用一个工具。它还没有接入 Agent，所以语言模型不会替你挑工具。读完后，你应能看懂一次 MCP 会话的顺序，并说清楚这条演示路径为什么还不是 Agent 的工具授权系统。

## 前置条件

只需要基础 Python、终端和 Git。这里的 Agent 指“根据用户任务向模型提问，并执行模型所请求工具”的程序；本课暂时不运行这条 Agent 流程，而是单独观察 Client 与 Server 如何通信。

先查看上一版和本版的差异，再切到本课代码：

```bash
git checkout v0.42
git diff --stat v0.42..v0.43
git checkout v0.43
```

阅读完毕后，用 `git checkout -` 返回切换前所在的分支。

## 新增与改动文件

本版新增的是一条独立的连接路径。下表只列理解这条路径需要认识的部分：

| 文件 | 负责什么 |
|---|---|
| [mcp/client.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.43/src/mini_agent/mcp/client.py) | 按 MCP 顺序握手、列出并保存工具目录、发送工具调用。 |
| [mcp/stdio.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.43/src/mini_agent/mcp/stdio.py) | 启动本地 Server，并分别读取协议消息和诊断输出。 |
| [mcp/protocol.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.43/src/mini_agent/mcp/protocol.py) | 检查请求、通知和响应的 JSON-RPC 消息格式。 |
| [mcp/__main__.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.43/src/mini_agent/mcp/__main__.py) | 提供 `list` 和每次都要确认的 `call` 命令。 |
| [config.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.43/src/mini_agent/config.py) | 读取本机 MCP Server 配置；导入配置时不会启动 Server。 |

## 版本变更定位

先把新旧两条路径分开看。v0.42 已有的 Agent 工具流程由 Runtime 管理；v0.43 新增的 MCP 命令独立运行，不会接入那个流程。

```text
上一版 v0.42：
[旧] 用户任务 → [旧] AgentRuntime ↔ [旧] 模型
                         └─ [旧] 模型请求工具 → [旧] ToolExecutor → [旧] 工具结果

本版 v0.43 新增：
[+] 命令行 `python -m mini_agent.mcp`
  → [C] McpClient → [+] StdioTransport → [+] 本机 MCP Server
       └─ [B] 独立演示路径；不接入 AgentRuntime 或 PermissionGate
```

图例：`[旧]` v0.42 已有；`[+]` v0.43 新增；`[C]` 主要消费者；`[B]` 本版边界。这就是本课最重要的结论：Client 能调用 Server，不代表 Agent 已经可以调用它。下一课才会把外部工具接入 Agent 已有的执行和授权流程。

项目把负责模型与工具循环的部分称为 `AgentRuntime`。下文会提到它来标出边界；本课不需要修改或启动它。

## 关键流程

stdio 是进程之间的两条管道：Client 写 Server 的标准输入（stdin），Server 把协议回复写到标准输出（stdout）；诊断信息写到标准错误（stderr）。程序因此能把机器要读的消息和人要看的日志分开。

一次会话先确认彼此支持的协议，再列目录，最后才能调用工具：

```text
Client                                      Server
  │── initialize（请求 id=1） ──────────────>│  确认协议版本和能力
  │<─ response（id=1） ──────────────────────│
  │── notifications/initialized（通知） ────>│  握手完成
  │── tools/list（请求 id=2） ──────────────>│  返回第一页工具
  │<─ response（id=2，可能有 nextCursor） ───│
  │── tools/list（请求 id=3，可选） ─────────>│  返回后续页
  │── tools/call（请求 id=4） ──────────────>│  执行指定工具
  │<─ response（id=4） ──────────────────────│
  └── 关闭 stdin，并回收 Server 进程
```

`initialize`、`tools/list` 和 `tools/call` 是请求，都会带一个 ID；Client 用相同 ID 找到对应的回复。`notifications/initialized` 是通知，只表示一个事件，不需要回复，因此没有 ID。本版固定使用 MCP `2025-11-25`，不会尝试猜测或切换协议版本。JSON-RPC 是承载这些消息的格式：回复中的 `error` 表示协议请求失败；工具也可能正常返回一个 `result`，但在结果里标记 `isError=true`，表示工具执行后报告了业务错误。

下面是 Client 中握手的关键顺序。省略号代表版本和返回值检查；要点是完成 `initialize` 后才通知 Server 握手结束：

```python
result = self._request(
    "initialize",
    {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "mini-agent", "version": __version__},
    },
    timeout=self.startup_timeout,
)
# 检查 Server 返回的协议版本与能力
self._send_notification("notifications/initialized", None)
```

这是 [`McpClient._connect()`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.43/src/mini_agent/mcp/client.py) 的简化摘录，省略了检查代码。完成握手后，Client 还会把工具目录读完再保存为快照。MCP 可以把长目录分成多页；`nextCursor` 是“还有下一页”的位置标记。重复游标、重复工具名或格式不符都会使整个目录失败，Client 不会只拿第一页就继续调用。

## 实现拆解

### 1. 启动进程时不经过命令解释器

配置中的命令按参数列表传给 `subprocess.Popen`，使用 `shell=False`。这表示 `python` 和脚本路径会作为明确参数执行，而不会先交给 shell 解释 `;`、管道或变量展开。配置可以指定工作目录和环境变量，但这些值只从本机 `config_local.py` 读取。

### 2. 协议输出和日志分开处理

stdout 逐行读取 UTF-8 JSON-RPC；stderr 独立排空，只保留末尾一小段诊断内容。单条消息、等待队列和关闭时间都有上限。遇到坏编码、坏 JSON、超时或进程退出时，连接会失效；可能已经执行过的 `tools/call` 不会自动重试。

关闭时先关 stdin，再等待 Server 结束；超时后才尝试终止进程并回收它。这样既给 Server 正常收尾的机会，也避免留下后台子进程。

### 3. 人工确认只保护独立命令

独立命令会在调用前完整显示 Server alias、工具原名和参数，并要求你输入确认。输入不是明确的 `yes` 或 `y`、参数无法完整显示，或 stdin 不是交互终端时，都不会发送调用。

这里的确认只属于这个命令行演示。它没有 Agent 的 PermissionGate（工具授权检查），也不会为后续的模型调用保存授权。

## 为什么这样设计

本课先单独验证协议和进程边界，可以看清 Client 要承担的工作：按顺序握手、配对请求与回复、读完目录，并在失败时关闭 Server。若一开始就把这些行为和 Agent 授权混在一起，读者很难判断一次失败来自 MCP 通信还是 Agent 决策。

本版使用 stdio，是因为本机进程的输入、输出和关闭过程容易观察。它只支持固定的 MCP `2025-11-25`，不探测其他版本、不重试调用，也不支持 HTTP、Resource 或 Prompt。Server 主动发起带 ID 的请求也会导致连接关闭；本课只实现 Client 主动请求的路径。

## 运行与观察

若想复现下面的结果，可在未跟踪的 `src/mini_agent/config_local.py` 中配置本课自带的本机演示 Server：

```python
MCP_SERVERS = [{
    "alias": "demo",
    "command": ["python", "../../tests/fixtures/mcp_stdio_server.py"],
    "cwd": ".",
    "environment": {},
}]
```

相对 `command` 和 `cwd` 从 `src/mini_agent` 目录解析。然后在仓库根目录运行：

```bash
PYTHONPATH=src python -m mini_agent.mcp demo list
PYTHONPATH=src python -m mini_agent.mcp demo call echo '{"text":"hello"}'
```

第一条命令应列出 `echo`、`sum`、`third`。它们分成两页，所以 Client 会发送两次 `tools/list`。第二条命令会先展示将要发送的参数；确认后，Server 返回的文本中应包含 `hello`。不确认时，Client 不发送 `tools/call`。

## 设计边界

- Client 只读取并调用冻结的工具目录；收到目录变化通知也不会在运行中刷新。
- `tools/call` 超时或断线后不自动重试，因为 Server 可能已经执行了动作。
- 本课没有接入 AgentRuntime、Tool Registry、PermissionGate、State 或 session。独立命令的确认不能代替后续 Agent 的权限检查。
- 只允许有界文本结果；本版不处理 Resource、Prompt、二进制结果或 Server 主动请求。

## 本版特性、下一课与代码索引

v0.43 完成了可独立运行的 stdio MCP Client。下一课会把已冻结的外部工具目录转换为 Agent 可以请求的工具，并让这些调用经过 Agent 自己的授权与执行流程。

固定代码索引：[client.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.43/src/mini_agent/mcp/client.py)、[stdio.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.43/src/mini_agent/mcp/stdio.py)、[protocol.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.43/src/mini_agent/mcp/protocol.py)、[mcp/__main__.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.43/src/mini_agent/mcp/__main__.py)。
