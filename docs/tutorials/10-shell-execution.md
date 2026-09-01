# 第 10 课：shell 执行

> 版本 v0.10 | [上一课](09-permission-upgrade.md) | [返回教程总览](README.md)

> 代码快照：`v0.10` · 相邻差异：`v0.09..v0.10` · 命令环境：Bash/zsh
>
> 运行要求：Python 3.10+。该 tag 的 `pyproject.toml` 仍标 3.9，但源码已使用 3.10 语法。

## 本课目标

前几课的 agent 能读写文件和调用工具，却不能自己运行测试或脚本。它修改代码后，仍要依赖用户去终端验证结果。

这一课新增 `run_shell` 工具，让 agent 能运行命令。v0.09 的二维权限已经能按命令模式判断权限，本课把这项能力真正接到工具上。

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

运行 shell 命令时，需要同时避免三类问题：命令卡住、输出过长，以及失败后模型看不出原因。`run_shell` 因此在执行时设置超时、收集输出并带上退出码。

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
- **`shell=True`**：直接执行命令字符串，读者不必先学习如何拆分参数数组。权限闸门负责兜住安全问题。
- **`capture_output=True`**：把 stdout 和 stderr 一起收集。
- **`timeout=30`**：30 秒后一定超时返回，避免命令卡住 agent。
- **输出截断 2000 字符**：长输出不会占满上下文，和 `read_file` 的 limit 有相同目的。
- **退出码前缀**：退出码非零时加 `[exit=N]`，让 LLM 能判断命令失败。

### 2. 二维命令模式权限

shell 命令的风险差别很大。例如读取 git 状态通常可以直接执行，删除文件则需要确认。因此权限规则不只看 `run_shell` 这个工具名，还要看命令文本。

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

结果是：`git status` → allow，`python tests/test_tools.py` → allow，`rm -rf /` → ask。

### 3. _from_config 排序修复

通配规则 `*` 能匹配所有命令。如果它先被找到，像 `echo *` 这样的具体规则就永远没有机会生效。

v0.10 发现并修复了 v0.09 的一个 findLast 语义缺陷：

**问题**：复杂格式 dict 里 `*` 放在最后，`_from_config` 按插入顺序展开后，`*` 也排在具体模式后面。findLast 从后往前找，会先遇到匹配一切的 `*`，具体模式便被遮蔽。

**修复**：`_from_config` 对复杂格式的 pattern 排序，把 `*` 放到最前，即最低优先级；具体模式放到后面，即更高优先级：

```python
# "*" 排最前（优先级最低），具体模式排后面（优先级更高）
items = sorted(value.items(), key=lambda kv: kv[0] != "*")
```

findLast 从后往前找，先碰具体模式（如 `echo *`），匹配就返回；匹配不到才往前碰 `*` 兜底。

## 为什么这样设计

### 为什么不做 BashArity 命令泛化？

原计划提过 “BashArity 命令泛化”，即把 `git checkout main` 泛化为 `git checkout *`。但 `_extract_pattern` 已返回完整 command 字符串，fnmatch 的 `git *` 也已经能按命令前缀匹配。

所以本版不引入 BashArity。以后如果必须把“命令”和“参数”分开匹配，才需要补上。

### 为什么用 shell=True？

`shell=True` 让命令字符串直接执行（`subprocess.run("echo hello", shell=True)` 等价于在终端敲 `echo hello`）。因此示例不需要先拆分参数数组。

它不自动保证命令安全。二维权限闸门会放行规则允许的命令，其余命令仍然 ask。

### 为什么超时 30s？

30 秒足够运行这里的测试。长任务，例如 `npm install`，可能超时，本版不会自动延长它。超时后工具返回错误信息，但不会结束 agent loop。

### 为什么输出截断 2000 字符？

这是为了防止长输出占满上下文。`read_file` 限制的是 2000 行，`run_shell` 截断的是 2000 字符；单位不同，但都在限制回灌给 LLM 的数据量。

截断后尾部会标出实际长度，因此 LLM 能知道输出并不完整。

### 为什么 stdout + stderr 合并？

LLM 一次就能看到全部输出。若分开返回，它还要分别解析两部分；合并后可以直接从 stderr 中看到 `Traceback` 等错误信息并判断结果。

## 使用指导

### 本版可用的命令

```bash
# 跑测试（现在可以用 run_shell 跑了！）
PYTHONPATH=src python tests/test_tools.py

# 验证 run_shell 工具
PYTHONPATH=src python -c "from mini_agent.tools import executor; print(executor.execute('run_shell', {'command': 'echo hello'}))"
# 期望输出: hello

# 验证二维权限
PYTHONPATH=src python -c "from mini_agent.permission import PermissionPolicy; p = PermissionPolicy(); print(p.check('run_shell', 'git status'))"
# 期望输出: allow
```

在 POSIX（Linux/macOS）上，`test_run_shell_exit_code` 使用命令 `false`。默认权限规则会询问一次；在提示中输入 `once`，测试随后会继续并完成。Windows 使用已放行的 `python -c "exit(1)"`，不会出现这次确认。

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

- [`src/mini_agent/tools/shell.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.10/src/mini_agent/tools/shell.py) — run_shell 工具
- [`src/mini_agent/permission.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.10/src/mini_agent/permission.py) — 二维权限规则
- [`src/mini_agent/prompt.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.10/src/mini_agent/prompt.py) — header 能力描述更新
- [`tests/test_tools.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.10/tests/test_tools.py) — 22 个 smoke test
