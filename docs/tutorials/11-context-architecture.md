# 第 11 课：上下文架构

> 版本 v0.11 | [上一课](10-shell-execution.md) | [返回教程总览](README.md)

## 本课目标

引入 `AgentState` 与 `ContextManager`，把 **Agent 状态**从 **LLM 上下文**中分离出来，并让 ContextManager 成为 LLM 调用前的统一入口。

本版是**纯重构**：外部行为与 v0.10 完全一致，不裁剪、不压缩、不估算 token——概念先于工程。后续 v0.12（预算与裁剪）、v0.13（上下文压缩）都在本版立的边界内实现。

## 前置

- 已读上一课文档
- `git checkout v0.11` 切到本版代码（或直接看 `src/mini_agent/state.py` 和 `src/mini_agent/context.py`）

## 新增/改动了什么

```bash
git diff --stat v0.10..v0.11
```

| 文件 | 改动 |
|------|------|
| `src/mini_agent/state.py` | **新增**：`AgentState` 数据类 + `record_tool` 执行记录接口 |
| `src/mini_agent/context.py` | **新增**：`ContextManager`（`prepare_messages()` 统一入口） |
| `src/mini_agent/agent.py` | `agent_loop` 改为接收 `context_manager` + `tool_executor`，经 `prepare_messages()` 调 LLM |
| `src/mini_agent/tools/base.py` | `ToolExecutor` 加 `on_result` 结果回调 |
| `src/mini_agent/__main__.py` | 组装 AgentState + ContextManager 注入 loop |
| `tests/test_state.py` | **新增**：State 单测（10 个） |
| `tests/test_context.py` | **新增**：ContextManager 单测（5 个） |
| `tests/test_executor.py` | **新增**：Executor 回调单测（14 个） |

## 核心概念

### 1. Agent State ≠ LLM Context

v0.10 及之前，agent 的全部"记忆"都在 `messages` 列表里：任务、历史对话、工具结果混作一团。这带来一个根本问题——**上下文是易耗品，状态不是**。

```
messages（LLM 上下文）          AgentState（执行状态）
├── system prompt               ├── task        当前任务
├── user task                   ├── current_goal 当前目标
├── assistant / tool 消息对      ├── tool_history 工具执行记录
│   （越长越贵，迟早要裁）        ├── files_changed 改过哪些文件
└── ...                         ├── errors      出过哪些错
                                └── status      running/done/failed
```

- **messages** 会被裁剪、截断、摘要（v0.12/v0.13 的主题），是有损的。
- **AgentState** 独立存在，永不参与裁剪/压缩——它是压缩后不失忆的锚。

这个分离其实 v0.09 已经验证过可行性：PermissionGate 的 always 状态就是存在 messages 之外、且删消息不受影响的运行时状态。

### 2. AgentState：由执行结果驱动

```python
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

关键设计：

- **只记事实，不做转述**：`record_tool` 由真实执行结果回调更新（哪个工具、什么参数、成败、结果摘要），`files_changed`/`errors` 是从记录派生的字段。v0.13 压缩后 summary 会漂移，但 State 永远准确——验收看 State，不苛求 summary。
- **线程安全**：v0.06 起同一轮多个 tool_calls 并发执行，回调来自线程池工作线程，所以更新加 `Lock`，读取走 `snapshot()` 拿独立副本。
- **不进 messages**：State 由 ContextManager 持有，本版不渲染进上下文（v0.13 的 Structured State 才注入）。

### 3. ContextManager：LLM 调用前统一入口

```python
class ContextManager:
    def __init__(self, state: AgentState, history: list[dict]): ...

    def prepare_messages(self) -> list[dict]:
        """LLM 调用前统一入口。本版直接返回完整 messages（立边界，不裁剪）。"""
        return self.history
```

本版 `prepare_messages()` 恒等返回，看起来"什么都没做"——但它的意义是**占住位置**：

```
agent_loop ──> cm.prepare_messages() ──> call_llm
                     ↑
        v0.12 在这里做预算检查 + 裁剪
        v0.13 在这里做压缩 + State 注入
```

所有 LLM 请求都经此构建后，后续加多少上下文策略，`agent_loop` 一行都不用改。

### 4. Executor 结果回调（D5）

```python
class ToolExecutor:
    def __init__(self, registry, gate=None, on_result=None): ...
```

工具执行结果如何进入 State？答案是 **Executor 回调，loop 不感知**：

```python
# __main__.py 组装
state = AgentState()
context = ContextManager(state, history)
tool_executor = ToolExecutor(registry, on_result=state.record_tool)
```

为什么不让 `agent_loop` 顺手更新 State？否则 State 会变成第二个无人维护的 messages——每个写 loop 的人都要记得同步两份数据。回调把"记录状态"收敛到 Executor 一处：权限拒绝、handler 异常、执行成功三条路径都走 `_notify_result`，brief 截断到 200 字符。回调本身抛异常只打印日志，绝不影响工具执行结果（回调是观察者，不是参与者）。

### 5. agent_loop 签名变化

```python
# v0.10
def agent_loop(messages): ...

# v0.11
def agent_loop(context_manager, tool_executor): ...
```

loop 只向 `context_manager.history` 追加 assistant 和 tool 消息，不把 AgentState 序列化到 messages。顺带消除了 v0.10 的一个已知隐患：以前达到 `MAX_ITERATIONS` 提前返回时，messages 可能停在"有 tool_calls 但无对应 tool 结果"的半截状态；现在每轮 tool 结果全部回灌后才进入下一轮或返回，协议始终合法。

## 为什么这样设计

### 为什么本版什么都不"做"？

上下文管理最大的坑不是算法，是**边界不清**：裁剪逻辑散在 loop 里、状态和消息互相污染、摘要时机不可控。v0.11 先把"谁能碰 messages"立好规矩（只有 ContextManager）、"状态放哪"立好位置（AgentState），v0.12/v0.13 的工程实现才有落点。纯重构 + 行为不变，也让回归验证变得简单：tests 全绿 + 典型任务跑通即可。

### 为什么 State 更新放 Executor 而不是 loop？

见上文 D5。补充一点：`record_tool` 里 `files_changed` 只在 `ok=True` 且工具是 `write_file`/`edit_file` 时记录路径——失败的工具调用不算"改过文件"。这类派生规则集中在 State 内部，调用方只管喂原始事实。

### 为什么 snapshot() 返回深拷贝？

工具并发执行时，读取方（未来的渲染逻辑）拿到的列表可能正被工作线程追加。`snapshot()` 在锁内 deepcopy，保证调用方拿到一致且独立的视图。

## 使用指导

### 本版可用的命令

```bash
# 跑全量测试（61 个）
PYTHONPATH=src python -m pytest tests/ -q

# 单独跑新增测试
PYTHONPATH=src python -m pytest tests/test_state.py tests/test_context.py tests/test_executor.py -q

# 验证 AgentState 记录
PYTHONPATH=src python -c "
from mini_agent.state import AgentState
s = AgentState()
s.record_tool('write_file', {'path': 'a.txt', 'content': 'hi'}, True, '已写入 a.txt')
print(s.files_changed)   # ['a.txt']
print(s.tool_history)    # [{tool: write_file, ...}]
"

# 验证 prepare_messages 恒等返回
PYTHONPATH=src python -c "
from mini_agent.state import AgentState
from mini_agent.context import ContextManager
history = [{'role': 'system', 'content': 'x'}]
cm = ContextManager(AgentState(), history)
print(cm.prepare_messages() is history)  # True
"
```

### 本版典型示例

**示例 1：Executor 回调更新 State**

```python
from mini_agent.state import AgentState
from mini_agent.tools import registry
from mini_agent.tools.base import ToolExecutor

state = AgentState()
exec_with_cb = ToolExecutor(registry, on_result=state.record_tool)

exec_with_cb.execute("calculate", {"expression": "1+1"})
print(state.tool_history[-1])  # {'tool': 'calculate', 'args': {...}, 'ok': True, 'brief': '2'}
print(state.status)             # running
```

**示例 2：CLI 行为与 v0.10 一致**

```bash
PYTHONPATH=src python -m mini_agent "读取 examples/input.txt 并计算里面的算式"
# 交互流程、权限提问、流式输出与 v0.10 完全相同
# 区别只在内部：Executor 每次执行后悄悄记了一笔 State
```

### 本版独有特性

- **AgentState 独立对象**：不进 messages、不被 loop 直接操作，并发安全
- **ContextManager 统一入口**：所有 LLM 请求经 `prepare_messages()` 构建
- **Executor 结果回调**：权限拒绝/异常/成功三路径都回灌 State，loop 零感知
- **半截状态消除**：每轮 tool results 全部回灌后才进下一轮，协议始终合法

## 下一课预告

边界立好了，v0.12 在 `prepare_messages()` 里做真事：token 启发式估算（`len(text) // 3`）、Context Budget 比例配置、超限时按轮次原子裁剪（一轮 = assistant(tool_calls) + 其全部 tool results，绝不拆散——孤儿 tool result 会被 OpenAI 协议直接 400）。

## 本版完整代码

- `src/mini_agent/state.py` — AgentState + record_tool + snapshot
- `src/mini_agent/context.py` — ContextManager（prepare_messages 恒等返回）
- `src/mini_agent/agent.py` — agent_loop(context_manager, tool_executor)
- `src/mini_agent/tools/base.py` — ToolExecutor 加 on_result 回调
- `src/mini_agent/__main__.py` — 组装注入
- `tests/test_state.py` / `tests/test_context.py` / `tests/test_executor.py` — 29 个新增单测
