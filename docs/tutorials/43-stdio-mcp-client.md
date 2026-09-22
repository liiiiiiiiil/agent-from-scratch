# 第 43 课：最小 stdio MCP Client

上一课：[具名本地资料](42-local-references.md) · [教程总览](README.md) · 下一课：v0.44 MCP Tool 接入 Runtime（规划中）

代码快照：`v0.43` · 相邻差异：`v0.42..v0.43`

本课示例命令使用 Bash/zsh。`v0.43` tag 由维护者在交付后固定；阅读者切换前请确认本地已有该 tag。

## 本课目标

第 42 课让 Agent 读取用户登记的本地资料，但它仍然不能连接一个遵守标准协议的外部服务。第 43 课先把问题缩小：启动一个用户在本地配置的 MCP Server，完成一次最小会话，然后由用户手动调用一个工具。

这里的 Client 是“发起连接和请求的一方”，Server 是“提供工具的一方”。两者通过标准输入和标准输出两根独立管道交换消息：Client 把 JSON-RPC 请求写给 Server 的 stdin，Server 把 JSON-RPC 响应写到 stdout；Server 的诊断信息写 stderr，由 Client 单独排空。

完成本课后，读者应能解释 MCP 生命周期的四个关键动作、JSON-RPC 请求 ID 如何配对、`tools/list` 为什么必须翻页，以及为什么本版的手动调用还不是 Agent 的授权机制。

## 前置条件

需要 Python 3.10+、基础 Python、Bash/zsh 和 Git 知识。本课的测试和 fixture 只使用标准库，不需要真实 LLM，也不访问网络。

查看版本变化时，可以执行：

```bash
git checkout v0.42
git diff --stat v0.42..v0.43
git checkout v0.43
```

第一条和第二条命令帮助你看到相邻版本的变化规模；第三条命令切到本课快照，使下面的实现和命令对应同一版本。阅读结束后回到原来的分支：

```bash
git checkout -
```

## 新增与改动文件

本版只新增一条独立的 MCP 演示路径。它不会把 MCP Tool 注册进父 Agent，因此不需要修改 `runtime.py`、`permission.py`、`state.py`、`session.py`、`tools/` 或已有的 `src/mini_agent/__main__.py`。

| 文件 | 作用 |
|---|---|
| [`mcp/protocol.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.43/src/mini_agent/mcp/protocol.py) | 构造和校验 JSON-RPC 请求、通知与响应，隐藏服务端原始错误正文。 |
| [`mcp/stdio.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.43/src/mini_agent/mcp/stdio.py) | 不经 shell 启动直接子进程，读取有界 stdout，排空 stderr，并在超时或失步后关闭连接。 |
| [`mcp/client.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.43/src/mini_agent/mcp/client.py) | 完成固定版本握手、分页列工具、冻结目录和一次串行工具调用。 |
| [`mcp/__main__.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.43/src/mini_agent/mcp/__main__.py) | 提供 `list` 和需要逐次确认的 `call` 演示命令。 |
| `config.py`、`config_example.py` | 增加本地 `MCP_SERVERS` 配置和 argv、cwd、environment 校验。 |
| `tests/fixtures/mcp_stdio_server.py` | 用标准库模拟成功、分页和失败场景的测试 Server。 |
| `tests/test_mcp_v043.py` | 覆盖协议配对、进程清理、CLI 确认和不发送调用的路径。 |

## 关键流程

先看一次完整会话，再看模块细节。`initialize` 是请求，所以带有 ID；`notifications/initialized` 是通知，没有 ID；后面的两个请求各自带新的 ID。

```text
Client                                      Server
  │── initialize(id=1, version=2025-11-25) ──>│
  │<─ response(id=1, capabilities.tools) ────│
  │── notifications/initialized ─────────────>│
  │── tools/list(id=2, cursor=空) ───────────>│
  │<─ response(id=2, tools, nextCursor) ──────│
  │── tools/list(id=3, cursor=nextCursor) ───>│
  │<─ response(id=3, tools) ──────────────────│
  │── tools/call(id=4, name, arguments) ─────>│
  │<─ response(id=4, isError, content) ───────│
  │                 关闭 stdin，回收子进程
```

JSON-RPC 是消息格式。请求 ID 让 Client 能确认响应属于哪一次请求；`result` 是成功响应，`error` 是 JSON-RPC 层失败，两者不能同时出现。MCP 的 `tools/call` 还可能返回 `result.isError=true`，这表示工具自己报告了业务失败，仍然是一个有效的 JSON-RPC `result`。

同一 Client 的请求通过锁串行发送。Server 发来的普通通知只记录最近 64 条；`notifications/tools/list_changed` 不会触发热刷新，后续列工具只返回已冻结的快照。Server 如果主动发起带 ID 的请求，本版会明确失败并关闭连接。

## 实现拆解

### 1. 固定握手版本

[`McpClient._connect()`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.43/src/mini_agent/mcp/client.py) 固定发送 `2025-11-25`、客户端名称和空 capabilities。返回版本必须完全相同，capabilities 必须包含 `tools`，然后才发送 initialized 通知。这里没有自动探测其他版本，也没有 fallback，因为不同生命周期的顺序和能力含义不能靠猜测拼接。

### 2. 用两根管道隔离协议和诊断

[`StdioTransport`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.43/src/mini_agent/mcp/stdio.py) 直接调用 `subprocess.Popen`，把配置的 argv 作为参数传入，`shell=False`。stdout 线程按 UTF-8 单行 JSON 读取，每条消息最多 1 MiB，等待队列最多 64 条；stderr 线程只保留末尾 16 KiB。这样 Server 写很多日志时不会填满 stderr 管道，也不会把日志误当作 JSON-RPC。

写入和读取都有截止时间。坏编码、坏 JSON、超大消息、EOF、写入失败和超时都会使连接失效；Client 不复用失步连接，也不自动重试可能产生副作用的 `tools/call`。关闭时先关闭 stdin，等待最多 2 秒，再终止、必要时强制结束并回收直接子进程；如果清理未完成，独立命令会报告失败并返回非零状态。

### 3. 把分页目录当作一次快照

`tools/list` 的每一页都必须是对象并包含工具数组。Client 继续请求 `nextCursor`，最多 16 页和 256 个工具；重复 cursor、重复名称、缺少名称或缺少对象形式的 `inputSchema` 都会拒绝整次目录。v0.43 不解释 JSON Schema 的语义，只保留它供后续版本使用。

调用时只能传冻结目录里的原始工具名，参数必须是 JSON object。返回结果仍是 MCP 的原始有界对象，不转换成项目已有的 `ExecutionResult`；这正是 v0.44 Adapter 需要负责的边界。

## 为什么这样设计

本版先把“能说 MCP”与“Agent 能执行外部 Tool”分开。一个独立 Client 可以验证协议顺序、分页、错误和子进程清理，而不会让一个还没有经过 PermissionGate 的远端工具进入模型工具列表。下一版如果接入 Runtime，仍需重新定义工具名、权限、副作用、持久边界和恢复行为，不能把本课的人工确认当成现成授权系统。

配置同样只提供连接来源：`MCP_SERVERS` 是列表，每项包含受限 alias、非空 argv、可选 cwd 和字符串环境映射。相对 cwd 以 `config_local.py` 所在目录为基准；配置导入不会启动 Server，只有独立命令选中 alias 时才会启动。真实命令、路径和环境值放在未跟踪的 `config_local.py`。

## 运行与观察

在未跟踪的 `src/mini_agent/config_local.py` 中加入一个本地 fixture 配置。下面的相对路径从 `src/mini_agent` 目录开始，适合在仓库根目录复现：

```python
MCP_SERVERS = [{
    "alias": "demo",
    "command": ["python", "../../tests/fixtures/mcp_stdio_server.py"],
    "cwd": ".",
    "environment": {},
}]
```

在终端执行：

```bash
PYTHONPATH=src python -m mini_agent.mcp demo list
PYTHONPATH=src python -m mini_agent.mcp demo call echo '{"text":"hello"}'
```

`list` 应列出 `echo`、`sum` 和 `third` 三个工具；fixture 把它们分成两页，所以 Client 必须发送两次 `tools/list`。`call` 会先完整显示 alias、原始工具名和参数，只有在交互终端输入 `yes` 或 `y` 后才发送 `tools/call`，结果中可以看到 `hello`。参数太长无法完整显示时会拒绝调用；把输入重定向、输入 EOF 或输入其他文字时，命令也会报告没有发送调用。

离线测试可以直接运行：

```bash
PYTHONPATH=src python -m pytest -q tests/test_mcp_v043.py
```

测试还会启动静默、坏编码、坏 JSON、超大 stdout、stderr 洪泛和提前退出的 fixture。观察这些测试全部快速结束，说明失败发生在有界等待和关闭路径，而不是无限等待。

## 设计边界与失败路径

- 只支持固定的 MCP `2025-11-25`，不做版本探测、modern lifecycle fallback、远程 HTTP、SSE、Resource、Prompt、sampling 或 Server 请求。
- 工具目录不会热刷新；收到 list changed 通知只作为普通通知记录。
- JSON-RPC `error` 会抛出 `McpRemoteError`；`isError=true` 仍作为有效工具结果返回。异常只携带错误类别、方法和服务端错误码，不显示原始正文、完整命令、环境变量或参数。
- 本版没有 Agent Runtime、Tool Registry、PermissionGate、State 或 session 集成。连接和 PID 只在演示命令进程内存在，不能通过 `/save` 恢复，也不能声称具备 v0.44 的授权和崩溃恢复合同。
- 手动确认只是 CLI 的交互保护。它没有替代 PermissionGate，也不会为未来的 Agent 自动记住授权。

## 本版特性、下一课与代码索引

v0.43 完成了一个可独立运行的最小 stdio MCP Client：固定生命周期、请求配对、完整分页、原始工具结果、有界 stdout/stderr、超时和直接子进程清理都已有离线测试。它把 MCP 连接的协议事实与 Agent 的工具治理明确分开。

下一课 v0.44 计划研究怎样把 MCP Tool 接到现有 ToolExecutor 和 PermissionGate；那一课会重新处理副作用、授权、持久工具边界、恢复和子代理隔离，不能直接把本课的 `call_tool` 当作 Agent Tool。

固定代码索引：[`client.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.43/src/mini_agent/mcp/client.py)、[`stdio.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.43/src/mini_agent/mcp/stdio.py)、[`protocol.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.43/src/mini_agent/mcp/protocol.py)、[`test_mcp_v043.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.43/tests/test_mcp_v043.py)。
