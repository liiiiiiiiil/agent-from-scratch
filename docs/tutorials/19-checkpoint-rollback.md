# 第 19 课：单文件 Checkpoint / Rollback（v0.19）

上一课：[受限恢复策略](18-recovery-policy.md) · [教程总览](README.md) · 下一课：按需追加

> 代码快照：`v0.19` · 相邻差异：`v0.18.1..v0.19` · 命令环境：Bash/zsh

> 运行要求：Python 3.10+，核心运行时只使用标准库。

## 本课目标

第 18 课能在失败后 retry、adjust 或保守停止，但不会撤销已经发生的文件副作用。本课增加一个严格有界的能力：只为一次获准的 `write_file` 或 `edit_file` 保存单文件前镜像，并在后镜像仍未被外部改变时恢复它。

读完本课，你应能解释：

- 为什么 checkpoint 同时保存普通文件的原始字节、mode、SHA-256，以及不存在文件的 `absent` tombstone；
- 为什么符号链接、工作区外路径、目录、特殊文件、无效父目录和超过 1 MiB 的前镜像只能得到 `unavailable`，却不能阻止原写入；
- 为什么 checkpoint ID 和相对路径等元数据会同时出现在文件工具结果与 critical Structured State 中，并在上下文压缩和超长状态降级后继续可见；
- 为什么文件 handler 抛错但前后镜像仍明确时，任务保持可恢复并提示 rollback，而前镜像或后镜像不可用时才按未知副作用阻塞；
- 为什么 rollback 必须经过 checkpoint 因果检查、额度预留、同一路径权限授权和当前后镜像冲突检测；
- 为什么 `rollback_checkpoint` 不能作为 retry/adjust 的目标，合法的原始 mode `0` 也必须原样恢复；
- 为什么恢复成功仍会打开新的 generation，并要求下一轮独立 verification 才能完成任务。

## 前置条件

建议先阅读第 18 课，并了解 `AgentState`、`ExecutionAttempt`、`FailureEvent`、`RecoveryAction` 和 `PermissionGate` 的职责。切换并查看差异：

```bash
git checkout v0.18.1
git diff --stat v0.18.1..v0.19
git checkout v0.19
```

## 新增与改动文件

| 文件 | 变化 |
|---|---|
| `src/mini_agent/checkpoint.py` | 新增任务级 `CheckpointStore`、`FileCheckpoint` 和内部恢复 handler |
| `src/mini_agent/state.py` | 保存 checkpoint 元数据，扩展 rollback action、内部 attempt 和 reset 边界 |
| `src/mini_agent/recovery.py` | 校验 rollback 引用，按两阶段流程授权并调用内部工具 |
| `src/mini_agent/tools/base.py` | 支持内部工具，捕获文件工具的前后镜像并保留 checkpoint ID |
| `src/mini_agent/tools/__init__.py` | 在任务 registry 中绑定同一个 store |
| `src/mini_agent/permission.py` | 为 `rollback_checkpoint` 提取路径并默认 ask |
| `tests/test_checkpoint_rollback.py` | 覆盖镜像、tombstone、边界、冲突、中断恢复、状态可见性和恢复边界 |

## 关键流程

一次文件写入和失败恢复的事实链如下：

```text
允许 write/edit
  -> reserve attempt + possible-effect generation
  -> capture before image (bytes/mode/hash or absent)
  -> handler returns or raises
  -> capture after image (type/hash)
  -> file result + critical Structured State: checkpoint metadata
       ├─ ready -> rollback candidate is visible
       ├─ handler exception + ready -> keep running and show rollback notice
       └─ unavailable/uncertain -> preserve original result, then block recovery
  -> verification failure
  -> recover(rollback, checkpoint_id)
  -> reserve recovery quota
  -> PermissionGate(path from checkpoint)
  -> open successor generation + internal attempt
  -> lstat/hash current after image
       ├─ mismatch -> rollback_conflict -> blocked
       ├─ restore error -> rollback_restore_failed -> blocked
       └─ match -> atomic restore -> checkpoint=restored
  -> next LLM round: independent verification
```

## 实现拆解

### 1. 前镜像与后镜像

`create_registry(state, workspace_root=None)` 在创建 registry 时捕获当前 cwd，生成的 `CheckpointStore` 同时绑定到 State、Executor 和 RecoveryRuntime。State 不保存 store 的绝对根目录，也不保存原始字节，只在 snapshot 中展示如下元数据：

```json
{
  "checkpoint_id": "cp-1",
  "attempt_id": "a-2",
  "generation_id": 2,
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

已有文件的原始 bytes 留在任务私有 store 中，恢复时重新写入；新文件的前镜像是 `absent`，恢复时的目标是删除这次创建的文件。前镜像大小恰好等于上限时允许，超过上限则 checkpoint 不可用。后镜像只用于并发变更判断，以类型和 digest 为准。

文件工具结果会附带不含文件内容的 checkpoint 元数据，例如 `checkpoint_id=cp-1; path=src/app.py; status=ready`。Structured State 会读取 `checkpoints` 和 `rollback_checkpoints`，在 critical 区域列出 ID、相对路径、attempt、generation、状态和哈希；上下文压缩重新渲染状态，超长状态降级时也优先保留这组恢复信息。原始 bytes、绝对路径和文件内容不会进入这些视图。

原始 mode 使用 `stat.S_IMODE` 保存，恢复时按显式的 `None` 判断；因此合法的 mode `0` 不会被误当成缺省值 `0644`。

路径检查以任务启动 cwd 为根：绝对路径只要规范化后位于根内仍会展示为相对路径；`..` 会先规范化；根外路径、任意已有符号链接组件、非普通文件和无效父目录都不会捕获 bytes。捕获不可用不会改变原 `write_file`/`edit_file` handler 的结果。

### 2. rollback 的协议与权限

`recover` schema 增加 `rollback` 和 `checkpoint_id`。rollback 禁止携带 `requested_attempt`、`requested_tool` 或 `requested_arguments`；其他 action 携带 checkpoint ID 也会被拒绝。Runtime 只接受本任务、状态为 `ready`、且创建 attempt 不晚于目标 failure 的 checkpoint。缺失、不可用、已恢复或因果顺序不合法的引用记录一次 rejected action，保留 checkpoint，并进入 `blocked`，不打开新 generation。

恢复的授权目标来自 checkpoint 保存的规范化相对路径，不来自模型参数。Runtime 先原子预留 rollback 的 fingerprint 额度，再用 `PermissionGate.guard("rollback_checkpoint", {"path": path})` 检查权限；拒绝时释放尚未使用的内部执行额度，也不推进 generation。只有授权通过才打开后继 generation 并预留内部 attempt。

如果获准的 `write_file`/`edit_file` handler 抛错，但捕获到的 checkpoint 仍有明确的前后镜像，State 会记录 unknown failure、保留 `running`，并在 Structured State 发出带 checkpoint ID 的 rollback notice。模型可以先请求 rollback，再进行独立 verification；只有 checkpoint 不可用或后镜像无法确定时，才按未知副作用进入 `blocked`。

### 3. 内部工具与原子恢复

`rollback_checkpoint` 注册在任务 registry 中，但 `ToolRegistry.schemas()` 会过滤内部工具，模型不能从 LLM schema 直接看到它。即使模型伪造调用，Executor 也会返回 `internal_tool` 而不运行 handler。RecoveryRuntime 使用专门的内部执行入口，仍会形成完整的 `ExecutionAttempt`，其中包括 `effect_class="possible"`、权限结果、generation、recovery 和 failure 因果链接。

所有 internal 工具都不能被 `retry` 或 `adjust` 选为恢复目标。Runtime 在预留恢复额度、检查权限和激活 successor generation 之前拒绝这类请求，并记录一次 `rejected` RecoveryAction；这样不会打开空 generation，也不会留下 `reserved` 且没有结果 attempt 的动作。

执行内部 handler 前再次 `lstat` 并计算当前 digest：

- 普通文件前镜像：在同一父目录创建临时文件，写入私有 bytes，恢复原 mode，再 `os.replace`；
- absent 前镜像：只有当前仍是与 checkpoint 后镜像匹配的普通文件时才 `unlink`；
- 类型或 digest 不同：不写入、不覆盖外部内容，生成 `rollback_conflict` failure 并阻塞；
- 临时文件、chmod、replace 或 unlink 失败：清理临时文件，checkpoint 标为 `restore_failed`，生成 `rollback_restore_failed` 并阻塞。

因此 rollback 不承诺 shell、网络、多文件写入或其他外部副作用。也不会回退 generation、复用旧 verification 或自动提升权限。

### 4. 恢复之后仍必须验证

成功恢复会把 checkpoint 标记为 `restored`，把 RecoveryAction 标为 `executed` 并关联内部 attempt。这只证明恢复系统完成了它承诺的文件操作；State 同时已清除旧 verification evidence 并将 `verification_required` 置为真。下一轮 LLM 回合必须调用独立的 `run_shell(purpose="verification")`，只有当前 generation 的成功证据才能重新满足完成条件。

## 为什么这样设计

checkpoint 不是工作区快照，而是一次单文件写入的可审计前镜像。把原始 bytes 放在任务私有 store，可以支持可靠恢复，又避免 Context、日志和工具结果泄露文件内容；把工作区根目录固定在创建 registry 时，可以避免任务过程中权限作用域漂移。

后镜像采用 type + SHA-256，而不是仅比较 mtime。这样外部修改、类型替换和 absent/regular-file 变化都会被发现；同目录临时文件加原子 replace 则避免在恢复过程中留下半写入目标。冲突时保守阻塞比覆盖用户在 agent 之外的新内容更安全。

rollback 沿用 v0.18 的两阶段恢复边界：先占用额度，再授权，最后才推进 generation 和执行。恢复结果回灌的是操作事实，不是完成宣称；新的 verification 是防止“回滚动作成功但任务仍不正确”的独立证据。

## 本版特性、下一课与代码索引

本版提供进程内、任务级、单文件 checkpoint/rollback；所有 checkpoint 保留到 `/reset` 或 `/new`，不跨进程持久化。它不实现 Repair Loop 调度，也不实现 Trace & Replay，只产生未来回放需要的 checkpoint、attempt、failure 和 recovery 因果事实。

下一课按路线图继续扩展失败恢复或回放能力；不要把本版 checkpoint 当作多文件事务或通用沙箱。

- [checkpoint.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.19/src/mini_agent/checkpoint.py)
- [state.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.19/src/mini_agent/state.py)
- [recovery.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.19/src/mini_agent/recovery.py)
- [tools/base.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.19/src/mini_agent/tools/base.py)
- [tools/__init__.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.19/src/mini_agent/tools/__init__.py)
- [permission.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.19/src/mini_agent/permission.py)
- [test_checkpoint_rollback.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.19/tests/test_checkpoint_rollback.py)

验收命令：

```bash
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python scripts/check_tutorials.py
PYTHONPATH=src python scripts/check_readme.py
git diff --check
```
