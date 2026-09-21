# 计划文档（plans）

存放路线图、功能计划、任务拆解。

## 索引
- 路线图与计划文档以本目录为权威来源。运行时硬约束和精简架构索引另见 `AGENTS.md`。
- [`teaching-repo-plan.md`](teaching-repo-plan.md) — 把仓库改造成多阶段教学仓库的完整方案（版本切片、目录结构、文档模板、git 操作清单、验收标准）。
- [`context-management-plan.md`](context-management-plan.md) — 阶段四 Context Management 实施计划（v0.11 架构 / v0.12 预算裁剪 / v0.13 压缩）。
- [`project-task-orchestration-plan.md`](project-task-orchestration-plan.md) — 阶段五项目感知与任务编排实施计划（v0.14 项目级指令 / v0.15 任务清单与状态 / v0.16 计划驱动执行；英文标识分别为 Project Instructions、Todo / Task State、Plan-driven Execution）。
- [`reliable-execution-plan.md`](reliable-execution-plan.md) — 阶段六可靠执行实施计划（v0.17 Failure Model / v0.18 Recovery Policy / v0.19 检查点与回滚（Checkpoint / Rollback）/ v0.20 Repair Loop / v0.21 Trace & Replay）。
- [`adaptive-planning-plan.md`](adaptive-planning-plan.md) — 阶段七自适应规划与重规划实施计划（v0.22 Plan Contract / v0.23 Plan Mode & Handoff / v0.24 Replanning Policy / v0.25 Plan Trace & Evaluation）。
- [`process-management-plan.md`](process-management-plan.md) — 阶段八进程管理实施计划（v0.26 后台进程 / v0.27 进程观察 / v0.28 进程控制 / v0.29 有界管道 stdin；PTY 后续评估）。
- [`session-persistence-resume-plan.md`](session-persistence-resume-plan.md) — 阶段九会话持久化与恢复实施计划（v0.30 会话持久化 / v0.31 安全恢复 / v0.32 持久工具边界 / v0.33 崩溃恢复）。

阶段九已完成：v0.30–v0.33 已实现安全点保存、跨进程恢复、持久工具边界和崩溃后的不确定调用交接；源 session 不覆盖，旧调用不自动 replay。
- [`subagent-delegation-plan.md`](subagent-delegation-plan.md) — 阶段十受控子代理委派实施计划；v0.34 最小同步只读委派、v0.35 共享父子运行循环、v0.36 多 provider/统一协议适配、v0.37 生命周期与聚合预算、v0.38 有界并行和 v0.39 持久委派交付已实现。保持子代理只读、单层，由父 Agent 独占修改和完成判定。
- [`memory-retrieval-references-plan.md`](memory-retrieval-references-plan.md) — 阶段十一轻量记忆、相关检索与资料引用实施计划；v0.40–v0.42 工作区持久 Memory、相关检索与具名本地 References 已实现。

阶段十一已完成 v0.40–v0.42：工作区 Memory 支持显式 CRUD 与相关检索，父 Context 可按预算临时注入不可信候选，父 Agent 可按稳定 alias 查阅受权限保护的本地资料。
- [`terminal-output-plan.md`](terminal-output-plan.md) — 终端输出、流式观察、CLI 交互和三种输出模式的实施计划（A–D 已完成）。

## 文档约定
- 文件名用小写 + 连字符，如 `add-shell-tool.md`。
- 每篇文档建议包含：目标、方案、任务拆解、验收标准。
- 完成后更新对应计划、教程索引、CHANGELOG 和版本信息；仅当运行时约束变化时更新 `AGENTS.md`。
