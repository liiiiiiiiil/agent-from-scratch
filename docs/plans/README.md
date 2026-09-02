# 计划文档（plans）

存放路线图、功能计划、任务拆解。

## 索引
- 路线图与计划文档以本目录为权威来源。运行时硬约束和精简架构索引另见 `AGENTS.md`。
- [`teaching-repo-plan.md`](teaching-repo-plan.md) — 把仓库改造成多阶段教学仓库的完整方案（版本切片、目录结构、文档模板、git 操作清单、验收标准）。
- [`context-management-plan.md`](context-management-plan.md) — 阶段四 Context Management 实施计划（v0.11 架构 / v0.12 预算裁剪 / v0.13 压缩）。
- [`project-task-orchestration-plan.md`](project-task-orchestration-plan.md) — 阶段五项目感知与任务编排实施计划（v0.14 项目级指令 / v0.15 任务清单与状态 / v0.16 计划驱动执行；英文标识分别为 Project Instructions、Todo / Task State、Plan-driven Execution）。
- [`reliable-execution-plan.md`](reliable-execution-plan.md) — 阶段六可靠执行实施计划（v0.17 Failure Model / v0.18 Recovery Policy / v0.19 Checkpoint & Rollback / v0.20 Repair Loop / v0.21 Trace & Replay）。

## 文档约定
- 文件名用小写 + 连字符，如 `add-shell-tool.md`。
- 每篇文档建议包含：目标、方案、任务拆解、验收标准。
- 完成后更新对应计划、教程索引、CHANGELOG 和版本信息；仅当运行时约束变化时更新 `AGENTS.md`。
