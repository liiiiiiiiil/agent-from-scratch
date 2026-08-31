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

> **Current status**: The code and tutorials are up to `v0.16` (Plan-driven Execution).

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

### Stage 1: Understand the Agent Loop

Goal: understand LLM calls, `messages`, the loop, and completion conditions.

| Version | Topic | What this version adds | Tutorial |
|---|---|---|---|
| **v0.01** | Minimal agent loop | `call_llm` + `agent_loop` (no tools) | [01-minimal-loop.md](./docs/tutorials/01-minimal-loop.md) |

### Stage 2: Tools and Safety

Goal: let the Agent call tools while controlling file-modifying side effects.

| Version | Topic | What this version adds | Tutorial |
|---|---|---|---|
| **v0.02** | First tool | `Tool`/`ToolRegistry` + `calculate` + function calling | [02-first-tool.md](./docs/tutorials/02-first-tool.md) |
| **v0.03** | File read/write tools | `read_file` / `write_file` | [03-file-tools.md](./docs/tutorials/03-file-tools.md) |
| **v0.04** | Permission gate | `permission.py` (allow/deny/ask) | [04-permission-gate.md](./docs/tutorials/04-permission-gate.md) |

### Stage 3: Mini Agent Milestone

Goal: add interaction, file operations, and command execution so the Agent can handle basic coding tasks.

| Version | Topic | What this version adds | Tutorial |
|---|---|---|---|
| **v0.05** | Streaming output | streaming `call_llm` + typewriter effect | [05-streaming.md](./docs/tutorials/05-streaming.md) |
| **v0.06** | Concurrent tool_calls | `ThreadPoolExecutor` concurrency | [06-concurrent-tool-calls.md](./docs/tutorials/06-concurrent-tool-calls.md) |
| **v0.07** | System prompt engineering | expand system prompt to a full specification | [07-system-prompt.md](./docs/tutorials/07-system-prompt.md) |
| **v0.08** | File operations complete | `list_dir` / `edit_file` / `grep` | [08-file-operations.md](./docs/tutorials/08-file-operations.md) |
| **v0.09** | Permission system upgrade | 2D permission (tool_name, pattern) + fnmatch | [09-permission-upgrade.md](./docs/tutorials/09-permission-upgrade.md) |
| **v0.10** | Shell execution | `run_shell` tool + subprocess + timeout + output truncation + 2D command pattern permission | [10-shell-execution.md](./docs/tutorials/10-shell-execution.md) |

> `v0.06.1` is a protocol-state fix for v0.06 and has no separate tutorial; use `git checkout v0.06.1` to reproduce it.

After `v0.10`, the Agent can inspect a project, search and modify files, run commands, run tests, and control high-risk operations through permissions. This is the repo's first stage milestone.

### Stage 4: Context Management

Goal: let the Agent keep executing complex tasks reliably within a finite context window by separating state from context, managing budget, trimming history, and compacting old history.

| Version | Topic | What this version adds | Tutorial |
|---|---|---|---|
| **v0.11** | Context architecture | `AgentState` + `ContextManager` + Executor result callback | [11-context-architecture.md](./docs/tutorials/11-context-architecture.md) |
| **v0.12** | Budget and trimming | token estimation + atomic round trimming | [12-token-budget-trimming.md](./docs/tutorials/12-token-budget-trimming.md) |
| **v0.13** | Context compaction | LLM summary + Structured State anchor + context observability | [13-context-compaction.md](./docs/tutorials/13-context-compaction.md) |

### Stage 5: Project-Aware Task Orchestration

| Version | Topic | Status |
|---|---|---|
| v0.14 | Project Instructions | [14-project-instructions.md](./docs/tutorials/14-project-instructions.md) |
| v0.15 | Todo / Task State | [15-task-state.md](./docs/tutorials/15-task-state.md) |
| v0.16 | Plan-driven Execution | [16-plan-driven-execution.md](./docs/tutorials/16-plan-driven-execution.md) |
| Later versions | Failure recovery, verification loops, observability, memory, sandboxing, and more | as needed |

**How to learn by version:**

```bash
git tag                    # list all versions
git checkout v0.01          # switch to v0.01
# first read docs/tutorials/README.md, then 01-minimal-loop.md
# run the commands in the tutorial's "Usage Guide"
git checkout v0.02          # see the diff: git diff v0.01..v0.02
# continue by the stage and version order in the tutorial overview
```

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

- **Progressive growth**: Add just enough capability each step, avoid over-engineering. New features are first recorded as intent in `AGENTS.md`, then implemented.
- **Keep the core loop clear**: The agent loop does not catch top-level LLM or CLI exceptions; the tool layer catches handler failures and feeds error results back to the LLM. Complex fault tolerance is introduced at the tool layer as needed.
- **Zero dependencies**: Standard library only, staying self-contained and easy to deploy. Switching HTTP clients reintroduces a known pitfall (see `AGENTS.md` key constraints).

## Documentation

- [Tutorial path index](./docs/tutorials/README.md) — **start here**
- [Full usage manual](./docs/operation/manual.md) — complete usage, config, FAQ for the latest version
- [Roadmap & plans](./docs/plans/teaching-repo-plan.md) — stage navigation and version slicing plan
- [AGENTS.md](./AGENTS.md) — project architecture & constraints memo
- [CHANGELOG.md](./CHANGELOG.md) — versioned change history

## Tests

With pytest installed in the development environment:

```bash
PYTHONPATH=src python -m pytest -q
```

Without pytest, run the standard-library smoke-test entry points listed in the [usage manual](./docs/operation/manual.md#4-测试).

## Contributing

Issues and PRs welcome. For new version slices, please read `docs/plans/teaching-repo-plan.md` and `AGENTS.md` first, and follow the "one concept per version" slicing principle.

## License

MIT — see [LICENSE](./LICENSE)

---

<div align="center">

**If this repo helps you, please consider giving it a Star ⭐ so more people can find it.**

</div>

<!-- Keywords: agent tutorial, LLM agent, coding agent, Python agent, function calling, build agent from scratch, AI agent, agent loop, tool calling, agent 教程 -->
