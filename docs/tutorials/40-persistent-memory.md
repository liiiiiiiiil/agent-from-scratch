# 第 40 课：让 Agent 明确保存和修订工作区记忆

上一课：[把子代理结果安全地交给父 Agent](39-durable-delegation.md) · [教程总览](README.md) · 下一课：阶段十一（相关性检索，规划中）

> 代码快照：`v0.40` · 相邻差异：`v0.39..v0.40` · 命令环境：Bash/zsh
>
> `v0.40` tag 由仓库所有者手动创建；本课不代为创建或推送 tag。

## 本课目标

Agent 经常会在一次任务中发现一条以后仍有用的项目知识：例如约定的测试命令、用户确认的设计取舍或一个需要长期注意的边界。只把它留在当前对话里，下一次任务就会忘记；把它自动写进项目文件，又可能污染源码或把旧话当成指令。

本课增加一个很小的 Memory（工作区记忆）层。它只在父 Agent 通过明确工具调用时保存、查看、修订或遗忘资料。读完后，你应能解释：

- 为什么 Memory 与 `AgentState`、Plan、verification evidence 和 `/save` session 分开；
- 五个工具如何通过权限闸门和既有 durable tool boundary；
- 为什么 `revision` 能阻止旧调用覆盖新内容；
- 为什么崩溃恢复会把已准入但未提交的记忆写入视为不确定事实而不自动重放。

## 前置条件

需要第 39 课、基础 Python、Bash/zsh 和 Git 知识。下面的命令用于查看代码和差异；它们不要求真实 API key。

```bash
git checkout v0.39
git diff --stat v0.39..v0.40
git checkout v0.40
```

上面的 `git diff --stat` 先让你看到版本规模；切到 `v0.40` 后，示例命令和源码链接才对应本课实现。

## 新增与改动文件

本版的主线是“父 Agent 明确调用 → 权限审阅 → 工作区独占写入 → 下次任务可查看”：

| 文件 | 作用 |
|---|---|
| `src/mini_agent/memory.py` | 按规范化工作区路径选择 JSON 文件，校验记录，处理锁、原子替换和同步错误。 |
| `src/mini_agent/tools/memory.py` | 提供 `list_memories`、`read_memory`、`remember`、`revise_memory`、`forget_memory` 五个父侧工具。 |
| `src/mini_agent/tools/__init__.py` | 只有绑定父 `AgentState` 的 registry 才创建和注册 MemoryStore；子代理过滤视图看不到这些工具。 |
| `src/mini_agent/permission.py` | 查看默认放行，新增、修订、遗忘默认询问，并把目标 revision 和拟写正文交给用户审阅。 |
| `src/mini_agent/config.py`、`config_example.py` | 增加可选的 `MEMORY_DIR`，默认是 `~/.mini_agent/memory`。 |
| `src/mini_agent/state.py`、`output.py`、`prompt.py` | 摘要不展开记忆正文，提示模型把 Memory 当作不可信资料。 |
| `tests/test_memory_v040.py` | 覆盖存储、工具注册、并发、损坏和原子提交边界。 |

完整实现可从固定快照阅读：[`memory.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.40/src/mini_agent/memory.py)、[`tools/memory.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.40/src/mini_agent/tools/memory.py)、[`permission.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.40/src/mini_agent/permission.py)。

## 版本变更定位

图例：`[旧]` v0.39 已有，`[+]` v0.40 新增，`[~]` v0.40 修改，`[C]` 主要消费者，`[B]` 本课边界。

上一版已经有父 Runtime、Executor、PermissionGate 和 schema 3 工具边界，但没有跨任务资料入口：

```text
[旧][C] 父 AgentRuntime
      -> [旧] ToolRegistry / ToolExecutor
      -> [旧] AgentState + Context
      -> [旧] /save session（可选、面向当前任务恢复）
      [B] 没有独立的工作区长期记忆
```

本版在父 registry 中插入一个独立的资料通道：

```text
[C] 父 AgentRuntime
      -> [~] PermissionGate：查看 allow，修改 ask
      -> [+] list/read：只读查看，不返回或存储为计划证据
      -> [+] remember/revise/forget：possible，预留 generation
      -> [+] MemoryStore：重读、校验、加锁、原子替换、目录同步
      -> [+] 工作区 SHA-256 文件：跨任务、跨进程可读
      [B] 不做检索排序、自动提炼、References 或远程同步
```

## 上一版的问题

`ContextManager` 的历史属于当前对话，可能被裁剪；`AgentState` 记录当前任务的 Plan、工具事实和 verification，不能把用户知识长期塞进去；session 只在用户开启 `/save` 后保存恢复任务所需的安全点。把三者混用会造成两个风险：旧资料被误当成当前指令，或者一次普通对话隐式改变长期数据。

因此 v0.40 把“长期资料”单独放在工作区记忆文件里，并要求模型显式调用工具。Memory 的 `source` 只是用户或 Agent 提供的来源说明，不代表已经独立验证；需要当前事实时仍要读取文件、运行检查或询问用户。

## 关键流程

### 1. 只读查看

模型先调用 `list_memories(limit=20, offset=0)` 查看摘要。结果包含 `total` 和 `next_offset`；当 `next_offset` 不是 `null` 时，继续用它请求下一页。每页只包含 `memory_id`、`revision`、标题、标签、来源和时间，不包含正文；确定目标后再调用 `read_memory(memory_id)`。这样日常浏览不会把所有旧正文塞进上下文，也不会因为记录超过 20 条而永远看不到后面的记录。

### 2. 显式修改

`remember` 新建记录，`revise_memory` 用 `memory_id + expected_revision` 修订，`forget_memory` 用同样的预期 revision 删除。新增、修订、遗忘都是 `effect_class="possible"`：即使写入的只是文字，也会沿用既有 PermissionGate、generation 预留和验证失效规则。

授权提示会显示完整的、已经受工具 schema 限制的拟写内容，以及目标 ID 和 revision。用户可以选择 `once`、`always` 或 `reject`。权限通过后，Executor 在 `/save` 开启时先提交 `handler_admitted`，再进入 Memory handler；State、`role=tool` 和 boundary 按模型调用顺序提交后，才允许下一次 LLM 请求。

### 3. 工作区文件提交

每个规范化真实工作区路径都会得到一个 SHA-256 键。默认文件类似下面的结构；文件名不包含工作区路径明文：

```json
{
  "schema_version": 1,
  "memories": [
    {
      "memory_id": "随机生成的 ID",
      "revision": 1,
      "title": "测试入口",
      "body": "PYTHONPATH=src python -m pytest -q",
      "tags": ["testing"],
      "source": "用户确认",
      "created_at": "2026-09-20T00:00:00Z",
      "updated_at": "2026-09-20T00:00:00Z"
    }
  ]
}
```

单个工作区最多 256 条记录；标题最多 120 字符，正文最多 2000 字符，最多 8 个标签且每个最多 32 字符，来源最多 240 字符，整个 JSON 不超过 1 MiB。目录权限是 `0700`，文件权限是 `0600`。`MEMORY_DIR` 会按真实路径检查，不能位于当前工作区内（包括指向工作区的符号链接），否则子代理的工作区只读工具可能绕过 Memory 工具边界读取正文；工作区外的自定义目录仍可用。缺失文件代表空集合；损坏 JSON、未知 schema 或超限文件会明确报错，绝不用空集合覆盖它。

## 实现拆解

### `MemoryStore` 的并发不变量

修改者先创建工作区专属的独占 lock 文件，并最多等待 2 秒。拿到锁后再次读取磁盘、校验整个集合、执行修改、把完整 JSON 写入同目录临时文件并 `fsync`，再用 `os.replace` 原子替换，最后同步目录。每个写入者都重新读取，所以两个进程不会把对方刚提交的记录静默覆盖。

如果临时写入或替换失败，旧文件保持不变。如果替换已经发生但目录同步失败，Store 返回独立的 `memory_commit_uncertain` 错误，State 将它记录为不可直接重试的未知结果，并停止当前进程后续写入；只读 `list`/`read` 仍可用来核查磁盘上的当前内容。恢复提示也会说明文件可能已经替换，用户应先核查再逐项处理，不能把它当成确定未写入。遗留 lock 不会被自动抢占，避免两个进程同时认为自己拥有写权限。

### 工具和父子边界

无状态的模块级 smoke-test registry 保持 v0.39 的工具集合。只有 `create_registry(state=...)` 才绑定工作区 MemoryStore 并注册五个工具。子代理仍从父 registry 得到四个固定只读工具 `calculate`、`read_file`、`list_dir`、`grep`，所以不会继承 Memory，也不能替父任务写入长期资料。

Memory 正文不会出现在 State 的参数摘要或普通终端进度行中；工具调用参数仍然属于模型历史。开启 `/save` 后，单次记忆调用也可能随 Context 保存在 session 中，因此本版只保证“不复制整份 Memory 快照到 session”，不承诺 session 绝无某次记忆正文。

### revision 冲突与恢复

假设两个调用都看到了 revision 3：第一个调用成功提交后变成 revision 4，第二个调用仍携带 `expected_revision=3`，它会在重读后的校验阶段失败且不写盘。这个检查是乐观并发控制，解决的是旧调用覆盖新内容的问题。

如果 Memory 文件已经替换，但父工具结果还没提交，schema 3 恢复仍按 v0.33 处理：源 session 保持只读，调用被标记为不确定，恢复不会自动重放 Memory handler。用户可以用只读工具查看当前记录，再逐项 `/resolve`；Memory 文件本身不回滚。

## 为什么这样设计

本版选本地 JSON 和标准库，是因为记忆规模有明确上限，而且用户可以直接备份、检查和迁移文件。按工作区真实路径派生文件键能隔离项目；随机 ID 让修订和遗忘不依赖标题，`revision` 则让旧模型回合不能覆盖新事实。锁和同目录原子替换把并发丢更新和半写文件风险压在存储层，而不是交给模型自行判断。

代价也很明确：列表不是相关性检索，模型必须先看摘要再按 ID 读取；`source` 没有自动验证；文件损坏需要人工处理；目录同步失败后当前进程不能继续写入。v0.40 刻意不做自动提炼、自动注入上下文、References、远程同步和文件来源变化检测。

## 运行与观察

本课的重点是观察边界，而不是让读者配置真实模型。可以先运行单课测试：

```bash
PYTHONPATH=src python -m pytest -q tests/test_memory_v040.py
```

预期看到全部测试通过。其中并发测试证明多个 Store 实例的记录不会互相覆盖；分页测试证明第 21 条记录可以从第二页找回；路径测试证明工作区内目录及其符号链接目标会被拒绝；原子写失败测试证明旧文件仍在；目录同步失败测试证明工具结果和 State 都保留 `memory_commit_uncertain`，只读核查仍可用但后续写入被停止；恢复测试证明父结果未提交时会派生 session、标记不确定且不重放 handler；registry 测试证明子代理过滤视图没有 Memory 工具。

## 本版特性、下一课与代码索引

本版完成了跨任务、跨进程的显式工作区记忆 CRUD，以及权限、并发、容量和恢复边界。下一课 v0.41 才会讨论相关性检索和有限的上下文候选选择；v0.42 再讨论具名本地 References。

- [`memory.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.40/src/mini_agent/memory.py)：schema、路径键、锁和原子提交。
- [`tools/memory.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.40/src/mini_agent/tools/memory.py)：五个父 Agent 工具和有界 JSON 结果。
- [`tools/__init__.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.40/src/mini_agent/tools/__init__.py)：父 registry 绑定点和子代理隔离入口。
- [`test_memory_v040.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.40/tests/test_memory_v040.py)：本版存储与注册边界的可执行索引。
