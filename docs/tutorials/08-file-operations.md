# 第 8 课：文件操作补全

> 版本 v0.08 | [上一课](07-system-prompt.md) | [下一课](09-permission-upgrade.md)

## 本课目标

之前 agent 能读写文件，却常常不知道项目里有什么、目标内容在哪，或者为了改一行而重写整个文件。
这一版加入 `list_dir`（列出目录）、`edit_file`（精确替换）和 `grep`（正则搜索），并让 `read_file` 支持 `offset`/`limit` 分段读取。这样它可以按“浏览→定位→读取→编辑”的顺序完成基础编程任务。

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

> 本版只改 `file.py`、`permission.py`、`__init__.py` 和测试。`agent.py` 不动，因为核心 loop 仍然只处理 `messages` 列表，不需要知道工具数量是否变化。

## 核心概念

### 为什么 v0.03 的三个工具不够

v0.03 已有 `read_file`/`write_file`，可以读写文件。但遇到真实编程任务时，仍会缺少三种动作：

1. **看不到目录结构**：agent 不知道项目里有哪些文件，只能靠用户告诉它路径。需要 `list_dir`。
2. **改文件只能整文件重写**：改一行代码也要 `write_file` 重写整个文件，大文件浪费 token、容易丢内容。需要 `edit_file` 做局部替换。
3. **找不到内容在哪**：agent 想改某个函数，但不知道在哪个文件。需要 `grep` 搜索内容定位文件。

这一版补上这三块，并增加 `read_file` 的分段读取，形成完整的文件操作链路。

### read_file 加 offset/limit（`src/mini_agent/tools/file.py`）

以前 `read_file` 一次读取全部内容。文件很大时，返回内容可能占满上下文。
v0.08 用 `offset`/`limit` 指定从哪一行开始、最多读取多少行：

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

这样读文件时还要解决两个问题：

- **行号前缀** `00001| `：参考 OpenCode 的 ReadTool。LLM 看见行号后，选择 `edit_file` 的 `old_string` 会更准确，也更不容易匹配错位置。
- **剩余行提示**：没有读完时，结果会追加 `(共 N 行，已读 M 行，还有 K 行未读)`。模型因此知道还有内容，可能继续调用 `read_file(offset=...)`。

### edit_file：精确字符串替换

改文件时，模型不擅长稳定地数行号。`edit_file` 因此用 `old_string`/`new_string` 做精确字符串替换：

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

**为什么用字符串匹配而非行号**：LLM 可能数错行，尤其在大文件中。字符串匹配让它直接从 `read_file` 的输出复制要改的片段，定位更可靠。

**多匹配安全检查**：`old_string` 出现多次时，代码一定报错，不会悄悄替换第一处。这样可以避免改到无关位置。
LLM 要么传入 `replace_all=true` 确认全部替换，要么把 `old_string` 扩大到唯一。这参考 OpenCode EditTool 的安全设计，但只保留一个简单策略；它的八种模糊匹配对本教学项目过于复杂。

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

目录会加 `/` 后缀，模型可以直接区分文件和目录。结果最多 200 条，避免目录很大时返回过多内容。

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

搜索实现需要兼顾以下三点：

- **零依赖**：使用 `re`、`os.walk` 和 `fnmatch`，不依赖 ripgrep。mini_agent 约定运行时不引入第三方依赖。
- **`include` 过滤**：支持 `*.py` 这类 glob 模式过滤文件名，避免搜索 `.git/` 或二进制文件。
- **100 条上限**：匹配可能很多，因此最多返回 100 条。输出格式 `file:line: content` 参考 ripgrep。

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

`list_dir`/`grep` 只读取信息，所以直接放行。`edit_file` 会修改文件，所以一定经过 ASK。
这延续 v0.04 的策略：有副作用的操作需要确认。

## 为什么这样设计

### 为什么 edit_file 不做模糊匹配

编辑时如果匹配不到，似乎可以尝试模糊匹配。OpenCode 的 EditTool 有八种策略，包括忽略空白、相似度和上下文感知。
mini_agent 只做精确匹配，原因是：

1. **教学优先**：八种策略的优先级和阈值属于工程细节，不是本课要讲的核心。
2. **精确匹配 + replace_all 已够用**：LLM 可以从 `read_file` 输出复制原文作为 `old_string`，通常已经足够可靠。
3. **失败会反馈**：匹配失败时，工具返回错误。LLM 可以扩大上下文，或重新 `read_file` 确认内容后重试。

### 为什么 grep 不用 ripgrep

OpenCode 的 GrepTool 使用 ripgrep 这个外部二进制。mini_agent 约定零第三方依赖，所以用 `re`、`os.walk` 和 `fnmatch` 实现。
它的性能远不如使用 Rust 并行和内存映射的 ripgrep，但教学场景文件较少，已经够用。v0.09 增加 `run_shell` 后，LLM 可以自行调用 `rg` 获得更快的搜索。

### 为什么 read_file 要加行号前缀

v0.03 的 `read_file` 只输出文本，LLM 不知道每段来自哪几行。加上 `00001| ` 前缀后，它能更准确地选择 `edit_file` 的 `old_string`，多匹配报错也会减少。
这是 `read_file` 和 `edit_file` 配合工作的地方。

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
预期：agent 先用 `list_dir` 列目录，再用 `read_file` 读取内容，最后回复。

**示例 2：精确编辑文件**
```bash
$env:PYTHONPATH="src"; python -m mini_agent "读取 examples/input.txt，把里面的 5 改成 100"
```
预期：agent 先 `read_file`，再由 `edit_file` 把 `3 + 5 * 2` 改成 `3 + 100 * 2`。这一步会触发 ASK 权限确认。

**示例 3：搜索定位**
```bash
$env:PYTHONPATH="src"; python -m mini_agent "搜索 src 目录下所有包含 calculate 的文件"
```
预期：`grep` 搜索后，结果以 `file:line: content` 格式返回。

**示例 4：大文件分段读**
```bash
$env:PYTHONPATH="src"; python -m mini_agent "读取 src/mini_agent/agent.py 的第 50 到 70 行"
```
预期：调用 `read_file(offset=50, limit=20)` 后，只返回 20 行，且每行带行号前缀。

### 本版独有特性

- **完整文件操作链路**：`list_dir` 浏览、`grep` 定位、`read_file` 读取、`edit_file` 编辑，agent 可以完成基础编程任务。
- **分段读取**：`read_file` 支持 `offset`/`limit`，大文件不会一次占用全部上下文。
- **精确编辑**：`edit_file` 按字符串替换，多匹配时会先报错，避免误改。
- **行号前缀**：`read_file` 输出的 `00001| ` 前缀帮助 LLM 选择 `edit_file` 的 `old_string`。

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
