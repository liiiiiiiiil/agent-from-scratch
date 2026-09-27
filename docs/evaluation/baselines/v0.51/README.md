# v0.51 首份编码基线

此目录用于保存首轮真实模型运行的脱敏原始 trial、从原始结果重建的报告、逐题观察和人工复核队列。

当前状态：四道题及评分标准已冻结为 `coding-benchmark@1.0`（SHA-256：`bf728faf8cff54e173a73c21946499b48b81fccce73a2b187f3d7b20b7e12d16`），并于 2026-09-27 经用户审阅批准。计划每题运行 3 次，共 12 个 live trial；本次实现提交固定后顺序运行。本目录暂未归档 live 结果；离线 grader 检查和 fixture trial 不会算作模型表现。

运行完成后，将原始输出目录中的 `suite-run.json`、`suite.json`、`trials/` 和由 `report-suite` 重建的 `report.json` 一并归档。保留失败、超时、模型连接问题和基础设施错误。若某题不足 3 个可评分样本，按原始记录报告为未完成，不补跑并替换样本。

报告应注明 suite 摘要、代码 revision、模型来源摘要、用量缺失数、样本量限制和人工复核队列。没有价格快照时 `cost_usd` 保持 `null`。
