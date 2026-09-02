# AGENTS.md

## 项目定位

`agent-from-scratch` 是逐步生长的 Python 编程 agent（包名 `mini_agent`），运行时仅使用标准库。
本文件只保存会直接影响 agent 运行、授权和代码修改的硬约束；版本路线、教程规范和完整架构见 `docs/`。

## 执行规则

- **先调查再修改**：先阅读相关实现、测试和计划，确认现有行为与边界，再提出最小改动。
- **标准库优先**：运行时不得引入第三方依赖；保持仓库可自包含部署。
- **HTTP 客户端约束**：LLM 调用必须使用 `http.client`，请求显式设置 `Accept-Encoding: identity`；不要改用 `requests` 或 `urllib`。
- **配置安全**：真实的 `BASE_URL`、`API_KEY`、`MODEL` 只放本地 `config_local.py`，不得提交到版本库。
- **异常边界**：工具层/执行器负责把 handler 异常转换为错误结果并回灌模型；核心 agent loop 不对 LLM 或 CLI 顶层异常做兜底。
- **协议完整**：工具调用必须为每个 call 回灌对应的 `role=tool` 结果；单轮工具结果全部回灌后再进入下一轮。
- **完成与上限**：无 `tool_calls` 才能结束；Todo、修改后的验证等完成条件由当前实现决定；默认最多 50 轮，超限返回明确结果。
- **修改后验证**：文件修改完成后，至少运行与改动相关的测试；交付前运行下列完整验证命令（或说明无法运行的原因）。
- **破坏性操作**：未经用户明确授权，不执行删除、重置、覆盖大量文件或其他难以恢复的操作。

## 当前状态

稳定基线为 `v0.16`（计划驱动执行，Plan-driven Execution）；后续版本处于规划中。新增功能意图记录在对应 `docs/plans/`，只有运行时硬约束变化才更新本文件。

## 架构索引

- `src/mini_agent/agent.py`：LLM 调用与 agent loop。
- `context.py`：每轮上下文视图、预算裁剪、历史压缩和受保护指令注入。
- `state.py`：独立于消息历史的任务、Todo、工具和验证状态。
- `permission.py`：按工具与参数模式匹配的 allow/deny/ask 权限闸门。
- `prompt.py`：分层 system prompt；`instructions.py`：发现并合并项目 `AGENTS.md`。
- `tools/`：标准工具注册、执行，以及文件、shell、计算能力；执行器负责权限和错误结果边界。

完整目录、参数、数据结构和运行时流程以[操作手册](docs/operation/manual.md)、[上下文架构说明](docs/operation/context-architecture.md)及对应版本教程为准。

## 常用验证

```bash
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python scripts/check_tutorials.py
```

未安装 pytest 时，可直接运行 `tests/` 中带标准库入口的 smoke test；运行方式见操作手册。

## 文档索引

- [操作手册](docs/operation/manual.md)：配置、运行、工具和故障排查。
- [教程索引](docs/tutorials/README.md)：按版本和阶段学习、复现与验收。
- [治理文档](docs/governance/README.md)：约束、规范和决策记录（含[教程作者规范](docs/governance/tutorial-authoring.md)）。
- [计划文档](docs/plans/README.md)：路线图、功能计划和任务拆解。
