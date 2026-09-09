from __future__ import annotations

from mini_agent.tools.base import ToolRegistry, ToolExecutor, Tool
from mini_agent.tools.calc import calculate_tool
from mini_agent.tools.file import (
    read_file_tool,
    write_file_tool,
    edit_file_tool,
    list_dir_tool,
    grep_tool,
)
from mini_agent.tools.shell import run_shell_tool
from mini_agent.permission import PermissionGate
from mini_agent.state import AgentState
from mini_agent.tools.todo import make_update_todo_tool
from mini_agent.recovery import RecoveryRuntime

def create_registry(state: AgentState | None = None) -> ToolRegistry:
    result = ToolRegistry()
    for tool in (calculate_tool, read_file_tool, write_file_tool, edit_file_tool, list_dir_tool, grep_tool, run_shell_tool):
        result.register(tool)
    if state is not None:
        result.register(make_update_todo_tool(state))
        # The outer task executor binds its own PermissionGate to this runtime.
        # Keeping only one runtime here prevents recovery from silently using a
        # second, default permission policy.
        recovery_runtime = RecoveryRuntime(state, None)
        result.register(__import__('mini_agent.recovery', fromlist=['make_recover_tool']).make_recover_tool(recovery_runtime))
        result._recovery_runtime = recovery_runtime
    return result

registry = create_registry()

executor = ToolExecutor(registry)
