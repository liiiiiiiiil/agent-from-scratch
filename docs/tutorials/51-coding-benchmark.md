# 第 51 课：为什么要重复运行编码任务（v0.51）

上一课：[单题评测链路](50-evaluation-harness.md) · [教程总览](README.md) · 下一阶段：可靠性评测

> 代码快照：`v0.51` · 相邻差异：`v0.50..v0.51` · 命令环境：Bash/zsh
>
> 源码链接固定到 `v0.51`。tag 建立前，请在当前工作树阅读对应文件。

## 本课目标

一次试跑成功只能说明 Agent 在那次模型响应和那份工作区上完成了任务。下一次模型回复可能不同，Agent 也可能只修好一部分。要了解它能不能稳定完成一类工作，就要让每次尝试从相同文件开始，独立运行和评分，并保留通过与失败的全部记录。

本课增加四道纯标准库 Python 编码题、固定题目摘要、重复运行账本和逐题报告。读完后，你应能分清“计划运行多少次”“实际运行多少次”“有多少次能评分”以及“有多少次 Agent 最终成功”。

## 前置条件与版本切换

需要 Python 3.10+、终端和 Git。第 50 课介绍了单题评测：Runner 为一个 Case 创建工作区，Agent 结束后由独立 grader 检查。维护者建立相邻 tag 后，可以查看本版改动：

```bash
git checkout v0.50
git diff --stat v0.50..v0.51
git diff v0.50..v0.51 -- src/mini_agent/evaluation tests/fixtures/evaluation/benchmark
git checkout v0.51
```

本课源码索引：[benchmark.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.51/src/mini_agent/evaluation/benchmark.py)、[schema.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.51/src/mini_agent/evaluation/schema.py)、[runner.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.51/src/mini_agent/evaluation/runner.py)、[report.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.51/src/mini_agent/evaluation/report.py)。

## 上一版的限制

v0.50 已能运行一道小题并保存单次结果，但只有一条记录时，看不出任务结果会不会随着模型回复变化。即使 Agent 自称完成，结果也可能只通过了部分要求。因此，本版先固定一组任务和评分，再为每题计划多次独立运行。

## 新增与改动文件

| 位置 | 变化 | 作用 |
|---|---|---|
| [benchmark/suite.json](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.51/tests/fixtures/evaluation/benchmark/suite.json) 与四个题目目录 | 新增 | 冻结题目顺序、任务、初始代码、grader 和正确版本摘要。 |
| [evaluation/benchmark.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.51/src/mini_agent/evaluation/benchmark.py) | 新增 | 校验摘要、验证题目基线、编排 suite run、重建逐题报告。 |
| [evaluation/schema.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.51/src/mini_agent/evaluation/schema.py) | 修改 | 增加有上限的 Suite / SuiteRun 合同和 TrialResult schema 2，同时保留 schema 1。 |
| [evaluation/runner.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.51/src/mini_agent/evaluation/runner.py)、[report.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.51/src/mini_agent/evaluation/report.py)、[__main__.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.51/src/mini_agent/evaluation/__main__.py) | 修改 | 复用单题 runner，保存每个槽位和原始 trial，再从它们重建报告。 |
| [test_evaluation_v051.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.51/tests/test_evaluation_v051.py) | 新增 | 验证题目评分、摘要拒绝、重复运行、部分中断和报告引用。 |

## 版本变更定位

图例：`[旧]` 上一版已有；`[+]` 本版新增；`[~]` 本版修改；`[C]` 主要使用者；`[B]` 边界。

v0.50 的入口处理一条 Case。每次运行有自己的初始工作区；评分器在 Agent 停止后检查文件：

```text
v0.50 基线：
[旧] validate / run --live / self-test
  → [旧] Case 与 fixture 检查
  → [旧] 单次新工作区 → Agent worker → 独立 grader
  → [旧] schema 1 TrialResult 原子保存
  → [C] report 从 trial.json 重建汇总
```

v0.51 在外层增加固定顺序的题集和运行账本。单题执行仍由 v0.50 的 `run_case()` 完成；新增逻辑负责预检全部题目、准备重复槽位，并在每次结束后更新账本：

```text
v0.51 变更：
[+] validate-suite
  → [~] Suite / 初始文件 / grader / 正确版本摘要核对
  → [B] grader 必须拒绝原始版本并接受正确版本
  → [B] 任一摘要或基线不符时不启动 Agent
  → [+] 按清单顺序建立 12 个计划槽位
  → [C] 每个槽位调用 [旧] EvaluationRunner.run_case()
  → [~] 保存 schema 2 TrialResult + [~] 原子更新 suite-run.json
  → [+] report-suite 校验原始证据引用并重建逐题/总报告

失败分支：[B] live 需显式 --live 且全部题目绑定同一模型来源；[+] 中断保留未运行槽位；
          [B] 不自动重试失败或基础设施错误槽位。
```

## 先冻结问题和评分标准

任务的题目文字告诉 Agent 要解决什么，grader 则在 Agent 停止后检查可观察结果。每道题还保留一份人工制作的正确版本。运行前的 `validate-suite` 会检查摘要，并让 grader 分别评分原始版和正确版；若原始版也通过，或正确版失败，这道题就不能进入 live 运行。

固定题集按这个顺序包含四个问题：

1. 分页函数漏掉末页，要求覆盖空集合、整页边界、部分末页和非法页大小。
2. 订单折扣舍入不正确，收据也未准确列出折扣和应付金额；要同时修好计算与展示两个模块。
3. 缓存条目在恰好到达 TTL 时仍被返回；除了修复，还要新增能在当前代码通过、在原始错误实现上失败的 `unittest` 回归测试。
4. 环境变量无法覆盖项目配置和默认值；任务只描述不同来源的预期优先级，不告诉 Agent 哪个文件有错。

题目目录里的 grader 和 `known_good/` 不会复制进 Agent 工作区。`suite.json` 保存它们以及 Case、任务文字和初始文件树的 SHA-256。Suite 本身还有固定的 ID/version 指纹；摘要改变但版本未更新时，加载会失败。

## 重复槽位如何保留证据

Suite run 有固定的 12 个槽位：按清单顺序先列出同一道题的三次运行，再进入下一题。每个槽位开始前会写成 `running`，完成后写成 `completed` 并保存 trial 相对路径。进程在中途退出时，已经结束的记录继续保留，未开始的槽位仍在账本中；下次运行必须使用新的输出目录，不会静默重跑旧槽位。

TrialResult schema 1 仍用于原来的单题结果。Suite trial 使用 schema 2，额外记录 suite ID/version/摘要、suite run ID、重复序号、初始文件摘要和运行时代码指纹。SuiteRun 账本还保存 Git code revision、运行时代码指纹与模型来源摘要；每次 trial 启动前核对实现来源，Agent 停止后再记录一次指纹，报告会标出中途变化的样本。

## 看报告时先看分母

计划次数表示账本里预留的槽位数。运行次数是已经生成并引用原始 trial 的槽位数。可评分次数还要求 grader 正常返回布尔结果；runner 或 grader 基础设施错误单列，不混入 Agent 成败。独立验收通过数只看 grader，最终成功数还要求 Agent 正常收束、状态为 `done` 并完成清理。

工具调用、成功模型响应和输入/输出 token 只加总实际观测到的值，同时列出对应观测样本数。耗时报告中位数和 P95。没有价格快照时成本保持 `null`。每个数字都能回到逐 trial 路径，检查 `trial.json`、`diff.patch` 和 `grader.log`。Fixture 结果单独标记，只验证评测器，不计入真实模型基线。

## 为什么这样设计

固定初始文件让重复运行可比较，独立 grader 避免把 Agent 的自我报告当成成功证据，原始 trial 路径则让汇总数字可复查。原始版与正确版的离线检查还能发现永远通过或永远失败的评分规则。

代价是摘要变更必须提升 suite 版本并重新审核；当前四题也只能反映这些具体行为，不能代表所有 Python 编码任务。每题三次的样本很小，报告用于描述这次冻结题集的观察结果，不用于宣称统计显著或普遍能力。工作区隔离仍不是操作系统安全沙箱。

## 关键流程

先执行离线预检。预期输出包含四道题，并为每题列出 `initial: passed=false` 与 `known_good: passed=true`；缓存题的 grader 还会验证回归测试能否揭露旧错误。此命令不调用模型：

```bash
PYTHONPATH=src python -m mini_agent.evaluation validate-suite tests/fixtures/evaluation/benchmark/suite.json
```

审阅任务与成功标准后，才运行首轮真实基线。命令会打印 12 个计划 trial、各题轮数和时间上限、获准工具及任务正文。运行完成后，报告应让你能按题查看计划、已运行、可评分、通过数和每条 trial 路径：

```bash
PYTHONPATH=src python -m mini_agent.evaluation run-suite tests/fixtures/evaluation/benchmark/suite.json --live --repeats 3 --output ./evaluation-baselines/v0.51
PYTHONPATH=src python -m mini_agent.evaluation report-suite ./evaluation-baselines/v0.51
```

本版首份 live 基线在任务审阅批准后运行；离线预检结果不会被记成 Agent 成绩。

## 实现拆解

Suite 的摘要不是只对 JSON 文件做一次校验。`load_suite()` 会把清单中的路径逐段限制在 suite 根目录内，重新计算 Case、任务文字、初始文件、grader 和正确版本摘要，再核对代码内固定的 ID/version 指纹。以下代码表达的是拒绝摘要变化的关键边界：

```python
if item[key] != actual_value:
    raise ValueError(f"{case.case_id} 的 {key} 摘要不匹配；请提升 suite version")
```

这一检查发生在创建任何 live worker 之前。固定指纹还要求改变摘要时同时提升 suite 版本并更新审核后的指纹。

`run_suite()` 先写出包含所有槽位的运行账本，再按顺序让现有 `run_case()` 处理每一题。运行账本通过同目录临时文件、文件同步和替换更新；如果一项任务中断，之前已保存的 trial 路径继续保留，剩余项不会从计划里消失。

```python
slot["status"] = "completed"
slot["trial_path"] = trial_dir.relative_to(root).as_posix()
ledger["updated_at"] = _now()
_atomic_json(ledger_path, ledger)
```

`build_suite_report()` 会先验证账本、每个槽位和原始 TrialResult 的 suite/run/repetition/fixture 摘要一致，再统计数字。每条结果保留相对路径，报告可回到原始 trial 查看。

## 本版特性、下一课与代码索引

v0.51 为单题评测增加有版本的任务合同、顺序编排、原子运行账本和逐题证据报告。它不改 Agent Runtime 的决策、计划或权限，也不加入新的模型工具。四道固定题和当前运行状态见[评测说明](../evaluation/README.md)；真实结果生成后归档到[首份基线目录](../evaluation/baselines/v0.51/README.md)。

下一阶段将在同一条评测链路上检查故障处理和恢复行为。
