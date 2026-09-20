# 第 34 课：最小受控子代理委派

> 代码快照：`v0.34` · 相邻差异：`v0.33..v0.34` · 命令环境：Bash/zsh

本课对应阶段十“受控子代理委派”。代码链接固定到由仓库维护者创建的 `v0.34` tag；阅读和运行本课均不需要创建 tag。

## 本课目标

上一课已经能把崩溃后的不确定调用安全地交给新的 session，但父 Agent 仍要亲自完成所有调查。调查文件、搜索定义和计算数字会占用父 Context，也容易把互不相关的线索混在一起。

本课加入一个最小 Subagent（子代理）：父 Agent 用 `delegate_task` 委派一个范围清楚的调查，子代理在自己的 Context 中只读地收集材料，再把一个有界的 JSON 报告交回父 Agent。这里的“委派”是分担认知调查，不是把工作区修改权分出去。

完成本课后，父 Agent 可以同步运行一个、单层的只读子代理；子代理不能写文件、执行 shell、操作进程、调用计划工具或再次委派。父 Agent 仍是唯一的修改者、权限请求者、计划维护者、权威验证者和完成判定者。

## 前置条件

- 已阅读第 33 课[崩溃恢复与不确定副作用交接](33-crash-recovery.md)。
- 具备基础 Python、JSON、函数调用和相对路径知识。
- 能运行 `PYTHONPATH=src python -m pytest -q`；本课的 fake LLM 测试不需要真实 API key。

## 为什么需要本版

工具并发和子代理委派解决的是两个不同问题：工具并发仍然是同一个 Agent 的一个回合，调用共享同一份 State、Context 和权限；子代理委派则创建新的 State、Context、loop 和提示词，只把明确选择的父事实和合同传给子代理。

```text
v0.33：父 Agent → 多个工具 → 父 Context → 父计划/修改/验证

v0.34：父 Agent → delegate_task
                 ↓
          独立 Subagent State/Context
          ├─ calculate / read_file / list_dir / grep
          └─ 严格 JSON 报告
                 ↓
          父 Context 中一个 role=tool 结果
```

子代理报告是“不可信调查材料”：它能指出文件位置和只读观察，却不能把“测试通过”变成父任务的 verification evidence。父 Agent 仍需自己复查和运行独立验证。

## 新增与改动文件

- [`src/mini_agent/delegation.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.34/src/mini_agent/delegation.py)：冻结合同、预算、scope gate、结果校验、`DelegationManager` 和 `SubagentRunner`。
- [`src/mini_agent/runtime.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.34/src/mini_agent/runtime.py)：可实例化的 Runtime 壳；父 loop 保留 v0.33 兼容实现，通用路径复用 tool-call 协议。
- [`src/mini_agent/tools/base.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.34/src/mini_agent/tools/base.py)：显式 `delegation_capability` 与冻结 `FilteredToolRegistryView`。
- [`src/mini_agent/tools/delegation.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.34/src/mini_agent/tools/delegation.py)：父侧 `delegate_task` 工具及参数校验。
- [`src/mini_agent/tools/__init__.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.34/src/mini_agent/tools/__init__.py)：真实父 Runtime 注册委派 Manager 和工具。
- [`src/mini_agent/prompt.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.34/src/mini_agent/prompt.py)：父委派规则和子代理身份提示词。
- [`tests/test_subagent_v034.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.34/tests/test_subagent_v034.py)：合同、隔离、越权工具和结果格式修正测试。

查看相邻版本差异：

```bash
git checkout v0.34
git diff --stat v0.33..v0.34
```

## 关键流程

一次委派的边界如下：

```text
父模型生成 delegate_task
  → 父 loop 检查它必须独占回合
  → PermissionGate / phase gate / 合同校验
  → Manager 分配 UUID、冻结合同、检查单子代理锁
  → 子 Runtime 加载 AGENTS.md，建立独立 State/Context/Registry view
  → 子模型调用四个只读工具并按序回灌 role=tool
  → 子模型输出报告 JSON，Runtime 校验或给一次格式修正提醒
  → 父得到一个有界 JSON tool result
```

父回合若同时包含 `delegate_task` 和另一个工具，或者包含两个 `delegate_task`，整轮的调用都会得到 `delegation_batch_gate`，不会启动子模型。这样可以保留 v0.34 的同步边界，也避免旧的只读线程池提前并行启动子代理。

## 实现拆解

### 1. 合同和固定预算

合同包含 `goal`、`scope`、`constraints`、`expected_findings`、`requested_tools`、`selected_parent_facts`、`purpose` 和可选的 `budget`。Runtime 生成 `delegation_id`、`subagent_id`、合同 hash 和 `depth=1`；模型不能伪造运行 ID 或提高深度。

固定上限是 8 轮、8 次 LLM 调用、24 次工具调用、约 32,000 个估算 token、12 KiB 结果和 120 秒墙钟时间。模型只能请求更小预算；v0.34 没有 provider usage、父聚合预算或后台取消。token 使用 `ContextManager.count_tokens()` 的保守估算，并标记 `token_accounting=estimated`。

### 2. Scope 和能力过滤

`ScopeGate` 要求 scope 是工作区内的 1–8 个相对路径，拒绝绝对路径、`..`、`config_local.py` 和 realpath 后逃出工作区的符号链接。每次 `read_file`、`list_dir`、`grep` 调用都会再次检查实际路径。

工具能力是独立元数据，不能从 `effect_class=none` 推断。只有 `calculate` 的 `pure_compute` 和三个工作区只读工具的 `readonly_workspace` 能进入冻结 Registry view；计划、恢复、进程观察、shell、写文件和 `delegate_task` 都不可见。子代理的 PermissionGate 是新建的固定 allow 策略，不继承父会话的 `once/always` 授权。

### 3. 独立上下文和结果报告

子 Context 只有子身份规则、重新发现的项目指令、冻结合同、selected facts 和自己的消息。父 history、父 State、父权限、父计划和父 verification 不会复制进去。子代理第一次输出非法报告时收到一次受保护 Runtime Notice；第二次仍非法，或修正阶段再次调用工具，结果为 `failed/invalid_result`。

报告体固定为：

```json
{
  "summary": "简短结论",
  "findings": [
    {"id": "f1", "claim": "...", "evidence_ids": ["e1"], "confidence": "observed", "caveat": null}
  ],
  "evidence": [
    {"id": "e1", "kind": "tool_observation", "claim": "...", "tool": "read_file", "path": "src/example.py", "observation_hash": "<sha256>"}
  ],
  "limitations": []
}
```

Runtime 检查 evidence ID 唯一、finding 引用不悬空、路径仍在 scope、行号为正数、工具名合法、hash 是 SHA-256；推断性 finding 必须同时有证据和 caveat。随后 Runtime 补充 `result_id`、父子 ID、outcome、usage 和时间戳，模型不能伪造这些事实。

`tool_observation` 的 `observation_hash` 必须来自本次子代理成功只读调用，不能只提交格式正确的随机 hash；`file_location` 也必须对应本次实际观察到的 scope 内路径。这样父 Agent 收到的是可复查的调查线索，而不是脱离工具事实的模型断言。

### 4. 父侧交付和状态边界

子代理失败、超时或预算耗尽仍返回唯一的结构化 tool result，不自动创建父 `FailureEvent`。父 Agent 的 `AgentState` 只按现有协议记录一次 `delegate_task` ExecutionAttempt；子结果不推进 Plan、generation 或 `verification_evidence`。开启 `/save` 时，委派仍经过父 durable boundary；v0.34 不保存 DelegationRecord、子 Context 或委派生命周期，也不恢复运行中的子代理。

## 运行与观察

运行全套验证：

```bash
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python scripts/check_tutorials.py
PYTHONPATH=src python scripts/check_readme.py
```

在 fake LLM 测试中，可以观察到父 Context 只有一个 `delegate_task` assistant call、一个对应的 `role=tool` JSON 结果和父自己的最终回复；子代理的工具消息保留在 `SubagentRunner.last_context`，不会泄漏到父 history。若让子模型请求 `write_file`、`run_shell` 或 `delegate_task`，它会收到 unknown/forbidden 结果，handler 不会执行。

## 为什么这样设计

- 单个同步子代理让父工具协议仍然清楚：一个父 call 对应一个结果，结果按模型顺序提交。
- 独立 State 和 Context 防止子代理意外改变父计划、权限、generation 或终态。
- 显式能力比 `effect_class=none` 更安全，因为有些无副作用工具仍绑定父任务状态或进程资源。
- 严格 evidence 引用让调查结论可以被父 Agent 复查，同时避免把子代理意见冒充权威验证。
- 固定预算和同步边界适合第一版教学实现；更复杂的预算聚合、取消和持久生命周期会增加状态机与恢复语义，留到后续版本。

## 本版特性、下一课与代码索引

本版实现的是最小受控委派：一个父任务同一时间最多一个子代理、同步等待、depth=1、四个只读工具、固定护栏和结构化结果。尚未实现多子代理并行、后台取消、父任务聚合预算、可持久化委派记录或跨 session 恢复。

下一课将处理子代理生命周期和可配置/聚合预算；再之后才讨论有界并行和 durable delegation。相关设计见 [`docs/plans/subagent-delegation-plan.md`](../plans/subagent-delegation-plan.md)，最新操作约束见 [`docs/operation/manual.md`](../operation/manual.md)。
