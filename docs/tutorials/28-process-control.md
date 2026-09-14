# 第 28 课：控制后台进程

上一课：[观察后台进程](27-process-observation.md) · [教程总览](README.md) · 下一课：按需扩展

代码快照：`v0.28` · 相邻差异：`v0.27..v0.28` · 示例命令环境：Bash/zsh

`v0.28` tag 由仓库维护者手动创建。tag 尚未创建时，可在当前分支阅读和运行本课；固定源码链接和跨版本 diff 须在 tag 创建后核对。命令行首条任务完成后，CLI 仍会进入交互循环。

## 本课目标

上一课的 Agent 已经能够发现后台服务退出，却还不能主动结束它。比如本地服务器已经完成检查，继续运行只会占端口；或者模型发现启动参数错误，必须先停止旧进程才能重试。任务边界清理虽然会在 `/new`、`/reset` 和 CLI 退出时处理进程，但这不是模型在任务中可使用的控制入口。

本课增加两个受权限保护的工具：`terminate_process` 请求正常终止，`kill_process` 强制结束。读完后，你应该能区分下面几个经常被混淆的事实：

- “信号已经发出”只表示控制请求送达，不表示进程已经退出。
- “已经退出”只有在直接进程、受管进程组和输出管道都达到稳定边界后才可以提交。
- 未知或跨任务的 ID 必须在权限询问前拒绝；一个工具的授权不能扩大成控制任意 PID 的授权。
- 控制动作和退出事件会使旧验证失效；结束进程后仍要在新的 generation 独立验证。
- `still_running`、`already_exited` 和 `control_failed` 是不同结果，后续动作也不同。

## 上一版的问题

`v0.27` 可以用 `get_process`、`read_process` 和 `wait_process` 观察状态与输出，但所有工具都是只读的。Agent 看见：

```text
process_id=proc-1
status=running
stdout 中有 ready
```

它仍然没有办法在当前任务中结束这个服务。直接把“发出 SIGTERM”当成成功也会带来更严重的问题：程序可能忽略正常终止，外层 shell 可能已经退出而后代仍在运行，后代还可能继续持有 stdout/stderr 管道。此时如果 State 立即写入“已结束”，Agent 就可能继续完成任务，同时留下一个仍在修改环境的进程。`v0.28` 要解决的正是“请求控制”和“确认退出”之间的间隔。

## 前置条件与版本切换

需要基础 Python、Bash/zsh，以及上一课的 `process_id`、稳定退出、`awaiting_process` 和 generation。下面的 diff 用于定位本版真正插入的位置：工具注册和权限在执行器，信号与等待在 Manager，最终事件在 State，因果检查在 Trace。

```bash
git checkout v0.27
git diff --stat v0.27..v0.28
git diff v0.27..v0.28 -- src/mini_agent/processes.py src/mini_agent/tools/process.py src/mini_agent/tools/base.py src/mini_agent/permission.py src/mini_agent/state.py src/mini_agent/trace.py
git checkout v0.28
```

没有 `v0.28` tag 时，留在当前开发分支运行本课命令即可；上面固定版本的 diff 和末尾源码链接等 tag 创建后再核对。

## 新增与改动文件

先把职责分开，后面才能看清“谁发信号”和“谁写入生命周期事实”不是同一件事：

| 文件 | 本课作用 |
| --- | --- |
| `src/mini_agent/processes.py` | 只对 Manager 已登记的进程或受管进程组发送信号，并在有界时间内确认退出。 |
| `src/mini_agent/tools/process.py` | 发布 `terminate_process` 和 `kill_process`，参数只有任务专属 `process_id`。 |
| `src/mini_agent/tools/base.py`、`permission.py` | 在执行 handler 前完成参数、任务归属、规划/修复阶段和权限检查，并记录执行尝试。 |
| `src/mini_agent/state.py` | 把确认的主动退出连接到启动 attempt、控制 attempt 和后继 generation。 |
| `src/mini_agent/trace.py` | 只读验证启动、控制、退出和验证之间的引用是否完整。 |
| `src/mini_agent/prompt.py`、`docs/operation/manual.md` | 告诉模型控制结果的含义，以及退出后仍需独立验证。 |

## 版本变更定位

图例：`[旧]` 上一版已有，`[+]` 本版新增，`[~]` 本版修改，`[C]` 主要消费者，`[B]` 本版边界。

上一版的主要链路是观察，控制只能由任务边界清理内部完成：

```text
[旧] start_process
       -> [C] ProcessManager 登记当前任务的进程和进程组
       -> get/read/list/wait_process
       -> State 更新状态、输出位置和自然退出事实
[旧] /new /reset / EOF / exit
       -> 内部 cleanup(task_id)
[B] 模型不能主动结束当前任务的后台进程
```

本版把控制插入执行器与 Manager 之间，并把“确认退出”交回前台 State：

```text
[+] terminate_process / kill_process(process_id)
       -> [~] ToolExecutor
           -> 先检查当前 task_id 是否拥有 process_id
           +-> 无效 ID：unknown_process_id，不询问权限、不预留 generation
           -> planning_gate / repair_gate
           +-> 当前阶段不允许：拒绝，不发送信号
           -> PermissionGate
           +-> 未获准：permission_denied，不发送信号
           -> [C] ProcessManager.control()
                 -> POSIX：向受管进程组发送 TERM 或 KILL
                 -> 有界等待稳定退出
                 +-> still_running：不写最终退出事件
                 +-> 已确认：返回 terminated 或 killed
       -> 每个 call 回灌一个 role=tool
       -> [~] 前台 State.sync_processes()
             -> 唯一最终 ProcessEvent
             -> 后继 generation
             -> verification_required
[C] Trace 只读检查启动 attempt、控制 attempt 和退出事件的引用
[B] 不接受任意 PID；不提供跨任务控制、PTY 或 stdin 写入
```

这个图里有两个“确认”：Manager 确认操作系统资源达到稳定退出边界，State 确认并记录一次可回放的生命周期事实。前者不能替代后者，后者也不能凭工具返回文字猜测前者已经发生。

## 关键流程：终止是一个过程

用户说“把服务停掉”，在操作系统里至少包含四步：

```text
请求       -> 发出 TERM 或 KILL
观察       -> 等待直接子进程和受管进程组变化
收口       -> 等待 stdout/stderr 管道和收集线程结束
记录       -> State 追加一个最终 ProcessEvent
```

如果程序响应 TERM 并自行退出，`terminate_process` 可以返回 `terminated`。如果程序忽略 TERM，到达默认宽限期后仍在运行，工具返回 `still_running`；这时没有退出事件，进程仍会阻止任务完成，Agent 可以继续观察或升级到 `kill_process`。如果调用时它已经自然退出，工具返回 `already_exited`，前台同步会保留自然退出的 `exited` 或 `failed` 事实，而不会把它改写成一次控制退出。

`kill_process` 的流程相同，只是使用强制结束信号。它不是“更快的 terminate”，而是一个需要单独授权、原因不同的控制动作。两个工具都只接受：

```json
{"process_id":"proc-1"}
```

返回的 PID 只用于诊断显示，不能复制出来作为控制参数。

## 核心概念一：先确认归属，再询问权限

### 为什么顺序很重要

控制进程是有副作用的动作。权限提示如果先于任务归属检查，就会出现一种错误体验：模型拿着别的任务或过期任务的 ID，仍然能让用户看到“是否允许结束它”的询问。更重要的是，用户的选择不应该成为访问另一个任务资源的凭证。

所以执行器采用下面的顺序：

```text
参数验证
  -> process_id 是否由当前 task_id 登记
  -> 当前 planning/repair 阶段是否允许控制
  -> PermissionGate 判断 terminate 或 kill 的独立规则
  -> 预留 possible-effect attempt 和 generation
  -> handler 再次核对归属并发送信号
```

归属检查有两层：执行器在授权前检查一次，handler 进入 Manager 后再检查一次。第二次检查用于覆盖检查和真正执行之间的竞态，例如任务切换或资源状态发生改变。

这会产生三种容易对照的结果：

| 情况 | 工具结果 | 是否发送信号 | 是否预留 generation |
| --- | --- | --- | --- |
| 未知、过期或跨任务 ID | `unknown_process_id` | 否 | 否 |
| 有效 ID，但用户拒绝权限 | `permission_denied` | 否 | 否 |
| 有效 ID且获准 | 进入 `terminated`、`killed`、`still_running` 或控制错误 | 可能 | 是，按 possible effect 处理 |

“拒绝”仍然会有对应的 `role=tool` 结果回灌模型，但它不创建一个已经发生的控制事实。工具调用协议完整和副作用没有发生，是两件可以同时成立的事。

### 规划和修复阶段仍然有效

在 `exploring`（只读调查）阶段，控制工具在权限前就被规划闸门拒绝；批准一个计划也不会自动允许控制。若任务已经进入 `diagnosis_required` 或严格的 `verification_required`，直接控制也会被修复闸门拒绝。若停止进程是修复的一部分，必须通过现有的 `recover` 或 `request_replan` 建立合法因果入口，不能用控制工具跳过 Repair Loop（失败后的诊断、恢复和验证循环）。

## 核心概念二：发信号不等于确认退出

### 正常终止和强制结束

`terminate_process` 给进程一次收尾机会。在 POSIX 系统上，Manager 向启动时建立的受管进程组发送 TERM；这能覆盖外层 shell 以及它仍在组内的子进程。`kill_process` 使用强制信号，适合进程忽略 TERM 或无法正常收尾的情况。工具并不把“系统调用没有抛异常”当作完成，因为那只说明请求发出去了。

Manager 的确认还要考虑两个资源边界：

- POSIX 下，受管进程组必须已经消失。外层 shell 先退出而后台子进程还活着时，不能返回成功。
- stdout 和 stderr 收集线程必须读到 EOF，管道才能安全关闭。仍然有人持有管道时，关闭动作可能阻塞或丢失尾部输出。

默认宽限期是 2 秒。它是有界等待，不是保证程序在 2 秒内一定结束的承诺；到期仍在运行就返回 `still_running`，把下一步交给 Agent 或用户决定。

## 实现拆解

下面把“发信号”“确认稳定退出”和“提交 State 事实”分成三个层次。Manager 只负责前两个层次，前台 Runtime 在后面的同步点负责最后一个层次。

### Manager 中的关键控制顺序

下面来自 `v0.28` 的核心控制逻辑，省略了错误消息和平台细节。阅读重点是：先识别已经退出，再发信号；发送后循环确认；到期只报告仍在运行。

```python
fact = managed.refresh()
if fact.status != "running":
    return {"process_id": process_id, "status": "already_exited",
            "exit_code": fact.exit_code}

sent, reason = managed._signal_group(signal_to_send)
if not sent:
    return {"process_id": process_id, "status": "error",
            "error_kind": "control_failed"}

while True:
    fact = managed.refresh()
    if fact.status != "running":
        return {"process_id": process_id,
                "status": "killed" if kill else "terminated",
                "exit_code": fact.exit_code}
    if time.monotonic() >= deadline:
        return {"process_id": process_id, "status": "still_running"}
```

代码中的条件表达式表示两个工具共享同一条确认路径；实际实现根据 `kill` 参数返回对应字符串。这个返回值是工具协议结果，不是 State 自动生成的事件。

### 结果应该怎样解释

| 状态 | 真实含义 | 下一步 |
| --- | --- | --- |
| `terminated` | 正常终止请求后，退出边界已确认 | 同步 State，读取需要的尾部输出，然后重新验证。 |
| `killed` | 强制结束后，退出边界已确认 | 同步 State，说明强制结束，再重新验证。 |
| `still_running` | 本次等待超时，进程仍未确认退出 | 保留活动进程；继续观察或调用 `kill_process`。 |
| `already_exited` | 控制开始前已经自然退出 | 同步自然退出事件，按退出码诊断。 |
| `error/control_failed` | 信号没有成功发出 | 不产生最终退出事件，处理控制错误。 |

Windows 的标准库控制路径只能直接控制子进程，不能证明任意 shell 派生进程树都已结束。结果会带出这个限制；不能把 Windows 上“直接进程退出”解释成任意后代都已消失。

## 核心概念三：退出事件和工具结果各有职责

### 为什么不能让工具结果代替 ProcessEvent

工具结果只回答这一次调用返回了什么；`ProcessEvent` 是 State 中追加的生命周期事实，回答“这个进程最终怎样结束，以及它由哪次启动和哪次控制导致”。一条控制调用仍然只对应一条 `role=tool` 消息；退出事件是独立状态记录，不应冒充第二次工具回复。

确认退出后，前台同步会把信息连接起来：

```text
+------------------+       +----------------------+       +--------------------+
| start_process    | ----> | ProcessEvent        | ----> | next generation    |
| start attempt    |       | terminated / killed |       | verification 必须重做 |
+------------------+       | caused_by_control  |
             ^              | attempt_id         |
             |              +----------------------+
             |
      terminate/kill attempt
```

一个受控退出事件至少带有：

- `start_attempt_id`：哪个 `start_process` 创建了进程。
- `caused_by_control_attempt_id`：哪一次获准的终止或强制结束触发了它。
- `generation_id`：退出发生在哪个验证代次。
- `exit_code`、stdout/stderr 累计位置：退出时的可解释结果。

同一进程只有一个最终事件。若 `terminate_process` 返回 `still_running`，此时还不能写 `terminated` 事件；之后的 `kill_process` 确认退出，才由同步点追加 `killed` 事件。若进程在控制之前已自然退出，最终事件保持 `exited` 或 `failed`，控制 attempt 只记录“调用时已经退出”。

### Trace 为什么要检查引用

Trace（任务轨迹）只从 State 快照重建，不重新访问 Popen、进程组或管道。它会检查控制退出事件是否引用存在的启动 attempt 和控制 attempt，检查进程 ID、工具名和 generation 是否一致。把 `caused_by_control_attempt_id` 改成一个不存在的 ID，Trace 应报告 `incomplete`；它不会猜测缺少的控制事实。

## 退出后为什么必须重新验证

控制工具是 `possible` effect（可能改变环境的动作）。在获准执行时，State 会记录执行 attempt，并让旧验证失效；确认退出后又会由退出事件开启一个后继 generation。这样做是因为进程在运行期间可能写文件、生成缓存、占用端口或修改外部环境。

所以这条链才允许最终收口：

```text
控制请求获准
  -> 进程稳定退出
  -> State 记录 terminated/killed ProcessEvent
  -> 后继 generation + verification_required
  -> 独占 run_shell(purpose="verification")
  -> 计划步骤、失败修复和其他完成条件都满足
  -> 才可能 done
```

“返回 `terminated`”只证明后台资源已到退出边界；它不证明代码正确，也不证明工作区仍符合任务目标。

## 工具回合中的协议行为

模型可能在同一条 assistant 消息中请求 `terminate_process` 和 `get_process`。由于控制是可能有副作用的动作，这一轮会按模型给出的顺序串行执行；两个 call 都得到对应的 `role=tool` 结果，全部结果回灌后才请求下一轮 LLM。第二个 `get_process` 看到的可以是 `terminated`，但它不是因为工具结果被“合并”，而是因为第一个控制调用已经完成并在随后的同步点提交了真实状态。

## 运行与观察

下面的命令适用于 Bash/zsh，运行本版控制相关场景：

```bash
PYTHONPATH=src python -m pytest -q tests/test_process_control_v028.py
```

把测试现象按下面的方式阅读：

| 现象 | 它说明的问题 |
| --- | --- |
| 正常终止只产生一条 `terminated` 事件 | 发送信号和前台事件提交是幂等且可区分的。 |
| 忽略 TERM 的程序先返回 `still_running`，再由 KILL 返回 `killed` | 工具没有把信号发送误报为退出。 |
| 已自然退出的进程返回 `already_exited`，仍保留自然事件 | 控制动作不会改写已经发生的生命周期事实。 |
| 未知 ID 和权限拒绝都不发送信号、不预留 generation | 归属检查和权限闸门位于副作用之前。 |
| 损坏的控制引用让 Trace 变为 `incomplete` | 回放只验证已有事实，不替缺失引用补因果。 |
| 控制后旧 verification 消失，新 verification 才恢复 | 退出可能改变环境，控制结果不能代替独立验证。 |

也可以让 Agent 启动一个短暂服务，读取到它的输出后请求 `terminate_process`。先观察工具返回的状态，再看 State 中是否出现唯一最终事件；如果进程忽略 TERM，应该继续保持活动状态，而不是直接进入完成。

## 为什么这样设计

把 `terminate` 和 `kill` 分成两个工具，是为了让模型和用户都能看见升级关系：先给程序正常收尾机会，只有仍未退出时才选择强制结束。两者各自经过 PermissionGate，避免普通的启动授权或一次终止授权被扩大成任意进程控制权限。代价是一次控制可能需要两个工具回合，而且用户需要理解 `still_running` 并决定是否升级。

把确认退出放在 Manager，把最终事件提交放在前台 State，是为了同时处理操作系统竞态和 Agent 协议边界。代价是退出事实不会在后台线程刚发现 `poll()` 变化时立即进入快照，而要等安全点同步；这换来了幂等事件、完整的 `role=tool` 回灌和只读 Trace。

任务归属只允许控制当前 CLI、当前任务登记的进程，能避免把一个模型生成的 ID 当作系统级进程管理器。代价是不能控制外部启动的 PID，也不能在新的 CLI 会话重连旧进程。

## 设计边界

- `terminate_process` 和 `kill_process` 只接受当前任务登记的 `process_id`；PID 只用于诊断。
- 两个工具都是 `possible` effect，默认按独立的控制权限处理；未知 ID、阶段拒绝和用户拒绝不会发送信号。
- `terminated` 或 `killed` 表示本版确认到的退出边界；POSIX 还检查受管进程组和两条输出管道，Windows 会说明无法证明任意派生树结束。
- `still_running` 不产生最终退出事件，也不允许任务完成；`control_failed` 不伪造退出事实。
- 控制后必须在新的 generation 做独立 verification；控制成功不是测试通过。
- `exploring`、待批准计划、诊断和严格验证阶段的限制继续有效；进程控制不能绕过 `recover` 或 `request_replan`。
- 本版不提供 stdin、PTY、跨会话重连或任意 PID 管理；有界管道 stdin 在 `v0.29` 讨论。

## 本版特性、下一课与代码索引

本课让 Agent 能在当前任务中请求正常终止或强制结束，并把“信号已发出”“仍在运行”“已经稳定退出”记录成不同结果。下一课会讨论有界管道 stdin：如何让等待输入的后台程序接收少量文本，以及为什么写入结果同样不能代替进程退出和最终验证。

固定源码入口：

- [进程信号与稳定退出确认](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.28/src/mini_agent/processes.py)
- [控制工具协议](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.28/src/mini_agent/tools/process.py)
- [执行器的归属与授权顺序](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.28/src/mini_agent/tools/base.py)
- [状态事件与 generation](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.28/src/mini_agent/state.py)
- [只读 Trace 校验](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.28/src/mini_agent/trace.py)
