# 阶段五：Project-Aware Task Orchestration 实施计划

> 状态：已完成（v0.14–v0.16 已实现并验收）
> 版本映射：v0.14 / v0.15 / v0.16
> 前置阶段：`context-management-plan.md`（v0.11–v0.13）
> 关联文档：`teaching-repo-plan.md`；运行时硬约束见 `AGENTS.md`

## 1. 目标与定位

把 Project-Aware Task Orchestration 设为教学**阶段五**。阶段四解决“在有限 Context Window 下记得住”，阶段五继续解决“理解项目约束、明确下一步，并持续做到可验证完成”。

一句话目标：**让 Agent 先理解当前项目的规则，再用独立于 messages 的任务状态组织复杂工作，最后形成规划、执行、验证和收口的闭环。**

阶段五分三个小阶段落地：

| 小阶段 | 版本 | 主题 | 核心问题 | 教程 |
|---|---|---|---|---|
| 5.1 | v0.14 | Project Instructions | 在当前项目中必须遵守什么规则？ | `14-project-instructions.md` |
| 5.2 | v0.15 | Todo / Task State | 准备做什么、当前做到哪一步？ | `15-task-state.md` |
| 5.3 | v0.16 | Plan-driven Execution | 如何根据结果推进、调整并验证计划？ | `16-plan-driven-execution.md` |

阶段主线：

```text
发现并加载项目规则
        ↓
Plan：建立符合规则的动态 Todo
        ↓
Execute：执行工具
        ↓
Observe：读取工具结果、错误和环境变化
        ↓
Replan：根据事实推进或调整 Todo
        ↓
Verify：执行验证并记录完成证据
```

这三个版本不是三个独立功能：Project Instructions 是规划的约束输入，Todo 是 Agent 的动态工作计划，Execution State 是 Runtime 的事实，Verification 是任务完成的证据；Plan-driven Execution 让它们共同参与任务生命周期。

## 2. 范围与非目标

### 2.1 本阶段范围

- 自动发现并注入适用于当前工作目录的 `AGENTS.md`
- 区分 Project Instructions、Task State、Execution State、Verification 和 LLM Context
- 用显式 Todo 表示复杂任务的动态工作计划
- 由模型通过受控工具更新 Todo，而不是从自然语言中猜测进度
- 在 Context compaction 后重新注入准确的项目规则与任务状态
- 建立“Plan → Execute → Observe → Replan → Verify”的最小闭环

### 2.2 本阶段不做

- 不做独立 Plan Agent、Explore Agent 或多 agent 协作
- 不做 `--plan` 只读模式及计划审批/交接协议
- 不加载 `.cursorrules`、`CLAUDE.md` 等多种指令文件，首版只认 `AGENTS.md`
- 不做跨进程 Todo 持久化，进程退出后状态仍然消失
- 不让 Project Instructions 修改 `PermissionGate` 的运行时权限
- 不做失败自动重试框架、checkpoint、事务回滚或沙箱隔离
- 不引入第三方依赖

这些能力会显著改变权限、执行器或 agent 拓扑，留给后续阶段逐项引入。

## 3. 关键架构决策

以下决策在实现前固定，避免三个版本各自形成一套状态或规则系统：

| # | 决策 | 理由 |
|---|---|---|
| D1 | **Project Instructions 是受保护上下文，不是 history** | 项目规则不应随老对话一起被 trimming 或 compaction 删除 |
| D2 | **首版只加载 `AGENTS.md`，作用域为启动时工作目录** | 一个版本只引入一个概念；目标文件级动态指令发现留待真实需求出现后扩展 |
| D3 | **多层指令按 root → cwd 合并，越近的文件越靠后** | 顺序清晰，便于模型将更具体的规则视为高优先级；不在代码中尝试解析自然语言冲突 |
| D4 | **Project Instructions 不能放宽 PermissionGate** | 提示词是行为约束，不是安全授权；权限仍只由 `PermissionGate` 决定 |
| D5 | **Execution State 与 Task State 共享 `AgentState`，但字段所有权不同** | 避免再造平行 `PlanState`；同时防止模型篡改工具执行事实 |
| D6 | **Execution State 由 Executor 回调维护；Task State 由模型通过 Todo 工具维护** | 事实来自真实执行，计划来自模型意图，两者来源可审计 |
| D7 | **Todo 是复杂任务的工作机制，不强迫简单任务先规划** | 保持简单任务的最短路径，避免为一次读取或计算增加无意义调用 |
| D8 | **同一时刻最多一个 `in_progress` Todo** | 当前工作焦点唯一，渲染和状态转换都更容易理解 |
| D9 | **compaction 可以重新渲染或结构化压缩 Task State，但必须语义保真** | 当前目标、Todo 内容与状态、阻塞原因不能像 Conversation History 一样被有损摘要丢失 |
| D10 | **Todo 是 Agent 的意图，Execution State 是 Runtime 的事实，Verification 是完成的证据** | 三者来源不同且可审计；计划声明不能替代执行事实或验证证据 |
| D11 | **工具执行事实优先于计划声明，验证证据优先于“看起来已完成”** | Todo 标记完成不代表任务真实完成；文件改动、命令结果和测试结果必须分别记录 |
| D12 | **v0.16 先做单 agent 的计划驱动执行，不引入独立 Plan Mode** | 先验证显式 Todo 是否足够，再决定是否值得增加 agent 角色与交接协议 |

## 4. 上下文与状态模型

阶段五完成后的 LLM Context：

```text
System Identity + Core Rules
+ Environment
+ Project Instructions       # 静态约束，受保护
+ Current Task
+ Structured State
  ├── Task State             # current_goal + dynamic todos
  ├── Execution State        # files_changed + errors + runtime facts
  └── Verification           # test/build/check evidence
+ Historical Summary
+ Recent Messages
```

五类信息的职责边界：

| 信息 | 来源 | 谁维护 | 是否可被压缩 |
|---|---|---|---|
| Project Instructions | `AGENTS.md` | `InstructionLoader` | 否 |
| Task State | Todo / 当前目标 / 阻塞原因 | LLM 经 Todo 工具 | 可重渲染或无损结构化压缩，必须语义保真 |
| Execution State | 工具结果 / 文件改动 / 错误 | Executor 回调 | 否，事实不可由摘要替代 |
| Verification | 测试、构建、检查等完成证据 | 工具结果记录，ContextManager 渲染 | 否，证据内容必须准确 |
| Conversation History | user / assistant / tool messages | agent loop | 是 |

`AgentState` 是运行时事实、任务进度和验证证据的容器，但不能整体开放给模型修改。Todo 工具只允许更新 Task State 字段，Executor 仍独占 Execution State 与 Verification 的原始执行记录；compaction 只负责重新渲染这些结构化信息。

## 5. v0.14 Project Instructions（5.1）

### 5.1 目标

一句话：**Agent 启动时自动发现当前项目的 `AGENTS.md`，将适用规则作为受保护的 system context 注入每次 LLM 请求。**

### 5.2 新增/改动文件

| 文件 | 动作 | 内容 |
|---|---|---|
| `src/mini_agent/instructions.py` | 新增 | `InstructionLoader`：发现、读取、合并 `AGENTS.md` |
| `src/mini_agent/prompt.py` | 修改 | 新增 `<project_instructions>` 分层渲染 |
| `src/mini_agent/__main__.py` | 修改 | 启动时加载一次项目指令并注入组装流程 |
| `src/mini_agent/context.py` | 修改 | 明确 Project Instructions 属于 protected context |
| `tests/test_instructions.py` | 新增 | 发现顺序、缺失文件、读取失败、边界测试 |
| `tests/test_prompt.py` | 修改 | prompt 注入 smoke test |

### 5.3 接口骨架

```python
class InstructionLoader:
    def __init__(self, cwd: str): ...

    def discover(self) -> list[str]:
        """返回 root → cwd 顺序的 AGENTS.md 路径。"""

    def load(self) -> str:
        """读取并按来源分段合并；找不到时返回空字符串。"""
```

建议渲染时保留来源，便于模型和用户定位规则：

```text
<project_instructions>
Source: /repo/AGENTS.md
...

Source: /repo/subdir/AGENTS.md
...
</project_instructions>
```

### 5.4 发现与合并规则

1. 以启动时 `cwd` 为作用域终点
2. 若位于 git 仓库内，以仓库根目录为搜索起点；否则只检查 `cwd`
3. 收集 root 到 cwd 路径链上的 `AGENTS.md`
4. 按 root → cwd 顺序合并，具体规则位于后面
5. 不执行指令文件中的命令，不将其转换为权限规则
6. 对总字符数设置标准库级简单上限，超限时保留文件来源并明确标记截断

首版不处理“工具后来访问 cwd 之外或更深目录时动态切换规则”。教程必须明确这是 v0.14 的作用域边界，避免声称已经实现目标文件级规则继承。

### 5.5 任务拆解

1. `InstructionLoader.discover()` + 临时目录单测
2. 多层 `AGENTS.md` 的 root → cwd 合并 + 来源标记
3. 空文件、无文件、非 git 目录和读取错误路径
4. `prompt.py` 增加 Project Instructions 分层
5. `ContextManager` 将指令视为 protected context，预算超限也不删除
6. CLI 组装接入，手工验证模型能复述并遵守仓库特定规则
7. 教程、CHANGELOG、README、版本号与 tag `v0.14`（运行时约束变化时同步 AGENTS.md）

### 5.6 验收标准

- [ ] 当前工作目录适用的 `AGENTS.md` 会被自动发现
- [ ] 多层文件按 root → cwd 稳定合并，并保留来源信息
- [ ] 无 `AGENTS.md` 时行为与 v0.13 一致
- [ ] Project Instructions 不进入 history，不被 trimming/compaction 删除
- [ ] 指令不能绕过 `PermissionGate`
- [ ] 加载上限和截断行为有测试且可观察
- [ ] 单测不依赖真实用户目录或网络

## 6. v0.15 Todo / Task State（5.2）

### 6.1 目标

一句话：**把模型脑中的动态工作计划变成独立于 messages、可验证且可在 compaction 后恢复的 Task State。**

### 6.2 数据模型

```python
@dataclass
class TodoItem:
    content: str
    status: str = "pending"  # pending / in_progress / completed


@dataclass
class AgentState:
    task: str = ""
    current_goal: str = ""
    todos: list[TodoItem] = field(default_factory=list)
    # 既有 Execution State 字段保持不变
```

建议由 `AgentState.update_todos()` 集中校验不变量，不让工具 handler 直接改公开 list：

- `content` 必须是去除首尾空白后的非空字符串
- `status` 只能是 `pending / in_progress / completed`
- 同时最多一个 `in_progress`
- Todo 数量与单项长度设置小上限，防止 Task State 无限制膨胀
- 工具采用“提交完整列表”语义，更新具有原子性，校验失败时不写入半份状态

### 6.3 新增/改动文件

| 文件 | 动作 | 内容 |
|---|---|---|
| `src/mini_agent/state.py` | 修改 | `TodoItem`、`todos`、原子更新和 snapshot 支持 |
| `src/mini_agent/tools/todo.py` | 新增 | 创建绑定当前 `AgentState` 的 `update_todo` 工具 |
| `src/mini_agent/tools/__init__.py` | 修改 | 支持启动时注册状态绑定工具 |
| `src/mini_agent/__main__.py` | 修改 | 将当前 state 绑定到 Todo 工具 |
| `src/mini_agent/context.py` | 修改 | Structured State 渲染 Task State |
| `src/mini_agent/prompt.py` | 修改 | 说明复杂任务使用 Todo、简单任务无需 Todo |
| `tests/test_state.py` | 修改 | Todo 不变量、深拷贝、并发一致性 |
| `tests/test_tools.py` | 修改 | Todo 工具 schema 与状态更新 |
| `tests/test_context.py` | 修改 | compaction 后 Todo 准确注入 |

### 6.4 工具接口

```json
{
  "name": "update_todo",
  "arguments": {
    "todos": [
      {"content": "读取项目规则和相关实现", "status": "completed"},
      {"content": "修改上下文构建逻辑", "status": "in_progress"},
      {"content": "补充并运行测试", "status": "pending"}
    ]
  }
}
```

采用完整列表替换而不是 `add/start/complete` 多个命令，可以让模型一次表达计划调整，也让状态校验保持集中。工具执行成功后，`current_goal` 从唯一的 `in_progress` 项派生；没有进行中项时为空。

Todo 工具是内存状态工具，不应触发文件写入权限；它也不能修改 `tool_history`、`files_changed`、`errors` 或整个任务的最终 `status`。

### 6.5 任务拆解

1. `TodoItem` + `AgentState.update_todos()` + 不变量单测
2. `snapshot()` 加 Task State，验证调用方无法通过返回值修改原状态
3. 状态绑定的 `update_todo` 工具 + schema/handler 单测
4. CLI 注册工具，保证不同 `AgentState` 不共享 Todo
5. ContextManager 将最新 Task State 每轮重新渲染，非追加进 history
6. 验证连续 2+ 次 compaction 后 Todo 与 `current_goal` 保持准确
7. prompt 加最小使用规则；手工运行一个三步编程任务
8. 教程、CHANGELOG、README、版本号与 tag `v0.15`（运行时约束变化时同步 AGENTS.md）

### 6.6 验收标准

- [ ] 模型可通过 `update_todo` 创建、推进和调整计划
- [ ] Todo 更新原子化，非法状态不会部分写入
- [ ] 同时最多一个 `in_progress`，`current_goal` 与其一致
- [ ] Execution State 不能被 Todo 工具修改
- [ ] Todo 不只存在于 messages，trimming/compaction 后仍准确
- [ ] Structured State 每轮反映最新 Todo，且不会重复膨胀
- [ ] 简单任务无需 Todo 即可直接完成

## 7. v0.16 Plan-driven Execution（5.3）

### 7.1 目标

一句话：**让 Todo 从静态 checklist 升级为动态工作协议，使复杂任务按“Plan → Execute → Observe → Replan → Verify”循环推进。**

v0.15 解决“计划存在哪里”，v0.16 解决“什么时候建计划、如何维护，以及何时算完成”。本版仍是单 agent，不增加新的 agent 角色。

### 7.2 动态任务生命周期

```text
收到任务
  ├── 简单任务 → 直接执行 → 验证（如适用）→ 回复
  └── 复杂任务 → Plan：建立或更新 Todo
                   ↓
                Execute：标记当前项并执行工具
                   ↓
                Observe：读取结果、错误和环境变化
                   ↓
             Replan：推进、重排、删除或新增 Todo
                   ↓
                Verify：运行测试/构建/检查并记录证据
                   ├── 未通过 → 回到 Replan
                   └── 通过且无未完成项 → 最终回复
```

Todo 是动态工作计划，不是一次性 checklist。执行结果可能使步骤完成、失败、重排、删除或产生新的必要步骤；模型必须根据 Observe 阶段的事实重新规划，而不是机械地按初始顺序逐项勾选。

### 7.3 复杂任务判断

通过 system prompt 提供可解释的启发式，不在 Python 中硬编码任务分类器。满足任一情况时建议建立 Todo：

- 涉及多个文件或多个相互依赖的步骤
- 需要先调查再修改
- 需要修改后运行测试或其他验证
- 用户明确要求计划、Todo 或进度跟踪
- 执行中发现原任务范围需要拆分

一次读取、一次搜索、简单计算或可直接回答的问题不要求 Todo。

### 7.4 执行协议

- 开始复杂工作前建立短小、面向结果的 Todo
- 执行某一步前将其设为唯一 `in_progress`
- 工具成功不自动完成 Todo，由模型结合结果判断
- 工具失败后先更新或重写计划，避免无变化地重复相同调用
- 新发现的必要工作可以加入 Todo；已完成项可以保留用于展示进度，也可以在计划重排时移除
- “修改代码”与“验证修改”通常是两个步骤
- 验证步骤必须产生可引用的完成证据；Todo 全部 completed 但没有必要的验证证据时，不得将任务表述为已验证完成
- 最终回复前，Todo 应全部完成且验证证据足够；无法完成时必须保留未完成/阻塞状态并如实说明原因

### 7.5 完成检查

本版采用**轻量运行时提醒，不做无限硬阻断**：当模型准备给最终文本回复但仍有 `pending` 或 `in_progress` Todo 时，loop 最多追加一次结构化提醒，让模型选择继续执行，或说明阻塞并显式结束任务。

建议将判断封装在 Task State 与 Verification 层，而不是把 Todo 细节散落到 loop：

```python
class AgentState:
    def unfinished_todos(self) -> list[TodoItem]: ...
    def completion_reminder(self) -> str | None: ...
    def has_verification_evidence(self) -> bool: ...
```

只提醒一次的原因：Todo 是模型维护的意图，不能因状态忘记更新而让 agent 永远无法返回。是否允许显式结束、如何标记阻塞，可根据 v0.16 实施时的真实模型表现选择最小协议并写入教程。

### 7.6 新增/改动文件

| 文件 | 动作 | 内容 |
|---|---|---|
| `src/mini_agent/prompt.py` | 修改 | 复杂任务判断与计划驱动执行规则 |
| `src/mini_agent/state.py` | 修改 | 未完成项查询与完成提醒所需接口 |
| `src/mini_agent/agent.py` | 修改 | 最终回复前最多一次未完成 Todo 提醒 |
| `src/mini_agent/context.py` | 修改 | 提醒后仍保持 Context 协议与预算合法 |
| `tests/test_loop.py` | 修改 | 正常推进、失败调整、一次提醒、简单任务直达 |
| `tests/test_context.py` | 修改 | 提醒跨 trimming/compaction 的合法性 |

### 7.7 任务拆解

1. 定义复杂任务启发式和 Todo 生命周期 prompt
2. `unfinished_todos()` 与一次性 completion reminder
3. loop 接入提醒，但不捕获 LLM 或 loop 主路径异常
4. 单测：简单任务无 Todo 直接结束
5. 单测：复杂任务创建、推进并全部完成
6. 单测：工具失败后计划调整，不污染 Execution State
7. 单测：带未完成 Todo 的最终回复只触发一次提醒，不形成死循环
8. 端到端任务：遵守项目规则，调查、修改、测试；首次验证失败后调整计划并再次验证
9. 教程、CHANGELOG、README、版本号与 tag `v0.16`（运行时约束变化时同步 AGENTS.md）

### 7.8 验收标准

- [ ] 复杂任务会建立并持续维护 Todo，简单任务保持短路径
- [ ] 任一时刻最多一个步骤进行中，执行焦点清晰
- [ ] 工具失败能促使计划调整，而不是把失败当完成
- [ ] 修改任务包含独立验证步骤，验证结果作为真实 Verification 证据记录
- [ ] Todo 完成状态不能替代验证证据；最终收口以 Verification 为依据
- [ ] 未完成 Todo 的最终回复最多触发一次提醒，不会无限循环
- [ ] 无法完成时能保留未完成状态并如实收口
- [ ] Project Instructions、Task State、Execution State 和 Verification 经 compaction 后仍准确

## 8. 测试与验证总表

| 版本 | 新增测试重点 | 手工验证 |
|---|---|---|
| v0.14 | 指令发现顺序 / 缺失降级 / protected context / 长度上限 | 在含 `AGENTS.md` 的仓库启动，确认 Agent 遵守一条仓库特定规则 |
| v0.15 | Todo 原子更新 / 状态不变量 / State 渲染 / 多次 compaction | 执行三步任务，观察 Todo 跨长上下文持续更新 |
| v0.16 | 简单任务直达 / 失败调整 / 验证步骤 / 单次完成提醒 | 修复一个失败测试，首次修复失败后调整计划并最终通过 |
| 阶段 E2E | `tests/test_stage5_e2e.py`：规则加载、Todo、失败重规划、compaction、协议完整性与最终验证 | 临时 Git 项目中脚本化模型响应驱动完整闭环 |

阶段级端到端验收任务：

> 在一个包含 `AGENTS.md` 和失败测试的临时项目中启动 Agent。Agent 需要读取并遵守项目规则，建立 Todo，定位问题，修改代码，运行测试；第一次修改后测试仍失败，Agent 根据真实结果调整 Todo，再次修改并验证通过。任务过程中构造足够历史以触发至少一次 compaction。

最终断言：

- 修改符合 Project Instructions
- Todo 在多轮执行和 compaction 后保持准确，且作为动态计划可被重排或扩展
- `files_changed`、`errors`、`tool_history` 与实际执行一致
- Verification 记录包含实际测试/构建/检查结果，能够支撑完成结论
- 失败会改变后续计划
- 最终完成前发生过明确验证
- 消息序列仍满足 OpenAI tool calling 协议，无孤儿 tool result

测试继续遵守零第三方依赖：临时目录用 `tempfile`，LLM 全部 mock，网络端到端验证只作为人工检查，不进入默认测试套件。

## 9. 版本边界与依赖关系

| 能力 | v0.14 | v0.15 | v0.16 |
|---|---:|---:|---:|
| 加载 `AGENTS.md` | 是 | 是 | 是 |
| Project Instructions 受保护 | 是 | 是 | 是 |
| Todo 存入 AgentState |  | 是 | 是 |
| Todo 工具更新 |  | 是 | 是 |
| Structured State 渲染 Todo |  | 是 | 是 |
| 复杂任务规划规则 |  |  | 是 |
| 失败后计划调整规则 |  |  | 是 |
| 最终回复前完成提醒 |  |  | 是 |

依赖方向必须保持单向：

```text
v0.14 Project Instructions
    ↓ 提供规划约束
v0.15 Todo / Task State
    ↓ 提供可持久化计划
v0.16 Plan-driven Execution
    ↓ 形成任务闭环
```

不要在 v0.14 提前加入 Todo，也不要在 v0.15 提前加入完成阻断；这样每个 tag 的教学差异仍然只聚焦一个新概念。

## 10. 与既有文档的同步

本计划确认后、开始 v0.14 前，应统一修改以下路线图，避免旧的“v0.14 plan 引导”与新阶段冲突：

- `AGENTS.md`
  - 阶段五改为 v0.14–v0.16
  - 路线图替换为 Project Instructions / Todo Task State / Plan-driven Execution
  - 每版完成时更新“当前架构”和勾选状态
- `docs/plans/teaching-repo-plan.md`
  - 版本切片表增加 v0.14–v0.16
  - 教程目录、使用指导差异和代码差异要点同步
- `docs/tutorials/README.md`
  - “阶段五：进阶能力（规划中）”替换为正式的阶段五导航
- `README.md`
  - 教学路径同步阶段五的名称、目标和三个版本
- `docs/operation/manual.md`
  - v0.14 增加指令加载规则
  - v0.15 增加 Todo 状态说明
  - v0.16 增加复杂任务生命周期说明
- `docs/plans/README.md`
  - 索引加入本计划

建议阶段名称统一使用：

- 英文：**Project-Aware Task Orchestration**
- 中文：**项目感知与任务编排**

## 11. 阶段完成定义

阶段五只有同时满足以下条件才算完成：

- [x] v0.14、v0.15、v0.16 三个版本分别有可读的增量 diff 和对应 git tag
- [x] 三篇教程均按仓库模板包含目标、核心概念、设计理由和使用指导
- [x] Project Instructions、Task State、Execution State、Context 四者边界在代码与文档中一致
- [x] 默认测试套件全部通过且无第三方依赖
- [x] 阶段级端到端任务完成，包含一次失败后的计划调整和最终验证
- [x] 阶段四的 trimming/compaction 与 OpenAI tool calling 协议没有回归
- [x] 路线图、README、操作手册、教程索引和 CHANGELOG 全部同步

完成阶段五后，mini_agent 将具备一个轻量编程 agent 的关键工作闭环：**理解项目规则、维护显式计划、执行真实操作、根据结果调整，并在验证后完成任务。** 后续阶段再根据实际使用中的主要失败模式，选择独立 Plan Mode、失败恢复、持久化记忆、安全隔离或多 agent，而不是在本阶段预先设计。
