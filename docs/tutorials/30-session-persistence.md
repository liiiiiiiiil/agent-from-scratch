# 第 30 课：会话持久化与安全点（v0.30）

上一课：[驱动等待输入的后台进程](29-interactive-process.md) · [教程总览](README.md) · 下一课：[v0.31 安全恢复](31-safe-resume.md)

> 代码快照：`v0.30` · 相邻差异：`v0.29..v0.30` · 命令环境：Bash/zsh

> 运行要求：Python 3.10+。本课只讲本地 session 的保存与校验；`--resume` 不属于 v0.30。

## 本课目标

完成本课后，你应能解释三件事：为什么 State 快照不等于可恢复会话；一个完整安全点需要满足什么条件；为什么 session 写入必须使用脱敏、校验和原子替换。你还可以在 CLI 中用 `/save` 显式开启保存，并从磁盘检查生成的 `active` 或 `clean` 文件。

## 上一版的问题

v0.29 已经把任务事实放在 `AgentState`，把消息历史交给 `ContextManager`，并把后台进程句柄留在当前 CLI 进程。`AgentState.snapshot()` 很适合给模型看或做 Trace 只读回放，但它只是一份投影视图：里面没有完整的消息历史、历史摘要、私有预算计数、原始恢复参数，也没有办法重建下一个进程所需的运行时对象。

因此，“把 snapshot 写成 JSON”并不能安全地继续任务。它可能漏掉计划或修复计数，也可能保存半个工具回合；如果进程或 stdin 仍在运行，文件还会把一个不稳定的瞬间伪装成完成状态。本版先建立可验证的保存表示，跨进程真正继续任务留到下一课。

## 前置条件与版本切换

需要基础 Python 和命令行能力，并先读完第 29 课。下面的命令使用 Bash/zsh；它们用于查看相邻版本的真实差异。v0.30 的 Git tag 由仓库维护者在交付后手动创建，助手不会代为创建或推送 tag。

```bash
git checkout v0.29
git diff --stat v0.29..v0.30
git diff v0.29..v0.30 -- src/mini_agent/session.py src/mini_agent/state.py src/mini_agent/context.py src/mini_agent/__main__.py
git checkout v0.30
```

## 新增与改动文件

先用 `git diff --stat v0.29..v0.30` 确认版本范围；下面只列本课主线直接涉及的文件。

| 文件 | 变化 | 作用 |
|---|---|---|
| `src/mini_agent/state.py` | 新增显式 session 导出与引用校验 | 保存权威任务事实、私有计数和检查点元数据 |
| `src/mini_agent/context.py` | 新增历史导出、配对校验和 stdin 参数脱敏 | 保存协议上下文，不保存 protected system prompt |
| `src/mini_agent/session.py` | 新增 `SessionStore` | 负责版本、大小、权限、锁、哈希和原子文件提交 |
| `src/mini_agent/__main__.py` | 新增 `/save` 和生命周期保存 | 在完整安全点提交 `active`，清理后提交 `clean` |
| `src/mini_agent/config.py` | 集中定义 16 MiB 上限 | 限制单个 session 文件增长 |

## 版本变更定位

下面的图把“上一版已有的运行链”与“本版插入的保存边界”分开。理解它的关键是：保存发生在 agent loop 返回之后，而不是 handler 仍在运行时。

```text
[旧] 上一版：
CLI 输入 -> run_task() -> agent_loop()
                         -> assistant tool_calls
                         -> handler / State 事实 / role=tool
                         -> 完整回合返回 CLI
                         -> 进程清理

[~][+] v0.30：
CLI 输入 -> run_task() -> agent_loop()
                         -> 完整 assistant/tool 回合
                         -> ContextManager.export_session()
                         -> AgentState.export_session()
                         -> SessionStore: canonical JSON -> SHA-256 -> 原子替换
                         -> active session

退出、/new、/reset -> [旧] 有界进程清理 -> [新增] clean session
                                  |
                                  +-> 清理失败 / 保存失败 -> 保留 active 或上一份完整文件

[B] v0.30 读取文件只做校验；不调用 LLM，不重建 State，不执行工具。
```

`AgentState.snapshot()` 仍然供 Structured State 和 Trace 使用；`export_session()` 是另一条更严格的保存协议。两者不能互相替代。

## 核心概念与数据结构

### 1. 安全点不是“当前内存看起来完整”

安全点是一个可以被明确描述的时刻：当前 assistant 的每个 tool call 都有按序的 `role=tool` 结果；没有正在运行的 handler、没有预留但未结算的 attempt；没有活动后台进程或在途 stdin；进程清理事实已经进入 State。这样保存下来的文件才至少代表一个完整协议回合。

在 State 中，`_pending_attempts`、活动 `ProcessRecord` 和未提交的进程控制事件会让导出失败。这个失败不是把任务标记为“已保存”，而是把原因交给 CLI。

### 2. 保存格式与完整性

Session 文件是一个固定 `schema_version=1` 的 JSON envelope。它包含随机 `session_id`、包版本、规范化工作区根目录、保存时间、`save_kind`、`handoff_status`、State、Context 和 `integrity.sha256`。哈希覆盖的是“删除完整性字段后的规范化 JSON”，因此读取时可以先重算再验证内部引用。

```python
payload_without_integrity = canonical_json(envelope_without_integrity)
digest = hashlib.sha256(payload_without_integrity).hexdigest()
envelope["integrity"] = {"algorithm": "sha256", "sha256": digest}
```

这段逻辑解决的是意外损坏和截断检测，不是防恶意篡改的认证。未知 schema、非法引用、tool-call/result 断链或超出 16 MiB 的文件都会被拒绝。

### 3. Context 导出必须先处理隐私和协议

Context 的 protected system messages 由当前项目指令在未来恢复时重新构造，所以 v0.30 不把它们写入导出。普通 history、摘要、压缩状态和待消费 Runtime Notice 会保留。

`write_process.input` 是一个专门的边界：assistant 消息仍保留原来的 `tool_call_id` 和参数 JSON 形状，但 `input` 的值变成 `<redacted:write_process.input>`。因此读取者能看出调用结构，却不能把原文误当作可重放输入。如果非空正文还出现在普通历史文本、摘要或 Runtime Notice，本次保存会拒绝，并且错误不显示正文；空输入用于发送 EOF 时不改写摘要。

## 为什么这样设计

本版选择一个标准库 JSON 文件，而不是 `pickle` 或通用数据库。JSON 易于检查，拒绝可执行反序列化；同目录临时文件配合 `fsync` 和 `os.replace`，可以让中断写入留下旧的完整提交。独占锁只用创建标记实现，遗留锁不自动抢占，因为“锁很旧”不能证明另一个 CLI 已经退出。

代价是 session 仍可能包含普通对话、文件内容和工具输出，用户必须把它当成本地敏感数据；16 MiB 上限也意味着不能靠截断半个工具回合来压缩文件。本版只校验文件，不尝试从它恢复运行时对象，因而不能跨进程继续任务。

## 设计边界

支持的路径是：输入 `/save`，获得一个 session ID；之后完整 agent 回合自动更新 `active`；正常退出、`/new` 或 `/reset` 先清理后台资源，再提交 `clean`。替换前写入失败时旧文件继续可读；替换后若目录同步或锁清理失败，CLI 报告提交状态未确认和 session ID，不能假设仍是旧文件。如果清理失败，旧会话保持 `active`，CLI 报告进程 ID、PID 或 stdin 原因。

不支持的路径是：在活动进程、在途 stdin、半轮 tool result 或未结算 attempt 中保存；从 v0.30 文件直接继续任务；跨进程重连 `Popen`、管道、线程、权限批准或检查点前镜像字节。`active` 只可用于诊断最后一次安全点，下一课才会定义只接受 `clean` 的恢复入口。

## 关键流程

```text
/save
  -> 检查有活动任务
  -> 同步进程事实
  -> 校验 State 安全点与 Context 配对
  -> 生成 envelope、计算 SHA-256
  -> 获取 session.lock
  -> 同目录临时文件 + fsync + os.replace
  -> 显示 session_id

替换前保存失败
  -> 临时文件清理，旧 .json 不覆盖
  -> CLI 显示失败原因，不显示“已保存”

替换后目录同步或锁清理失败
  -> 磁盘文件可能已更新，持久性尚未确认
  -> CLI 显示 session ID 与检查提示，不宣称旧文件仍在
```

运行 CLI 后输入 `/save`，应看到随机 session ID；再次输入 `/save`，应看到“已更新会话”。退出后打开 `~/.mini_agent/sessions/<session_id>.json`，应看到 `handoff_status` 为 `clean`。如果在进程仍运行或 stdin 写入在途时保存，预期是拒绝保存，而不是创建一个可继续的快照。

## 实现拆解

`SessionStore` 只接收显式的 State/Context 导出，不读取 `config_local.py`，也不接受模型指定的文件路径。写入使用当前用户私有的 `~/.mini_agent/sessions/`；读取先检查文件大小，再解析 JSON、验证版本和哈希，最后检查 State/Context 引用。

State 导出保留 `PlanRevision`、进度事件、generation、attempt、failure、recovery、verification history、预算计数、修复阶段、原始恢复参数和检查点元数据。元组键计数字典被转换为排序后的 JSON 列表；锁、ProcessManager 和检查点字节不在导出中。

关键实现索引：

- [`src/mini_agent/session.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.30/src/mini_agent/session.py)：envelope、锁、哈希、大小检查与原子提交。
- [`src/mini_agent/state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.30/src/mini_agent/state.py)：权威 State 导出、安全点和引用校验。
- [`src/mini_agent/context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.30/src/mini_agent/context.py)：history 配对校验与 `write_process.input` 脱敏。
- [`src/mini_agent/__main__.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.30/src/mini_agent/__main__.py)：`/save`、自动保存和 clean 交接顺序。

## 运行与观察

下面命令使用 Bash/zsh。命令行首条任务处理完后，程序仍会进入交互循环。

```bash
PYTHONPATH=src python -m mini_agent "检查一个小改动"
```

在提示符输入 `/save`，观察随机 ID；继续完成一轮任务后，session 文件的保存时间会更新。输入 `exit` 后，若后台进程已经清理完成，`handoff_status` 会变为 `clean`。这些现象说明保存动作位于完整回合和生命周期清理之后，而不是说明新进程已经具备恢复能力。

## 本版特性、下一课与代码索引

本版新增的是“可验证、可原子保存的本地 session”，并保持现有 `snapshot()`、Trace、Plan Contract、PermissionGate 和 agent loop 语义不变。下一课 v0.31 将讨论如何显式加载 `clean` 会话、重新发现项目指令并让旧验证失效；本课不提前实现这些行为。

核心代码索引：[`session.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.30/src/mini_agent/session.py)、[`state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.30/src/mini_agent/state.py)、[`context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.30/src/mini_agent/context.py)、[`__main__.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.30/src/mini_agent/__main__.py)。
