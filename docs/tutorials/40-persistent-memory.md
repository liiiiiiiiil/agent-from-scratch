# 第 40 课：给 Agent 一份可控的长期备忘录

上一课：[把子代理结果安全地交给父 Agent](39-durable-delegation.md) · [教程总览](README.md) · 下一课：[相关记忆检索](41-memory-retrieval.md)

> 代码快照：`v0.40` · 相邻差异：`v0.39..v0.40` · 命令环境：Bash/zsh
>
> `v0.40` tag 由仓库所有者手动创建；本课不代为创建或推送 tag。

## 本课目标

先想一个很实际的场景：你告诉 Agent“这个项目运行测试要用
`PYTHONPATH=src python -m pytest -q`”。如果这句话只留在当前对话里，下一次任务
Agent 可能又要问一遍；如果它未经允许就改写项目文件，又可能污染源码。

本课给 Agent 增加一份独立的、可以跨任务使用的工作区备忘录。这里的“工作区”就是
当前项目目录；“记忆”就是一条由用户或 Agent 明确保存的项目资料。它不是自动学习，
也不是一组更高优先级的指令。

读完本课，你应能回答：

- 当前对话、任务状态、session 和长期记忆分别负责什么；
- Agent 如何列出、读取、新增、修订和遗忘记忆；
- 为什么写入必须询问权限，为什么修订要携带 `revision`；
- 如果文件已经改好但进程在返回结果前崩溃，为什么系统不自动重做这次写入。

## 前置条件

需要基础 Python、Bash/zsh 和 Git 知识。第 39 课有助于理解“工具结果如何安全地
提交”，但不需要真实 API key；本课主要通过源码和离线测试理解行为。

下面的命令先切到本课代码，再查看相邻版本的规模。最后一条命令切回你原来的分支；
如果工作区有未提交修改，请先确认它们不会被版本切换影响。

```bash
git checkout v0.39
git diff --stat v0.39..v0.40
git checkout v0.40
```

## 上一版的问题：对话不是长期资料库

Agent 是“语言模型 + 工具 + 一段运行流程”：模型提出下一步，程序执行工具，工具
结果再回到模型。上一版已经能执行工具，但这条对话历史属于当前任务，可能被压缩；
`AgentState`（当前任务账本）记录计划、工具事实和验证状态，也会随着新任务重新建立；
`/save` session（恢复文件）保存的是一次任务的恢复信息，不是项目知识库。

把长期资料塞进其中任何一个位置都会混淆职责：旧资料可能被误认为当前指令，普通对话
也可能在用户没有明确同意时改变持久数据。v0.40 的解决办法很简单：长期资料单独存放，
只有父 Agent 明确调用记忆工具时才会变化。

## 本版新增什么

本版建立的是一条清晰的通路：

```text
父 Agent 明确提出记忆操作
        ↓
参数检查 → PermissionGate（权限闸门）→ MemoryStore（记忆存储）
        ↓                         ↓
   用户可审阅                 工作区专属 JSON 文件
```

“权限闸门”是程序在真正执行工具前检查“是否允许这样做”的位置；“记忆存储”负责
文件格式、并发写入和恢复边界。模型不能绕过工具直接改文件，子代理也不会得到这些
工具。

对应的主要代码如下。先看问题和流程，再看文件名，读者就能知道每个模块为什么存在。

## 新增与改动文件

| 文件 | 负责什么 |
|---|---|
| [`memory.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.40/src/mini_agent/memory.py) | 以工作区为单位读取、校验和安全替换 JSON 文件。 |
| [`tools/memory.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.40/src/mini_agent/tools/memory.py) | 提供五个父 Agent 工具。 |
| [`tools/__init__.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.40/src/mini_agent/tools/__init__.py) | 只给父 Runtime 绑定 MemoryStore；子代理看不到它。 |
| [`permission.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.40/src/mini_agent/permission.py) | 让查看与修改遵守已有的权限规则。 |

## 版本变更定位

图中的 `[旧]` 是 v0.39 已有能力，`[+]` 是本课新增，`[~]` 是本课修改，`[C]`
表示主要使用者，`[B]` 表示本课边界。

上一版可以执行工具，但没有跨任务的长期资料入口：

```text
[旧][C] 父 Agent Runtime
      ├─→ [旧] ToolExecutor → 工具结果回到当前对话
      ├─→ [旧] AgentState / Context → 当前任务的账本和消息视图
      └─→ [旧] /save session → 可选的任务恢复文件
          [B] 没有独立的长期记忆
```

本版把长期资料接在父侧工具入口上，而不是把它混入 State 或 session：

```text
[C] 父 Agent Runtime
      └─→ [~] PermissionGate
            ├─→ [+] list_memories / read_memory（只读）
            └─→ [+] remember / revise_memory / forget_memory
                       │ 可能改变磁盘，需权限和 generation
                       ▼
                 [+] MemoryStore
                       ├─→ 重新读取并校验
                       ├─→ 加工作区锁、原子替换、同步目录
                       └─→ 工作区专属 JSON 文件

[B] 不自动从对话提炼；不做相关性检索；不提供 References；不加入子代理白名单。
```

## 关键流程

### 1. 先看摘要，再按 ID 读取正文

`list_memories(limit=20, offset=0)` 只返回记忆的编号、标题、标签、来源和时间，
不会返回正文。记录超过一页时，结果中的 `next_offset` 告诉模型从哪里继续；找到目标
后再调用 `read_memory(memory_id)`。

这一步有两个好处：浏览很多记录时不会把所有正文塞进上下文，也不会因为第一批记录
太多而找不到后面的记录。记忆里的 `source` 只是用户填写的来源说明，返回
`source_status="unverified"`；它不能证明文件仍然存在或内容仍然正确。

### 2. 新增、修订和遗忘都要明确提出

五个工具可以按下面的方式理解：

| 工具 | 作用 | 是否可能改变持久数据 |
|---|---|---|
| `list_memories` | 分页查看摘要 | 否 |
| `read_memory` | 读取一条完整记忆 | 否 |
| `remember` | 新增一条记忆 | 是 |
| `revise_memory` | 修订已有记忆 | 是 |
| `forget_memory` | 删除一条记忆 | 是 |

后三个工具即使只是写文字，也被标记为 `effect_class="possible"`，意思是“可能
产生副作用”。PermissionGate 会展示拟写的标题、正文、来源、目标 ID 和版本号，
用户可以选择 `once`、`always` 或 `reject`。拒绝后不会进入 handler（真正改文件的
函数）。开启 `/save` 时，系统还要先提交 `handler_admitted`，再执行 handler；本轮
每个工具结果按模型顺序提交完成后，才允许请求下一轮模型。

### 3. 一个工作区对应一个记忆集合

默认情况下，记忆文件位于用户本地的 `~/.mini_agent/memory`，文件名由工作区的真实
路径计算出 SHA-256 键，因此文件名不会直接暴露项目路径。一个最小的记录看起来像
这样：

```json
{
  "schema_version": 1,
  "memories": [
    {
      "memory_id": "稳定的随机 ID",
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

`revision` 可以理解成这条记录的版本号。修改时必须带上“我预计它还是第几版”，
如果磁盘上已经被别人改成了下一版，旧调用就会失败，而不会覆盖新内容。这是乐观
并发控制：先假定没有冲突，提交时再检查。

## 实现拆解

### 1. 写入不会直接覆盖旧文件

`MemoryStore` 写入时会先取得当前工作区的独占锁，再重新读取磁盘上的完整集合；随后
校验记录、写入同目录临时文件并 `fsync`，最后用 `os.replace` 原子替换旧文件并同步
目录。这样两个进程同时保存时，后写入者不会悄悄抹掉前一个进程刚保存的记录。

文件缺失代表“还没有记忆”。损坏 JSON、未知 schema 或超限文件会明确报错，不会被
当成空集合覆盖。默认限制包括：最多 256 条记录、正文最多 2000 字符、整个 JSON
最多 1 MiB；存储目录权限为 `0700`，文件权限为 `0600`。自定义 `MEMORY_DIR` 也必须
位于工作区之外（包括符号链接指向工作区的情况）。

### 2. 子代理只做调查，不接管长期记忆

父 Runtime 的 registry 在绑定 `AgentState` 后才创建 MemoryStore 并注册记忆工具。子
代理使用固定的四个只读工具：`calculate`、`read_file`、`list_dir`、`grep`。因此子
代理不能替父任务保存、修订或删除长期资料，也不能把记忆写入伪装成普通调查。

### 3. 崩溃恢复保守地处理“不确定”

一次修改可能已经替换了记忆文件，但还没来得及把父 Agent 的工具结果写入 session。
这时系统知道“调用已经获准”，却不能确定调用者是否收到了成功结果。v0.40 沿用既有
schema 3 工具边界：源 session 保持只读，恢复会把该调用标成不确定，不自动重放
handler。用户可以先用只读工具核查当前内容，再逐项 `/resolve`；记忆文件不会因为
恢复而自动回滚。

## 为什么这样设计

本版选择小型 JSON 文件和标准库，是因为记忆集合有明确上限，用户可以直接备份、检查
和迁移。把 Memory 独立于 State、Context 和 session，能同时满足“跨任务保存”和
“不把旧资料伪装成当前事实”两点。稳定 ID 让修订不依赖标题，`revision`、锁和原子
替换则把并发与半写文件风险放在存储层处理。

代价同样明确：v0.40 只有分页，没有相关性搜索；来源文本不自动验证；写入失败或目录
同步失败需要核查；开启 `/save` 时某次记忆调用正文仍可能随普通工具历史进入 session。
本版刻意不做自动提炼、自动注入上下文、远程同步和工作区外资料引用。

## 运行与观察

本课的观察重点不是“记住了一句话”，而是确认记忆写入确实经过边界控制。运行：

```bash
PYTHONPATH=src python -m pytest -q tests/test_memory_v040.py
```

你应看到测试通过，并能从行为上理解这些结果：分页能找到第 21 条记录；两个 Store
实例不会互相覆盖；旧调用的 `revision` 冲突不会写盘；原子写失败会保留旧文件；
父结果尚未提交时恢复不会重放 handler；子代理的过滤视图没有 Memory 工具。

## 本版特性、下一课与代码索引

v0.40 完成了跨任务、跨进程的显式记忆查看、新增、修订和遗忘。下一课会回答另一个
问题：记忆多起来以后，Agent 怎样只找回与当前任务相关的少量内容？请继续阅读
[第 41 课：相关记忆检索与有界上下文](41-memory-retrieval.md)。

- [`memory.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.40/src/mini_agent/memory.py)：schema、工作区隔离、锁和原子提交。
- [`tools/memory.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.40/src/mini_agent/tools/memory.py)：五个父 Agent 工具。
- [`tools/__init__.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.40/src/mini_agent/tools/__init__.py)：父 registry 绑定点和子代理隔离入口。
- [`test_memory_v040.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.40/tests/test_memory_v040.py)：本课行为边界的可执行索引。
