# 第 4 课：权限闸门

> 版本 v0.04 | [上一课](03-file-tools.md) | [下一课](05-streaming.md)

> 代码快照：`v0.04` · 相邻差异：`v0.03..v0.04` · 命令环境：Bash/zsh
>
> 运行要求：Python 3.10+。该 tag 的 `pyproject.toml` 仍标 3.9，但源码已使用 3.10 语法。

## 本课目标

上一课的 `write_file` 收到调用后会直接覆盖文件。模型一旦选错路径，用户没有机会阻止。

这一课加入权限闸门 `PermissionGate`。它会在工具执行前检查规则，并用 `ALLOW`、`DENY`、`ASK` 三种结果决定直接放行、直接拒绝，还是先询问用户。

## 前置

- 已读 [第 3 课](03-file-tools.md)，理解 read_file/write_file 工具
- `git checkout v0.04` 切到本版代码

## 新增/改动了什么

```
 src/mini_agent/
+├── permission.py        # 新增：PermissionPolicy + PermissionGate（allow/deny/ask 三态）
 └── tools/
     ├── base.py          # 改：ToolExecutor 加 gate 参数，execute 里先过闸门
     ├── __init__.py      # 改：import PermissionGate（executor 自动创建默认 gate）
     ├── calc.py          # 不变
     └── file.py          # 不变
 tests/
 └── test_tools.py        # 改：write_file 用放行策略绕过 ASK；新增 DENY 测试
```

## 核心概念

### 三态权限模型（`src/mini_agent/permission.py`）

不同工具的风险不同。读取和计算可以默认执行，写文件则需要用户决定。因此每个工具都有一种权限状态：

```python
ALLOW, DENY, ASK = "allow", "deny", "ask"

PERMISSION_RULES = {
    "read_file": ALLOW,    # 无副作用，直接放行
    "calculate": ALLOW,    # 无副作用，直接放行
    "write_file": ASK,     # 有副作用，每次问一下
}
```

- **ALLOW**：一定直接执行，不询问用户。
- **DENY**：一定拒绝，不会进入工具 handler。
- **ASK**：执行前显示 `[once/always/reject]`，由用户当场选择。

### PermissionPolicy（`permission.py:27`）

策略对象，负责"查规则"：

```python
class PermissionPolicy:
    def __init__(self, rules: dict | None = None):
        self.rules = rules if rules is not None else PERMISSION_RULES
        self._approved = set()  # 运行时"本轮已批准"的工具名

    def check(self, tool_name, args=None) -> str:
        action = self.rules.get(tool_name, ALLOW)
        if action == ASK:
            if tool_name in self._approved:
                return ALLOW  # 本轮已批准，免再问
            return ASK
        return action
```

`PermissionPolicy` 只负责查规则。`_approved` 集合记录用户在本次运行中选择了 `always` 的工具名，后续同名工具会直接得到 `ALLOW`。程序重启后，这份记录会消失。

### PermissionGate（`permission.py:58`）

闸门对象，封装"检查 + 交互 + 锁"：

```python
class PermissionGate:
    def guard(self, tool_name: str, args: dict) -> str | None:
        action = self.policy.check(tool_name, args)

        if action == DENY:
            return f"权限拒绝: 规则禁止调用 {tool_name}"

        if action == ASK:
            with self._ask_lock:
                choice = input(
                    f"允许执行 {tool_name}({args})? [once/always/reject] "
                ).strip().lower()
                if choice == "always":
                    self.policy.approve(tool_name)
                elif choice != "once":
                    return f"权限拒绝: 用户拒绝执行 {tool_name}"

        return None  # None = 放行，str = 拒绝原因
```

`guard` 返回 `None` 时，Executor 可以继续执行。返回字符串时，字符串就是拒绝原因，Executor 不会调用 handler，而是把原因作为工具结果回灌给 LLM。

### ToolExecutor 集成闸门（`src/mini_agent/tools/base.py:93`）

```python
class ToolExecutor:
    def __init__(self, registry: ToolRegistry, gate: PermissionGate | None = None):
        self.registry = registry
        self.gate = gate or PermissionGate()  # 不传则用默认策略

    def execute(self, name: str, arguments: dict):
        tool = self.registry.get(name)

        # ① 权限闸门
        denied = self.gate.guard(name, arguments)
        if denied:
            print(f"[Permission] {denied}")
            return denied  # 拒绝原因回灌给 LLM

        # ② 执行 handler
        try:
            result = tool.handler(**arguments)
        except Exception as e:
            return f"Tool 执行失败: {e}"
        return result
```

Executor 在调用 handler 前先调用 `gate.guard(name, args)`。权限判断和用户交互都封装在闸门内部，所以 Executor 只需要区分“继续执行”和“返回拒绝原因”。

## 为什么这样设计

### 为什么是三态不是两态

如果只有 allow 和 deny，`write_file` 要么每次都执行，要么完全无法使用。两种选择都不适合“有时允许写入”的场景。

ASK 把决定留到调用发生时。规则只说明这个工具需要确认，最终是否执行由用户结合当前参数判断。

### 为什么用 _approved 集合实现 always

用户选择 `always` 后，`policy.approve(tool_name)` 会把工具名加入 `_approved`。同一次程序运行中再次调用这个工具时，`check` 会直接返回 ALLOW。

授权只绑定工具名，不检查参数，并且不会写入磁盘。它会在程序重启后失效，但本次运行中的其他同名调用也都将被放行。

### 为什么 guard 里有 _ask_lock

`_ask_lock` 是一个 `threading.Lock()`。v0.04 仍按顺序执行工具，因此暂时看不到它的作用。第 6 课改为并发后，多个线程可能同时请求确认；这把锁会让提示逐个出现，避免输入对应错请求。

### 为什么拒绝原因回灌给 LLM

用户拒绝后，`guard` 返回 `"权限拒绝: 用户拒绝执行 write_file"`。这个结果仍会使用对应的 `tool_call_id` 回灌，所以协议保持完整。

拒绝表示工具没有执行，但不是程序异常。LLM 拿到结果后，可能改用其他方法，也可能直接告诉用户无法完成写入。

## 使用指导

### 本版可用的命令

```bash
# 命令行首条任务（可能触发权限交互）
PYTHONPATH=src python -m mini_agent "把'hello'写入 examples/test.txt"

# 跑 smoke test（不触发交互，用放行策略绕过）
PYTHONPATH=src python tests/test_tools.py
```

### 本版典型示例

**示例 1：write_file 触发权限交互**
```bash
PYTHONPATH=src python -m mini_agent "把'测试内容'写入 examples/test.txt"
```
预期输出：
```
=== [1] LLM 回复 ===
  决策调用: write_file({'path': 'examples/test.txt', 'content': '测试内容'})
允许执行 write_file({'path': 'examples/test.txt', 'content': '测试内容'})? [once/always/reject]
```
此时输入：
- `once`：本次允许，下次 write_file 还会问
- `always`：本轮内所有 write_file 都允许，不再问
- `reject` 或其他：拒绝，工具返回拒绝原因给 LLM

**示例 2：read_file 不触发权限**
```bash
PYTHONPATH=src python -m mini_agent "读取 examples/input.txt"
```
预期：read_file 是 ALLOW，直接执行，不问。

**示例 3：拒绝后 LLM 的反应**
```bash
PYTHONPATH=src python -m mini_agent "把'hello'写入 examples/test.txt"
# 在权限提示时输入 reject
```
预期：LLM 收到"权限拒绝"后，会告知用户"你没有授权我写文件"或换方式完成任务。

### 本版独有特性

- **权限交互提示**：write_file 执行前出现 `[once/always/reject]` 提示，这是 v0.04 最明显的体感变化。
- **always 免再问**：选了 always 后，本轮内再让 agent 写别的文件，不会再次询问。
- **拒绝是工具结果**：拒绝不会让 agent 崩溃，而是作为工具结果回灌，LLM 据此调整策略。

## 动手验证

1. **跑 smoke test**：
   ```bash
   PYTHONPATH=src python tests/test_tools.py
   ```
   预期：8 个 PASS（含"write_file 被 DENY 策略拒绝"）。

2. **触发权限交互**：
   ```bash
   PYTHONPATH=src python -m mini_agent "把'hello'写入 examples/test.txt"
   ```
   在权限提示时输入 `once`，预期文件被写入。

3. **测试 always**：
   ```bash
   PYTHONPATH=src python -m mini_agent
   ```
   输入"把'hello'写入 examples/a.txt"，权限提示时输入 `always`。
   再输入"把'world'写入 examples/b.txt"，预期**不再问权限**直接写。

## 本版完整代码

- [`src/mini_agent/permission.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.04/src/mini_agent/permission.py) — PermissionPolicy + PermissionGate
- [`src/mini_agent/tools/base.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.04/src/mini_agent/tools/base.py) — ToolExecutor 加 gate 参数
- [`src/mini_agent/tools/__init__.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.04/src/mini_agent/tools/__init__.py) — import PermissionGate
- [`tests/test_tools.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.04/tests/test_tools.py) — 含权限测试（放行/DENY）
