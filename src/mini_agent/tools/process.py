"""The v0.26 background process start tool."""
from __future__ import annotations

from mini_agent.processes import ProcessManager
from mini_agent.state import AgentState
from mini_agent.tools.base import Tool


def make_start_process_tool(state: AgentState, manager: ProcessManager) -> Tool:
    def start_process(command: str, cwd: str | None = None):
        task_id = state.ensure_task_id()
        started = manager.start(command, cwd, task_id)
        return {
            "process_id": started.process_id,
            "pid": started.pid,
            "status": started.status,
            "task_id": started.task_id,
            "started_at": started.started_at,
            "command": started.command,
            "cwd": started.cwd,
        }

    return Tool(
        name="start_process",
        description=(
            "在当前任务中启动长期运行的 shell 命令，立即返回任务专属 process_id；"
            "创建成功不代表命令最终成功。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string", "minLength": 1, "maxLength": 4000,
                    "pattern": r"\S",
                    "description": "沿用 run_shell shell 语义的命令字符串",
                },
                "cwd": {
                    "type": "string", "minLength": 1, "maxLength": 1000,
                    "description": "存在的工作目录；缺省为当前工作目录",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        handler=start_process,
        effect_class="possible",
    )
