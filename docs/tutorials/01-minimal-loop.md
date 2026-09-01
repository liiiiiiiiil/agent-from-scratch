# 第 1 课：最简 agent loop

> 版本 v0.01 | [下一课](02-first-tool.md)

> 代码快照：`v0.01` · 相邻差异：无（首版） · 命令环境：Bash/zsh
>
> 运行要求：Python 3.9+。

## 本课目标

这一课先解决一个最基础的问题：怎样让程序把用户的话交给 LLM，并拿回最终回答。

我们会从零搭出 agent loop，也就是“反复调用模型并处理回复的循环”。v0.01 还没有任何工具，所以它只展示循环骨架：调用 LLM → 收到回复 → 判断是否结束。

## 前置

- Python 3.9+
- 已按 [教学路径 README](README.md) 完成环境准备（克隆仓库、填 config.py、装好包）
- `git checkout v0.01` 切到本版代码

## 新增了什么

本版是起点，没有"上一版"。全部文件如下：

```
src/mini_agent/
├── __init__.py         # 包入口（空文件）
├── __main__.py         # CLI 入口：python -m mini_agent
├── agent.py            # agent loop：call_llm + agent_loop
└── config.py           # BASE_URL/API_KEY/MODEL/MAX_ITERATIONS（硬编码）
tests/
└── test_loop.py        # import 链路 smoke test
```

## 核心概念

### 什么是 agent loop

普通聊天程序调用一次 LLM 就结束。编程 agent 不同：模型可能先要求调用工具，拿到工具结果后才能继续回答。因此 agent 的核心是一个**循环**：

1. 把对话历史（`messages` 列表）发给 LLM
2. LLM 返回一条回复，append 到 `messages`
3. 判断回复里有没有 `tool_calls`
   - 没有 → LLM 给出最终答案，循环结束
   - 有 → 执行工具，把结果 append 回 `messages`，回到第 1 步

`tool_calls` 表示模型请求程序调用工具。v0.01 没有向模型提供工具，所以模型不会产生这类请求，通常回复一次就会结束。这里仍然保留判断分支，因为后续版本会在这个位置接入工具。

### messages 列表

LLM 本身不会记住上一次请求。为了让它看懂对话前后关系，程序需要在每次请求时重新发送已有消息。

这些消息保存在 `messages` 中。它是一个由字典组成的 Python 列表，每个字典记录一条消息及其发送者：

```python
messages = [
    {"role": "system", "content": "你是一个助手。"},
    {"role": "user", "content": "你好"},
    {"role": "assistant", "content": "你好！有什么可以帮你？"},
]
```

每轮调用 LLM 时，程序都会发送完整的 `messages`。收到回复后，再用 `append` 把新消息放到列表末尾。因为下一轮还能看到前面的消息，所以模型表现得像是“记住了”对话。

### call_llm：用 http.client 调 LLM

`src/mini_agent/agent.py:12` 中的 `call_llm` 负责把 `messages` 变成 HTTP 请求，并从响应中取出模型消息：

```python
def call_llm(messages):
    p = urlparse(BASE_URL)
    conn = http.client.HTTPConnection(p.hostname, p.port or 80, timeout=120)
    body = json.dumps(
        {"model": MODEL, "messages": messages},
        ensure_ascii=False,
    ).encode()
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        "Accept-Encoding": "identity",   # ← 关键，见下文"为什么这样设计"
    }
    conn.request("POST", f"{p.path.rstrip('/')}/chat/completions", body=body, headers=headers)
    resp = conn.getresponse()
    data = json.loads(resp.read().decode("utf-8"))
    conn.close()
    return data["choices"][0]["message"]
```

这里使用 OpenAI Chat Completions 协议。程序向 `{BASE_URL}/chat/completions` 发送 POST 请求，在请求体中放入 `model` 和 `messages`，并通过 `Authorization` 请求头传递 API key。

响应仍是 JSON。`data["choices"][0]["message"]` 就是本轮回复，调用方会把它追加到 `messages`。

### agent_loop：循环主体

`src/mini_agent/agent.py:34` 的 `agent_loop`：

```python
def agent_loop(messages):
    for i in range(MAX_ITERATIONS):
        msg = call_llm(messages)
        messages.append(msg)

        print(f"\n=== [{i+1}] LLM 回复 ===")
        print(msg.get("content", ""))

        if not msg.get("tool_calls"):
            return msg.get("content", "")

    return "达到最大迭代次数"
```

这里有三个要点：
1. **跨轮复用消息**：`messages` 由调用方创建，`agent_loop` 只负责追加。这样 `__main__.py` 多次调用 loop 时仍能保留之前的对话。
2. **判断何时结束**：`if not msg.get("tool_calls")` 表示模型没有请求工具，此时当前文本就是最终回答。v0.01 没有提供工具，因此第一轮通常就会走到这里。
3. **限制循环次数**：`MAX_ITERATIONS = 10` 防止循环一直运行。达到上限后会返回 `"达到最大迭代次数"`。这一版不会进一步解释任务为什么没有完成。

### __main__.py：CLI 入口

`src/mini_agent/__main__.py` 支持两种模式：

- **命令行首条任务**：`PYTHONPATH=src python -m mini_agent "你的任务"` — 先处理参数，再进入同一个交互循环
- **交互模式**：`python -m mini_agent` — 进入 `你: ` 提示符，每输一行调一次 loop，`exit`/`quit` 退出

两种模式共用同一个 `messages` 列表，所以交互模式下跨轮有上下文。

## 为什么这样设计

### 为什么用 http.client 而不是 requests

项目使用的网关在收到 `Accept-Encoding: gzip` 时可能返回 502。`http.client` 属于 Python 标准库，并且可以准确控制请求头，所以代码显式发送 `Accept-Encoding: identity`，要求服务端不要压缩响应。

这是项目的运行约束，后续版本也继续使用 `http.client`。

### 为什么不用 try/except 兜底

`agent_loop` 没有使用 `try/except` 包住 `call_llm`。网络中断、API key 错误或响应无法解析时，异常会直接交给上层。

这一课只保留“调用 LLM → 判断回复 → 结束”的主路径。下一课开始，工具执行中的可预期错误会由 `ToolExecutor` 处理；LLM 调用和 CLI 顶层异常仍不会在 loop 中被隐藏。

### 为什么 config 硬编码

`BASE_URL`、`API_KEY` 和 `MODEL` 直接写在 `config.py` 中，不读取环境变量。这样初学者只需修改一个文件就能运行。代价是它不适合多环境部署，真实配置也不能提交到版本库。

## 使用指导

### 本版可用的命令

```bash
# 命令行首条任务模式
PYTHONPATH=src python -m mini_agent "你好"

# 交互模式
PYTHONPATH=src python -m mini_agent

# 跑 smoke test（验证 import 链路）
PYTHONPATH=src python tests/test_loop.py
```

> 本课命令按 Bash/zsh 编写；未 `pip install -e .` 时需要显式设置 `PYTHONPATH=src`。处理完命令行首条任务后，程序仍会进入交互循环；输入空行、`exit`、`quit` 或发送 EOF 可退出。

### 本版典型示例

**示例 1：命令行首条任务**
```bash
PYTHONPATH=src python -m mini_agent "用一句话介绍你自己"
```
预期输出：
```
=== [1] LLM 回复 ===
我是一个 AI 助手，可以帮你回答问题、提供建议。
我是一个 AI 助手，可以帮你回答问题、提供建议。
```
（第二行是 `__main__.py` 里 `print(reply)` 打的，和上面重复——v0.01 的小瑕疵，后续会优化）

**示例 2：交互模式**
```bash
PYTHONPATH=src python -m mini_agent
```
进入后：
```
你: 我叫小明
=== [1] LLM 回复 ===
你好，小明！有什么可以帮你？
你: 我刚才说我叫什么？
=== [1] LLM 回复 ===
你刚才说你叫小明。
你: exit
```
第二轮 LLM 能回答“小明”，不是因为模型在服务端保存了记忆，而是因为程序再次发送了包含前一轮内容的 `messages`。

### 本版独有特性

- **无工具**：v0.01 的 LLM 只能生成文本，不能真的读取文件。它可能说明自己做不到，也可能生成未经验证的内容。
- **非流式**：LLM 回复是整段返回的，终端一次性打印，没有打字机效果（v0.05 才加流式）。
- **无权限交互**：因为没工具，没有任何"是否允许执行"的提示。

## 动手验证

跑完下面三步确认你理解了 v0.01：

1. **跑 smoke test**：
   ```bash
   PYTHONPATH=src python tests/test_loop.py
   ```
   预期：打印 3 个 `PASS: ...` 和 `全部 smoke test 通过`。

2. **命令行首条任务**：
   ```bash
   PYTHONPATH=src python -m mini_agent "1+1等于几"
   ```
   预期：LLM 回答"2"（它自己算的，不是调工具——v0.01 没工具）。

3. **交互模式测上下文**：
   ```bash
   PYTHONPATH=src python -m mini_agent
   ```
   先输 `我叫张三`，再输 `我叫什么？`，预期 LLM 能答出"张三"。

## 本版完整代码

- [`src/mini_agent/agent.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.01/src/mini_agent/agent.py) — `call_llm` + `agent_loop`
- [`src/mini_agent/__main__.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.01/src/mini_agent/__main__.py) — CLI 入口
- [`src/mini_agent/config.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.01/src/mini_agent/config.py) — 配置
- [`tests/test_loop.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.01/tests/test_loop.py) — smoke test（预期 3 个 PASS）
