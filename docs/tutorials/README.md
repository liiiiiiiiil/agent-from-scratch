# 如何学习本教程

本目录是默认分支持续修订的**权威教程**。课程号表示学习顺序，包版本表示发布版本，Git tag 表示代码快照；第 43 课对应最小 stdio MCP Client。每课开头会声明自己的 tag 和相邻 diff 基线。

当前最新课程是[第 43 课：最小 stdio MCP 客户端](43-stdio-mcp-client.md)，属于阶段十二已实现的独立 MCP Client 切片。它解释 Client 与本地 Server 如何通过两根管道交换 JSON-RPC 消息、完成固定握手、翻页列工具并在人工确认后调用一次工具；v0.44–v0.46 仍在规划。

## 前置条件

- 阅读课程需要基础 Python 和 Git；运行需要 LLM 的示例时，再准备可访问的 LLM 网关。
- 阅读[主 README 的完整学习路径](../../README.md#学习路径)，或按下面三条路线选择入口。
- 历史 tag 可能没有当前的 `docs/tutorials/`，也可能仍使用旧的配置加载方式；按课程正文准备环境，不要把当前主线的 `config_local.py` 机制套到 `v0.01`，真实密钥也不要写入受跟踪配置。

## 三条学习路线

1. **完整路线**：从[阶段一](../../README.md#stage-1)开始，依次读到[阶段十二](../../README.md#stage-12)的第 43 课。
2. **先做出 Mini Agent**：完成[阶段一](../../README.md#stage-1)至[阶段三](../../README.md#stage-3)，再做阶段三的实践任务；`v0.10` 后已有基础编程任务闭环。
3. **按能力查阅**：从[主 README 的课程表](../../README.md#学习路径)进入指定课程，再回到上下一个阶段补齐前置知识。

每课按“问题 → diff → 运行 → 观察 → 取舍”阅读：先说明上一版的限制，再用 tag 固定的源码或真实 diff 定位变化，运行课程命令，观察结果，最后理解边界和未解决的问题。

## 环境准备

一次性克隆仓库，并按当前课程正文选择安装或免安装方式：

```bash
git clone https://github.com/liiiiiiiiil/agent-from-scratch.git
cd agent-from-scratch
```

主线通常使用 `src/mini_agent/config_local.py` 保存本地配置；历史课程以各自正文和 tag 为准。课程命令默认使用 Bash/zsh；免安装运行时在命令前加 `PYTHONPATH=src`。PowerShell 先执行 `$env:PYTHONPATH="src"`，再运行对应的 `python ...` 命令。

## 版本工作流

网页或默认分支阅读最新教程；本地只为运行课程代码切到该课声明的 tag：

```bash
git switch --detach v0.08
git diff v0.07..v0.08
# 按课程正文运行并观察
git switch -                 # 回到切换前的分支
```

`git tag` 可查看实际存在的快照。补丁 tag 也按课程声明操作，例如 `v0.18` → `v0.18.1`；不要把课程号、包版本和 tag 当成同一个概念。

## 阶段三实践任务

完成 `v0.10` 后，让 Agent 检查一个失败的测试、定位原因、修改代码、运行测试并总结结果，用它验证基础工作闭环。

## 给学习者与作者

遇到运行问题，先查[完整使用手册](../operation/manual.md)，仍无法判断时再提 Issue。新增课程请从 [`_template.md`](_template.md) 开始，并阅读[教程作者规范](../governance/tutorial-authoring.md)与[主 README 编写规范](../governance/readme-authoring.md)；作者验收命令和提交规范以治理文档为准。
