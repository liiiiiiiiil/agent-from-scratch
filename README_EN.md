<div align="center">

# agent-from-scratch

### A coding agent that grows step by step

Build a working AI agent from scratch with the Python standard library, one concept and one Git snapshot at a time.

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)](https://www.python.org/) [![Dependencies](https://img.shields.io/badge/core%20dependencies-zero-green)](#quick-start) [![Versions](https://img.shields.io/badge/versions-v0.01%E2%86%92ongoing-orange)](#learning-path) [![License](https://img.shields.io/badge/license-MIT-lightgrey)](./LICENSE)

**English** · **[中文](./README.md)**

</div>

---

For developers who want to understand how an LLM agent runs without a framework. Each lesson focuses on one concept added since the previous version, with source, diffs, and design trade-offs kept traceable.

**Current status**: code and tutorials reach `v0.30` (lesson 30: session persistence and safe points). The authoritative tutorials are the current files under `docs/tutorials/`; check out the lesson's declared tag only when running its code.

Quick links: [run](#quick-start) · [learning path](#learning-path) · [tutorial guide](./docs/tutorials/README.md) · [manual](./docs/operation/manual.md)

## Learning Path

<table width="100%">
  <thead>
    <tr><th>Version</th><th>Topic</th><th>Overview</th></tr>
  </thead>
  <tbody>
    <tr><th colspan="3"><a id="stage-1"></a>Stage 1 · Understand the Agent Loop</th></tr>
    <tr><td><strong>v0.01</strong></td><td><a href="./docs/tutorials/01-minimal-loop.md">Minimal agent loop</a></td><td>Build the smallest conversation loop and its completion conditions.</td></tr>
    <tr><th colspan="3"><a id="stage-2"></a>Stage 2 · Tools and Safety</th></tr>
    <tr><td><strong>v0.02</strong></td><td><a href="./docs/tutorials/02-first-tool.md">First tool</a></td><td>Add calculate and walk through the function-calling protocol.</td></tr>
    <tr><td><strong>v0.03</strong></td><td><a href="./docs/tutorials/03-file-tools.md">File read/write tools</a></td><td>Let the Agent read and write files in a real project.</td></tr>
    <tr><td><strong>v0.04</strong></td><td><a href="./docs/tutorials/04-permission-gate.md">Permission gate</a></td><td>Gate side-effecting tools with allow, deny, and ask actions.</td></tr>
    <tr><th colspan="3"><a id="stage-3"></a>Stage 3 · Mini Agent Milestone</th></tr>
    <tr><td><strong>v0.05</strong></td><td><a href="./docs/tutorials/05-streaming.md">Streaming output</a></td><td>Receive and display LLM responses incrementally.</td></tr>
    <tr><td><strong>v0.06</strong> (patch <code>v0.06.1</code>)</td><td><a href="./docs/tutorials/06-concurrent-tool-calls.md">Concurrent tool_calls</a></td><td>Run one turn's tool calls concurrently; the patch fixes the multi-turn context contract.</td></tr>
    <tr><td><strong>v0.07</strong></td><td><a href="./docs/tutorials/07-system-prompt.md">System prompt engineering</a></td><td>Layer identity, rules, and environment details into a stable prompt.</td></tr>
    <tr><td><strong>v0.08</strong></td><td><a href="./docs/tutorials/08-file-operations.md">File operations complete</a></td><td>Add directory listing, precise edits, and regular-expression search.</td></tr>
    <tr><td><strong>v0.09</strong></td><td><a href="./docs/tutorials/09-permission-upgrade.md">Permission system upgrade</a></td><td>Match permissions by tool and path or command pattern.</td></tr>
    <tr><td><strong>v0.10</strong></td><td><a href="./docs/tutorials/10-shell-execution.md">Shell execution</a></td><td>Run commands with timeouts, output limits, and command-level authorization.</td></tr>
    <tr><th colspan="3"><a id="stage-4"></a>Stage 4 · Context Management</th></tr>
    <tr><td><strong>v0.11</strong></td><td><a href="./docs/tutorials/11-context-architecture.md">Context architecture</a></td><td>Separate durable execution state from trimmable conversation context.</td></tr>
    <tr><td><strong>v0.12</strong></td><td><a href="./docs/tutorials/12-token-budget-trimming.md">Budget and trimming</a></td><td>Estimate tokens and safely trim complete conversation rounds.</td></tr>
    <tr><td><strong>v0.13</strong> (patches <code>v0.13.1</code>, <code>v0.13.2</code>)</td><td><a href="./docs/tutorials/13-context-compaction.md">Context compaction</a></td><td>Use summaries and structured state to reduce forgetting; patches add observability and task-boundary isolation.</td></tr>
    <tr><th colspan="3"><a id="stage-5"></a>Stage 5 · Project-Aware Task Orchestration</th></tr>
    <tr><td><strong>v0.14</strong></td><td><a href="./docs/tutorials/14-project-instructions.md">Project instructions</a></td><td>Discover and inject applicable <code>AGENTS.md</code> instructions.</td></tr>
    <tr><td><strong>v0.15</strong></td><td><a href="./docs/tutorials/15-task-state.md">Todo and task state</a></td><td>Track multi-step progress with explicit structured state.</td></tr>
    <tr><td><strong>v0.16</strong> (patch <code>v0.16.1</code>)</td><td><a href="./docs/tutorials/16-plan-driven-execution.md">Plan-driven execution</a></td><td>Close the plan/execute/observe/reorder/verify loop; the patch narrows completion-notice reopening.</td></tr>
    <tr><th colspan="3"><a id="stage-6"></a>Stage 6 · Reliable Execution</th></tr>
    <tr><td><strong>v0.17</strong></td><td><a href="./docs/tutorials/17-failure-model.md">Failure model</a></td><td>Audit generations, attempts, and structured failure facts.</td></tr>
    <tr><td><strong>v0.18</strong> (patch <code>v0.18.1</code>)</td><td><a href="./docs/tutorials/18-recovery-policy.md">Recovery policy</a></td><td>Use bounded recovery actions with generation isolation; the patch tightens boundary consistency.</td></tr>
    <tr><td><strong>v0.19</strong></td><td><a href="./docs/tutorials/19-checkpoint-rollback.md">Checkpoint / Rollback</a></td><td>Restore one file atomically from a before-image with conflict detection, visible recovery state, and independent verification.</td></tr>
    <tr><td><strong>v0.20</strong></td><td><a href="./docs/tutorials/20-repair-loop.md">Repair Loop</a></td><td>Connect failure, diagnosis, bounded recovery, and independent verification through explicit phases and budgets.</td></tr>
    <tr><td><strong>v0.21</strong></td><td><a href="./docs/tutorials/21-trace-replay.md">Trace &amp; Replay</a></td><td>Replay Todo, execution, failure, recovery, verification, and terminal conclusions by generation without side effects.</td></tr>
    <tr><th colspan="3"><a id="stage-7"></a>Stage 7 · Structured Planning</th></tr>
    <tr><td><strong>v0.22</strong></td><td><a href="./docs/tutorials/22-plan-contract.md">Plan Contract</a></td><td>Keep immutable plan revisions, independent progress events, and a bounded execution view in context.</td></tr>
    <tr><td><strong>v0.23</strong></td><td><a href="./docs/tutorials/23-plan-mode-handoff.md">Read-only planning and handoff</a></td><td>Explore without effects, submit a plan for user approval, and keep tool permission separate.</td></tr>
    <tr><td><strong>v0.24</strong></td><td><a href="./docs/tutorials/24-replanning-policy.md">Evidence-backed replanning and bounded stagnation</a></td><td>Reference real failures or observations for plan revisions, then bound repeated tool rounds.</td></tr>
    <tr><td><strong>v0.25</strong></td><td><a href="./docs/tutorials/25-plan-trace-evaluation.md">Plan trace and evaluation</a></td><td>Connect generations, plan revisions, triggers, decisions, execution, and verification with ordered events.</td></tr>
    <tr><th colspan="3"><a id="stage-8"></a>Stage 8 · Background Processes and Task Boundaries</th></tr>
    <tr><td><strong>v0.26</strong></td><td><a href="./docs/tutorials/26-background-process-boundaries.md">Background process start and task boundaries</a></td><td>Start long-running commands, drain bounded output, synchronize exits by task, and clean up at handoff or task boundaries.</td></tr>
    <tr><td><strong>v0.27</strong></td><td><a href="./docs/tutorials/27-process-observation.md">Observing background processes</a></td><td>Query state across rounds, read incremental output, and wait with a bounded CLI handoff.</td></tr>
    <tr><td><strong>v0.28</strong></td><td><a href="./docs/tutorials/28-process-control.md">Controlling background processes</a></td><td>Authorize termination or forced exit by task, then verify after confirmed exit.</td></tr>
    <tr><td><strong>v0.29</strong></td><td><a href="./docs/tutorials/29-interactive-process.md">Driving input waiting processes</a></td><td>Explicitly enable bounded pipe stdin, write small UTF-8 text, send EOF, and preserve authorization, redaction, cleanup, and independent verification boundaries.</td></tr>
    <tr><th colspan="3"><a id="stage-9"></a>Stage 9 · Session Persistence and Safe Handoff</th></tr>
    <tr><td><strong>v0.30</strong></td><td><a href="./docs/tutorials/30-session-persistence.md">Session persistence and safe points</a></td><td>Opt in with /save, atomically update an active session only at complete safe points, and commit clean after process cleanup; this version validates files but cannot resume a task.</td></tr>
    <tr><td>Later versions</td><td>Added as needed</td><td>Continue expanding memory, sandboxing, and related capabilities.</td></tr>
  </tbody>
</table>

After `v0.10`, the Agent can inspect a project, search and modify files, run commands and tests, and control high-risk operations through permissions.

## Quick Start

Requirements: Python 3.10+ and an accessible LLM gateway. Bash/zsh:

```bash
git clone https://github.com/liiiiiiiiil/agent-from-scratch.git
cd agent-from-scratch
cp src/mini_agent/config_example.py src/mini_agent/config_local.py
# edit config_local.py with BASE_URL / API_KEY / MODEL; it is not tracked
python -m pip install -e .
python -m mini_agent "calculate 123 * 456"
```

Optional multiline input: `python -m pip install -e '.[interactive]'`. A command-line argument supplies the first task; the process then enters the interactive loop. Leave with an empty line, `exit`, `quit`, or EOF. See the [manual](./docs/operation/manual.md) for PowerShell, no-install usage, and configuration details.

## Project Structure

```text
src/mini_agent/   runtime, config, state, permissions, context, and tools
tests/             tests and smoke tests
docs/tutorials/    stage navigation and version-sliced lessons
docs/operation/    latest-version runbook
docs/plans/        roadmap and feature plans
docs/governance/   writing rules and decision records
examples/          example input/output files
```

## Design and Documentation

- Core LLM calls, the agent loop, tools, permissions, and state use only the standard library; UX enhancements are optional dependencies.
- The tool layer turns handler failures into results for the model; runtime constraints are summarized in [`AGENTS.md`](./AGENTS.md).
- [Tutorial guide](./docs/tutorials/README.md) · [manual](./docs/operation/manual.md) · [context architecture](./docs/operation/context-architecture.md) · [roadmap](./docs/plans/teaching-repo-plan.md) · [CHANGELOG](./CHANGELOG.md)

## Contributing

Issues and PRs are welcome. Before adding a lesson, read the [tutorial authoring guide](./docs/governance/tutorial-authoring.md), [README authoring guide](./docs/governance/readme-authoring.md), and `AGENTS.md`; the tutorial guide links the template and author entry points.

## License

MIT — see [LICENSE](./LICENSE)

<!-- Keywords: agent tutorial, LLM agent, coding agent, Python agent, function calling, build agent from scratch, AI agent, agent loop, tool calling -->
