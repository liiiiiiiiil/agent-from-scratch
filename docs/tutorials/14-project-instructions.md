# 第 14 课：Project Instructions

> 版本 v0.14 | [返回教程总览](README.md) | [上一课：上下文压缩](13-context-compaction.md) | 下一课：v0.15 Todo / Task State（规划中）

## 本课目标

v0.13 已经能压缩旧对话并观察上下文变化，但模型仍不知道当前项目的约束，例如测试命令、代码风格和禁止操作。本课加入一个边界清晰的输入源：启动 Agent 时读取适用的 `AGENTS.md`，将其作为受保护的项目指令注入每次 LLM 请求。

读完本课后，你应能：

- 解释 `AGENTS.md` 的发现范围、合并顺序和长度上限；
- 说明 Project Instructions 为什么不属于 `history`，以及它如何穿过 trimming/compaction；
- 区分“模型行为提示”和 `PermissionGate` 的实际授权；
- 用无网络示例和测试验证正常路径、缺失文件、读取失败及截断路径。

## 前置条件与版本切换

- Python 3.9+，仓库运行时仍然只有标准库依赖。
- 已读 [第 13 课](13-context-compaction.md)，理解 `ContextManager`、`AgentState` 和历史压缩。

```bash
git checkout v0.14
git diff --stat v0.13..v0.14
git diff v0.13..v0.14 -- src/mini_agent/instructions.py src/mini_agent/prompt.py src/mini_agent/context.py src/mini_agent/__main__.py tests/test_instructions.py tests/test_prompt.py tests/test_context.py
```

## 新增与改动文件

| 文件 | 变化 | 作用 |
|---|---|---|
| `src/mini_agent/instructions.py` | 新增 `InstructionLoader` | 发现、读取并合并项目指令 |
| `src/mini_agent/prompt.py` | `build_system_prompt()` 增加项目指令区块 | 保留身份、规则、环境和项目约束的分层 |
| `src/mini_agent/context.py` | 支持 `protected_messages` | 项目 system context 不进入 history，也不参与历史裁剪 |
| `src/mini_agent/__main__.py` | 启动时加载一次指令 | 将 prompt 作为受保护消息交给 ContextManager |
| `tests/test_instructions.py` | 新增加载器测试 | 覆盖顺序、非 Git、读取和截断 |
| `tests/test_prompt.py`、`tests/test_context.py` | 增加注入与保留测试 | 验证 prompt 区块和压缩后的存活性 |

## 为什么需要本版

上下文管理只能保留模型已经知道的信息，不能让模型自动获得仓库约束。把项目规则混入普通 history 又会被裁剪或摘要，因此需要独立发现规则，并把它们放入受保护的 system context。

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

`history` 仍只保存 user、assistant 和 tool 消息。ContextManager 构建请求时先复制受保护消息，再复制 history；进入压缩模式后，受保护消息仍位于消息前缀，Structured State、Historical Summary 和近期轮次接在其后。因此项目规则不会被 `TrimPolicy` 删除，也不会随旧轮次摘要消失。

## 实现拆解

### InstructionLoader 的规则

`InstructionLoader(cwd, max_chars=12000)` 提供两个主要接口：`discover()` 返回路径，`load()` 返回带来源标记的文本。

1. 在 Git 仓库内，从仓库根目录到启动目录逐层检查 `AGENTS.md`，结果按 root → cwd 排列；越靠近 cwd 的规则位于后面。
2. 不在 Git 仓库时，只检查启动目录本身，不向父目录寻找。
3. 空文件会产生来源段但没有正文；文件不存在则不产生段。
4. 读取发生 `OSError` 时跳过正文并保留 `[读取失败，已跳过]`，不会阻止 Agent 启动。
5. 总字符数最多 12,000。超限时保留来源和截断标记，例如 `[指令已截断，最多保留 12000 个字符]`。

首版刻意不做以下事情：不执行指令文件中的命令，不把自然语言规则转换成权限规则，不读取 `.cursorrules` 或 `CLAUDE.md`，也不根据工具后来访问的目录动态重新加载规则。作用域固定在**进程启动时的 cwd**；项目指令只能影响模型选择行为，文件写入和 shell 执行仍由 `PermissionGate` 决定。

### CLI 与上下文边界

`__main__.py` 启动时执行一次：

```python
instructions = InstructionLoader(os.getcwd()).load()
system_prompt = build_system_prompt(project_instructions=instructions)
context = ContextManager(state, history)
context.protected_messages = [{"role": "system", "content": system_prompt}]
```

没有 `AGENTS.md` 时，`project_instructions` 为空，生成的 prompt 和 v0.13 兼容。即使预算超限，受保护消息也会保留；代价是极端情况下可供 history 使用的空间变少，这是保护项目约束的明确取舍。

## 设计选择与边界

规则只在进程启动时加载一次，保证实现和作用域可解释；代价是运行期间新增或切换目录不会动态刷新。项目指令影响模型行为，但不改变 `PermissionGate` 的授权结果。读取失败会降级为来源标记，不阻断 Agent 启动。

## 最小无网络示例

下面的示例不调用 LLM，直接观察发现顺序、来源和截断行为：

```bash
PYTHONPATH=src python - <<'PY'
import os
import tempfile
from mini_agent.instructions import InstructionLoader

with tempfile.TemporaryDirectory() as root:
    os.mkdir(os.path.join(root, ".git"))
    child = os.path.join(root, "src")
    os.mkdir(child)
    open(os.path.join(root, "AGENTS.md"), "w", encoding="utf-8").write("运行 pytest\n")
    open(os.path.join(child, "AGENTS.md"), "w", encoding="utf-8").write("禁止修改 fixtures\n")
    loaded = InstructionLoader(child, max_chars=300).load()
    print(loaded)
PY
```

预期结果是先出现根目录的 `Source` 和规则，再出现 `src/AGENTS.md` 的规则；如果正文超过上限，输出末尾包含截断标记。真实 CLI 示例需要先配置 `config_local.py`：

```bash
PYTHONPATH=src python -m mini_agent "读取项目规则并列出当前目录"
```

## 测试与验收

运行本版直接相关测试：

```bash
PYTHONPATH=src python tests/test_instructions.py
PYTHONPATH=src python tests/test_prompt.py
PYTHONPATH=src python -m pytest -q tests/test_instructions.py tests/test_prompt.py tests/test_context.py
```

验收点：

- Git 仓库中的多层文件按 root → cwd 合并并保留 `Source:`；
- 非 Git 目录不读取父目录文件；
- 缺失、空文件和读取异常不会让加载失败；
- 超长内容不超过上限并包含截断标记；
- `<project_instructions>` 只在有内容时出现；
- 项目指令不在 `history` 中，并在 trimming/compaction 后仍存在；
- 指令文本不会改变 `PermissionGate` 的 allow/deny/ask 结果。

## 本版特性、下一课与代码索引

- [instructions.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/instructions.py)
- [prompt.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/prompt.py)
- [context.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/context.py)
- [__main__.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/src/mini_agent/__main__.py)
- [test_instructions.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/tests/test_instructions.py)
- [test_prompt.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/tests/test_prompt.py)
- [test_context.py](/Users/lihao/Public/Projects/codes/agent-from-scratch/tests/test_context.py)

### 本版独有特性与下一课

v0.14 的新增能力是“静态项目规则进入 Agent 上下文”，而不是任务计划、动态目录规则或权限升级。下一课 v0.15 将把动态 Todo / Task State 从自然语言中分离出来，并让它在压缩后仍可恢复。
