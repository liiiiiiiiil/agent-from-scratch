# 第 28 课：控制后台进程

上一课：[观察后台进程](27-process-observation.md) · [教程总览](README.md) · 下一课：按需扩展

代码快照：`v0.28` · 相邻差异：`v0.27..v0.28` · 示例命令环境：Bash/zsh

`v0.28` tag 由维护者手动创建。tag 尚未创建时，在当前开发分支阅读和运行本课；固定源码链接和跨版本 diff 须在创建 tag 后核对。命令行首条任务完成后，CLI 仍会进入交互循环。

## 本课目标

上一课让 Agent 看见一个后台程序是否仍在运行，却没有给它主动结束程序的入口。比如开发服务器已经完成检查，Agent 只能等它自行退出，或者等到切换任务时由 CLI 清理。本课增加两个受授权的控制工具。读完应能解释：发出终止信号为什么还不等于进程退出，以及确认退出后为什么还需要重新验证工作区。

## 前置条件与版本切换

需要基础 Python 和 Bash/zsh；先理解上一课的任务专属 `process_id`、状态查询和有界等待。已有 tag 时可用下面的命令看真实差异；没有 `v0.28` tag 时，留在当前开发分支。

```bash
git checkout v0.27
git diff --stat v0.27..v0.28
git diff v0.27..v0.28 -- src/mini_agent/processes.py src/mini_agent/tools/process.py src/mini_agent/state.py
git checkout v0.28
```

差异应显示新增控制入口、进程确认、事件记录和只读轨迹检查。切换代码是为了让下面的行为与课程内容对应。

## 新增与改动文件

| 文件 | 本课作用 |
| --- | --- |
| `processes.py`、`tools/process.py` | 发送受限信号，有界确认进程退出，并向模型提供两个控制工具。 |
| `tools/base.py`、`permission.py` | 在询问授权前检查归属，再按工具分别授权与记录执行尝试。 |
| `state.py`、`trace.py` | 把确认退出连接到控制尝试、后继验证代次，并只读检查引用。 |
| `prompt.py`、`docs/operation/manual.md` | 告诉模型和使用者控制结果与最终验证的边界。 |

## 版本变更定位

图例：`[旧]` 上一版已有，`[+]` 本版新增，`[~]` 本版修改，`[C]` 主要消费者，`[B]` 本版边界。

上一版只有观察和任务边界清理：

```text
[旧] start_process -> ProcessManager 登记当前任务进程
          +-> get/read/list/wait_process -> [C] Agent 观察状态与输出
          +-> 自然退出 -> State 最终事件 -> 后继 generation
[旧] /new、/reset、退出 CLI -> 内部 cleanup
[B] 模型不能主动结束进程
```

本版在执行器的授权边界加入控制，并等到稳定退出才写入最终事实：

```text
[+] terminate_process / kill_process
       -> [~] 执行器检查当前 task_id 与 process_id
           +-> 无效 ID：unknown_process_id；不询问权限
           +-> 有效 ID：各自 PermissionGate -> [C] ProcessManager 发信号并有界确认
                                         +-> still_running：保留活动状态
                                         +-> 确认退出：[~] 前台 State 同步唯一最终事件
                                                        -> 后继 generation -> 独立 verification
[C] Trace 只读检查启动、控制、退出和验证的引用
[B] 不能控制任意系统 PID；Windows 不能证明任意 shell 派生树退出
```

## 关键流程

从模型发起控制到确认退出，顺序是“检查归属 → 授权 → 发送信号 → 等待稳定状态 → 提交事件”。归属检查放在授权前，避免为无权控制的进程弹出权限询问。最后一步由前台 Runtime（负责一轮工具结果与任务状态的运行部分）执行，而不是由读输出的后台线程修改任务状态。

## 实现拆解

`terminate_process(process_id)` 发送正常终止请求，给程序机会自行收尾。`kill_process(process_id)` 发送强制结束信号。两者都是可能影响工作区的工具，所以各有独立授权，默认需要用户确认。有效调用获准后会留下一个执行尝试（attempt，即一次实际工具执行的记录）并预留新的 generation（副作用后的验证代次）。未知、过期或跨任务的 ID 则在授权前被拒绝，不创建执行尝试。handler 还会再次核对归属，防止授权期间任务状态变化。

发信号只表明请求已送出。程序可能忽略正常终止，shell 启动的后代也可能仍在运行或持有输出管道。Manager 因此最多等待现有的 2 秒宽限期，并同时检查直接子进程、受管进程组和 stdout/stderr 是否结束。只有这些条件稳定满足时才返回 `terminated` 或 `killed`。结果为 `still_running` 时，程序仍属于活动进程，Agent 可以继续观察或请求强制结束；这次结果不是退出事实。

下面的调用形状强调模型使用任务专属 ID，而非系统 PID：

```json
{"process_id":"proc-1"}
```

若程序已自行退出，工具会返回 `already_exited`；前台同步点记录真实的自然退出。控制信号失败则返回 `control_failed`，不伪造最终事件。在 Windows 上，结果会说明无法确认任意 shell 派生进程树。这里的确认范围与上一课任务边界清理的范围一致。

## 退出后的记录与验证

进程被确认结束后，前台运行循环把结果写入 State：同一进程只有一条最终 `ProcessEvent`（进程生命周期事实），种类是 `terminated` 或 `killed`。它同时引用启动 attempt 与这次控制 attempt；下一次同步不会再写第二条。随后开启一个新的 generation，让进程运行期间的旧验证证据失效。每个工具调用仍只获得一条 `role=tool` 结果；退出事件是独立状态事实，不冒充第二次工具回复。

只读 Trace 从 State 快照重建因果链，并检查控制 attempt 的工具名、进程 ID 和 generation 是否与最终事件吻合。引用断裂时，轨迹标记为不完整，不推测缺失事实。Trace 不会运行工具或代替验证。

想观察这个区别，可在 Bash/zsh 的当前代码分支运行相关进程测试：

```bash
PYTHONPATH=src python -m pytest -q tests/test_process_control_v028.py
```

应看到正常终止只记录一条控制退出；忽略正常终止的程序先返回 `still_running`，随后强制结束才产生最终事件。测试还检查损坏的 Trace 引用会被标记为不完整。程序停止后，完成任务仍须满足计划步骤和修复义务，并在稳定边界上单独调用 `run_shell(purpose="verification")`；控制工具的成功结果不能代替这项检查。

## 为什么这样设计

直接把“信号已发送”当作成功退出会漏掉仍在运行的子进程，并让 Agent 过早宣布任务完成。有界等待给出可解释的结果，同时避免工具无限卡住；代价是 `still_running` 可能需要后续一次控制或观察。对控制工具分别授权，避免一次普通 shell 命令授权扩大为结束进程的权限。

本版只控制当前 CLI、当前任务登记的进程。它不提供任意 PID、stdin、PTY 或跨会话重连。诊断阶段的终止若要作为修复动作，仍须走合法的 `recover` 目标或通过 `request_replan` 建立新计划，不能靠控制工具跳过 Repair Loop。

## 本版特性、下一课与代码索引

本课让 Agent 能在当前任务中请求正常终止或强制结束，观察确认结果，并在退出后重新验证。下一课可按项目需要扩展后台进程输入等外围能力；本版没有预设实现。

固定源码入口：[进程控制与确认](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.28/src/mini_agent/processes.py) · [工具与授权](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.28/src/mini_agent/tools/process.py) · [状态事件](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.28/src/mini_agent/state.py) · [只读 Trace](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.28/src/mini_agent/trace.py)
