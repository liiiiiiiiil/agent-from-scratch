# 第 14 课：项目级指令（Project Instructions，v0.14）

上一课：[上下文压缩](13-context-compaction.md) · [教程总览](README.md) · 下一课：[任务清单与状态](15-task-state.md)

> 代码快照：`v0.14` · 相邻差异：`v0.13.1..v0.14` · 命令环境：Bash/zsh

## 本课目标

压缩能让对话变短，却不会让模型自动知道仓库的测试命令、代码风格和禁止操作。把这些规则只写在项目文件里，模型就可能看不到。本课让 Agent 启动时读取适用的 `AGENTS.md`，并把内容作为每次 LLM 请求都会携带的项目级指令。这里的项目级指令（Project Instructions）有明确边界：它提示模型如何做，不直接授予工具权限。

读完本课后，你应能：

- 解释 `AGENTS.md` 的发现范围、合并顺序和长度上限；
- 说明项目级指令（Project Instructions）为什么不属于 `history`，以及它如何穿过 trimming/compaction；
- 区分“模型行为提示”和 `PermissionGate` 的实际授权；
- 能沿着正常路径解释缺失文件、读取失败和截断时的行为。

## 前置条件与版本切换

- Python 3.10+，仓库运行时仍然只有标准库依赖。该历史 tag 的 `pyproject.toml` 仍写 3.9，但源码已经使用 3.10 语法，应以 3.10+ 运行。
- 已读 [第 13 课](13-context-compaction.md)，理解 `ContextManager`、`AgentState` 和历史压缩。

```bash
git checkout v0.13.1
git diff --stat v0.13.1..v0.14
git diff v0.13.1..v0.14 -- src/mini_agent/instructions.py src/mini_agent/prompt.py src/mini_agent/context.py src/mini_agent/__main__.py
git checkout v0.14
```

## 新增与改动文件

| 文件 | 变化 | 作用 |
|---|---|---|
| `src/mini_agent/instructions.py` | 新增 `InstructionLoader` | 发现、读取并合并项目级指令 |
| `src/mini_agent/prompt.py` | `build_system_prompt()` 增加项目级指令区块 | 保留身份、规则、环境和项目约束的分层 |
| `src/mini_agent/context.py` | 支持 `protected_messages` | 项目级 system context 不进入 history，也不参与历史裁剪 |
| `src/mini_agent/__main__.py` | 启动时加载一次指令 | 将 prompt 作为受保护消息交给 ContextManager |
| `tests/test_instructions.py` | 新增加载器测试 | 覆盖顺序、非 Git、读取和截断 |
| `tests/test_prompt.py`、`tests/test_context.py` | 增加注入与保留测试 | 验证 prompt 区块和压缩后的存活性 |

## 版本变更定位

v0.13.1 已有 ContextManager 的受保护前缀；v0.14 新增 `InstructionLoader`，并在启动入口把加载结果注入 system prompt，再交给 ContextManager。规则不进入 history，也不改变权限闸门。

```text
启动 cwd -> InstructionLoader.discover/load
          -> build_system_prompt(project_instructions)
          -> ContextManager.protected_messages
          -> prepare_messages() 保留规则 + 裁剪/压缩 history
          -> call_llm()
```

主要消费者是每次主 LLM 请求；本版不负责动态刷新、外部规则格式或权限授权。

## 为什么这样设计

普通上下文管理只能保留已经发给模型的内容，不能发现仓库规则。若把规则塞进普通 `history`，对话变长后它可能被裁剪或写进摘要，原文就不一定还在。

因此，本版单独查找规则，并把它们放进受保护的 system context（系统上下文）。这样每次请求都能看到规则，历史处理也不会删除它们。

## 关键流程

```text
启动 cwd
  -> InstructionLoader._git_root()
  -> discover(): root/.../cwd 逐层查找 AGENTS.md
  -> load(): 按 root -> cwd 合并，保留 Source，最多 12,000 字符
  -> build_system_prompt(project_instructions=...)
  -> ContextManager.protected_messages
  -> 每次 prepare_messages() 与当前 history 合并后再预算裁剪
```

`history` 仍只保存 user、assistant 和 tool 消息。ContextManager 先放入受保护消息，再放入 `history`。即使后续发生压缩，项目规则仍在消息前缀，Structured State、Historical Summary 和近期轮次排在它后面。因此 `TrimPolicy` 不会删除这些规则，它们也不会随着旧轮次被摘要掉。

## 实现拆解

### InstructionLoader 的规则

启动目录里可能有多层规则，读者需要知道哪些文件会生效、谁排在后面。`InstructionLoader(cwd, max_chars=12000)` 为此提供两个接口：`discover()` 返回找到的路径，`load()` 返回带来源标记的文本。

1. 在 Git 仓库内，从仓库根目录到启动目录逐层检查 `AGENTS.md`，结果按 root → cwd 排列；越靠近 cwd 的规则位于后面。
2. 不在 Git 仓库时，只检查启动目录本身，不向父目录寻找。
3. 空文件会产生来源段但没有正文；文件不存在则不产生段。
4. 读取发生 `OSError` 时跳过正文并保留 `[读取失败，已跳过]`，不会阻止 Agent 启动。
5. 总字符数最多 12,000。超限时保留来源和截断标记，例如 `[指令已截断，最多保留 12000 个字符]`。

这里有意把范围收窄。运行时不会执行指令文件里的命令，也不会把自然语言规则转换成权限规则；它不读取 `.cursorrules` 或 `CLAUDE.md`，也不会因为工具访问了新目录就重新加载规则。作用域固定为**进程启动时的 cwd**。因此项目级指令只能影响模型的选择，文件写入和 shell 执行仍一定由 `PermissionGate` 决定。

### CLI 与上下文边界

`__main__.py` 启动时执行一次：

```python
instructions = InstructionLoader(os.getcwd()).load()
system_prompt = build_system_prompt(project_instructions=instructions)
context = ContextManager(state, history)
context.protected_messages = [{"role": "system", "content": system_prompt}]
```

没有 `AGENTS.md` 时，`project_instructions` 为空，生成的 prompt 与 v0.13 一致。即使预算超限，受保护消息也会保留。因为规则不能丢，所以极端情况下留给 `history` 的空间会更少。

## 设计选择与边界

规则只在进程启动时加载一次，所以来源和作用范围始终清楚。相应地，运行期间新增规则或切换目录时，内容不会自动刷新。项目级指令可以影响模型行为，但一定不会改变 `PermissionGate` 的 allow/deny/ask 结果。读取失败只留下来源标记，Agent 仍会启动。

## 运行与观察

真实 CLI 示例需先配置 `config_local.py`：

```bash
PYTHONPATH=src python -m mini_agent "读取项目规则并列出当前目录"
```

## 本版特性、下一课与代码索引

- [`src/mini_agent/instructions.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.14/src/mini_agent/instructions.py)
- [`src/mini_agent/prompt.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.14/src/mini_agent/prompt.py)
- [`src/mini_agent/context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.14/src/mini_agent/context.py)
- [`tests/test_instructions.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.14/tests/test_instructions.py)

### 本版独有特性与下一课

v0.14 只让静态项目规则进入 Agent 上下文。它不提供任务计划、动态目录规则或权限升级。下一课 v0.15 会把动态任务清单与状态（Todo / Task State）从自然语言中拿出来单独保存，这样压缩后也能恢复。
