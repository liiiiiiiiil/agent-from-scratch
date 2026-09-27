# 第 50 课：让 Agent 做一次可复查的任务评测（v0.50）

上一课：[可续接子会话](49-resumable-child-session.md) · [教程总览](README.md) · 下一课：v0.51（编码任务集，规划中）

> 代码快照：`v0.50` · 相邻差异：`v0.49..v0.50` · 命令环境：Bash/zsh

> 源码链接固定使用 `v0.50` 路径；tag 尚未建立前，请在当前工作树阅读链接所指文件。

## 本课目标

单元测试能告诉我们工具和权限边界有没有按代码工作，却不能回答 Agent 能否接到一道任务、实际调用模型、修改文件并达到用户要求。只看 Agent 最后的文字也不够：它可能说“已修复”，文件却仍然错误。

本课加入一条独立评测流程：先把固定的初始文件复制到新目录，让 Agent 在受限工具下运行；Agent 结束后，再由单独的评分程序检查结果。读完后，你应能从原始 trial 记录中分清 Agent 是否正常停止、评分器是否通过，以及离线固定响应为何不能算真实模型成绩。

## 前置条件与版本切换

只需要 Python 3.10+、终端和 Git。建议先读第 49 课，了解上一阶段如何复用同一个 Runtime。当前实现位于本次 v0.50 工作树；维护者建立 tag 后，可按下面的命令复查相邻版本：

```bash
git checkout v0.49
git diff --stat v0.49..v0.50
git diff v0.49..v0.50 -- src/mini_agent/evaluation docs/evaluation
git checkout v0.50
```

本文源码索引：[runner.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.50/src/mini_agent/evaluation/runner.py)、[worker.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.50/src/mini_agent/evaluation/worker.py)、[schema.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.50/src/mini_agent/evaluation/schema.py)。

## 上一版的问题

v0.49 加强了父 Agent 与子代理会话的协作，但项目还没有统一方式衡量父 Agent 在一项固定编码任务上的表现。现有测试直接调用函数或模拟边界，不会给 Agent 一份新工作区、启动模型并检查它留下的文件。

如果把 Agent 自己的最后一句话当作成功证据，评测就会把“声称完成”和“实际通过要求”混在一起。本版把模型运行与独立验收拆成前后两个阶段，并把每次运行保存成单独结果。

## 新增与改动文件

| 文件 | 变化 | 读者可从这里看到什么 |
|---|---|---|
| [evaluation/schema.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.50/src/mini_agent/evaluation/schema.py) | 新增 | 题目、请求和结果的版本合同及路径/大小限制。 |
| [evaluation/worker.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.50/src/mini_agent/evaluation/worker.py) | 新增 | 如何在单任务进程中组装受限 Registry、非交互授权和 canonical Runtime。 |
| [evaluation/runner.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.50/src/mini_agent/evaluation/runner.py)、[report.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.50/src/mini_agent/evaluation/report.py) | 新增 | 如何建新工作区、设置超时、运行 grader、原子保存结果和重建汇总。 |
| [tests/fixtures/evaluation/smoke](https://github.com/liiiiiiiiil/agent-from-scratch/tree/v0.50/tests/fixtures/evaluation/smoke) | 新增 | 一个原始版本会失败、已知正确修改会通过的单文件题。 |
| [docs/evaluation/README.md](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.50/docs/evaluation/README.md) | 新增 | 题目格式、统计分母、结果字段、隔离边界和人工复核步骤。 |

第 49 课的父 `AgentRuntime.run()` 和 `ParentRuntimePolicy` 仍提供模型到工具的主循环；第 50 课新增的 worker 为它们准备专用工具和任务条件。

## 版本变更定位

图例：`[旧]` 上一版已有；`[+]` 本版新增；`[~]` 本版修改；`[C]` 主要消费者；`[B]` 本版边界。

v0.49 的运行入口服务交互 CLI。它处理首条任务后继续等待用户输入；默认 `PermissionGate` 遇到 `ask` 时会读终端输入。它适合人和 Agent 共同工作，不适合无人值守地重复运行同一题。

```text
v0.49 基线：
[旧] CLI 首条任务
  → [旧] 父 Runtime 组装 State / Context / Registry / PermissionGate
  → [旧] AgentRuntime.run()
  → [旧] CLI 继续交互，最终任务状态由 CLI 生命周期收口
```

v0.50 增加专用入口与第二个进程。Case 是一份描述任务、初始文件、工具授权和预算的 JSON 题目合同；Trial 是这道题的一次运行记录。

```text
v0.50 变更：
[+] validate / run --live / self-test / report
  → [+] Case + fixture 检查（坏路径、超限或缺 grader 时不启动 Agent）
  → [+] 新 trial 工作区 + 独立 HOME
  → [+] Worker：State / Context / 冻结模型 binding / 受限 Registry / 非交互 Gate
  → [~] ParentRuntimePolicy + AgentRuntime.run()
  → Agent 进程停止或超时清理
  → [+] 独立 grader 进程 → TrialResult + 有界 diff/log → 原子发布
  → [C] report 从原始 TrialResult 重建汇总

失败分支：[B] 无真实配置或没有 --live，不发送 live 请求；[B] grader 超时/异常标为基础设施错误。
```

## 核心概念与数据结构

### 1. 先冻结题目，再启动模型

如果评分规则跟着 Agent 的输出临时变化，就无法公平比较两次试跑。因此 `case.json` 在运行前固定题目 ID、版本、任务文字、初始 fixture、独立 grader、轮数、两类超时和可见工具。schema 1 由标准库代码校验，不需要额外 JSON Schema 依赖。

`allowed_tools` 决定模型能看到什么；`authorized_tools` 明确哪些工具在这次无人的运行中获准。比如 `edit_file` 可以出现在模型工具目录中，但若没有放进授权列表，调用会收到拒绝结果。`ask` 不会触发 `input()`，也不会被自动批准。

下面的题目片段显示 schema 把默认时间和轮数写在 testcase 本身。校验器会在创建 Agent 子进程之前检查这些预算：

```json
{
  "schema_version": 1,
  "agent_timeout_seconds": 120,
  "grader_timeout_seconds": 30,
  "max_rounds": 6,
  "allowed_tools": ["read_file", "edit_file"],
  "authorized_tools": ["read_file", "edit_file"]
}
```

这样两次运行能使用相同权限与时间上限；未授权的工具不会等候终端输入。

### 2. 一个工作区对应一个 trial

每次运行从只读 fixture 复制新目录，再创建独立 `HOME`、临时目录和 Memory 位置。Worker 只接收题目任务和工作区路径；评分脚本路径不会作为模型工具内容暴露。文件工具在 handler 执行前检查 canonical 路径仍在 trial 根目录，输入 fixture 与运行输出也都有文件数量和总大小上限。

Worker 通过现有 `ToolExecutor` 执行工具，并由 `AgentRuntime.run()` 维持完整的模型工具协议。每个 trial 不开启 `/save`。worker 的状态只在该子进程内使用，不写会话文件。

对文件工具，每次调用都会先把目标解析到 trial 根目录内：

```python
relative = absolute.relative_to(root)
```

如果路径不能相对于根目录表示，handler 会在读写文件前拒绝它。路径范围检查限制的是文件工具；它不构成 Python 子进程的操作系统安全边界。

### 3. Agent 收束与独立评分是两项事实

`agent_stop_reason` 记录 Runtime 停止原因，`agent_state_status` 记录结构化任务终态，`grader_passed` 记录独立断言结果。Agent 最终文本不会作为通过依据写入 `trial.json`。只有 Agent 正常以文本结束、State 为 `done`、模型至少成功响应一次、grader 通过且临时资源清理完成，`success` 才为真。

受限 smoke 题不开放 `run_shell`，因此普通文件修改的 Runtime verification 证据不可由模型命令产生。评测策略仍调用 `ParentRuntimePolicy`；对 Direct Path 上单纯的普通验证提醒，它只允许 Agent 正常停止，不写入或伪造 `verification_evidence`。独立 grader 仍在 Agent 停止后运行，且独占评测成败判定。

runner 的成功条件同时要求模型正常结束、grader 通过且清理完成：

```python
success = bool(
    failure_kind == "none"
    and grader_passed is True
    and cleanup_complete
    and result_payload is not None
    and int(result_payload.get("successful_model_responses", 0)) > 0
)
```

因此，Agent 输出“完成”本身不足以让 trial 成功。

### 4. 离线响应不冒充真实模型运行

`self-test` 把两条固定响应交给同一 worker，验证工具路径、Agent Runtime、独立 grader、原子保存和报告链路。结果标记为 `fixture`，汇总将它与 `live` 分组展示。只有显式执行 `run ... --live` 才会读取本地模型配置并尝试真实请求。

报告分别收集两种来源，避免固定响应抬高真实成功率：

```python
groups = {
    kind: [row for row in rows if row.get("run_kind") == kind]
    for kind in ("live", "fixture")
}
```

`report` 从原始 JSON 重建数字，不改写任何 trial。

## 为什么这样设计

CLI 会继续交互并可能等待授权输入，评测入口需要明确的一次性任务边界。单独 worker 让一次 trial 可以通过墙钟超时结束，并在退出时清理由 runner 创建的进程组；独立 grader 确保判定不依赖模型的说法。

v0.50 只交付一个小型文件修改题，重点验证评测链路。它没有 shell 编码、持久化恢复或子代理评测能力，也没有形成代表性的编码基准。独立工作区让重跑从相同初始文件开始，但它不是操作系统安全沙箱：worker 与 grader 仍以调用者的系统账户运行。

## 关键流程

先验证题目，再运行固定响应的离线链路。第一条命令应显示 fixture 有 1 个文件，并列出被允许和被授权的工具；第二条命令应给出两个不同 trial ID，每个 trial 的独立 grader 均通过：

```bash
PYTHONPATH=src python -m mini_agent.evaluation validate tests/fixtures/evaluation/smoke/case.json
PYTHONPATH=src python -m mini_agent.evaluation self-test --output ./evaluation-results
PYTHONPATH=src python -m mini_agent.evaluation report ./evaluation-results
```

接下来才显式进行一次真实试跑。运行前 CLI 打印题目和时间/轮次上限；Agent 结束或被停止后，Runner 才启动 grader。观察 `trial.json` 中 `run_kind=live`、至少一个成功模型响应、模型来源摘要、用量来源、`grader_passed` 和耗时字段。fixture 结果不能代替这条 live 记录。

```bash
PYTHONPATH=src python -m mini_agent.evaluation run tests/fixtures/evaluation/smoke/case.json --live --output ./evaluation-live
PYTHONPATH=src python -m mini_agent.evaluation report ./evaluation-live
```

本次工作树的 live 验收记录 `trial_id=fecf1d04-48f2-4c86-bf42-e0588dabc1fa` 正常结束并通过独立 grader：4 次成功模型响应、3 次工具调用、provider usage 为 6,520 input 与 243 output tokens，Agent 用时 3,004 ms、grader 用时 38 ms。首次受限网络连接失败的另一条 live trial 也保留在原始结果中，因此该输出目录汇总为 1/2，而不是只展示通过样本。它们只验证这一道 smoke 题，不代表编码能力基准。

## 实现拆解

- [`case_from_dict()`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.50/src/mini_agent/evaluation/schema.py) 检查 schema 版本、预算、工具名单、相对路径和 fixture；grader 缺失或 fixture 超限时运行还未创建。
- [`run_request()`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.50/src/mini_agent/evaluation/worker.py) 建立父 State、Context、`ToolExecutor` 与冻结的模型 binding，并将最终结构化停止原因交回 Runner。
- [`EvaluationRunner.run_case()`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.50/src/mini_agent/evaluation/runner.py) 顺序运行 Agent 和 grader；超时会停止 Agent 进程组，结果目录通过同目录原子替换发布。
- [`build_report()`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.50/src/mini_agent/evaluation/report.py) 仅读取原始 schema 1 结果；基础设施错误从成功率分母剔除，Agent 超时仍会纳入分母，只要 grader 正常给出结果。

价格快照没有配置，因此费用是 `null`。v0.50 不采集恢复成功率和无效重复次数，这两项也用 `null` 表示，不把缺失观测写成 0。

## 设计边界

工作区隔离保证每次从同一份 fixture 重新开始，也让 grader 在 Agent 停止后检查实际输出。工具路径闸门不会约束 Python 进程本身；不可信题目和 grader 需要操作系统级隔离，而 v0.50 没有提供这种隔离。每条结果都应人工核对原始 JSON、文件 diff 和 grader 日志。

固定模型响应只证明 harness 的流程工作，不证明真实模型会完成任务。成功率只针对题目版本和当前条件；v0.51 才开始扩充题目和重复试验。

## 实现拆解补充

当 Agent 超时，Runner 尝试有界结束进程组；随后独立 grader 仍可检查超时前已经写入的文件。此时 `grader_passed` 可为真，但 `success` 必须为假，因为 Agent 没有正常收束。若 grader 自身超时或输出不是要求的 JSON，结果标记 grader 基础设施错误，不进入成功率分母。

原始 trial 结果保存在 `trial.json`，`report` 只重算汇总。确认某次 diff 或题目评分需要调整时，应保留原始 trial，并提升题目版本重新运行，而不是覆盖历史事实。

## 本版特性、下一课与代码索引

v0.50 建立了固定题目、受限 live worker、独立评分、单次结果和分组报告；仓库只有一个无网络依赖的小型试题。下一课 v0.51 将扩展编码任务集，再讨论重复运行和任务代表性。

- [评测使用说明](../evaluation/README.md)
- [源码：schema.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.50/src/mini_agent/evaluation/schema.py)
- [源码：worker.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.50/src/mini_agent/evaluation/worker.py)
- [源码：runner.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.50/src/mini_agent/evaluation/runner.py)
- [源码：report.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.50/src/mini_agent/evaluation/report.py)
