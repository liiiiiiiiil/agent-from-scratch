# 第 8 课：文件操作补全

> 版本 v0.08 | [上一课](07-system-prompt.md) | [下一课](09-permission-upgrade.md)

## 本课目标

给 agent 补全文件操作能力：`list_dir`（列目录）、`edit_file`（精确替换）、`grep`（正则搜索），同时给 `read_file` 加 `offset`/`limit` 分段读取。完成后 agent 具备"浏览→定位→读取→编辑"的完整文件操作链路，能独立完成基础编程任务。

## 前置

- 已读 [第 7 课](07-system-prompt.md)，理解系统提示词工程化
- `git checkout v0.08` 切到本版代码

## 新增/改动了什么

```
 src/mini_agent/tools/
 ├── file.py            # 改：read_file 加 offset/limit + 新增 edit_file/list_dir/grep
 ├── __init__.py        # 改：注册 edit_file/list_dir/grep
 ├── base.py            # 不变
 └── calc.py            # 不变
 src/mini_agent/
 └── permission.py      # 改：list_dir/grep=ALLOW，edit_file=ASK
 tests/
 └── test_tools.py      # 改：加 6 个新测试，共 15 个
```

> 本版只改 `file.py` + `permission.py` + `__init__.py` + 测试。`agent.py` 不动——核心 loop 仍然只认 `messages` 列表，不感知工具数量变化。这是 v0.02 三件套分离关注点的持续红利。

## 核心概念

### 为什么 v0.03 的三个工具不够

v0.03 给了 `read_file`/`write_file`，agent 能读写文件，但实际编程任务还差三块：

1. **看不到目录结构**：agent 不知道项目里有哪些文件，只能靠用户告诉它路径。需要 `list_dir`。
2. **改文件只能整文件重写**：改一行代码也要 `write_file` 重写整个文件，大文件浪费 token、容易丢内容。需要 `edit_file` 做局部替换。
3. **找不到内容在哪**：agent 想改某个函数，但不知道在哪个文件。需要 `grep` 搜索内容定位文件。

v0.08 补齐这三块，加上 `read_file` 的分段读取，形成完整操作链路。

### read_file 加 offset/limit（`src/mini_agent/tools/file.py`）

v0.03 的 `read_file` 一次读全量，大文件会爆上下文。v0.08 加 `offset`/`limit`：

```python
def read_file(path: str, offset: int = 0, limit: int = 2000):
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    total = len(lines)
    start = max(0, offset)
    end = min(start + limit, total)
    numbered = [f"{i + 1:05d}| {lines[i]}" for i in range(start, end)]
    ...
```

两个设计点：

- **行号前缀** `00001| `：借鉴 OpenCode 的 ReadTool。LLM 看到行号后，调用 `edit_file` 时能更准确地定位 `old_string` 所在位置，减少误匹配。
- **剩余行提示**：未读完整时追加 `(共 N 行，已读 M 行，还有 K 行未读)`，LLM 知道还有内容、需要再调一次 `read_file(offset=...)`。

### edit_file：精确字符串替换

`edit_file` 用 `old_string`/`new_string` 做精确匹配替换，而非按行号编辑：

```python
def edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False):
    if old_string == new_string:
        raise ValueError("old_string 与 new_string 不能相同")
    content = ...
    count = content.count(old_string)
    if count == 0:
        raise ValueError(f"未找到匹配内容: {old_string[:50]}...")
    if count > 1 and not replace_all:
        raise ValueError(f"找到 {count} 处匹配，需指定 replace_all=true 或提供更长的唯一上下文")
    ...
```

**为什么用字符串匹配而非行号**：LLM 容易数错行号（尤其大文件），而字符串匹配让 LLM 直接从 `read_file` 的输出里复制要改的片段，更可靠。

**多匹配安全检查**：当 `old_string` 在文件中出现多次时，默认报错而非静默替换第一处——避免误改不相关的地方。LLM 要么传 `replace_all=true`（确认全部替换），要么扩大 `old_string` 范围使其唯一。这是借鉴 OpenCode EditTool 的安全设计，但简化为单策略（OpenCode 有 8 种模糊匹配策略，对教学项目过度设计）。

### list_dir：列出目录内容

```python
def list_dir(path: str = "."):
    entries = []
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        kind = "/" if os.path.isdir(full) else ""
        entries.append(f"{name}{kind}")
    ...
```

目录加 `/` 后缀，让 LLM 一眼区分文件和目录。200 条上限防爆（借鉴 OpenCode 的截断思路）。

### grep：正则搜索文件内容（纯标准库）

```python
def grep(pattern: str, path: str = ".", include: str = "*"):
    regex = re.compile(pattern)
    results = []
    for root, dirs, files in os.walk(path):
        for fname in sorted(files):
            if not fnmatch.fnmatch(fname, include):
                continue
            ...
            for i, line in enumerate(f, 1):
                if regex.search(line):
                    results.append(f"{fpath}:{i}: {line.rstrip()}")
```

三个设计点：

- **零依赖**：用 `re` + `os.walk` + `fnmatch`，不依赖 ripgrep。mini_agent 约定零第三方依赖。
- **`include` 过滤**：支持 `*.py` 等 glob 模式过滤文件名，避免搜到 `.git/`、二进制文件。
- **100 条上限**：搜索结果可能巨量，截断防爆。输出格式 `file:line: content` 借鉴 ripgrep。

### 权限策略更新（`src/mini_agent/permission.py`）

```python
PERMISSION_RULES = {
    "read_file": ALLOW,
    "calculate": ALLOW,
    "list_dir": ALLOW,   # 只读，放行
    "grep": ALLOW,       # 只读，放行
    "write_file": ASK,   # 有副作用
    "edit_file": ASK,    # 有副作用，同 write_file
}
```

只读工具（`list_dir`/`grep`）放行，写操作（`edit_file`）走 ASK，与 v0.04 的策略一致：能力越强，越要管住副作用。

## 为什么这样设计

### 为什么 edit_file 不做模糊匹配

OpenCode 的 EditTool 有 8 种匹配策略（精确、行首尾空白忽略、锚定行+相似度、空白标准化、缩进灵活、转义标准化、边界截断、上下文感知）。mini_agent 只做精确匹配，原因：

1. **教学优先**：8 种策略的优先级和阈值是工程经验，不是概念，教学项目不需要。
2. **精确匹配 + replace_all 已够用**：LLM 从 `read_file` 输出复制原文作为 `old_string`，精确匹配足够可靠。
3. **失败即反馈**：匹配失败时报错，LLM 会看到错误信息并重试（扩大上下文或改用 `read_file` 重新确认内容），这是 agent loop 的自我修正能力。

### 为什么 grep 不用 ripgrep

OpenCode 的 GrepTool 用 ripgrep（外部二进制）。mini_agent 约定零第三方依赖，用 `re` + `os.walk` + `fnmatch` 实现。性能差很多（ripgrep 用 Rust 并行+内存映射），但教学场景文件量小，够用。v0.09 加 `run_shell` 后，LLM 可以自己调 `rg` 命令获得高性能搜索。

### 为什么 read_file 要加行号前缀

v0.03 的 `read_file` 输出纯文本，LLM 不知道每行行号。v0.08 加 `00001| ` 前缀后，LLM 调 `edit_file` 时能更准确地选择 `old_string`（知道选哪几行），减少多匹配误报。这是 `read_file` 和 `edit_file` 的协同设计。

## 使用指导

### 本版可用的命令

```bash
# 单次任务
python -m mini_agent "列出 examples 目录的内容"

# 交互模式
python -m mini_agent

# 跑 smoke test
$env:PYTHONPATH="src"; python tests/test_tools.py
```

### 本版典型示例

**示例 1：列目录 + 读文件**
```bash
$env:PYTHONPATH="src"; python -m mini_agent "看看 examples 目录里有什么，然后读取 input.txt"
```
预期：`list_dir` 列目录 → `read_file` 读内容 → 最终回复。

**示例 2：精确编辑文件**
```bash
$env:PYTHONPATH="src"; python -m mini_agent "读取 examples/input.txt，把里面的 5 改成 100"
```
预期：`read_file` 读内容 → `edit_file` 把 `3 + 5 * 2` 改成 `3 + 100 * 2`（会触发 ASK 权限）。

**示例 3：搜索定位**
```bash
$env:PYTHONPATH="src"; python -m mini_agent "搜索 src 目录下所有包含 calculate 的文件"
```
预期：`grep` 搜索 → 返回 `file:line: content` 格式结果。

**示例 4：大文件分段读**
```bash
$env:PYTHONPATH="src"; python -m mini_agent "读取 src/mini_agent/agent.py 的第 50 到 70 行"
```
预期：`read_file(offset=50, limit=20)` → 只返回 20 行，带行号前缀。

### 本版独有特性

- **完整文件操作链路**：`list_dir`（浏览）→ `grep`（定位）→ `read_file`（读取）→ `edit_file`（编辑），agent 能独立完成基础编程任务。
- **分段读取**：`read_file` 支持 `offset`/`limit`，大文件不爆上下文。
- **精确编辑**：`edit_file` 用字符串匹配替换，多匹配安全检查防误改。
- **行号前缀**：`read_file` 输出带 `00001| ` 前缀，帮 LLM 定位 `edit_file` 的 `old_string`。

## 动手验证

1. **跑 smoke test**：
   ```bash
   $env:PYTHONPATH="src"; python tests/test_tools.py
   ```
   预期：15 个 PASS + "全部 smoke test 通过"。

2. **列目录 + 读文件**：
   ```bash
   $env:PYTHONPATH="src"; python -m mini_agent "列出 examples 目录，读取 input.txt"
   ```

3. **精确编辑**：
   ```bash
   $env:PYTHONPATH="src"; python -m mini_agent "读取 examples/input.txt，把 5 改成 100"
   ```
   跑完后检查 `examples/input.txt` 是否被正确修改。

4. **搜索定位**：
   ```bash
   $env:PYTHONPATH="src"; python -m mini_agent "搜索 src 目录下所有包含 PermissionGate 的文件"
   ```

## 本版完整代码

- [`src/mini_agent/tools/file.py`](../../src/mini_agent/tools/file.py) — read_file（加 offset/limit）+ write_file + edit_file + list_dir + grep
- [`src/mini_agent/permission.py`](../../src/mini_agent/permission.py) — 权限规则（list_dir/grep=ALLOW，edit_file=ASK）
- [`src/mini_agent/tools/__init__.py`](../../src/mini_agent/tools/__init__.py) — 注册 6 个工具
- [`tests/test_tools.py`](../../tests/test_tools.py) — 15 个 smoke test
