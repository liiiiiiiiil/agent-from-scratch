# 第 42 课：用稳定名称读取本地参考资料

上一课：[相关记忆检索与有界上下文](41-memory-retrieval.md) · [教程总览](README.md)

> 代码快照：`v0.42` · 相邻差异：`v0.41..v0.42` · 命令环境：Bash/zsh

## 本课目标

第 40、41 课处理的是工作区自己的 Memory：它适合保存少量、明确记录的项目知识。现在
再想一个场景：团队有一个工作区外的离线设计文档目录，你希望 Agent 在需要时查阅它，
却不希望把整个目录复制到项目里，也不希望把真实的本机路径暴露给模型。

本课增加本地 References（参考资料）。用户在本地配置中给目录一个稳定的别名，Agent
先看到别名和说明，再按需搜索或读取目录内的文件。它是只读资料入口，不是远程仓库，
也不是自动注入上下文的第二套 Memory。

读完本课，你应能回答：

- 为什么模型看到的是 alias（别名），而不是本机真实路径；
- 为什么“能发现”不等于“已经获准读取”；
- 为什么每次搜索和读取都要重新检查相对路径与符号链接；
- 为什么读到的 Reference 仍只是供模型判断的资料，不能代替当前任务的验证。

## 前置条件

先阅读[第 41 课：让 Agent 找回相关记忆，而不是翻遍所有记录](41-memory-retrieval.md)，
理解 Memory、Context 和普通 tool history 的区别。需要 Python 3.10+；本课命令使用
Bash/zsh，不需要真实模型服务。

先切到本课快照并查看相邻版本的变化：

```bash
git checkout v0.42
git diff --stat v0.41..v0.42
```

阅读结束后可以回到原来的分支：

```bash
git checkout -
```

## 上一版的问题：记忆和本地参考资料不是一回事

Memory 适合保存“这个项目的测试命令”这类短小、经过明确记录的知识；它不适合替代
一个可能有很多文件的离线资料目录。v0.41 的自动检索也只读工作区 Memory，不会
访问工作区外的本地路径。

直接把本机绝对路径交给模型有两个问题：路径可能包含用户名、项目结构等不必要的
敏感信息；如果模型再把路径拼接到文件工具中，`..` 或符号链接还可能越出用户原本
想开放的目录。v0.42 因此把“可被模型发现的名称”和“进程内部使用的真实目录”分开。

## 本版新增什么

本版把一次资料访问拆成四步：

```text
config_local.py 的 References 配置
              ↓ 启动时解析并冻结
       ReferenceCatalog（进程内目录簿）
              ↓
list_references → alias + description       （发现）
search/read     → 权限 → 路径检查 → 有界文件内容（使用）
```

`ReferenceCatalog` 是进程内的目录簿：它保存别名、说明和真实目录，但真实目录不写入
State、Context、session 或 Trace。`alias` 只是“查找时使用的名字”，本身不等于读取
授权；搜索和读取仍要经过 PermissionGate。

主要代码变化如下：

## 新增与改动文件

| 文件 | 作用 |
|---|---|
| [`references.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.42/src/mini_agent/references.py) | 冻结别名和真实目录，校验路径、符号链接、敏感位置，并执行有界访问。 |
| [`tools/references.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.42/src/mini_agent/tools/references.py) | 定义三个父侧只读工具的输入和结果合同。 |
| `config.py`、`config_example.py` | 提供默认空配置，并说明真实配置只放在未跟踪的 `config_local.py`。 |
| [`tools/__init__.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.42/src/mini_agent/tools/__init__.py) | 只在父 registry 中绑定 ReferenceCatalog。 |
| [`permission.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.42/src/mini_agent/permission.py) | 以 `alias:relative_path` 形式审阅搜索和读取权限。 |

## 版本变更定位

图中的 `[旧]` 是 v0.41 已有能力，`[+]` 是本课新增，`[~]` 是本课修改，`[C]`
表示主要使用者，`[B]` 表示本课边界。

上一版的父 Runtime 只有工作区 Memory 的临时检索和普通工具：

```text
[旧][C] 父 Agent Runtime
      ├─→ [旧] ContextManager → MemoryRetriever → 临时 Memory 资料区
      └─→ [旧] ToolExecutor → 父 ToolRegistry → role=tool history
          [B] 没有工作区外本地资料的稳定入口
```

本版在父 registry 的工具入口增加进程内 catalog，但不改自动 Memory 检索和 session schema：

```text
[C] 父 Agent Runtime
      └─→ [~] create_registry(state=parent)
            ├─→ [旧] MemoryStore / Memory tools
            └─→ [+] ReferenceCatalog（真实根只留在进程内）
                  ├─→ [+] list_references → alias + description
                  ├─→ [+] search_reference → 权限 → 路径检查 → 搜索
                  └─→ [+] read_reference → 权限 → 路径检查 → 按行读取

[旧] ToolExecutor → 完整 JSON tool history
[+] State / Trace metadata → alias、相对路径、行号、摘要和 hash
[B] 子代理、自动 Context、Memory、Plan、verification evidence 不接收 References。
```

## 关键流程

### 1. 配置一个名字，而不是把路径写进提示词

真实的 `BASE_URL`、`API_KEY` 和 `MODEL` 仍只能放在未跟踪的 `config_local.py`。本地
资料可以在同一个文件中登记：

```python
REFERENCES = [
    {
        "alias": "python-docs",
        "path": "../../offline/python",
        "description": "本地 Python 设计资料",
    },
]
```

相对 `path` 以 `config_local.py` 所在目录为基准。启动父 Runtime 时，系统会把它展开、
绝对化并解析成真实目录，然后冻结这份定义；运行中修改配置对象不会悄悄改变正在运行
的 catalog。alias 必须以小写字母开头，只能使用小写字母、数字、`_` 和 `-`，最长
64 个字符。

### 2. 发现资料和读取资料是两件事

模型可以先调用：

```text
list_references()
```

它只返回 `python-docs` 和“本地 Python 设计资料”，不返回真实根路径，也不扫描目录。
如果模型接着调用：

```text
search_reference(alias="python-docs", query="dataclass", include="*.py")
```

PermissionGate 默认会询问用户。提示只显示 alias、alias 内相对路径、搜索词和限制，
不显示真实本机路径。权限 pattern 是 `python-docs:.` 或 `python-docs:subdir`；用户
选择 `always` 时只记住这个精确的 `alias:path`，不会自动放开另一个 alias 或整棵子树。

### 3. 搜索和读取都提供可核查的位置

`search_reference` 做大小写不敏感的字面量匹配，不执行正则表达式。结果使用 alias
内的相对路径、行号、匹配文本和 SHA-256，例如：

```json
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

需要更大范围时，模型可以调用：

```text
read_reference(alias="python-docs", path="guide/dataclasses.py", offset=12, limit=6)
```

返回值带有行号、实际返回数量、起止行、总行数、文件 SHA-256 和截断状态。`limit` 是
请求上限，不保证一定能返回这么多行；如果最终 JSON 的字节上限又裁掉了内容，起止行
和 `omitted_lines` 会同步更新。搜索结果中的 `scan_truncated` 表示资料没有完整扫描，
与展示集合被裁剪的 `truncated` 不是一回事。

### 4. 每一次访问都重新验证路径

catalog 冻结的是允许的根目录，不是某个文件的永久通行证。每次搜索或读取都会检查：

- 路径必须是 alias 内的相对路径，不能是绝对路径、空路径、包含 `..` 或 NUL 的路径；
- 目标必须是允许的 UTF-8 普通文件，不能是目录、设备文件或超过 1 MiB 的文件；
- 文件符号链接解析后的终点仍要位于冻结根内，目录符号链接不会被搜索遍历；
- `config_local.py`、Memory 根目录和默认 `~/.mini_agent/sessions` 等敏感位置不能被读取。

打开文件时还会复核路径组件和文件身份；如果文件在检查与打开之间发生竞态变化，
访问会失败，而不是冒险读取不确定的目标。错误只返回明确的错误类型和不含真实根
路径的说明。

## 实现拆解

`create_registry(state=...)` 只在父 Agent 有自己的 `AgentState` 时创建 catalog。没有
状态的模块级 smoke registry 不注册 References；子代理的固定白名单也不会因为父侧
新增三个工具而扩大。

三个工具都使用严格的 JSON schema，拒绝未声明参数。它们的 `effect_class` 都是
`none`，所以不会预留 generation、创建 verification evidence 或修改 Memory/State；
但搜索和读取的权限询问仍然发生在 handler 之前。

完整正文必须留在普通 tool history，模型需要它来判断搜索结果；State 和 Trace 只保留
alias、相对路径、行号、数量、SHA-256 和截断状态等摘要。这样用户可以核查“读了哪个
别名下的哪一份资料”，又不会把真实根路径复制到状态摘要中。访问失败由 Executor
记录为 `outcome="failed"` 和 `error_kind="reference_access_error"`。

## 为什么这样设计

alias 把稳定的模型接口和易变、可能敏感的本机路径分开；启动时冻结定义，避免运行中
悄悄换资料根；每次重新检查，避免把旧的路径判断当成永久安全保证。相对路径、行号和
SHA-256 让一次读取更容易复查，也能看出文件在两次访问之间是否改变。

列表默认允许，是因为它只展示用户主动登记的名称和说明；搜索与读取默认询问，是因为
目录中可能有用户不想交给模型的文件。只读设计和父侧限定，避免 References 变成隐式
文件写入或子代理越权入口。

代价也很明确：v0.42 不支持写入、远程仓库、正则搜索、二进制读取、缓存和自动 Context
注入；Reference 不会自动成为 Memory 的来源；它也不能覆盖项目指令、Plan 或
PermissionGate，更不能单独证明当前工作区已经通过验证。

## 设计边界

搜索最多访问 2,000 个文件、10,000 个目录项并读取 64 MiB 正文，最多保留 100 条命中；
单个文件最多 1 MiB，单行和最终 JSON 也有固定上限。`scan_truncated` 表示扫描提前
停止，`truncated` 表示展示结果被裁剪。

真实根只存在于当前进程的 catalog；alias、相对路径和正文仍可能像普通工具结果一样
进入 Context 或 session。References 不进入自动 Memory 检索，不加入子代理的
`calculate`、`read_file`、`list_dir`、`grep` 之外的白名单。父 Agent 必须自己判断
Reference 内容，不能把它交给子代理后再把子代理的报告升级成 verification evidence。

## 运行与观察

本课的观察重点是“alias 能帮助定位资料，但不能绕过权限和路径边界”。运行：

```bash
PYTHONPATH=src python -m pytest -q tests/test_references_v042.py
```

你应观察到：合法目录能返回稳定的相对路径、行号和 SHA-256；同一内容的重复访问顺序
一致；修改文件后再次读取会得到新的摘要。拒绝 `search_reference` 或 `read_reference`
权限时，handler 不会执行；使用绝对路径、`..` 或逃逸符号链接时会失败，错误中不出现
真实根路径；`list_references` 可以发现 alias，却不会列出根目录内容。

## 本版特性、下一课与代码索引

v0.42 完成了父 Agent 的具名本地 References：它们可发现、可搜索、可按行读取，并带有
逐次权限和路径检查。它们仍是不可信资料，不会进入自动 Context、Memory、Plan、
verification evidence 或 Subagent。后续能力应在新的计划中定义，不能把本课的本地
读取边界默认扩展成远程资料能力。

- [`references.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.42/src/mini_agent/references.py)：冻结配置、敏感路径、符号链接和有界文件访问。
- [`tools/references.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.42/src/mini_agent/tools/references.py)：三个父侧工具的 JSON schema 和结果合同。
- [`tools/base.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.42/src/mini_agent/tools/base.py)：完整 tool history 与 metadata excerpt 的分界。
- [`permission.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.42/src/mini_agent/permission.py)：alias-relative 权限 pattern 和安全提示。
- [`test_references_v042.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.42/tests/test_references_v042.py)：本课行为边界的可执行索引。
