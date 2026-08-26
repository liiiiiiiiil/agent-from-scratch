# AGENTS.md

## 项目定位

`mini_agent` 是一个**逐步生长的编程 agent**：从最小可用的 agent loop 起步，按需增加工具与能力，目标是能独立完成基础的编程任务（读写改文件、跑命令、跑测试、简单多步任务）。

设计原则：
- **零第三方依赖**（仅 Python 标准库），保持自包含、易部署。
- **渐进式生长**：每次只加刚好够用的能力，避免过度设计。新功能先在 `AGENTS.md` 记下意图，再落地代码。
- **核心 loop 保持清晰**：agent loop 不加 try/except 兜底，工具失败直接抛异常——保持主路径可读。复杂容错按需在工具层或执行器层引入。

## 当前架构

标准 Python `src/` 包布局：

```
mini_agent/
├── pyproject.toml          # 项目元数据（零第三方依赖）
├── README.md
├── .gitignore
├── src/mini_agent/
│   ├── __init__.py         # 包入口
│   ├── __main__.py         # CLI 入口：python -m mini_agent
│   ├── agent.py            # agent loop：call_llm + agent_loop
│   ├── config.py           # BASE_URL/API_KEY/MODEL/MAX_ITERATIONS（硬编码）
│   ├── permission.py       # 权限闸门：allow/deny/ask 三态
│   └── tools/
│       ├── __init__.py     # registry + executor 实例
│       ├── base.py         # Tool / ToolRegistry / ToolExecutor
│       ├── file.py         # read_file / write_file
│       └── calc.py         # calculate
├── tests/
│   └── test_tools.py       # smoke test
├── examples/               # 示例 IO 文件
└── doc/
    ├── README.txt          # 文档目录说明
    ├── governance/         # 治理文档：约束、规范、决策记录
    ├── plans/               # 计划文档：路线图、功能计划、任务拆解
    └── operation/          # 操作文档：运行手册、使用指南
        └── manual.md       # 操作手册
```

- LLM 调用：`http.client` 流式，OpenAI function calling 协议（`tools` 参数）。
- 工具：`read_file` / `write_file` / `calculate`。`write_file` 走 ASK 权限。
- 迭代上限 `MAX_ITERATIONS = 10`（硬编码，长任务可能静默截断，后续需调）。
- 包未 pip install 时需 `PYTHONPATH=src`；`pip install -e .` 后可免。

## 路线图

按"能完成基础编程工作"倒推，待补能力（优先级递减，按需逐个加）：

- [ ] **文件操作工具补全**：`list_dir`、`edit_file`（精准替换而非整文件重写）、`grep`/搜索。不改文件没法干活。
- [ ] **shell 执行工具**：跑测试、跑脚本、git 操作。权限从严（ASK 或白名单命令）。
- [ ] **系统提示词工程化**：当前 system prompt 仅一行。需补工作目录约定、工具使用规范、迭代规划引导——成本最低收益最高。
- [ ] **迭代上限 + 上下文管理**：10 轮对编程任务不够；直接调高会爆上下文，需配 message 裁剪/摘要策略。
- [ ] **多步任务规划**：复杂任务先 plan 后 execute（`plan` 工具或 prompt 引导）。

> 每加一项，在此打勾并在"当前架构"更新对应模块说明。

## 运行

```bash
python -m mini_agent "你的任务"
# 或交互式输入
python -m mini_agent
```

包未安装时需设 `PYTHONPATH=src`（Windows 用 `$env:PYTHONPATH="src"`）；`pip install -e .` 后可从任意目录直接运行。

示例文件在 `examples/`，工具的相对路径（如 `examples/input.txt`）需从项目根目录 `mini_agent/` 起算：

```bash
# 未安装时
$env:PYTHONPATH="src"; python -m mini_agent "读取 examples/input.txt"
# 安装后
pip install -e .
python -m mini_agent "读取 examples/input.txt"
```

## 关键约束（踩坑备忘，勿违反）

- **必须用 `http.client`，不能用 `requests`/`urllib`**：`your-gateway-host` 网关对 `Accept-Encoding: gzip` 响应异常返回 502。`call_llm` 里显式设 `Accept-Encoding: identity` 绕过。换 HTTP 客户端会重新踩坑。
- 配置（`BASE_URL`/`API_KEY`/`MODEL`）硬编码在文件顶部，不从环境变量读。
- 工具调用用 OpenAI function calling 协议（`tools` 参数）。`EB-GLM-5.2` 已验证支持。
- 工具签名为单参数字符串、返回字符串。`write_file` 用 `"路径\n内容"` 格式传参。
- agent loop 无 try/except 兜底，工具失败会直接抛异常终止——这是有意为之，保持核心逻辑清晰。
- 迭代上限 `MAX_ITERATIONS = 10`（硬编码）。超限直接返回 `"达到最大迭代次数"`，不报错——长任务可能静默截断。

## 已知行为

- 模型可在同一轮返回多个 `tool_calls`（并发），代码串行执行。无依赖的工具调用会并发发起，有依赖的分轮。
- 结束条件：LLM 回复不含 `tool_calls` 即视为完成。
