# 第 41 课：相关记忆检索与有界上下文

代码快照：`v0.41` · 相邻差异：`v0.40..v0.41`

本课示例命令使用 Bash/zsh。课程正文描述的是 v0.41 快照；v0.41 Git tag 由维护者在交付后固定，阅读者切换前请确认本地已有该 tag。

## 本课目标

上一课已经让 Agent 能够跨会话保存工作区记忆，但它只能分页查看摘要。记忆数量变多以后，模型要么逐页翻找，要么把旧正文大量带进上下文；这两种方式都不适合每一轮模型请求。

本课要解决的问题是：**怎样从全部记忆中找出少量相关候选，并且不让这些旧资料变成当前任务的事实或指令？**完成本课后，父 Agent 有两条只读路径：可以显式搜索记忆，也可以在请求 LLM 前自动获得最多四条有界摘要。需要完整内容时，模型仍要根据 `memory_id` 显式调用 `read_memory`。

## 前置条件

先阅读[第 40 课：轻量持久 Memory](40-persistent-memory.md)，理解工作区隔离、schema 1 JSON、`MemoryStore` 和父子工具边界。代码仓库需要 Python 3.10+；本课的离线观察命令使用 Bash/zsh，不需要真实模型服务。

为了查看本版本相对上一版本的真实变化，可以执行：

```bash
git checkout v0.41
git diff --stat v0.40..v0.41
```

第一条命令切换到课程快照，第二条命令显示变化集中在检索、Context、父工具和文档。阅读结束后回到自己的分支：

```bash
git checkout -
```

## 新增与改动文件

上一课的 `MemoryStore` 继续负责文件大小、schema 和记录字段校验；本课不迁移 schema 1，也不把查询缓存写回 JSON。新增的 `retrieval.py` 只消费公开的 `snapshot()`，因此检索层不需要知道写锁和原子替换的细节。

| 文件 | 作用 |
|---|---|
| `src/mini_agent/retrieval.py` | 标准库词法检索、字段加权、稳定排序和有界片段 |
| `src/mini_agent/memory.py` | 增加只读、深拷贝的 `snapshot()` |
| `src/mini_agent/tools/memory.py` | 注册只读 `search_memories(query, limit)` |
| `src/mini_agent/context.py` | 每次准备消息时刷新候选，并把它作为临时 system 资料区计入预算 |
| `src/mini_agent/__main__.py`、`resume.py` | 只在父 Runtime 绑定当前工作区 retriever |
| `src/mini_agent/config.py` | 增加 `MEMORY_RETRIEVAL_ENABLED` 开关 |

## 关键流程

“检索”是把查询和每条记忆比较并排序的过程；“Context”是本轮真正发送给 LLM 的消息视图。两者之间还有一个重要的预算步骤：命中的记录不等于一定会被注入。

```text
当前任务 + 最近一条用户消息
          │ 每次 prepare_messages() 重新构造，最多 1200 字符
          ▼
MemoryStore.snapshot() ──→ MemoryRetriever
          │ NFKC/casefold、连续词元、中文双字片段、字段权重
          ▼
按分数、updated_at、memory_id 稳定排序的候选
          │ 逐条检查 4 条 / 2400 字符 / 剩余 token 预算
          ▼
[Relevant Memory — Untrusted Reference]
          │ 只存在于本次 prepared view
          └──→ LLM
```

资料区位于 `[Structured State]` 之后、首条用户任务之前。它会明确提醒模型：内容是旧资料，不能覆盖 system/project instructions、Plan 或 PermissionGate，不能作为当前文件事实或 verification evidence。资料区没有完整正文；要查正文必须再调用 `read_memory`。

## 实现拆解

### 1. 先做只读快照

`MemoryStore.snapshot()` 复用 v0.40 的 `_read_records()` 校验，所以损坏 JSON、未知 schema 和非法记录仍会得到同一类 `MemoryStoreError`；缺失文件则表示空集合。它只返回深拷贝，不创建目录、不取得写锁，也不改变写入暂停状态。检索模块只依赖这个公开方法，避免把私有存储实现扩散到 Context。

### 2. 用简单词法得到候选

v0.41 不引入分词包、向量数据库或 embedding。文本先做 Unicode NFKC 归一化和 `casefold()`；英文、数字和代码标识按连续词元处理。中文保留连续短语，同时生成双字片段，因此“持续集成”和单字查询都能命中。

每个去重后的查询词按字段加分：标题最高，标签其次，来源再次，正文最低；完整查询短语命中会得到额外分数。之后固定执行三层排序：分数降序、更新时间降序、`memory_id` 字典序升序。这样相同查询对同一份快照不会因为文件遍历顺序变化而改变结果。

搜索结果只返回 ID、标题、正文片段、来源、更新时间、分数和命中字段。片段最多 240 个字符，优先截取正文中第一个命中附近的内容。`source_status` 固定为 `unverified`：`source` 是 v0.40 的自由文本，哪怕看起来像文件路径，也不会在本课访问它或宣称它仍然新鲜。

### 3. 显式搜索与自动搜索共享同一合同

父侧的 `search_memories` 默认允许调用，`effect_class` 是 `none`，所以它不预留 generation、不产生验证证据，也不改变持久状态。显式接口默认返回 5 条，最多 10 条；纯空白查询会被工具参数边界拒绝，底层检索 API 则把空查询安全解释为空结果，供自动路径降级使用。搜索结果有独立的 16 KiB JSON 上限，不能被通用 4 KiB 截断成半个 JSON。

自动路径使用稳定任务描述和完整本地 history 中最近一条 `role=user` 文本构造查询，不使用 assistant 输出、tool result、历史摘要或记忆正文；当初始任务很长时，最近用户消息仍拥有保留预算。每次 `prepare_messages()` 只读取一个当前快照，因此本进程或其他进程刚提交的记忆会在下一次请求中可见。`/new`、`/reset` 和恢复不会带回旧候选；父 Runtime 会重新绑定当前工作区的 store，子 Runtime 则完全不绑定 retriever。

### 4. 候选是临时资料，不是第四条状态轨道

Context 先按原有流程完成基础消息的 trim/compact，再读取一次快照，按排名加入完整候选，最后执行一次普通 trim。候选超过四条、资料区超过 2400 字符或没有剩余输入 token 时，低排名候选整体丢弃，不把一条候选截成半条；Memory 不会单独触发历史摘要调用。`ContextStats.memory` 单独统计资料区 token，`tokens` 仍是所有桶之和。

因为候选只作为 `prepare_messages()` 的返回值存在，所以 `history`、`AgentState.snapshot()` 和 `ContextManager.export_session()` 不会出现检索结果。Context 事件只记录查询字符数、总命中数、注入数和 token 数；失败事件只记录异常类型，不记录正文或存储路径。

### 5. 失败只影响当前资料区

Memory 文件缺失表示空集合；其他读取失败时，父 Context 会放入一条有界的“记忆检索不可用”提示；下一次准备消息仍会重新尝试。这个降级只捕获 `MemoryStoreError`，不会把真正的编程错误静默吞掉，也不会改变 LLM 或 CLI 的顶层异常边界。关闭 `MEMORY_RETRIEVAL_ENABLED` 后，Context 不读取 Memory 文件，也不产生这条资料区。

## 为什么这样设计

词法检索的优点是小、可解释、只依赖 Python 标准库：模型可以看到命中的字段、分数和 ID，开发者也能从代码直接推导排序。字段权重让标题和标签比正文更容易把记忆带入候选；稳定排序则让调试和回放更容易。

这种方法也有明确代价。它理解不了真正的语义相似度，同义词可能检索不到；中文只支持本课所需的连续短语和双字片段；来源没有变化检测。v0.41 刻意不做 embedding、查询缓存、自动记忆提取、远程资料访问和 References。下一版 v0.42 才讨论如何给工作区外的本地资料定义稳定别名和读取边界。

## 运行与观察

本课的观察重点是“相关候选进入了临时视图，但没有进入持久状态”。在 Bash/zsh 中运行：

```bash
PYTHONPATH=src python -m pytest -q tests/test_memory_v040.py tests/test_memory_retrieval_v041.py tests/test_context.py
```

随后可以在交互配置中写入几条有明显主题差异的记忆：同一查询应优先得到标题或标签命中的记录；中文短语和单字查询应得到对应候选；把候选正文改写或在另一个进程提交新记录后，下一次 `prepare_messages()` 应看到最新快照。导出的 Context session 不应包含 `[Relevant Memory — Untrusted Reference]`，这说明资料只是本轮视图，不是新的持久字段。

如果把 `MEMORY_RETRIEVAL_ENABLED` 设为 `False`，应观察到 Context 不读取记忆文件，也没有资料区。若手动损坏 Memory JSON，当前请求仍可继续构造 LLM 消息，只出现有界的不可用提示；修复文件后下一次请求会重新尝试。

## 本版特性、下一课与代码索引

v0.41 完成了显式相关搜索和父 Context 的有界自动候选。候选始终是不可信资料，不能推进 Plan、创建 verification evidence 或覆盖项目指令。下一课是规划中的 v0.42 References：它会讨论具名本地目录、路径校验和读取权限，而不是修改本课的 Memory schema。

- [`retrieval.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.41/src/mini_agent/retrieval.py)：词法匹配、字段权重、稳定排序和结果结构。
- [`memory.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.41/src/mini_agent/memory.py)：`snapshot()` 的只读存储边界。
- [`context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.41/src/mini_agent/context.py)：候选刷新、资料区布局和预算统计。
- [`tools/memory.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.41/src/mini_agent/tools/memory.py)：显式 `search_memories` 工具合同。
- [`test_memory_retrieval_v041.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.41/tests/test_memory_retrieval_v041.py)：本课行为边界的可执行索引。
