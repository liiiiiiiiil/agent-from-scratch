# 第 36 课：让父子 Agent 使用不同的模型服务

上一课：[父 Agent 和子代理共用一条运行循环](35-shared-agent-runtime.md) · [教程总览](README.md) · 下一课：[子代理生命周期与聚合预算](37-subagent-lifecycle-budget.md)

> 代码快照：`v0.36` · 相邻差异：`v0.35..v0.36` · 命令环境：Bash/zsh

本课的源码链接固定到 `v0.36`；阅读和运行本课都不需要创建 Git tag。

## 本课目标

上一课已经让父 Agent 和子代理共用一条运行循环，但它们还默认使用同一组 `BASE_URL`、`API_KEY`、`MODEL`。如果临时修改全局变量让子代理换模型，父请求也可能被影响；如果把某个服务的消息格式直接写进 Runtime，核心循环就会越来越难维护。

本课把三个问题分开：向哪个服务发送请求、服务使用什么消息格式、项目给这个模型绑定什么本地名字。读完后，你应能解释：

- provider、protocol、model profile 分别是什么；
- 为什么父子必须在创建 Runtime 前冻结自己的 model binding；
- 适配器怎样把不同服务翻译成统一的 assistant/tool 消息；
- 为什么未知或越权的子模型别名必须在 HTTP 请求前拒绝。

## 上一版的问题

v0.35 的控制循环已经统一，但模型来源仍是全局配置。父 Agent 和子代理不能安全地各用一个服务；更换服务方还会把认证头、工具调用格式、流式解析和错误处理带进 Runtime。

本版的原则是：Runtime 只接触统一的消息结构；provider 选择和协议翻译停在调用边界。这样“换服务”不会偷偷改变工具权限、完成判断或父子隔离。

## 前置条件与版本切换

需要第 35 课、基础 Python、JSON 和 HTTP 请求的基本概念。命令使用 Bash/zsh：

```bash
git checkout v0.35
git diff --stat v0.35..v0.36
git checkout v0.36
```

真实 `endpoint`、API key 和 model ID 只能放在本地未跟踪的 `src/mini_agent/config_local.py`；提交到仓库的示例只能使用占位值。

## 新增与改动文件

这些文件共同完成“本地配置 → 冻结绑定 → 协议适配 → 统一 Runtime”这条链：

| 文件 | 作用 |
|---|---|
| [`src/mini_agent/providers/catalog.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.36/src/mini_agent/providers/catalog.py) | 校验 provider/profile 配置，生成冻结的 `ModelBinding`。 |
| [`src/mini_agent/providers/base.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.36/src/mini_agent/providers/base.py) | 定义统一响应、usage 和适配器边界。 |
| [`src/mini_agent/providers/openai_chat.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.36/src/mini_agent/providers/openai_chat.py) | 处理 OpenAI-compatible Chat Completions 请求和 SSE 分片。 |
| [`src/mini_agent/providers/anthropic_messages.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.36/src/mini_agent/providers/anthropic_messages.py) | 处理 Anthropic Messages 的 system、tool block 和流式事件。 |
| [`src/mini_agent/providers/http.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.36/src/mini_agent/providers/http.py) | 用标准库 `http.client` 提供连接和有界读取。 |
| [`src/mini_agent/runtime.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.36/src/mini_agent/runtime.py) | 消费统一响应并记录 usage 来源。 |
| [`src/mini_agent/delegation.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.36/src/mini_agent/delegation.py) | 检查子 profile 白名单并冻结子绑定。 |

## 版本变更定位

图例：`[旧]` v0.35 已有，`[+]` v0.36 新增，`[~]` v0.36 修改，`[C]` 主要消费者，`[B]` 本课边界。

v0.35 的模型入口依赖一组全局值：

```text
[C] agent_loop / SubagentRunner
      -> [旧] AgentRuntime.run()
           -> [旧] call_llm()
                -> [旧] BASE_URL / API_KEY / MODEL
```

v0.36 在调用边界插入配置目录和协议适配器，但仍只有一个 Runtime：

```text
[C] agent_loop ───────────────┐
                              ├─> [~] AgentRuntime.run()
[C] SubagentRunner ───────────┘       -> [~] 冻结的 ModelBinding
                                        -> [+] ProviderCatalog
                                        -> [+] ProviderAdapter
                                             ├─ OpenAI Chat Completions
                                             └─ Anthropic Messages
                                        -> 统一 assistant/tool 消息
                                        -> [~] UsageMeter

[B] 本版不加入生命周期、聚合预算、取消、并行委派或持久化委派记录
```

父、子分别在 Runtime 创建前冻结 binding。绑定里有安全的 profile/provider/protocol/fingerprint 摘要；真实 endpoint、model ID、key 和认证头不会进入 State、Context、session 或用户可见错误。

## 核心概念与数据结构

### 1. provider、protocol、profile 是三层不同的名字

可以先用三个问题区分它们：

- **provider**：请求发给哪一个模型服务，包含 endpoint、认证方式和超时；
- **protocol**：这个服务要求怎样组织请求和响应，本版支持 `openai_chat` 与 `anthropic_messages`；
- **model profile**：项目给一组具体配置起的本地别名，指向 provider 和真实模型，并声明上下文窗口、输出上限和工具能力。

下面只展示占位配置。它表达的是父子可以选择不同服务，不是要求把真实密钥写进代码：

```python
PROVIDERS = {
    "gateway-openai": {"protocol": "openai_chat", "endpoint": "https://api.example.invalid/...", "api_key": "sk-PLACEHOLDER"},
    "gateway-anthropic": {"protocol": "anthropic_messages", "endpoint": "https://api.example.invalid/...", "api_key": "key-PLACEHOLDER"},
}
MODEL_PROFILES = {
    "parent-default": {"provider_id": "gateway-openai", "model_id": "model-PLACEHOLDER", "context_window": 128_000, "max_output_tokens": 8_192},
    "child-anthropic": {"provider_id": "gateway-anthropic", "model_id": "model-PLACEHOLDER", "context_window": 200_000, "max_output_tokens": 4_096},
}
PARENT_MODEL_PROFILE = "parent-default"
SUBAGENT_MODEL_PROFILE = "child-anthropic"
SUBAGENT_ALLOWED_MODEL_PROFILES = ("child-anthropic",)
```

父模型发出 `delegate_task(model_profile="child-anthropic")` 时只能传这个本地别名，不能传 endpoint、API key、真实 model ID 或自定义认证头。省略别名时使用子默认 profile，再没有时使用父 profile；显式但未知或不在白名单中的别名直接失败，不会偷偷 fallback（静默换用另一个服务）。

### 2. binding 是一次运行的冻结依赖

`ProviderCatalog` 启动时检查配置完整性、协议是否支持、数值限制是否合理以及模型是否支持工具。通过后，`bind()` 生成不可变的 `ModelBinding`。运行期间不能通过全局变量切换模型，这保证父、子请求不会互相污染。

binding 的 fingerprint 来自非认证配置事实。State、session 和 Trace 只保存 profile、provider、protocol 和 fingerprint 等无凭据摘要；恢复时重新从本地配置解析 binding，不从 session 恢复 key 或活动连接。如果配置缺失或发生变化，系统应明确报错，而不是自动改用其他 provider。

旧配置仍有兼容路径：空的 provider/profile 配置会把旧三元组转换成 `legacy-default`。新配置则必须完整定义 provider 和 profile，不能只填写一半而产生模糊绑定。

### 3. 适配器只翻译，不替 Runtime 做决定

适配器接收统一的消息列表和工具 schema，返回统一的 assistant message、finish reason 和 usage：

```python
ProviderResponse(
    message={"role": "assistant", "content": "...", "tool_calls": [...]},
    finish_reason="tool_use",
    usage=ProviderUsage(input_tokens=12, output_tokens=8, source="provider"),
)
```

OpenAI 适配器负责重组按 index 到达的 SSE 分片；Anthropic 适配器负责把顶层 system、`tool_use` 和 `tool_result` block 转成内部格式。响应在 `[DONE]` 或完整事件序列前中断，或 tool arguments 不是完整 JSON object 时，适配器返回协议错误，executor 不会执行半个调用。

Runtime 不需要知道这些原生字段。它仍然追加一个 assistant 消息，再为每个工具调用按模型顺序追加唯一 `role=tool` 结果。适配器不执行工具、不改 State、不判断完成，也不做隐藏重试。

## 关键流程

父子使用不同协议时，流程仍然只有一条运行循环：

```text
启动 CLI
  -> 读取本地 catalog
  -> 冻结 parent binding
  -> 创建父 Runtime

父 Runtime
  -> parent binding -> OpenAI Chat Completions
  -> 统一 assistant(tool_calls)
  -> delegate_task(model_profile="child-anthropic")
       -> HTTP 之前检查 profile 白名单
       -> 冻结 child binding -> Anthropic Messages
       -> 独立 child Context / usage meter
       -> 一个结构化 SubagentResult
  -> 父 Context 收到一个对应的 role=tool 结果
```

Context 压缩摘要也是模型调用，不是免费的后台步骤：父摘要使用父 binding，子摘要使用子 binding，并分别计入 usage。服务商没有返回 usage 时使用保守估算并标记 `estimated`；同一运行混合两种来源时标记 `mixed`。

## 运行与观察

在本地配置了两个可用的 provider/profile 后，用 Bash/zsh 启动 CLI：

```bash
PYTHONPATH=src python -m mini_agent
```

观察一次委派时，父请求应使用父 profile，子请求使用白名单内的子 profile；父 Context 仍只收到一个结构化委派结果。把未知 profile 写进 `delegate_task` 时，应在 HTTP 请求前得到明确拒绝，不会访问任何服务。命令行首条任务处理后，CLI 仍会进入交互循环。

## 实现拆解

`providers/catalog.py` 把本地映射转换为 `ProviderConfig`、`ModelProfile` 和 `ModelBinding`；`providers/openai_chat.py` 与 `providers/anthropic_messages.py` 各自处理认证头、消息转换、流式分片和协议错误；`providers/http.py` 使用标准库 `http.client`，每次请求独立连接并显式发送 `Accept-Encoding: identity`。

`AgentRuntime` 继续是唯一的模型—工具—观察循环，只增加 binding 和统一 usage 字段。`ContextManager` 使用所绑定 profile 的上下文窗口，摘要也使用同一个 binding。`DelegationManager` 在启动子 Runner 前完成白名单检查和 binding 冻结。

## 为什么这样设计

把 provider 选择放进 catalog、把协议翻译放进 adapter，可以让 Runtime 只维护一套工具闭合、权限和完成判定。若在 `run()` 里写“遇到 Anthropic 就……”，协议细节会逐渐污染核心控制流程，错误路径也很难审计。

显式 profile 白名单和不自动 fallback 是安全边界，不是使用体验的牺牲：fallback 会改变数据接收方、权限环境和计费对象，尤其不能让子代理自行选择未获准的服务。配置错误在第一次 HTTP 请求前暴露，用户可以明确修正。

代价是配置多了一层 provider/profile，适配器必须严格处理两种流式协议；旧三元组兼容路径保留了原有入口。本版刻意不处理生命周期、聚合预算、取消、并行委派和持久化委派记录。

## 设计边界

- provider 原生消息不进入 Runtime、Context、ToolExecutor 或 State，内部只使用归一化结构。
- 适配器不执行工具、不修改 State、不判断完成、不重试，也不读取“当前全局模型”。
- 父子 binding、usage meter、Context 和 history 独立；父仍独占修改、权限、计划、generation、verification 和完成判定。
- State、session、Trace、工具结果和用户可见错误不包含真实 endpoint、model ID、认证头或 API key。
- 旧配置映射到 `legacy-default`；新配置必须完整且明确。
- 本版仍是同步、depth=1、只读子代理；后续课程才处理聚合预算、取消和并行。

## 本版特性、下一课与代码索引

本课完成了多 provider、两种协议适配、父子独立 profile、统一响应和 usage 归属。父子仍进入同一个 `AgentRuntime.run()`，每个 tool call 仍有唯一且按顺序提交的 `role=tool` 结果；未授权 profile 在联网前拒绝。

下一课会在不改变子代理只读边界的前提下，增加委派生命周期、取消和父任务聚合预算。

核心源码：

- [`src/mini_agent/providers/base.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.36/src/mini_agent/providers/base.py)
- [`src/mini_agent/providers/catalog.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.36/src/mini_agent/providers/catalog.py)
- [`src/mini_agent/providers/openai_chat.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.36/src/mini_agent/providers/openai_chat.py)
- [`src/mini_agent/providers/anthropic_messages.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.36/src/mini_agent/providers/anthropic_messages.py)
- [`src/mini_agent/runtime.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.36/src/mini_agent/runtime.py)

完整设计见 [`docs/plans/subagent-delegation-plan.md`](../plans/subagent-delegation-plan.md)，配置约束见 [`docs/operation/manual.md`](../operation/manual.md)。
