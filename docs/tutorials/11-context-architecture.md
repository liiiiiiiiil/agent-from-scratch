# 第 11 课：上下文架构

> 版本 v0.11 | [上一课](10-shell-execution.md) | [返回教程总览](README.md)

## 本课目标

对话变长后，agent 不能永远把所有内容都发给模型。可是任务进度、改过哪些文件、发生过哪些错误，又不能随着旧消息一起丢掉。

这一课引入 `AgentState` 和 `ContextManager`。前者记录执行事实，后者负责准备发给 LLM 的上下文。两者分开后，后续可以裁剪消息，却不会丢失任务状态。

本版是**纯重构**：外部行为与 v0.10 完全一致。它不会裁剪、压缩或估算 token，只先把职责边界放好。v0.12 的预算与裁剪、v0.13 的上下文压缩都会在这条边界内实现。

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

v0.10 及之前，agent 的全部“记忆”都在 `messages` 列表里：任务、历史对话和工具结果混在一起。消息越长，之后越可能需要裁掉；但执行状态不能跟着消失。

这就是“上下文是易耗品，状态不是”的意思：LLM Context（模型上下文）可以缩短，AgentState（执行状态）必须独立保留。

```
messages（LLM 上下文）          AgentState（执行状态）
├── system prompt               ├── task        当前任务
├── user task                   ├── current_goal 当前目标
├── assistant / tool 消息对      ├── tool_history 工具执行记录
│   （越长越贵，迟早要裁）        ├── files_changed 改过哪些文件
└── ...                         ├── errors      出过哪些错
                                └── status      running/done/failed
```

- **messages** 之后可能被裁剪、截断或摘要（v0.12/v0.13 的主题），因此信息会减少。
- **AgentState** 独立存在，不参与裁剪或压缩。上下文缩短后，它仍保留执行事实。

v0.09 已经有类似做法：PermissionGate 的 always 状态存在 messages 之外，删掉消息也不会影响它。

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

工具可以并发执行，因此 State 的记录必须来自真实结果，并且能安全地被多个线程更新。关键设计如下：

- **只记事实，不做转述**：`record_tool` 从真实执行结果回调中记录工具、参数、成败和结果摘要。`files_changed` 与 `errors` 根据这些记录生成。v0.13 的 summary 可能因压缩而变化，State 仍保持准确。
- **线程安全**：v0.06 起，同一轮的多个 tool_calls 可能并发执行。回调来自线程池工作线程，所以更新使用 `Lock`，读取通过 `snapshot()` 取得独立副本。
- **不进 messages**：State 由 ContextManager 持有，本版不会把它渲染进上下文；v0.13 才会注入 Structured State。

### 3. ContextManager：LLM 调用前统一入口

```python
class ContextManager:
    def __init__(self, state: AgentState, history: list[dict]): ...

    def prepare_messages(self) -> list[dict]:
        """LLM 调用前统一入口。本版直接返回完整 messages（立边界，不裁剪）。"""
        return self.history
```

本版 `prepare_messages()` 只是原样返回 history，看起来“什么都没做”。它先成为唯一入口，后续策略才有固定的位置可以加入：

```
agent_loop ──> cm.prepare_messages() ──> call_llm
                     ↑
        v0.12 在这里做预算检查 + 裁剪
        v0.13 在这里做压缩 + State 注入
```

所有 LLM 请求都经过这里构建。以后新增上下文策略时，`agent_loop` 不需要修改。

### 4. Executor 结果回调（D5）

```python
class ToolExecutor:
    def __init__(self, registry, gate=None, on_result=None): ...
```

工具执行后，如何把结果记入 State？这里使用 **Executor 回调**，而不是让 loop 自己记账：

```python
# __main__.py 组装
state = AgentState()
context = ContextManager(state, history)
tool_executor = ToolExecutor(registry, on_result=state.record_tool)
```

如果让 `agent_loop` 顺手更新 State，每次修改 loop 都要记得同步两份数据，之后很容易漏掉路径。回调把“记录状态”集中到 Executor：权限拒绝、handler 异常和执行成功这三条路径都会经过 `_notify_result`，brief 会截断到 200 字符。

回调本身若抛异常，只会打印日志，一定不会改变工具执行结果。它只观察结果，不参与执行。

### 5. agent_loop 签名变化

```python
# v0.10
def agent_loop(messages): ...

# v0.11
def agent_loop(context_manager, tool_executor): ...
```

loop 只向 `context_manager.history` 追加 assistant 和 tool 消息，不把 AgentState 序列化到 messages。

这也消除了 v0.10 的一个隐患：以前达到 `MAX_ITERATIONS` 后提前返回时，messages 可能停在“有 tool_calls、没有对应 tool 结果”的半截状态。现在每轮一定会回灌全部 tool 结果，再进入下一轮或返回，因此协议保持合法。

## 为什么这样设计

### 为什么本版什么都不"做"？

上下文管理先会遇到的通常不是算法问题，而是职责混在一起：裁剪散在 loop 中，状态和消息互相影响，摘要时机也难控制。

v0.11 先明确规则：只有 ContextManager 准备 messages，AgentState 专门保存状态。因为外部行为不变，回归验证也直接，tests 全绿并跑通典型任务即可。

### 为什么 State 更新放 Executor 而不是 loop？

见上文 D5。还有一点：`record_tool` 只有在 `ok=True` 且工具是 `write_file`/`edit_file` 时才记录路径；失败调用不会算作“改过文件”。这类规则放在 State 内部，调用方只提供原始事实。

### 为什么 snapshot() 返回深拷贝？

工具并发执行时，读取方拿到的列表可能正被工作线程追加。`snapshot()` 会在锁内 deepcopy，所以调用方拿到的是一致且独立的视图。

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
