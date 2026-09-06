# 第 14 课：项目级指令（Project Instructions，v0.14）

上一课：[上下文压缩](13-context-compaction.md) · [教程总览](README.md) · 下一课：[任务清单与状态](15-task-state.md)

> 代码快照：`v0.14` · 相邻差异：`v0.13.1..v0.14` · 命令环境：Bash/zsh
>
> 运行要求：Python 3.10+。历史 tag 的 `pyproject.toml` 仍标记 Python 3.9，但源码已使用 3.10 语法。

## 本课目标

上下文压缩只能处理已经进入消息历史的内容。仓库里的测试命令、代码风格和禁止操作如果没有发给模型，它们就不会自动生效。本课为 Agent 增加项目级指令：启动时发现适用的 `AGENTS.md`，把规则合并进 system prompt（系统提示词），并作为每次请求都保留的受保护消息。

读完本课，你应该能够：

- 解释 `AGENTS.md` 的发现范围、合并顺序和 12,000 字符上限；
- 说明项目级指令为什么不进入 `history`，以及它如何穿过 trimming/compaction；
- 区分“提示模型遵守规则”和 `PermissionGate` 实际授予工具权限；
- 沿着缺失文件、读取失败、非 Git 目录和截断等路径说明程序行为。

本课的核心边界是：**项目指令影响模型选择，但不改变工具授权。**

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

| 文件 | 变化 | 作用 |
|---|---|---|
| `src/mini_agent/instructions.py` | 新增 `InstructionLoader` | 发现、读取并合并启动目录适用的 `AGENTS.md` |
| `src/mini_agent/prompt.py` | `build_system_prompt()` 接受 `project_instructions` | 把规则放进 `<project_instructions>` 区块 |
| `src/mini_agent/context.py` | 增加 `protected_messages` | 让项目 system prompt 不进入 history，也不被历史裁剪或压缩 |
| `src/mini_agent/__main__.py` | 启动时加载指令并注入 ContextManager | 固定本次进程的规则作用域 |
| `tests/test_instructions.py` | 增加加载器覆盖 | 固化发现、顺序、非 Git 和长度上限行为 |
| `tests/test_prompt.py`、`tests/test_context.py` | 增加注入与保留覆盖 | 验证 prompt 区块和受保护消息 |

## 上一版的问题

v0.13 已经会在请求过长时重建上下文，但它只认识当前 history 和 AgentState。若把项目规则直接追加到普通 history，规则可能在后续 trimming 中被删除，也可能被压缩成有损摘要：

```text
AGENTS.md --(未加载)--> 模型看不到项目约束
AGENTS.md --(塞进 history)--> trimming/compaction 可能丢失原文
```

因此 v0.14 要增加一个明确的边界：规则在进程启动时加载一次，成为受保护 system context；普通对话仍由 ContextManager 自己管理。

## 版本变更定位

v0.13 的主链路从 `history` 构造请求；v0.14 在启动入口增加指令加载，并把生成的 system prompt 交给 ContextManager 的 `protected_messages`。新能力的入口是 `InstructionLoader.load()`，主要消费者是每次 `prepare_messages()` 的请求视图。

```text
v0.13：history -> ContextManager.prepare_messages()
                   -> trimming/compaction -> call_llm()

v0.14：启动 cwd
          -> InstructionLoader.discover()/load()
          -> build_system_prompt(project_instructions)
          -> protected_messages + history
          -> prepare_messages() 保留规则并处理 history
          -> call_llm()
```

本版不负责动态刷新规则、不解析其他编辑器约定文件，也不把自然语言规则转换为权限决定。

## 为什么这样设计

项目规则与对话历史的生命周期不同：规则应在每次请求出现，history 则可以按预算缩短。把两者分开并将规则放入 protected messages 有三个收益：

- 每次请求都保留同一份规则原文；
- trimming 和 compaction 只处理 history，不会把规则改写进摘要；
- `PermissionGate` 仍是唯一的工具授权边界，模型不能通过一段文本自行授予权限。

代价是受保护消息也占用上下文预算。规则很长时，可供 history 使用的空间会减少；如果规则本身超过模型窗口，ContextManager 也无法压缩它。启动时加载一次则让作用域可审计，但运行期间新增或修改 `AGENTS.md` 不会自动生效。

## 关键流程

### 启动到请求

```text
os.getcwd()
  -> InstructionLoader._git_root()
  -> discover(): root 到 cwd 逐层查找 AGENTS.md
  -> load(): 按 root -> cwd 合并，最多 12,000 字符
  -> build_system_prompt(...)
  -> ContextManager.protected_messages
  -> 每次 prepare_messages(): protected + history
  -> trimming/compaction 只处理可裁剪的历史部分
```

没有任何规则文件时，`load()` 返回空字符串，system prompt 与 v0.13 保持一致。读取失败不会阻止启动；加载结果保留来源并写入读取失败标记。非 Git 目录只检查启动目录本身，不向父目录搜索。

### 与压缩的关系

`history` 仍只保存 user、assistant 和 tool 消息。压缩后，项目 system prompt 位于受保护前缀，随后才是 Structured State、Historical Summary 和近期完整轮次：

```text
protected system prompt | State + Summary + recent history
<---------------------- 不裁剪 ----------------------->
```

这样规则不会被删除或摘要；相应的代价是它始终计入 `protected_tokens`。

## 实现拆解

### 1. InstructionLoader 的发现规则

`InstructionLoader(cwd, max_chars=12000)` 提供 `discover()` 和 `load()` 两个接口。Git 仓库内，加载器从仓库根目录沿目录向下查找，结果按 root 到 cwd 排列，因此越靠近启动目录的规则出现在后面：

```python
return [
    os.path.join(directory, "AGENTS.md")
    for directory in directories
    if os.path.isfile(os.path.join(directory, "AGENTS.md"))
]
```

仓库根目录由当前目录向上查找 `.git` 文件或目录得到。非 Git 场景只检查 `cwd/AGENTS.md`。`discover()` 只返回存在的路径，不读取文件内容。

### 2. 读取、来源和长度上限

`load()` 为每个文件保留 `Source: <path>` 标记，并按发现顺序拼接内容。单次加载总长度最多 12,000 字符；超限时追加截断标记并停止读取后续文件。文件读取抛出 `OSError` 时使用 `[读取失败，已跳过]`，仍保留该文件的来源段。

这套规则让模型知道一段约束来自哪里，也避免项目文件无限膨胀地占用上下文。加载器不会执行文件中的命令，不会读取 `.cursorrules` 或 `CLAUDE.md`，也不会因为 Agent 后续访问了新目录而重新发现规则。

### 3. Prompt 与 ContextManager 的边界

`build_system_prompt()` 只有在参数非空时才追加项目区块：

```python
if project_instructions.strip():
    sections.append(
        "<project_instructions>\n"
        + project_instructions.strip()
        + "\n</project_instructions>"
    )
return "\n\n".join(sections)
```

CLI 启动时先加载规则，再把完整 prompt 作为受保护消息交给 ContextManager：

```python
instructions = InstructionLoader(os.getcwd()).load()
system_prompt = build_system_prompt(project_instructions=instructions)
context = ContextManager(state, history)
context.protected_messages = [{
    "role": "system",
    "content": system_prompt,
}]
```

`ContextManager` 也支持在构造时传入 `protected_messages`。无论采用哪种入口，规则都不写入 `history`；构造请求时会先复制受保护消息，再追加可裁剪历史。即使发生 v0.13 的 compaction，受保护消息仍在前缀中。

### 4. 模型提示不等于工具授权

项目文件可以写“先运行测试”或“不要修改某目录”，这些内容只会成为模型看到的行为约束。文件写入、shell 执行等工具仍由 `PermissionGate` 按工具和参数模式决定 allow/deny/ask。v0.14 不改变闸门结果，也不尝试把自然语言规则编译成权限配置。

## 设计边界

- 规则作用域固定为进程启动时的 cwd；运行期间修改文件、切换目录或访问新目录都不会刷新。
- Git 仓库内只沿 root 到 cwd 查找 `AGENTS.md`；非 Git 目录不向父目录继承规则。
- 只支持 `AGENTS.md`，不兼容其他工具的规则文件格式。
- 加载失败是非致命的，但模型可能因此缺少项目约束；权限系统不会因失败而放宽。
- 总长度上限只控制加载文本，不保证项目规则与 system prompt 一定适合模型窗口。
- 受保护规则会占用预算，ContextManager 不能通过 trimming/compaction 删除它们。

## 运行与观察

配置好本地 LLM 后，从一个包含 `AGENTS.md` 的目录启动：

```bash
PYTHONPATH=src python -m mini_agent "读取项目规则并列出当前目录"
```

命令行首条任务处理完成后，程序仍进入交互循环。模型收到的 system prompt 会包含 `<project_instructions>` 区块；长任务发生裁剪或压缩时，该区块仍保留。没有 `AGENTS.md` 时则不会出现该区块。

## 本版特性、下一课与代码索引

v0.14 增加了启动时的 `AGENTS.md` 发现、按层级合并、来源标记、长度限制和受保护 system prompt 注入。它不提供动态规则刷新、外部规则格式或权限升级。下一课 v0.15 会把 Todo 与任务状态从自然语言中分离出来，使任务进度也能在上下文压缩后保持准确。

完整实现固定在 v0.14：

- [`src/mini_agent/instructions.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.14/src/mini_agent/instructions.py) — 发现与加载 `AGENTS.md`
- [`src/mini_agent/prompt.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.14/src/mini_agent/prompt.py) — 项目指令 prompt 区块
- [`src/mini_agent/context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.14/src/mini_agent/context.py) — 受保护消息与历史处理
- [`src/mini_agent/__main__.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.14/src/mini_agent/__main__.py) — 启动注入入口
- [`tests/test_instructions.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.14/tests/test_instructions.py) — 加载器边界
- [`tests/test_context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.14/tests/test_context.py) — 受保护消息行为
