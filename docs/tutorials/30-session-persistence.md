# 第 30 课：把 Agent 会话保存到磁盘（v0.30）

上一课：[驱动等待输入的后台进程](29-interactive-process.md) · [教程总览](README.md) · 下一课：[从完整安全点恢复会话](31-safe-resume.md)

> 代码快照：`v0.30` · 相邻差异：`v0.29..v0.30` · 命令环境：Bash/zsh

> 运行要求：Python 3.10+。本课只讲本地 session 的保存和校验；v0.30 还不能用 `--resume` 继续任务。

## 本课目标

当 Agent 正在工作时，任务事实和对话都在内存里。程序一旦退出，下一次启动就不知道做到哪一步了。本课先解决“怎样留下一个可信的记录”，但不急着解决“怎样从记录继续运行”。

读完本课，你应该能说清楚：

- `State`、`Context` 和 session 各自保存什么；
- 什么叫安全点（safe point），为什么半个工具回合不能保存；
- `/save` 为什么只在安全点写入，以及 `active` 和 `clean` 的区别；
- JSON、SHA-256、原子替换和脱敏分别解决什么问题。

## 上一版的问题

v0.29 已经能完成一次 Agent 回合：模型提出工具调用，程序执行工具，把 `role=tool` 结果送回模型。问题是这些信息主要存在当前 Python 进程的内存里。只保存 `AgentState.snapshot()` 也不够，因为它是给模型和 Trace 阅读的状态投影，不包含完整消息历史、摘要和恢复所需的内部计数。

所以本版建立一种单独的保存格式：session。它是一个放在用户私有目录中的 JSON 文件，里面同时保存权威任务事实和协议上下文。v0.30 只保证“这个文件是经过检查的完整记录”；跨进程把它变成新的运行时，留到下一课。

## 前置条件与版本切换

需要基础 Python、命令行和第 29 课的工具调用概念。下面的命令使用 Bash/zsh。切换 tag 会改变当前工作树；学习结束后，请按自己的 Git 工作流切回开发分支。tag 的创建、移动和推送由仓库维护者手动完成。

```bash
git checkout v0.29
git diff --stat v0.29..v0.30
git diff v0.29..v0.30 -- src/mini_agent/session.py src/mini_agent/state.py src/mini_agent/context.py src/mini_agent/__main__.py src/mini_agent/config.py
git checkout v0.30
```

## 新增与改动文件

先看 `git diff --stat`，确认本课的变化确实集中在“导出、校验、写入和生命周期”这条链上。文件名不是学习重点，后文会按使用顺序解释它们。

| 文件 | 变化 | 作用 |
|---|---|---|
| `src/mini_agent/state.py` | 增加 session 导出和安全点检查 | 保存任务事实、计数和检查点元数据 |
| `src/mini_agent/context.py` | 增加历史导出、配对校验和输入脱敏 | 保存对话协议，不保存受保护 system prompt |
| `src/mini_agent/session.py` | 增加 `SessionStore` | 负责 JSON envelope、锁、大小限制、哈希和原子写入 |
| `src/mini_agent/__main__.py` | 增加 `/save` 和退出时保存 | 在完整回合后写 `active`，清理完成后写 `clean` |
| `src/mini_agent/config.py` | 集中定义 session 文件上限 | 默认限制单个文件为 16 MiB |

## 版本变更定位

图例：`[旧]` v0.29 已有，`[+]` v0.30 新增，`[~]` v0.30 修改，`[C]` 主要消费者，`[B]` 本课边界。

先看旧流程。它能把工具结果交回模型，但没有可供下一次进程读取的完整保存格式：

```text
[旧] CLI 输入
  -> [旧] agent_loop
  -> [旧] 工具 handler
  -> [旧] State 事实 + role=tool 消息
  -> [旧] 回合结束，回到 CLI
  [B] 退出后没有跨进程恢复入口
```

本版在“完整回合结束”和“任务边界清理”处加入保存：

```text
[旧] CLI -> agent_loop -> 工具 handler -> State + role=tool
                                      |
                                      v
                              [+] 导出 State / Context
                                      |
                                      v
                              [+] SessionStore
                                  JSON -> SHA-256 -> 原子替换
                                      |
                                      v
                              [C] active session 文件

退出、/new、/reset
  -> [旧] 有界清理后台资源
  -> [+] 清理事实进入 State
  -> [+] 保存 clean session

[B] 活动 handler、未结算 attempt、活动进程或在途 stdin
  -> 拒绝保存，不制造“看起来完整”的文件
[B] v0.30 读取 session 只做校验和诊断，不重建运行时、不调用 LLM
```

## 核心概念与数据结构

### 1. 三种“状态”不是同一份东西

先把三个容易混淆的词分开：

- `State` 是任务事实，例如任务状态、计划进度、工具执行记录、预算和验证记录；
- `Context` 是送给模型的对话材料，例如 user、assistant、tool 消息和摘要；
- session 是为了保存/恢复而设计的文件格式，它把前两者连同恢复所需的内部数据放进一个 envelope（外层 JSON 容器）。

`snapshot()` 仍然保留，因为模型和 Trace 需要一个有界、易读的投影。session 导出则必须更严格：它要保留消息的 tool-call/result 配对、私有计数和引用关系。把 snapshot 直接写成 JSON，会漏掉下一进程所需的信息。

保存接口因此分别导出两类数据，再由 `SessionStore` 组合它们；这不是把一个临时 snapshot 原样写到磁盘：

```python
state_export = state.export_session()
context_export = context.export_session()
```

### 2. 安全点表示“可以停在这里”

安全点不是“内存里现在有一堆字段”，而是一个明确的协议边界。至少要同时满足：本轮每个 assistant tool call 都有对应的 `role=tool` 结果；State 和 Context 一起提交；没有正在运行的 handler、未结算的 attempt、活动后台进程或在途 stdin 写入。

这里的 handler 指真正执行工具动作的 Python 函数，attempt 指一次被 State 记账的执行尝试。只要其中一个仍未收束，程序就不知道文件应该描述动作之前还是动作之后，因此保存会失败，并把原因交给 CLI。

正常路径是：工具回合完整返回后自动更新 `active`；输入 `/save` 时再次保存当前安全点；退出、`/new` 或 `/reset` 时先清理进程，再尝试写 `clean`。`active` 表示“最后一次安全点存在，但任务交接未被证明正常完成”，`clean` 才表示可以作为完整交接记录。

例如退出交接最终请求的是同一个安全点格式，只改变交接状态：

```python
store.save(session_id, state, context,
           handoff_status="clean", save_kind="safe_point")
```

### 3. JSON 文件如何避免“写坏旧记录”

session 顶层包含 `schema_version=1`、随机 `session_id`、工作区根路径、保存时间、`save_kind`、`handoff_status`、State、Context 和完整性字段。`session_id` 标识磁盘文件，不等于任务内部的 `task_id`。

写入大致分为四步：生成规范化 JSON、计算 SHA-256、写入同目录临时文件并 `fsync`、用 `os.replace` 替换正式文件。下面是 v0.30 规范化字节的关键代码；排序和固定分隔符让同一内容得到同一哈希：

```python
def _canonical_bytes(payload):
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
```

读取时会先检查文件大小，再解析 JSON、验证版本、重算哈希和检查 State/Context 的引用。哈希能发现截断或意外损坏，但不是防恶意篡改的密码学认证；session 仍应被当作本地敏感数据。

### 4. 保存格式同时处理隐私

模型消息、文件内容和工具输出可能含有敏感信息，因此 session 放在 `~/.mini_agent/sessions/`，单文件默认上限为 16 MiB。`write_process.input` 是特殊边界：历史中保留工具调用的结构和 `tool_call_id`，但输入正文替换成占位符；非空正文如果出现在普通历史、摘要或 Runtime Notice 中，保存会拒绝。

这说明“脱敏”不是把所有消息都删掉，而是在仍能检查调用配对的前提下移除不能进入持久化文件的正文。

## 为什么这样设计

本版选择标准库 JSON，而不是 `pickle` 或通用数据库。JSON 可以人工检查，也不会在读取时执行任意 Python 对象；同目录临时文件、`fsync` 和原子替换可以让中断写入尽量留下上一份完整提交。独占锁拒绝并发写入；遗留锁不会因为“看起来很旧”就自动抢占，因为它可能仍属于另一个 CLI。

代价也很明确：session 可能包含对话、文件片段和工具输出，不能随便分享；16 MiB 上限意味着不能通过截断半个工具回合来省空间；普通 SHA-256 不提供身份认证；最重要的是，本版还不能从文件直接继续任务。

## 设计边界

支持的路径是：有活动任务时输入 `/save`，首次得到随机 session ID；之后同一任务的安全点更新同一个文件；正常退出时，清理完成后把它标记为 `clean`。替换前写入失败时，旧文件不被覆盖；替换后目录同步或锁清理失败时，CLI 会报告“提交状态未确认”和 session ID，不能武断地说新旧哪一份已经持久化。

不支持的路径是：在活动进程、在途 stdin、半轮 tool result 或未结算 attempt 中保存；从 v0.30 文件启动下一轮 LLM；跨进程重连 `Popen`、管道、线程、权限批准或 checkpoint 前镜像字节。`active` 只能用于诊断最后安全点，下一课才定义安全加载入口。

## 关键流程

```text
/save
  -> 确认当前有活动任务
  -> 同步进程事实
  -> 检查 State / Context 是否是安全点
  -> 生成 envelope 和 SHA-256
  -> 获取 session.lock
  -> 临时文件写入、fsync、原子替换
  -> 显示 session_id

发现 handler / attempt / 进程 / stdin 仍未收束
  -> 保存失败
  -> 旧 session 保持可读
  -> CLI 不显示“已保存”

正常 exit、/new 或 /reset
  -> 有界清理后台资源
  -> 清理失败：保留 active，并报告具体进程或 stdin 原因
  -> 清理成功：写入 clean
```

## 实现拆解

`SessionStore` 只接收显式的 State/Context 导出，不让模型指定保存路径，也不读取 `config_local.py`。`AgentState.export_session()` 负责导出权威事实和安全点判断；`ContextManager.export_session()` 负责消息配对、摘要和脱敏。锁、`ProcessManager`、handler 和线程属于当前进程资源，不会被序列化。

关键实现索引：

- [`src/mini_agent/session.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.30/src/mini_agent/session.py)：envelope、锁、大小检查、哈希和原子提交；
- [`src/mini_agent/state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.30/src/mini_agent/state.py)：权威 State 导出和安全点检查；
- [`src/mini_agent/context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.30/src/mini_agent/context.py)：历史导出、消息配对和 `write_process.input` 脱敏；
- [`src/mini_agent/__main__.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.30/src/mini_agent/__main__.py)：`/save`、自动保存和 clean 交接顺序。

## 运行与观察

下面的命令使用 Bash/zsh。命令行首条任务执行完后，程序仍会进入交互循环；它不会因为参数任务结束就立即退出。

```bash
PYTHONPATH=src python -m mini_agent "检查一个小改动"
```

在提示符输入 `/save`，观察终端显示随机 session ID；再完成一轮任务并再次输入 `/save`，同一个 ID 的文件会更新。输入 `exit` 后，如果后台资源清理成功，最后的 session 会是 `handoff_status: clean`。这些现象证明“完整回合和生命周期清理之后可以安全落盘”，不证明 v0.30 已经可以恢复运行。

## 本版特性、下一课与代码索引

本版新增的是可验证、可原子保存的本地 session：它保存 State、Context 和安全点元数据，保护敏感输入，并区分 `active` 与 `clean`。下一课 v0.31 会在新进程中加载 `clean` 安全点，重新检查工作区、重建运行时，并让旧验证失效；本课不提前实现这些行为。

核心代码索引：[`session.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.30/src/mini_agent/session.py)、[`state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.30/src/mini_agent/state.py)、[`context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.30/src/mini_agent/context.py)、[`__main__.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.30/src/mini_agent/__main__.py)。
