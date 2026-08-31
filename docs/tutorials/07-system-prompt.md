# 第 7 课：系统提示词工程化

> 版本 v0.07 | [上一课](06-concurrent-tool-calls.md) | [下一课](08-file-operations.md)

## 本课目标

把 system prompt 从 `__main__.py` 里的一行硬编码字符串，升级为 `prompt.py` 模块里分层组装的完整规范，让模型知道自己是谁、该怎么干活、在什么环境下干活。

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

能跑，但有三个问题：

1. **模型不知道环境**：工作目录在哪？什么平台？今天几号？模型只能猜，相对路径容易错、平台命令容易选错。
2. **行为不可控**：模型可能啰嗦、加 emoji、每次工具调用后复述结果、主动总结"我做了什么"——多轮迭代时这些坏习惯会污染上下文。
3. **无法区分 agent 身份**：后续要多 agent / sub-agent，每个 agent 职责不同，一行字符串没法承载"你是 build 还是 explore"。

### 分层组装：借鉴 OpenCode 做减法

OpenCode 的 system prompt 分四层：`header`（身份）+ `provider`（按模型适配）+ `environment`（环境）+ `custom`（用户规则）。mini_agent 按「渐进生长」原则裁剪为三层：

| OpenCode 层 | mini_agent 对应 | 是否做 | 理由 |
|---|---|---|---|
| `header()` 身份层 | `header(agent_name)` | ✅ | 为多 agent 预留参数，当前只有 `build` |
| `provider()` 行为层 | `_CORE_RULES` 静态文本 | ✅ | 单模型不需要按 provider 分模板 |
| `environment()` 环境层 | `environment()` 动态生成 | ✅ | 纯标准库即可，投入产出比最高 |
| `custom()` 用户规则 | — | ❌ | 留 v0.08 文件工具补全后 |

### 三层结构（`src/mini_agent/prompt.py`）

`build_system_prompt(agent_name="build")` 返回一个字符串，三段用 `\n\n` 拼接：

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

关键点：
- **dict + fallback**：未知 `agent_name` 回退到 `build`，安全。
- **身份描述具体**：说明当前能力边界（读写改文件 + 计算）和目标（独立完成任务），不只说"你是助手"。
- **为多 agent 预留**：后续加 explore/plan 只需在 dict 加一行，调用方传不同 `agent_name` 即可。

#### 第二层：`_CORE_RULES` — 行为规范

静态文本，所有 agent 共享，分四段：

| 段 | 规则要点 |
|---|---|
| `# Tone and style` | 简洁、无 emoji、不复述工具输出、不主动总结修改 |
| `# Professional objectivity` | 优先技术准确性、不迎合假设、不确定先调查、如实纠正 |
| `# Tool usage` | 优先用工具、参数完整合法、可并发 tool_calls |
| `# Safety` | 权限闸门是预期行为、不猜 URL、工具失败直接抛异常 |

> 为什么用 `<rules>...</rules>` 标签包裹？对 GLM 系列模型，显式标签比纯 Markdown 标题有更强的"这是硬规则"语义提示。

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

四项字段，纯标准库获取：

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

用纯目录遍历而非 `subprocess.run(["git", ...])`，理由：
- **零外部依赖**：不依赖 git 可执行文件，符合 mini_agent 自包含原则。
- **无子进程开销**：目录遍历比起进程快得多。
- **worktree/submodule 漏判**：当前教学场景无此需求，后续按需升级。

## 为什么这样设计

### 为什么不动 `agent.py`

`call_llm` / `agent_loop` 仍然只接收 `messages` 列表，完全不感知 system prompt 的构造。这符合运行时契约中的「核心 loop 保持清晰」原则——prompt 组装是入口层（`__main__.py`）的职责，核心 loop 不掺混。

### 为什么在入口调一次而非每轮调

`build_system_prompt()` 在 `__main__.py` 启动时调一次，构造 `messages[0]`，之后跨轮复用。环境信息（工作目录/平台/日期）在一次会话内不变，无需每轮重新生成。

```python
# __main__.py
messages = [{"role": "system", "content": build_system_prompt()}]
```

### 为什么不照搬 OpenCode 的 `header()` 身份伪装

OpenCode 的 `header()` 对 Anthropic 模型做身份伪装（让 Claude 以为自己不是 Claude）——这是针对特定模型的绕过 hack。mini_agent 对接 GLM 系列，不需要这种绕过，照搬无意义。

### 为什么 `core_rules` 用中文

- 对接的 `EB-GLM-5.2` 是中文模型，中文 prompt 指令遵循度更高。
- 教学仓库面向中文学习者，prompt 可读性优先。
- `environment()` 的字段名用英文（`Working directory` 等）——结构化标签英文更稳定，模型识别一致性好。混搭是合理的。

## 使用指导

### 本版可用的命令

```bash
# 看 system prompt 完整内容
$env:PYTHONPATH="src"; python -c "from mini_agent.prompt import build_system_prompt; print(build_system_prompt())"

# 跑 smoke test
$env:PYTHONPATH="src"; python tests/test_prompt.py

# 实跑 agent，观察行为变化
$env:PYTHONPATH="src"; python -m mini_agent "你好，简单介绍下你自己"
```

### 本版典型示例

**示例 1：查看组装后的 system prompt**
```bash
$env:PYTHONPATH="src"; python -c "from mini_agent.prompt import build_system_prompt; print(build_system_prompt())"
```
预期输出：三段文本——身份描述 + `<rules>` 行为规范 + `<env>` 环境信息。

**示例 2：对比 v0.06 vs v0.07 的模型自我介绍**
```bash
# v0.06：一行 prompt，模型可能啰嗦或加 emoji
git checkout v0.06
$env:PYTHONPATH="src"; python -m mini_agent "你好，简单介绍下自己"

# v0.07：规范 prompt，模型简洁、无 emoji、Markdown 格式
git checkout v0.07
$env:PYTHONPATH="src"; python -m mini_agent "你好，简单介绍下自己"
```
预期：v0.07 的回复更简洁、结构化、无 emoji，自报身份为 mini_agent。

**示例 3：环境信息帮助路径解析**
```bash
$env:PYTHONPATH="src"; python -m mini_agent "读取 examples/input.txt 并告诉我内容"
```
预期：模型从 `<env>` 知道工作目录，能正确解析相对路径，无需猜。

### 本版独有特性

- **环境感知**：模型知道工作目录、平台、日期、git 状态，不再靠猜。
- **行为收敛**：回复简洁、无 emoji、不复述工具输出、不主动总结。
- **多 agent 预留**：`header(agent_name)` 参数已就位，后续加 explore/plan 只需在 dict 加一行。

## 动手验证

1. **跑 smoke test**：
   ```bash
   $env:PYTHONPATH="src"; python tests/test_prompt.py
   ```
   预期：4 个 PASS（组装成功 / header build / 未知回退 / 环境字段）。

2. **查看 system prompt 内容**：
   ```bash
   $env:PYTHONPATH="src"; python -c "from mini_agent.prompt import build_system_prompt; print(build_system_prompt())"
   ```
   预期：输出含 `mini_agent`、`<rules>`、`<env>` 三段。

3. **对比 v0.06 vs v0.07 行为**：
   ```bash
   git checkout v0.06
   $env:PYTHONPATH="src"; python -m mini_agent "你好"
   # v0.06：可能啰嗦、加 emoji、不报身份

   git checkout v0.07
   $env:PYTHONPATH="src"; python -m mini_agent "你好"
   # v0.07：简洁、Markdown、自报 mini_agent 身份
   ```

## 本版完整代码

- [`src/mini_agent/prompt.py`](../../src/mini_agent/prompt.py) — 分层组装 system prompt
- [`src/mini_agent/__main__.py`](../../src/mini_agent/__main__.py) — 入口调用 `build_system_prompt()`
- [`tests/test_prompt.py`](../../tests/test_prompt.py) — smoke test
