# 第 36 课：多 provider 与统一协议适配

上一课：[共享父子运行循环](35-shared-agent-runtime.md) · [教程总览](README.md) · 下一课：[子代理生命周期与聚合预算](37-subagent-lifecycle-budget.md)

> 代码快照：`v0.36` · 相邻差异：`v0.35..v0.36` · 命令环境：Bash/zsh

本课的源码链接固定到由仓库维护者创建的 `v0.36` tag；阅读和运行本课均不需要创建 tag。

## 本课目标

上一课已经把父 Agent 和只读 Subagent 收进同一个 `AgentRuntime.run()`。但如果父子仍只能通过一组三元组配置访问一个模型服务，运行循环虽然统一了，模型来源却没有真正成为可替换的实例依赖。本课解决这个问题：同一个任务可以让父 Agent 使用一个服务，Subagent 使用另一个服务，同时两者继续经过同一套工具协议和完成边界。

读完本课后，你应能分清三个容易混淆的词：provider 是模型服务的配置身份，protocol 是服务方使用的请求/响应格式，model profile 是项目给某个模型绑定起的本地别名。你还应能解释为什么 provider 差异要停在适配层，以及为什么未知的子模型别名必须在发出 HTTP 请求前拒绝。

## 上一版的问题

v0.35 的父子循环已经共享，但模型调用入口仍然以旧的 `BASE_URL`、`API_KEY`、`MODEL` 为中心。把其中一个全局变量临时改掉，会影响其他调用；更换服务方还会把认证头、工具格式和流式解析一起带进 Runtime。这样做有两个实际问题：父子无法安全地各用一个模型，协议差异也会逐渐污染完成判定和工具执行代码。

本课把“选择哪个模型”和“怎样收发协议”拆开。Runtime 只看到统一的 assistant message、tool call 和 usage；适配器负责把这些结构翻译成具体服务方的 HTTP 请求，并把返回值翻译回来。

## 前置条件

前置条件是第 35 课、基础 Python、JSON 和 HTTP 请求的基本概念。先查看相邻版本的真实差异，再切到本课代码快照：

```bash
git checkout v0.35
git diff --stat v0.35..v0.36
git checkout v0.36
```

第一条命令用于对照上一版，最后一条进入本课快照。真实配置仍只写入未跟踪的 `src/mini_agent/config_local.py`，不要把 endpoint、API key 或真实 model ID 写进提交的示例文件。

## 新增与改动文件

下面的链接全部固定到本课的 `v0.36` 快照，便于将协议适配器和调用入口放在同一版本中阅读。

| 文件 | 变化 | 作用 |
|---|---|---|
| [`src/mini_agent/providers/base.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.36/src/mini_agent/providers/base.py) | 新增 | 定义统一响应、usage、适配器协议和无凭据 provider 错误边界。 |
| [`src/mini_agent/providers/catalog.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.36/src/mini_agent/providers/catalog.py) | 新增 | 校验 provider/profile 配置，生成冻结的父子 ModelBinding 和 fingerprint。 |
| [`src/mini_agent/providers/openai_chat.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.36/src/mini_agent/providers/openai_chat.py) | 新增 | 实现 OpenAI-compatible Chat Completions 的请求、SSE 和 tool call 重组。 |
| [`src/mini_agent/providers/anthropic_messages.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.36/src/mini_agent/providers/anthropic_messages.py) | 新增 | 实现 Anthropic Messages 的 system、tool block 和流式事件转换。 |
| [`src/mini_agent/providers/http.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.36/src/mini_agent/providers/http.py) | 新增 | 用 `http.client` 提供独立连接、有界读取和 SSE 公共骨架。 |
| [`src/mini_agent/runtime.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.36/src/mini_agent/runtime.py) | 修改 | 接收归一化响应并统一记录 provider、estimated、mixed usage。 |
| [`src/mini_agent/context.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.36/src/mini_agent/context.py) | 修改 | 使用绑定 profile 的上下文窗口，并让摘要请求使用同一个 binding。 |
| [`src/mini_agent/delegation.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.36/src/mini_agent/delegation.py) | 修改 | 校验子 profile 白名单、冻结子绑定并把安全来源摘要放入结果。 |

如果想先确认文件数量和变更范围，使用：

```bash
git diff --stat v0.35..v0.36
```

这个命令只显示本课两个固定快照间的变化，帮助读者看到 provider 层、Runtime、Context 和委派入口同时变化，但控制循环没有复制出第二份。

## 版本变更定位

图例：

```text
[旧] 上一版已有    [+] 本版新增    [~] 本版修改
[C] 主要消费者     [B] 本版边界或刻意不负责
```

v0.35 的调用链可以简化为：

```text
[C] agent_loop / SubagentRunner
              │
              └─> [旧] AgentRuntime.run()
                         │
                         └─> [旧] call_llm()
                                    └─> 全局 BASE_URL/API_KEY/MODEL
```

v0.36 把配置选择和协议转换插入模型调用边界，Runtime 的下游仍然只有一种消息形状：

```text
[C] agent_loop ───────────────┐
                              ├─> [~] AgentRuntime.run()
[C] SubagentRunner ───────────┘       │
                                      ├─> [~] frozen ModelBinding
                                      │       ├─> [+] ProviderCatalog
                                      │       └─> [+] ProviderAdapter
                                      │               ├─> [+] OpenAI Chat
                                      │               └─> [+] Anthropic Messages
                                      ├─> 统一 assistant/tool 消息
                                      └─> [~] UsageMeter

[B] 本版不负责 ──> 生命周期、聚合预算、取消、并行委派和持久化委派记录
```

关键变化不是增加两个独立 Agent Loop，而是让父子在创建 Runtime 前各自冻结一个 `ModelBinding`。绑定包含 profile、provider、适配器和 usage meter；State、Context 导出和 session 只允许出现 profile、provider、protocol 和 fingerprint 等无凭据来源摘要。

## 核心概念与数据结构

### 1. provider、protocol、profile 是三件事

provider 表示“向哪个模型服务发送请求”，包含 endpoint、认证方式和超时；protocol 表示“这个服务要求怎样组织请求和响应”，本版支持 `openai_chat` 与 `anthropic_messages`；model profile 是本地配置中的可读别名，指向 provider 和真实模型标识，并声明上下文窗口、输出上限和工具能力。

例如，父子可以这样配置。下面只展示占位值，真实值应放在 `config_local.py`：

```python
PROVIDERS = {
    "gateway-openai": {
        "protocol": "openai_chat",
        "endpoint": "https://api.example.invalid/v1/chat/completions",
        "api_key": "sk-PLACEHOLDER",
    },
    "gateway-anthropic": {
        "protocol": "anthropic_messages",
        "endpoint": "https://api.example.invalid/v1/messages",
        "api_key": "key-PLACEHOLDER",
    },
}
MODEL_PROFILES = {
    "parent-default": {
        "provider_id": "gateway-openai",
        "model_id": "model-PLACEHOLDER",
        "context_window": 128_000,
        "max_output_tokens": 8_192,
    },
    "child-anthropic": {
        "provider_id": "gateway-anthropic",
        "model_id": "model-PLACEHOLDER",
        "context_window": 200_000,
        "max_output_tokens": 4_096,
    },
}
PARENT_MODEL_PROFILE = "parent-default"
SUBAGENT_MODEL_PROFILE = "child-anthropic"
SUBAGENT_ALLOWED_MODEL_PROFILES = ("child-anthropic",)
```

`delegate_task(model_profile="child-anthropic")` 只能传这个本地别名。它不能传 endpoint、API key、真实 model ID 或认证头。省略别名时使用子默认 profile，再没有子默认时使用父 profile；显式但未知或不在白名单中的别名直接失败，不会偷偷改用另一个模型。

### 2. binding 是一次运行的冻结依赖

`ProviderCatalog` 在启动时检查映射是否完整、协议是否支持、数值限制是否合理，以及父子所选模型是否支持工具。通过检查后，`bind()` 生成不可变的 `ModelBinding`。它的 fingerprint 来自非认证配置事实，不包含 API key 或额外请求头；对外只显示哈希，因此日志和结果可以说明“这次使用了哪个来源”，却不泄露 endpoint、认证头或真实 model ID。

旧配置仍然有效：如果 `PROVIDERS` 和 `MODEL_PROFILES` 都为空，catalog 会把旧三元组转换成 `legacy-default` provider/profile，并把旧的 base URL 补成 `/chat/completions`。新旧配置不能只定义一半，否则会在启动时报告歧义。

### 3. 适配器只翻译协议

适配器收到统一的消息列表和工具 schema，返回：

```python
ProviderResponse(
    message={"role": "assistant", "content": "...", "tool_calls": [...]},
    finish_reason="tool_use",
    usage=ProviderUsage(input_tokens=12, output_tokens=8, source="provider"),
)
```

OpenAI 适配器重组 SSE 中按 index 分片的 content 和多个 tool call；在 `[DONE]` 之前中断，或 arguments 不是完整 JSON object 时，返回协议错误而不是让 executor 尝试执行半个调用。Anthropic 适配器把 system 提到顶层，把 assistant tool call 转成 `tool_use` block，把内部 `role=tool` 合并成相邻 user 消息中的 `tool_result` block；流式 `input_json_delta` 完成前同样拒绝。

Runtime 不知道这些 provider 原生字段。它仍然追加一个 assistant 消息，然后为每个 tool call 按模型顺序追加唯一的 `role=tool` 结果；工具执行、权限、Plan 和完成判定仍然在现有 Runtime/policy 边界内。

## 关键流程

父子跨协议运行时，消息和资源边界如下：

```text
启动 CLI
  → load_provider_catalog()
  → 冻结 parent binding
  → 创建父 Context / Registry / Runtime

父 Runtime
  → parent binding → OpenAI Chat Completions
  → 统一 assistant(tool_calls)
  → delegate_task(model_profile="child-anthropic")
       → 白名单校验（HTTP 之前）
       → 冻结 child binding → Anthropic Messages
       → 独立 child Context / usage meter
       → 一个结构化 SubagentResult
  → 父 Context 只收到一个对应的 role=tool 结果
```

摘要是普通的模型调用，不是免费辅助步骤。父 Context 的压缩摘要通过父 binding，子 Context 的摘要通过子 binding；两者各自计入自己的 usage meter。provider 返回完整 usage 时优先使用它，缺失时用保守估算；同一运行中两种来源混合时标记为 `mixed`。

请求层每次使用独立 `http.client` connection，并显式发送 `Accept-Encoding: identity`。适配器不自动重试，也不跨 provider fallback。这样一次请求失败时，失败原因不会隐式改变接收方，也不会造成重复计费或重复工具调用。

## 实现拆解

### 配置和绑定

`providers/catalog.py` 负责把本地映射转换为 `ProviderConfig`、`ModelProfile` 和 `ModelBinding`。它只把 binding 注入运行时对象；`DelegatedTask.to_dict()`、`SubagentResult.to_json()`、State 快照和 session 只保留安全的 profile/ref 信息。恢复会话时重新读取当前本地配置，不从 session 恢复凭据或活动适配器。

### OpenAI-compatible Chat Completions

`providers/openai_chat.py` 负责 Bearer 认证、工具 schema、非流式 JSON 和 SSE。流式工具调用按照 index 建立累积槽位，再校验每个 ID、函数名和 JSON arguments。content callback 只观察已经收到的文本，不改变 Runtime 的消息提交顺序。

### Anthropic Messages

`providers/anthropic_messages.py` 负责 `x-api-key`、`anthropic-version`、顶层 system 和 block 转换。连续的内部 tool result 会合并到一个 user 消息中；响应中的 text 与 tool-use block 会重新归一为内部 assistant message。`message_start`、`content_block_*`、`message_delta` 和 `message_stop` 必须形成完整事件序列，错误事件、未知事件、缺少 block ID 或未闭合 JSON 都会停止本次响应。

### Runtime、Context 和委派

`AgentRuntime` 继续是唯一的模型—工具—观察循环，只增加统一 usage 字段和可选 binding ref。`ContextBudget` 从 profile 的 context window 和 output reserve 构造；`ContextManager` 接收显式 summarizer，避免摘要偷偷调用父全局模型。`DelegationManager` 在启动子 Runner 前解析白名单并冻结 binding，子 Runner 仍只有四个只读工具、depth=1 和独立 State/Context。

## 为什么这样设计

把 provider 选择放在 catalog，把协议翻译放在 adapter，可以让 Runtime 继续维护一套工具闭合和完成判定规则。另一种做法是在 `run()` 里判断“如果是 Anthropic 就……”，短期少几个文件，长期会把每种协议的消息细节、错误处理和流式状态带进核心循环，难以审计。

本版坚持显式 profile 白名单和不自动 fallback。fallback 看似提高可用性，却会改变数据流向、权限环境和计费对象；尤其是子代理只能使用父配置明确批准的本地别名。配置错误在首次 LLM 请求前暴露，也让调用者能明确修正配置。

代价是本地配置需要多写一层 provider/profile，适配器也必须严格处理两套流式协议；旧三元组仍保留兼容路径，避免一次升级破坏已有调用方。本版刻意不加入生命周期、聚合预算、取消、并行委派或持久化委派记录，这些不属于统一协议适配的最小边界。

## 设计边界

- provider 原生消息不会进入 Runtime、Context、ToolExecutor 或 State；内部只使用归一化 assistant/tool 结构。
- 适配器不执行工具、不修改 State、不判断完成、不重试，也不读取全局当前模型。
- 父子 binding、usage meter、Context 和 history 独立；父仍独占修改、权限、Plan、generation、verification 和完成判定。
- State、session、Trace、工具结果和用户可见错误不包含凭据、真实 endpoint、认证头或真实 model ID，只允许安全 profile/ref/fingerprint。
- 旧配置继续映射到 `legacy-default`；新配置必须完整定义 provider 与 profile，不能产生模糊绑定。
- 本版仍是单个同步 depth=1 只读 Subagent；没有聚合预算、后台取消、多子代理并行或 `DelegationRecord`。

## 本版特性、下一课与代码索引

本版完成多 provider、两种首批协议、父子独立 profile、统一响应和 usage 归属；父子仍进入同一个 `AgentRuntime.run()`，每个 tool call 仍有唯一且按序的 `role=tool` 结果。旧三元组兼容，未授权 profile 在联网前拒绝，摘要调用不会成为免费调用。

下一课会在不改变子代理只读边界的前提下，为委派增加生命周期和父任务聚合预算。本课源码入口见上面的 `v0.36` 固定链接；配置入口是 [`src/mini_agent/config.py`](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.36/src/mini_agent/config.py)。

运行离线回归可观察到旧测试与 provider/catalog 测试同时通过；受限环境若不能绑定本地 TCP 端口，HTTP 集成测试会被安全地跳过，不会访问真实服务：

```bash
PYTHONPATH=src python -m pytest -q
```

相关计划和运行约束见 [`docs/plans/subagent-delegation-plan.md`](../plans/subagent-delegation-plan.md)、[`docs/operation/manual.md`](../operation/manual.md) 与 [`AGENTS.md`](../../AGENTS.md)。
