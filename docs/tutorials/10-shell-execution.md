# 第 10 课：shell 执行

> 版本 v0.10 | [上一课](09-permission-upgrade.md) | [返回教程总览](README.md)

## 本课目标

新增 `run_shell` 工具，让 agent 能跑命令（跑测试、跑脚本）。v0.09 的二维权限系统已为命令模式权限铺路，本课落地工具本身。

## 前置

- 已读上一课文档
- `git checkout v0.10` 切到本版代码（或直接看 `src/mini_agent/tools/shell.py`）

## 新增/改动了什么

```bash
git diff --stat v0.09..v0.10
```

| 文件 | 改动 |
|------|------|
| `src/mini_agent/tools/shell.py` | **新增**：`run_shell` 工具（subprocess + 超时 + 输出截断） |
| `src/mini_agent/tools/__init__.py` | 注册 `run_shell_tool`（7 个工具） |
| `src/mini_agent/permission.py` | `PERMISSION_RULES` 加 `run_shell` 二维权限规则；`_from_config` 排序修复 |
| `src/mini_agent/prompt.py` | `header()` 能力描述更新 |
| `tests/test_tools.py` | 新增 3 个 run_shell 测试 |

## 核心概念

### 1. run_shell 工具

```python
def run_shell(command: str):
    proc = subprocess.run(
        command,
        shell=True,
        cwd=os.getcwd(),
        capture_output=True,
        text=True,
        timeout=_TIMEOUT,   # 30s
    )
    output = proc.stdout + proc.stderr
    # ... 截断 + 退出码前缀
```

关键设计：
- **`shell=True`**：命令字符串直接执行，教学简洁。安全性由权限闸门兜底。
- **`capture_output=True`**：stdout + stderr 合并捕获
- **`timeout=30`**：超时 30s，防止命令卡死 agent
- **输出截断 2000 字符**：防长输出爆上下文，与 `read_file` 的 limit 设计一致
- **退出码前缀**：非零退出码时加 `[exit=N]` 前缀，让 LLM 知道命令失败了

### 2. 二维命令模式权限

v0.09 的 `_extract_pattern()` 对 `run_shell` 返回完整命令字符串，v0.10 的权限规则用 fnmatch 通配符按命令前缀匹配：

```python
PERMISSION_RULES = {
    # ...
    "run_shell": {
        "git *": ALLOW,      # git 操作放行
        "python *": ALLOW,   # python 脚本/测试放行
        "pip *": ALLOW,      # pip 安装放行
        "ls *": ALLOW,       # 只读命令放行
        "cat *": ALLOW,      # 只读命令放行
        "echo *": ALLOW,     # 只读命令放行
        "*": ASK,             # 其他命令每次问
    },
}
```

效果：`git status` → allow，`python tests/test_tools.py` → allow，`rm -rf /` → ask。

### 3. _from_config 排序修复

v0.10 发现并修复了 v0.09 的一个 findLast 语义缺陷：

**问题**：复杂格式 dict 里 `*` 放在最后，`_from_config` 按插入顺序展开，`*` 排在具体模式后面。findLast 从后往前找，先碰到 `*`（匹配一切）就返回，具体模式被遮蔽。

**修复**：`_from_config` 对复杂格式的 pattern 排序，`*` 排最前（优先级最低），具体模式排后面（优先级更高）：

```python
# "*" 排最前（优先级最低），具体模式排后面（优先级更高）
items = sorted(value.items(), key=lambda kv: kv[0] != "*")
```

findLast 从后往前找，先碰具体模式（如 `echo *`），匹配就返回；匹配不到才往前碰 `*` 兜底。

## 为什么这样设计

### 为什么不做 BashArity 命令泛化？

原计划提了 "BashArity 命令泛化"（把 `git checkout main` 泛化为 `git checkout *`），但 v0.09 的 `_extract_pattern` 返回完整 command 字符串，fnmatch 的 `git *` 通配符已能按命令前缀匹配。教学简洁性优先，不做 BashArity。如果后续发现需要按"命令+参数"分离匹配，再引入。

### 为什么用 shell=True？

`shell=True` 让命令字符串直接执行（`subprocess.run("echo hello", shell=True)` 等价于在终端敲 `echo hello`）。教学简洁，不需要拆分参数数组。安全性由二维权限闸门兜底——安全命令 allow，其他 ask。

### 为什么超时 30s？

跑测试够用。长任务（如 `npm install`）可能超时，后续 v0.011 上下文管理再调。超时后返回错误信息，不杀 agent loop。

### 为什么输出截断 2000 字符？

防长输出爆上下文。`read_file` 的 limit 是 2000 行，`run_shell` 的截断是 2000 字符——不同单位但同思路：限制回灌给 LLM 的数据量。截断后尾部标注实际长度，让 LLM 知道输出被截了。

### 为什么 stdout + stderr 合并？

让 LLM 一次看到所有输出。分开返回需要两次解析，合并后 LLM 能从 stderr 里看到错误信息（如 `Traceback`），直接判断命令是否失败。

## 使用指导

### 本版可用的命令

```bash
# 跑测试（现在可以用 run_shell 跑了！）
$env:PYTHONPATH="src"; python tests/test_tools.py

# 验证 run_shell 工具
$env:PYTHONPATH="src"
python -c "from mini_agent.tools import executor; print(executor.execute('run_shell', {'command': 'echo hello'}))"
# 期望输出: hello

# 验证二维权限
$env:PYTHONPATH="src"
python -c "from mini_agent.permission import PermissionPolicy; p = PermissionPolicy(); print(p.check('run_shell', 'git status'))"
# 期望输出: allow
```

### 本版典型示例

**示例 1：跑 echo 命令**

```python
from mini_agent.tools import executor
result = executor.execute("run_shell", {"command": "echo hello_world"})
# → "hello_world\n"
```

**示例 2：非零退出码**

```python
result = executor.execute("run_shell", {"command": 'python -c "exit(1)"'})
# → "[exit=1] (无输出)"
```

**示例 3：二维权限 git allow / rm deny**

```python
from mini_agent.permission import PermissionPolicy, PermissionGate
from mini_agent.tools import registry, ToolExecutor

policy = PermissionPolicy({"run_shell": {"git *": "allow", "rm *": "deny", "*": "ask"}})
gate = PermissionGate(policy)
exec_test = ToolExecutor(registry, gate=gate)

exec_test.execute("run_shell", {"command": "git --version"})
# → 放行，返回 "git version 2.x.x..."

exec_test.execute("run_shell", {"command": "rm -rf /tmp/nonexist"})
# → 拒绝："权限拒绝: 规则禁止调用 run_shell(rm -rf /tmp/nonexist)"
```

### 本版独有特性

- **跑命令能力**：agent 现在能跑测试、跑脚本、跑 git 命令
- **命令模式权限**：`git *`/`python *` 等安全命令放行，其他命令每次问
- **输出截断**：长输出被截断到 2000 字符，尾部标注实际长度
- **退出码前缀**：非零退出码时带 `[exit=N]` 前缀，让 LLM 知道命令失败了
- **findLast 排序修复**：`*` 排最前（优先级最低），具体模式优先匹配

## 本版完整代码

- `src/mini_agent/tools/shell.py` — run_shell 工具（subprocess + 超时 + 截断）
- `src/mini_agent/permission.py` — 二维权限规则（含 run_shell 命令模式权限）
- `src/mini_agent/prompt.py` — header 能力描述更新
- `tests/test_tools.py` — 22 个 smoke test（含 3 个新增 run_shell 测试）
