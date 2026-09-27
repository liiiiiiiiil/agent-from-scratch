"""Versioned, bounded contracts for Evaluation Harness trials."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
import os
import re
import stat
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
ALLOWED_TOOLS = frozenset({
    "read_file", "list_dir", "grep", "write_file", "edit_file", "calculate",
})
MAX_FIXTURE_FILES = 256
MAX_FIXTURE_BYTES = 4 * 1024 * 1024
MAX_FIXTURE_FILE_BYTES = 1024 * 1024
MAX_CASE_BYTES = 64 * 1024
MAX_TASK_CHARS = 8000
MAX_AGENT_ROUNDS = 50
MAX_AGENT_TIMEOUT_SECONDS = 600
MAX_GRADER_TIMEOUT_SECONDS = 120
MAX_RESULT_BYTES = 256 * 1024

_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){0,2}(?:[-+][a-zA-Z0-9.-]+)?\Z")


CASE_SCHEMA_V1: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "mini-agent-evaluation-case-v1",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version", "case_id", "version", "task", "fixture_dir",
        "grader_script", "agent_timeout_seconds", "grader_timeout_seconds",
        "max_rounds", "allowed_tools", "authorized_tools",
    ],
    "properties": {
        "schema_version": {"const": 1},
        "case_id": {"type": "string", "pattern": "^[a-z][a-z0-9_-]{0,63}$"},
        "version": {"type": "string", "maxLength": 64},
        "task": {"type": "string", "minLength": 1, "maxLength": MAX_TASK_CHARS},
        "fixture_dir": {"type": "string", "minLength": 1, "maxLength": 512},
        "grader_script": {"type": "string", "minLength": 1, "maxLength": 512},
        "agent_timeout_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_AGENT_TIMEOUT_SECONDS},
        "grader_timeout_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_GRADER_TIMEOUT_SECONDS},
        "max_rounds": {"type": "integer", "minimum": 1, "maximum": MAX_AGENT_ROUNDS},
        "allowed_tools": {
            "type": "array", "minItems": 1, "maxItems": len(ALLOWED_TOOLS),
            "uniqueItems": True, "items": {"enum": sorted(ALLOWED_TOOLS)},
        },
        "authorized_tools": {
            "type": "array", "maxItems": len(ALLOWED_TOOLS),
            "uniqueItems": True, "items": {"enum": sorted(ALLOWED_TOOLS)},
        },
        "model_profile": {"type": ["string", "null"], "pattern": "^[a-z][a-z0-9_-]{0,63}$"},
        "description": {"type": "string", "maxLength": 1000},
        "tags": {"type": "array", "maxItems": 32, "items": {"type": "string", "maxLength": 64}},
    },
}

TRIAL_REQUEST_SCHEMA_V1: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "mini-agent-evaluation-trial-request-v1",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "trial_id", "run_kind", "case", "workspace", "responses"],
    "properties": {
        "schema_version": {"const": 1},
        "trial_id": {"type": "string", "pattern": "^[a-f0-9-]{36}$"},
        "run_kind": {"enum": ["live", "fixture"]},
        "case": {"type": "object"},
        "workspace": {"type": "string", "minLength": 1, "maxLength": 4096},
        "responses": {"type": "array", "maxItems": MAX_AGENT_ROUNDS, "items": {"type": "object"}},
    },
}

TRIAL_RESULT_SCHEMA_V1: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "mini-agent-evaluation-trial-result-v1",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version", "trial_id", "case_id", "case_version", "run_kind",
        "started_at", "ended_at", "agent_started", "agent_stop_reason",
        "agent_state_status", "agent_error_kind", "agent_duration_ms",
        "grader_passed", "grader_error_kind", "grader_duration_ms", "success",
        "failure_kind", "agent_exit_code", "grader_exit_code", "agent_llm_calls",
        "successful_model_responses", "tool_calls", "permission_denials", "subagent_tool_calls",
        "subagent_count", "input_tokens", "output_tokens",
        "token_accounting", "usage_source", "model_binding_ref", "cleanup_complete",
        "cleanup_issue", "recovery_success_rate", "invalid_repeat_count",
        "recovery_metrics_note", "grader_result", "artifacts", "changed_files", "code_revision",
        "cost_usd", "price_snapshot",
    ],
    "properties": {
        "schema_version": {"const": 1}, "trial_id": {"type": "string"},
        "case_id": {"type": "string"}, "case_version": {"type": "string"},
        "run_kind": {"enum": ["live", "fixture"]},
        "started_at": {"type": "string"}, "ended_at": {"type": "string"},
        "agent_started": {"type": "boolean"}, "agent_stop_reason": {"type": ["string", "null"]},
        "agent_state_status": {"type": ["string", "null"]}, "agent_error_kind": {"type": ["string", "null"]},
        "agent_duration_ms": {"type": "integer", "minimum": 0},
        "grader_passed": {"type": ["boolean", "null"]}, "grader_error_kind": {"type": ["string", "null"]},
        "grader_duration_ms": {"type": "integer", "minimum": 0}, "success": {"type": "boolean"},
        "failure_kind": {"enum": ["none", "agent_timeout", "agent_error", "task_failed", "grader_infrastructure_error", "infrastructure_error", "agent_output_limit"]},
        "agent_exit_code": {"type": ["integer", "null"]}, "grader_exit_code": {"type": ["integer", "null"]},
        "agent_llm_calls": {"type": ["integer", "null"], "minimum": 0},
        "successful_model_responses": {"type": ["integer", "null"], "minimum": 0},
        "tool_calls": {"type": ["integer", "null"], "minimum": 0},
        "permission_denials": {"type": ["integer", "null"], "minimum": 0},
        "subagent_tool_calls": {"const": 0}, "subagent_count": {"const": 0},
        "input_tokens": {"type": ["integer", "null"], "minimum": 0},
        "output_tokens": {"type": ["integer", "null"], "minimum": 0}, "token_accounting": {"type": "string"},
        "usage_source": {"enum": ["provider", "estimated", "mixed", "fixture", "unavailable"]},
        "model_binding_ref": {"type": ["object", "null"]}, "cleanup_complete": {"type": "boolean"},
        "cleanup_issue": {"type": ["string", "null"]},
        "recovery_success_rate": {"type": ["number", "null"]},
        "invalid_repeat_count": {"type": ["integer", "null"]},
        "recovery_metrics_note": {"type": "string", "maxLength": 200},
        "grader_result": {"type": ["object", "null"]},
        "artifacts": {"type": "object"},
        "changed_files": {"type": "array", "maxItems": MAX_FIXTURE_FILES, "items": {"type": "string", "maxLength": 1024}},
        "code_revision": {"type": ["string", "null"], "maxLength": 80},
        "cost_usd": {"type": ["number", "null"], "minimum": 0},
        "price_snapshot": {"type": ["object", "null"]},
        "grader_sha256": {"type": ["string", "null"], "pattern": "^[0-9a-f]{64}$"},
    },
}


@dataclass(frozen=True)
class Case:
    case_id: str
    version: str
    task: str
    fixture_dir: str
    grader_script: str
    agent_timeout_seconds: int
    grader_timeout_seconds: int
    max_rounds: int
    allowed_tools: tuple[str, ...]
    authorized_tools: tuple[str, ...]
    model_profile: str | None = None
    description: str = ""
    tags: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION
    case_dir: str = field(default="", compare=False, repr=False)

    def to_dict(self, *, include_directory: bool = False) -> dict[str, Any]:
        value = asdict(self)
        value.pop("case_dir", None)
        value["allowed_tools"] = list(self.allowed_tools)
        value["authorized_tools"] = list(self.authorized_tools)
        value["tags"] = list(self.tags)
        if include_directory:
            value["case_dir"] = self.case_dir
        return value


@dataclass(frozen=True)
class TrialRequest:
    trial_id: str
    run_kind: str
    case: dict[str, Any]
    workspace: str
    responses: tuple[dict[str, Any], ...] = ()
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "trial_id": self.trial_id,
            "run_kind": self.run_kind,
            "case": self.case,
            "workspace": self.workspace,
            "responses": list(self.responses),
        }


@dataclass(frozen=True)
class TrialResult:
    trial_id: str
    case_id: str
    case_version: str
    run_kind: str
    started_at: str
    ended_at: str
    agent_started: bool
    agent_stop_reason: str | None
    agent_state_status: str | None
    agent_error_kind: str | None
    agent_duration_ms: int
    grader_passed: bool | None
    grader_error_kind: str | None
    grader_duration_ms: int
    success: bool
    failure_kind: str
    tool_calls: int | None
    input_tokens: int | None
    output_tokens: int | None
    token_accounting: str
    usage_source: str
    model_binding_ref: dict[str, str] | None
    cleanup_complete: bool
    cleanup_issue: str | None
    recovery_success_rate: float | None = None
    invalid_repeat_count: int | None = None
    recovery_metrics_note: str = "本版不适用/未采集"
    grader_result: dict[str, Any] | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    changed_files: list[str] = field(default_factory=list)
    agent_exit_code: int | None = None
    grader_exit_code: int | None = None
    agent_llm_calls: int | None = 0
    successful_model_responses: int | None = 0
    permission_denials: int | None = 0
    subagent_tool_calls: int = 0
    subagent_count: int = 0
    code_revision: str | None = None
    cost_usd: float | None = None
    price_snapshot: dict[str, Any] | None = None
    grader_sha256: str | None = None
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _validate_json_schema(value: Any, schema: dict[str, Any], where: str) -> None:
    """Validate the bounded JSON Schema subset used by schema 1 contracts."""
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        matching = any(_schema_type_matches(value, item) for item in types)
        _require(matching, f"{where} 类型不符合 schema")
    if "const" in schema:
        constant = schema["const"]
        _require(type(value) is type(constant) and value == constant, f"{where} 必须为 {constant!r}")
    if "enum" in schema:
        _require(any(type(value) is type(item) and value == item for item in schema["enum"]), f"{where} 不在允许值中")
    if isinstance(value, str):
        if "minLength" in schema:
            _require(len(value) >= schema["minLength"], f"{where} 长度不足")
        if "maxLength" in schema:
            _require(len(value) <= schema["maxLength"], f"{where} 超过长度上限")
        if "pattern" in schema:
            _require(re.search(schema["pattern"], value) is not None, f"{where} 格式非法")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema:
            _require(value >= schema["minimum"], f"{where} 小于允许下限")
        if "maximum" in schema:
            _require(value <= schema["maximum"], f"{where} 超过允许上限")
    if isinstance(value, list):
        if "minItems" in schema:
            _require(len(value) >= schema["minItems"], f"{where} 项数不足")
        if "maxItems" in schema:
            _require(len(value) <= schema["maxItems"], f"{where} 项数超过上限")
        if schema.get("uniqueItems"):
            encoded = [json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for item in value]
            _require(len(set(encoded)) == len(encoded), f"{where} 含重复项")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_json_schema(item, item_schema, f"{where}[{index}]")
    if isinstance(value, dict):
        if schema.get("additionalProperties") is False:
            known = set(schema.get("properties", {}))
            _require(set(value) <= known, f"{where} 包含未知字段: " + ", ".join(sorted(set(value) - known)))
        required = schema.get("required", [])
        missing = set(required) - set(value)
        _require(not missing, f"{where} 缺少字段: " + ", ".join(sorted(missing)))
        properties = schema.get("properties", {})
        for name, child_schema in properties.items():
            if name in value and isinstance(child_schema, dict):
                _validate_json_schema(value[name], child_schema, f"{where}.{name}")


def _schema_type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _bounded_int(value: Any, name: str, low: int, high: int) -> int:
    _require(isinstance(value, int) and not isinstance(value, bool), f"{name} 必须是整数")
    _require(low <= value <= high, f"{name} 必须在 {low} 到 {high} 之间")
    return value


def _safe_relative(value: Any, name: str) -> str:
    _require(isinstance(value, str) and 0 < len(value) <= 512, f"{name} 必须是 1 到 512 字符的相对路径")
    _require("\x00" not in value and "\\" not in value, f"{name} 包含非法字符")
    path = Path(value)
    _require(not path.is_absolute() and not re.match(r"^[A-Za-z]:", value), f"{name} 必须是相对路径")
    _require(all(part not in ("", ".", "..") for part in path.parts), f"{name} 不得包含 . 或 ..")
    return path.as_posix()


def _no_symlink_path(base: Path, relative: str, *, must_be_dir: bool = False) -> Path:
    target = base
    for part in Path(relative).parts:
        target = target / part
        try:
            mode = target.lstat().st_mode
        except OSError as error:
            raise ValueError(f"题目路径不存在: {relative}") from error
        _require(not stat.S_ISLNK(mode), f"题目路径不能经过符号链接: {relative}")
    _require(target.is_dir() if must_be_dir else target.is_file(), f"题目路径类型错误: {relative}")
    return target


def validate_fixture_directory(path: Path) -> tuple[int, int]:
    """Reject symlinks and special files before copying a fixture."""
    _require(path.is_dir(), "fixture_dir 必须是目录")
    file_count = 0
    total_bytes = 0
    node_count = 0
    def fail_walk(error: OSError) -> None:
        raise ValueError(f"fixture 无法完整扫描: {type(error).__name__}") from error

    for current, dirs, files in os.walk(path, topdown=True, followlinks=False, onerror=fail_walk):
        for name in list(dirs):
            entry = Path(current) / name
            mode = entry.lstat().st_mode
            _require(stat.S_ISDIR(mode), f"fixture 含符号链接或特殊目录项: {entry.name}")
            node_count += 1
            _require(node_count <= MAX_FIXTURE_FILES * 2, f"fixture 文件/目录项超过 {MAX_FIXTURE_FILES}")
        for name in files:
            entry = Path(current) / name
            mode = entry.lstat().st_mode
            _require(stat.S_ISREG(mode), f"fixture 含符号链接或特殊文件: {entry.name}")
            size = entry.stat().st_size
            _require(size <= MAX_FIXTURE_FILE_BYTES, f"fixture 单文件超过 {MAX_FIXTURE_FILE_BYTES} bytes")
            file_count += 1
            node_count += 1
            total_bytes += size
            _require(file_count <= MAX_FIXTURE_FILES and node_count <= MAX_FIXTURE_FILES * 2,
                     f"fixture 文件/目录项超过 {MAX_FIXTURE_FILES}")
            _require(total_bytes <= MAX_FIXTURE_BYTES, f"fixture 总大小超过 {MAX_FIXTURE_BYTES} bytes")
    return file_count, total_bytes


def case_from_dict(raw: Any, *, case_file: str | os.PathLike[str] | None = None) -> Case:
    _require(isinstance(raw, dict), "Case 必须是 JSON object")
    _validate_json_schema(raw, CASE_SCHEMA_V1, "Case")
    required = set(CASE_SCHEMA_V1["required"])
    allowed = set(CASE_SCHEMA_V1["properties"])
    _require(not (set(raw) - allowed), "Case 包含未知字段: " + ", ".join(sorted(set(raw) - allowed)))
    _require(required <= set(raw), "Case 缺少字段: " + ", ".join(sorted(required - set(raw))))
    _require(raw.get("schema_version") == SCHEMA_VERSION, "Case schema_version 必须为 1")
    case_id = raw.get("case_id")
    _require(isinstance(case_id, str) and _ID.fullmatch(case_id) is not None, "case_id 格式非法")
    version = raw.get("version")
    _require(isinstance(version, str) and _VERSION.fullmatch(version) is not None, "version 格式非法")
    task = raw.get("task")
    _require(isinstance(task, str) and task.strip() and len(task) <= MAX_TASK_CHARS, "task 不能为空且不得超过 8000 字符")
    _require("\x00" not in task, "task 含 NUL 字符")
    fixture_dir = _safe_relative(raw.get("fixture_dir"), "fixture_dir")
    grader_script = _safe_relative(raw.get("grader_script"), "grader_script")
    _require(grader_script.endswith(".py"), "grader_script 必须是 Python 文件")
    agent_timeout = _bounded_int(raw.get("agent_timeout_seconds"), "agent_timeout_seconds", 1, MAX_AGENT_TIMEOUT_SECONDS)
    grader_timeout = _bounded_int(raw.get("grader_timeout_seconds"), "grader_timeout_seconds", 1, MAX_GRADER_TIMEOUT_SECONDS)
    max_rounds = _bounded_int(raw.get("max_rounds"), "max_rounds", 1, MAX_AGENT_ROUNDS)
    tools = raw.get("allowed_tools")
    _require(isinstance(tools, list) and 1 <= len(tools) <= len(ALLOWED_TOOLS)
             and all(isinstance(item, str) for item in tools), "allowed_tools 必须是非空工具列表")
    _require(len(set(tools)) == len(tools) and set(tools) <= ALLOWED_TOOLS, "allowed_tools 含重复或不支持的工具")
    authorized = raw.get("authorized_tools")
    _require(isinstance(authorized, list) and len(authorized) <= len(ALLOWED_TOOLS)
             and all(isinstance(item, str) for item in authorized), "authorized_tools 必须是工具列表")
    _require(len(set(authorized)) == len(authorized) and set(authorized) <= set(tools), "authorized_tools 必须是 allowed_tools 的无重复子集")
    model_profile = raw.get("model_profile")
    _require(model_profile is None or (isinstance(model_profile, str) and _ID.fullmatch(model_profile)), "model_profile 格式非法")
    description = raw.get("description", "")
    _require(isinstance(description, str) and len(description) <= 1000, "description 超过 1000 字符")
    tags = raw.get("tags", [])
    _require(isinstance(tags, list) and len(tags) <= 32 and all(isinstance(x, str) and len(x) <= 64 for x in tags), "tags 格式非法")

    base = None
    if case_file is not None:
        case_path = Path(case_file).absolute()
        _require(case_path.is_file() and not case_path.is_symlink(), "case.json 必须是普通文件")
        base = case_path.parent.resolve()
        _no_symlink_path(base, fixture_dir, must_be_dir=True)
        grader = _no_symlink_path(base, grader_script)
        _require(grader.stat().st_size <= MAX_CASE_BYTES, f"grader_script 超过 {MAX_CASE_BYTES} bytes")
        fixture = base / fixture_dir
        validate_fixture_directory(fixture)

    return Case(
        case_id, version, task, fixture_dir, grader_script,
        agent_timeout, grader_timeout, max_rounds, tuple(tools), tuple(authorized), model_profile,
        description, tuple(tags), SCHEMA_VERSION, str(base or ""),
    )


def load_case(path: str | os.PathLike[str]) -> Case:
    case_path = Path(path).absolute()
    try:
        if case_path.stat().st_size > MAX_CASE_BYTES:
            raise ValueError(f"case.json 超过 {MAX_CASE_BYTES} bytes")
        raw = json.loads(case_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"无法读取 case.json: {type(error).__name__}") from error
    return case_from_dict(raw, case_file=case_path)


def validate_trial_request(raw: Any) -> TrialRequest:
    _require(isinstance(raw, dict), "TrialRequest 必须是 JSON object")
    _validate_json_schema(raw, TRIAL_REQUEST_SCHEMA_V1, "TrialRequest")
    expected = set(TRIAL_REQUEST_SCHEMA_V1["properties"])
    _require(set(raw) == expected, "TrialRequest 字段不匹配 schema 1")
    _require(raw.get("schema_version") == SCHEMA_VERSION, "TrialRequest schema_version 必须为 1")
    trial_id = raw.get("trial_id")
    _require(isinstance(trial_id, str) and re.fullmatch(r"[a-f0-9-]{36}", trial_id), "trial_id 必须是 UUID")
    run_kind = raw.get("run_kind")
    _require(run_kind in ("live", "fixture"), "run_kind 必须是 live 或 fixture")
    case = case_from_dict(raw.get("case"))
    workspace = raw.get("workspace")
    _require(isinstance(workspace, str) and os.path.isabs(workspace) and len(workspace) <= 4096, "workspace 必须是绝对路径")
    responses = raw.get("responses")
    _require(isinstance(responses, list) and len(responses) <= case.max_rounds, "responses 数量非法")
    _require(all(isinstance(item, dict) for item in responses), "responses 每项必须是 object")
    return TrialRequest(trial_id, run_kind, case.to_dict(), workspace, tuple(responses), SCHEMA_VERSION)


def validate_trial_result(raw: Any) -> dict[str, Any]:
    _require(isinstance(raw, dict), "TrialResult 必须是 JSON object")
    _validate_json_schema(raw, TRIAL_RESULT_SCHEMA_V1, "TrialResult")
    required = set(TRIAL_RESULT_SCHEMA_V1["required"])
    allowed = set(TRIAL_RESULT_SCHEMA_V1["properties"])
    _require(required <= set(raw) <= allowed, "TrialResult 字段不匹配 schema 1")
    _require(raw.get("schema_version") == SCHEMA_VERSION, "TrialResult schema_version 必须为 1")
    _require(raw.get("run_kind") in ("live", "fixture"), "TrialResult run_kind 非法")
    _require(isinstance(raw.get("trial_id"), str) and re.fullmatch(
        r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}", raw["trial_id"]
    ) is not None, "TrialResult trial_id 必须是 UUID")
    _require(isinstance(raw.get("success"), bool), "TrialResult.success 必须是 bool")
    _require(isinstance(raw.get("grader_passed"), (bool, type(None))), "grader_passed 必须是 bool 或 null")
    _require(raw.get("success") is False or (raw.get("grader_passed") is True and raw.get("agent_stop_reason") == "text" and raw.get("agent_state_status") == "done" and raw.get("cleanup_complete") is True), "success 与终态/评分/清理事实不一致")
    if raw.get("success") is True:
        _require(raw.get("failure_kind") == "none" and raw.get("grader_error_kind") is None
                 and raw.get("agent_error_kind") is None
                 and raw.get("agent_started") is True and raw.get("agent_exit_code") == 0
                 and raw.get("agent_stop_reason") == "text" and raw.get("agent_state_status") == "done"
                 and raw.get("grader_passed") is True and raw.get("cleanup_complete") is True
                 and isinstance(raw.get("successful_model_responses"), int)
                 and raw.get("successful_model_responses") > 0,
                 "success 缺少正常 Agent 收束、独立评分或模型响应事实")
    if raw.get("failure_kind") == "none":
        _require(raw.get("success") is True, "failure_kind=none 必须对应 success=true")
    if raw.get("failure_kind") == "agent_timeout":
        _require(raw.get("agent_stop_reason") == "timeout", "agent_timeout 必须对应 timeout 停止原因")
    if raw.get("failure_kind") == "agent_error":
        _require(isinstance(raw.get("agent_error_kind"), str), "agent_error 必须保留 error kind")
    if raw.get("failure_kind") == "grader_infrastructure_error":
        _require(isinstance(raw.get("grader_error_kind"), str), "grader infrastructure error 必须保留 grader error kind")
    grader_result = raw.get("grader_result")
    if grader_result is not None:
        _require(isinstance(grader_result.get("passed"), bool)
                 and raw.get("grader_passed") is grader_result.get("passed"),
                 "grader_result 与 grader_passed 不一致")
    model_ref = raw.get("model_binding_ref")
    if model_ref is not None:
        _require(set(model_ref) == {"profile", "provider", "protocol", "fingerprint"}
                 and all(isinstance(model_ref.get(key), str) for key in model_ref),
                 "model_binding_ref 必须是脱敏来源摘要")
        _require(re.fullmatch(r"[0-9a-f]{64}", model_ref["fingerprint"]) is not None,
                 "model_binding_ref fingerprint 格式非法")
    for timestamp_name in ("started_at", "ended_at"):
        try:
            datetime.fromisoformat(raw[timestamp_name])
        except (TypeError, ValueError) as error:
            raise ValueError(f"{timestamp_name} 必须是 ISO-8601 时间") from error
    _require(raw.get("cost_usd") is None or raw.get("price_snapshot") is not None,
             "没有价格快照时 cost_usd 必须为 null")
    encoded = json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    _require(len(encoded) <= MAX_RESULT_BYTES, f"TrialResult 超过 {MAX_RESULT_BYTES} bytes")
    return raw
