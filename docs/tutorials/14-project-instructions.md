# 第 14 课：项目级指令（Project Instructions，v0.14）

上一课：[上下文压缩](13-context-compaction.md) · [教程总览](README.md) · 下一课：[任务清单与状态](15-task-state.md)

> 代码快照：`v0.14` · 相邻差异：`v0.13.1..v0.14` · 命令环境：Bash/zsh

> 运行要求：Python 3.10+。历史 tag 的 `pyproject.toml` 仍标记 Python 3.9，但源码已使用 3.10 语法。

## 本课目标

上下文压缩只能处理已经进入消息历史的内容。仓库中的测试命令、代码风格和禁止操作若没有发给模型，就不会自动生效。本课为 Agent 增加项目级指令：启动时发现适用的 `AGENTS.md`，合并规则并注入 system prompt（系统提示词），再作为每次请求都保留的受保护消息。

读完本课，你应该能够：

- 解释 `AGENTS.md` 的发现范围、合并顺序和 12,000 字符上限；
- 说明项目级指令为什么不进入 `history`，以及它如何穿过 trimming（裁剪）和 compaction（压缩）；
- 区分“提示模型遵守规则”和 `PermissionGate` 实际授予工具权限；
- 沿着缺失文件、读取失败、非 Git 目录和截断等路径说明程序行为。

本课的核心边界是：**项目指令影响模型选择，但不改变工具授权。**

## 上一版的问题

v0.13 已经会在请求过长时重建上下文，但它只认识当前 history 和 `AgentState`。如果把项目规则直接追加到普通 history，规则可能在后续 trimming 中被删除，也可能被压缩成有损摘要：

```text
AGENTS.md --(未加载)--> 模型看不到项目约束
AGENTS.md --(塞进 history)--> trimming/compaction 可能丢失原文
```

因此 v0.14 增加一个明确边界：规则在进程启动时加载一次，成为受保护的 system context；普通对话仍由 `ContextManager` 管理。

## 前置条件与版本切换

- 已读第 13 课，理解 `ContextManager`、受保护前缀、`AgentState` 和历史压缩。
- 以下命令均适用于 Bash/zsh；阅读完成后切回 v0.14。

```bash
git checkout v0.13.1
git diff --stat v0.13.1..v0.14
git diff v0.13.1..v0.14 -- src/mini_agent/instructions.py src/mini_agent/prompt.py src/mini_agent/context.py src/mini_agent/__main__.py
git checkout v0.14
```

## 新增与改动文件

`git diff --stat v0.13.1..v0.14` 显示本版还同步更新了 README、操作手册、计划和版本元数据；下表只列本课主线直接涉及的文件。

| 文件 | 变化 | 作用 |
|---|---|---|
| `src/mini_agent/instructions.py` | 新增 `InstructionLoader` | 发现、读取并合并启动目录适用的 `AGENTS.md` |
| `src/mini_agent/prompt.py` | `build_system_prompt()` 增加 `project_instructions` | 把规则放进 `<project_instructions>` 区块 |
| `src/mini_agent/context.py` | 增加 `protected_messages` | 让项目 system prompt 不进入 history，也不被历史裁剪或压缩 |
| `src/mini_agent/__main__.py` | 启动时加载并注入指令 | 固定本次进程的规则作用域 |
| `tests/test_instructions.py`、`tests/test_prompt.py`、`tests/test_context.py` | 增加覆盖 | 固化发现、顺序、上限、注入和保留行为 |

## 版本变更定位

v0.13 的主链路从 `history` 构造请求；v0.14 在启动入口增加指令加载，并把生成的 system prompt 交给 `ContextManager` 的 `protected_messages`。新能力入口是 `InstructionLoader.load()`，主要消费者是每次 `prepare_messages()` 生成的请求视图。

```text
v0.13：history -> ContextManager.prepare_messages()
                   -> trimming/compaction -> call_llm()

v0.14：启动 cwd
          -> InstructionLoader.discover()/load()
          -> build_system_prompt(project_instructions)
          -> protected_messages + history
          -> prepare_messages(): 保留规则并处理 history
          -> call_llm()
                         |
                         +-> 主要消费者：每次 LLM 请求
                         +-> 边界：不刷新文件，不决定工具权限
```

## 核心概念与数据结构

### 1. 适用文件列表

`InstructionLoader(cwd, max_chars=12000)` 以启动目录为作用域。Git 仓库内，它从仓库根目录到 `cwd` 逐层查找 `AGENTS.md`；返回值是按 root → cwd 排列的路径列表。非 Git 目录只检查 `cwd` 本身，不向父目录继承。

```python
return [
    os.path.join(directory, "AGENTS.md")
    for directory in directories
    if os.path.isfile(os.path.join(directory, "AGENTS.md"))
]
```

列表顺序是不变量：越靠近启动目录的规则越晚拼接；加载器只检查文件是否存在，不执行文件内容。

### 2. 带来源的规则文本

`load()` 将每个文件包装为 `Source: <path>` 加正文，并按发现顺序拼接。读取异常变成非致命的 `[读取失败，已跳过]` 标记；累计文本最多保留 12,000 个字符，超限时加入截断标记并停止读取后续文件。返回值始终是字符串，找不到文件时为空字符串。

### 3. 受保护消息

CLI 把完整 system prompt 放入 `protected_messages`，而不是 `history`。`ContextManager._build_messages()` 每次先复制受保护消息，再插入结构化状态和可裁剪历史；compaction 也只摘要历史轮次。因此规则原文在请求中保持不变，但仍计入上下文预算。

## 为什么这样设计

项目规则与对话历史的生命周期不同：规则应在每次请求出现，history 则可以按预算缩短。分开保存并使用 `protected_messages` 的收益是：规则原文不会被删除或改写，且每个请求的作用域可审计。

代价是受保护消息也占用上下文预算；规则过长时，留给 history 的空间会减少，超过模型窗口也无法靠压缩解决。启动时加载一次使行为稳定，却意味着运行期间新增、修改 `AGENTS.md` 或切换目录都不会自动生效。选择纯文本注入而不是把规则编译成权限配置，则保留了实现简单性，但不能替代权限系统。

## 设计边界

- Git 仓库内只沿 root 到启动 `cwd` 查找 `AGENTS.md`；非 Git 目录不向父目录继承规则。
- 只支持 `AGENTS.md`，不兼容 `.cursorrules`、`CLAUDE.md` 等其他格式。
- 加载失败不会阻止启动，但模型可能因此缺少项目约束；`PermissionGate` 不会因失败而放宽权限。
- 规则文本上限只控制加载器输出，不保证整个 system prompt 适合模型窗口。
- 规则作用域固定为进程启动时的 `cwd`，不会因后续访问新目录而刷新。

## 关键流程

```text
os.getcwd()
  -> InstructionLoader._git_root()
  -> discover(): root 到 cwd 逐层查找
  -> load(): 合并 Source 标记，最多 12,000 字符
  -> build_system_prompt(...)
  -> ContextManager.protected_messages
  -> prepare_messages(): protected + state + history
  -> 超预算时只裁剪/压缩 history
  -> call_llm()
```

没有规则文件时，`load()` 返回空字符串，system prompt 与 v0.13 保持一致。命令行启动后若发生裁剪或压缩，`<project_instructions>` 区块仍在；若 CLI 或 LLM 顶层异常，则不由本课新增逻辑兜底。

## 实现拆解

### 1. 启动入口与 Prompt

`__main__.py` 在创建上下文前加载规则，并将结果传给 `build_system_prompt()`；参数为空时不添加项目区块：

```python
instructions = InstructionLoader(os.getcwd()).load()
system_prompt = build_system_prompt(project_instructions=instructions) if instructions else build_system_prompt()
protected_messages = [{"role": "system", "content": system_prompt}]
context = ContextManager(state, history)
context.protected_messages = protected_messages
```

这解决了“每轮重新拼接可能遗漏规则”的问题，同时不污染本地 `history`。

### 2. ContextManager 的请求视图

```python
source = ([dict(message) for message in self.protected_messages]
          if self.protected_messages is not None else [])
source.extend(dict(message) for message in self.history)
```

后续的状态注入、trimming 和 compaction 都基于这个副本；原始 history 不被改写，且受保护前缀不会被删除。

### 3. 模型提示不等于工具授权

`AGENTS.md` 可以写“先运行测试”或“不要修改某目录”，这些内容只是模型看到的行为约束。文件写入、shell 执行等工具仍由 `PermissionGate` 按工具和参数模式决定 allow/deny/ask。v0.14 不把自然语言规则编译成权限，也不改变闸门结果。

## 运行与观察（按需）

配置好本地 LLM 后，从一个包含 `AGENTS.md` 的目录启动（命令环境：Bash/zsh）：

```bash
PYTHONPATH=src python -m mini_agent "读取项目规则并列出当前目录"
```

这是“命令行首条任务”；处理完成后程序仍进入交互循环。模型收到的 system prompt 会包含 `<project_instructions>` 区块；没有 `AGENTS.md` 时则不会出现该区块。

## 本版特性、下一课与代码索引

v0.14 增加启动时的 `AGENTS.md` 发现、按层级合并、来源标记、长度限制和受保护 system prompt 注入。它不提供动态刷新、外部规则格式或权限升级。下一课 v0.15 会把 Todo 与任务状态从自然语言中分离出来，使任务进度在上下文压缩后仍保持准确。

完整实现固定在 `v0.14`：

- [`src/mini_agent/instructions.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.14/src/mini_agent/instructions.py) — 发现与加载 `AGENTS.md`
- [`src/mini_agent/prompt.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.14/src/mini_agent/prompt.py) — 项目指令 prompt 区块
- [`src/mini_agent/context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.14/src/mini_agent/context.py) — 受保护消息与历史处理
- [`src/mini_agent/__main__.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.14/src/mini_agent/__main__.py) — 启动注入入口
- [`tests/test_instructions.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.14/tests/test_instructions.py) — 加载器边界
- [`tests/test_prompt.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.14/tests/test_prompt.py) — Prompt 区块
- [`tests/test_context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.14/tests/test_context.py) — 受保护消息行为
