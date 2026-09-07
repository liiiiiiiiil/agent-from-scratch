# 第 14 课：项目级指令（Project Instructions，v0.14）

上一课：[上下文压缩](13-context-compaction.md) · [教程总览](README.md) · 下一课：[任务清单与状态](15-task-state.md)

> 代码快照：`v0.14` · 相邻差异：`v0.13.1..v0.14` · 命令环境：Bash/zsh

> 运行要求：Python 3.10+。历史 tag 的 `pyproject.toml` 仍标记 Python 3.9，但 v0.14 源码已使用 3.10 语法。

## 本课目标

本课只介绍一个变化：Agent 在启动时读取适用的 `AGENTS.md`，把项目规则放入 system prompt（系统提示词），并在每次 LLM 请求中保留这段规则。

完成本课后，你应该能够：

- 说明 Git 仓库内外 `AGENTS.md` 的发现范围和合并顺序；
- 看懂来源标记、12,000 字符上限和读取失败的处理；
- 解释为什么项目指令放在 `protected_messages`，而不是普通 `history`；
- 区分“告诉模型一条规则”和 `PermissionGate` 实际授予工具权限。

本课的核心不变量是：**项目规则随每次请求保留，但不会被当作工具授权配置。**

## 上一版的问题

v0.13 能在上下文超预算时压缩旧历史，但它只管理已经进入消息历史的内容。项目的测试命令、代码风格或禁止操作如果没有显式发给模型，模型就看不到；如果把规则追加到普通历史，后续 trimming（裁剪）可能删除它，compaction（压缩）也可能改写它。

因此 v0.14 把“项目规则”和“对话历史”分开管理：规则在进程启动时加载一次，成为受保护的 system 消息；历史仍交给 `ContextManager` 按预算处理。

## 前置条件与版本切换

先阅读第 13 课，理解 `ContextManager`、历史轮次和受保护前缀。以下命令均适用于 Bash/zsh；最后切回本课的 `v0.14` 快照。

```bash
git checkout v0.13.1
git diff --stat v0.13.1..v0.14
git diff v0.13.1..v0.14 -- src/mini_agent/instructions.py src/mini_agent/prompt.py src/mini_agent/context.py src/mini_agent/__main__.py
git checkout v0.14
```

## 新增与改动文件

完整差异以 `git diff --stat v0.13.1..v0.14` 为准。下表只保留理解本课主线所需的文件。

| 文件 | 变化 | 作用 |
|---|---|---|
| `src/mini_agent/instructions.py` | 新增 `InstructionLoader` | 找到并合并适用于启动目录的 `AGENTS.md` |
| `src/mini_agent/prompt.py` | 修改 `build_system_prompt()` | 在有规则时添加 `<project_instructions>` 区块 |
| `src/mini_agent/context.py` | 增加 `protected_messages` | 每次构造请求时保留 system prompt，历史仍可裁剪/压缩 |
| `src/mini_agent/__main__.py` | 修改启动流程 | 在创建上下文前加载规则并注入受保护消息 |
| `tests/test_instructions.py`、`tests/test_prompt.py`、`tests/test_context.py` | 新增覆盖 | 固化发现、上限、注入和保留行为 |

## 版本变更定位

图例：`[旧]` v0.13.1 已有，`[+]` v0.14 新增，`[~]` v0.14 修改，`[C]` 主要消费者，`[B]` 本版边界。

v0.13.1 的真实入口和收口方式是：

```text
[旧] CLI main()
  -> [旧] AgentState + history + ContextManager
  -> [旧] agent_loop
       -> [旧] prepare_messages()
            -> trimming / compaction（只处理 history）
            -> [C] call_llm()
```

v0.14 在启动入口插入加载和注入：

```text
[旧] CLI main()
  -> [+] InstructionLoader(os.getcwd())
       -> [+] discover(): Git root 到 cwd 逐层找 AGENTS.md
       -> [+] load(): Source 标记、最多 12,000 字符
  -> [~] build_system_prompt(project_instructions)
  -> [~] ContextManager(protected_messages + history)
       -> [旧] prepare_messages()
            -> [旧] 只裁剪/压缩 history
            -> [C] 每次 call_llm() 都带项目规则

[B] 启动后不刷新文件；规则不改变 PermissionGate 的 allow/deny/ask 结果
```

关键插入点不是“多了一段文本”，而是 `protected_messages`：它让规则进入发送视图，却不进入可变的 `history`。

## 核心概念与数据结构

### 1. 发现范围决定规则作用域

要解决的问题是：启动在子目录时，哪些项目规则适用？`InstructionLoader` 先从 `cwd` 向上寻找 Git 根目录。若找到 Git 根目录，就按“根目录 → 当前目录”逐层检查 `AGENTS.md`；越靠近 `cwd` 的文件越晚出现，因此可以覆盖或补充上层规则。若不在 Git 仓库中，只检查 `cwd` 自身，不向父目录继承。

```python
return [
    os.path.join(directory, "AGENTS.md")
    for directory in directories
    if os.path.isfile(os.path.join(directory, "AGENTS.md"))
]
```

`discover()` 的返回值是路径列表，不执行文件内容。这个顺序不变量让合并结果可预测，也把规则作用域固定为本次启动目录。

### 2. 加载结果带来源并有硬上限

`load()` 为每个文件加上 `Source: <path>`，再按发现顺序拼接正文。读取某个文件发生 `OSError` 时，不会让 CLI 启动失败，而是写入 `[读取失败，已跳过]`。累计文本最多 12,000 个字符；超限时追加截断标记并停止读取后续文件。没有文件时返回空字符串。

核心拼接逻辑会先计算本次还剩多少空间；如果完整区块放不下，就保留前缀并追加截断标记，然后结束加载：

```python
available = self.max_chars - used - len(separator)
if len(chunk) > available:
    marker = f"\\n[指令已截断，最多保留 {self.max_chars} 个字符]"
    chunk = chunk[:max(0, available - len(marker))] + marker
    chunks.append(separator + chunk)
    break
```

这保证返回值不会超过加载器上限，同时保留“哪个文件提供了规则”的来源线索。

因此调用方只需要处理一个字符串：空字符串表示不添加项目区块，非空字符串表示已经完成来源标记和长度控制。

### 3. 受保护消息与普通历史是两种生命周期

要解决的问题是：规则怎样穿过第 13 课的裁剪和压缩？CLI 把完整 system prompt 放进 `protected_messages`。`ContextManager._build_messages()` 每次复制这组消息，再追加 `history`；只有后者会被裁剪、分轮或压缩。

```python
source = ([dict(message) for message in self.protected_messages]
          if self.protected_messages is not None else [])
source.extend(dict(message) for message in self.history)
```

规则原文不会被历史裁剪或摘要改写，但它仍然计入上下文预算。受保护只表示“不可由历史策略移除”，不表示可以无限增长。

### 4. Prompt 注入不等于权限授予

`build_system_prompt()` 只有在参数非空时才追加：

```python
if project_instructions.strip():
    sections.append(
        "<project_instructions>\n"
        + project_instructions.strip()
        + "\n</project_instructions>"
    )
```

模型可以根据这段内容选择“先运行测试”或“不要碰某目录”，但真正的文件写入、shell 执行仍由工具层和 `PermissionGate` 判断。v0.14 没有把自然语言规则编译成 allow/deny/ask 配置。

## 为什么这样设计

规则和历史的生命周期不同。规则应该每轮出现且保持原文，历史则需要在窗口变小时牺牲旧内容。分开保存能同时满足这两个需求，也使规则作用域和请求内容更容易审计。

代价是规则会占用每次请求的上下文预算；12,000 字符只是加载器上限，不保证整个 system prompt 一定小于模型窗口。启动时只加载一次可以让一次运行的行为稳定，但运行中修改 `AGENTS.md`、切换工作目录都不会自动生效。

选择纯文本注入而不是自动修改权限系统，避免了把自然语言解释成安全决策的风险，也保持了实现简单；相应地，项目规则不能替代权限闸门。

## 设计边界

- Git 仓库内只检查从仓库根到启动 `cwd` 的路径；非 Git 目录只检查当前目录。
- 只支持文件名严格为 `AGENTS.md`，不自动读取其他编辑器或 Agent 格式。
- 文件不存在、读取失败或被截断都不会阻止启动；但模型可能缺少部分项目约束。
- 规则在进程启动时确定，不会因后续访问新目录而刷新。
- 加载器最多输出 12,000 个字符；system prompt 和受保护消息本身仍可能使预算紧张。
- `PermissionGate` 的授权结果、工具异常边界和工具调用回灌协议均不因本版改变。

## 关键流程

```text
启动 cwd
  -> _git_root()
  -> discover()：得到有序 AGENTS.md 路径
  -> load()：读取、标记来源、限制长度
  -> build_system_prompt(project_instructions)
  -> protected_messages
  -> prepare_messages()
       ├─ 规则：始终保留
       └─ history：按预算 trimming / compaction
  -> call_llm()
```

正常路径中，模型每次请求都能看到 `<project_instructions>`。没有规则文件时，`load()` 返回空字符串，system prompt 与 v0.13.1 相同。读取失败时启动继续，但 system prompt 中会留下跳过标记；若后续历史超预算，仍只处理历史副本。CLI 或 LLM 顶层异常不由本课新增逻辑兜底。

## 实现拆解

### 启动入口

`__main__.py` 先创建 `AgentState`，再读取当前工作目录的规则，最后把 system 消息交给上下文管理器：

```python
instructions = InstructionLoader(os.getcwd()).load()
system_prompt = (
    build_system_prompt(project_instructions=instructions)
    if instructions else build_system_prompt()
)
protected_messages = [{"role": "system", "content": system_prompt}]
context = ContextManager(state, history)
context.protected_messages = protected_messages
```

这样规则只在启动处决定一次；每轮 `prepare_messages()` 都从同一份受保护消息复制，不会因为忘记在某条路径拼接而遗漏。

### 压缩视图中的位置

当上下文已经进入压缩模式，`ContextManager` 会重建“受保护 system → Structured State → Historical Summary → 任务 → 最近历史”的视图。项目规则属于最前面的受保护 system 部分；`Historical Summary` 仍只描述旧历史，不能覆盖项目规则。

### 默认摘要器的调用边界

v0.14 沿用 v0.13 的摘要通道。`summarize_messages()` 调用 `call_llm(..., include_tools=False, stream_output=False)`：摘要请求没有工具定义，也不会把内部摘要流打印到终端。项目指令不参与摘要，因此不会被摘要器改写。

## 运行与观察（按需）

配置好本地 LLM 后，在包含 `AGENTS.md` 的项目目录启动（Bash/zsh）：

```bash
PYTHONPATH=src python -m mini_agent "读取项目规则并列出当前目录"
```

这是“命令行首条任务”；首条任务处理完成后程序仍进入交互循环。观察模型请求或上下文日志时，应看到 system prompt 中有 `<project_instructions>` 区块；在没有 `AGENTS.md` 的目录启动时，该区块不会出现。若任务变长并触发压缩，项目区块仍保留，而旧 history 可能被摘要或裁剪。

## 本版特性、下一课与代码索引

v0.14 引入了启动目录作用域、层级发现、来源标记、12,000 字符限制，以及不受历史裁剪影响的项目级 system prompt。它不提供动态刷新、不支持其他规则文件格式，也不把规则转换成工具权限。下一课 v0.15 会把 Todo 与任务状态保存到独立的 `AgentState`，让计划在上下文压缩后仍可准确呈现。

本课涉及的 v0.14 固定源码：

- [`src/mini_agent/instructions.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.14/src/mini_agent/instructions.py) — 发现和加载 `AGENTS.md`
- [`src/mini_agent/prompt.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.14/src/mini_agent/prompt.py) — 组装项目指令区块
- [`src/mini_agent/context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.14/src/mini_agent/context.py) — 受保护消息、历史裁剪与压缩视图
- [`src/mini_agent/__main__.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.14/src/mini_agent/__main__.py) — 启动时注入规则
- [`tests/test_instructions.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.14/tests/test_instructions.py) — 发现、顺序和长度边界
- [`tests/test_prompt.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.14/tests/test_prompt.py) — 项目指令区块
- [`tests/test_context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.14/tests/test_context.py) — 受保护消息行为
