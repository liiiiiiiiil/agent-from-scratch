# 第 27 课：观察后台进程

上一课：[后台进程启动与任务边界](26-background-process-boundaries.md) · [教程总览](README.md) · 下一课：[控制后台进程](28-process-control.md)

代码快照：`v0.27` · 相邻差异：`v0.26..v0.27` · 示例命令环境：Bash/zsh

`v0.27` tag 由仓库维护者手动创建。tag 尚未创建时，可在当前分支阅读和运行本课的接口；固定源码链接和跨版本 diff 须在 tag 创建后核对。命令行参数仍是 CLI 的首条任务，处理后会进入交互循环。

## 本课目标

想象 Agent 启动了一个开发服务器。服务器很快打印 `ready`，但真正的错误可能在几秒后才出现在 stderr；服务器也可能长时间没有新输出。只返回启动时的 `process_id`，无法回答三个实际问题：它现在还活着吗？刚才输出了什么？它最后成功还是失败？

本课给这个进程增加一条“观察通道”。读完后，你应该能顺着一次调用解释下面的链路：

1. Agent 用 `get_process` 或 `list_processes` 查询生命周期，用 `read_process` 取出尚未交付的 stdout/stderr。
2. `read_process` 用两个字节游标记住已经交付的位置，因此重复读取不会重复返回日志。
3. 环形缓冲被新输出覆盖时，结果显式报告缺口；Agent 不会把一段残缺日志误当成完整日志。
4. 没有输出时，`wait_process` 只等待一个有上限的时间，超时后把任务交回 CLI，而不是继续消耗 LLM 回合。
5. 进程自然退出后，Runtime 记录一次退出事实；退出前得到的验证不能跨到新的 generation（验证代次）。

这里的“观察”只读取当前任务登记的进程。主动终止和 stdin 写入分别留给下一课及 `v0.29`，本课不会提前使用它们。

## 上一版的问题

`v0.26` 已经解决了后台启动最危险的部分：`start_process` 创建进程后立即返回；两个后台收集线程持续排空 stdout 和 stderr；每条流最多保留 64 KiB。这样，子进程不会因为 Agent 暂时没有读日志而被管道写满。

但是，排空只是“把日志安全地接住”，还不是“把日志交给模型”。上一版的启动结果大致只有下面这些信息：

```json
{"process_id":"proc-1","pid":12345,"status":"running"}
```

这能证明进程创建成功，不能说明它后来打印了什么，也不能说明它是否已经退出。若把整个缓冲区每轮都返回，模型会反复看到旧日志；若让模型每隔一小段时间调用一次查询，又会在“仍然没有输出”时浪费工具回合。`v0.27` 要把这两种情况区分开：有新事实时交付新事实，没有新事实时有限等待并交接。

## 前置条件与版本切换

需要基础 Python、Bash/zsh，以及上一课提到的任务专属 `process_id`、stdout/stderr 排空和任务边界。下面的命令先让你看到“观察接口插在哪里”，再切换到本课代码；`git diff --stat` 是范围速览，后面的文件和符号才是理解重点。

```bash
git checkout v0.26
git diff --stat v0.26..v0.27
git diff v0.26..v0.27 -- src/mini_agent/processes.py src/mini_agent/tools/process.py src/mini_agent/state.py src/mini_agent/agent.py src/mini_agent/trace.py
git checkout v0.27
```

没有 `v0.27` tag 时，留在当前开发分支运行本课命令即可；固定源码链接和跨版本事实检查等 tag 创建后再核对。

## 新增与改动文件

先用这张表建立地图。它不要求读者记住所有文件；后面每次提到一个内部名称时，都会说明它解决的具体问题。

| 文件 | 本课作用 |
| --- | --- |
| `src/mini_agent/processes.py` | 在 v0.26 的字节环上增加逐流读取游标、UTF-8 片段读取和有界等待。 |
| `src/mini_agent/tools/process.py` | 发布 `get_process`、`read_process`、`list_processes`、`wait_process` 四个观察工具。 |
| `src/mini_agent/state.py` | 把状态、输出位置和一次性退出事实写入可快照的 State，并在自然非零退出时建立失败事实。 |
| `src/mini_agent/agent.py` | 按工具调用顺序提交观察结果；等待超时后进入 `awaiting_process`，把任务交回 CLI。 |
| `src/mini_agent/trace.py` | 只读检查进程启动、退出、失败和 generation 之间的引用。 |

## 版本变更定位

图例：`[旧]` 上一版已有，`[+]` 本版新增，`[~]` 本版修改，`[C]` 主要消费者，`[B]` 本版边界。

上一版已经在后台收集输出，但模型没有读取入口：

```text
[旧] start_process
       -> [C] ProcessManager: Popen + stdout/stderr 字节环
       -> role=tool 返回 process_id

[旧] agent_loop 安全点
       -> State 同步自然退出
       -> 活动进程仍会阻止错误 done
[B] 模型只能知道“已经启动”，不能查询状态或读取新增日志
```

本版把观察工具接到同一条任务归属和工具协议上：

```text
[旧] start_process -> [C] ProcessManager 的进程与有界字节环
                           |
             [+] get_process / list_processes
             [+] read_process / wait_process
                           |
             [~] agent_loop 按模型顺序执行并逐个回灌 role=tool
                           +-> read：新增 stdout/stderr + 新位置 + 缺口
                           +-> wait 超时：still_running
                                         -> [C] awaiting_process -> CLI
                           +-> 自然零退出：ProcessEvent(exited)
                           +-> 自然非零退出：ProcessEvent(failed)
                                                 -> FailureEvent
                           -> 后继 generation -> 独立 verification
[C] State 保存可解释的位置和生命周期事实；Trace 只读检查这些事实
[B] 本版不提供终止、kill、stdin、PTY 或任意 PID 操作
```

观察工具返回的内容会先形成完整的 `role=tool` 消息，再进入下一次 LLM 请求。特别是 `wait_process` 超时，虽然任务会交回 CLI，但对应的工具结果仍然必须先回灌；否则下一次上下文会出现没有结果的工具调用。

## 关键流程：四个工具各回答什么

可以把四个工具看成四种不同的问题，而不是四个相似的查询接口：

| 工具 | 它回答的问题 | 是否消费日志 |
| --- | --- | --- |
| `get_process(process_id)` | 进程现在是什么状态？退出码是多少？累计输出到哪个字节位置？ | 否 |
| `list_processes()` | 当前任务登记了哪些进程？哪些仍在运行？ | 否 |
| `read_process(process_id)` | 自上次读取后，stdout/stderr 新增了什么？ | 是，推进两个读取游标 |
| `wait_process(process_id)` | 在有限时间内是否有新输出，或进程是否退出？ | 否 |

`process_id` 是 Runtime 给当前任务分配的标识。它和操作系统的 PID 不同：PID 只在启动结果中用于诊断显示，观察工具不会让模型拿一个任意 PID 去查询系统上的其他进程。未知、过期或跨任务的 `process_id` 会得到 `unknown_process_id`，不会替模型越过任务归属边界。

典型的观察顺序是：先 `start_process` 拿到 `process_id`，再用 `wait_process` 等待 `ready`，然后用 `read_process` 读取响应，最后用 `get_process` 确认状态和退出码。若已经知道输出马上到达，也可以直接读取；`wait_process` 只是减少“没有新事实时不断轮询”的成本。

## 核心概念一：读取游标让日志只出现一次

### 要解决的问题

stdout 和 stderr 是两个独立的字节流。后台线程已经把它们放进内存缓冲，但 Agent 需要一个规则知道“哪些字节已经交付过”。如果每次从 0 开始读取，旧日志会在每个工具回合重复出现；如果只看当前缓冲长度，缓冲淘汰后又无法知道中间是否丢过内容。

### 直观解释

每条流都有一个绝对字节位置，表示“上一次成功交给模型之后，下一次从哪里继续”。例如 stdout 依次产生：

```text
ready\n       6 个 UTF-8 字节
```

第一次读取返回 `ready\n`，并把 stdout 游标推进到 6。第二次读取如果没有新字节，就返回空的 `stdout`，游标仍然是 6。stderr 有自己的游标，所以 stderr 何时到达不会改变 stdout 的位置。

下面是实际协议的一个最小形状。字段名比示例内容更重要：正文是本次新增片段，`next_*_offset` 是下一次读取的起点。

```json
{
  "process_id": "proc-1",
  "status": "running",
  "exit_code": null,
  "stdout": "ready\n",
  "stderr": "",
  "next_stdout_offset": 6,
  "next_stderr_offset": 0,
  "output_gap": false,
  "stdout_output_gap": false,
  "stderr_output_gap": false,
  "stdout_lost_bytes": 0,
  "stderr_lost_bytes": 0
}
```

### 代码中真正改变游标的位置

实现先从 Manager 的游标计算片段，组装出结果，确认结果可以返回后才写回游标。下面省略了 JSON 预算和 stdout/stderr 分配，只保留这条顺序；顺序保证了“交付过的内容不会再次交付”。

```python
# v0.27 ProcessManager.read_process 的关键顺序
stdout, out_next, out_gap, out_lost, _ = self._stream_fragment(
    managed.stdout_ring, managed.stdout_cursor, ...
)
stderr, err_next, err_gap, err_lost, _ = self._stream_fragment(
    managed.stderr_ring, managed.stderr_cursor, ...
)
result = {
    "stdout": stdout,
    "stderr": stderr,
    "next_stdout_offset": out_next,
    "next_stderr_offset": err_next,
    "output_gap": out_gap or err_gap,
}
managed.stdout_cursor = out_next
managed.stderr_cursor = err_next
return result
```

这里的游标是**字节**位置，不是 Python 字符串下标。一个中文字符通常占多个 UTF-8 字节，后台线程可能先收到它的一部分。实现使用增量解码器：未收齐的尾部暂时不推进游标，下一次读取会从同一个字节继续；进程已经结束时，剩余非法或不完整字节才用替代字符交付。这样既不会重复中文字符，也不会把半个字符交给模型。

### 读得太晚时为什么要报告缺口

每条流只保留最近 64 KiB。假设游标还在 0，stdout 已经产生了 64 KiB 以上内容，最早的字节被环形缓冲淘汰；这时下一次读取只能从当前缓冲的起点开始。结果会把 `stdout_output_gap` 设为 `true`，并在 `stdout_lost_bytes` 中报告估计丢失的字节数。stderr 也有对应字段，`output_gap` 是两条流的汇总标记。

缺口不是普通的空输出。空输出表示“当前没有尚未交付的新内容”；缺口表示“旧内容已经无法恢复”。Agent 看到缺口后可以把后续日志当作诊断线索，但不能声称自己看到了完整历史。这个取舍让内存有上限，也让信息损失可见。

`max_chars` 默认是 2000，允许范围是 1–4000；两个流共享结果预算，完整 JSON 结果最多 8000 个字符。限制的是一次交给模型的结果，不是后台收集线程的总写入量。

## 核心概念二：观察状态与交付日志是两件事

`get_process` 不消费日志，因此适合回答“是否已经退出”和“退出码是多少”；`read_process` 会推进游标，因此适合回答“上次读取以后发生了什么”。`list_processes` 只返回当前任务的有界元数据，不包含日志正文；记录太多时从较早记录开始省略，并给出 `omitted_count`。把这三者混在一起，会让模型不知道一次查询是否已经改变了后续读取结果。

`wait_process` 又比 `get_process` 多一步：它可以在后台线程发现新输出或进程退出时提前返回，但不消费日志。它的返回原因有三种：

```text
reason=output_available  -> 有未读 stdout/stderr，随后调用 read_process
reason=exited            -> 已确认进程结束，随后调用 read_process/get_process
reason=still_running      -> 到达等待上限仍无新事实，任务交回 CLI
```

默认等待 1000 毫秒，允许范围是 0–30000 毫秒。等待工具必须独占一个工具回合，因为它是一个有时间边界的控制流动作；如果同一轮还包含其他工具调用，Runtime 会为每个 call 回灌拒绝结果，而不会让其中一个调用在不清晰的时间点继续等待。

## 核心概念三：没有输出时要等待和交接

### 为什么不能一直问模型

服务器可能 10 分钟都没有新日志，但这不代表任务失败，也不代表任务已经完成。若 Agent 每秒调用一次 LLM 去问“有变化吗”，它会把轮次和上下文预算消耗在重复观察上。反复得到相同状态或空日志也不算新的进展。

`wait_process` 因此只占用一个有上限的工具调用。若超时返回：

```json
{
  "process_id": "proc-1",
  "reason": "still_running",
  "status": "running",
  "exit_code": null,
  "stdout_offset": 6,
  "stderr_offset": 0
}
```

Agent 会先把这一条结果写进 history，然后进入 `awaiting_process`。这是一个“等用户下一次输入再继续”的非终态，意思是任务还没有完成，后台进程也仍然属于当前任务；它不是 `done`、不是 `blocked`，也不是进程失败。用户继续输入时，CLI 先同步真实进程状态，再恢复原任务，避免模型看到过期的 `running`。

如果模型没有调用工具就直接回复文本，而此时仍有活动进程，Runtime 也会走同一类交接。这条护栏不能由模型的一句“进程还在运行”来伪造，必须由真实的 Manager 和 State 共同确认。

## 实现拆解：退出是前台提交的生命周期事实

### 收集线程只收集，Runtime 才改变 State

Manager 持有 `Popen`、进程组、收集线程、字节环和读取游标；这些是当前 CLI 进程内的运行资源，不能放进快照。后台线程只负责排空 stdout/stderr 和通知等待者。它不直接创建 `ProcessEvent`、`FailureEvent` 或 generation。

前台 Runtime 在几个安全点调用 `sync_processes()`：

```text
LLM 请求前
工具结果全部回灌后
无 tool_calls 的完成判断前
用户从 awaiting_process 继续任务时
```

这保证 State 看到的是一个完整、可解释的观察。同步会更新 stdout/stderr 的累计位置；只有首次确认稳定退出时才追加最终 `ProcessEvent`。重复 `get_process`、重复 `read_process` 或重复同步不会复制退出事件。

### 什么叫“确认稳定退出”

Manager 不把 `Popen.poll()` 立刻返回的退出码当作完整退出。它还要确认 POSIX 下受管进程组已经消失，并确认 stdout、stderr 收集线程都读到 EOF。原因是外层 shell 可能先退出，而它的后代仍在运行或仍持有管道。只确认直接子进程会让 Agent 过早以为资源已经收口。

确认后，零退出码形成 `ProcessEvent(kind="exited")`；非零退出码形成 `ProcessEvent(kind="failed")`，并额外形成一个 `FailureEvent`。非零退出不能靠解析日志文字推断，必须来自真实的退出码。

### 为什么自然失败要连接启动事实

`FailureEvent` 会引用启动 attempt（一次已经进入执行记录的工具尝试）和产生它的进程事件。可以把因果关系读成：

```text
start_process attempt
        ↓
ProcessEvent(kind=failed, exit_code=7)
        ↓
FailureEvent(caused_by_process_event_id=...)
```

这让 Repair Loop（失败后的诊断、恢复和验证循环）知道失败来自哪一个进程，而不必把一次观察调用伪装成失败的执行调用。多次同步只会找到同一个 `terminal_event_id`，所以同一退出只产生一次失败事实。

### 退出为什么会开启新的 generation

`generation` 是验证代次：它标记一组验证证据是在环境经历哪些副作用之前还是之后获得的。后台进程即使不修改仓库，也可能写文件、生成缓存、占用端口或改变外部服务状态。进程退出时，Runtime 会清除当前验证证据、开启由退出事件关联的后继 generation，并设置 `verification_required`。

因此，“启动成功”“读到了 `ready`”“进程以 0 退出”都不是最终 verification。退出后仍需在新的 generation 独立调用 `run_shell(purpose="verification")`；若是非零退出，还要先进入现有诊断或重规划流程。若已有另一个活动失败或正在严格验证，进程退出会保留双方事实，并在无法安全判断时阻塞任务，而不是覆盖原来的修复义务。

## 运行与观察

下面的命令适用于 Bash/zsh，用于运行本版与后台观察直接相关的测试场景：

```bash
PYTHONPATH=src python -m pytest -q tests/test_process_management_v027.py
```

观察测试结果时，不要只看“全部通过”。把现象和概念对应起来：

| 现象 | 它证明了什么 |
| --- | --- |
| UTF-8 字符跨收集块后只返回一次 | 游标按字节保存，增量解码不会重复或交付半个字符。 |
| 两次 `read_process` 的第二次正文为空 | 游标在成功返回后推进，读取是增量的。 |
| 缓冲淘汰后出现 `output_gap` 和丢失字节数 | 有界内存不会掩盖日志缺失。 |
| `wait_process` 返回 `output_available` 后 `read_process` 仍能读到日志 | 等待只观察，不消费游标。 |
| 非零退出只产生一个带因果引用的失败事实 | 前台同步是幂等的，失败来自真实退出事件。 |
| `wait_process` 超时后 LLM 只被调用一次，状态为 `awaiting_process` | 无输出会交回 CLI，不会自动轮询消耗回合。 |

也可以在当前代码分支让 Agent 启动一个会先打印 `ready`、随后退出的短命令，然后依次调用 `get_process`、`read_process` 和 `wait_process`。如果读取发生在进程退出之后，仍应能看到尾部输出；如果等待没有新输出，应看到 `still_running`，而不是把空结果当成失败。

## 为什么这样设计

把读取游标放在拥有句柄的 Manager，是为了让同一任务内的多次观察共享一个确定的位置；把日志正文留在本次工具结果，是为了避免 State 和 Trace 无限增长。代价是 CLI 进程结束后不能重新连接旧的 `Popen` 和管道，Trace 也只能回放已经提交的结构化事实。

stdout 和 stderr 分开保存，能分别报告位置和缺口，代价是本课不承诺两条流在终端上的精确混排顺序。想要终端式交互的程序还需要 stdin、回显和 PTY 语义，这不属于本版。

等待采用“有上限的工具调用 + CLI 交接”，是因为没有输出不等于没有未来，也不值得让一次 LLM 回合无限占用。代价是用户可能需要再次输入才能恢复观察。把退出事实放在前台同步点提交，则能同时满足工具协议完整、State 可快照、Trace 只读和退出事件幂等这几个要求。

## 设计边界

- 观察工具只能访问当前任务登记的 `process_id`；不能查询任意 PID，也不能跨 CLI 会话重连。
- `get_process`、`list_processes`、`read_process` 和 `wait_process` 不会终止进程，不会推进计划步骤，也不会产生 verification evidence。
- 每条输出流最多保留 64 KiB；`output_gap` 出现后不能恢复被淘汰的历史。
- `read_process` 的 `max_chars` 默认 2000，范围 1–4000；`wait_process` 默认 1000 毫秒，范围 0–30000 毫秒。
- 观察工具结果不会代替 `role=tool` 回灌；同轮多个观察调用按模型顺序提交，`wait_process` 必须独占回合。
- 自然退出会使旧验证失效；活动进程、待验证 generation 或已有修复义务仍然会阻止任务错误完成。
- 本课不提供 `terminate_process`、`kill_process`、`write_process` 或 PTY；主动控制见下一课，管道 stdin 见 `v0.29`。

## 本版特性、下一课与代码索引

本版新增四个当前任务内的只读观察工具、逐流增量输出、字节游标、缺口报告、自然退出事实、非零退出的 `FailureEvent` 和有界等待交接。下一课会在这个观察结果之上增加受权限保护的正常终止与强制结束；本课不把“观察到进程”当成“可以控制进程”。

固定源码入口：

- [Manager 的读取游标与等待](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.27/src/mini_agent/processes.py)
- [四个观察工具的协议](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.27/src/mini_agent/tools/process.py)
- [退出失败事实与前台同步](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.27/src/mini_agent/state.py)
- [agent loop 的串行观察和交接](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.27/src/mini_agent/agent.py)
- [Trace 的跨 generation 因果校验](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.27/src/mini_agent/trace.py)
