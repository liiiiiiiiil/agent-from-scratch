"""Tool definitions, registry, validation, and structured execution."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import json
import re
from time import monotonic
from typing import Any, Callable, Literal

from mini_agent.permission import PermissionGate
from mini_agent.state import AttemptBudgetExceeded, AttemptReservation, EffectClass, PlanRejected
from mini_agent.tools.file_errors import EditMultipleMatchesError, EditNoMatchError

RESULT_BRIEF_MAX_LENGTH = 200
RESULT_BRIEF_FALLBACK = "<unavailable>"
ResultCallback = Callable[[str, dict[str, Any], bool, str], None]
DelegationCapability = Literal[
    "unavailable", "readonly_workspace", "pure_compute", "readonly_skill",
]


def format_tool_result(value: Any, max_chars: int = 4000) -> str:
    """Render a bounded, protocol-safe tool result for the model."""
    try:
        text = str(value)
    except Exception:
        return "工具调用失败: RuntimeError"
    if len(text) <= max_chars:
        return text
    marker = f"\n[... output truncated; {len(text) - max_chars} characters omitted ...]\n"
    keep = max(2, max_chars - len(marker))
    head = (keep + 1) // 2
    tail = keep // 2
    return text[:head] + marker + text[-tail:]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[..., Any]
    effect_class: EffectClass = "none"
    internal: bool = False
    argument_validator: Callable[[dict[str, Any]], None] | None = None
    delegation_capability: DelegationCapability = "unavailable"
    permission_context: dict[str, str] | None = None

    def to_llm_schema(self):
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": self.parameters}}

    def effect_for(self, arguments: dict[str, Any]) -> EffectClass:
        # A shell command may write files regardless of its declared purpose.
        if self.name in {"run_shell", "recover"}:
            return "possible"
        return self.effect_class


@dataclass(frozen=True)
class ExecutionResult:
    tool: str
    arguments: dict[str, Any]
    permission: Literal["allowed", "denied", "not_checked"]
    handler_admitted: bool
    outcome: Literal["succeeded", "failed", "denied", "timeout", "invalid"]
    duration_ms: int
    effect_class: EffectClass
    output: Any
    output_excerpt: str
    exit_code: int | None = None
    error_kind: str | None = None
    reservation: AttemptReservation | None = None
    checkpoint_id: str | None = None
    process_metadata: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == "succeeded"

    @property
    def result(self) -> Any:
        return self.output

    @property
    def timed_out(self) -> bool:
        return self.outcome == "timeout"

    def tool_content(self) -> str:
        if self.tool in {"get_process", "read_process", "list_processes", "wait_process"}:
            return format_tool_result(self.output, max_chars=8000)
        if self.tool in {"delegate_task", "get_subagent_result"}:
            return format_tool_result(self.output, max_chars=12 * 1024)
        if self.tool in {"spawn_subagent", "get_subagent_status", "cancel_subagent"}:
            return format_tool_result(self.output, max_chars=2000)
        if self.tool == "skill":
            # SKILL.md is bounded at 32 KiB. Preserve the complete JSON tool
            # result instead of applying the generic 4 KiB limit.
            return format_tool_result(self.output, max_chars=256 * 1024)
        if self.tool.startswith("mcp_"):
            return format_tool_result(self.output, max_chars=64 * 1024)
        if self.tool in {
            "list_memories", "read_memory", "remember", "revise_memory", "forget_memory",
            "search_memories",
        }:
            # Memory handlers already serialize a bounded JSON document.  Do
            # not apply the generic 4 KiB text truncation, which could turn a
            # valid list response into invalid JSON.
            limit = 16 * 1024 if self.tool == "search_memories" else 64 * 1024
            return format_tool_result(self.output, max_chars=limit)
        if self.tool in {"list_references", "search_reference", "read_reference"}:
            # Reference handlers construct complete bounded JSON.  Preserve
            # it for ordinary tool history instead of generic truncation.
            return format_tool_result(self.output, max_chars=64 * 1024)
        return format_tool_result(self.output)


@dataclass(frozen=True)
class ControlledToolResult:
    """A handler result with an explicit executor outcome and safe excerpt."""

    output: Any
    outcome: Literal["succeeded", "failed", "timeout", "invalid"] = "succeeded"
    error_kind: str | None = None
    output_excerpt: str = ""


@dataclass(frozen=True)
class ToolAdmission:
    """Validated, authorized execution that has not entered its handler."""

    tool: Tool
    name: str
    arguments: dict[str, Any]
    effect_class: EffectClass
    reservation: AttemptReservation | None
    started: float
    state: Any = None


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool, internal: bool | None = None):
        if tool.name in self._tools:
            raise ValueError(f"Tool 已经存在: {tool.name}")
        if tool.effect_class not in ("none", "possible"):
            raise ValueError(f"非法 effect_class: {tool.effect_class}")
        if tool.delegation_capability not in (
                "unavailable", "readonly_workspace", "pure_compute", "readonly_skill"):
            raise ValueError(f"非法 delegation_capability: {tool.delegation_capability}")
        if internal is not None:
            tool.internal = internal
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise ValueError(f"未知 Tool: {name}")
        return self._tools[name]

    def list_tools(self):
        return list(self._tools.values())

    def schemas(self):
        return [tool.to_llm_schema() for tool in self._tools.values() if not tool.internal]

    def effect_for(self, name: str, arguments: dict[str, Any]) -> EffectClass:
        return self.get(name).effect_for(arguments)

    def validate_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Normalize and validate one call through the registry boundary."""
        tool = self.get(name)
        normalized = validate_arguments(tool.parameters, arguments)
        if tool.argument_validator is not None:
            tool.argument_validator(normalized)
        return normalized

    def filtered_for_subagent(self, allowed: set[str] | frozenset[str],
                              scope_gate: Any = None, skill_catalog: Any = None):
        """Return a frozen, capability-based view for a read-only child runtime."""
        return FilteredToolRegistryView(
            self, allowed, scope_gate=scope_gate, skill_catalog=skill_catalog,
        )


class FilteredToolRegistryView:
    """Read-only registry view with frozen tool metadata.

    The view deliberately copies schemas and Tool metadata at construction time.
    It never exposes the parent's mutable mapping and cannot register or replace
    tools.  ``scope_gate`` is an optional validator used by workspace readers.
    """

    def __init__(self, parent: ToolRegistry, allowed: set[str] | frozenset[str],
                 scope_gate: Any = None, skill_catalog: Any = None):
        allowed = set(allowed)
        frozen: dict[str, Tool] = {}
        for name, tool in parent._tools.items():
            if name not in allowed:
                continue
            if tool.delegation_capability == "unavailable":
                continue
            if tool.delegation_capability == "readonly_skill":
                if name != "skill" or skill_catalog is None:
                    continue
                from mini_agent.tools.skill import make_skill_tool
                cloned = make_skill_tool(skill_catalog)
                frozen[name] = cloned
                continue
            if name == "skill":
                # A Skill tool is never inherited from a parent handler that
                # can see the full parent catalog.
                continue
            cloned = Tool(
                name=tool.name,
                description=str(tool.description),
                parameters=deepcopy(tool.parameters),
                handler=(scope_gate.wrap_handler(tool.name, tool.handler)
                         if scope_gate is not None and hasattr(scope_gate, "wrap_handler")
                         else tool.handler),
                effect_class=tool.effect_class,
                internal=tool.internal,
                argument_validator=tool.argument_validator,
                delegation_capability=tool.delegation_capability,
                permission_context=deepcopy(tool.permission_context),
            )
            frozen[name] = cloned
        self._tools = frozen
        self._schemas = tuple(deepcopy(tool.to_llm_schema()) for tool in frozen.values())
        self._scope_gate = scope_gate
        self._skill_catalog = skill_catalog

    @staticmethod
    def _copy_tool(tool: Tool) -> Tool:
        """Return a defensive definition copy, never the view's executable entry."""
        return Tool(
            name=tool.name,
            description=tool.description,
            parameters=deepcopy(tool.parameters),
            handler=tool.handler,
            effect_class=tool.effect_class,
            internal=tool.internal,
            argument_validator=tool.argument_validator,
            delegation_capability=tool.delegation_capability,
            permission_context=deepcopy(tool.permission_context),
        )

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise ValueError(f"未知或禁止的 Subagent Tool: {name}")
        return self._copy_tool(self._tools[name])

    def list_tools(self) -> list[Tool]:
        return [self._copy_tool(tool) for tool in self._tools.values()]

    def schemas(self) -> list[dict[str, Any]]:
        return deepcopy(list(self._schemas))

    def effect_for(self, name: str, arguments: dict[str, Any]) -> EffectClass:
        if name not in self._tools:
            raise ValueError(f"未知或禁止的 Subagent Tool: {name}")
        return self._tools[name].effect_for(arguments)

    def validate_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in self._tools:
            raise ValueError(f"未知或禁止的 Subagent Tool: {name}")
        tool = self._tools[name]
        normalized = validate_arguments(tool.parameters, arguments)
        if self._scope_gate is not None:
            normalized = self._scope_gate.normalize_tool_call(name, normalized)
        if tool.argument_validator is not None:
            tool.argument_validator(normalized)
        return normalized

    def register(self, *_args, **_kwargs):
        raise TypeError("FilteredToolRegistryView 不允许注册或替换工具")

    def replace(self, *_args, **_kwargs):
        raise TypeError("FilteredToolRegistryView 不允许注册或替换工具")


def _json_type_matches(value: Any, expected: str) -> bool:
    return {
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "array": isinstance(value, list),
        "object": isinstance(value, dict),
        "null": value is None,
    }.get(expected, True)


def validate_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    """Validate the JSON-schema subset used by this standard-library project."""
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments 必须是 JSON object")
    normalized = deepcopy(arguments)
    properties = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        unknown = [key for key in normalized if key not in properties]
        if unknown:
            raise ValueError("未知参数: " + ", ".join(unknown))
    for key, prop in properties.items():
        if key not in normalized and "default" in prop:
            normalized[key] = deepcopy(prop["default"])
    missing = [key for key in schema.get("required", []) if key not in normalized]
    if missing:
        raise ValueError("缺少必需参数: " + ", ".join(missing))
    for key, value in normalized.items():
        prop = properties.get(key)
        if prop is None:
            continue
        expected = prop.get("type")
        if expected and not _json_type_matches(value, expected):
            raise ValueError(f"参数 {key} 类型应为 {expected}")
        if "enum" in prop and value not in prop["enum"]:
            raise ValueError(f"参数 {key} 不在允许值中")
        if isinstance(value, str):
            if "minLength" in prop and len(value) < prop["minLength"]:
                raise ValueError(f"参数 {key} 长度不足")
            if "maxLength" in prop and len(value) > prop["maxLength"]:
                raise ValueError(f"参数 {key} 超过长度上限")
            if "pattern" in prop and re.search(prop["pattern"], value) is None:
                raise ValueError(f"参数 {key} 格式非法")
        if isinstance(value, list):
            if "maxItems" in prop and len(value) > prop["maxItems"]:
                raise ValueError(f"参数 {key} 超过数量上限")
            item_schema = prop.get("items")
            if isinstance(item_schema, dict):
                for index, item in enumerate(value):
                    item_type = item_schema.get("type")
                    if item_type and not _json_type_matches(item, item_type):
                        raise ValueError(f"参数 {key}[{index}] 类型应为 {item_type}")
                    if isinstance(item, str):
                        if "minLength" in item_schema and len(item) < item_schema["minLength"]:
                            raise ValueError(f"参数 {key}[{index}] 长度不足")
                        if "maxLength" in item_schema and len(item) > item_schema["maxLength"]:
                            raise ValueError(f"参数 {key}[{index}] 超过长度上限")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in prop and value < prop["minimum"]:
                raise ValueError(f"参数 {key} 小于允许下限")
            if "maximum" in prop and value > prop["maximum"]:
                raise ValueError(f"参数 {key} 超过允许上限")
    return normalized


def _brief(value: Any) -> str:
    try:
        return str(value)[:RESULT_BRIEF_MAX_LENGTH]
    except Exception:
        return RESULT_BRIEF_FALLBACK


def _mcp_metadata_excerpt(tool: Tool, category: str) -> str:
    context = getattr(tool, "permission_context", None) or {}
    return json.dumps({
        "source": "mcp",
        "alias": str(context.get("alias", "<unknown>"))[:64],
        "tool": str(context.get("tool", "<unknown>"))[:64],
        "category": category[:64],
        "text_bytes": 0,
        "content_items": 0,
    }, ensure_ascii=False, separators=(",", ":"))


def _memory_excerpt(value: Any) -> str:
    """Keep Memory正文 out of State/trace excerpts while preserving the result."""
    if not isinstance(value, str):
        return _brief(value)
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return _brief(value)

    def scrub(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                key: "<omitted>" if key == "body" else scrub(child)
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [scrub(child) for child in item]
        return item

    try:
        return _brief(json.dumps(scrub(parsed), ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError):
        return _brief(value)


def _reference_excerpt(value: Any) -> str:
    """Keep Reference正文 out of State/Trace excerpts."""
    if not isinstance(value, str):
        return _brief(value)
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return _brief(value)
    if not isinstance(parsed, dict):
        return _brief(value)
    if "references" in parsed:
        scrubbed = {
            key: parsed[key]
            for key in ("status", "total")
            if key in parsed
        }
        scrubbed["references"] = [
            {
                key: item[key]
                for key in ("alias", "description")
                if isinstance(item, dict) and key in item
            }
            for item in parsed.get("references", [])
            if isinstance(item, dict)
        ]
    elif "matches" in parsed:
        scrubbed = {
            key: parsed[key]
            for key in (
                "status", "alias", "path", "total_matches", "returned_matches",
                "scanned_files", "scanned_bytes", "visited_entries",
                "scan_truncated", "truncated",
            )
            if key in parsed
        }
        scrubbed["matches"] = [
            {
                key: item[key]
                for key in ("path", "line", "sha256")
                if isinstance(item, dict) and key in item
            }
            for item in parsed.get("matches", [])
            if isinstance(item, dict)
        ]
    else:
        scrubbed = {
            key: parsed[key]
            for key in (
                "status", "alias", "path", "offset", "start_line", "end_line",
                "returned_lines", "omitted_lines", "total_lines", "sha256", "truncated",
            )
            if key in parsed
        }
        if isinstance(parsed.get("lines"), list):
            scrubbed["line_count"] = len(parsed["lines"])
    try:
        return _brief(json.dumps(scrubbed, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError):
        return _brief(value)


def _skill_excerpt(value: Any) -> str:
    """Keep Skill正文 out of State/Trace while retaining load metadata."""
    if not isinstance(value, str):
        return _brief(value)
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return _brief(value)
    if not isinstance(parsed, dict):
        return _brief(value)
    if parsed.get("status") == "ok":
        safe = {
            key: parsed[key]
            for key in ("skill_id", "source", "bytes")
            if key in parsed
        }
    else:
        safe = {
            "error_kind": parsed["error_kind"]
        } if "error_kind" in parsed else {}
    try:
        return _brief(json.dumps(safe, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError):
        return _brief(value)


def _checkpoint_notice(checkpoint: Any) -> str:
    """Return metadata-only checkpoint information for a file-tool result."""
    fields = (
        f"checkpoint_id={checkpoint.checkpoint_id}",
        f"path={checkpoint.path}",
        f"status={checkpoint.status}",
    )
    if checkpoint.unavailable_reason:
        fields += (f"reason={checkpoint.unavailable_reason}",)
    return "Checkpoint: " + "; ".join(fields)


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, gate: PermissionGate | None = None,
                 on_result: ResultCallback | None = None):
        self.registry = registry
        self.gate = gate or PermissionGate()
        self.on_result = on_result
        self.checkpoint_store = getattr(registry, "_checkpoint_store", None)
        recovery_runtime = getattr(registry, "_recovery_runtime", None)
        if recovery_runtime is not None:
            recovery_runtime.bind_executor(self)
        delegation_manager = getattr(registry, "_delegation_manager", None)
        if delegation_manager is not None and hasattr(delegation_manager, "bind_parent_permission_gate"):
            delegation_manager.bind_parent_permission_gate(self.gate)

    def authorize(self, name: str, arguments: dict[str, Any]) -> str | None:
        """Run the same session permission gate used by normal execution."""
        tool = self.registry.get(name)
        return self._guard(tool, name, arguments)

    def _guard(self, tool: Tool, name: str, arguments: dict[str, Any]) -> str | None:
        context = getattr(tool, "permission_context", None)
        if name == "skill":
            catalog = getattr(self.registry, "_skill_catalog", None)
            if catalog is not None:
                try:
                    definition = catalog.get(arguments.get("name"))
                    context = {"skill_id": definition.name, "source": definition.source}
                except Exception:
                    # Unknown IDs still go through the same gate, but their
                    # prompt contains no path or guessed source.
                    context = None
        if context is None:
            return self.gate.guard(name, arguments)
        return self.gate.guard(name, arguments, display_context=context)

    @staticmethod
    def _record_recovery_rejection(state: Any, arguments: dict[str, Any], detail: str) -> str | None:
        if state is None or not hasattr(state, "reject_recovery"):
            return None
        arguments = arguments if isinstance(arguments, dict) else {}
        record = state.reject_recovery(
            arguments.get("action", "block"),
            arguments.get("caused_by_failure_id", "<missing>"),
            arguments.get("reason", ""),
            str(detail), arguments.get("requested_attempt"),
            arguments.get("requested_tool"), arguments.get("requested_arguments"),
            arguments.get("checkpoint_id"),
        )
        return json.dumps({
            "status": "rejected", "recovery_id": record.recovery_id,
            "message": str(detail)[:RESULT_BRIEF_MAX_LENGTH],
        }, ensure_ascii=False)

    def _terminal_result(self, name: str, arguments: dict[str, Any], state: Any) -> ExecutionResult:
        reason = getattr(state, "terminal_reason", "") or "任务已终止"
        text = f"工具调用拒绝: task_terminal: {reason}"
        if name == "recover":
            rejection = self._record_recovery_rejection(state, arguments, text)
            if rejection is not None:
                text = json.dumps({
                    "status": "rejected",
                    "error_kind": "task_terminal",
                    "recovery_id": json.loads(rejection)["recovery_id"],
                    "message": text,
                }, ensure_ascii=False)
        return ExecutionResult(
            name, deepcopy(arguments) if isinstance(arguments, dict) else {},
            "not_checked", False, "invalid", 0, "none", text, _brief(text),
            error_kind="task_terminal",
        )

    def _notify_result(self, result: ExecutionResult) -> None:
        if self.on_result is None:
            return
        try:
            self.on_result(result.tool, result.arguments,
                           result.outcome == "succeeded", result.output_excerpt)
        except Exception as error:
            try: print(f"[Executor] 结果回调失败: {type(error).__name__}")
            except Exception: pass

    def _execute_admitted_result(self, admission: ToolAdmission,
                                 *, notify: bool = True) -> ExecutionResult:
        """Enter exactly the handler captured by a completed admission."""
        tool = admission.tool
        name = admission.name
        normalized = admission.arguments
        effect_class = admission.effect_class
        reservation = admission.reservation
        state = admission.state
        started = admission.started
        is_plan_tool = name in (
            "begin_plan", "cancel_planning", "commit_plan", "update_plan_progress",
            "request_replan",
        )
        if is_plan_tool:
            try:
                output = tool.handler(**normalized)
            except PlanRejected as error:
                text = json.dumps({
                    "status": "plan_rejected",
                    "message": str(error)[:RESULT_BRIEF_MAX_LENGTH],
                }, ensure_ascii=False)
                result = ExecutionResult(
                    name, normalized, "allowed", True, "invalid",
                    int((monotonic() - started) * 1000), effect_class,
                    text, _brief(text), error_kind="plan_rejected",
                )
                if notify:
                    self._notify_result(result)
                return result
            except Exception as error:
                text = f"Tool 执行失败: {error}"
                result = ExecutionResult(
                    name, normalized, "allowed", True, "failed",
                    int((monotonic() - started) * 1000), effect_class,
                    text, _brief(text), error_kind="handler_exception",
                )
                if notify:
                    self._notify_result(result)
                return result
            result = ExecutionResult(
                name, normalized, "allowed", True, "succeeded",
                int((monotonic() - started) * 1000), effect_class,
                output, _brief(output),
            )
            if notify:
                self._notify_result(result)
            return result

        checkpoint_capture = None
        checkpoint_id = None
        checkpoint = None
        checkpoint_store = self.checkpoint_store
        if (state is not None and checkpoint_store is not None and
                name in ("write_file", "edit_file") and effect_class == "possible"):
            try:
                checkpoint_capture = checkpoint_store.capture_before(
                    reservation.attempt_id, reservation.generation_id,
                    normalized.get("path"),
                )
                checkpoint_id = checkpoint_capture.checkpoint_id
            except Exception:
                checkpoint_capture = None
        try:
            output = tool.handler(**normalized)
        except Exception as error:
            if checkpoint_capture is not None:
                try:
                    checkpoint = checkpoint_store.capture_after(checkpoint_capture)
                except Exception:
                    pass
            text = f"Tool 执行失败: {error}"
            if hasattr(error, "error_kind"):
                error_kind = str(error.error_kind)
            elif isinstance(error, EditNoMatchError):
                error_kind = "edit_no_match"
            elif isinstance(error, EditMultipleMatchesError):
                error_kind = "edit_multiple_matches"
            else:
                error_kind = "handler_exception"
            safe_excerpt = _brief(text)
            if name.startswith("mcp_") and tool.permission_context is not None:
                text = "工具调用失败: MCP handler error"
                error_kind = "mcp_handler_error"
                safe_excerpt = _mcp_metadata_excerpt(tool, error_kind)
            if name == "skill":
                # Skill paths and body text are never part of a user-visible
                # failure or State/Trace excerpt.
                error_kind = getattr(error, "error_kind", None) or "skill_access_error"
                text = json.dumps({
                    "status": "error", "error_kind": error_kind,
                    "message": "Skill access failed",
                }, ensure_ascii=False, separators=(",", ":"))
                safe_excerpt = _skill_excerpt(text)
            if checkpoint is not None:
                text += "\n" + _checkpoint_notice(checkpoint)
            if name == "recover":
                rejection = self._record_recovery_rejection(state, normalized, text)
                if rejection is not None:
                    text = rejection
                    error_kind = "recovery_rejected"
            result = ExecutionResult(
                name, normalized, "allowed", True, "failed",
                int((monotonic() - started) * 1000), effect_class,
                text, safe_excerpt, error_kind=error_kind,
                reservation=reservation, checkpoint_id=checkpoint_id,
            )
            if notify:
                self._notify_result(result)
            return result
        if isinstance(output, ControlledToolResult):
            result = ExecutionResult(
                name, normalized, "allowed", True, output.outcome,
                int((monotonic() - started) * 1000), effect_class,
                output.output, output.output_excerpt or _brief(output.output),
                error_kind=output.error_kind, reservation=reservation,
                checkpoint_id=checkpoint_id,
            )
            if notify:
                self._notify_result(result)
            return result
        if checkpoint_capture is not None:
            try:
                checkpoint = checkpoint_store.capture_after(checkpoint_capture)
            except Exception:
                pass
        process_metadata = None
        if name == "start_process" and isinstance(output, dict):
            process_metadata = deepcopy(output)
            public = {
                key: output[key] for key in ("process_id", "pid", "status", "stdin_mode")
                if key in output
            }
            if reservation is not None:
                public["start_attempt_id"] = reservation.attempt_id
            output = json.dumps(public, ensure_ascii=False)
        if checkpoint is not None:
            output = f"{output}\n{_checkpoint_notice(checkpoint)}"
        if name == "skill":
            excerpt = _skill_excerpt(output)
        elif name in {
            "list_memories", "read_memory", "remember", "revise_memory", "forget_memory",
            "search_memories",
        }:
            excerpt = _memory_excerpt(output)
        elif name in {"list_references", "search_reference", "read_reference"}:
            excerpt = _reference_excerpt(output)
        else:
            excerpt = _brief(output)
        exit_code = None
        outcome: Literal["succeeded", "failed", "denied", "timeout", "invalid"] = "succeeded"
        error_kind = None
        if isinstance(output, str) and output.startswith("[timeout]"):
            outcome, error_kind = "timeout", "timeout"
        elif name in {"get_process", "read_process", "list_processes", "wait_process",
                      "terminate_process", "kill_process", "write_process"}:
            try:
                process_result = json.loads(output) if isinstance(output, str) else {}
            except ValueError:
                process_result = {}
            if isinstance(process_result, dict) and process_result.get("status") == "error":
                error_kind = str(process_result.get("error_kind", "process_error"))
                io_error = name == "write_process" and error_kind in {
                    "broken_pipe", "stdin_pipe_error", "stdin_write_error",
                    "stdin_write_thread_error",
                }
                control_error = (name in {"terminate_process", "kill_process"}
                                 and error_kind == "control_failed")
                outcome = "failed" if io_error or control_error else "invalid"
        elif name == "run_shell" and isinstance(output, str):
            match = re.match(r"\[exit=(-?\d+)\]", output)
            if match:
                exit_code = int(match.group(1))
                if exit_code != 0:
                    outcome, error_kind = "failed", "nonzero_exit"
        result = ExecutionResult(
            name, normalized, "allowed", True, outcome,
            int((monotonic() - started) * 1000), effect_class,
            output, excerpt, exit_code, error_kind, reservation,
            checkpoint_id, process_metadata,
        )
        if notify:
            self._notify_result(result)
        return result

    def execute_result(self, name: str, arguments: dict[str, Any], state: Any = None,
                       notify: bool = True, reservation: AttemptReservation | None = None,
                       permission_already_checked: bool = False,
                       _internal: bool = False, on_admitted: Callable[[ToolAdmission], None] | None = None,
                       admit_only: bool = False) -> ExecutionResult | ToolAdmission:
        """Execute and return facts; only admitted possible effects reserve generation."""
        started = monotonic()
        if state is not None and getattr(state, "is_terminal", lambda: False)():
            return self._terminal_result(name, arguments, state)
        is_plan_tool = name in (
            "begin_plan", "cancel_planning", "commit_plan", "update_plan_progress",
            "request_replan",
        )

        def plan_rejected(detail: object) -> ExecutionResult:
            text = json.dumps({
                "status": "plan_rejected",
                "message": str(detail)[:RESULT_BRIEF_MAX_LENGTH],
            }, ensure_ascii=False)
            return ExecutionResult(
                str(name), deepcopy(arguments) if isinstance(arguments, dict) else {},
                "not_checked", False, "invalid", int((monotonic() - started) * 1000),
                "none", text, _brief(text), error_kind="plan_rejected",
            )

        try:
            tool = self.registry.get(name)
        except (TypeError, ValueError) as error:
            return ExecutionResult(str(name), deepcopy(arguments) if isinstance(arguments, dict) else {},
                                   "not_checked", False, "invalid", 0, "none",
                                   f"工具调用失败: {type(error).__name__}",
                                   f"工具调用失败: {type(error).__name__}", error_kind="unknown_tool")
        if tool.internal and not _internal:
            text = "工具调用失败: internal_tool 只能由 RecoveryRuntime 调用"
            return ExecutionResult(
                str(name), deepcopy(arguments) if isinstance(arguments, dict) else {},
                "not_checked", False, "invalid", 0, "none", text, text,
                error_kind="internal_tool",
            )
        if state is not None and hasattr(state, "crash_recovery_gate"):
            raw_arguments = arguments if isinstance(arguments, dict) else {}
            crash_error = state.crash_recovery_gate(
                name, raw_arguments, tool.effect_for(raw_arguments),
            )
            if crash_error:
                return ExecutionResult(
                    name, deepcopy(raw_arguments), "not_checked", False, "invalid", 0,
                    tool.effect_for(raw_arguments), crash_error, _brief(crash_error),
                    error_kind="crash_recovery_gate",
                )
        if (name != "request_replan" and state is not None
                and hasattr(state, "planning_gate")):
            raw_arguments = arguments if isinstance(arguments, dict) else {}
            phase_error = state.planning_gate(
                name, raw_arguments, tool.effect_for(raw_arguments),
            )
            if phase_error:
                if is_plan_tool:
                    return plan_rejected(phase_error)
                return ExecutionResult(
                    name, deepcopy(raw_arguments), "not_checked", False, "invalid", 0,
                    tool.effect_for(raw_arguments), phase_error, _brief(phase_error),
                    error_kind="planning_phase_gate",
                )
        try:
            normalized = self.registry.validate_call(name, arguments)
        except (TypeError, ValueError) as error:
            if is_plan_tool:
                return plan_rejected(error)
            text = f"工具调用失败: {type(error).__name__}: {error}"
            invalid_excerpt = _brief(text)
            if str(name).startswith("mcp_") and tool.permission_context is not None:
                invalid_excerpt = _mcp_metadata_excerpt(tool, "invalid_arguments")
            planning_phase = getattr(getattr(state, "planning_state", None), "phase", None)
            if str(name) == "recover" and state is not None and planning_phase not in (
                    "exploring", "awaiting_approval"):
                rejection = self._record_recovery_rejection(state, arguments, text)
                if rejection is not None:
                    return ExecutionResult(
                        name, deepcopy(arguments) if isinstance(arguments, dict) else {},
                        "not_checked", False, "invalid", 0, "none", rejection,
                        _brief(rejection), error_kind="recovery_rejected",
                    )
            return ExecutionResult(name, deepcopy(arguments) if isinstance(arguments, dict) else {},
                                   "not_checked", False, "invalid", 0,
                                   tool.effect_for(arguments if isinstance(arguments, dict) else {}),
                                   text, invalid_excerpt, error_kind="invalid_arguments")
        effect_class = tool.effect_for(normalized)
        if state is not None and hasattr(state, "delegation_gate"):
            delegation_error = state.delegation_gate(name, normalized, effect_class)
            if delegation_error:
                return ExecutionResult(
                    name, normalized, "not_checked", False, "invalid", 0,
                    effect_class, delegation_error, _brief(delegation_error),
                    error_kind="delegation_gate",
                )
        if state is not None and hasattr(state, "crash_recovery_gate"):
            crash_error = state.crash_recovery_gate(name, normalized, effect_class)
            if crash_error:
                return ExecutionResult(
                    name, normalized, "not_checked", False, "invalid", 0,
                    effect_class, crash_error, _brief(crash_error),
                    error_kind="crash_recovery_gate",
                )
        if name in {"terminate_process", "kill_process", "write_process"} and state is not None:
            manager = getattr(state, "_process_manager", None)
            if manager is None or manager.get_owned(state.task_id, normalized["process_id"]) is None:
                output = json.dumps({"status": "error", "process_id": normalized["process_id"],
                                     "error_kind": "unknown_process_id",
                                     "message": "未知、过期或跨任务 process_id"}, ensure_ascii=False)
                return ExecutionResult(name, normalized, "not_checked", False, "invalid", 0,
                                       effect_class, output, _brief(output),
                                       error_kind="unknown_process_id")
        if state is not None and hasattr(state, "planning_gate"):
            phase_error = state.planning_gate(name, normalized, effect_class)
            if phase_error:
                if is_plan_tool:
                    return plan_rejected(phase_error)
                return ExecutionResult(
                    name, normalized, "not_checked", False, "invalid", 0,
                    effect_class, phase_error, _brief(phase_error),
                    error_kind="planning_phase_gate",
                )
        # Crash recovery has a deliberately narrow investigation lane.  Once
        # the crash gate has admitted a genuinely read-only observation, the
        # ordinary Repair Loop gate must not turn the preserved verification or
        # diagnosis obligation into a deadlock.  This does not clear any
        # repair state and does not admit effects or verification commands.
        crash_investigation = bool(
            state is not None
            and hasattr(state, "has_unresolved_crash_recovery")
            and state.has_unresolved_crash_recovery()
            and effect_class == "none"
            and hasattr(state, "crash_recovery_gate")
            and state.crash_recovery_gate(name, normalized, effect_class) is None
        )
        if state is not None and hasattr(state, "repair_gate") and not crash_investigation:
            phase_error = state.repair_gate(
                name, normalized, effect_class, reservation=reservation,
            )
            if phase_error:
                if name == "recover" and hasattr(state, "reject_recovery"):
                    rejection = self._record_recovery_rejection(state, normalized, phase_error)
                    return ExecutionResult(
                        name, normalized, "not_checked", False, "invalid",
                        int((monotonic() - started) * 1000), effect_class,
                        rejection or phase_error, _brief(rejection or phase_error),
                        error_kind="recovery_rejected",
                    )
                if is_plan_tool:
                    return plan_rejected(phase_error)
                return ExecutionResult(
                    name, normalized, "not_checked", False, "invalid", 0,
                    effect_class, phase_error, _brief(phase_error),
                    error_kind="repair_phase_gate",
                )
        denied = None if permission_already_checked else self._guard(tool, name, normalized)
        if denied:
            if name == "recover":
                rejection = self._record_recovery_rejection(state, normalized, denied)
                if rejection is not None:
                    result = ExecutionResult(name, normalized, "denied", False, "denied",
                                             int((monotonic() - started) * 1000), effect_class,
                                             rejection, _brief(rejection), error_kind="recovery_rejected")
                    if notify: self._notify_result(result)
                    return result
            result = ExecutionResult(name, normalized, "denied", False, "denied",
                                     int((monotonic() - started) * 1000), effect_class,
                                     denied, _brief(denied), error_kind="permission_denied")
            if notify: self._notify_result(result)
            return result
        # Ownership is checked before permission above.  Capability and
        # in-flight state are checked after permission but before reserving a
        # possible-effect generation, so rejected writes never reserve or
        # deliver bytes.
        if name == "write_process" and state is not None and hasattr(manager, "write_preflight"):
            preflight = manager.write_preflight(state.task_id, normalized["process_id"])
            if preflight is not None:
                output = json.dumps(preflight, ensure_ascii=False)
                pending = preflight.get("status") == "write_pending"
                return ExecutionResult(
                    name, normalized, "allowed", False,
                    "succeeded" if pending else "invalid", 0,
                    effect_class, output, _brief(output),
                    error_kind="write_pending" if pending else str(
                        preflight.get("error_kind", "process_error")
                    ),
                )
        # Plan tools mutate only the plan state itself.  Run their atomic
        # state-bound handler before reserving an execution attempt so a
        # rejected submission cannot consume a fingerprint, attempt number,
        # generation, or any other execution fact.
        if is_plan_tool:
            admission = ToolAdmission(
                tool, name, normalized, effect_class, None, started, state,
            )
            if admit_only:
                return admission
            if on_admitted is not None:
                on_admitted(admission)
            return self._execute_admitted_result(admission, notify=notify)
        try:
            if reservation is None and state is not None and name != "recover":
                reservation = state.reserve_attempt(effect_class, name, normalized)
        except AttemptBudgetExceeded as error:
            text = f"工具调用失败: {error}"
            result = ExecutionResult(
                name, normalized, "allowed", False, "invalid",
                int((monotonic() - started) * 1000), effect_class,
                text, _brief(text), error_kind="attempt_fingerprint_budget",
            )
            if notify:
                self._notify_result(result)
            return result
        admission = ToolAdmission(
            tool, name, normalized, effect_class, reservation, started, state,
        )
        if admit_only:
            return admission
        if on_admitted is not None:
            on_admitted(admission)
        return self._execute_admitted_result(admission, notify=notify)

    def admit(self, name: str, arguments: dict[str, Any], state: Any = None,
              *, reservation: AttemptReservation | None = None,
              permission_already_checked: bool = False,
              _internal: bool = False) -> ToolAdmission | ExecutionResult:
        """Run all pre-handler checks and return an admission or a result.

        A caller that needs a durable handler boundary calls this method,
        persists the returned ``ToolAdmission``, and only then calls
        :meth:`execute_admitted`.
        """
        return self.execute_result(
            name, arguments, state=state, notify=False, reservation=reservation,
            permission_already_checked=permission_already_checked,
            _internal=_internal, admit_only=True,
        )

    def execute_admitted(self, admission: ToolAdmission, *, notify: bool = True) -> ExecutionResult:
        """Enter a previously admitted handler after its caller's commit."""
        if not isinstance(admission, ToolAdmission):
            raise TypeError("需要 ToolAdmission")
        return self._execute_admitted_result(admission, notify=notify)

    def execute_admitted_delegation(self, admission: ToolAdmission, result: Any,
                                    *, notify: bool = False) -> ExecutionResult:
        """Render a pre-reserved delegate result through the executor boundary.

        The batch scheduler owns the frozen child contract and worker.  This
        narrow entry point keeps the parent handler/result shape and callbacks
        identical to the ordinary ``delegate_task`` path without exposing an
        internal field in the public tool schema.
        """
        if not isinstance(admission, ToolAdmission) or admission.name != "delegate_task":
            raise TypeError("需要已准入的 delegate_task")
        if not hasattr(result, "to_json"):
            raise TypeError("子代理结果必须支持 to_json")
        started = admission.started
        content = result.to_json()
        execution = ExecutionResult(
            admission.name, admission.arguments, "allowed", True, "succeeded",
            int((monotonic() - started) * 1000), admission.effect_class,
            content, _brief(content), reservation=admission.reservation,
        )
        if notify:
            self._notify_result(execution)
        return execution

    def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        """Compatibility API returning the handler/tool-protocol value."""
        # Preserve the historical unknown-tool exception at this API boundary.
        self.registry.get(name)
        return self.execute_result(name, arguments).output

    def execute_internal_result(self, name: str, arguments: dict[str, Any],
                                state: Any = None, notify: bool = True,
                                reservation: AttemptReservation | None = None,
                                permission_already_checked: bool = False) -> ExecutionResult:
        """Execute a registered internal tool through the normal fact boundary."""
        result = self.execute_result(
            name, arguments, state=state, notify=notify, reservation=reservation,
            permission_already_checked=permission_already_checked, _internal=True,
        )
        if name == "rollback_checkpoint" and isinstance(arguments, dict):
            return replace(result, checkpoint_id=arguments.get("checkpoint_id"))
        return result
