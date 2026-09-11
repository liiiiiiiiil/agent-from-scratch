# 第 19 课：单文件检查点与回滚（Checkpoint / Rollback，v0.19）

上一课：[受限恢复策略](18-recovery-policy.md) · [教程总览](README.md) · 下一课：按需追加

> 代码快照：`v0.19` · 相邻差异：`v0.18.1..v0.19` · 命令环境：Bash/zsh

> 运行要求：Python 3.10+，核心运行时只使用标准库。

## 本课目标

上一课已经能在失败后 retry、adjust、ask 或 block，但它只能决定“下一步做什么”，不能撤销已经发生的文件写入。例如，agent 写完文件后验证失败：再次调用工具可能继续改变文件，直接停止又会把失败后的文件留在工作区。v0.19 解决的是其中一个很窄、但可审计的问题：只对一次获准的 `write_file` 或 `edit_file` 保存一个单文件前镜像，在文件没有被外部改动时才恢复它。

这里的“检查点”（checkpoint）是一次文件操作前后留下的、可用于判断能否恢复的记录；“回滚”（rollback）是根据这条记录把文件恢复到操作前的状态。它不是工作区快照，也不是多文件事务。

读完本课，你应能解释：

- 普通文件的前镜像为什么保存原始 bytes、mode 和 SHA-256，而不存在的文件为什么用 `absent` tombstone（明确表示“原本不存在”）；
- 符号链接、工作区外路径、目录、特殊文件、无效父目录、读取失败和超过 1 MiB 的前镜像为什么只能得到 `unavailable`，却不能阻止原来的文件写入；
- 检查点 ID、相对路径和哈希为什么同时出现在文件工具结果与 critical Structured State 中，并在上下文压缩和超长状态降级后仍可见；
- 文件 handler 抛错时，前后镜像明确为何仍可恢复，而镜像不可用时为何按未知副作用阻塞；
- 回滚为什么必须先做检查点因果检查和额度预留，再用保存的路径经过同一个权限闸门，并在恢复前检查当前后镜像是否冲突；
- 为什么 `rollback_checkpoint` 不能成为 retry/adjust 的目标，合法的原始 mode `0` 也必须原样恢复；
- 为什么恢复成功仍会打开新的 generation（代次），并要求下一轮独立 verification（验证）才能完成任务。

## 上一版的问题

v0.18.1 已把失败记录成 `FailureEvent`，也把恢复请求记录成 `RecoveryAction`。`generation` 是验证证据的边界：恢复动作之后，旧验证不能继续证明新状态。但 v0.18.1 明确拒绝 `rollback`，所以它没有保存“写入前是什么”，也不能安全地撤销文件、shell、网络或其他外部副作用。

本课只增加文件工具的一条受限路径：在 handler（真正执行文件操作的函数）前捕获前镜像，在 handler 返回或抛错后捕获后镜像；只有两者都能明确描述，并且当前文件仍与后镜像一致时，才允许恢复。这样“可以回滚”的范围从一开始就有明确边界。

## 前置条件

建议先阅读第 18 课，并先理解这些概念的直观含义：`AgentState` 保存任务事实，`ExecutionAttempt` 表示一次工具尝试，`FailureEvent` 表示一次失败，`RecoveryAction` 表示一次受控恢复申请，`PermissionGate` 在 handler 运行前决定是否获准。

以下命令适用于 Bash/zsh。先切到相邻基线查看文件规模，再切回本课的 tag；`git diff --stat` 只回答“本版改了哪些文件、规模多大”，后文再解释调用顺序。

```bash
git checkout v0.18.1
git diff --stat v0.18.1..v0.19
git checkout v0.19
```

## 新增与改动文件

| 文件 | 变化 | 读者应关注什么 |
|---|---|---|
| `src/mini_agent/checkpoint.py` | 新增 | `CheckpointStore` 保存任务内的前镜像 bytes；`FileCheckpoint` 只暴露元数据；恢复 handler 做冲突检查和原子恢复 |
| `src/mini_agent/state.py` | 修改 | State 保存检查点元数据，校验 rollback 的因果关系，扩展 rollback action、内部 attempt 和 reset 边界 |
| `src/mini_agent/recovery.py` | 修改 | 校验 rollback 请求，按“预留额度 → 授权 → 打开 successor generation”执行，并调用内部工具 |
| `src/mini_agent/tools/base.py` | 修改 | 在文件 handler 前后捕获镜像，把 checkpoint ID 放入文件结果和执行事实 |
| `src/mini_agent/tools/__init__.py` | 修改 | 为同一个任务的 State、Executor 和 RecoveryRuntime 绑定同一个 store，并注册内部恢复工具 |
| `src/mini_agent/permission.py` | 修改 | 为 `rollback_checkpoint` 按目标路径提取权限匹配值，默认 action 为 ask |
| `tests/test_checkpoint_rollback.py` | 新增 | 用现有文件、新文件、冲突、边界、中断和状态压缩场景固定本版行为 |

## 版本变更定位

这一版跨越 State、恢复调度、工具执行和权限边界。下面先画 v0.18.1 的入口和收口，再画 v0.19 把检查点插入哪里；这样读者可以把“新增能力”与“原有恢复协议”区分开。

图例：`[旧]` v0.18.1 已有，`[+]` v0.19 新增，`[~]` v0.19 修改，`[C]` 主要消费者，`[B]` 本版边界。

```text
v0.18.1 基线：
[旧] write_file/edit_file
  -> [旧] ToolExecutor -> handler -> ExecutionResult
  -> [旧] AgentState.record_execution_result()
  -> ExecutionAttempt / FailureEvent / generation
  -> recover(retry|adjust|ask|block)
       ├─retry/adjust -> PermissionGate -> 工具 handler
       └─ask/block -> state-only generation -> blocked
  -> 下一轮独立 verification
  -> [C] Structured State
  -> [B] 没有文件前镜像，明确拒绝 rollback
```

```text
v0.19 变更：
[旧] write_file/edit_file
  -> [~] ToolExecutor
       -> [+] CheckpointStore.capture_before()
            -> bytes + mode + SHA-256，或 absent
            -> [B] unavailable 不改变原 handler 行为
       -> handler 返回或抛错
       -> [+] CheckpointStore.capture_after()：类型 + SHA-256
       -> [C] 文件结果 + critical Structured State：checkpoint 元数据
  -> verification failure
  -> [~] recover(action=rollback, checkpoint_id)
       -> [+] 因果校验 + rollback fingerprint 额度预留
       -> [~] PermissionGate：使用 checkpoint 保存的相对路径
       -> [+] successor generation + internal attempt
       -> [+] rollback_checkpoint（内部工具，模型不可见）
            -> [+] 当前 lstat/hash
                 ├─不匹配 -> rollback_conflict -> blocked
                 ├─恢复失败 -> rollback_restore_failed -> blocked
                 └─匹配 -> 原子恢复 -> checkpoint=restored
  -> 下一轮独立 verification
  -> [B] 仍不回滚 shell、网络、多文件或未知副作用
```

基线图中的 `recover` 仍是本版入口；v0.19 只新增 `rollback` 分支和它需要的检查点。变更图中的 `unavailable` 分支故意回到原文件 handler：它表示“没有可回滚凭证”，不是“拒绝这次原写入”。

## 关键流程

先把一次写入想成三段事实：前镜像是 handler 运行前看到的文件，后镜像是 handler 返回或抛错后重新观察到的文件，检查点是把两段观察和恢复元数据放在一起的任务内记录。只有后镜像仍能与检查点对上，回滚才不会覆盖 agent 之外的新修改。

```text
获准 write/edit
  -> reserve attempt + possible-effect generation
  -> capture before image
       ├─regular file：bytes/mode/SHA-256
       ├─absent：tombstone
       └─其他情况：status=unavailable
  -> handler returns or raises
  -> capture after image：type/SHA-256
  -> file result + critical Structured State：checkpoint metadata
       ├─ready -> rollback candidate visible
       ├─handler exception + ready -> 保持 running，给出 rollback notice
       ├─handler exception + unavailable/uncertain -> 未知副作用，进入 blocked
       └─handler success + unavailable -> 保留成功结果，但没有 rollback candidate
  -> verification failure
  -> recover(rollback, checkpoint_id)
  -> validate 本任务、ready、且不晚于目标 failure 的 checkpoint
  -> reserve rollback quota
  -> PermissionGate(path from checkpoint)
       ├─拒绝 -> release 未使用额度，不打开 generation
       └─通过 -> open successor generation + internal attempt
  -> lstat/hash 当前后镜像
       ├─mismatch -> rollback_conflict，拒绝写入并 blocked
       ├─restore error -> rollback_restore_failed 并 blocked
       └─match -> atomic restore，checkpoint=restored
  -> role=tool 结果回灌
  -> 下一轮独立 verification
```

观察这条链时，关键现象不是“模型说回滚成功”，而是：文件工具结果带有 `checkpoint_id`；State 中出现 `status=ready` 和 `rollback_checkpoints`；恢复执行形成一个新的 generation 和 `rollback_checkpoint` attempt；恢复后 Structured State 明确要求 verification。冲突时文件应保持外部版本，而 State 应进入 `blocked`。

## 实现拆解

### 1. 前镜像、后镜像和可见元数据

`create_registry(state, workspace_root=None)` 为一个任务绑定同一个 `CheckpointStore` 到 State、Executor 和 RecoveryRuntime。store 把原始 bytes 和绝对路径留在任务私有内存中；对外的 `FileCheckpoint` 只展示 JSON-safe 元数据。下面的片段用于认识 State 和工具结果会看到的形状，不是完整实现：

```json
{
  "checkpoint_id": "cp-1",
  "attempt_id": "a-1",
  "generation_id": 1,
  "path": "src/app.py",
  "before_type": "regular_file",
  "before_sha256": "…",
  "after_type": "regular_file",
  "after_sha256": "…",
  "mode": 420,
  "status": "ready",
  "unavailable_reason": null
}
```

这个形状说明三件事：`path` 是工作区相对路径，不泄露绝对根目录；哈希用于确认镜像，不把文件内容放进上下文；`status=ready` 才表示可以成为 rollback 候选。已有文件的 bytes 只留在 store 中，恢复时重新写入；新文件的前镜像是 `absent`，恢复时的目标是删除这次创建的文件。前镜像大小恰好等于 `MAX_CHECKPOINT_BYTES`（默认 1 MiB）时允许，超过上限时为 `unavailable`。

路径检查以任务创建 registry 时使用的工作区根为界：绝对路径规范化后仍在根内时展示为相对路径，`..` 会先规范化；工作区外路径、任意已有符号链接组件、目录或特殊文件、无效父目录和读取失败都不会捕获 bytes。捕获不可用不会改变原 `write_file`/`edit_file` handler 的准入、执行和结果。

前镜像保存原始 mode，恢复时按显式的 `None` 判断。因此 mode `0` 是合法值，不会被误当成缺省的 `0644`。后镜像只用于并发变更判断，以类型和 digest 为准；它不需要保存后文件内容。

文件工具结果会附带不含内容的通知，例如 `checkpoint_id=cp-1; path=src/app.py; status=ready`。State 的 `snapshot()` 同时提供 `checkpoints` 和当前可用的 `rollback_checkpoints`；Context 会把 ID、相对路径、attempt、generation、状态和哈希放进 critical Structured State。压缩或超长状态降级时，这组恢复信息仍优先保留。

### 2. rollback 请求先校验，再授权

下面这个差异只展示协议入口：它告诉模型 rollback 需要哪一个检查点，但不把内部恢复工具直接暴露给模型。完整 schema 还保留 `additionalProperties=False`，运行时会做最终的 action-specific 校验。

```diff
 parameters = {
-    "action": {"enum": ["retry", "adjust", "ask", "block"]},
+    "action": {"enum": ["retry", "adjust", "ask", "block", "rollback"]},
+    "checkpoint_id": {"type": "string"},
 }
```

因此 rollback 不能携带 `requested_attempt`、`requested_tool` 或 `requested_arguments`；其他 action 携带 `checkpoint_id` 也会被拒绝。Runtime 只接受当前任务、状态为 `ready`、且创建 attempt 不晚于目标 failure 的检查点。缺失、不可用、已恢复或因果顺序不合法时，系统记录一次 `status="rejected"` 的 RecoveryAction，保留检查点，进入 `blocked`，且不打开新 generation。

授权目标来自检查点保存的规范化相对路径，不来自模型临时提交的参数。Runtime 先在 State 中预留 rollback fingerprint 额度，再调用同一个 `PermissionGate` 的 `rollback_checkpoint` 路径规则；权限被拒绝时释放尚未使用的内部执行额度，也不推进 generation。只有授权通过，才打开后继 generation 并预留内部 attempt。

这条顺序是本版的重要不变量：无效请求不会产生空 generation，未获准的恢复不会绕过权限边界，模型也不能借 rollback 自己换目标路径或内容。

### 3. 内部工具只负责执行事实

`rollback_checkpoint` 会注册到任务 registry，但 `ToolRegistry.schemas()` 会过滤 `internal=True` 的工具，所以它不会出现在 LLM schema 中。即使模型伪造调用，普通 Executor 也会返回 `internal_tool`，不运行 handler。只有 RecoveryRuntime 在已完成检查、额度预留和权限授权后，才通过内部执行入口调用它。

内部调用仍然形成完整的 `ExecutionAttempt`，包括 `effect_class="possible"`、权限结果、generation、recovery 和 failure 因果链接。这样回滚不是一条无法审计的旁路；它只是模型不能直接选择的执行入口。

handler 执行前会再次 `lstat` 并计算当前 digest：

- 普通文件前镜像：在同一父目录创建临时文件，写入私有 bytes，恢复原 mode，再用 `os.replace` 原子替换目标；
- `absent` 前镜像：只有当前仍是与检查点后镜像匹配的普通文件时才 `unlink`；
- 类型或 digest 不同：不写入、不覆盖外部内容，生成 `rollback_conflict` failure 并阻塞；
- 临时文件、chmod、replace 或 unlink 失败：清理临时文件，检查点标为 `restore_failed`，生成 `rollback_restore_failed` 并阻塞。

因此 rollback 的结果要按 State 事实阅读：恢复 action 的结果会回灌为工具结果，但“action 已执行”不等于“任务已修复”；冲突或恢复错误还会在 attempt/failure 和终态原因中体现。

所有 internal 工具都不能被 `retry` 或 `adjust` 选为恢复目标。Runtime 会在预留恢复额度、检查权限和激活 successor generation 之前拒绝这类请求，并记录一次 `rejected` RecoveryAction；这样不会打开空 generation，也不会留下没有结果 attempt 的 `reserved` 动作。

### 4. handler 抛错时的恢复边界

文件 handler 可能在已经改动一部分文件后才抛错。Executor 因此在异常路径也尝试捕获后镜像：如果前后镜像都明确，State 把 failure 归为可继续诊断的 unknown failure，保持 `running`，并在 Structured State 发出带 checkpoint ID 的 rollback notice。模型可以先请求 rollback，再做独立 verification。

如果前镜像或后镜像不可用，系统就无法证明应该恢复什么、也无法证明当前内容是不是 agent 刚写的；这时按未知副作用进入 `blocked`。这只描述 handler 已经抛错的路径；一次普通成功写入拿到 `unavailable` 检查点时，原写入仍然成功，只是没有 rollback 凭证。

### 5. 恢复之后仍必须验证

成功恢复会把检查点标记为 `restored`，把 RecoveryAction 标为 `executed` 并关联内部 attempt。这只证明恢复系统完成了它承诺的文件操作；State 同时清除旧 verification evidence，把 `verification_required` 置为真。下一轮 LLM 回合必须调用独立的 `run_shell(purpose="verification")`，只有当前 generation 的成功证据才能重新满足完成条件。

恢复不会回退 generation，也不会复用旧 verification。即使 bytes 已恢复，下一轮验证仍是判断“当前任务是否正确”的唯一新证据入口。

## 为什么这样设计

检查点采用“单文件前镜像 + 后镜像指纹”，而不是工作区快照：

- 原始 bytes 放在任务私有 store，可以支持恢复，又避免 Context、日志和工具结果泄露文件内容；
- 固定 registry 创建时的工作区根，避免任务执行过程中权限作用域漂移；
- type + SHA-256 比只比较 mtime 更能发现外部修改、类型替换以及 absent/regular-file 变化；
- 同目录临时文件加原子 replace，避免恢复过程留下半写入目标；冲突时保守阻塞，避免覆盖用户在 agent 之外的新内容。

回滚沿用 v0.18.1 的两阶段恢复边界：先预留额度，再授权，最后才推进 generation 和执行。它的代价是一次额外的控制动作和下一轮验证；收益是路径、额度、attempt、failure 和恢复结果都可审计，而且失败时不会静默覆盖外部修改。

本版刻意不把恢复模块变成新的文件执行器：真实恢复仍进入内部 ToolExecutor 边界；也不把恢复结果当完成宣称。这样保留了统一的权限、异常和协议记录，但能力只覆盖能明确保存和比较的单文件镜像。

## 设计边界

- 检查点是进程内、任务级数据，保留到 `/reset` 或 `/new`；不跨进程持久化。
- 只支持一次单文件 `write_file`/`edit_file` 的前镜像；不提供 shell、网络、多文件写入或其他外部副作用的回滚。
- 前镜像恰好达到 1 MiB 上限允许，超过上限、工作区外、符号链接、目录、特殊文件、无效父目录或读取失败得到 `unavailable`；这不会阻止原文件工具调用。
- 只有 `ready` 检查点能参与 rollback；恢复成功后是 `restored`，不能再次恢复。
- 回滚前当前类型或 digest 不匹配就进入 `rollback_conflict` 和 `blocked`，不覆盖外部内容；恢复系统错误则进入 `rollback_restore_failed` 和 `blocked`。
- `rollback_checkpoint` 不出现在模型 schema 中，模型不能直接调用，也不能把它作为 retry/adjust 目标；恢复不会自动提升权限。
- 恢复不回退 generation、不复用旧 verification，也不实现 Repair Loop 调度或 Trace & Replay；本版只产生未来回放可能需要的 checkpoint、attempt、failure 和 recovery 因果事实。

## 本版特性、下一课与代码索引

本版把 v0.18.1 的受限恢复扩展为可审计的单文件 checkpoint/rollback：文件结果和 Structured State 会告诉模型哪些检查点可用，RecoveryRuntime 决定是否有资格恢复，内部 handler 在冲突时拒绝覆盖，恢复后仍必须独立验证。

下一课按路线图继续扩展失败恢复或回放能力；不要把本版检查点当作多文件事务或通用沙箱。

- [checkpoint.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.19/src/mini_agent/checkpoint.py)
- [state.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.19/src/mini_agent/state.py)
- [recovery.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.19/src/mini_agent/recovery.py)
- [tools/base.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.19/src/mini_agent/tools/base.py)
- [tools/__init__.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.19/src/mini_agent/tools/__init__.py)
- [permission.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.19/src/mini_agent/permission.py)
- [test_checkpoint_rollback.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.19/tests/test_checkpoint_rollback.py)
