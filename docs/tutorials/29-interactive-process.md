# 第 29 课：驱动等待输入的后台进程

上一课：[控制后台进程](28-process-control.md) · [教程总览](README.md)

代码快照：`v0.29` · 相邻差异：`v0.28..v0.29` · 示例命令环境：Bash/zsh

`v0.29` tag 由维护者手动创建。tag 尚未创建时，可在当前开发分支阅读本课；固定源码链接和跨版本 diff 须在 tag 创建后核对。命令行首条任务完成后，CLI 仍会进入交互循环。

## 本课目标

前两课已经让 Agent 启动后台程序、读取输出并在必要时结束程序，但后台程序一直拿不到可写的 stdin。这个默认行为适合开发服务器和文件监听器：它们不需要输入，并且可以立即看到 EOF。它不适合下面这种程序：

```text
打印 ready
等待用户输入一行
打印 reply
继续等待 EOF
退出
```

如果 Agent 只看到 `status=running`，却不能发送那一行文本，就无法驱动这个程序继续执行。本课增加的是**有界管道输入**：只有启动时明确选择 `stdin_mode="pipe"`，后续的 `write_process` 才能发送少量 UTF-8 文本。

读完后，你应该能回答这些问题：

- 为什么默认仍然是 `stdin_mode="closed"`，而不是所有进程都打开管道？
- 为什么一行输入通常要显式带 `\n`，而 `close_stdin=true` 表示 EOF？
- 为什么上限按 UTF-8 编码后的字节计算，而不是按 Python 字符数计算？
- `written`、`closed`、`write_pending` 和 `error` 分别允许下一步做什么？
- 为什么写入成功后仍要调用 `read_process` 或 `wait_process`，并在进程退出后重新 verification？

本课实现的是文本管道，不是终端模拟。密码提示、Ctrl-C、终端回显、全屏界面和终端尺寸都不在这个能力内。

## 上一版的问题

`v0.28` 启动后台进程时把 stdin 连接到 `DEVNULL`。子进程看到的是立即到达的 EOF，这能避免无人写入的进程永远等待读取，却也意味着 Agent 后面没有输入入口。观察工具可以发现它在等待，控制工具可以结束它，但两者都不能把数据送进去。

把所有 stdin 改成管道也不是安全的默认值。一个本来不需要输入的命令可能因为一直等不到 EOF 而长期占用任务；管道写入还可能因为子进程不读取而阻塞。于是本版把两个决定拆开：启动时选择能力，后续调用单独写入，并为每次写入设定大小和等待上限。

## 前置条件与版本切换

需要基础 Python、Bash/zsh，以及第 28 课介绍的任务专属 `process_id`、PermissionGate、possible effect、退出事件和独立 verification。下面先查看本版相对控制版的真实插入点，再切换代码：

```bash
git checkout v0.28
git diff --stat v0.28..v0.29
git diff v0.28..v0.29 -- src/mini_agent/processes.py src/mini_agent/tools/process.py src/mini_agent/tools/base.py src/mini_agent/state.py src/mini_agent/trace.py src/mini_agent/agent.py src/mini_agent/output.py
git checkout v0.29
```

没有 `v0.29` tag 时，留在当前开发分支运行本课命令即可；固定版本的 diff 和源码链接等 tag 创建后再核对。运行示例不需要第三方依赖，命令统一使用 `PYTHONPATH=src`。

## 新增与改动文件

这些文件共同完成“开启能力 → 写入 → 观察 → 收口”这条链。文件名本身不是学习目标，后文会按这条链解释它们各自为什么存在。

| 文件 | 本课作用 |
| --- | --- |
| `src/mini_agent/processes.py` | 启动时在 `DEVNULL` 和 `PIPE` 间选择；管理一次写入、2 秒有界等待和清理。 |
| `src/mini_agent/tools/process.py` | 扩展 `start_process` 的 `stdin_mode`，注册 `write_process`，并校验 UTF-8 字节上限。 |
| `src/mini_agent/tools/base.py`、`src/mini_agent/permission.py` | 在写入前执行归属、阶段、权限和在途状态检查；写入默认作为可能有副作用的动作处理。 |
| `src/mini_agent/state.py`、`src/mini_agent/trace.py` | 保存 stdin 能力和安全元数据，保留脱敏 attempt 与退出因果，不保存输入正文。 |
| `src/mini_agent/prompt.py`、`src/mini_agent/context.py`、`src/mini_agent/output.py` | 告诉模型如何开启管道和发送 EOF；展示状态与字节数，隐藏正文。 |
| `src/mini_agent/agent.py`、`src/mini_agent/__main__.py`、`src/mini_agent/recovery.py` | 在途写入阻止完成；交接、恢复和任务边界清理时有界收束写入线程。 |

## 版本变更定位

图例：`[旧]` 上一版已有，`[+]` 本版新增，`[~]` 本版修改，`[C]` 主要消费者，`[B]` 本版边界。

`v0.28` 的输入路径在启动时就结束：

```text
[旧] start_process(command, cwd?)
       -> ProcessManager: stdin=DEVNULL
       -> 子进程立即看到 EOF
       -> get/read/wait 观察 stdout/stderr 和退出
       -> terminate/kill 或任务边界清理
[B] Agent 没有向进程写入文本的工具
```

`v0.29` 把能力开关和写入动作加入同一条任务边界：

```text
[+] start_process(command, cwd?, stdin_mode="closed" | "pipe")
       -> [~] closed: DEVNULL；pipe: PIPE + stdin_state=open
       -> [C] write_process(process_id, input, close_stdin?)
             -> 归属 / 阶段 / 权限 / stdin 状态检查
             -> UTF-8 编码后最多 4096 字节
             -> 专用写入线程，工具回合最多等待 2 秒
             +-> written：字节已写入，管道仍 open
             +-> closed：写入后关闭 stdin，即发送 EOF
             +-> write_pending：仍在途，不能重试
             +-> error：不确定或未成功交付，按错误原因处理
       -> [C] read_process / wait_process 观察进程响应
       -> 进程退出 -> ProcessEvent -> 后继 generation -> 独立 verification
[B] 只有管道字节流；没有 PTY、回显、控制字符或终端尺寸语义
[B] 输入正文不进入 State、Trace、结果、授权提示或终端输出
```

注意图中的 `write_pending`：它不是“写入失败”，也不是“已经写入”。工具已经停止等待，但后台写入线程还没有给出最终结果。任务必须保留这条在途资源，直到状态查询或任务清理确认它收束。

## 关键流程：stdin 有“能力”和“状态”两层

`stdin_mode` 说的是启动时有没有创建可写管道，是一个能力选择；`stdin_state` 说的是这条管道此刻能不能继续写，是运行状态。两者不能混为一谈：

| 启动方式/运行状态 | 含义 | `write_process` |
| --- | --- | --- |
| `stdin_mode=closed`，`stdin_state=disabled` | 没有为该进程开放写入能力，子进程得到 EOF | 返回 `stdin_not_enabled` |
| `pipe`，`open` | 可以开始一笔写入 | 接受不超过上限的文本 |
| `write_pending` | 前一笔写入或 flush 仍在途 | 不接受下一笔 |
| `closed` | 已发送 EOF，stdin 不能再打开 | 返回 `stdin_closed`，或进程已退出 |
| `error` | 管道出现 Broken Pipe 或其他写入错误 | 返回已记录的错误 |

这里的 EOF（End of File）不是一段特殊字符串，而是“输入流已经结束”的操作系统状态。程序调用 `readline()` 通常先需要换行符；程序调用 `read()` 读到 EOF 才会返回。因此发送 `hello\n` 和关闭 stdin 是两个不同动作。

## 核心概念一：启动时显式开启管道

### 默认关闭解决什么问题

`start_process` 的 `stdin_mode` 默认是 `closed`。实现会把 stdin 连接到 `subprocess.DEVNULL`，保留此前版本“子进程立即看到 EOF”的行为。旧的服务器启动命令不需要因为升级而突然开始等待输入；不需要输入的命令也不会多出一条无人消费的管道。

只有明确知道程序需要少量输入时，才这样启动：

```json
{
  "command": "python -u -c 'import sys; print(\"ready\", flush=True); line=sys.stdin.readline().rstrip(\"\\n\"); print(\"reply:\" + line, flush=True); sys.stdin.read(); print(\"eof\", flush=True)'",
  "stdin_mode": "pipe"
}
```

启动结果会告诉模型能力是否打开：

```json
{
  "process_id": "proc-1",
  "status": "running",
  "stdin_mode": "pipe"
}
```

这个小程序会先输出 `ready`，等一行输入；收到换行后输出 `reply:...`，然后继续等 EOF，最后输出 `eof` 并退出。命令中的 `-u` 和 `flush=True` 只是为了让观察工具及时看到输出，不改变 stdin 协议。

### 对应的实现选择

下面是 `v0.29` 中启动参数的关键部分。它把旧行为保留在默认分支，把新能力限制在显式的 `pipe` 分支：

```python
kwargs = {
    "shell": True,
    "cwd": resolved_cwd,
    "stdin": subprocess.PIPE if stdin_mode == "pipe" else subprocess.DEVNULL,
    "stdout": subprocess.PIPE,
    "stderr": subprocess.PIPE,
}
```

读这段代码时抓住一个结论就够了：是否可以写 stdin 在 `Popen` 创建时已经决定，不能等进程启动后再把一个关闭的 stdin 变成管道。

## 核心概念二：一次写入先按字节验证，再有限等待

### 为什么按 UTF-8 字节限制

工具接收的是 Python 字符串，但真正写入的是 UTF-8 字节。ASCII 字符通常一个字符一个字节；中文等字符通常占三个或更多字节。如果只按字符数限制，`4096` 个字符可能超过管道和协议设计的 4096 字节上限。

因此本版先编码再检查：

```python
encoded = input_text.encode("utf-8")
if len(encoded) > MAX_STDIN_BYTES:
    raise ValueError("input UTF-8 编码后不能超过 4096 字节")
if not encoded and not close_stdin:
    raise ValueError("input 为空时必须设置 close_stdin=true")
```

一次写入最多 4096 个 UTF-8 字节。空文本只有在 `close_stdin=true` 时才有意义，因为它代表“这次不发送正文，只发送 EOF”。写入工具不会自动添加换行；需要 `readline()` 的程序必须由调用者把 `\n` 放进 `input`。

### 为什么需要写入线程和 2 秒上限

管道的另一端可能暂时不读取。若工具 handler 直接在主线程调用 `write` 和 `flush`，一次写入就可能把整个 Agent 回合卡住。Manager 为每笔写入创建专用线程，主工具回合最多等待 2 秒：

```text
开始写入
  -> 2 秒内完成 write + flush -> 返回 written 或 closed
  -> 2 秒内没有确认       -> 返回 write_pending
                                写入线程仍由 Manager 管理
```

`write_pending` 要按“结果未知”来处理。不能因为工具回合返回了就再次发送同一段文本：第一笔可能已经送达，重复发送会改变子进程收到的内容。下一次 `get_process`、`read_process` 或 `wait_process` 会同步 `stdin_state`；任务边界清理也会有界回收这条写入线程。

一个进程同时最多有一笔在途写入。前一笔未收束时，下一次写入会得到明确的 `write_pending` 结果，不会并行写入同一条管道。

## 核心概念三：用结果状态驱动下一步

### 写入一行，再发送 EOF

沿用上面的等待输入程序，模型可以分成三步：

第一步，启动时显式开管道，并等待 `ready`：

```text
start_process(..., stdin_mode="pipe")
  -> read_process
  -> stdout: "ready\n", status: "running", stdin_state: "open"
```

第二步，发送带换行的一行文本，不关闭管道：

```json
{
  "process_id": "proc-1",
  "input": "hello\n",
  "close_stdin": false
}
```

成功时结果的关键字段类似：

```json
{
  "status": "written",
  "process_id": "proc-1",
  "written_bytes": 6,
  "closed": false,
  "stdin_state": "open",
  "write_pending": false
}
```

此时只证明 6 个字节已经完成写入和 flush。它不保证程序已经处理完输入，也不保证程序已经退出。下一步用 `read_process` 读取 `reply:hello\n`，或用 `wait_process` 等待新输出。

第三步，示例程序在打印 reply 后还会调用 `sys.stdin.read()`，所以需要单独发送 EOF：

```json
{
  "process_id": "proc-1",
  "input": "",
  "close_stdin": true
}
```

结果会是 `status=closed`、`written_bytes=0`、`stdin_state=closed`。之后再用 `read_process` 读取 `eof\n`，用 `get_process` 确认退出。也可以把正文和 EOF 放在同一次调用中：设置 `input="hello\n"` 且 `close_stdin=true`，此时结果是 `closed`，`written_bytes` 为正文的字节数。

### 错误结果不能自动重试

常见失败路径的含义如下：

| 结果 | 说明 | 应对方式 |
| --- | --- | --- |
| `stdin_not_enabled` | 启动时没有选择 `pipe` | 只能结束该进程并重新以 `pipe` 启动。 |
| `stdin_closed` | 已发送 EOF 或 stdin 已被关闭 | 不能重新打开；检查进程是否应已退出。 |
| `process_exited` | 写入前进程已经退出 | 先读取尾部输出和退出码。 |
| `write_pending` | 写入线程仍未给出最终结果 | 不重复发送；后续观察 `stdin_state`。 |
| `broken_pipe` | 子进程关闭了读端 | `written_bytes=0` 且非空输入会标记 `delivery_uncertain=true`，不要自动重试。 |

Broken Pipe 的特殊之处是：写入端无法可靠知道有多少字节在错误发生前已经到达。因此 `written_bytes=0` 并不等同于“对方绝对没有收到任何字节”；它表示没有确认完成的字节数。模型应把它当作一次执行错误，依据后续进程输出或用户决定处理。

## 核心概念四：正文只用于这次动作，不能污染审计状态

stdin 可能包含密码、token、用户数据或只在本次交互中有意义的文本。v0.29 仍然需要记录“发生过一次写入”，但不需要把正文复制到长期状态和诊断输出中。于是这些观察面只保留安全元数据：

```text
State / Trace        -> process_id、写入 attempt、字节数、脱敏参数、状态
权限提示             -> process_id、编码后字节数、close_stdin
工具结果             -> written_bytes、stdin_state、closed、错误类型
普通/debug 终端输出  -> input_bytes 等元数据，不显示正文
```

执行 attempt 的参数会保存脱敏形式，例如 `<str:...>`，用于判断重复调用和回放因果，而不是恢复输入内容。未知 ID、参数超限和用户拒绝也在写入前处理，不会把正文交给进程。

## 实现拆解：写入也要经过完整的 Agent 边界

`write_process` 是 `possible` effect，因为 stdin 可能让进程继续修改工作区或外部环境。执行器会先验证参数和 UTF-8 字节数，再检查当前任务是否拥有该 `process_id`，再经过 planning/repair gate 和 PermissionGate；只有这些检查通过，才进入 Manager 的写入 handler。Manager handler 还会再次检查归属和 stdin 状态，避免授权与执行之间的竞态。

因此下面几种情况都不会发送正文：

- 输入超过 4096 个 UTF-8 字节，或空输入没有配合 `close_stdin=true`。
- `process_id` 未知、过期或属于另一个任务。
- 当前处于 `exploring`、待批准计划、诊断或严格验证阶段。
- 用户拒绝 `write_process` 的独立权限请求。
- 进程没有启用 pipe、已经关闭 stdin，或上一笔写入仍在途。

写入尝试本身也不能作为 `run_shell(purpose="verification")`。如果写入导致进程退出，前台同步会追加退出事件、开启后继 generation 并清除旧 verification；如果写入没有导致退出，活动进程或在途写入仍然会阻止任务进入 `done`。

## 运行与观察

下面的命令适用于 Bash/zsh，用于运行本版的管道输入和脱敏边界场景：

```bash
PYTHONPATH=src python -m pytest -q tests/test_process_input_v029.py
```

运行时可以用下面的对应关系检查自己是否理解了结果：

| 观察到的现象 | 它证明了什么 |
| --- | --- |
| 默认启动的进程立即看到 EOF，显式 pipe 的进程可以接收文本 | 能力在启动时选择，默认行为保持兼容。 |
| `hello\n` 让 `readline()` 继续，而空输入加 `close_stdin=true` 让 `read()` 结束 | 换行和 EOF 是两个不同的输入协议。 |
| 中文输入按编码后字节数判断，恰好 4096 字节可通过，超出则参数无效 | 限制作用于真实写入的 UTF-8 字节。 |
| 写入线程故意变慢时返回 `write_pending`，随后状态才收束 | 工具回合有上限，Manager 继续负责在途资源。 |
| 输入中的唯一标记不出现在 State、Trace、授权提示和终端输出 | 审计保留动作元数据，不复制正文。 |
| Broken Pipe 返回 `delivery_uncertain=true` | 不能在无法确认交付时自动重试。 |
| 输入导致进程退出后旧 verification 被清除 | 写入结果不能代替退出后的独立验证。 |

测试文件使用标准库启动短生命周期子进程。它们的价值在于把并发和边界现象固定下来；实际学习时应先看每个结果的含义，再把它对应回 `ProcessManager`、`ToolExecutor` 和 `AgentState` 的职责。

## 为什么这样设计

默认关闭 stdin，是为了保持既有后台命令的 EOF 行为，并避免不需要输入的进程因为管道一直打开而等待。显式 `pipe` 把“我知道这个程序需要输入”的判断交给启动动作，代价是模型必须在启动时做对选择；已经以 `closed` 启动的进程不能事后补开管道。

把写入限制为 UTF-8 文本、4096 字节和单笔在途，是为了让输入、内存和工具结果都保持有界。专用线程加 2 秒等待避免主 Agent 回合被不读 stdin 的子进程卡住，代价是会出现 `write_pending`，模型必须等待最终状态，不能靠猜测重试。

使用管道而不是 PTY，能复用现有的 stdout/stderr 排空和任务清理边界，也能明确规定输入正文的脱敏范围。代价是管道没有终端语义：不会提供输入回显、Ctrl-C、信号控制字符、终端尺寸或全屏交互。

## 设计边界

- 只有 `stdin_mode="pipe"` 的进程接受 `write_process`；默认 `closed` 的进程得到 EOF，不能在运行中转换模式。
- 单次输入按 UTF-8 编码后最多 4096 字节；空输入只能与 `close_stdin=true` 一起发送 EOF；工具不会自动添加换行。
- `write_process` 默认按独立的 `possible` 动作处理；未知 ID、参数错误、阶段拒绝和权限拒绝不会写入或预留对应的写入副作用。
- 一次最多一笔在途写入；`write_pending` 不是已交付确认，后续不能重复发送同一正文。
- 写入结果不构成 verification；进程退出后必须在新的 generation 执行独立 `run_shell(purpose="verification")`。
- 输入正文不进入 State、Trace、工具结果、权限提示或终端输出；State 和 Trace 只保留脱敏参数与安全元数据。
- 任务切换、CLI 退出和异常路径会有界回收 stdin 写入线程和管道；无法确认收束时保留旧任务登记并报告原因。
- 本版不支持 PTY、终端回显、控制字符、终端尺寸、交互式 shell 语义或跨会话重连。

## 本版特性、下一课与代码索引

本课让 Agent 能启动一个显式开启管道的后台进程，发送有界 UTF-8 文本，单独发送 EOF，并在写入超时、重复写入、Broken Pipe 和敏感正文场景下保持可解释边界。后续若评估 PTY，需要另行定义回显、控制字符、终端尺寸、跨平台进程组和清理协议；本课不把管道输入描述成已经具备这些能力。

固定源码入口：

- [进程与 stdin 生命周期](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.29/src/mini_agent/processes.py)
- [进程工具与参数校验](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.29/src/mini_agent/tools/process.py)
- [执行器权限与在途检查](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.29/src/mini_agent/tools/base.py)
- [状态、generation 与脱敏](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.29/src/mini_agent/state.py)
- [Trace 校验](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.29/src/mini_agent/trace.py)
