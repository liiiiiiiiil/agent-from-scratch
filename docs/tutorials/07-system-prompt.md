# 第 7 课：系统提示词工程化

> 版本 v0.07 | [上一课](06-concurrent-tool-calls.md) | [下一课](08-file-operations.md)

> 代码快照：`v0.07` · 相邻差异：`v0.06.1..v0.07` · 命令环境：Bash/zsh
>
> 运行要求：Python 3.10+。该 tag 的 `pyproject.toml` 仍标 3.9，但源码已使用 3.10 语法。

## 本课目标

上一版只用一行 system prompt（系统提示词，给模型的固定工作说明）。模型因此可能不知道当前目录、平台和应遵守的工作方式。
这一版把提示词放进 `prompt.py`，分层组装身份、规则和环境信息，让模型知道该做什么以及在何处执行。

## 前置

- 已读 [第 6 课](06-concurrent-tool-calls.md)，理解并发 tool_calls
- `git checkout v0.07` 切到本版代码

## 新增/改动了什么

```
 src/mini_agent/
 ├── prompt.py          # 新增：build_system_prompt + header + environment + _CORE_RULES
 └── __main__.py        # 改：messages[0] 改为调用 build_system_prompt()
tests/
 └── test_prompt.py     # 新增：smoke test
```

> 本版新增一个模块 + 改入口一处。`agent.py` 不动——核心 loop 仍然只认 `messages` 列表，不感知 prompt 构造。

## 核心概念

### 为什么一行 system prompt 不够

v0.06 的 system prompt 是这样的（`__main__.py:11`）：

```python
messages = [{"role": "system", "content": "你是一个助手，通过调用工具完成任务。"}]
```

这段提示词能运行，但遇到实际任务时有三个问题：

1. **模型不知道环境**：它只能猜工作目录、平台和日期，因此相对路径或平台命令可能选错。
2. **行为不稳定**：模型可能啰嗦、加 emoji、反复复述工具结果，或主动总结“我做了什么”。多轮对话中，这些内容会占用上下文。
3. **无法区分 agent 身份**：以后有多个 agent / sub-agent 时，它们职责不同；一行字符串无法说清“你是 build 还是 explore”。

### 分层组装：借鉴 OpenCode 做减法

要解决这些问题，不需要把提示词堆成一大段。这里参考 OpenCode 的分层方式，再按教学需要删减。
OpenCode 分为 `header`（身份）、`provider`（按模型适配）、`environment`（环境）和 `custom`（用户规则）四层。mini_agent 暂时保留其中最需要的三层：

| OpenCode 层 | mini_agent 对应 | 是否做 | 理由 |
|---|---|---|---|
| `header()` 身份层 | `header(agent_name)` | ✅ | 为多 agent 预留参数，当前只有 `build` |
| `provider()` 行为层 | `_CORE_RULES` 静态文本 | ✅ | 单模型不需要按 provider 分模板 |
| `environment()` 环境层 | `environment()` 动态生成 | ✅ | 纯标准库即可，投入产出比最高 |
| `custom()` 用户规则 | — | ❌ | 留 v0.08 文件工具补全后 |

### 三层结构（`src/mini_agent/prompt.py`）

`build_system_prompt(agent_name="build")` 返回一个字符串。它用 `\n\n` 把三段内容隔开，使每段只负责一类信息：

```python
def build_system_prompt(agent_name: str = "build") -> str:
    return "\n\n".join([
        header(agent_name),      # 身份：你是 build / explore / plan ...
        _CORE_RULES,              # 行为规范（所有 agent 共享）
        environment(),            # 动态环境信息
    ])
```

#### 第一层：`header(agent_name)` — 身份

```python
def header(agent_name: str = "build") -> str:
    agents = {
        "build": (
            "你是 mini_agent，一个编程 agent。"
            "你通过调用工具完成编程任务：当前能读写改文件、做数学计算，"
            "后续会扩展到跑命令、跑测试。"
            "你的目标是独立完成基础的编程任务，不只是聊天。"
        ),
        # 预留，v0.07 不实现：
        # "explore": "你是 mini_agent 的 explore 子 agent，只负责只读探索代码库...",
        # "plan": "你是 mini_agent 的 plan agent，只负责规划不执行...",
    }
    return agents.get(agent_name, agents["build"])
```

这一层先解决“模型是谁、能做什么”的问题：

- **dict + fallback**：传入未知 `agent_name` 时一定回退到 `build`。
- **身份描述具体**：明确当前只能读、写、修改文件和计算，而不是笼统地说“你是助手”。
- **为多 agent 预留**：以后增加 explore/plan 时，只需在 dict 加一项，并传入对应的 `agent_name`。

#### 第二层：`_CORE_RULES` — 行为规范

身份明确后，还需要所有 agent 都遵守同一组规则。`_CORE_RULES` 是静态文本，分为四段：

| 段 | 规则要点 |
|---|---|
| `# Tone and style` | 简洁、无 emoji、不复述工具输出、不主动总结修改 |
| `# Professional objectivity` | 优先技术准确性、不迎合假设、不确定先调查、如实纠正 |
| `# Tool usage` | 优先用工具、参数完整合法、可并发 tool_calls |
| `# Safety` | 权限闸门是预期行为、不猜 URL、工具失败直接抛异常 |

> 这里用 `<rules>...</rules>` 包住规则。对 GLM 系列模型，这种显式标签比只有 Markdown 标题更容易表达“这是必须遵守的规则”。

#### 第三层：`environment()` — 动态环境

```python
def environment() -> str:
    cwd = os.getcwd()
    is_git = _detect_git(cwd)
    return "\n".join([
        "<env>",
        f"  Working directory: {cwd}",
        f"  Is directory a git repo: {'yes' if is_git else 'no'}",
        f"  Platform: {sys.platform}",
        f"  Today's date: {date.today().isoformat()}",
        "</env>",
    ])
```

模型还需要了解当前运行位置。`environment()` 用标准库读取四项信息：

| 字段 | 获取方式 | 作用 |
|---|---|---|
| `Working directory` | `os.getcwd()` | 解析相对路径的基础 |
| `Is directory a git repo` | `_detect_git(cwd)` | 判断能否用 git 操作 |
| `Platform` | `sys.platform` | 选对平台命令（`ls` vs `dir`） |
| `Today's date` | `date.today().isoformat()` | 理解时间敏感上下文 |

实际输出示例：
```
<env>
  Working directory: E:\claude\agent_projects\mini_agent
  Is directory a git repo: yes
  Platform: win32
  Today's date: 2026-08-27
</env>
```

### `_detect_git` 的实现选择

```python
def _detect_git(cwd: str) -> bool:
    p = os.path.abspath(cwd)
    while True:
        if os.path.isdir(os.path.join(p, ".git")):
            return True
        parent = os.path.dirname(p)
        if parent == p:
            return False
        p = parent
```

这里只需要判断当前目录或其父目录是否有 `.git`。因此使用目录遍历，不调用 `subprocess.run(["git", ...])`：

- **零外部依赖**：不必依赖 git 可执行文件，符合 mini_agent 自包含的约定。
- **无需启动子进程**：目录遍历足以完成当前判断。
- **限制**：worktree 和 submodule 可能被漏判；当前教学场景不处理，后续有需求再升级。

## 为什么这样设计

### 为什么不动 `agent.py`

提示词变复杂后，核心循环不应因此承担组装工作。`call_llm` / `agent_loop` 仍然只接收 `messages` 列表，不知道 system prompt 是如何生成的。
组装提示词是入口层（`__main__.py`）的职责，所以 `agent.py` 不需要改动。

### 为什么在入口调一次而非每轮调

一次会话里，工作目录、平台和日期通常不会改变。因此 `__main__.py` 启动时调用一次 `build_system_prompt()`，写入 `messages[0]`，之后每轮直接复用。

```python
# __main__.py
messages = [{"role": "system", "content": build_system_prompt()}]
```

### 为什么不照搬 OpenCode 的 `header()` 身份伪装

OpenCode 的 `header()` 会对 Anthropic 模型做身份伪装，让 Claude 以为自己不是 Claude。这是针对特定模型的绕过方式。
mini_agent 对接 GLM 系列，并不需要它，所以这里不照搬。

### 为什么 `core_rules` 用中文

- 对接的 `EB-GLM-5.2` 是中文模型，所以中文指令通常更容易被遵循。
- 教学仓库面向中文学习者，提示词也应便于直接阅读。
- `environment()` 的字段名保留英文，如 `Working directory`。结构化标签用英文更稳定，模型也更容易一致识别。

## 使用指导

### 本版可用的命令

```bash
# 看 system prompt 完整内容
PYTHONPATH=src python -c "from mini_agent.prompt import build_system_prompt; print(build_system_prompt())"

# 跑 smoke test
PYTHONPATH=src python tests/test_prompt.py

# 实跑 agent，观察行为变化
PYTHONPATH=src python -m mini_agent "你好，简单介绍下你自己"
```

### 本版典型示例

**示例 1：查看组装后的 system prompt**
```bash
PYTHONPATH=src python -c "from mini_agent.prompt import build_system_prompt; print(build_system_prompt())"
```
预期输出：按顺序包含身份描述、`<rules>` 行为规范和 `<env>` 环境信息三段。

**示例 2：对比 v0.06 vs v0.07 的模型自我介绍**
```bash
# v0.06：一行 prompt，模型可能啰嗦或加 emoji
git checkout v0.06
PYTHONPATH=src python -m mini_agent "你好，简单介绍下自己"

# v0.07：规范 prompt，模型简洁、无 emoji、Markdown 格式
git checkout v0.07
PYTHONPATH=src python -m mini_agent "你好，简单介绍下自己"
```
预期：v0.07 的回复通常更简洁、更有结构、不带 emoji，并会说明自己是 mini_agent。模型输出仍可能有差异。

**示例 3：环境信息帮助路径解析**
```bash
PYTHONPATH=src python -m mini_agent "读取 examples/input.txt 并告诉我内容"
```
预期：模型能从 `<env>` 取得工作目录，因此可以正确解析相对路径，而不必猜测。

### 本版独有特性

- **环境感知**：模型会收到工作目录、平台、日期和 git 状态，不必猜测。
- **行为约束**：规则要求回复简洁、不带 emoji、不复述工具输出，也不主动总结。
- **多 agent 预留**：`header(agent_name)` 已提供参数；以后加 explore/plan 只需扩充 dict。

## 动手验证

1. **跑 smoke test**：
   ```bash
   PYTHONPATH=src python tests/test_prompt.py
   ```
   预期：4 个 PASS（组装成功 / header build / 未知回退 / 环境字段）。

2. **查看 system prompt 内容**：
   ```bash
   PYTHONPATH=src python -c "from mini_agent.prompt import build_system_prompt; print(build_system_prompt())"
   ```
   预期：输出含 `mini_agent`、`<rules>`、`<env>` 三段。

3. **对比 v0.06 vs v0.07 行为**：
   ```bash
   git checkout v0.06
   PYTHONPATH=src python -m mini_agent "你好"
   # v0.06：可能啰嗦、加 emoji、不报身份

   git checkout v0.07
   PYTHONPATH=src python -m mini_agent "你好"
   # v0.07：简洁、Markdown、自报 mini_agent 身份
   ```

## 本版完整代码

- [`src/mini_agent/prompt.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.07/src/mini_agent/prompt.py) — 分层组装 system prompt
- [`src/mini_agent/__main__.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.07/src/mini_agent/__main__.py) — 入口调用 `build_system_prompt()`
- [`tests/test_prompt.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.07/tests/test_prompt.py) — smoke test
