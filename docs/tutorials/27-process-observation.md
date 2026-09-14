# 第 27 课：观察后台进程

上一课：[后台进程启动与任务边界](26-background-process-boundaries.md) · [教程总览](README.md) · 下一课：进程控制（规划中）

代码快照：`v0.27` · 相邻差异：`v0.26..v0.27` · 示例命令环境：Bash/zsh

`v0.27` tag 由仓库维护者手动创建。tag 尚未创建时，可在当前分支阅读和运行本课的接口；固定源码链接和跨版本 diff 须在 tag 创建后核对。命令行参数仍是 CLI 的首条任务，处理后会进入交互循环。

## 本课目标

上一课的 Agent 能启动长期命令，却只能看到启动时的 `process_id`。本课让它在后续轮次知道程序是否还在运行、刚刚输出了什么、最终怎样退出。读完应能解释：为什么读取日志要记住位置，为什么缓冲丢失必须明说，以及为什么“暂时没有输出”应交回用户等待。

## 上一版的问题

开发服务器可能先打印 `ready`，稍后才报错。v0.26 虽然一直排空两条输出管道，防止程序因管道写满而卡住，但没有把日志读取接口发布给模型。启动结果只能说明进程创建成功，不能回答后来的输出或退出码。

直接把整个缓冲反复返回也不行：模型会重复看到旧日志，还可能把“又读了一遍”误认为任务有了新进展。持续运行的服务也可能很久不输出；Agent 若不断查询，会浪费轮次并触发停滞护栏。

## 前置条件与版本切换

需要基础 Python、Bash/zsh，以及上一课对任务专属 `process_id` 和双流排空的理解。先看相邻版本的真实差异，再切换到本课代码；没有 `v0.27` tag 时，留在当前开发分支运行后面的命令。

```bash
git checkout v0.26
git diff --stat v0.26..v0.27
git diff v0.26..v0.27 -- src/mini_agent/processes.py src/mini_agent/tools/process.py src/mini_agent/agent.py
git checkout v0.27
```

`git diff --stat` 应显示本课的观察工具、Manager、State、Trace 和文档改动；它用于确认本课讲述的范围，而不是要求记住每个文件。

## 新增与改动文件

| 文件 | 本课作用 |
| --- | --- |
| `src/mini_agent/processes.py` | 在既有字节环上保存读取位置，按 UTF-8 边界交出片段，并提供有界等待。 |
| `src/mini_agent/tools/process.py` | 向模型发布状态、增量日志、列表和等待四个只读工具。 |
| `src/mini_agent/state.py`、`src/mini_agent/trace.py` | 把自然非零退出连接到失败事实，并只读检查跨 generation 的因果关系。 |
| `src/mini_agent/agent.py` | 串行提交观察结果；等待超时时交回 CLI。 |

## 版本变更定位

图例：`[旧]` 上一版已有，`[+]` 本版新增，`[~]` 本版修改，`[C]` 主要消费者，`[B]` 本版边界。

上一版的运行链路中，输出已经被收集，但模型没有读取入口：

```text
[旧] start_process -> [C] ProcessManager -> Popen + stdout/stderr 字节环
          |                    |
          |                    +-> 仅内部收集输出
          +-> role=tool 返回 process_id

[旧] agent_loop 安全点 -> State 同步自然退出 -> 后继 generation
          +-> 进程仍运行且模型只回复文本 -> awaiting_process -> CLI
[B] 模型尚不能查询状态或读取日志
```

本版保留同一资源归属，在工具结果和状态事实之间增加观察接口：

```text
[旧] start_process -> [C] ProcessManager 的进程与有界字节环
                           |
             [+] get/read/list/wait_process（当前 task_id）
                           |
             [~] agent_loop 按模型顺序提交观察结果
                           +-> read：新增片段 + 下一字节位置 + 缺口
                           +-> wait 超时：role=tool -> awaiting_process -> CLI
                           +-> 自然非零退出：[~] ProcessEvent -> FailureEvent
                                                  -> 后继 generation -> 诊断
[C] State 保存位置和因果事实；Trace 只读检查这些事实
[B] 本版不提供模型主动终止、stdin 或任意 PID 操作
```

正常读取沿用 v0.26 的排空线程；等待超时也先有一个对应的 `role=tool` 结果，然后才交回 CLI。这样下一次模型调用不会看到缺少结果的工具请求。

## 关键流程：一次读取只交付新增内容

**读取游标**是“上次成功交付到哪里”的字节位置。stdout 和 stderr 各有一个游标，因为两条流独立到达；第 27 课不猜它们在终端上的混排顺序。`read_process` 返回内容后才推进两个游标，同一进程同轮多次读取按调用顺序执行。

看一个最小结果。假设 stdout 已写出 `ready\n`，stderr 还没有输出：

```json
{"process_id":"proc-1","status":"running","stdout":"ready\n","stderr":"","next_stdout_offset":6,"next_stderr_offset":0,"output_gap":false,"stdout_output_gap":false,"stderr_output_gap":false,"stdout_lost_bytes":0,"stderr_lost_bytes":0}
```

再读而没有新输出时，两个正文都是空字符串，位置不变。字节位置不能直接当作 Python 字符下标：中文字符的 UTF-8 编码可占多个字节。Manager 在字节层保存位置，解码时暂留未收齐的尾部，下一轮才交出完整字符；确认结束后，残缺或非法字节显示为替代字符。

每条流只保存最近 64 KiB。若读者落后太多，旧字节已经被淘汰，结果会分别指出 `stdout_output_gap`、`stderr_output_gap` 与丢失字节数。此时不能把剩余片段当成完整日志。`max_chars` 默认合计 2000，可设 1–4000；完整 JSON 回复不超过 8000 字符。日志不会进入长期 State 或 Trace，只作为这次工具结果回灌给模型。

## 实现拆解：退出和等待都是独立事实

状态查询不应自己“制造”程序失败。v0.26 已在前台安全点同步退出，v0.27 进一步在自然非零退出时记录一次 `FailureEvent`：它引用启动的 attempt 和真正发生的退出事件，失败属于观察退出时的 generation。随后打开新 generation，使程序运行期间取得的旧验证不能用来完成任务。多次 `get_process` 或 `read_process` 不会复制该事件。

```python
# 关键关系的简写；字段名与 State 中的事实相同。
failure.caused_by_attempt_id == process.start_attempt_id
failure.caused_by_process_event_id == process.terminal_event_id
failure.generation_id == exit_event.generation_id
```

这三个引用让只读 Trace 可以说明“哪个启动动作创建了进程、哪次退出导致失败”，而不用把退出伪装成第二次启动工具结果。若已有另一个活动失败或处于严格验证阶段，Runtime 保留两个来源并阻塞，避免覆盖原来的修复义务。

`wait_process` 的任务是避免无意义轮询。它只等待**未读输出**或确认退出，不消费日志；默认最多 1000 毫秒，单次上限 30000 毫秒，且必须独占工具回合。超时给出 `still_running`，Agent 进入非终态 `awaiting_process`，CLI 等用户输入。用户继续时先同步真实进程状态；等待本身不是进展、失败或验证。

## 运行与观察

在 Bash/zsh 中运行当前代码的独立观察场景：

```bash
PYTHONPATH=src python -m pytest -q tests/test_process_management_v027.py
```

应看到这些场景通过：跨块的中文字符只出现一次，两次读取没有重复正文，非零退出只生成一条带因果引用的失败事实，等待超时只发生一次 LLM 调用并交回 CLI。这些现象分别证明游标、异步失败和有界交接确实生效。运行交互式任务时，也可让 Agent 启动短暂的本地命令，再依次调用 `get_process`、`read_process` 和 `wait_process`；`wait_process` 超时后输入下一条消息继续原任务。

## 为什么这样设计

读取位置留在持有句柄的 Manager，State 只保存累计位置和生命周期事实，因此快照不会携带文件描述符或日志全文。代价是 CLI 进程结束后不能重连之前的后台进程。把两流分开能明确报告缺口和位置，但不提供跨流精确排序；需要终端式交互的程序仍不适用。

等待选择有上限的工具调用和 CLI 交接，因为无输出不等于任务进展。即便服务运行时做过连通性检查，活动进程仍可能继续修改环境；自然退出后必须在新的 generation 中独立验证。v0.27 只负责观察，主动控制归后续版本。

## 本版特性、下一课与代码索引

本版新增四个当前任务内的只读观察工具、逐流增量输出、自然非零退出的独立失败事实和超时交接。下一课计划处理有权限的正常终止与强制结束；本课不提前开放控制接口。

以下固定源码链接指向交付时由用户手动创建的 `v0.27` tag：

- [Manager 的读取游标与等待](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.27/src/mini_agent/processes.py)
- [四个观察工具的协议](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.27/src/mini_agent/tools/process.py)
- [退出失败事实与前台同步](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.27/src/mini_agent/state.py)
- [agent loop 的串行观察和交接](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.27/src/mini_agent/agent.py)
- [Trace 的跨 generation 因果校验](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.27/src/mini_agent/trace.py)
