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
from mini_agent.tools.plan import (make_begin_plan_tool, make_cancel_planning_tool,
                                   make_commit_plan_tool, make_request_replan_tool,
                                   make_update_plan_progress_tool)
from mini_agent.recovery import RecoveryRuntime
from mini_agent.checkpoint import CheckpointStore, make_rollback_checkpoint_tool

def create_registry(state: AgentState | None = None,
                    workspace_root: str | None = None) -> ToolRegistry:
    result = ToolRegistry()
    for tool in (calculate_tool, read_file_tool, write_file_tool, edit_file_tool, list_dir_tool, grep_tool, run_shell_tool):
        result.register(tool)
    if state is not None:
        checkpoint_store = (
            getattr(state, "checkpoint_store", None)
            if workspace_root is None else None
        ) or CheckpointStore(workspace_root)
        if hasattr(state, "bind_checkpoint_store"):
            state.bind_checkpoint_store(checkpoint_store)
        result._checkpoint_store = checkpoint_store
        result.register(make_begin_plan_tool(state))
        result.register(make_cancel_planning_tool(state))
        result.register(make_commit_plan_tool(state))
        result.register(make_request_replan_tool(state))
        result.register(make_update_plan_progress_tool(state))
        # The outer task executor binds its own PermissionGate to this runtime.
        # Keeping only one runtime here prevents recovery from silently using a
        # second, default permission policy.
        recovery_runtime = RecoveryRuntime(state, None)
        result.register(__import__('mini_agent.recovery', fromlist=['make_recover_tool']).make_recover_tool(recovery_runtime))
        result.register(make_rollback_checkpoint_tool(checkpoint_store))
        result._recovery_runtime = recovery_runtime
    return result

registry = create_registry()

executor = ToolExecutor(registry)
