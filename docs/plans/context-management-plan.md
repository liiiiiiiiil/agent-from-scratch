# 阶段四：Context Management 实施计划

> 状态：v0.11 已实施并验收；v0.12/v0.13 待实施
> 评审结论：原六 Phase 方案依赖顺序正确、原则正确、粒度偏细，合并为三个版本落地；修正项见"关键架构决策"
> 版本映射：v0.11 / v0.12 / v0.13（tag 命名弃用 v0.011，理由见 D8）
> 关联文档：`teaching-repo-plan.md`（版本切片表已同步）、`AGENTS.md` 路线图

## 1. 目标与定位

把 Context Management 设为教学**阶段四**（继阶段一 Agent Loop、阶段二工具与安全、阶段三 Mini Agent 里程碑之后），分三个小阶段落地：

| 小阶段 | 版本 | 主题 | 合并自原方案 | 教程 |
|---|---|---|---|---|
| 4.1 | v0.11 | 上下文架构 | Phase 0 + Phase 1 | `11-context-architecture.md` |
| 4.2 | v0.12 | 预算与裁剪 | Phase 2 + Phase 3 | `12-token-budget-trimming.md` |
| 4.3 | v0.13 | 上下文压缩 | Phase 4 + `MAX_ITERATIONS` 调大 | `13-context-compaction.md` |

阶段主线：**4.1 是概念革命（Agent State ≠ LLM Context），4.2/4.3 是工程实现**——先改世界观，再改代码。

原方案 Phase 5（Tool Result Management）、Phase 6（External Memory & Retrieval）不在本阶段：Phase 5 的最小子集（`run_shell` 2000 字符截断）已在 v0.10 隐式落地，其余并入 v0.13 可选收尾或作为阶段五开篇，实施时再定；Phase 6 归阶段五。

## 2. 关键架构决策（评审修正项）

以下决策为计划评审确定的约束，实施时勿偏离：

| # | 决策 | 理由 |
|---|---|---|
| D1 | **Agent State ≠ LLM Context**：`AgentState` 独立对象，永不参与裁剪/压缩 | State 是压缩后不失忆的锚；PermissionGate 的 always 状态已验证此分离可行 |
| D2 | **token 计数用启发式 `len(text) // 3`**（中英混合场景），不引入 tiktoken | 零第三方依赖约束；估算只喂 trim 决策，±20% 误差无害 |
| D3 | **trimming 按轮次原子删除**：一轮 = assistant(tool_calls) + 其全部 tool results，绝不拆散 | OpenAI 协议要求 tool message 紧跟对应 tool_calls，孤儿 tool result 直接 400 |
| D4 | **预算用比例配置**，不用固定 K 数 | 适应不同模型窗口；system/task 保底 + history/tool 按比例 + output reserve |
| D5 | **State 由 Executor 结果回调更新**，loop 不感知 | 否则 State 会变成第二个无人维护的 messages |
| D6 | **compaction 摘要失败降级为纯 trimming**，不崩 | 容错放 ContextManager 层而非 loop——保持"loop 无 try/except 兜底"的既有约束 |
| D7 | **Summary 允许有损，State 必须准确**：多次 compaction 的验收看 State，不苛求 summary 质量 | summary-of-summary 必然漂移，State 是校正锚 |
| D8 | **tag 命名 v0.11（弃用 v0.011）** | v0.10 已存在，v0.011 视觉上像 v0.01 的补丁且排序有歧义；v0.11 是 v0.10 的自然延续 |

## 3. v0.11 上下文架构（4.1）

### 目标

一句话：**Agent State 独立于 messages，ContextManager 成为 LLM 调用前的统一入口。** 本版是纯重构，外部行为与 v0.10 完全一致——概念先于工程。

### 新增/改动文件

| 文件 | 动作 | 内容 |
|---|---|---|
| `src/mini_agent/state.py` | 新增 | `AgentState` 数据类 + 执行记录接口 |
| `src/mini_agent/context.py` | 新增 | `ContextManager` |
| `src/mini_agent/agent.py` | 修改 | `agent_loop` 改经 `prepare_messages()` 调 LLM |
| `src/mini_agent/tools/base.py` | 修改 | Executor 加结果回调（D5） |
| `src/mini_agent/__main__.py` | 修改 | 组装 AgentState + ContextManager 注入 loop |
| `tests/test_state.py` | 新增 | State 单测 |
| `tests/test_context.py` | 新增 | ContextManager 单测 |

### 接口骨架

```python
# state.py
@dataclass
class AgentState:
    task: str = ""
    current_goal: str = ""
    tool_history: list[dict] = field(default_factory=list)  # {tool, args, ok, brief}
    files_changed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    status: str = "running"  # running / done / failed

    def record_tool(self, name, args, ok, brief): ...
```

```python
# context.py
class ContextManager:
    def __init__(self, state: AgentState, history: list[dict]): ...

    def prepare_messages(self) -> list[dict]:
        """LLM 调用前统一入口。本版直接返回完整 messages（立边界，不裁剪）。"""
```

内部层次（system / task / history / tool）本版只立代码边界，不分别实现定制。

### 任务拆解

1. `state.py` + `test_state.py`（已完成）
2. `context.py` 骨架 + `test_context.py`（本版 `prepare_messages` 恒等返回，已完成）
3. Executor 结果回调 → `state.record_tool` / `files_changed` / `errors`（D5，已完成）
4. `agent_loop` 改造：`call_llm(cm.prepare_messages())`；顺带消除 agent.py 中"MAX_ITERATIONS 半截状态"注释对应的调用方契约（已完成）
5. 手工回归：跑 v0.10 的典型任务，行为不变（已完成，真实 LLM 回归通过）
6. 教程 `11-context-architecture.md` + CHANGELOG + AGENTS.md 打勾 + tag `v0.11`（已完成）

### 验收标准

- [x] AgentState 独立对象，不进 messages、不被 loop 直接操作
- [x] 所有 LLM 请求经 ContextManager 构建，loop 不再直接拼接 context
- [x] System / Task / History / Tool Result 的边界有明确代码位置
- [x] 行为与 v0.10 一致（现有 tests 全绿 + 手工任务回归）
- [x] `git diff v0.10..v0.11 --stat` 在"两个新文件 + 三四个文件小改"量级

## 4. v0.12 预算与裁剪（4.2）

### 目标

一句话：**Context 是有限资源：超限时优先删除低价值信息，而不是直接失败。**

### 新增/改动文件

全部在 `context.py` 内扩展 + `config.py` 加配置，不新增文件：

```python
def count_tokens(text_or_messages) -> int  # D2: len // 3

@dataclass
class ContextBudget:
    window: int                  # config: CONTEXT_WINDOW，默认 128_000
    output_reserve_ratio: float  # 默认 0.15
    history_ratio: float         # 默认 0.45（system/task 保底额从中优先扣）

class TrimPolicy:
    def trim(self, messages, budget) -> list[dict]  # 返回裁剪后 messages，打印裁剪日志
```

`config.py` 新增 `CONTEXT_WINDOW`；`prepare_messages()` 内做超限检查，超限自动进入裁剪。

### 裁剪算法（按价值从低到高删除）

1. **轮次划分**：messages 按 D3 划分为 rounds；system 与首条 user task 永不入 rounds
2. 老轮次的 tool result 内容截断（保留首尾 + 省略行数提示，原地替换 content）
3. 仍超限 → 从最老轮次开始**整轮原子删除**（D3）
4. 每步打印裁剪日志（删了哪些轮、各省多少 token）——教程观察点

### 任务拆解

1. `count_tokens` + `ContextBudget` + 单测
2. 轮次划分器 + 单测（构造含 tool_calls 的消息序列，验证划分正确、无孤儿）
3. tool result 截断策略 + 单测
4. 整轮删除 + 预算循环收敛 + 单测
5. `prepare_messages` 接入超限检查；手工构造长任务（如让 agent 反复 `read_file` 大文件）
6. 教程 `12-token-budget-trimming.md` + CHANGELOG + AGENTS.md + tag `v0.12`

### 验收标准

- [ ] LLM 调用前检查 token 预算，超限自动进入裁剪流程
- [ ] tool result 支持截断；history 支持按轮次删除
- [ ] System / 首条 user task 在任何情况下不被删（单测断言）
- [ ] 裁剪后消息序列协议合法：无孤儿 tool result（单测断言，D3 正确性红线）
- [ ] 预算可配置（window 与比例）
- [ ] 超限时 agent 不崩，能继续任务
- [ ] 裁剪有明确优先级且日志可观察

## 5. v0.13 上下文压缩（4.3）

### 目标

一句话：**历史信息不能简单删除：老历史摘要为 Summary，State 锚定事实。**

### 压缩后 Context 结构

```
System
+ Current Task
+ Structured State（由 AgentState 渲染，D7 的锚）
+ Historical Summary（可多次叠加，允许有损）
+ Recent Messages（近 N 轮原文，默认 6 轮）
```

### 实现要点

```python
# context.py
def compact(self, keep_rounds: int = 6) -> None:
    """老轮次 -> 无 tools 的 call_llm 摘要 -> 替换为一条 summary message。"""
```

- 摘要 prompt 用固定模板：任务 / 已做 / 已改 / 结论 / 下一步（与 Structured State 字段对齐）
- 触发时机：trimming 删到"只剩保底轮次仍超限"时自动触发
- 摘要失败降级为纯 trimming，agent 不崩（D6，容错在 ContextManager 层）
- Structured State 每次 compaction 时由 `AgentState` 重新渲染注入（非追加），多次压缩不膨胀
- `config.py`：`MAX_ITERATIONS` 10 → 50

### 任务拆解

1. 摘要调用（复用 `call_llm`，不带 tools 参数）+ 固定 prompt 模板
2. `compact` 主流程 + 单测（mock LLM，零网络依赖）
3. 失败降级路径 + 单测
4. Structured State 渲染注入 + 单测
5. trimming → compaction 自动触发链路
6. `MAX_ITERATIONS` 调大；端到端长任务手工验证（压缩后继续原任务、多次压缩 State 仍准）
7. （可选收尾，Phase 5 前置）`read_file`/`grep` 结果接入统一的截断处理
8. 教程 `13-context-compaction.md` + CHANGELOG + AGENTS.md + tag `v0.13`

### 验收标准

- [ ] 老历史可自动摘要，压缩后 agent 能继续原任务
- [ ] Structured State 始终在 context 中，且由真实执行记录维护（非 summary 转述）
- [ ] Recent history 保留原文
- [ ] 摘要失败时降级为 trimming，agent 不崩
- [ ] 支持多次 compaction：连续触发 2+ 次后 State 字段仍准确（单测，D7）
- [ ] 长任务（>10 轮工具调用）不因 MAX_ITERATIONS 静默截断

## 6. 测试与验证总表

| 版本 | 新增测试 | 手工验证 |
|---|---|---|
| v0.11 | State 记录更新 / prepare_messages 恒等构建 | v0.10 典型任务回归 |
| v0.12 | 轮次划分无孤儿 / 保底不删 / 预算循环收敛 | 反复读大文件触发裁剪，观察日志 |
| v0.13 | compact mock / 失败降级 / 多次压缩 State 准确 | 20+ 轮长任务跨压缩完成 |

测试原则：单测全部 mock LLM（零网络依赖），风格与现有 `tests/` 一致；协议合法性（无孤儿 tool result）是唯一红线断言。

## 7. 与既有文档的同步

- `teaching-repo-plan.md`：版本切片表 / 目录树 / 使用指导表 / 代码差异要点已同步为 v0.11–v0.13，plan 引导顺移至 v0.14
- `AGENTS.md`：路线图与教学阶段划分已同步；每版完成时打勾并更新"当前架构"
- `docs/plans/README.md`：索引已加本文档
- `docs/tutorials/README.md`：阶段四改为 Context Management 三版表
