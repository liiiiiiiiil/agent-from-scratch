# 第 X 课：主题（v0.X）

上一课：[上一课](上一课.md) · [教程总览](README.md) · 下一课：v0.Y（规划中）

> 代码快照：`v0.X` · 相邻差异：`v0.X-1..v0.X` · 命令环境：Bash/zsh

> 运行要求：Python 3.10+。如历史 `pyproject.toml` 元数据与源码语法不一致，在这里明确说明。

## 本课目标

说明上一版限制、本版目标，以及读者完成本课后应能解释或完成的事情。

## 前置条件与版本切换

列出环境、上一课和可复现的版本切换命令。

```bash
git checkout v0.X-1
git diff --stat v0.X-1..v0.X
git diff v0.X-1..v0.X -- <本版相关文件>
git checkout v0.X
```

## 新增与改动文件

| 文件 | 变化 | 作用 |
|---|---|---|
| `src/...` | ... | ... |

## 为什么需要本版

先描述上一版的具体问题，再给出本版核心概念、不变量和取舍。

## 关键流程

用代码块或短流程图展示入口、主路径和失败路径。

## 实现拆解

按入口、主路径、辅助函数和失败/降级路径解释实现，并链接源码。

## 设计选择与边界

说明设计理由、代价，以及本版刻意不解决的事情。

## 最小示例与典型场景

提供可运行示例；涉及运行时流程时补充可观察输出。命令块声明 shell；命令行参数称为“命令行首条任务”，并说明程序随后仍进入交互循环。

## 测试与验收

```bash
PYTHONPATH=src python -m pytest -q tests/test_<本版>.py
PYTHONPATH=src python tests/test_<本版>.py
```

至少保留一条不需要 API_KEY 或网络、只依赖标准库的验收路径。列出与本版行为直接对应的验收点，不写固定测试数量。

## 本版特性、下一课与代码索引

总结本版独有能力，链接下一课，并列出完整实现和测试文件。源码链接必须固定到本课 tag：

```markdown
[src/mini_agent/example.py](https://github.com/liiiiiiiiil/agent-from-scratch/blob/v0.X/src/mini_agent/example.py)
```
