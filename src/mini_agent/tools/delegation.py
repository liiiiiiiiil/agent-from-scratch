"""Parent-only delegate_task tool."""

from __future__ import annotations

from typing import Any

from mini_agent.delegation import (
    DELEGATION_MIN_RESULT_BYTES,
    DelegationManager,
    validate_delegation_arguments,
)
from mini_agent.tools.base import Tool


def make_delegate_task_tool(parent_state: Any, manager: DelegationManager) -> Tool:
    def validate(arguments: dict[str, Any]) -> None:
        validate_delegation_arguments(arguments, parent_state, manager.provider_catalog)

    def delegate_task(**arguments: Any) -> str:
        result = manager.run(arguments, parent_state)
        return result.to_json()

    profile_schema: dict[str, Any] = {"type": "string", "maxLength": 120}
    if manager.provider_catalog is not None:
        profile_schema["enum"] = list(manager.provider_catalog.subagent_allowed_profiles)

    return Tool(
        name="delegate_task",
        description="委派一个单层、只读的调查子代理；同一回合可提交多个相互独立的委派，结果按 tool-call 顺序返回结构化 JSON 发现",
        parameters={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "goal": {"type": "string", "minLength": 1, "maxLength": 4000},
                "scope": {
                    "type": "array", "minItems": 1, "maxItems": 8,
                    "items": {"type": "string", "maxLength": 500},
                },
                "constraints": {
                    "type": "array", "maxItems": 32,
                    "items": {"type": "string", "maxLength": 2000},
                },
                "expected_findings": {
                    "type": "array", "maxItems": 32,
                    "items": {"type": "string", "maxLength": 2000},
                },
                "requested_tools": {
                    "type": "array", "minItems": 1, "maxItems": 4,
                    "items": {"type": "string", "enum": ["calculate", "read_file", "list_dir", "grep"]},
                },
                "selected_parent_facts": {
                    "type": "array", "maxItems": 32,
                    "items": {"type": "string", "maxLength": 2000},
                },
                "purpose": {
                    "type": "string",
                    "enum": ["investigation", "diagnosis", "crash_investigation"],
                },
                "source_id": {"type": "string", "maxLength": 200},
                "model_profile": profile_schema,
                "budget": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "max_rounds": {"type": "integer", "minimum": 1, "maximum": 8},
                        "max_llm_calls": {"type": "integer", "minimum": 1, "maximum": 8},
                        "max_tool_calls": {"type": "integer", "minimum": 1, "maximum": 24},
                        "max_tokens": {"type": "integer", "minimum": 1, "maximum": 32000},
                        "max_result_bytes": {
                            "type": "integer", "minimum": DELEGATION_MIN_RESULT_BYTES,
                            "maximum": 12288,
                        },
                        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120},
                    },
                },
            },
            "required": [
                "goal", "scope", "constraints", "expected_findings", "requested_tools",
                "selected_parent_facts", "purpose",
            ],
        },
        handler=delegate_task,
        argument_validator=validate,
    )
