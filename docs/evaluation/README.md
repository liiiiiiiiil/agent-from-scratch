# Evaluation Harness

Evaluation Harness 为固定题目创建干净工作区，运行一次 Agent，再用独立评分器检查修改。普通测试检查程序边界是否正确；Harness 检查 Agent 是否能在相同初始文件上完成一项具体任务。离线自测使用固定模型响应，只检查这条链路，不计入真实模型成功率。

## 运行命令

命令默认使用 Bash/zsh。先校验仓库附带的单文件修复题：

```bash
PYTHONPATH=src python -m mini_agent.evaluation validate tests/fixtures/evaluation/smoke/case.json
```

`self-test` 会先检查评分器能否拒绝原始 fixture、接受已知正确修复，再从相同初始文件跑两次固定响应。每次结果落在不同 trial 目录，记录为 `fixture`：

```bash
PYTHONPATH=src python -m mini_agent.evaluation self-test --output ./evaluation-results
PYTHONPATH=src python -m mini_agent.evaluation report ./evaluation-results
```

真实模型调用必须显式传 `--live`。命令会显示题目 ID、Agent 轮数、Agent/评分时间上限、任务和获准工具；本版 smoke 题默认最多 6 轮、Agent 120 秒、评分器 30 秒：

```bash
PYTHONPATH=src python -m mini_agent.evaluation run tests/fixtures/evaluation/smoke/case.json --live --output ./evaluation-live
PYTHONPATH=src python -m mini_agent.evaluation report ./evaluation-live
```

真实模型绑定从本地 `config_local.py` 解析。结果只记录 profile、provider、protocol 和不可逆 fingerprint 摘要；没有价格快照时 `cost_usd` 与 `price_snapshot` 都是 `null`。

## v0.51 编码任务集

`tests/fixtures/evaluation/benchmark/suite.json` 固定四题及顺序：分页末页边界、订单折扣与收据双模块修改、缓存 TTL 修复并新增回归测试、配置来源优先级调查与修复。每题有独立的 `initial/`、`grader.py` 和 `known_good/`。Agent 只收到 case 的任务与 `initial/`；评分器和已知正确版本不会复制进 Agent 工作区。

Suite 清单同时保存 case、任务文字、初始文件树、grader 和正确版本的 SHA-256。套件还有由代码固定的 ID/version 指纹。任何摘要变化都会拒绝运行；重新审核题目后要提升 suite version 并更新固定指纹。`validate-suite` 会预先检查所有题目，并要求 grader 拒绝原始版本、接受正确版本；缓存题还要验证所交 unittest 在原始错误实现上出现断言失败。

先审阅 `validate-suite` 输出中的任务文字、成功标准、预算和获准工具。只有审定后才执行显式 live 套件命令：

```bash
PYTHONPATH=src python -m mini_agent.evaluation validate-suite tests/fixtures/evaluation/benchmark/suite.json
PYTHONPATH=src python -m mini_agent.evaluation run-suite tests/fixtures/evaluation/benchmark/suite.json --live --repeats 3 --output ./evaluation-baselines/v0.51
PYTHONPATH=src python -m mini_agent.evaluation report-suite ./evaluation-baselines/v0.51
```

运行器先校验全部题目、离线基线和模型 binding，再创建包含 12 个顺序槽位的 `suite-run.json`。每个槽位只尝试一次；启动前故障记为基础设施错误，中断留下 `not_run` 槽位，已完成 trial 保留其原始目录引用。每次 trial 的 schema 2 记录 suite ID/version/摘要、run ID、重复序号、初始 fixture 摘要、Git code revision、运行时代码指纹和模型来源；运行时代码指纹也在 trial 前后核对。单题 schema 1 与 TrialRequest schema 1 保持兼容。报告从账本和被引用的原始 trial 重建，不把 `fixture` 与 `live` 混算。

每题分别报告计划、运行、可评分数量、独立 grader 通过数、Agent 最终成功数、错误类别、调用/token 观测数、耗时分布和 trial 路径。`scorable_trials` 要求独立 grader 返回布尔结果且没有 grader/runner 基础设施错误；`final_successes` 还要求 Agent 正常完成。`live_baseline_complete` 只有在 live run 收束且每个计划槽位都有可评分结果时为真；少于每题 3 个可评分样本会明确保持未完成。未观测到的 token 和调用保持缺失；没有价格快照时成本为 `null`。报告另列基础设施错误、实际模型来源摘要、用量缺失 trial，以及 Agent 正常停止但 grader 未通过、grader 与最终成功条件不一致等人工复核项。fixture 只用于验证评测链路，不能计入模型成绩。

suite 1.0 的 12 次真实试跑已归档于 [`baselines/v0.51/`](baselines/v0.51/README.md)。评审发现收据题的精确展示格式未公开，原成绩保留并标为争议样本；当前 suite 1.1 已在任务中补充格式要求，尚未运行真实基线，不沿用旧成绩。

运行中按 Ctrl+C 会先有界停止当前子进程，保存已启动 trial（Agent 中断为 `agent_interrupted`，评分器中断为评分基础设施错误），再将 suite 标为 `interrupted`；剩余槽位保持未运行。代码或模型来源不一致的原始 trial 仍保留，但从评分分母和最终成功数中剔除，单列 `source_mismatch_trials`，且不能标为完整基线。

## 题目格式

`case.json` 使用严格的 schema 1。fixture 与 grader 路径都是相对题目目录的路径；`..`、绝对路径、符号链接、特殊文件、缺失评分器和超限 fixture 会在启动 Agent 前拒绝。

```json
{
  "schema_version": 1,
  "case_id": "smoke-scale-repair",
  "version": "1.0",
  "task": "修改 src/scale.py 中的 scale(value)，使它返回参数乘以 2。",
  "fixture_dir": "initial",
  "grader_script": "grader.py",
  "agent_timeout_seconds": 120,
  "grader_timeout_seconds": 30,
  "max_rounds": 6,
  "allowed_tools": ["read_file", "list_dir", "grep", "write_file", "edit_file", "calculate"],
  "authorized_tools": ["read_file", "edit_file"]
}
```

`allowed_tools` 决定模型能看到哪些工具；`authorized_tools` 是本次无人值守运行明确放行的子集。其他已显示工具会被非交互 `PermissionGate` 拒绝，不读取 stdin，也不会把 `ask` 自动变成 `allow`。文件工具在 handler 执行时再次检查目标仍在 trial 根目录。fixture 最多 256 个文件、单文件 1 MiB、总大小 4 MiB；trial 工作区最多 256 个文件、总大小 8 MiB。

本版只允许 `read_file`、`list_dir`、`grep`、`write_file`、`edit_file`、`calculate`。不注册 shell、进程、MCP、Memory、Skill 或子代理工具；子代理调用数在结果中为 0。独立 grader 在 Agent 结束或超时并停止后才以另一个进程运行，输出必须是含布尔字段 `passed` 的 JSON object。

## 单次结果

每个 `trial-<case-id>-<uuid>` 目录以目录原子替换的方式发布，并包含：

- `trial.json`：schema 1 的结构化事实、用量、评分、失败类别、可选的评分脚本 SHA-256 和相对 artifact 路径。早期 schema 1 结果可能没有该摘要。
- `diff.patch`：有界文件差异；文本 diff 最多 1 MiB。
- `agent.log`、`grader.log`：有界日志。结果中不保存 Agent 最终自述、原始模型响应、真实 endpoint、model ID、API key 或认证头。

`success` 只有在 Agent 以正常文本终止、State 为 `done`、至少收到一个模型响应、grader 通过并且清理完成时才为真。`agent_stop_reason`、`agent_state_status` 和 `grader_passed` 分开记录，因此 Agent 自称完成、自行读回文件或自行验证都不能替代 grader。当前小任务不开放 shell；普通 Direct Path 修改后 Runtime 的 shell 验证证据不可用时，评测策略允许 Agent 正常停止，但不会伪造 Runtime verification evidence，独立 grader 仍是成败依据。

超时、Agent 异常和任务未完成保留为失败样本。Agent 停止后 grader 仍会检查工作区，所以超时 trial 也可能有 `grader_passed`。Runner 无法启动 worker、结果协议损坏、清理失败以及 grader 超时/异常属于基础设施错误。Worker 被强制停止时尚未写盘的调用和 token 计数为 `null`，不填 0 假装已经观测。

恢复成功率与无效重复次数在 v0.50 不适用，值为 `null` 并带有说明。工具调用数包括被拒绝调用。Token 分开保存 input/output，并注明 `provider`、`estimated`、`mixed` 或 `fixture` 来源。成本没有价格快照时不估算账单。

## 汇总口径

`report <dir>` 每次都从原始 `trial.json` 重建 JSON 汇总，不回写 trial 文件。Live 与 fixture 分开统计，不能将固定响应结果算进真实模型成功率。

- `scored_trials` 是存在独立 grader 布尔结果且 grader 没有基础设施错误的 trial；Agent 超时或失败仍在分母中，只要 grader 正常完成。
- `independent_acceptance_pass_rate` 是 grader 通过数除以 `scored_trials`。Agent 最终任务成功数另列，不是这个指标的分子。
- Runner/grader 基础设施错误单列并从评分分母剔除；grader 失败或无效输出不能算 Agent 失败，也不能算通过。
- Token 和调用的总数只加总有观测值的 trial，并同时给出 `observed_trials`。没有被 worker 保存的计数保持缺失。
- 成本、恢复成功率、无效重复次数目前均为 `null`。

## 隔离边界与人工复核

每个 trial 从只读 fixture 复制出新工作区，分配独立 `HOME`、临时目录和 Memory 路径；这保证同一题重跑从相同文件开始。Runner 只清理它自己创建的临时目录，结果目录不会覆盖旧 trial。

**独立工作区不是操作系统安全沙箱。** Worker 和 grader 与调用者使用同一系统账户、同一个 Python 解释器和本地配置；工具路径闸门限制了 Agent 文件工具，但没有用容器或系统权限隔离 Python 进程。不要把不可信 grader 或未审阅题目放进 live 测试。v0.50 的 grader 是仓库内固定、离线、无凭据脚本。

人工复核时先打开 `trial.json`，核对 run kind、Agent 停止原因、State 终态、计数来源和失败类别；再看 `diff.patch`，最后检查 `grader.log` 中每项预设断言。若 Agent 自述与 grader 结果不一致，以保留的独立评分事实为准，并在新版本题目中修正规则时提升 testcase `version`，不要改写既有 trial。

v0.50 的一次成功 live 样本为 `trial_id=fecf1d04-48f2-4c86-bf42-e0588dabc1fa`：4 次成功模型响应、3 次工具调用、provider 报告 6,520 input / 243 output tokens，Agent 用时 3,004 ms，grader 用时 38 ms，独立评分通过。首次受限网络下的连接失败也作为另一条 live trial 保留；该结果目录的汇总是 2 个 trial、1 个成功（1/2）。这是链路验收样本，不是编码能力基准。
