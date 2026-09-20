# 第 39 课：把子代理结果安全地交给父 Agent

上一课：[让多个只读子代理有界并行](38-parallel-delegation.md) · [教程总览](README.md) · 下一课：阶段十一（规划中）

> 代码快照：`v0.39` · 相邻差异：`v0.38..v0.39` · 命令环境：Bash/zsh

> 运行要求：Python 3.10+。本课使用本地 session 和测试代码理解恢复边界，不需要真实 API key。

## 本课目标

第 38 课中，子代理可以先完成，结果暂时留在当前进程的内存里，等待父线程按 A、B、C 顺序交付。问题是：如果 B 已经得到一个可信结果，但父进程还没来得及把它写入父 Context 就崩溃了，下一次进程既不能假定 B 成功，也不能安全地重跑 B。

本课把“结果产生”和“结果交付”分开保存。读完后，你应能解释：

- `result_ready` 和 `committed` 分别表示什么；
- session 为什么要保存结果原文和 hash，而不是只保存“完成”标签；
- 恢复为什么可以交付原结果，却不能重新调用子模型；
- 没有持久结果原文时，为什么必须把调用交给用户处理。

## 上一版的问题

v0.38 的调度器可以把 B、C 的完成结果暂存在内存，等 A 完成后按顺序写入父 Context。这个设计能保证正常运行时的消息顺序，却留下一个跨进程窗口：内存消失后，session 可能只知道某个调用已经准入或正在运行。

“没有结果”不等于“没有执行”。子 LLM 可能已经消耗额度，旧请求也可能已经返回但尚未落盘。自动重跑会产生新的调用和新的调查事实，甚至重复费用。因此本版保存足以重建父 `role=tool` 消息的受控原文。

## 前置条件与版本切换

需要第 38 课、基础 Python、Bash/zsh 和 Git 知识。下面命令只用于查看代码；命令行首条任务不会暗示程序执行后立即退出。

```bash
git checkout v0.38
git diff --stat v0.38..v0.39
git diff v0.38..v0.39 -- src/mini_agent/session.py src/mini_agent/state.py src/mini_agent/delegation.py src/mini_agent/runtime.py src/mini_agent/resume.py
git checkout v0.39
```

## 新增与改动文件

本版沿着“生成结果 → 保存结果 → 按序交付 → 崩溃恢复”这条主线修改：

| 文件 | 作用 |
|---|---|
| [`src/mini_agent/session.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.39/src/mini_agent/session.py) | 保存有界的待交付结果原文，并提供原子校验和交付入口。 |
| [`src/mini_agent/state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.39/src/mini_agent/state.py) | 记录结果结算、委派状态和恢复时沿用的账本事实。 |
| [`src/mini_agent/delegation.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.39/src/mini_agent/delegation.py) | 分开“子任务完成”回调和“父侧交付”回调。 |
| [`src/mini_agent/runtime.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.39/src/mini_agent/runtime.py) | 接入启动、`result_ready` 和 `committed` 三个时点。 |
| [`src/mini_agent/resume.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.39/src/mini_agent/resume.py) | 从源 session 派生恢复 session，并按原顺序交付结果。 |
| [`src/mini_agent/context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.39/src/mini_agent/context.py) | 保持父侧上下文只显示结果引用和有限生命周期信息。 |

## 版本变更定位

图例：`[旧]` v0.38 已有，`[+]` v0.39 新增，`[~]` v0.39 修改，`[C]` 主要消费者，`[B]` 本课边界。

v0.38 的完成结果停在当前进程：

```text
[旧][C] 父 Runtime
      -> [旧] Scheduler worker
      -> [旧] 内存结果缓冲 B、C
      -> [旧] 等待 A
      -> [旧] 按 A、B、C 写 State / role=tool / boundary
      [B] 进程崩溃后无法从 session 重建内存结果
```

v0.39 在“结果生成”和“父侧交付”之间增加可恢复边界：

```text
[C] 父 Runtime
      -> [~] 保存合同、预留额度和 running
      -> [~] worker 完成并校验结果
      -> [+] State: result_ready
      -> [+] schema 3 保存结果原文、摘要和 hash
      -> [~] 父线程按 A、B、C 交付
      -> [+] State + attempt + role=tool + boundary 原子提交
      -> [C] State: committed -> 下一次父 LLM

active + pending boundary
      -> [C] resume 派生新 session
      -> 按父调用顺序交付已有原文
      -> [B] 不调用子 LLM，不重放旧 worker
```

## 核心概念与数据结构

### 1. `result_ready` 是“结果已经保存”，不是“父已经收到”

`result_ready` 表示子代理已经完成、结果通过合同校验，并且原文已经保存到待交付区域。`committed` 表示父 State、父 `role=tool` 消息和 durable boundary 也已经共同提交。二者之间的窗口正是本课要保护的恢复窗口。

父模型发出 A、B、C 后，B 可以先进入 `result_ready`，但不能跳过 A 直接成为父 history 中的第二条消息。恢复仍按父调用顺序交付。

### 2. session 保存什么

session 不保存子代理完整 history、隐藏提示词、连接或线程。它只保存能够重建父工具结果的有界 JSON，以及调用身份和完整性信息：

```json
{
  "invocation_id": "r-3-c-1",
  "delegation_id": "d-2",
  "result_id": "result-2",
  "result_hash": "<64 个十六进制字符>",
  "result_json": "{...规范化的 SubagentResult...}",
  "result_summary": "发现配置读取路径"
}
```

恢复时，系统会重新序列化 `result_json`，检查是否得到相同的 canonical JSON 和 SHA-256 hash，再核对调用 ID、委派 ID、State 状态、大小上限和父调用顺序。只保存 hash 只能证明原文曾经存在，却不能构造父模型需要的 `role=tool` 内容。

### 3. 完成回调和父交付回调是两个时刻

v0.38 的“完成”回调同时承担了两个责任。v0.39 将其拆成：

```python
def on_result_ready(index, result):
    state.delegation_result_ready(task.delegation_id, result)
    boundary.record_delegation_result_ready(invocation_id, result, state, context)

def on_result(index, result):
    # 这里只接收按父模型顺序轮到的结果
    commit_parent_tool_result(index, result)
```

这样 B 可以先安全落盘，C 也可以继续使用已经释放的并行槽位；但父 Context 仍只看到连续的 A、B、C。

### 4. 恢复交付原文，不重跑子代理

恢复时，已有 `result_ready` 原文会先通过身份、hash、状态和顺序检查，然后直接构造父侧 `role=tool`，并在新的恢复 generation 中完成结算。源 session 保持只读，同一源只能 claim 一次；恢复不会继承旧 PID、stdin 或 verification 资格，也不会重置预算。

如果某个调用只有 `created` 或 `running`，没有持久化原文，系统无法证明它曾经返回过什么。它会被记录为调查中断或不确定事实，生成明确 issue 交给用户，而不是伪造成功或自动重新请求子模型。

## 关键流程

正常路径：

```text
父 assistant: delegate_task A, B, C
  -> admission + 批量预留
  -> 保存 created/running
  -> worker 运行
  -> B/C/A 各自完成并保存 result_ready
  -> 按 A、B、C 交付父 tool result
  -> 每次同时保存 attempt、committed、role=tool 和 boundary
  -> 整轮 committed 后请求父 LLM
```

崩溃路径：

```text
进程在 B result_ready 与父交付之间退出
  -> active + schema 3 pending boundary
  -> resume 派生新 session
  -> 校验并交付已保存的 B 原文
  -> 没有原文的调用生成 issue
  -> 不调用子 LLM、不重放旧 worker
```

## 运行与观察

在启用 `/save` 的本地任务中，观察点不是“session 里有一行完成文字”，而是：子结果产生后先出现 `result_ready` 原文；父侧按顺序提交后，待交付原文才进入 `committed`。如果在两者之间恢复，父 Context 应收到与原文相同的 JSON，子模型调用次数不增加。

命令行首条任务处理后，CLI 仍会进入交互循环；恢复入口只会在当前任务和 session 边界允许时接管，不会自动替旧任务继承进程资源。

## 实现拆解

`DurableToolBoundary.persist_delegation_batch()` 在启动 worker 前保存合同摘要、预留额度和委派引用；`record_delegation_result_ready()` 只接受 State 中已经是 `result_ready` 的结果，并重新计算规范化原文的 hash。

父 Runtime 交付时先构造 `ExecutionResult`，再调用 State 的 `commit_delegation_tool_result()`；随后以当前 Context 和 State 一起做原子 session 提交。若替换失败，Runtime 不会进入下一次父模型请求。提交成功后，pending 原文从 boundary 移除，但结果 ID、hash、摘要和 usage 仍保留给 Trace 查看。

`ResumeCandidate.prepare_resume()` 按 boundary 中的父 call 顺序读取结果，交叉核对源 State；没有 ready 原文的调用沿用第 33 课的 issue 分类。源 session 保持只读，恢复只 claim 一次。

## 为什么这样设计

只保存 hash 无法重建父消息，保存完整结果原文又可能让 session 无限增长，所以本版同时采用 canonical JSON、SHA-256、单条和总字节上限。它保存的是恢复必需的父侧材料，不是完整子代理日志。

把结果产生和父交付拆开，是并行执行与确定性消息顺序之间的折中：结果可以乱序落盘，父 history 仍按调用顺序提交。代价是整轮完成前 session 会保留待交付原文，这段内容不属于 clean safe point。

恢复时交付已有原文、把无原文调用交给用户，是因为自动重跑会产生新的模型调用和新的事实，而且旧调用的真实用量可能未知。保守地保留原预留账本，比假装未消耗更安全。

## 设计边界

- `/save` 仍是启用 session 持久化的唯一入口；不新增后台结果文件或公开工具参数。
- 子代理仍是 depth=1、只读实例，不能写工作区、运行 shell、操作进程或再次委派。
- 只有通过原文、hash、调用身份、State 状态和顺序检查的结果才能直接恢复。
- 旧 session 没有待交付原文时，继续按第 33 课分类为不确定或调查中断事实。
- 子 findings/evidence 仍是父侧调查材料，不进入父 `verification_evidence`，不能替代独立验证。
- Trace 只读取父 State 快照和结果引用，不调用 LLM，也不通过回放猜测缺失事实。

## 本版特性、下一课与代码索引

本课新增了跨进程可恢复的委派结果交付：子结果先持久化为 `result_ready`，父侧再按模型顺序把 State、attempt、`role=tool` 和 durable boundary 一起提交为 `committed`。恢复使用同一份原文，不重跑子代理；没有原文的调用继续由用户决策流程处理。

本阶段到此完成最小的持久委派链路；后续课程不在本课中提前引入可写子代理、递归委派或跨主机 worker。

核心源码：

- [`src/mini_agent/session.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.39/src/mini_agent/session.py)
- [`src/mini_agent/state.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.39/src/mini_agent/state.py)
- [`src/mini_agent/delegation.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.39/src/mini_agent/delegation.py)
- [`src/mini_agent/runtime.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.39/src/mini_agent/runtime.py)
- [`src/mini_agent/resume.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.39/src/mini_agent/resume.py)
