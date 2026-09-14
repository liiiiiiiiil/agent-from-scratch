# 第 29 课：驱动等待输入的后台进程

上一课：[控制后台进程](28-process-control.md) · [教程总览](README.md)

代码快照：`v0.29` · 相邻差异：`v0.28..v0.29` · 示例命令环境：Bash/zsh

`v0.29` tag 由维护者手动创建。tag 尚未创建时，在当前开发分支阅读本课；固定源码链接和跨版本 diff 须在创建 tag 后核对。命令行首条任务完成后，CLI 仍会进入交互循环。

## 本课目标

前两课已经让 Agent 启动后台程序、读取输出并在必要时结束程序，但程序的输入一直是 EOF。这个默认行为适合服务器和文件监听器，却不适合“启动后等待一行文本”的小程序：Agent 看得到进程还活着，却没有安全、可审计的方式把那一行送进去。

本课增加有界的管道 stdin。读完后应能解释：为什么必须在启动时显式选择 `stdin_mode="pipe"`，为什么 `close_stdin=true` 是 EOF，为什么一次写入最多 4096 字节且最多等待 2 秒，以及为什么写入完成后仍要观察进程并独立验证。

## 前置条件

需要基础 Python、Bash/zsh，以及[第 28 课](28-process-control.md)介绍的任务专属 `process_id`、权限和进程退出边界。先在仓库根目录查看相邻版本：

```bash
git checkout v0.28
git diff --stat v0.28..v0.29
git diff v0.28..v0.29 -- src/mini_agent/processes.py src/mini_agent/tools/process.py src/mini_agent/tools/base.py src/mini_agent/state.py
git checkout v0.29
```

如果 `v0.29` tag 尚未存在，留在当前开发分支；上面的 diff 命令等 tag 创建后再运行。运行示例不需要第三方依赖，未安装包时在命令前加 `PYTHONPATH=src`。

## 新增与改动文件

| 文件 | 本课作用 |
| --- | --- |
| `processes.py` | 在启动时选择 `DEVNULL` 或 `PIPE`，串行管理一次写入、2 秒等待和有界清理。 |
| `tools/process.py`、`tools/__init__.py` | 扩展 `start_process` schema，注册 `write_process` 并校验 UTF-8 字节上限。 |
| `tools/base.py`、`permission.py` | 在授权和 generation 预留前做归属、状态与参数检查；写入默认单独询问。 |
| `state.py`、`trace.py` | 保存 stdin 能力和状态等元数据，保留脱敏 attempt 与代际引用，不保存输入正文。 |
| `prompt.py`、`context.py`、`output.py` | 告诉模型如何开启管道、发送 EOF 和处理在途结果；上下文与调试输出只显示安全元数据。 |
| `recovery.py`、`agent.py`、`__main__.py` | 防止写入被恢复重放，并在任务交接、完成判断和清理时等待写入收束。 |

## 关键流程

v0.28 的后台进程只有一个输入结局：启动时连接 `DEVNULL`，子进程立即得到 EOF。它的调用链是：

```text
[旧] start_process(command, cwd?)
       -> ProcessManager: stdin=DEVNULL
       -> get/read/wait_process: 观察输出和退出
       -> terminate/kill 或任务清理
```

v0.29 把“是否需要输入”放到启动参数中，把真正写入单独放到后续工具调用：

```text
[+] start_process(command, cwd?, stdin_mode="pipe")
       -> [~] ProcessManager: stdin=PIPE，状态为 open
       -> [C] write_process(process_id, input, close_stdin?)
              -> UTF-8 编码，最多 4096 字节
              -> 独立 ask + possible generation
              -> 写入线程最多等待 2 秒
              -> written / closed / write_pending / error
       -> [C] read_process 或 wait_process 观察响应
       -> close_stdin=true 发送 EOF
       -> 进程退出后独立 verification
[B] 一次最多一个在途写入；正文不进入 State、Trace、结果、提示或终端
```

图例：`[旧]` 上一版已有，`[+]` 本版新增，`[~]` 本版修改，`[C]` 主要消费者，`[B]` 本版边界。

## 实现拆解

`start_process` 的 `stdin_mode` 默认是 `closed`。保持默认值可以让旧命令继续得到 EOF；只有显式传入 `pipe` 才创建管道。这个选择也让 Agent 在看到程序等待时能判断它是否真的具备输入能力，而不是把所有后台命令都变成可能永远等待 stdin 的进程。

`write_process` 接收 `process_id`、文本 `input` 和可选的 `close_stdin`。实现先把文本编码成 UTF-8，再按字节数检查上限，所以多字节字符会占用多个字节；输入为空时，只有 `close_stdin=true` 才合法。关闭动作与写入共用一次权限判断，默认是 `ask`，授权提示只显示进程 ID、字节数和关闭标志。

写管道可能因为子进程不读取而阻塞，因此 Manager 用专用线程执行一次写入和 flush，并在工具回合等待最多 2 秒。2 秒内无法确认结果时返回 `write_pending`。这表示写入仍在途，不能把它当成已经投递，也不能再次发送相同文本；下一次状态查询会显示它最终变成可写、关闭或错误。每个进程有串行写入锁，前一笔未收束时下一笔直接返回明确结果。

写入结果不携带正文，只报告进程 ID、字节数、stdin 状态、是否已关闭和有界错误类型。子进程提前退出、stdin 未启用、管道已关闭、Broken Pipe、未知或跨任务 ID 都有独立结果。管道报错时无法确认已经送达多少字节，结果用 `written_bytes=0` 和 `delivery_uncertain=true` 提醒 Agent 不要自动重试；真实写入失败会进入执行失败记录，而无效 ID 是正常的拒绝事实，不会使只读轨迹误报断链。输入正文即使包含唯一标记，也不会进入 State 快照、Trace、兼容工具历史、授权提示或普通/debug 终端输出；执行尝试只保留脱敏参数。

一个最小的等待输入程序可以这样启动。它先输出 ready，再等待一行文本，最后在收到 EOF 后退出：

```json
{
  "command": "python -u -c 'import sys; print(\"ready\", flush=True); line=sys.stdin.readline(); print(\"reply:\" + line.strip(), flush=True)'",
  "stdin_mode": "pipe"
}
```

收到 `process_id` 后，模型可以发送一行：

```json
{
  "process_id": "proc-1",
  "input": "hello\n"
}
```

如果程序需要 EOF 才会退出，再发送：

```json
{
  "process_id": "proc-1",
  "input": "",
  "close_stdin": true
}
```

然后用 `read_process` 读取 `reply:hello`，或用 `wait_process` 等待退出。重复关闭、关闭后写入和关闭后进程状态都会返回明确状态，不会重复投递字节。

## 为什么需要本版

把所有 stdin 都改成管道会改变旧命令的 EOF 行为，也会让不读取输入的程序更容易长期占用任务。因此 v0.29 采用显式能力开关：默认保持关闭，只有调用者知道程序需要输入时才开启管道。

把写入放进同步 handler 也会让不读取 stdin 的子进程卡住工具回合。专用线程和 2 秒上限把等待变成可观察状态；代价是 `write_pending` 之后不能安全猜测字节是否已经送达，必须等待 Manager 的最终状态。输入属于敏感的当前协议参数，但长期状态、轨迹和终端不需要复制它，所以这些观察面只保留字节数和脱敏标记。

本版只提供 UTF-8 管道字节流。管道没有终端回显、控制字符、终端尺寸或交互式 shell 语义，因此不能替代需要 PTY 的全屏程序、密码提示或依赖终端驱动的 CLI。PTY 需要单独的跨平台协议、清理和测试，暂留后续评估。

进程在写入后仍可能修改工作区。`written` 或 `closed` 只是输入操作的结果，不是进程退出事实，也不是 verification evidence。进程退出、控制和写入线程清理完成后，仍要在最终 generation 使用独立的 `run_shell(purpose="verification")` 收口；活动进程或在途写入都不能让任务进入 `done`。

## 本版特性、下一课与代码索引

本课让 Agent 能驱动等待少量文本的后台程序，并通过显式 EOF 结束输入。下一课可继续评估 PTY 或更复杂的终端协议，但必须先单独定义回显、控制字符、终端尺寸、跨平台行为和清理边界。

固定源码入口：[进程与 stdin 生命周期](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.29/src/mini_agent/processes.py) · [进程工具与参数校验](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.29/src/mini_agent/tools/process.py) · [执行器权限边界](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.29/src/mini_agent/tools/base.py) · [状态与脱敏](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.29/src/mini_agent/state.py) · [Trace 校验](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.29/src/mini_agent/trace.py)
