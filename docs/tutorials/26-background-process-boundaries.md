# 第 26 课：后台进程启动与任务边界

上一课：[计划轨迹回放与验收](25-plan-trace-evaluation.md) · [教程总览](README.md) · 下一课：后续版本课程（待规划）

代码快照：`v0.26` · 相邻差异：`v0.25..v0.26` · 示例命令环境：Bash/zsh

`v0.26` tag 由仓库维护者在交付后手动创建；tag 尚未创建时，可以留在当前分支阅读和运行实现。命令行参数是命令行首条任务，处理后程序仍进入交互循环。

## 本课目标

上一课把一次任务的计划和执行事实连成了只读轨迹。本课继续处理一个更实际的运行时问题：开发服务器、文件监听器或长时间构建可能几分钟都不退出，但 Agent 仍需要检查文件、计算结果或继续计划。

本课结束后，读者应能说明四件事：为什么带管道的长命令会堵住；`start_process` 如何立即返回任务内的 ID；为什么进程退出会让旧 verification 失效；以及为什么任务切换和 CLI 退出必须先清理旧任务登记的进程。

本版只发布启动工具。观察日志、查询状态和主动终止分别留给后续版本；同步的 `run_shell` 行为保持不变。

## 上一版的问题

`run_shell` 使用同步执行，Agent 必须等子进程退出后才能获得结果。这个模型适合测试、搜索和其他有限时间命令，却不适合启动开发服务器：命令还在提供服务时，Agent 已经无法进入下一次工具回合。

直接把 stdout 和 stderr 接到管道也会造成另一个问题。子进程写满管道后会在下一次写入处阻塞；即使 Agent 从未请求读取日志，命令也可能停在那里。后台能力必须从进程创建的一刻开始排空两条流，同时限制内存。

## 前置条件与版本切换

需要基础 Python、Bash/zsh，以及第 25 课中的 generation、执行 attempt、verification 和只读 Trace 概念。先查看相邻版本的真实差异，再切换到本课快照：

```bash
git checkout v0.25
git diff --stat v0.25..v0.26
git diff v0.25..v0.26 -- src/mini_agent/state.py src/mini_agent/tools/base.py src/mini_agent/__main__.py
git checkout v0.26
```

如果本地还没有 `v0.26` tag，可以保留在当前开发分支运行下面的实现；固定源码链接和教程事实检查以维护者手动创建 tag 后的快照为准。

## 新增与改动文件

本版的关键变化横跨资源管理、工具执行、任务状态和 CLI。文件表只列帮助理解主线的入口；完整范围先用 `git diff --stat` 核对。

| 文件 | 变化 | 作用 |
| --- | --- | --- |
| `src/mini_agent/processes.py` | 新增 `ProcessManager` | 独占 `Popen`、进程组、排空线程和 stdout/stderr 有界字节环。 |
| `src/mini_agent/tools/process.py` | 新增 `start_process` | 校验命令和 cwd，启动当前任务的后台进程，并返回有界启动结果。 |
| `src/mini_agent/tools/base.py`、`permission.py` | 修改执行器和权限模式 | 为启动动作独立授权，在 handler 成功后把启动元数据交给 State。 |
| `src/mini_agent/state.py` | 新增进程投影和生命周期事件 | 保存任务归属、启动 attempt、generation、退出事实、等待交接和验证隔离。 |
| `src/mini_agent/agent.py`、`context.py`、`prompt.py` | 修改 loop 与上下文 | 在安全点同步退出，阻止错误完成，并向模型展示任务和进程边界。 |
| `src/mini_agent/__main__.py` | 修改 CLI 生命周期 | 显示 `awaiting_process`，恢复原任务，并在 `/new`、`/reset`、退出和异常路径清理进程。 |
| `src/mini_agent/trace.py` | 修改只读回放 | 校验进程事件与退出 generation 的引用，回放不接触句柄和管道。 |

## 版本变更定位

图例：

```text
[旧] 上一版已有    [+] 本版新增    [~] 本版修改
[C] 主要消费者    [B] 本版边界或不负责
```

上一版的同步入口等待命令完成，再把一个结果写入 history：

```text
[旧] agent_loop -> ToolExecutor -> PermissionGate -> run_shell
                                      │
                                      └── 等待 subprocess.run 返回
                                      ↓
                              role=tool -> 下一次 LLM 请求
```

本版把后台启动插入同一条工具协议，并在前台安全点接收自然退出事实：

```text
[旧] agent_loop -> [~] ToolExecutor -> [~] PermissionGate -> [+] start_process
       │                 │                                      │
       │                 │                                      └── [C] ProcessManager
       │                 └── possible attempt/generation        │    ├── Popen + 独立进程组
       │                                                        │    ├── stdout/stderr 排空
       │                                                        │    └── 64 KiB/流有界缓冲
       ↓                                                        ↓
[~] 完整 role=tool 回灌 -> [~] sync_processes -> [C] AgentState ProcessEvent
                                │                         ├── exited/failed
                                │                         └── 后继 generation + 新 verification 要求
                                ↓
                         [C] 完成护栏 / awaiting_process -> CLI

[~] /new /reset / EOF / 异常 -> [C] manager.cleanup(task_id)
                                      ├── TERM，最多等待 2 秒
                                      ├── KILL，最多等待 2 秒
                                      └── 报告不完整清理

[B] get_process、read_process、list_processes、wait_process、terminate_process、kill_process 未在本版注册
[B] start_process 和进程状态本身不产生 verification evidence
```

关键映射是：`ProcessManager` 只拥有不可快照的操作系统资源；`AgentState` 只接收可解释事实；`agent_loop` 决定何时提交事实和阻止完成；CLI 负责把等待交回用户以及清理任务边界。这样，Trace 可以只检查 State 快照，回放不会重新 `poll()` 或读取管道。

## 核心概念与数据结构

### 先排空管道，再谈后台运行

要解决的问题是“模型不读日志时，子进程也不能被日志写入拖住”。`ProcessManager.start()` 用 `stdout=PIPE`、`stderr=PIPE` 和 `stdin=DEVNULL` 创建进程，并在返回前分别启动两个排空线程。每条流进入 `_ByteRing`，最多保留 64 KiB；环以累计写入字节数计位置，所以释放旧日志不会改变已经发生过的偏移。

下面这段实现只展示排空动作。`read1(8192)` 最多读取 8192 字节，但少量输出到达时也能及时返回；如果用缓冲流的 `read(8192)`，短输出可能一直等到缓冲凑满或管道关闭。线程持续消费管道，环只保存有界数据。v0.26 没有日志读取工具，但这项内部能力保证高频 stdout/stderr 不会因为管道填满而停住。

```python
def _collect(self, stream, ring):
    while True:
        chunk = stream.read1(8192)
        if not chunk:
            break
        ring.append(chunk)
```

非法 UTF-8 不会在排空线程中抛给 Agent；后续读取接口使用 `errors="replace"` 解码。State 和 Trace 只保留 stdout/stderr 的累计位置，不保存日志正文，因此上下文裁剪不会把长日志变成无限增长的状态。

### 启动 ID 属于任务，启动成功属于一次 attempt

要解决的问题是“进程已经创建，但模型不能把它误说成命令最终成功”。`start_process` 的 `effect_class` 是 `possible`，所以它通过权限闸门后先预留 attempt 和 generation，再执行 handler。handler 返回的内部元数据包含 PID、任务 ID、命令摘要和 cwd 摘要；Executor 的公开结果只保留有界字段：

```json
{"process_id":"proc-1","pid":12345,"status":"running","start_attempt_id":"a-3"}
```

这里的 `process_id` 是 Runtime 在当前 CLI 生命周期中单调分配的标识，后续版本的工具也只会接受它；PID 仅用于诊断显示。每次 `begin_task` 还会分配不复用的 `task_id`，因此两个文本相同的任务也不会共享进程归属。

启动 attempt 提交时，State 在同一把锁内追加 `ProcessRecord` 和 `started` `ProcessEvent`。这一步放在 handler 成功之后，解决了“进程立即退出，退出线程先于启动记录写入”的竞态：前台同步只会处理已经登记的进程，下一安全点仍能发现这次退出。

### 退出是新的环境事实，不能复用旧验证

要解决的问题是“服务运行期间可能改变环境，退出后旧检查不能继续代表当前状态”。收集线程只更新 Manager 的原始句柄和缓冲；`agent_loop` 在组装上下文前、完整工具结果回灌后、完成判断前和恢复任务时调用同一个 `sync_processes()`。只有外层进程已退出、POSIX 受管进程组已消失且两条管道都读到文件结束标记（EOF），Manager 才提交最终退出事实。在此之前，进程仍占活动额度并阻止 `done`。State 看到新退出后追加一个最终事件：零退出为 `exited`，非零退出为 `failed`，两者都引用原启动 attempt。

随后 State 只开启一次由该事件引用的后继 generation，清空当前 generation 的 verification evidence，并置 `verification_required`。已有 active failure 或严格 verification 阶段与退出冲突时，双方事实都保留，任务进入带 `process_exit_conflict` 原因的保守阻塞；进程退出不会覆盖先前的失败。

Trace 只从快照校验 `ExecutionGeneration.opened_by_process_event_id` 是否指向对应的退出事件，并检查每个进程至多有一个最终事件。下面的字段说明这个因果方向，读者应看到“退出事件开启后继代次”，而不是“启动结果直接通过验证”：

```python
ExecutionGeneration(
    generation_id=next_generation,
    opened_by_process_event_id="pe-2",
    open_reason="process_exit",
)
```

### 活动进程会把文本回复交给 CLI

要解决的问题是“模型已经没有工具调用，但程序仍在运行”。完成判断先检查 State 中 `status == "running"` 的进程；如果模型返回无 `tool_calls` 的文本，loop 写入 `awaiting_process`，立即把文本交给 CLI。这个状态是非终态：它不生成 `FailureEvent`，不消耗额外 LLM 轮次，也不把等待时间算作进展。

CLI 显示 task ID、每个活动 process ID 和累计 stdout/stderr 位置，同时显示尚未完成的计划、修复和验证义务。用户下一次输入仍属于原任务；CLI 先同步退出事实，再清除等待状态并恢复 `running`。如果进程已经退出，恢复过程会先开启新的 generation 和验证要求，不能沿用运行期间的检查结果。

## 关键流程

正常的启动路径如下：

```text
模型 tool_calls(start_process)
  -> 参数校验 command/cwd
  -> 独立 start_process 命令模式权限
  -> 预留 possible attempt + generation
  -> Popen(shell=True, stdin=DEVNULL)
  -> 启动 stdout/stderr 排空线程
  -> 返回有界 role=tool 结果
  -> State 登记 ProcessRecord + started event
  -> Agent 继续其他工具回合
```

退出和完成护栏路径如下：

```text
进程自然退出
  -> Manager 在前台安全点产生一次退出事实
  -> State 记录 exited/failed event
  -> 新 generation + verification_required
  -> 活动进程为空后仍需独立 verification
  -> 验证通过且其他义务完成，才可能 done

模型无 tool_calls 且进程仍 running
  -> awaiting_process
  -> CLI 显示 process_id 和剩余义务
  -> 用户继续输入
  -> 先 sync，再恢复原任务
```

任务边界路径在 State 清空之前执行：

```text
/new、/reset、EOF、exit 或异常
  -> sync 当前任务
  -> TERM + 最多 2 秒
  -> 未退出则 KILL + 最多 2 秒
  -> 确认直接子进程、受管进程组（POSIX）和两条管道结束
  -> 有界等待已启动的收集线程，再关闭管道
  -> 完整：记录清理结果并允许边界切换
  -> 不完整：显示 pid/process_id/原因，保留旧任务，不开始新任务
```

## 实现拆解

`ProcessManager` 是唯一接触 `Popen` 的对象。POSIX 启动时创建独立进程组，清理优先向进程组发送正常终止信号，再在有界等待后发送强制信号；外层 shell 退出仍不足以证明同组后代结束。Windows 确认直接子进程和管道结束即可完成任务边界清理，并在报告中说明无法证明任意派生进程树已结束。收集线程或管道未结束时，Manager 保留登记供下次重试，不做可能无限阻塞的关闭。失败的 `Popen` 不会进入 State 的成功进程记录，但已经分配的 process ID 也不会复用；若第二条收集线程启动失败，只等待已启动的线程，清理不完整时保留可见的 Manager 登记。

`ToolExecutor` 仍负责参数、权限、attempt 和 handler 异常边界。授权拒绝发生在 attempt 预留前，所以没有 generation；授权后 `cwd` 无效、额度耗尽或 `Popen` 抛错，则保留已预留的 generation 和失败 attempt。成功的启动元数据只作为 `ExecutionResult` 的内部字段传给 State，公开 tool result 不携带命令正文或日志。

`AgentState.sync_processes()` 只接受 Manager 在前台生成的事实。重复安全点会更新累计位置，却不会重复追加最终事件或重复开启 generation；`acknowledge_exit()` 在 State 的登记和事件提交后才标记 Manager，避免立即退出在启动记录尚未完成时被吞掉。

## 为什么这样设计

把操作系统资源放在 Manager、把审计事实放在 State，会让线程和文件描述符不会进入快照，也让 Trace 保持真正只读。代价是进程只属于当前 CLI 生命周期；CLI 崩溃或断电后没有重连能力。把输出限制在每流 64 KiB 可以控制内存，但 v0.26 尚未向模型公开读取接口，日志尾部只能等待后续版本的观察工具。

启动与退出分成两个事实，是为了区分“创建成功”和“命令最终成功”。它会让自然退出多开启一个 generation，并要求重新验证；这增加了步骤，但避免运行期间或退出时的环境变化错误地复用旧证据。`run_shell` 仍保持原同步协议，使短命令的既有调用和返回格式不变。

把等待交回 CLI 可以避免 Agent 在没有观察工具时反复调用 LLM。限制是 v0.26 不能由模型主动查询、读取或终止进程；长期服务要么自然退出，要么在任务切换和 CLI 退出时由内部清理。

## 设计边界

- `start_process` 只接受非空命令和存在的 cwd，拒绝未知参数；命令规则独立于 `run_shell`，默认 ASK。进程启动成功不等于命令成功。
- 每任务最多 4 个活动进程；每条输出流最多 64 KiB；stdin 为 `DEVNULL`。输出不进入 State 正文，也不产生 verification evidence。
- 只有当前任务的 State 会登记进程。旧 `task_id` 和旧 `process_id` 不会命中新任务；PID 不作为模型工具参数。
- 自然零退出记 `exited`，自然非零退出记 `failed`；v0.26 不把异步非零退出转换为 `FailureEvent`，该因果扩展留给 v0.27。
- `exploring` 和 `--plan` 仍拒绝启动；计划批准不替代 `PermissionGate`。同一轮多个调用继续为每个 call 回灌一个 `role=tool` 结果。
- `/new`、`/reset`、EOF、`exit` 和异常路径都尝试有界清理。清理失败可见且保留旧任务；清理不会撤销进程已经造成的文件或环境修改。
- POSIX 的进程组与管道边界无法发现或控制同时脱离受管进程组、关闭继承管道的后代。Windows 的直接子进程清理成功也不证明任意派生进程树已结束。
- 本版不提供 `get_process`、`read_process`、`list_processes`、`wait_process`、`terminate_process`、`kill_process`，也不提供跨 CLI 恢复、通用沙箱、PTY 或 stdin 写入。

## 运行与观察

下面的命令适用于 Bash/zsh，用于启动本版 CLI 的命令行首条任务：

```bash
PYTHONPATH=src python -m mini_agent "启动一个长期运行的本地服务，服务可用后继续检查项目"
```

如果模型获准调用 `start_process`，终端应先看到含 `process_id` 和 PID 的有界启动结果，随后还能看到其他工具回合。若模型在进程仍运行时直接回复文本，CLI 应显示 `awaiting_process`、任务 ID、进程 ID 和“继续输入以观察任务”；这说明 Runtime 阻止了错误的 `done`。用户继续输入后，CLI 会先同步进程，再把输入交给原任务。任务结束或输入 `/new`、`/reset` 时，应看到清理结果；清理不完整时会显示具体 PID、process ID 和原因。

`start_process` 的授权确认独立于 `run_shell`。即使某条命令已经通过 `run_shell` 规则放行，后台启动仍会按自己的命令规则询问；选择 `always` 也只影响同一工具的同类 pattern。

## 本版特性、下一课与代码索引

本版新增非阻塞后台启动、任务专属 ID、启动 attempt 与生命周期事件、双流排空、有界缓冲、退出后的 generation 隔离、`awaiting_process` CLI 交接和任务边界清理。`run_shell` 的同步行为、30 秒超时、返回格式、Plan Mode、Repair Loop、Trace 只读边界和完成提醒继续有效。

下一课尚未固定；后续版本会在本版的进程事实之上增加受限观察能力。本课固定源码索引如下，链接指向交付时的 `v0.26` tag：

- [`processes.py` 的 Manager、排空与清理](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.26/src/mini_agent/processes.py)
- [`tools/process.py` 的 start_process schema](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.26/src/mini_agent/tools/process.py)
- [`state.py` 的 ProcessRecord、ProcessEvent 与同步](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.26/src/mini_agent/state.py)
- [`agent.py` 的安全点与 awaiting_process](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.26/src/mini_agent/agent.py)
- [`__main__.py` 的 CLI 交接与边界清理](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.26/src/mini_agent/__main__.py)
- [`test_process_management_v026.py` 的生命周期示例](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.26/tests/test_process_management_v026.py)
