# 第 23 课：先调查，再交付计划（v0.23）

上一课：[Plan Contract（结构化计划）](22-plan-contract.md) · [教程总览](README.md) · 下一课：[v0.24 证据驱动重规划与停滞收口](24-replanning-policy.md)

> 代码快照：`v0.23` · 相邻差异：`v0.22..v0.23` · 命令环境：Bash/zsh

> 运行要求：Python 3.10+；核心运行时只使用标准库。`v0.23` tag 由维护者建立后，版本对照命令和固定源码链接才可直接使用。

## 本课目标

上一课让计划有了稳定的步骤和不可变修订，但提交计划会立即进入执行。模型即使声称“先调查”，运行时也没有阻止它在调查中写文件。本课结束后，你应能解释：只读调查如何由 Runtime 强制、为什么提交计划后可以停下来交给用户决定，以及批准计划为什么不等于批准具体工具。

## 前置条件

先读第 22 课，准备基础 Python、Git 和可用的 LLM 配置。下面的 Bash/zsh 命令用上一版作参照，再查看本版差异：

```bash
git checkout v0.22
git diff --stat v0.22..v0.23
git diff v0.22..v0.23 -- src/mini_agent/state.py src/mini_agent/agent.py src/mini_agent/tools/base.py
git checkout v0.23
```

`git diff --stat` 先显示改动规模；后一个 diff 帮你找到状态转换和两层工具准入。切换 tag 前，请保存自己工作区中未提交的修改。

## 新增与改动文件

| 文件 | 本课作用 |
| --- | --- |
| `src/mini_agent/state.py` | 保存规划阶段、用户决定与反馈触发记录；校验当前 revision 和阶段转换。 |
| `src/mini_agent/tools/plan.py` | 增加 `begin_plan`、`cancel_planning`，扩展 `commit_plan` 的反馈引用。 |
| `src/mini_agent/agent.py`、`src/mini_agent/tools/base.py` | 分别在整轮和单个调用层拒绝不合规动作。 |
| `src/mini_agent/__main__.py` | 提供 `--plan` 启动和 revision 级的用户交接命令。 |
| `src/mini_agent/context.py` | 让当前阶段和用户反馈在压缩对话后仍可见。 |

## 上一版的问题

v0.22 的 `commit_plan` 会直接把任务送到 `executing`。复杂任务虽能写出计划，模型却可以在调查尚未完成时调用写文件或执行命令。用户也无法先看一份完整计划，再决定是否允许按它推进。

本版让“调查”和“执行”成为不同的运行阶段。调查阶段只准调用无副作用工具；`--plan` 任务提交计划后停在 `awaiting_approval`，等待用户明确决定。普通简单任务仍可直接执行。

## 版本变更定位

图例：`[旧]` 上版已有，`[+]` 本版新增，`[~]` 本版修改，`[C]` 主要消费者，`[B]` 本版边界。

```text
v0.22 基线
[旧] 用户任务 -> agent_loop -> LLM tool_calls -> ToolExecutor -> PermissionGate -> handler
                              -> commit_plan -> AgentState.commit_plan()
                                                -> PlanRevision / executing
                    <- 每个 call 的 role=tool 结果 <- ToolExecutor
                    -> ContextManager(State 快照) -> 下一轮 LLM / 完成提醒
[旧] 写入或执行 -> generation -> 独立 verification -> 最终完成
```

v0.23 在调用链前加入规划阶段判断；计划提交后的交接由 CLI 驱动，不由模型自行批准：

```text
v0.23 变更
[+] 普通任务 -> begin_plan ─┐
[+] --plan 首条任务 ───────┴-> [~] PlanningState.exploring
                                  -> [C] ContextManager 显示只读阶段
                                  -> [~] agent_loop 整轮准入
                                  -> [~] ToolExecutor 单调用准入
                                       ├-> 无副作用调查 -> role=tool -> 下一轮 LLM
                                       ├-> 独占 commit_plan -> PlanRevision
                                       │      ├-> auto: executing
                                       │      └-> plan_only: awaiting_approval -> CLI 用户决定
                                       │                ├-> approve 当前 revision -> executing
                                       │                └-> reject / continue -> exploring -> 新 revision
                                       └-> [B] 副作用、verification、混合提交被拒绝
                                               -> 每个 call 仍有 role=tool；不进 PermissionGate
[旧] executing -> PermissionGate -> handler -> generation / verification
```

两层检查解决不同问题：agent loop 看完整回合，能发现 `commit_plan` 与其他调用混在一起；执行器即使被单独调用，也会在权限和 handler 前检查当前阶段。拒绝不会产生新的 generation 或验证证据。

## 关键流程

普通任务由模型独占调用 `begin_plan`；`--plan` 则从任务开始就进入 `exploring`。阶段记录在 State 中，下一轮上下文从 State 快照重建，因而不会依赖模型记住一句“请只读”。下面是单调用闸门的关键判断：

```python
if phase == "awaiting_approval":
    return "工具调用拒绝: 当前计划等待用户决定"
if phase == "exploring":
    if name == "commit_plan":
        return None
    if name == "run_shell" and arguments.get("purpose", "execution") == "verification":
        return "工具调用拒绝: exploring 阶段不能进行 verification"
    if effect_class != "none":
        return "工具调用拒绝: exploring 阶段只允许只读调查"
```

这段只摘取等待、提交、验证和副作用四个判断；实际函数还检查 `cancel_planning`、进度工具与恢复工具。`effect_class` 是工具自身声明的副作用类别，不由计划文字决定。`run_shell(purpose="execution")` 属于可能有副作用的调用；即使命令看起来只读也不能用于调查。`verification` 虽被归为无副作用调用，调查阶段仍会单独拒绝，因为此时没有新执行结果可验收。

调查后，模型独占调用 `commit_plan`。普通模式继续执行；`--plan` 模式立即把控制权交给 CLI，agent loop 不再向模型发起下一轮请求。用户输入 `/approve <revision_id>` 时，State 只接受当前待批 revision。下面的转换刻意不改权限策略：

```python
if decision == "approved":
    self.planning_state = replace(self.planning_state, phase="executing")
```

因此批准只表示“可以继续按当前方案工作”。后续写文件或执行命令仍由 PermissionGate 按工具和参数逐次决定是否放行。批准旧 revision、重复批准以及缺少反馈的驳回都会被拒绝，状态保持原样。

驳回和继续调查都要求反馈，并产生当前任务内的用户反馈触发记录。模型提交修改后的完整计划时，必须同时引用当前 `parent_revision_id` 与 `trigger_id`；新 revision 创建后才消费触发记录。继续调查保留原计划，如果新事实没有改变方案，用户可输入 `/review <revision_id>` 让原 revision 重新等待审批。驳回后的旧计划不能通过 `/review` 重新交付。

## 实现拆解

`begin_task(..., mode="plan_only")` 让 `--plan` 从一开始就处于只读阶段。普通模式仍从 `direct` 开始，不会按任务长度自动推断是否必须规划。普通模式的 `cancel_planning` 只在尚未提交计划时可用；plan-only 任务不能用它绕开用户交接。

当 exploring 中的 `commit_plan` 与其他工具出现在同一回复时，agent loop 为整批调用分别生成拒绝结果。这样不会出现“先写文件、再提交计划”或“先提交计划、再执行同批写入”的部分成功。无效调用仍按原顺序写入 `role=tool` 结果，供模型下一轮修正。

用户决定由 CLI 写入 State，不由模型工具伪造。Structured State 显示最近一次决定及有界反馈摘要；上下文压缩后仍可从 State 重建。用户反馈触发修订是本版为交接所需的最小记录；失败与只读观察触发的通用重规划策略属于下一课。

## 运行与观察

在 Bash/zsh 中运行下面的命令行首条任务。需要先按操作手册准备本地 `config_local.py`；程序处理首条任务后仍进入交互循环。

```bash
PYTHONPATH=src python -m mini_agent --plan "先检查当前项目的测试入口，给出修改前的计划"
```

观察模型只读调查并提交 revision 后，CLI 会从 State 中取出刚保存的计划并显示完整内容，然后等待决定。例如，模型可能提交下面这份计划；目标和步骤取决于实际调查结果：

```text
计划 revision 1
目标：确认项目的测试入口并拟定修改方案
制订原因：需要先了解现有测试如何运行
限制：
- 调查期间不修改文件
任务验收标准：
- 明确测试入口及后续验证命令
步骤：
1. [pending] inspect：检查项目测试入口
   依赖：无
   本步骤验收：
   - 找到现有测试命令
2. [pending] propose：拟定修改方案
   依赖：inspect
   本步骤验收：
   - 说明修改范围和验证方式

计划 revision 1 等待决定：
/approve 1
/reject 1 <反馈>
/continue 1 <反馈>
```

这份展示直接来自已提交的计划，不需要模型再复述。此时可以输入 `/continue 1 请再检查一处依赖`；模型会回到只读调查。若原计划仍合适，输入 `/review 1` 会再次显示这份计划，然后可输入 `/approve 1`。若模型提交了新 revision，就审阅新内容并对提示中的新 ID 做决定。也可输入 `/reject 1 <具体反馈>`，让模型根据反馈提交新计划。具体 revision 编号以运行时提示为准。

批准后若模型请求写文件或执行命令，仍会看到对应的权限决策；计划批准不会写入一条永久 allow 规则。`/reset` 或 `/new <任务>` 会结束当前任务的交接上下文，新任务默认回到普通模式。

## 为什么这样设计

只在提示词里要求“先调查”容易被一次错误工具调用绕过；State 阶段和双层准入使边界可以由测试重复验证。代价是同一批次里的合法调查也会随非法混合提交一起拒绝，模型需要拆成下一轮再调用。

用户批准的是完整方案，不是某次具体写入的权限。把两个决定分开，才能在计划合理但某条命令不安全时仍由 PermissionGate 拦住。交接也需要任务内保留 revision ID 和反馈，而不能把一次自然语言“同意”误当成可重复使用的授权。

## 设计边界

- 普通简单任务保持 Direct Path；Runtime 不根据关键词推断复杂度。
- 本版不提供失败或观察触发的通用 `request_replan`、重规划预算及停滞护栏。
- 计划批准、步骤完成和独立 verification 是不同事实；只有实际执行后的当前 generation 验证可支持完成判定。
- 等待批准不调用 LLM，也不让模型工具继续运行；用户输入无效 revision 时不改变计划状态。

## 本版特性、下一课与代码索引

本版新增可强制的只读调查、`--plan` 交接和 revision 级用户决定。下一课会讨论真实失败或新观察出现时如何有界地修订计划。

固定到本课 tag 的源码入口：

- [`state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.23/src/mini_agent/state.py)：规划阶段、用户决定和反馈触发记录。
- [`agent.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.23/src/mini_agent/agent.py)：整轮准入及提交后的交接停止点。
- [`base.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.23/src/mini_agent/tools/base.py)：单调用准入先于权限和 handler。
- [`__main__.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.23/src/mini_agent/__main__.py)：CLI 启动和用户决定入口。
