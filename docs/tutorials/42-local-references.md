# 第 42 课：具名本地资料

代码快照：`v0.42` · 相邻差异：`v0.41..v0.42`

本课示例命令使用 Bash/zsh。课程正文描述的是 v0.42 快照；v0.42 Git tag 由维护者在交付后固定，阅读者切换前请确认本地已有该 tag。

## 本课目标

第 41 课解决了“怎样从工作区 Memory 找回少量相关资料”。但有一类资料不适合放进 Memory：它们可能是工作区外的本地设计文档、离线规范或团队资料。上一版 Agent 不能安全地发现这些目录，也不能把一次读取和稳定来源对应起来。

本课增加具名本地 References：用户在本地配置中登记目录，Agent 只看到稳定 alias 和说明；需要内容时在一个 alias 内搜索或按行读取。完成本课后，读者应能解释四件事：真实根路径为什么不能进入持久状态、为什么 alias 不等于读取授权、为什么每次读取都重新检查路径，以及为什么 Reference 内容仍只是模型判断用的不可信资料。

## 前置条件

先阅读[第 41 课：相关记忆检索与有界上下文](41-memory-retrieval.md)，理解 Memory 与普通 tool history 的区别。代码仓库需要 Python 3.10+；本课的离线观察命令不需要真实模型服务。

为了查看本版本相对上一版本的真实变化，可以执行：

```bash
git checkout v0.42
git diff --stat v0.41..v0.42
```

第一条命令切换到课程快照，第二条命令显示变化集中在配置、ReferenceCatalog、父侧工具和安全边界。阅读结束后回到自己的分支：

```bash
git checkout -
```

## 新增与改动文件

本课把“目录如何解析和读取”与“模型能调用什么”分开。前者由 `ReferenceCatalog` 负责，后者由工具定义和父 registry 负责；这样工具不会直接处理真实根路径，也不会把 References 带进子代理视图。

| 文件 | 作用 |
|---|---|
| `src/mini_agent/references.py` | 冻结 alias、解析配置路径、检查符号链接和敏感目录，执行有界搜索与按行读取 |
| `src/mini_agent/tools/references.py` | 定义三个严格 JSON schema、默认参数、结果上限和父侧 handler |
| `src/mini_agent/config.py`、`config_example.py` | 提供默认空配置，并说明真实 References 只写入未跟踪的 `config_local.py` |
| `src/mini_agent/tools/__init__.py` | 只给绑定父 `AgentState` 的 registry 注入 catalog 和三个工具 |
| `src/mini_agent/permission.py` | 列表默认允许，搜索/读取按 `alias:relative_path` 默认询问 |
| `src/mini_agent/tools/base.py` | 保留完整 JSON tool result，同时清洗 State/Trace 的 metadata excerpt |
| `src/mini_agent/state.py`、`prompt.py` | 允许只读恢复调查，保持 workspace drift、verification、Plan 和子代理边界 |

## 版本变更定位

v0.41 的入口是父 Context 对 Memory 的临时检索；它不读取工作区外目录。下面的图只画上一版已经存在的真实调用链，帮助读者先看清新增能力插在哪里。

```text
[旧] AgentState.task + 最近 user 文本
                 │
                 ▼
[旧] ContextManager.prepare_messages()
                 │
                 ├── [旧] MemoryRetriever → 临时、不可信 Memory 资料区
                 └── [旧] ToolExecutor → parent ToolRegistry → role=tool history
```

v0.42 不改 Context 的自动检索流程，也不增加 session 字段。它在父 registry 的普通工具入口旁插入一个进程内 catalog；工具结果仍走原有 Executor、PermissionGate 和 history 边界。

```text
[旧] 父 Agent Runtime
          │
          ├── [~] create_registry(state=parent)
          │       ├── [旧] MemoryStore / Memory tools
          │       └── [+] ReferenceCatalog(冻结真实根，进程内)
          │                 │
          │                 └── [+] list_references  [C]  alias + description
          │                     [+] search_reference [C]  PermissionGate → 路径检查 → 搜索
          │                     [+] read_reference   [C]  PermissionGate → 路径检查 → 读取
          │
          ├── [旧] ToolExecutor → 完整 JSON tool history
          └── [旧] State/Trace → [+] alias-relative metadata excerpt

[B] 子代理 FilteredToolRegistryView、自动 Context、Memory、verification evidence、session schema
    不接收 ReferenceCatalog 或三个工具。
```

图中的 `[旧]` 表示 v0.41 已有节点，`[+]` 表示本课新增，`[~]` 表示修改，`[C]` 表示主要消费者，`[B]` 表示本版边界。正常路径是“配置冻结 → 父工具调用 → 权限 → 每次路径检查 → 有界 JSON”。权限拒绝在 handler 前结束；路径、文件类型、编码和资源错误由 handler 抛出，再由 Executor 记录为统一的工具失败。

## 关键流程

### 1. 配置只登记名字、路径和说明

真实模型配置仍只放在未跟踪的 `config_local.py`。References 使用同一个本地文件登记资料目录：

```python
REFERENCES = [
    {
        "alias": "python-docs",
        "path": "../../offline/python",
        "description": "本地 Python 设计资料",
    },
]
```

相对 `path` 以 `src/mini_agent/config_local.py` 所在目录为基准。启动时，catalog 把路径展开、绝对化并解析为真实目录；随后即使配置对象被修改，已经运行的父 Runtime 仍使用原来的冻结定义。alias 必须以小写字母开头，只能包含小写字母、数字、`_`、`-`，最长 64 个字符。

### 2. 发现不等于授权

模型先可以调用：

```text
list_references()
```

结果只包含 alias 和 description，例如 `python-docs` 与“本地 Python 设计资料”，不会返回真实根路径。配置中的目录不会因此自动获得读取许可：

```text
search_reference(alias="python-docs", query="dataclass", include="*.py")
```

默认权限会询问一次，提示只显示 alias、相对路径、搜索词和限制，不显示真实根。权限 pattern 是 `python-docs:.` 或 `python-docs:subdir`；用户选择 `always` 时只记住精确的 `alias:path` pattern，不自动授权该目录的子树；配置目录本身不等于授权规则。

### 3. 搜索和按行读取返回可核查来源

搜索是大小写不敏感的字面量匹配，不执行正则表达式：

```text
{
  "alias": "python-docs",
  "matches": [
    {"path": "guide/dataclasses.py", "line": 18,
     "text": "...dataclass...", "sha256": "..."}
  ],
  "total_matches": 1,
  "truncated": false
}
```

`path` 始终是 alias 内相对路径。读取时使用 `offset=0`、`limit=200` 的有界行片段：

```text
read_reference(alias="python-docs", path="guide/dataclasses.py", offset=12, limit=6)
```

结果带有行号、实际 `returned_lines`、自洽的起止行、`omitted_lines`、总行数、文件 SHA-256 和 `truncated`。`limit` 是请求上限，不是承诺的实际返回数量；最终 JSON 因字节上限裁掉行时，数量、起止行和省略数量会一起更新。搜索还返回 `returned_matches`；`scan_truncated` 表示搜索资料没有完整扫描，和展示结果裁剪的 `truncated` 不同。摘要是本次实际读取内容的摘要；如果文件在搜索和读取之间发生变化，后一次读取返回新内容和新 SHA-256，不把旧搜索结果当成快照。

### 4. 每次访问都重新检查边界

ReferenceCatalog 不把一次路径检查缓存成永久许可。它拒绝绝对路径、空路径、`..`、NUL、目录读取、设备文件、非 UTF-8 文件和超过 1 MiB 的文件。文件符号链接只有在终点仍位于冻结根内时才可读；目录符号链接不会被搜索遍历。打开时会从冻结根的目录 fd 逐段打开 canonical 路径，并复核根目录、路径组件和文件身份；不支持这组 fd 能力的平台采用打开前后复核，竞态变化直接失败。

启动时位于 MemoryStore 根或默认 `~/.mini_agent/sessions` 内的 Reference 根会直接拒绝。较宽的 Reference 根如果包含这些敏感子目录，递归搜索跳过它们；显式读取会报错。名为 `config_local.py` 的目标同样直接拒绝。错误只返回明确的错误种类和不含真实根的说明。

## 实现拆解

### 1. ReferenceCatalog 只在父 registry 中创建

`create_registry(state=...)` 先得到当前 MemoryStore，再把它的根和默认 session 根交给 `ReferenceCatalog` 作为敏感路径集合。没有 `AgentState` 的模块级 smoke registry 不注册 References；子代理的固定白名单也不会因为父 registry 新增工具而扩大。

### 2. 工具 schema 先限制模型输入

三个工具都使用 `additionalProperties=False`。`search_reference` 的 `path`、`include`、`limit` 有默认值，`read_reference` 的 `offset` 和 `limit` 有默认值；alias、query、相对路径和数字范围在进入 catalog 前就会被统一验证。三个工具的 `effect_class` 都是 `none`，因此不预留 generation、不创建 verification evidence，也不修改 Memory 或 State。

### 3. 完整结果与摘要分开

普通 tool history 需要正文，模型才能理解搜索命中或读取内容；State/Trace 的 `output_excerpt` 只需要知道“读了哪个 alias 的哪个相对路径、命中了几条、文件摘要是什么”。因此 Executor 对 References 单独清洗 `text` 和行正文，保留 alias、相对路径、行号、数量、SHA-256 和截断状态。成功结果始终是有界 JSON；路径或文件错误直接抛给 Executor，记录为 `outcome="failed"` 和 `error_kind="reference_access_error"`。输出超限时只移除末尾集合项，并同步更新 `returned_lines`/`returned_matches` 与起止范围，不会截出残缺 JSON。

## 为什么这样设计

具名 alias 把“模型可发现的稳定名称”和“进程内必须保护的真实路径”分开。路径冻结避免运行中悄悄改变配置；每次重新解析又能应对文件变化和符号链接变化。按行来源和 SHA-256 让模型或用户可以在之后核查“哪一个文件、哪一行、哪一份内容”。

搜索和读取仍默认询问，是因为本地资料可能包含不适合交给模型的内容；列表默认允许只提供用户登记过的名称和说明。把权限 pattern 设为 alias 加相对路径，并让 `always` 只记住精确 pattern，既能记住一次明确选择，也不会把另一个 alias 的同名路径一并放行。

本课刻意不做写入、远程仓库、正则搜索、二进制读取、缓存、自动 Context 注入、Memory 来源迁移和子代理访问。Reference 内容也不能覆盖项目指令、Plan 或 PermissionGate，更不能当作 verification evidence。Memory schema 1 的自由文本 `source` 仍统一是 `source_status="unverified"`，v0.42 没有把它改成文件来源系统。

## 设计边界

- 真实根只留在进程内 catalog；alias、相对路径和正文仍可能作为普通 tool history 随既有 session 行为保存。
- References 不是当前工作区状态的证据；崩溃恢复时它们可作为普通只读调查，但不能结算 workspace drift。
- `list_references` 不读取目录内容；搜索和读取必须先通过 PermissionGate，再进入 handler。
- 搜索最多访问 2,000 个文件、10,000 个目录项并读取 64 MiB 正文，保留 100 条命中；单文件最多 1 MiB，单行和最终 JSON 也有固定上限。`scan_truncated` 表示资料扫描提前停止，`truncated` 表示展示集合被裁剪。
- 子代理仍严格只有 `calculate`、`read_file`、`list_dir`、`grep`；父 Agent 必须自己判断 Reference 材料，不能把子代理报告升级为验证事实。

## 运行与观察

本课的观察重点是“alias 能发现和定位资料，但不能绕过权限或路径边界”。在 Bash/zsh 中运行：

```bash
PYTHONPATH=src python -m pytest -q tests/test_references_v042.py
```

你应观察到合法目录可以返回稳定的相对路径、行号和 SHA-256；重复调用在同一文件内容下顺序相同。修改文件后再次读取，摘要应变化。将 `search_reference` 或 `read_reference` 的权限决定设为 `reject` 时，结果应在 handler 前结束；把路径改为绝对路径、`..` 或逃逸符号链接时，handler 会失败但错误不应出现真实根路径。`list_references` 可以发现 alias，但不会列出根目录。

## 本版特性、下一课与代码索引

v0.42 完成了父 Agent 的具名本地 References：它们可发现、可搜索、可按行读取，并带有逐次权限和路径检查。References 仍是不可信资料，不会进入自动 Context、Memory、Plan、verification evidence 或 Subagent。下一课应在新的计划中定义，而不是把本课的本地读取边界默认为远程资料能力。

- [`references.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.42/src/mini_agent/references.py)：冻结配置、敏感路径、符号链接和有界文件访问。
- [`tools/references.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.42/src/mini_agent/tools/references.py)：三个父侧工具的 JSON schema 和结果合同。
- [`tools/base.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.42/src/mini_agent/tools/base.py)：完整 tool history 与 metadata excerpt 的分界。
- [`permission.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.42/src/mini_agent/permission.py)：alias-relative 权限 pattern 和安全提示。
- [`test_references_v042.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.42/tests/test_references_v042.py)：本课行为边界的可执行索引。
