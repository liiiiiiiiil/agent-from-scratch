# 第 9 课：权限系统升级

> 版本 v0.09 | [上一课](08-file-operations.md) | [下一课](10-shell-execution.md)

## 本课目标

上一版只能按工具名决定放行、询问还是拒绝。同一个工具面对不同文件或命令时，往往只能采用同一种处理方式，权限粒度太粗。

这一课把权限升级为二维 `(tool_name, pattern) -> action`：除了工具名，还会检查文件路径或命令模式。这样可以为 v0.10 的 `run_shell` 做准备，让安全命令自动放行，危险命令继续询问。

## 前置

- 已读上一课文档
- `git checkout v0.09` 切到本版代码（或直接看 `src/mini_agent/permission.py`）

## 新增/改动了什么

```bash
git diff --stat v0.08..v0.09
```

| 文件 | 改动 |
|------|------|
| `src/mini_agent/permission.py` | 重写：Rule 三元组 + fnmatch + findLast + approve(pattern) + _extract_pattern |
| `tests/test_tools.py` | 新增 4 个二维权限测试 |

> `tools/base.py` 不改——`PermissionGate.guard()` 签名不变，内部从 args 提取 pattern。

## 核心概念

### 1. Rule 三元组：从嵌套 dict 到扁平 list

先看上一版的限制：v0.04 的规则是嵌套 dict：`{"write_file": "ask"}`，只能按工具名控制。它无法表达“只允许写某类文件”这样的要求。

v0.09 把规则保存为扁平列表。每条规则都记录工具、匹配模式和动作，下面这三个字段合在一起就是一个 Rule（三元组）：

v0.09 内部存扁平 `list[dict]`，每条规则是三元组：

```python
[
    {"permission": "read_file", "pattern": "*", "action": "allow"},
    {"permission": "write_file", "pattern": "*", "action": "ask"},
    {"permission": "read_file", "pattern": "*.env", "action": "deny"},
]
```

构造函数 `_from_config()` 仍兼容两种配置格式，因此旧配置不需要立刻改写：

```python
# 简单格式（一维兼容，pattern 默认 "*"）
PermissionPolicy({"write_file": "ask"})

# 复杂格式（二维，按 pattern 细控）
PermissionPolicy({"read_file": {"*": "allow", "*.env": "deny"}})
```

### 2. check()：fnmatch + findLast

有了多条规则之后，运行时必须回答“当前调用应该执行哪一条规则”。`check()` 会把工具名和模式分别拿去匹配，并从后往前查找。

```python
def check(self, tool_name: str, pattern: str = "*") -> str:
    merged = self._rules + self._approved
    for rule in reversed(merged):
        if fnmatch.fnmatch(tool_name, rule["permission"]) and fnmatch.fnmatch(pattern, rule["pattern"]):
            return rule["action"]
    return ASK  # 未匹配默认 ask
```

**关键设计**：
- `fnmatch.fnmatch()` 做 wildcard 匹配（通配符匹配）：`*` 匹配任意字符，`?` 匹配单字符。
- `reversed()` 从后往前找，体现 `findLast` 语义，也就是后出现的规则优先级更高。
- 如果没有任何规则匹配，结果一定是 `ask`，由用户确认后才会继续。

**为什么用 findLast？** 运行时批准过的规则会追加到 `self._approved` 末尾。`merged = rules + approved` 后再反向扫描，就会先看到 approved 规则，自然覆盖前面的 `ask`，不需要显式删除旧规则。

### 3. approve()：存 (tool_name, pattern)

当用户选择“始终允许”时，系统要记住允许的范围。v0.09 连工具名和模式一起保存，所以一次批准不会意外扩大到所有文件。

```python
# v0.04：只存工具名
def approve(self, tool_name: str):
    self._approved.add(tool_name)

# v0.09：存 (tool_name, pattern)
def approve(self, tool_name: str, pattern: str = "*"):
    self._approved.append({
        "permission": tool_name, "pattern": pattern, "action": "allow"
    })
```

效果是：用户对 `write_file` + `*.txt` 选 `always` 后，后续写 `.txt` 文件不再询问，但写 `.py` 文件仍然会询问。

### 4. _extract_pattern()：从 args 提取 pattern

工具调用的参数里没有统一叫作 `pattern` 的字段，因此闸门需要先从参数提取用于匹配的文本：文件工具取路径，shell 工具取完整命令，其他工具使用 `*`。

```python
@staticmethod
def _extract_pattern(tool_name: str, args: dict) -> str:
    if tool_name == "run_shell":
        return args.get("command", "*")  # v0.10 直接用完整命令做 pattern
    if tool_name in ("read_file", "write_file", "edit_file"):
        return args.get("path", "*")
    return "*"
```

`PermissionGate.guard()` 调用 `_extract_pattern()`，把得到的 pattern 传给 `check()`。对文件工具，pattern 是文件路径，`fnmatch` 可以用 `*.env` 等模式匹配文件名。

## 为什么这样设计

### 为什么不沿用一维？

一维权限 `tool_name -> action` 无法区分同一工具的不同操作。比如 `run_shell` 执行 `git status`（通常安全）和 `rm -rf /`（危险）时只能共用一个 action。结果要么全部放行，要么全部询问。

二维权限按命令模式分别处理：`git *` 可以 allow，`rm *` 可以 deny，其他命令保持 ask。

### 为什么用 findLast 而非 first？

OpenCode 的 `evaluate()` 使用 `findLast`，所以后出现的规则优先级更高。运行时追加的 `approved` 规则会自然覆盖前面的 `ask` 规则。

如果改用 first，前面的 ask 会挡住 approved，系统就得显式删除旧规则，逻辑更复杂。

### 为什么未匹配默认 ask 而非 allow？

这是安全优先的选择。新工具或没有配置的工具一定要先询问用户，避免危险操作被静默放行。

### 为什么 _extract_pattern 对 run_shell 只返回完整命令？

v0.09 只升级权限框架，还没有实现 `run_shell` 工具。`_extract_pattern()` 先返回完整命令字符串，v0.10 再直接用 fnmatch 通配符（如 `git *`）按命令前缀匹配。这里不做 BashArity 命令泛化，因为 fnmatch 已经够用。

### 为什么现有工具行为不变？（二维退化为一维）

升级后，`calculate`/`read_file`/`write_file`/`edit_file`/`list_dir`/`grep` 这 6 个工具的实际权限判定仍与 v0.08 完全一致。这是为了向后兼容，并不代表二维匹配没有生效。原因有二：

**① 规则里 pattern 全是 `*`**

`PERMISSION_RULES`（`permission.py` 的硬编码配置）仍是简单 dict 格式：

```python
PERMISSION_RULES = {
    "read_file": ALLOW,   # _from_config 转成 {permission: "read_file", pattern: "*", action: "allow"}
    "write_file": ASK,    # _from_config 转成 {permission: "write_file", pattern: "*", action: "ask"}
    ...
}
```

`_from_config()` 把简单格式 `"write_file": "ask"` 统一补成 `pattern="*"`（见 `permission.py:68`），没有按路径细分的规则条目。

**② `fnmatch("*", X)` 恒为 True**

`check()` 里对 pattern 做 `fnmatch` 匹配（`permission.py:84`）：

- 对 `calculate`/`list_dir`/`grep`：`_extract_pattern` 返回 `"*"`（`permission.py:147`），规则 pattern 也是 `"*"`，`fnmatch("*", "*")` → True，等价于一维判定。
- 对 `read_file`/`write_file`/`edit_file`：`_extract_pattern` 返回真实 path（如 `"a.txt"`，`permission.py:145`），但规则 pattern 仍是 `"*"`，`fnmatch("*", "a.txt")` → True，同样等价于一维。

**设计意图**：二维权限主要服务于 v0.10 的 `run_shell`，因为 shell 命令确实需要按模式区分（`git *` allow、`rm *` deny）。v0.09 没有给文件工具预置细粒度规则，仍用 `*` 兜底；如果确实需要按路径限制，可以显式配置：

```python
PermissionPolicy({"read_file": {"*": "allow", "*.env": "deny", "*.key": "deny"}})
```

**唯一行为差异是 approve 粒度收窄。**

v0.09 的 `approve()` 存 `(tool_name, pattern)` 而不是只存 `tool_name`（`permission.py:88-95`）。因此对 `a.txt` 选择 `always` 后，写 `b.txt` 仍会询问。免问范围从“整个工具”收窄为“工具加路径模式”，行为更安全。

如果要让所有 `write_file` 都免问，可以显式配置 `{"write_file": {"*": "allow"}}`，或者在 `always` 时让 pattern 使用 `"*"`。

### 借鉴了 OpenCode 什么？去掉了什么？

借鉴：
- `evaluate()` 的 `findLast` + `Wildcard.match` + 未匹配默认 ask
- `fromConfig()` 支持简单+复杂两种格式
- `approve()` 存 pattern 而非只存工具名

去掉（CLI 同步交互不需要）：
- 事件总线 `Bus.publish`——`input()` 同步阻塞即可
- `pending` 待处理队列——无异步 UI
- 规则持久化到磁盘——保持内存级
- `CorrectedError`（reject 带说明）——CLI 下难以收集说明

## 使用指导

### 本版可用的命令

```bash
# 跑测试
$env:PYTHONPATH="src"; python tests/test_tools.py

# 验证权限系统
$env:PYTHONPATH="src"
python -c "from mini_agent.permission import PermissionPolicy; p = PermissionPolicy({'read_file': {'*': 'allow', '*.env': 'deny'}}); print(p.check('read_file', 'secret.env'))"
# 期望输出: deny
```

### 本版典型示例

**示例 1：按文件名模式控制读权限**

```python
from mini_agent.permission import PermissionPolicy

policy = PermissionPolicy({"read_file": {"*": "allow", "*.env": "deny", "*.env.example": "allow"}})
policy.check("read_file", "config.env")       # → deny
policy.check("read_file", "config.env.example") # → allow
policy.check("read_file", "README.md")         # → allow
```

**示例 2：always 存 pattern，同类免问**

```python
policy = PermissionPolicy({"write_file": "ask"})
policy.approve("write_file", "*.txt")
policy.check("write_file", "test.txt")  # → allow（approved 了 *.txt）
policy.check("write_file", "test.py")   # → ask（未 approved *.py）
```

**示例 3：findLast 优先级**

```python
policy = PermissionPolicy({"write_file": "ask"})
policy.approve("write_file", "*")
policy.check("write_file", "anything")  # → allow（approved 追加在末尾，覆盖 ask）
```

### 本版独有特性

- **二维权限匹配**：`fnmatch` 按 pattern 控制同一工具的不同操作
- **findLast 优先级**：后出现的规则优先级更高，approved 自然覆盖 ask
- **向后兼容**：旧版简单 dict 格式 `{"write_file": "ask"}` 仍然可用，pattern 默认 `"*"`

## 本版完整代码

- `src/mini_agent/permission.py` — 权限系统核心（Rule 三元组 + check + approve + _extract_pattern）
- `tests/test_tools.py` — 19 个 smoke test（含 4 个新增二维权限测试）
