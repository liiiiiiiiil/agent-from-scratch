<div align="center">

# agent-from-scratch

### 逐步生长的编程 Agent：从零开始构建一个能干活的 AI Agent

从最小的 agent loop 开始，按 Git tag 和能力阶段逐步加入工具、安全、上下文和可靠执行能力。

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)](https://www.python.org/) [![Dependencies](https://img.shields.io/badge/core%20dependencies-zero-green)](#快速开始) [![Versions](https://img.shields.io/badge/versions-v0.01%E2%86%92ongoing-orange)](#学习路径) [![License](https://img.shields.io/badge/license-MIT-lightgrey)](./LICENSE)

**[English](./README_EN.md)** · **中文**

</div>

---

适合想用 Python 标准库理解 LLM agent 如何运行的开发者。每课聚焦一个版本相对上一版新增的核心概念，源码、diff 和设计取舍都可追溯。

**当前状态**：主线代码和教程已完成 `v0.35`（第 35 课：共享父子运行循环），阶段十继续推进。教程以默认分支的 `docs/tutorials/` 为准；运行某课时再切换该课声明的代码 tag。

快速入口：[运行](#快速开始) · [学习路径](#学习路径) · [学习指南](./docs/tutorials/README.md) · [完整手册](./docs/operation/manual.md)

## 学习路径

<table width="100%">
  <thead>
    <tr><th>版本</th><th>主题</th><th>简介</th></tr>
  </thead>
  <tbody>
    <tr><th colspan="3"><a id="stage-1"></a>阶段一 · 理解 Agent Loop</th></tr>
    <tr><td><strong>v0.01</strong></td><td><a href="./docs/tutorials/01-minimal-loop.md">最简 agent loop</a></td><td>建立最小对话循环，理解请求、回复与结束条件。</td></tr>
    <tr><th colspan="3"><a id="stage-2"></a>阶段二 · 工具与安全</th></tr>
    <tr><td><strong>v0.02</strong></td><td><a href="./docs/tutorials/02-first-tool.md">第一个工具</a></td><td>接入 calculate，跑通 function calling 基本协议。</td></tr>
    <tr><td><strong>v0.03</strong></td><td><a href="./docs/tutorials/03-file-tools.md">文件读写工具</a></td><td>让 Agent 读取和写入真实项目文件。</td></tr>
    <tr><td><strong>v0.04</strong></td><td><a href="./docs/tutorials/04-permission-gate.md">权限闸门</a></td><td>为有副作用的工具加入 allow、deny、ask 三态授权。</td></tr>
    <tr><th colspan="3"><a id="stage-3"></a>阶段三 · Mini Agent 里程碑</th></tr>
    <tr><td><strong>v0.05</strong></td><td><a href="./docs/tutorials/05-streaming.md">流式输出</a></td><td>逐块接收并显示 LLM 回复，改善交互反馈。</td></tr>
    <tr><td><strong>v0.06</strong>（补丁 <code>v0.06.1</code>）</td><td><a href="./docs/tutorials/06-concurrent-tool-calls.md">并发 tool_calls</a></td><td>并发执行同一轮的多个工具调用，减少等待时间；补丁修正多轮上下文状态契约。</td></tr>
    <tr><td><strong>v0.07</strong></td><td><a href="./docs/tutorials/07-system-prompt.md">系统提示词工程化</a></td><td>分层组织身份、规则和环境信息，稳定 Agent 行为。</td></tr>
    <tr><td><strong>v0.08</strong></td><td><a href="./docs/tutorials/08-file-operations.md">文件操作补全</a></td><td>补齐目录列举、精确编辑和正则搜索能力。</td></tr>
    <tr><td><strong>v0.09</strong></td><td><a href="./docs/tutorials/09-permission-upgrade.md">权限系统升级</a></td><td>按工具和路径或命令模式细粒度匹配权限规则。</td></tr>
    <tr><td><strong>v0.10</strong></td><td><a href="./docs/tutorials/10-shell-execution.md">shell 执行</a></td><td>执行命令并处理超时、输出截断和命令级授权。</td></tr>
    <tr><th colspan="3"><a id="stage-4"></a>阶段四 · 上下文管理</th></tr>
    <tr><td><strong>v0.11</strong></td><td><a href="./docs/tutorials/11-context-architecture.md">上下文架构</a></td><td>将持久执行状态与可裁剪的对话上下文分离。</td></tr>
    <tr><td><strong>v0.12</strong></td><td><a href="./docs/tutorials/12-token-budget-trimming.md">预算与裁剪</a></td><td>估算 token，并按完整对话轮次安全裁剪历史。</td></tr>
    <tr><td><strong>v0.13</strong>（补丁 <code>v0.13.1</code>、<code>v0.13.2</code>）</td><td><a href="./docs/tutorials/13-context-compaction.md">上下文压缩</a></td><td>用历史摘要和结构化状态降低长任务遗忘；补丁增加可观测性与任务边界隔离。</td></tr>
    <tr><th colspan="3"><a id="stage-5"></a>阶段五 · 项目感知与任务编排</th></tr>
    <tr><td><strong>v0.14</strong></td><td><a href="./docs/tutorials/14-project-instructions.md">项目级指令</a></td><td>自动发现并注入适用的 <code>AGENTS.md</code> 指令。</td></tr>
    <tr><td><strong>v0.15</strong></td><td><a href="./docs/tutorials/15-task-state.md">任务清单与状态</a></td><td>用结构化任务清单与显式状态跟踪多步任务进度。</td></tr>
    <tr><td><strong>v0.16</strong>（补丁 <code>v0.16.1</code>）</td><td><a href="./docs/tutorials/16-plan-driven-execution.md">计划驱动执行</a></td><td>通过计划、执行、观察、重排和验证形成闭环；补丁收窄完成提醒的重开规则。</td></tr>
    <tr><th colspan="3"><a id="stage-6"></a>阶段六 · 可靠执行</th></tr>
    <tr><td><strong>v0.17</strong></td><td><a href="./docs/tutorials/17-failure-model.md">失败模型</a></td><td>记录 generation、执行尝试与可审计失败事实。</td></tr>
    <tr><td><strong>v0.18</strong>（补丁 <code>v0.18.1</code>）</td><td><a href="./docs/tutorials/18-recovery-policy.md">受限恢复策略</a></td><td>用受限动作恢复失败并隔离 generation；补丁修正边界一致性。</td></tr>
    <tr><td><strong>v0.19</strong></td><td><a href="./docs/tutorials/19-checkpoint-rollback.md">单文件检查点与回滚</a></td><td>为单文件写入保存前镜像，在冲突检测下原子恢复，并保持恢复状态可见、可验证。</td></tr>
    <tr><td><strong>v0.20</strong></td><td><a href="./docs/tutorials/20-repair-loop.md">修复循环</a></td><td>把失败、诊断、受限恢复和独立验证串成有上限的阶段化循环。</td></tr>
    <tr><td><strong>v0.21</strong></td><td><a href="./docs/tutorials/21-trace-replay.md">任务轨迹回放</a></td><td>按 generation 只读回放 Todo、执行、失败、恢复、验证和终态的因果链。</td></tr>
    <tr><th colspan="3"><a id="stage-7"></a>阶段七 · 结构化计划</th></tr>
    <tr><td><strong>v0.22</strong></td><td><a href="./docs/tutorials/22-plan-contract.md">结构化计划合同</a></td><td>用不可变 revision 保存计划结构，用独立 progress event 推进步骤状态，并把计划执行视图注入上下文。</td></tr>
    <tr><td><strong>v0.23</strong></td><td><a href="./docs/tutorials/23-plan-mode-handoff.md">只读规划与用户交接</a></td><td>复杂任务先只读调查；计划模式提交后等待用户决定，批准仍不绕过工具授权。</td></tr>
    <tr><td><strong>v0.24</strong></td><td><a href="./docs/tutorials/24-replanning-policy.md">证据驱动重规划与停滞收口</a></td><td>执行中引用真实 failure 或观察修订计划；重复工具回合先提醒，再有界阻塞。</td></tr>
    <tr><td><strong>v0.25</strong></td><td><a href="./docs/tutorials/25-plan-trace-evaluation.md">计划轨迹回放与验收</a></td><td>按顺序事件连接 generation、revision、触发事实、用户决定、执行与独立验证。</td></tr>
    <tr><th colspan="3"><a id="stage-8"></a>阶段八 · 后台进程与任务边界</th></tr>
    <tr><td><strong>v0.26</strong></td><td><a href="./docs/tutorials/26-background-process-boundaries.md">后台进程启动与任务边界</a></td><td>启动长期命令后继续工作，排空有界输出，按任务归属同步退出，并在交接和任务切换时清理进程。</td></tr>
    <tr><td><strong>v0.27</strong></td><td><a href="./docs/tutorials/27-process-observation.md">观察后台进程</a></td><td>跨轮次查询状态、读取新增输出，并在没有新消息时有界等待和交接。</td></tr>
    <tr><td><strong>v0.28</strong></td><td><a href="./docs/tutorials/28-process-control.md">控制后台进程</a></td><td>按任务授权终止或强制结束进程，确认退出后重新验证。</td></tr>
    <tr><td><strong>v0.29</strong></td><td><a href="./docs/tutorials/29-interactive-process.md">驱动等待输入的后台进程</a></td><td>显式开启有界管道 stdin，写入少量 UTF-8 文本并发送 EOF，同时保持授权、脱敏、清理和独立验证边界。</td></tr>
    <tr><th colspan="3"><a id="stage-9"></a>阶段九 · 会话持久化与安全交接</th></tr>
    <tr><td><strong>v0.30</strong></td><td><a href="./docs/tutorials/30-session-persistence.md">会话持久化与安全点</a></td><td>通过 /save 显式开启本地保存，在完整安全点原子更新 active 会话，退出或任务切换时在进程清理后提交 clean；本版只能校验文件，不能恢复任务。</td></tr>
    <tr><td><strong>v0.31</strong></td><td><a href="./docs/tutorials/31-safe-resume.md">从完整安全点恢复会话</a></td><td>用 --resume 检查 schema 2、工作区清单和 clean 交接，在新进程重建运行时；旧验证、进程句柄和 checkpoint 回滚资格不会被继承。</td></tr>
    <tr><td><strong>v0.32</strong></td><td><a href="./docs/tutorials/32-durable-tool-boundaries.md">持久化工具执行边界</a></td><td>在同一 schema 3 session 文件中按 handler 准入、单 call 结果和整轮 complete 提交 State 与 Context；半轮只用于诊断，暂不续跑。</td></tr>
    <tr><td><strong>v0.33</strong></td><td><a href="./docs/tutorials/33-crash-recovery.md">崩溃恢复与不确定副作用交接</a></td><td>从 active pending 边界派生新 session，逐项区分未执行与不确定事实；不自动重放，先只读调查并由用户决定继续或阻塞。</td></tr>
    <tr><th colspan="3"><a id="stage-10"></a>阶段十 · 受控子代理委派</th></tr>
    <tr><td><strong>v0.34</strong></td><td><a href="./docs/tutorials/34-minimal-delegation.md">最小受控子代理委派</a></td><td>父 Agent 同步委派单个、单层、只读 Subagent；用隔离 Context、能力过滤、scope 和结构化结果合同收集调查材料。</td></tr>
    <tr><td><strong>v0.35</strong></td><td><a href="./docs/tutorials/35-shared-agent-runtime.md">共享父子运行循环</a></td><td>父 Agent 与只读 Subagent 共用唯一的 AgentRuntime.run() 协议骨架，差异由策略、Context、工具面和预算表达。</td></tr>
    <tr><td>v0.36–v0.39</td><td>模型、生命周期、并行与持久交付</td><td>后续版本再加入多 provider、生命周期与聚合预算、有界并行和持久化委派记录。</td></tr>
  </tbody>
</table>

完成 `v0.10` 后，Agent 已能读取项目、搜索和修改文件、执行命令、运行测试，并通过权限机制控制高风险操作。

## 快速开始

要求：Python 3.10+；准备一个可访问的 LLM 网关。Bash/zsh：

```bash
git clone https://github.com/liiiiiiiiil/agent-from-scratch.git
cd agent-from-scratch
cp src/mini_agent/config_example.py src/mini_agent/config_local.py
# 编辑 config_local.py，填入 BASE_URL / API_KEY / MODEL；它不会进 git
python -m pip install -e .
python -m mini_agent "帮我算一下 123 * 456"
```

可选：`python -m pip install -e '.[interactive]'` 启用多行终端输入。命令行参数只是首条任务，处理后仍进入交互循环；用空行、`exit`、`quit` 或 EOF 退出。PowerShell、免安装运行和配置细节见[完整手册](./docs/operation/manual.md)。

## 项目结构

```text
src/mini_agent/   核心运行时、配置、状态、权限、上下文和工具
tests/             测试与 smoke test
docs/tutorials/    分阶段、按版本的教程
docs/operation/    最新版运行手册
docs/plans/        路线图与功能计划
docs/governance/   写作规范与决策记录
examples/          示例输入输出文件
```

## 设计与文档

- 核心 LLM 调用、agent loop、工具、权限和状态只用 Python 标准库；外围交互增强为可选依赖。
- 工具层处理 handler 错误并把结果回灌模型；核心 loop 保持清晰，具体约束见 [`AGENTS.md`](./AGENTS.md)。
- [学习指南](./docs/tutorials/README.md) · [完整手册](./docs/operation/manual.md) · [上下文架构](./docs/operation/context-architecture.md) · [路线图](./docs/plans/teaching-repo-plan.md) · [CHANGELOG](./CHANGELOG.md)

## 贡献

欢迎提交 Issue / PR。新增课程前请读[教程作者规范](./docs/governance/tutorial-authoring.md)、[主 README 编写规范](./docs/governance/readme-authoring.md)和 `AGENTS.md`；作者入口与模板见[学习指南](./docs/tutorials/README.md)。

## License

MIT — 见 [LICENSE](./LICENSE)

<!-- 关键词 / Keywords: agent tutorial, agent 教程, LLM agent, coding agent, Python agent, function calling, 从零构建 agent, AI agent, agent loop, tool calling -->
