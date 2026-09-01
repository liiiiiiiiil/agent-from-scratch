"""Tool definitions, registry, validation, and structured execution."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import re
from time import monotonic
from typing import Any, Callable, Literal

from mini_agent.permission import PermissionGate
from mini_agent.state import AttemptReservation, EffectClass

RESULT_BRIEF_MAX_LENGTH = 200
RESULT_BRIEF_FALLBACK = "<unavailable>"
ResultCallback = Callable[[str, dict[str, Any], bool, str], None]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[..., Any]
    effect_class: EffectClass = "none"

    def to_llm_schema(self):
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": self.parameters}}

    def effect_for(self, arguments: dict[str, Any]) -> EffectClass:
        if self.name == "run_shell" and arguments.get("purpose", "execution") == "verification":
            return "none"
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
        try:
            return str(self.output)
        except Exception:
            return "工具调用失败: RuntimeError"


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool):
        if tool.name in self._tools:
            raise ValueError(f"Tool 已经存在: {tool.name}")
        if tool.effect_class not in ("none", "possible"):
            raise ValueError(f"非法 effect_class: {tool.effect_class}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise ValueError(f"未知 Tool: {name}")
        return self._tools[name]

    def list_tools(self):
        return list(self._tools.values())

    def schemas(self):
        return [tool.to_llm_schema() for tool in self._tools.values()]

    def effect_for(self, name: str, arguments: dict[str, Any]) -> EffectClass:
        return self.get(name).effect_for(arguments)


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
        if isinstance(value, list) and "maxItems" in prop and len(value) > prop["maxItems"]:
            raise ValueError(f"参数 {key} 超过数量上限")
    return normalized


def _brief(value: Any) -> str:
    try:
        return str(value)[:RESULT_BRIEF_MAX_LENGTH]
    except Exception:
        return RESULT_BRIEF_FALLBACK


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, gate: PermissionGate | None = None,
                 on_result: ResultCallback | None = None):
        self.registry = registry
        self.gate = gate or PermissionGate()
        self.on_result = on_result

    def _notify_result(self, result: ExecutionResult) -> None:
        if self.on_result is None:
            return
        try:
            self.on_result(result.tool, result.arguments,
                           result.outcome == "succeeded", result.output_excerpt)
        except Exception as error:
            try: print(f"[Executor] 结果回调失败: {type(error).__name__}")
            except Exception: pass

    def execute_result(self, name: str, arguments: dict[str, Any], state: Any = None,
                       notify: bool = True) -> ExecutionResult:
        """Execute and return facts; only admitted possible effects reserve generation."""
        started = monotonic()
        try:
            tool = self.registry.get(name)
        except (TypeError, ValueError) as error:
            return ExecutionResult(str(name), deepcopy(arguments) if isinstance(arguments, dict) else {},
                                   "not_checked", False, "invalid", 0, "none",
                                   f"工具调用失败: {type(error).__name__}",
                                   f"工具调用失败: {type(error).__name__}", error_kind="unknown_tool")
        try:
            normalized = validate_arguments(tool.parameters, arguments)
        except (TypeError, ValueError) as error:
            text = f"工具调用失败: {type(error).__name__}: {error}"
            return ExecutionResult(name, deepcopy(arguments) if isinstance(arguments, dict) else {},
                                   "not_checked", False, "invalid", 0,
                                   tool.effect_for(arguments if isinstance(arguments, dict) else {}),
                                   text, text[:RESULT_BRIEF_MAX_LENGTH], error_kind="invalid_arguments")
        effect_class = tool.effect_for(normalized)
        denied = self.gate.guard(name, normalized)
        if denied:
            result = ExecutionResult(name, normalized, "denied", False, "denied",
                                     int((monotonic() - started) * 1000), effect_class,
                                     denied, _brief(denied), error_kind="permission_denied")
            if notify: self._notify_result(result)
            return result
        reservation = state.reserve_attempt(effect_class) if state is not None else None
        try:
            output = tool.handler(**normalized)
        except Exception as error:
            text = f"Tool 执行失败: {error}"
            result = ExecutionResult(name, normalized, "allowed", True, "failed",
                                     int((monotonic() - started) * 1000), effect_class,
                                     text, _brief(text), error_kind="handler_exception",
                                     reservation=reservation)
            if notify: self._notify_result(result)
            return result
        excerpt = _brief(output)
        exit_code = None
        outcome: Literal["succeeded", "failed", "denied", "timeout", "invalid"] = "succeeded"
        error_kind = None
        if isinstance(output, str) and output.startswith("[timeout]"):
            outcome, error_kind = "timeout", "timeout"
        elif name == "run_shell" and isinstance(output, str):
            match = re.match(r"\[exit=(-?\d+)\]", output)
            if match:
                exit_code = int(match.group(1))
                if exit_code != 0:
                    outcome, error_kind = "failed", "nonzero_exit"
        result = ExecutionResult(name, normalized, "allowed", True, outcome,
                                 int((monotonic() - started) * 1000), effect_class,
                                 output, excerpt, exit_code, error_kind, reservation)
        if notify: self._notify_result(result)
        return result

    def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        """Compatibility API returning the handler/tool-protocol value."""
        # Preserve the historical unknown-tool exception at this API boundary.
        self.registry.get(name)
        return self.execute_result(name, arguments).output
