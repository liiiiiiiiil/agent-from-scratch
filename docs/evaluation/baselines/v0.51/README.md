# v0.51 首份编码基线

此目录用于保存首轮真实模型运行的脱敏原始 trial、从原始结果重建的报告、逐题观察和人工复核队列。

题目及评分标准冻结为 `coding-benchmark@1.0`（SHA-256：`bf728faf8cff54e173a73c21946499b48b81fccce73a2b187f3d7b20b7e12d16`），于 2026-09-27 经用户审阅批准。12 个 live trial 已于 2026-09-27 顺序完成，未重跑或替换任何样本。原始记录和重建报告保存在 [`live-20260927/`](live-20260927/)。离线 grader 检查和 fixture trial 不计入模型成绩。

## 首轮结果

| 题目 | 已运行 / 可评分 | 独立验收通过 | 最终成功 | 观察 |
|---|---:|---:|---:|---|
| `pagination-boundary` | 3 / 3 | 3 | 3 | 三次均修复末页边界并通过独立评分。 |
| `orders-discount-receipt` | 3 / 3 | 0 | 0 | 三次的折扣计算、应付金额等行为检查通过；收据格式未匹配冻结的精确展示标准。差异见各 trial 的 `grader.log` 和 `diff.patch`。 |
| `cache-expiry-regression` | 3 / 3 | 2 | 1 | 两次达到 120 秒上限；其中一次提交的修复和回归测试通过评分，但 Agent 未正常完成。另一次超时前没有文件改动且评分失败。第三次完整通过。 |
| `config-priority-investigation` | 3 / 3 | 3 | 2 | 三次提交的优先级行为均通过评分；一次虽有正确改动但达到 120 秒上限。 |

总计计划 12、运行 12、可评分 12；独立验收通过 8，最终成功 6。失败类别为 `task_failed=3`、`agent_timeout=3`，基础设施错误为 0。评分通过和最终成功是不同指标：超时样本即使通过 grader，也不会记为最终成功。

## 冻结来源与观测限制

- 代码 revision：`95c533793ff1af0242ba3124ae6b33590c51b4d5`；运行期间代码摘要一致。
- 模型来源摘要：provider/profile `legacy-default`、协议 `openai_chat`、fingerprint `f5d29d7d52439c419e672fd0a4d7007a8197e4afe7b24083d4d0116fb4fc4cf6`。真实 endpoint、模型 ID 和凭据不进入归档。
- 9 个 trial 有用量观测：合计 56 次模型调用、59 次工具调用、输入 124,975 tokens、输出 10,016 tokens；3 个超时 trial 的调用和 token 用量不可用。上述汇总不代表 12 次单独 HTTP 请求，每个 trial 可包含多次模型调用。
- Agent 耗时中位数 8,445.5 ms，p95 为 120,017 ms。没有价格快照，`cost_usd` 为 `null`。
- 每题只有 3 个样本，结果只描述这次冻结题集与模型来源上的观察，不代表一般编码能力。

## 人工复核队列

重建报告的 `manual_review_queue` 列出以下 5 个 trial：

- `orders-discount-receipt`：`04268614`、`60549c61`、`51ffeed7`。复核收据中的折扣标签、百分比和负号格式与 grader 要求的差异。
- `cache-expiry-regression`：`f9d35ed5`。独立 grader 通过且修复/回归测试已提交，但 Agent 超时；核对其 diff 与结束状态。
- `config-priority-investigation`：`1acd4694`。优先级修复通过 grader，但 Agent 超时；核对 diff 与结束状态。

另一个超时样本 `cache-expiry-regression-f0f441ee` 没有改动文件，grader 失败；它不在自动复核队列中，仍作为失败原始证据保留。

完整逐 trial 证据、suite ledger 和可重建报告位于 [`live-20260927/`](live-20260927/)。用 `PYTHONPATH=src python -m mini_agent.evaluation report-suite docs/evaluation/baselines/v0.51/live-20260927` 可重新生成报告视图。
