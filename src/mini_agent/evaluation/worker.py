"""One isolated Evaluation Harness Agent Runtime worker."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

from mini_agent.agent import ParentRuntimePolicy
from mini_agent.context import ContextBudget, ContextManager
from mini_agent.permission import DENY, PermissionGate, PermissionPolicy
from mini_agent.providers.catalog import ProviderCatalog
from mini_agent.runtime import AgentRuntime, RuntimeDecision, ToolRoundPlan
from mini_agent.state import AgentState
from mini_agent.tools.base import ExecutionResult, Tool, ToolExecutor, ToolRegistry
from mini_agent.tools.calc import calculate_tool
from mini_agent.tools.file import (
    edit_file_tool, grep_tool, list_dir_tool, read_file_tool, write_file_tool,
)
from mini_agent.evaluation.schema import (
    ALLOWED_TOOLS, MAX_RESULT_BYTES, TrialRequest, validate_trial_request,
)


MAX_TOOL_CALLS = 48
MAX_WORKER_RESULT_BYTES = 64 * 1024
MAX_WORKSPACE_FILES = 256
MAX_WORKSPACE_BYTES = 8 * 1024 * 1024
_TOOL_DEFINITIONS = {
    "read_file": read_file_tool,
    "list_dir": list_dir_tool,
    "grep": grep_tool,
    "write_file": write_file_tool,
    "edit_file": edit_file_tool,
    "calculate": calculate_tool,
}


class NonInteractivePermissionGate(PermissionGate):
    """Use exact local allow/deny policy and never read stdin."""

    def guard(self, tool_name: str, args: dict, *, display_context: dict | None = None) -> str | None:
        pattern = self._extract_pattern(tool_name, args)
        action = self.policy.check(tool_name, pattern)
        if action == "allow":
            return None
        if action == "deny":
            return f"权限拒绝: 评测策略禁止调用 {tool_name}"
        return f"权限拒绝: 评测没有为 {tool_name} 配置非交互授权"


def _scoped_path(root: Path, raw: Any, *, tool_name: str) -> str:
    if not isinstance(raw, str) or not raw or len(raw) > 1024 or "\x00" in raw:
        raise ValueError("evaluation_path_invalid")
    requested = Path(raw)
    candidate = requested if requested.is_absolute() else root / requested
    # Reject every existing symlink component. The normal file tools then
    # receive a canonical path rooted in the trial workspace.
    absolute = Path(os.path.abspath(candidate))
    try:
        relative = absolute.relative_to(root)
    except ValueError as error:
        raise ValueError("evaluation_path_outside_workspace") from error
    current = root
    for part in relative.parts:
        current = current / part
        try:
            if current.is_symlink():
                raise ValueError("evaluation_symlink_rejected")
        except OSError as error:
            raise ValueError("evaluation_path_unavailable") from error
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError("evaluation_path_outside_workspace") from error
    if resolved.exists() and not (resolved.is_file() or resolved.is_dir()):
        raise ValueError("evaluation_special_file_rejected")
    if tool_name in {"read_file", "write_file", "edit_file"} and resolved.exists() and not resolved.is_file():
        raise ValueError("evaluation_expected_file")
    if tool_name == "list_dir" and resolved.exists() and not resolved.is_dir():
        raise ValueError("evaluation_expected_directory")
    if tool_name == "grep" and resolved.exists() and not resolved.is_dir():
        raise ValueError("evaluation_expected_directory")
    return str(resolved)


def _scoped_handler(root: Path, tool: Tool):
    def handler(**arguments):
        normalized = dict(arguments)
        if tool.name in {"read_file", "write_file", "edit_file", "list_dir", "grep"}:
            key = "path"
            raw = normalized.get(key, ".")
            normalized[key] = _scoped_path(root, raw, tool_name=tool.name)
            if tool.name in {"write_file", "edit_file"}:
                _ensure_workspace_budget(root, Path(normalized[key]), normalized, tool.name)
        return tool.handler(**normalized)
    return handler


def _ensure_workspace_budget(root: Path, target: Path, arguments: dict[str, Any], tool_name: str) -> None:
    files = 0
    total_bytes = 0
    for current, dirs, names in os.walk(root, topdown=True, followlinks=False):
        for directory in dirs:
            entry = Path(current) / directory
            if entry.is_symlink():
                raise ValueError("evaluation_symlink_rejected")
        for name in names:
            entry = Path(current) / name
            if entry.is_symlink() or not entry.is_file():
                raise ValueError("evaluation_special_file_rejected")
            if entry == target:
                continue
            files += 1
            total_bytes += entry.stat().st_size
    projected = len(str(arguments.get("content", "")).encode("utf-8")) if tool_name == "write_file" else (
        target.stat().st_size + len(str(arguments.get("new_string", "")).encode("utf-8"))
        if target.exists() else 0
    )
    if target.exists():
        files += 1
    else:
        files += 1
    total_bytes += projected
    if files > MAX_WORKSPACE_FILES or total_bytes > MAX_WORKSPACE_BYTES:
        raise ValueError("evaluation_workspace_budget_exceeded")


def _build_registry(case: dict[str, Any], workspace: Path) -> ToolRegistry:
    registry = ToolRegistry()
    visible = tuple(case["allowed_tools"])
    if not visible or set(visible) - ALLOWED_TOOLS:
        raise ValueError("evaluation_tool_surface_invalid")
    for name in visible:
        source = _TOOL_DEFINITIONS[name]
        parameters = deepcopy(source.parameters)
        properties = parameters.setdefault("properties", {})
        for key, prop in properties.items():
            if prop.get("type") == "string":
                prop.setdefault("maxLength", 1024 * 1024 if key in {"content", "old_string", "new_string"} else 1024)
        registry.register(Tool(
            name=source.name,
            description=source.description,
            parameters=parameters,
            handler=_scoped_handler(workspace, source),
            effect_class=source.effect_class,
            internal=False,
            argument_validator=source.argument_validator,
            delegation_capability=source.delegation_capability,
        ))
    return registry


def _evaluation_system_prompt(workspace: Path, visible_tools: tuple[str, ...]) -> str:
    tool_names = ", ".join(visible_tools)
    return "\n".join((
        "你是在固定任务工作区中运行的编程 Agent。",
        f"工作目录是 {workspace}。所有文件路径必须留在此目录内。",
        f"本次评测只提供这些工具：{tool_names}。没有列出的工具不可调用。",
        "只按用户任务修改文件；不要猜测隐藏评分器的内容。",
        "完成后用简短文本说明结果。你的自我说明不会替代独立评分。",
        "若工具返回权限拒绝或路径拒绝，停止尝试同类越界调用。",
    ))


class EvaluationRuntimePolicy(ParentRuntimePolicy):
    """Keep parent completion policy while imposing a per-trial call budget."""

    def __init__(self) -> None:
        super().__init__()
        self.tool_budget_rejected = False

    def prepare_tool_round(self, runtime, calls):
        plan = super().prepare_tool_round(runtime, calls)
        if runtime.tool_calls + len(calls) <= MAX_TOOL_CALLS:
            return plan
        self.tool_budget_rejected = True
        rejected = dict(plan.rejection_by_index)
        for index, (name, arguments) in enumerate(runtime.parsed_calls):
            rejected[index] = ExecutionResult(
                name, arguments, "not_checked", False, "denied", 0,
                runtime.effects[index], "权限拒绝: 本次评测达到工具调用上限",
                "工具调用预算耗尽", error_kind="budget_exhausted",
            )
        return ToolRoundPlan(serial=True, rejection_by_index=rejected)

    def after_tool_round(self, runtime, calls, results):
        decision = super().after_tool_round(runtime, calls, results)
        if self.tool_budget_rejected:
            state = getattr(runtime.context, "state", None)
            if state is not None and state.status not in ("blocked", "failed"):
                state.status = "failed"
                state.terminal_reason = "evaluation_tool_call_budget"
            return RuntimeDecision("finish", "评测工具调用预算耗尽", "tool_call_limit")
        return decision

    def on_text(self, runtime, content):
        decision = super().on_text(runtime, content)
        state = getattr(runtime.context, "state", None)
        if decision.action != "continue" or state is None:
            return decision
        # This v0.50 harness intentionally exposes no shell command tool. A
        # normal direct-path file edit therefore cannot create Runtime's
        # run_shell verification evidence. Let the Agent stop normally and
        # leave task success entirely to the independent grader; do not mint
        # State verification evidence or weaken Repair/Plan gates.
        reminder = state.completion_reminder()
        if (
            reminder is not None
            and reminder.get("verification_required") is True
            and not reminder.get("unfinished_plan_steps")
            and reminder.get("repair_phase") == "idle"
            and state.planning_state.phase == "direct"
            and not state.plan_revisions
            and not state.crash_issues
        ):
            return RuntimeDecision("finish", content, "text")
        return decision


def _llm_client(request: TrialRequest, workspace: Path, registry: ToolRegistry):
    counts = {"successful_responses": 0}
    if request.run_kind == "fixture":
        responses = list(request.responses)
        cursor = 0

        def fixture_client(_messages, *, include_tools=True, tool_registry=None,
                           stream_output=False, **_options):
            nonlocal cursor
            if cursor >= len(responses):
                raise RuntimeError("fixture_response_exhausted")
            response = deepcopy(responses[cursor])
            cursor += 1
            counts["successful_responses"] += 1
            return response

        return fixture_client, None, counts

    from mini_agent import config
    catalog = ProviderCatalog.from_module(config)
    selected = request.case.get("model_profile")
    profile = catalog.resolve_parent_profile(selected)
    binding = catalog.bind(profile)

    def live_client(messages, *, include_tools=True, tool_registry=None,
                    stream_output=False, timeout=None, **options):
        response = binding.complete(
            messages,
            include_tools=include_tools,
            tool_registry=tool_registry or registry,
            stream_output=False,
            timeout=timeout,
            **options,
        )
        counts["successful_responses"] += 1
        return response

    return live_client, binding, counts


def run_request(raw_request: Any, *, on_started=None) -> dict[str, Any]:
    request = validate_trial_request(raw_request)
    workspace = Path(request.workspace).resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("evaluation_workspace_invalid")
    case = request.case
    state = AgentState()
    state.begin_task(case["task"])
    registry = _build_registry(case, workspace)
    authorized = set(case.get("authorized_tools", ()))
    rules = {name: ("allow" if name in authorized else DENY) for name in case["allowed_tools"]}
    gate = NonInteractivePermissionGate(PermissionPolicy(rules))
    executor = ToolExecutor(registry, gate=gate)
    binding = None
    runtime = None
    agent_started = False
    state_status: str | None = None
    stop_reason: str | None = None
    error_kind: str | None = None
    counts = {"successful_responses": 0}
    result_content = ""
    started = None
    try:
        client, binding, counts = _llm_client(request, workspace, registry)
        system_prompt = _evaluation_system_prompt(workspace, tuple(case["allowed_tools"]))
        context = ContextManager(
            state,
            [{"role": "user", "content": case["task"]}],
            budget=ContextBudget(
                window=(binding.profile.context_window if binding is not None else 128_000),
            ),
            summarizer=(None if binding is not None else lambda _messages: ""),
            observability=False,
            protected_messages=[{"role": "system", "content": system_prompt}],
            model_binding=binding,
            memory_retrieval_enabled=False,
        )
        runtime = AgentRuntime(
            llm_client=client,
            context=context,
            executor=executor,
            policy=EvaluationRuntimePolicy(),
            max_rounds=case["max_rounds"],
            model_binding=binding,
        )
        agent_started = True
        started = time.monotonic()
        if on_started is not None:
            on_started()
        runtime_result = runtime.run()
        stop_reason = runtime_result.stop_reason
        result_content = runtime_result.content
        if stop_reason == "text" and state.status == "running":
            # The interactive CLI normally closes a task after Runtime returns.
            # The worker is the non-interactive task owner and records that same
            # terminal fact directly from the structured stop reason.
            state.status = "done"
        state_status = state.status
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            error_kind = type(error).__name__
        else:
            error_kind = type(error).__name__
        state_status = state.status
        stop_reason = "agent_error"
    elapsed_ms = int((time.monotonic() - started) * 1000) if started is not None else 0
    if runtime is not None:
        runtime._refresh_usage()
        tool_calls = runtime.tool_calls
        input_tokens = runtime.input_tokens
        output_tokens = runtime.output_tokens
        accounting = runtime.token_accounting
    else:
        tool_calls = input_tokens = output_tokens = 0
        accounting = "fixture" if request.run_kind == "fixture" else "unavailable"
    ref = binding.reference.to_dict() if binding is not None else None
    usage_source = "fixture" if request.run_kind == "fixture" else (
        accounting if accounting in {"provider", "estimated", "mixed"} else "unavailable"
    )
    # The final assistant content is intentionally excluded. It can contain
    # unverified claims or copied task data and is not needed by the grader.
    return {
        "schema_version": 1,
        "trial_id": request.trial_id,
        "agent_started": agent_started,
        "agent_stop_reason": stop_reason,
        "agent_state_status": state_status,
        "agent_error_kind": error_kind,
        "agent_duration_ms": elapsed_ms,
        "agent_llm_calls": runtime.llm_calls if runtime is not None else 0,
        "successful_model_responses": counts["successful_responses"],
        "tool_calls": tool_calls,
        "permission_denials": sum(1 for attempt in state.attempts if attempt.permission == "denied"),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "token_accounting": accounting,
        "usage_source": usage_source,
        "model_binding_ref": ref,
        "cleanup_complete": True,
        "cleanup_issue": None,
        "state_terminal_reason": state.terminal_reason[:200],
    }


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 2:
        print("evaluation worker expects a request path and start marker path", file=sys.stderr)
        return 2
    request_path = Path(arguments[0])
    start_marker = Path(arguments[1])
    trial_id = None
    try:
        if request_path.stat().st_size > MAX_RESULT_BYTES:
            raise ValueError("worker_request_too_large")
        request = json.loads(request_path.read_text(encoding="utf-8"))
        trial_id = request.get("trial_id") if isinstance(request, dict) else None

        def mark_started():
            start_marker.write_text("started\n", encoding="ascii")
            os.chmod(start_marker, 0o600)

        result = run_request(request, on_started=mark_started)
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_WORKER_RESULT_BYTES:
            raise ValueError("worker_result_too_large")
        sys.stdout.write(encoded + "\n")
        sys.stdout.flush()
        return 0
    except BaseException as error:
        # Never emit exception text or a traceback: provider/config messages can
        # contain local endpoint or authentication details.
        safe = {
            "schema_version": 1,
            "trial_id": trial_id,
            "agent_started": False,
            "agent_stop_reason": "agent_error",
            "agent_state_status": None,
            "agent_error_kind": type(error).__name__,
            "agent_duration_ms": 0,
            "agent_llm_calls": 0,
            "successful_model_responses": 0,
            "tool_calls": 0,
            "permission_denials": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "token_accounting": "estimated",
            "usage_source": "unavailable",
            "model_binding_ref": None,
            "cleanup_complete": True,
            "cleanup_issue": None,
            "state_terminal_reason": "",
        }
        sys.stdout.write(json.dumps(safe, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
