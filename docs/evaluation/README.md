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
