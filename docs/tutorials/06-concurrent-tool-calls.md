# 第 6 课：并发 tool_calls

> 版本 v0.06 | [上一课](05-streaming.md) | [下一课](07-system-prompt.md)

## 本课目标

上一版同一轮收到多个工具调用时，会一个接一个执行。只要其中一个工具在等待文件或网络，后面的调用也只能等着。
这一版用 `ThreadPoolExecutor` 同时执行互不依赖的 tool_calls，从而缩短这一轮的等待时间。

## 前置

- 已读 [第 5 课](05-streaming.md)，理解流式 call_llm
- `git checkout v0.06` 切到本版代码

## 新增/改动了什么

```
 src/mini_agent/
 └── agent.py            # 改：agent_loop 里 tool_calls 执行从串行 for 循环改为 ThreadPoolExecutor 并发
```

> 本版只改 `agent.py` 一个文件，且只改 agent_loop 里的 tool_calls 执行部分。

## 核心概念

### 串行 vs 并发

v0.05 是串行执行（前一个完成后才开始下一个）。同一轮的 N 个 tool_calls 用 `for` 循环逐个执行，总耗时 = Σ(每个工具耗时)。

```python
for tc in msg["tool_calls"]:
    result = executor.execute(name, args)   # 第 2 个等第 1 个跑完才开始
```

v0.06 改为并发执行（多个调用同时进行）。`ThreadPoolExecutor` 把 N 个 tool_calls 放进线程池，总耗时约等于最慢的那个调用。

```python
with ThreadPoolExecutor(max_workers=len(tool_calls)) as pool:
    results = list(pool.map(_run, tool_calls))
```

### LLM 何时发多个 tool_calls

问题只会在模型一次返回多个 `tool_calls` 时出现。LLM 可以在一轮回复里发出多个调用；它们通常互相独立，例如：

```
用户："同时读取 a.txt 和 b.txt"

LLM 回复（一轮 2 个 tool_calls）:
  tool_calls[0]: read_file({"path": "a.txt"})
  tool_calls[1]: read_file({"path": "b.txt"})
```

这两个 read_file 互不依赖，读取 a.txt 不需要 b.txt 的结果。因此串行会浪费等待时间，并发可以让它们同时完成。

### ThreadPoolExecutor 实现（`src/mini_agent/agent.py:95`）

```python
tool_calls = msg["tool_calls"]

def _run(tc):
    name = tc["function"]["name"]
    args = json.loads(tc["function"]["arguments"])
    result = executor.execute(name, args)
    print(f"  执行 {name} -> {result}")
    return tc["id"], str(result)

with ThreadPoolExecutor(max_workers=len(tool_calls)) as pool:
    results = list(pool.map(_run, tool_calls))

for tool_call_id, content in results:
    messages.append({
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": content,
    })
```

实现需要注意以下几点：
1. **`max_workers=len(tool_calls)`**：本轮有几个调用就开几个线程。
2. **`pool.map`**：提交和返回都保持原始顺序，所以 `results[0]` 一定对应 `tool_calls[0]`。
3. **`with` 语句**：离开代码块时会自动 `join()`，也就是等所有线程完成后才继续。
4. **回灌顺序**：结果按原序追加为 `role=tool` 消息，模型收到的对应关系不会改变。

## 为什么这样设计

### 为什么用线程而不是协程

`read_file`/`write_file` 主要时间花在 IO（磁盘读写）等待上，线程可以在一个调用等待时运行另一个调用。
协程（asyncio）也能实现并发，但那会要求从 `call_llm` 到 `executor.execute` 的整条调用链都改成 async，超出本课范围。
所以这里选择线程池，改动小且足以解决当前的等待问题。

### 为什么 max_workers = len(tool_calls)

每轮的 tool_calls 通常只有 2～5 个，因此直接按调用数创建线程即可。
agent loop 仍然是一轮一轮执行，当前一轮结束后才会进入下一轮，所以不必在多轮之间复用线程池。

### 为什么 pool.map 而不是 submit + as_completed

`pool.map` 保证**结果顺序与输入顺序一致**。`as_completed` 会按完成先后返回，顺序可能改变。
回灌时需要让 LLM 发出的 tool_calls[0] 对应第一条 tool 消息，因此这里使用 `map`。

### 为什么权限闸门的 _ask_lock 在这里起作用

v0.04 的 `PermissionGate` 已经准备了 `self._ask_lock = threading.Lock()`。
如果两个 write_file 同时触发 ASK，没有这把锁，两个提示可能交错显示。锁让同一时刻只有一个 ASK 交互占用终端，因此本轮并发不会破坏权限确认。

## 使用指导

### 本版可用的命令

```bash
# 让 LLM 一次调多个工具（观察并发）
python -m mini_agent "同时读取 examples/input.txt 和 examples/input2.txt"

# 跑 smoke test
$env:PYTHONPATH="src"; python tests/test_tools.py
```

### 本版典型示例

**示例 1：并发读取两个文件**
```bash
$env:PYTHONPATH="src"; python -m mini_agent "同时读取 examples/input.txt 和 examples/input2.txt，告诉我两个文件的内容"
```
预期输出：
```
=== [1] LLM 回复 ===
  决策调用: read_file({"path": "examples/input.txt"})
  决策调用: read_file({"path": "examples/input2.txt"})
[Executor] 执行 Tool: read_file
[Executor] 参数: {'path': 'examples/input.txt'}
[Executor] 执行 Tool: read_file
[Executor] 参数: {'path': 'examples/input2.txt'}
  执行 read_file -> 3 + 5 * 2
  执行 read_file -> 3 + 5 * 3
```
注意：两个 `[Executor] 执行 Tool: read_file` 会几乎同时出现，这说明调用正在并发执行。
在 v0.05 串行版中，第一个 read_file 打印结果后，第二个才会开始。

**示例 2：并发计算 + 读取**
```bash
$env:PYTHONPATH="src"; python -m mini_agent "计算 100*200，同时读取 examples/input.txt"
```
预期：LLM 一次发 `calculate` + `read_file` 两个 tool_calls，线程池并发执行。

**示例 3：对比串行 vs 并发**
```bash
# v0.05（串行）：两个 read_file 依次执行
git checkout v0.05
$env:PYTHONPATH="src"; python -m mini_agent "同时读取 examples/input.txt 和 examples/input2.txt"

# v0.06（并发）：两个 read_file 同时执行
git checkout v0.06
$env:PYTHONPATH="src"; python -m mini_agent "同时读取 examples/input.txt 和 examples/input2.txt"
```

### 本版独有特性

- **并发执行日志**：多个工具的 `[Executor]` 日志可能交错出现，因为它们同时运行。
- **结果按原序回灌**：虽然执行是并发的，`pool.map` 仍保证 results 与 tool_calls 原序一致。
- **权限锁生效**：并发触发多个 write_file 的 ASK 时，锁会让提示逐个出现。

## 动手验证

1. **跑 smoke test**：
   ```bash
   $env:PYTHONPATH="src"; python tests/test_tools.py
   ```
   预期：8 个 PASS。

2. **并发读取两个文件**：
   ```bash
   $env:PYTHONPATH="src"; python -m mini_agent "同时读取 examples/input.txt 和 examples/input2.txt"
   ```
   预期：两个 read_file 的 Executor 日志几乎同时出现，结果一起回灌。

3. **对比 v0.05 vs v0.06**：
   ```bash
   git checkout v0.05
   $env:PYTHONPATH="src"; python -m mini_agent "同时读取 examples/input.txt 和 examples/input2.txt"
   # 串行：第一个 read_file 结果打印后，第二个才开始

   git checkout v0.06
   $env:PYTHONPATH="src"; python -m mini_agent "同时读取 examples/input.txt 和 examples/input2.txt"
   # 并发：两个 read_file 的 Executor 日志几乎同时出现
   ```

## 本版完整代码

- [`src/mini_agent/agent.py`](../../src/mini_agent/agent.py) — agent_loop 里 tool_calls 并发执行

---

## v0.06.1 修订：多轮上下文状态契约

> 版本 v0.06.1 | v0.06 的 patch，不改功能，只修多轮上下文的隐性 bug。

### 发现的问题

上一版虽然能工作，但调用方不容易看出 `agent_loop` 会怎样改变传入的 `messages`。它通过 **list 可变副作用** 追加 assistant / tool 消息，规则只藏在源码和含糊的 docstring 里。
这会让多轮调用时难以判断列表当前是否完整。

由此引发两个实际问题：

1. **`__main__.py` 两条路径分叉**：argv 分支和交互循环分支维护 messages 的方式不同。虽然 argv 分支实际上依靠副作用拿到了完整状态，但读代码时很难确认调用后列表是什么样，后续维护容易出错。

2. **半截状态未文档化**：当 `agent_loop` 因达到 `MAX_ITERATIONS` 提前返回 `"达到最大迭代次数"` 时，messages 会停在 **"有 tool_calls 但无对应 tool 结果"** 的半截状态。下一轮调用前若不处理，OpenAI 协议会因 `tool_calls` 后缺 `role=tool` 消息而报错。此前这个边界完全没文档。

### 修复点（方案 A：不改签名）

这一版选择**不改 `agent_loop` 签名**（保持 `agent_loop(messages)` 单参数），因为：
- `tests/test_loop.py` 的 `test_agent_loop_signature` 断言签名必须只有 `messages`，改签名会破坏现有测试。
- 问题本质不在签名，而在契约不清晰和调用方路径分叉。

具体改动：

**`src/mini_agent/agent.py` — `agent_loop` docstring 显式契约化**

```python
def agent_loop(messages):
    """循环：调 LLM -> 有 tool_calls 就执行并回灌 -> 无则结束。

    【messages 契约】本函数以副作用方式向传入的 messages 列表追加内容，
    调用方应跨轮复用同一个 list 对象，不要重新构造。每轮会 append：
      1. assistant 消息（含 content 和/或 tool_calls）
      2. 若有 tool_calls：对应每条的 role=tool 结果消息
    返回值是最终 assistant 回复的 content 字符串（仅用于打印），
    真正的上下文状态已写入 messages，调用方无需再手动 append assistant 回复。

    注意：若因达到 MAX_ITERATIONS 提前返回，messages 可能停在
    "有 tool_calls 但无对应 tool 结果"的半截状态，下一轮调用前
    调用方有责任处理该状态（当前实现未做清理，长任务可能触发协议错误）。
    """
```

**`src/mini_agent/__main__.py` — 统一 argv 与交互循环路径**

改前：argv 分支单独写一套逻辑，交互循环另一套，两路径行为不一致隐患。

改后：argv 分支与交互循环走同一套"append user → agent_loop → 复用 messages"路径，并在文件顶部注释点明 messages 状态契约：

```python
# messages 列表跨轮复用：agent_loop 以副作用方式向其追加
# assistant / tool 消息，调用方无需手动 append assistant 回复。
# 详见 agent.agent_loop 的 docstring 契约。
messages = [{"role": "system", "content": "你是一个助手，通过调用工具完成任务。"}]

# 命令行首条任务（可选）：与交互循环走同一套路径，
# 保证 argv 分支后 messages 状态完整，后续追问上下文不丢。
if len(sys.argv) > 1:
    messages.append({"role": "user", "content": sys.argv[1]})
    reply = agent_loop(messages)
    print(reply)
```

### 为什么是 patch 不是 minor

- 没有新增功能，也没有改变行为；argv 分支本来就依靠副作用保存完整状态。
- 改动只是把原来隐含的规则写清楚，减少维护时的误解。
- 测试无需增加，因为写清契约不会改变可观察结果。

### 遗留问题（未在本版修）

1. **MAX_ITERATIONS 半截状态清理**：docstring 现在会提示风险，但运行时仍不会自动清理。长任务达到上限后，下一轮仍可能触发协议错误。v0.11 的上下文架构才会要求每轮先回灌全部 tool results。
2. **`agent_loop` 返回值语义**：当前返回的 content 字符串只用于打印，真正的多轮状态保存在 messages 列表中。这个双通道设计暂时保留，改动签名会影响现有调用。

### 本版改动文件

```
src/mini_agent/
├── agent.py        # 改：agent_loop docstring 契约化
└── __main__.py     # 改：统一两路径，加 messages 契约注释
```
