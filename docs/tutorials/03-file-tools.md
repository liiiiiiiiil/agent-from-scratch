# 第 3 课：文件读写工具

> 版本 v0.03 | [上一课](02-first-tool.md) | [下一课](04-permission-gate.md)

## 本课目标

上一课的 agent 只能计算，仍然无法查看或修改项目文件，因此不能完成实际的编程任务。

这一课加入 `read_file` 和 `write_file`。前者读取文本，后者覆盖写入文本。重点不只是新增两个函数，还要看模型如何把“读取 → 处理 → 写入”拆成连续的工具调用。

## 前置

- 已读 [第 2 课](02-first-tool.md)，理解 Tool 三件套和 function calling 协议
- `git checkout v0.03` 切到本版代码

## 新增/改动了什么

```
 src/mini_agent/tools/
+├── file.py             # 新增：read_file / write_file 工具
 ├── __init__.py         # 改：注册 read_file / write_file
 ├── base.py             # 不变
 └── calc.py             # 不变
+examples/
+├── input.txt            # 示例文件（内容：3 + 5 * 2）
+└── input2.txt           # 示例文件（内容：3 + 5 * 3）
 tests/
 └── test_tools.py       # 改：加 read_file / write_file 测试
```

## 核心概念

### read_file / write_file（`src/mini_agent/tools/file.py`）

```python
def read_file(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def write_file(path: str, content: str):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"已写入文件: {path}"
```

两个 handler 都只使用 Python 的 `open`。真正需要解释的是 Tool 定义中的 schema，因为 LLM 根据它判断参数应该怎样填写：

```python
read_file_tool = Tool(
    name="read_file",
    description="读取一个文本文件的内容",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要读取的文件路径"}
        },
        "required": ["path"],
    },
    handler=read_file,
)
```

`description` 说明工具做什么，`parameters` 说明参数名称、类型和必填项。schema 写得越明确，模型越不容易传错参数。

### 注册新工具（`src/mini_agent/tools/__init__.py`）

```python
registry = ToolRegistry()
registry.register(calculate_tool)
registry.register(read_file_tool)    # ← 新增
registry.register(write_file_tool)   # ← 新增
```

新增工具只要完成三个动作：编写 handler、创建 `Tool`、放入注册表。`agent.py` 和 loop 不需要知道文件工具的细节，因为它们只依赖统一的工具协议。

### 多步工具串联

一个任务可能需要多个步骤。例如“读取 `examples/input.txt` 并计算”必须先取得文件内容，才能把其中的表达式交给计算工具。LLM 可以根据每次回灌的结果决定下一步：

```
=== [1] LLM 回复 ===
  决策调用: read_file({'path': 'examples/input.txt'})
  执行结果: 3 + 5 * 2

=== [2] LLM 回复 ===
  决策调用: calculate({'expression': '3 + 5 * 2'})
  执行结果: 13

=== [3] LLM 回复 ===
文件内容为 3 + 5 * 2，计算结果为 13。
```

示例经过 3 轮：先读文件，再计算，最后回答。这个顺序由 LLM 选择；agent loop 只负责执行请求并回灌结果，不会替模型规划步骤。

## 为什么这样设计

### 为什么 write_file 直接执行不问人

v0.03 收到 `write_file` 请求后会直接执行，不会询问用户。因为它使用覆盖写，所以错误路径或错误内容可能破坏原文件。

这一版先单独展示文件能力，暂时不处理授权问题。v0.04 会引入 `permission.py`，把 `write_file` 设为 ASK，也就是每次执行前询问用户。

### 为什么用 `"w"` 模式而非 `"a"`

`"w"` 表示覆盖写入，因此调用后的文件内容就是本次传入的 `content`。`"a"` 会在旧内容末尾追加，多次调用会不断累积。为了让本课的结果更容易预测，这一版选择 `"w"`。

### 为什么工具签名是单参数/双参数字符串

`read_file(path: str)` 单参数，`write_file(path: str, content: str)` 双参数。
这一版只需要路径和文本，因此参数都使用字符串。结构越简单，模型越容易按 schema 生成正确参数；嵌套字典或列表则会增加格式错误的可能。

## 使用指导

### 本版可用的命令

```bash
# 单次任务
python -m mini_agent "读取 examples/input.txt 并计算"

# 交互模式
python -m mini_agent

# 跑 smoke test
$env:PYTHONPATH="src"; python tests/test_tools.py
```

### 本版典型示例

**示例 1：读文件 + 计算（多步串联）**
```bash
$env:PYTHONPATH="src"; python -m mini_agent "读取 examples/input.txt 的内容并计算"
```
预期：3 轮循环，read_file → calculate → 最终回复"13"。

**示例 2：写文件**
```bash
$env:PYTHONPATH="src"; python -m mini_agent "把'Hello World'写入 examples/output.txt"
```
预期：LLM 调 `write_file({'path': 'examples/output.txt', 'content': 'Hello World'})`，回复"已写入"。

**示例 3：读后写（复制文件）**
```bash
$env:PYTHONPATH="src"; python -m mini_agent "读取 examples/input.txt，把内容写入 examples/copy.txt"
```
预期：read_file 拿到内容，write_file 写入 copy.txt。

### 本版独有特性

- **多步工具串联**：LLM 能在一轮对话里串联多个工具（读 → 算 → 答），每步结果回灌后 LLM 自行决定下一步。
- **副作用工具**：`write_file` 会改变文件系统，这类会影响外部环境的操作称为“副作用”。v0.03 没有权限保护，收到调用后一定会尝试写入。
- **相对路径约定**：工具的相对路径从项目根目录 `mini_agent/` 起算。必须在 `mini_agent/` 目录下运行。

## 动手验证

1. **跑 smoke test**：
   ```bash
   $env:PYTHONPATH="src"; python tests/test_tools.py
   ```
   预期：7 个 PASS + "全部 smoke test 通过"。

2. **读文件 + 计算**：
   ```bash
   $env:PYTHONPATH="src"; python -m mini_agent "读取 examples/input.txt 并计算"
   ```
   预期：3 轮循环，最终结果 13。

3. **写文件验证**：
   ```bash
   $env:PYTHONPATH="src"; python -m mini_agent "把'测试内容'写入 examples/test.txt"
   ```
   跑完后检查 `examples/test.txt` 是否被创建、内容是否正确。

## 本版完整代码

- [`src/mini_agent/tools/file.py`](../../src/mini_agent/tools/file.py) — read_file / write_file
- [`src/mini_agent/tools/__init__.py`](../../src/mini_agent/tools/__init__.py) — 注册三个工具
- [`tests/test_tools.py`](../../tests/test_tools.py) — 工具 smoke test（含 read_file/write_file）
