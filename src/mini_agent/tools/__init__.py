from __future__ import annotations

import os

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
from mini_agent.tools.process import (make_start_process_tool, make_get_process_tool,
                                      make_list_processes_tool, make_read_process_tool,
                                      make_wait_process_tool, make_write_process_tool,
                                      make_control_process_tool)
from mini_agent.processes import ProcessManager
from mini_agent.permission import PermissionGate
from mini_agent.state import AgentState
from mini_agent.tools.plan import (make_begin_plan_tool, make_cancel_planning_tool,
                                   make_commit_plan_tool, make_request_replan_tool,
                                   make_update_plan_progress_tool)
from mini_agent.recovery import RecoveryRuntime
from mini_agent.checkpoint import CheckpointStore, make_rollback_checkpoint_tool
from mini_agent.providers.catalog import ProviderCatalog

def create_registry(state: AgentState | None = None,
                    workspace_root: str | None = None,
                    process_manager: ProcessManager | None = None,
                    subagent_llm=None,
                    include_delegation: bool | None = None,
                    provider_catalog: ProviderCatalog | None = None) -> ToolRegistry:
    result = ToolRegistry()
    for tool in (calculate_tool, read_file_tool, write_file_tool, edit_file_tool, list_dir_tool, grep_tool, run_shell_tool):
        result.register(tool)
    if include_delegation is None:
        include_delegation = state is not None
    if include_delegation:
        if state is None:
            raise ValueError("delegate_task 必须绑定父 AgentState")
        from mini_agent.delegation import DelegationManager
        from mini_agent.tools.delegation import make_delegate_task_tool
        manager = DelegationManager(
            workspace_root=workspace_root or os.getcwd(),
            subagent_llm=subagent_llm,
            parent_registry=result,
            provider_catalog=provider_catalog,
        )
        result._delegation_manager = manager
        result.register(make_delegate_task_tool(state, manager))
    if state is not None:
        process_manager = process_manager or ProcessManager()
        if hasattr(state, "bind_process_manager"):
            state.bind_process_manager(process_manager)
        # A resumed State already owns its imported metadata-only checkpoint
        # store.  Keep that exact object even when workspace_root is supplied
        # for delegation scope construction; replacing it would erase audit
        # records and break recovery/Trace references.
        checkpoint_store = (
            getattr(state, "checkpoint_store", None)
            or CheckpointStore(workspace_root)
        )
        if hasattr(state, "bind_checkpoint_store"):
            state.bind_checkpoint_store(checkpoint_store)
        result._checkpoint_store = checkpoint_store
        result._process_manager = process_manager
        result.register(make_start_process_tool(state, process_manager))
        result.register(make_get_process_tool(state, process_manager))
        result.register(make_read_process_tool(state, process_manager))
        result.register(make_list_processes_tool(state, process_manager))
        result.register(make_wait_process_tool(state, process_manager))
        result.register(make_write_process_tool(state, process_manager))
        result.register(make_control_process_tool(state, process_manager, kill=False))
        result.register(make_control_process_tool(state, process_manager, kill=True))
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

# Keep the historical module-level smoke-test registry stable.  CLI and all
# task-bound registries use create_registry()'s normal parent view above.
registry = create_registry(include_delegation=False)

executor = ToolExecutor(registry)
