# 第 41 课：让 Agent 找回相关记忆，而不是翻遍所有记录

上一课：[给 Agent 一份可控的长期备忘录](40-persistent-memory.md) · [教程总览](README.md) · 下一课：[具名本地资料](42-local-references.md)

> 代码快照：`v0.41` · 相邻差异：`v0.40..v0.41` · 命令环境：Bash/zsh

## 本课目标

第 40 课让 Agent 能把“测试命令”“发布约定”这类资料保存到工作区记忆中。记录少时，
逐页查看还可以；记录多起来后，逐页翻找很慢，把所有正文都放进每次模型请求又会
浪费上下文空间。

本课增加“相关记忆检索”。“检索”就是拿当前任务中的关键词和记忆逐条比较，挑出
少量候选并排序；“上下文（Context）”就是本轮真正发送给语言模型的消息。检索结果
只是供模型参考的旧资料，不会自动变成当前任务的事实或指令。

读完本课，你应能回答：

- 显式的 `search_memories` 和每轮自动候选有什么区别；
- 为什么只读取当前任务和最近一条用户消息来构造自动查询；
- 为什么“命中”不等于“一定进入上下文”；
- 如何让相关资料有界、可解释，并在记忆文件损坏时只影响当前资料区。

## 前置条件

先阅读[第 40 课：给 Agent 一份可控的长期备忘录](40-persistent-memory.md)，理解
工作区记忆、schema 1 JSON 和 `MemoryStore`。需要 Python 3.10+；下面的离线命令使用
Bash/zsh，不需要真实模型服务。

先切到本课快照并查看相邻版本的差异：

```bash
git checkout v0.41
git diff --stat v0.40..v0.41
```

阅读结束后可以回到原来的分支：

```bash
git checkout -
```

## 上一版的问题：记得很多，不等于找得出来

v0.40 的 `list_memories` 只按存储顺序分页，`read_memory` 需要先知道目标 ID。假设
工作区里有几十条记忆，当前任务是“为什么 CI 的类型检查失败”，模型需要找到测试、
CI 和类型检查相关资料；让它把每一条都读一遍，既增加调用次数，也会把无关正文挤掉
当前任务和最近工具结果。

v0.41 把问题拆成两步：先用轻量的词法方法找“可能相关”的候选，再由上下文预算
决定哪些候选能进入本轮消息。需要完整细节时，模型仍然必须显式调用
`read_memory(memory_id)`。

## 本版新增什么

本版的主线是：

```text
当前任务 + 最近一条用户消息
              ↓
        构造一个有界查询
              ↓
MemoryStore.snapshot() → MemoryRetriever → 候选排序
              ↓
最多 4 条、最多 2400 字符、还要有剩余 token 才能进入 Context
              ↓
      [Relevant Memory — Untrusted Reference]
```

这里的 `Untrusted Reference` 意为“不可信参考资料”：它可以帮助模型回忆，但不能
覆盖系统指令、项目规则、Plan、权限判断或验证结果。候选只存在于本次
`prepare_messages()` 返回的消息视图中，不会写入历史和 session。

主要代码变化如下：

## 新增与改动文件

| 文件 | 作用 |
|---|---|
| [`retrieval.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.41/src/mini_agent/retrieval.py) | 用标准库做词法匹配、加权、稳定排序和片段裁剪。 |
| [`memory.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.41/src/mini_agent/memory.py) | 提供只读的 `snapshot()`，给检索层一份一致快照。 |
| [`tools/memory.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.41/src/mini_agent/tools/memory.py) | 增加只读 `search_memories(query, limit)`。 |
| [`context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.41/src/mini_agent/context.py) | 把候选作为临时资料区纳入消息预算。 |
| `__main__.py`、`resume.py`、`config.py` | 只在父 Runtime 绑定检索器，并提供开关。 |

## 版本变更定位

图中的 `[旧]` 是 v0.40 已有能力，`[+]` 是本课新增，`[~]` 是本课修改，`[C]`
表示主要使用者，`[B]` 表示本课边界。

上一版的入口是分页查看：

```text
[旧][C] 父 Agent Runtime
      └─→ [旧] MemoryStore
            ├─→ [旧] list_memories / read_memory → tool history
            └─→ [旧] 记忆文件
      [B] Context 不会自动挑选相关记忆
```

本版在“准备发给模型的消息”这一步增加只读候选，同时保留显式搜索工具：

```text
[C] 父 Agent Runtime
      ├─→ [~] ContextManager.prepare_messages()
      │       ├─→ [+] MemoryStore.snapshot()
      │       ├─→ [+] MemoryRetriever → 排序候选
      │       └─→ [+] 临时资料区 → LLM
      └─→ [~] 父 ToolRegistry
              └─→ [+] search_memories → 有界 tool result

[B] 候选不进入 history、State、session、Plan、Trace 或 verification evidence；
    不做向量检索、自动写记忆、远程资料读取或 References。
```

## 关键流程

### 1. 先读一个只读快照

`MemoryStore.snapshot()` 会按第 40 课的规则校验整个 JSON，然后返回一份深拷贝。它
不会创建目录、取得写锁或修改任何持久状态。检索器只依赖这份公开快照，不需要知道
MemoryStore 如何加锁和原子替换。

每次准备模型消息时只读取一次快照。因此另一个进程刚刚提交的新记忆，会在下一次
`prepare_messages()` 中可见；旧的候选不会因为 `/new`、`/reset` 或恢复而被带回。

### 2. 用容易解释的词法方法排序

v0.41 不引入分词包、向量数据库或 embedding（把文字变成向量再比较语义相似度的
方法）。它先做 Unicode NFKC 归一化和 `casefold()`，再匹配连续的英文、数字、代码
标识；中文还会保留短语并生成双字片段，所以“持续集成”和其中的双字查询可以互相
命中。

模型也可以主动搜索，而不必等待自动候选。例如：

```text
search_memories(query="CI 类型检查", limit=5)
```

这个调用只读记忆文件，返回最多 5 条有 ID 和短片段的候选；模型若要确认完整内容，
还要再调用 `read_memory`。因此“搜索到”与“已经把整条记忆当成事实”之间仍然有一道
明确的判断步骤。

字段有不同权重：标题最高，标签其次，来源再次，正文最低；完整查询短语命中还会加分。
同分时继续按更新时间和 `memory_id` 排序。这种固定顺序很重要：同一份快照和同一条
查询应得到相同结果，便于解释和排查。

显式搜索默认返回 5 条，最多 10 条。结果包含 ID、标题、正文片段、来源、更新时间、
分数和命中字段；片段最多 240 字符。`source_status` 固定为 `unverified`，因为第
40 课的 `source` 是自由文本，检索器不会顺着它去访问文件，也不会声称来源仍然新鲜。

### 3. 自动候选只从当前用户意图构造

自动路径使用 `AgentState.task` 和完整本地 history 中最近一条 `role=user` 消息，
拼成最多 1200 字符的查询。它不使用 assistant 输出、工具结果、历史摘要或已经检索
出的记忆正文。这样旧资料只能被当前用户问题“召回”，不能层层扩展成新的查询。

自动路径与显式搜索共用同一套匹配规则，但它不是一次工具调用；它只是每次请求 LLM
前临时准备一小块 system 资料。若模型需要完整正文，仍要自己调用
`read_memory(memory_id)`。

### 4. “命中”还要经过上下文预算

Context 先完成原有的历史裁剪和压缩，再读取快照、按排序加入候选，最后再做一次普通
裁剪。候选必须同时满足三个条件：最多 4 条、资料区最多 2400 字符、剩余输入 token
足够。预算不够时，整体丢弃排名靠后的候选，不把某条候选截成半条。

`ContextStats.memory` 单独记录资料区 token，`tokens` 仍表示所有消息桶的总量。候选
只是 `prepare_messages()` 的临时返回值，所以 `history`、`AgentState.snapshot()`
和 `ContextManager.export_session()` 都不会出现这块资料。

### 5. 读取失败只降级当前资料区

记忆文件不存在时表示空集合；如果 JSON 损坏、schema 不认识或记录越界，父 Context
会放入一条有界的“记忆检索不可用”提示，然后继续构造普通 LLM 消息。下一次准备消息
仍会重新尝试；系统不会因为一次失败就永久关闭检索，也不会把真正的编程错误全部吞掉。

把 `MEMORY_RETRIEVAL_ENABLED` 设为 `False` 后，Context 不读取 Memory 文件，也不会
出现资料区。这个开关只控制自动候选，不会删除记忆，也不影响显式的只读工具合同。

## 实现拆解

检索层和上下文层刻意分开。`retrieval.py` 只回答“哪些记录更相关”，`context.py`
才回答“本轮消息还剩多少空间”。这样将来调整上下文预算时，不必改写记忆文件格式；
调整匹配方式时，也不会意外改变历史和 State。

资料区会明确标注为 `[Relevant Memory — Untrusted Reference]`，并说明它不能覆盖
更高优先级指令，不能直接证明当前文件状态，更不能创建 verification evidence。Context
事件只保留查询字符数、命中数、注入数和 token 数；失败事件只记录异常类型，不记录
正文或存储路径。

## 为什么这样设计

标准库词法检索小、可解释、容易在离线环境运行。标题和标签的高权重让用户可以通过
短标题整理记忆；稳定排序让调试和回放不依赖 JSON 的遍历顺序。把候选放在临时资料区，
则同时保留了“跨任务有帮助”和“不会污染任务账本”这两个性质。

代价是它不理解真正的语义相似度，同义词可能找不到；中文只覆盖本课需要的短语和
双字片段；资料来源没有变化检测。v0.41 刻意不做 embedding、查询缓存、自动记忆
提取、远程访问和 References；工作区外资料将在下一课单独定义读取边界。

## 运行与观察

本课的观察重点是“相关候选进入了本轮视图，但没有变成持久状态”。运行：

```bash
PYTHONPATH=src python -m pytest -q tests/test_memory_v040.py tests/test_memory_retrieval_v041.py tests/test_context.py
```

你应观察到：标题或标签命中的记忆排名更靠前；中文短语和双字查询可以命中；另一个
进程提交新记忆后，下一次 `prepare_messages()` 使用新快照；导出的 Context session
不包含 `[Relevant Memory — Untrusted Reference]`。禁用开关后没有资料区；手动损坏
Memory JSON 时，当前请求仍能构造普通消息，修复后下一次请求会重新尝试。

## 本版特性、下一课与代码索引

v0.41 完成了显式相关搜索和父 Context 的有界自动候选。候选始终是不可信资料，不能
推进 Plan、创建 verification evidence 或覆盖项目指令。下一课将把“工作区内的长期
记忆”与“工作区外的本地资料”区分开来：
[第 42 课：具名本地资料](42-local-references.md)。

- [`retrieval.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.41/src/mini_agent/retrieval.py)：词法匹配、字段权重、稳定排序和结果结构。
- [`memory.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.41/src/mini_agent/memory.py)：`snapshot()` 的只读存储边界。
- [`context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.41/src/mini_agent/context.py)：候选刷新、资料区布局和预算统计。
- [`tools/memory.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.41/src/mini_agent/tools/memory.py)：显式 `search_memories` 工具合同。
- [`test_memory_retrieval_v041.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.41/tests/test_memory_retrieval_v041.py)：本课行为边界的可执行索引。
