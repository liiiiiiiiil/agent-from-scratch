<div align="center">

# agent-from-scratch

### A coding agent that grows step by step · Build a working AI agent from scratch in incremental versions

逐步生长的编程 Agent —— 持续迭代，从零构建一个能干活的 AI Agent。

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/dependencies-zero-green)](#quick-start)
[![Versions](https://img.shields.io/badge/versions-v0.01%E2%86%92ongoing-orange)](#learning-path)
[![License](https://img.shields.io/badge/license-MIT-lightgrey)](./LICENSE)

**English** · **[中文](./README.md)**

</div>

---

> **What this is**: A tutorial repo sliced by git tags and organized by capability stages. Starting from a minimal agent loop (v0.01, no tools, pure conversation), each version **introduces exactly one new concept**, growing all the way to a Mini Agent that can read/write files, run commands, and run tests (incremental versions, no fixed endpoint).
>
> **Who it's for**: Developers who want to understand how an LLM agent actually ticks. No frameworks, no LangChain — just the Python standard library, built from scratch.

> **Current status**: The code and tutorials are up to `v0.17` (Failure Model).

> **Reading model**: The authoritative tutorials are the current files under `docs/tutorials/` on the default branch. Each lesson names one code tag and links source files to that immutable snapshot. Read the current tutorial online, then check out its tag locally to run the code. Historical tags are not rewritten; tutorial paths inside `v0.01` through `v0.06` are historical copies.

## Why learn agents with this repo

- **Zero third-party dependencies** — Pure Python standard library throughout (`http.client` / `json` / `concurrent.futures`). No LangChain, no requests. Self-contained, every line is readable.
- **Version slices, one concept per version** — `git diff v0.01..v0.02` is the entire change for "add a tool". Diffs are readable, cognitive load is low. Versions keep incrementing with no fixed cap.
- **A real, runnable agent** — Not a toy demo: supports function calling, streaming output, a permission gate, concurrent tool calls. It can actually read/write files and run commands.
- **Companion tutorials** — Tutorials are organized by learning stage, with one tutorial doc per version (`docs/tutorials/`) explaining *why* it's designed this way, not just pasting code.
- **Progressive-growth philosophy** — Watch an agent project grow from a minimal loop to a working Mini Agent, with every trade-off documented.

## Quick Start

```bash
# 1. Clone
git clone https://github.com/liiiiiiiiil/agent-from-scratch.git
cd agent-from-scratch

# 2. Configure the LLM gateway
cp src/mini_agent/config_example.py src/mini_agent/config_local.py
#    edit config_local.py with your BASE_URL / API_KEY / MODEL (this file is gitignored)

# 3. Install (recommended)
pip install -e .

# 4. Run
python -m mini_agent "calculate 123 * 456"
python -m mini_agent             # or interactive input
```

To run without installing, use this on Linux/macOS:

```bash
PYTHONPATH=src python -m mini_agent "calculate 123 * 456"
```

In PowerShell:

```powershell
$env:PYTHONPATH="src"
python -m mini_agent "calculate 123 * 456"
```

> Requires Python 3.10+ (the project standardizes on Python 3.10 and newer).

## Learning Path

Tutorials are organized by capability stage, while versions remain the basic units of code evolution and Git tags. Start at Stage 1 and continue through Stage 3; after `v0.10`, you have a Mini Agent that can handle basic coding tasks. Continue with Stage 4 to learn context management. **This is the core learning path.**

<table>
  <thead>
    <tr><th>Version</th><th>Topic</th><th>Overview</th></tr>
  </thead>
  <tbody>
    <tr><th colspan="3">Stage 1 · Understand the Agent Loop</th></tr>
    <tr><td><strong>v0.01</strong></td><td><a href="./docs/tutorials/01-minimal-loop.md">Minimal agent loop</a></td><td>Build the smallest conversation loop and its completion conditions.</td></tr>
    <tr><th colspan="3">Stage 2 · Tools and Safety</th></tr>
    <tr><td><strong>v0.02</strong></td><td><a href="./docs/tutorials/02-first-tool.md">First tool</a></td><td>Add calculate and walk through the function-calling protocol.</td></tr>
    <tr><td><strong>v0.03</strong></td><td><a href="./docs/tutorials/03-file-tools.md">File read/write tools</a></td><td>Let the Agent read and write files in a real project.</td></tr>
    <tr><td><strong>v0.04</strong></td><td><a href="./docs/tutorials/04-permission-gate.md">Permission gate</a></td><td>Gate side-effecting tools with allow, deny, and ask actions.</td></tr>
    <tr><th colspan="3">Stage 3 · Mini Agent Milestone</th></tr>
    <tr><td><strong>v0.05</strong></td><td><a href="./docs/tutorials/05-streaming.md">Streaming output</a></td><td>Receive and display LLM responses incrementally for better feedback.</td></tr>
    <tr><td><strong>v0.06</strong></td><td><a href="./docs/tutorials/06-concurrent-tool-calls.md">Concurrent tool_calls</a></td><td>Run multiple tool calls from one turn concurrently.</td></tr>
    <tr><td><strong>v0.07</strong></td><td><a href="./docs/tutorials/07-system-prompt.md">System prompt engineering</a></td><td>Layer identity, rules, and environment details into a stable prompt.</td></tr>
    <tr><td><strong>v0.08</strong></td><td><a href="./docs/tutorials/08-file-operations.md">File operations complete</a></td><td>Add directory listing, precise edits, and regular-expression search.</td></tr>
    <tr><td><strong>v0.09</strong></td><td><a href="./docs/tutorials/09-permission-upgrade.md">Permission system upgrade</a></td><td>Match permissions by tool and file-path or command pattern.</td></tr>
    <tr><td><strong>v0.10</strong></td><td><a href="./docs/tutorials/10-shell-execution.md">Shell execution</a></td><td>Run commands with timeouts, output limits, and command-level authorization.</td></tr>
    <tr><th colspan="3">Stage 4 · Context Management</th></tr>
    <tr><td><strong>v0.11</strong></td><td><a href="./docs/tutorials/11-context-architecture.md">Context architecture</a></td><td>Separate durable execution state from the trimmable conversation context.</td></tr>
    <tr><td><strong>v0.12</strong></td><td><a href="./docs/tutorials/12-token-budget-trimming.md">Budget and trimming</a></td><td>Estimate tokens and safely trim complete conversation rounds.</td></tr>
    <tr><td><strong>v0.13</strong></td><td><a href="./docs/tutorials/13-context-compaction.md">Context compaction</a></td><td>Use historical summaries and structured state to reduce long-task forgetting.</td></tr>
    <tr><td><strong>v0.13.1</strong></td><td><a href="./docs/tutorials/13-context-compaction.md#v0131-context-observability">Context Observability addendum</a></td><td>Expose token statistics, trim/compact events, and structured snapshots.</td></tr>
    <tr><th colspan="3">Stage 5 · Project-Aware Task Orchestration</th></tr>
    <tr><td><strong>v0.14</strong></td><td><a href="./docs/tutorials/14-project-instructions.md">Project Instructions</a></td><td>Discover and inject project-level AGENTS.md instructions automatically.</td></tr>
    <tr><td><strong>v0.15</strong></td><td><a href="./docs/tutorials/15-task-state.md">Todo / Task State</a></td><td>Track multi-step progress with explicit Todo state.</td></tr>
    <tr><td><strong>v0.16</strong></td><td><a href="./docs/tutorials/16-plan-driven-execution.md">Plan-driven Execution</a></td><td>Close the execution loop with plans, verification evidence, and failure states.</td></tr>
    <tr><td><strong>v0.17</strong></td><td><a href="./docs/tutorials/17-failure-model.md">Failure Model</a></td><td>Audit generations, attempts, and structured failure facts.</td></tr>
    <tr><td>Later versions</td><td>Added as needed</td><td>Continue expanding recovery, memory, sandboxing, and related capabilities.</td></tr>
  </tbody>
</table>

After `v0.10`, the Agent can inspect a project, search and modify files, run commands, run tests, and control high-risk operations through permissions. This is the repo's first stage milestone.

**How to learn by version:**

```bash
git tag                    # list all versions
git checkout v0.01          # switch to v0.01
# first read docs/tutorials/README.md, then 01-minimal-loop.md
# run the commands in the tutorial's "Usage Guide"
git checkout v0.02          # see the diff: git diff v0.01..v0.02
# continue by the stage and version order in the tutorial overview
```

A command-line argument supplies the first task; it does not make the process one-shot. The CLI then enters the interactive loop, which you can leave with an empty line, `exit`, `quit`, or EOF. Tutorial commands target Bash/zsh. In PowerShell, set `$env:PYTHONPATH="src"` first and then run the corresponding `python ...` command.

## Project Structure

```
agent-from-scratch/
├── src/mini_agent/
│   ├── agent.py            # agent loop: call_llm + agent_loop
│   ├── __main__.py         # CLI entry point
│   ├── config.py           # config placeholder + auto-loads config_local.py
│   ├── config_example.py   # config template (copy to config_local.py)
│   ├── context.py           # ContextManager: unified pre-LLM entry point
│   ├── state.py             # AgentState: execution state separate from messages
│   ├── permission.py       # permission gate: allow/deny/ask
│   ├── prompt.py            # layered system prompt builder
│   └── tools/
│       ├── base.py         # Tool / ToolRegistry / ToolExecutor (with result callback)
│       ├── calc.py         # calculate tool
│       ├── file.py         # read_file / write_file / edit_file / list_dir / grep tools
│       └── shell.py        # run_shell tool
├── tests/                  # smoke tests
├── docs/
│   ├── tutorials/          # stage navigation, version-sliced tutorials (core)
│   ├── plans/              # roadmap, feature plans
│   ├── operation/          # runbook, usage guide
│   └── governance/         # governance docs, decision records
└── examples/               # example IO files
```

## Design Philosophy

- **Progressive growth**: Add just enough capability each step, avoid over-engineering. Record feature intent in the corresponding plan document; update `AGENTS.md` only when runtime constraints change.
- **Keep the core loop clear**: The agent loop does not catch top-level LLM or CLI exceptions; the tool layer catches handler failures and feeds error results back to the LLM. Complex fault tolerance is introduced at the tool layer as needed.
- **Zero dependencies**: Standard library only, staying self-contained and easy to deploy. HTTP client and other runtime contracts are summarized in [`AGENTS.md`](./AGENTS.md).

## Documentation

- [Tutorial path index](./docs/tutorials/README.md) — **start here**
- [Full usage manual](./docs/operation/manual.md) — complete usage, config, FAQ for the latest version
- [Context architecture](./docs/operation/context-architecture.md) — current context view, state, and budget mechanics
- [Roadmap & plans](./docs/plans/teaching-repo-plan.md) — stage navigation and version slicing plan
- [AGENTS.md](./AGENTS.md) — runtime hard constraints and concise architecture index
- [Governance docs](./docs/governance/README.md) — detailed standards and decision records
- [CHANGELOG.md](./CHANGELOG.md) — versioned change history

## Tests

With pytest installed in the development environment:

```bash
PYTHONPATH=src python -m pytest -q
```

Without pytest, run the standard-library smoke-test entry points listed in the [usage manual](./docs/operation/manual.md#4-测试).

## Contributing

Issues and PRs welcome. For new version slices, read `docs/plans/teaching-repo-plan.md`, the [tutorial authoring guide](./docs/governance/tutorial-authoring.md), and `AGENTS.md`; follow the “one concept per version” slicing principle.

## License

MIT — see [LICENSE](./LICENSE)

---

<div align="center">

**If this repo helps you, please consider giving it a Star ⭐ so more people can find it.**

</div>

<!-- Keywords: agent tutorial, LLM agent, coding agent, Python agent, function calling, build agent from scratch, AI agent, agent loop, tool calling, agent 教程 -->
