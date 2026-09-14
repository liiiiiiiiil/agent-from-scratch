# 阶段八：进程管理（Process Management）实施计划

> 状态：`v0.26`–`v0.27` 已完成（后台启动与任务边界；增量观察、退出失败事实和等待交接）；`v0.28`–`v0.29` 仍按下列版本切片规划
> 前置阶段：阶段六可靠执行（`v0.17`–`v0.21`）与阶段七结构化计划（`v0.22`–`v0.25`）
> 建议版本范围：`v0.26`–`v0.29`；`v0.26`–`v0.28` 为核心，`v0.29` 为可选增强

## 1. 目标与定位

当前 `run_shell` 用 `subprocess.run` 等待命令结束，30 秒超时后返回，适合一次性命令。开发服务器、文件监听器和长时间构建无法按这个模型跨 Agent 轮次运行。阶段八要回答一个问题：**Agent 启动的程序没有立即结束时，能否在当前任务中继续工作，并在后续轮次观察和控制它？**

目标流程：

```text
start_process → 返回任务内 process_id → Agent 继续执行
                    ↓
             get/read_process → 状态、增量输出、退出码
                    ↓
             terminate/kill → 确认退出 → 独立验证或任务收口
```

保留 `run_shell` 处理有限时间命令；后台进程使用独立工具和 `ProcessManager`。核心运行时只用 Python 标准库。

## 2. 范围与非目标

### 2.1 本阶段范围

- 在当前 CLI 进程、当前任务内启动和管理后台进程；`run_shell` 的同步行为保持不变。
- 持续排空并有界保存 stdout/stderr；后续轮次按游标读取新增输出、查询状态和最终退出码。
- 仅对本任务登记的进程执行 terminate、kill 和任务边界清理；在平台支持的范围内清理 shell 派生子进程。
- 把进程元数据、生命周期事件及其因果引用接入 State、Failure Model、Trace 和完成判定。
- 为没有新输出的长时间等待提供有界等待与非终态交接，避免反复轮询触发停滞护栏或耗尽轮次。
- `v0.29` 可选增加管道 stdin 输入，并在具备可靠跨平台方案时评估 PTY。

### 2.2 本阶段不做

- 不做跨进程持久化或重连；CLI 退出后不提供 session resume。
- 不做定时任务、Docker/通用沙箱、远程进程管理或多 Agent 共享进程。
- 不提供任意 PID 操作、shell 副作用回滚或“终止进程即可撤销它做过的修改”的承诺。
- 不把 PTY、交互式 shell、tmux 或持续自动调度作为核心版本的完成条件。
- 不引入第三方核心运行时依赖，也不让进程声明改变现有工具的授权语义。

## 3. 先确定的设计边界

### D1：进程归 `ProcessManager` 管，State 保存可解释的事实

建议增加 `src/mini_agent/processes.py`：`ProcessManager` 持有 `Popen` 句柄、输出缓冲区和读取位置；工具层只负责参数、权限和结果转换。Runtime 在 `begin_task` 分配不复用的任务标识，不能只用可能相同的任务文本区分归属。State 保存任务内 `process_id`、所属任务、启动 attempt、generation、状态、启动/结束时间、退出码及有界命令摘要；不把 `Popen`、文件描述符或线程放进可快照 State。History 仍只承载工具调用协议，裁剪后进程事实依然可见。

`process_id` 由 Runtime 分配，不能让模型传入系统 PID 控制任意进程。只接受当前任务注册且仍有效的 ID；未知、过期或跨任务 ID 返回结构化工具错误。状态先保持 `running`、`exited`、`terminated`、`failed` 四类：自然零退出为 `exited`，非预期非零退出为 `failed`，确认主动停止后为 `terminated`。启动失败没有可运行进程，必须记录为启动尝试失败；进程 ID 不复用。

### D2：启动时就建立有界输出收集

`Popen` 使用管道时，哪怕模型尚未调用 `read_process`，也必须持续排空 stdout/stderr，否则子进程可能因管道写满而卡住。收集器只维护有界缓冲区，不在后台线程直接改写 State。按流保存单调递增的字节位置；`read_process(process_id, max_chars)` 返回上次成功读取后的新增内容和新位置。缓冲区淘汰旧内容时分别报告 stdout/stderr 的 `output_gap` 与丢失字节数，不假装日志完整。stdout/stderr 分开保留，结果和错误摘要同样有长度上限。

初始资源上限建议为：同一任务最多 4 个活动进程，每个进程每条输出流最多保留 64 KiB，单次工具回复正文最多 8000 字符。按字节读取并增量解码 UTF-8，非法字节以替代字符表示；解码边界不能造成输出重复或吞字。额度应集中配置并进入操作手册，具体值在 `v0.26` 实现时以真实测试冻结。日志是未经信任的工具数据，不能提升为系统指令；State 与 Trace 只保留有界摘要和位置，不复制完整日志。

### D3：进程生命周期有独立事实，不能借启动结果宣称完成

`start_process` 经过 PermissionGate 后、调用 handler 前按 `effect_class=possible` 预留 generation。启动成功只证明进程已创建，不证明命令成功；随后观察到的退出码必须作为引用启动 attempt 的独立生命周期事实，且同一次退出只记录一次。非预期的非零退出生成 FailureEvent；失败归属观察时的当前 generation，同时引用启动 attempt，不追溯修改已关闭 generation 的状态。进程随后退出的事实不能伪装成原 `start_process` 工具调用的第二个返回值或第二次 `role=tool` 消息。日志里的 traceback 或 `read_process` 本身不自动构成失败。主动终止与异常退出应区分，进程控制失败不能假装已经清理。

运行中的进程仍可能改变环境。启动时使旧验证失效；从 `v0.26` 起，仍有活动后台进程时不能凭现有证据判定任务完成。进程退出或被终止后，不能复用启动前或运行期间的验证证据完成任务。开发服务器运行期间可以做连通性检查，但检查结果不能单独证明最终环境稳定。观察到最终退出时，Runtime 为退出前仍可能发生的写入开启后继 generation；若先调用 terminate/kill，这些获准的控制动作本身也按 `possible` 预留 generation，不能合并或回退已预留的代。随后需在稳定边界上独立验证。进程状态或日志不充当 verification。

`v0.26` 就实行活动进程的保守完成护栏，并在自然退出被同步时开启后继 generation；`v0.27` 补齐可查询的退出事件和失败记录；`v0.28` 补齐控制动作与最终完成准入。开启 generation 的是已登记进程的异步生命周期事实，不是把 `get/read_process` 的 `effect_class=none` 偷偷改成 `possible`。已有的 `run_shell(purpose="verification")` 仍是验证入口，不能与副作用工具同轮，也不能借“进程已启动”绕过 Repair Loop。已观察到的退出事件若与另一活动失败或 `verification_required` 发生冲突，不替换其 active failure、不悄悄清除验证义务；先保留两个来源并有界阻塞，等待用户决定新的任务路径。这里采用保守规则，不尝试自动合并两个修复流程。

### D4：权限与只读规划边界贯穿所有版本

`start_process` 按命令模式独立授权，不能因为同一命令获准用于 `run_shell` 就自动获准后台运行；终止、强制杀死和写入 stdin 各有工具准入。`get/read/list` 只观察本任务已登记进程；`start/terminate/kill/write` 均为可能有副作用的动作，经过现有计划阶段与执行器双层准入。`exploring` 和 `--plan` 的只读边界不能被后台启动绕过；计划批准也不代替工具授权。

现有 PermissionGate 只对 `run_shell` 提取命令模式，新增 `start_process` 时必须显式提取其 `command` 并配置独立规则；遗漏规则会退回默认 `ask`，不能继承 `run_shell` 的 `allow`。`cwd` 在启动前解析和校验；它只是工作目录，不提供文件系统隔离。内部 `cleanup()` 是已授权启动动作的资源收尾，只能作用于本任务登记的进程，不是模型可用的权限捷径。任何被拒绝的 tool call 仍需收到对应结果，且不启动进程、不预留 generation。

### D5：任务和 CLI 边界都要清理

从 `v0.26` 起就提供内部 `cleanup()`：退出 CLI、`/new`、`/reset` 或异常离开任务时，对本任务仍运行的进程先请求正常终止，有界等待后强制结束，并记录清理结果。正常工具调用只控制 `ProcessManager` 登记的进程；POSIX 平台优先使用进程组清理 shell 子进程。Windows 标准库对任意子进程树的控制能力有限，应明确实际支持的清理范围；无法确认子进程已结束时报告清理不完整，不能宣称完全清理。进程崩溃或断电后的自动清理不在本阶段保证内。

清理必须先于 `AgentState.begin_task/reset_task` 清空旧任务事实；否则会丢失 process ID 与任务归属。清理失败应在旧任务留下可见结果，CLI 也应提示用户，不能静默宣称无遗留。输出收集线程、管道与 `Popen` 句柄都要在退出后有界回收。

### D6：长时间无输出是等待，不是进展

`get_process` 和 `read_process` 反复得到相同状态或空日志，不产生新的调查事实，也不能靠时间流逝清零阶段七的停滞计数。`v0.26` 起，当模型在有活动进程的任务中暂停并输出无工具调用文本时，Runtime 可用 `awaiting_process` 非终态交回 CLI，显示进程 ID 和继续方式，不把它判成 `done` 或立刻耗尽完成提醒。其他未满足的计划/修复义务仍需在交接中说明，不能由这个状态消除。`v0.27` 增加有上限的 `wait_process(process_id, timeout_ms)`，在退出或出现新输出时返回；达到等待上限而进程仍运行时，也进入同一交接。下一次用户输入可恢复原任务并再次观察；这不算 `blocked` 或 `FailureEvent`，也不自动调用 LLM。等待时长有单次上限，不能无限占住一轮工具调用。

等待交接必须是显式 Runtime 状态，而非模型凭一段“还在运行”文本绕过 completion reminder。进程运行期间不自动重启 LLM；如果进程在用户回来前退出，下一次进入任务时先同步其状态并记录一次退出事实。`MAX_ITERATIONS` 继续保护单次 agent loop，不作为后台进程的运行时长上限。

### D7：状态读取是只读，状态转移由 Runtime 提交

后台收集器只维护原始进程句柄和有界输出缓冲；进程退出、日志游标提交、FailureEvent 和 generation 更新由前台 Runtime 在 `get/read/list/wait`、用户继续任务和完成判定前的安全点统一提交。`poll()` 不能因两次查询给同一进程创建两个退出事件。只读观察工具可获取已登记进程的事实，但不能靠 `read_process` 清除失败、推进计划步骤或产出验证证据。

前台同步时要先提交进程生命周期变化，再向模型组装可见状态。若一次 assistant 回复同时包含多个只读观察工具，仍遵守现有“每个 call 一个结果、全部回灌后再下一轮”的协议；对同一进程的游标读取要串行化，避免并发读取重复或跳过输出。可以把读取游标改为调用方提供的显式位置，或由 Manager 锁保证同一任务的顺序，实施时选一种并写测试。

## 4. 工具协议与状态模型

### 4.1 模型可见工具

| 工具 | 首次版本 | 关键参数 | 结果与副作用 |
|---|---|---|---|
| `start_process` | `v0.26` | `command: str`, `cwd?: str` | 通过授权后启动；返回 `process_id`、诊断用 PID、状态和启动 attempt；`possible`。 |
| `get_process` | `v0.27` | `process_id: str` | 返回当前状态、退出码和输出位置，不消费日志；`none`。 |
| `read_process` | `v0.27` | `process_id: str`, `max_chars?: int` | 分别返回 stdout/stderr 新增片段、下一位置及缺口；`none`。 |
| `list_processes` | `v0.27` | 无 | 仅列当前任务登记的有界元数据，不含日志正文；`none`。 |
| `wait_process` | `v0.27` | `process_id: str`, `timeout_ms?: int` | 有界等待新输出或退出；超时返回 `still_running`，可交回 CLI；`none`。 |
| `terminate_process` | `v0.28` | `process_id: str` | 请求正常退出并有界确认；未退出则明确报告仍在运行；`possible`。 |
| `kill_process` | `v0.28` | `process_id: str` | 强制结束并确认结果；`possible`。 |
| `write_process` | `v0.29`，可选 | `process_id: str`, `input: str` | 向仍运行的进程写有界 stdin；`possible`，不得用来提交 verification。 |

模型只获得已发布版本的工具 schema。工具结果使用结构化、长度受限的 JSON；非零进程退出不能靠解析日志文字或沿用 `run_shell` 的 `[exit=N]` 字符串规则判断。示例：

```json
{"process_id":"proc-1","pid":12345,"status":"running","start_attempt_id":"a-3"}
```

```json
{"process_id":"proc-1","status":"running","stdout":"ready\n","stderr":"","next_stdout_offset":6,"next_stderr_offset":0,"output_gap":false}
```

`pid` 仅供诊断显示，不作为任何控制工具的参数。启动使用与当前 `run_shell` 一致的命令字符串语义；`v0.26`–`v0.28` 的 stdin 默认关闭，避免程序等待输入却表现为无输出卡住。`wait_process` 只报告有新输出可读或进程已退出，不消费读取游标；单次等待建议不超过 30 秒，超时不是工具失败或进程失败。

### 4.2 Runtime 记录

```text
ProcessRecord                         # State 中可快照的当前投影
- process_id, task_id
- start_attempt_id, start_generation_id
- command_summary, cwd_summary, pid
- status: running | exited | terminated | failed
- started_at, ended_at?, exit_code?
- stdout_offset, stderr_offset        # 只保存位置，不保存日志全文
- terminal_event_id?

ProcessEvent                          # append-only 生命周期事实
- event_id, process_id, kind: started | exited | terminated | killed | cleanup_failed
- generation_id, start_attempt_id
- caused_by_control_attempt_id?, exit_code?, reason?

ProcessWaitState                      # 仅在等待交接期间存在
- process_id, reason: no_new_output | still_running
- last_observed_event_id?, last_stdout_offset, last_stderr_offset

ProcessManager 私有运行态
- process_id -> Popen / 进程组标识 / 收集器
- stdout/stderr 有界缓冲、总字节位置、读取游标和同步锁
```

State 只允许 Runtime 写入 `ProcessRecord`、`ProcessEvent` 和等待状态；模型不能提供状态、退出码、FailureEvent 或 generation。启动 attempt 继续由 Executor 记录；进程退出是新的 Runtime 事实，不创建伪造的工具调用。Trace 只消费 State 的 append-only 事件和公开快照，不能在回放时重新 `poll()`、读管道、发信号或调用 PermissionGate。

不变量：同一任务的 `process_id` 唯一；每个成功启动恰有一个 `started` 事件，每个进程至多一个最终退出事件；最终退出必须引用存在的启动 attempt；`running` 之外不得接受 stdin；只有确认退出后才能标记 `terminated`；读游标单调递增，日志缺口不能隐去；旧任务 ID 不可用于新任务；有活动进程或待验证 generation 时不得判定 `done`。

## 5. 版本切片

### 5.1 `v0.26` Background Process（已完成）

目标：建立最小的非阻塞进程生命周期。此版允许 Agent 启动后继续执行其他工具；观察和显式控制留给后续版本。

已完成工作：

1. 新增 `ProcessManager`、任务内 ID 分配、`Popen` 启动与当前任务注册；设置独立进程组或平台对应的最小控制边界。
2. 新增 `start_process` 工具和独立命令模式权限；在 handler 前按 `possible` 预留 generation。启动失败保留原失败结果与 generation，不生成成功 ProcessRecord。
3. 从启动时就分别排空 stdout/stderr，维持固定内存上限；stdin 暂时关闭。
4. 在 State 加入最小 `ProcessRecord` 投影；上下文注入当前任务的 ID、状态和有界摘要，让历史裁剪不丢失进程事实。
5. 在工具回合和完成判断前同步自然退出，记录最小最终事件，开启后继 generation 并要求重新验证；活动进程阻止错误 `done`，模型暂停时可用 `awaiting_process` 交回 CLI。
6. CLI 主循环用 `finally` 清理进程；`/new`、`/reset` 在 State 重置前清理旧任务。清理先 terminate、有限等待后 kill，并报告失败。

验收重点已覆盖：启动服务立即返回，下一工具回合可以继续；日志再多也不会堵住管道；退出和切换任务执行有界清理并报告遗留风险；简单 `run_shell` 仍按原有超时与返回协议工作。观察工具、主动控制和自然非零退出对应的 `FailureEvent` 留给后续版本。

### 5.2 `v0.27` Process Observation（已完成）

目标：跨轮次获得进程的真实状态、增量输出和最终退出码，不让轮询消耗无意义的 Agent 回合。

主要工作：

1. 新增 `get_process`、`read_process`、`list_processes` 和有界 `wait_process`；同一进程的游标读取按调用顺序提交。
2. 输出位置按字节计数，分别保留 stdout/stderr；处理 UTF-8 跨块、输出截断、空输出、已退出后仍有尾部输出和缓冲淘汰造成的缺口。
3. 扩充 `ProcessEvent` 的可查询元数据和幂等退出同步；自然非零退出生成引用启动 attempt 的 FailureEvent，且不把观察工具本身判为失败。
4. 将新状态、新日志与重复空结果交给阶段七停滞护栏区分；无变化的轮询不算新事实。`wait_process` 超时进入 `awaiting_process` 交接，下一次用户输入先同步进程再继续原任务。
5. 将可读的进程事实加入 Structured State 和 Trace；日志正文仅随本次工具结果有界回灌，不放入长期 State。

验收重点：两次读取无重复、日志丢失有缺口标记、自然退出仅记一次；长时间无输出能等待并交回 CLI，不被误判为任务完成或异常失败。

### 5.3 `v0.28` Process Control

目标：让 Agent 可以有授权地结束自己启动的程序，并使整个任务按现有执行与验证合同收口。

主要工作：

1. 新增 `terminate_process` 与 `kill_process`；两者先按 ID 确认当前任务所有权，再经各自 PermissionGate 准入，获准时按 `possible` 预留 generation。
2. `terminate` 发送正常终止请求并有界等待，超过等待期返回 `still_running`；`kill` 强制结束并确认。确认前不得把状态写成 `terminated`。
3. 对 shell 派生子进程实现平台可保证的清理；无法确认清理完整时报告限制或失败，终止失败和权限拒绝不得影响其他进程。
4. 显式控制、自然退出和内部 cleanup 各留独立原因；内部 cleanup 在任务边界执行，不伪造模型 tool call。
5. 完成活动进程、退出后新 generation、独立 verification、Repair Loop、Plan Mode 和最终 `done` 的组合规则；Trace 可回放启动—观察—控制—验证链及损坏引用。

验收重点：正常退出、拒绝终止、终止后仍运行、强制结束和控制失败都有可区分结果；清理后再独立验证才能完成，旧证据不会跨代复用。

### 5.4 `v0.29` Interactive Process（可选增强）

目标：支持需要少量 stdin 的命令，而不把阶段八变成完整终端模拟器。

主要工作：

1. 新增管道 stdin 的 `write_process`；限制单次写入大小和可写状态，明确关闭 stdin、进程提前退出和写入失败的结果。
2. 输入经单独权限检查和 `possible` generation；写入内容不进入 State、Trace 或普通终端摘要。输入之后仍由 `read_process` 观察输出。
3. 评估 PTY 是否值得单独发布：终端尺寸、回显、控制字符、Windows 行为、退出清理和验证证据均需有可运行测试。若无法保持教学切片清晰，停在管道 stdin，并把 PTY 移到后续阶段。

验收重点：一个等待文本输入的简单子进程可被驱动并正常结束；输入不会意外回显到审计摘要；不支持的交互式 CLI 返回明确能力边界。

## 6. 与现有状态机的组合

进程生命周期与 Planning State、Repair Loop 正交，不新增独立 Planner 或取代原有任务状态。准入按现有规则取更严格的一侧：

| 当前状态 | 允许的进程动作 | 关键限制 |
|---|---|---|
| `direct/executing` + `idle` | 按权限启动、观察、控制；可单独验证 | 活动进程阻止最终完成，退出后需要新验证。 |
| `exploring` | 仅观察已登记进程的状态和输出 | 不允许启动、终止、强制结束、写入 stdin 或 verification。 |
| `awaiting_approval` | CLI 等待用户决定；内部可同步事实和清理 | 模型工具调用继续被拒绝；批准计划不自动授权启动。 |
| `diagnosis_required` | 只读观察用于诊断 | 控制动作必须走现有受限恢复入口或由用户明确决定；不能以终止工具绕过 Repair Loop。 |
| `verification_required` | 下一模型工具回合仍为独占 `run_shell(purpose="verification")` | 若期间出现新的进程退出冲突事实，保守阻塞并保留原失败和验证义务。 |
| `awaiting_process` | CLI 等待用户下一次输入 | 保持原任务与计划，恢复前先同步进程；不自动调用 LLM。 |

控制工具与 RecoveryAction 的关系在 `v0.28` 必须明确：诊断中的进程终止若是修复的一部分，先通过现有 `recover` 或 `request_replan` 建立合法因果入口；不能直接放开新的 `possible` 工具。它不取消单轮全工具结果回灌、独立 verification 或 PermissionGate。

## 7. 测试与验收

### 7.1 单元与集成测试

- **启动与授权**：启动返回快而非等到退出；权限拒绝、非法参数、无效 cwd 和超过并发上限时不创建进程；获准启动才预留 generation。
- **输出**：高频 stdout/stderr 不阻塞；多字节字符跨块、空输出、超长日志、缺口标记和重复读取均按字节位置正确；日志不进入 State 全量快照。
- **状态**：自然零退出、非零退出、退出码与启动 attempt 关联；多次查询不重复记录退出或 FailureEvent；多个进程互不串游标。
- **等待**：有新输出、进程退出、等待超时分别返回明确结果；超时交接不计作成功进展、失败或终态，下一次用户输入恢复原任务。
- **控制**：仅能控制当前任务 ID；TERM、KILL、超时未退出、进程已退出、拒绝与控制失败都不伪造状态；清理管道和收集线程。
- **状态机**：Explore、待批准计划与诊断/验证阶段均执行正确准入；启动与控制后 generation、验证隔离、完成提醒和停滞护栏保持原约束。
- **协议与回放**：同轮多个工具调用各有一个 `role=tool` 结果；Trace 只读、引用完整，损坏或跨任务进程事件标记不完整。

测试用标准库启动短生命周期脚本与本地 HTTP 服务，避免依赖外部包或固定端口。平台差异用能力检测与显式跳过处理，不能把“当前机器能杀外层 shell”当作对子进程清理的证明。

### 7.2 阶段级 E2E

1. 启动本地 HTTP 服务，立即拿到 `process_id`；Agent 执行其他工具，后续读取就绪输出并检查响应，终止服务，确认退出，再完成独立验证。
2. 启动两秒后非零退出的进程；后续查询得到退出码、唯一 FailureEvent 和完整启动因果引用，模型按现有诊断或重规划路径处理。
3. 启动持续输出的进程；多次增量读取不重复、不无限涨内存；缓冲淘汰后返回明确 `output_gap`。
4. 启动长时间无输出的进程；有界等待后交回 CLI，用户继续时仍可观察原进程，不因为空轮询被标记 `done` 或耗尽 Agent 轮次。
5. 进程仍运行时尝试最终回复；Runtime 不接受完成。停止进程后旧验证仍无效，新的独立验证才允许收口。
6. `/new`、`/reset`、EOF/正常退出及异常路径均清理当前任务可控制的进程；旧 ID 对新任务无效，且不误伤外部 PID。平台无法确认子进程清理时明确报告。

### 7.3 阶段完成定义

- [x] `v0.26`、`v0.27` 有独立教程、变更记录和可运行测试；`v0.28` 仍待实现，`v0.29` 是否发布按管道 stdin 验收结果决定。
- [ ] 保留现有 `run_shell` 短命令行为，后台命令可跨 Agent 轮次启动、观察、控制。
- [ ] 所有后台进程受任务归属、资源上限、独立权限和任务边界清理约束。
- [ ] 进程退出、失败、控制和最终验证有可回放的因果关系；运行中和退出后的旧证据不能错误完成任务。
- [ ] 默认测试套件、教程检查、README 检查和阶段级 E2E 全部通过；核心仍仅依赖标准库。
- [ ] 各版本 tag 由用户手动创建后，完成相应的教程事实检查和发布验收。

## 8. 版本依赖关系

```text
v0.25 计划轨迹回放与验收
  ↓
v0.26 后台启动、任务归属、输出排空与基础清理
  ↓
v0.27 增量观察、退出事实、等待交接
  ↓
v0.28 显式控制、完整清理、验证与 Trace 闭环
  ↓
v0.29 可选管道 stdin；PTY 另行决策
```

`v0.26` 不要求实现完整观察 UI，但不能推迟输出排空、清理和完成护栏；`v0.27` 不开放任意 PID 控制；`v0.28` 不把信号发送成功当作进程已退出。每版保留一个可教学的主题和可独立验收的行为。

## 9. 文档与发布同步

实现各版时同步对应教程、`docs/tutorials/README.md`、`docs/operation/manual.md`、`CHANGELOG.md`、`README.md` 学习路径、`pyproject.toml` 版本信息和本计划状态；仅在运行时硬约束确实变化时更新 `AGENTS.md`。教程面对刚接触 Agent 的读者，先说明长命令为什么会阻塞，再介绍工具、进程事实和验证规则。主 README 的阶段名与版本主题遵守治理写作规范。

每版交付前运行：

```bash
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python scripts/check_tutorials.py
PYTHONPATH=src python scripts/check_readme.py
```

教程完成且用户手动创建对应 tag 后，再运行依赖本地 Git 对象的事实检查。助手不得创建、移动、覆盖、删除或推送 tag。

## 10. 阶段完成后的能力边界

阶段八核心完成后，mini_agent 可以让长期运行程序留在当前任务中：启动后继续工作，随后读取新增输出和最终状态，有授权地结束进程，并以退出后的独立验证收口。它仍只管理当前 CLI 进程内登记的任务进程，不提供崩溃恢复、通用沙箱、自动守护服务或完整终端模拟。
