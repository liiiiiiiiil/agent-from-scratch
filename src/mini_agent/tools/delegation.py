"""Parent-only synchronous and process-local background Subagent tools."""

from __future__ import annotations

import json
import re
from typing import Any

from mini_agent.delegation import (
    DELEGATION_MIN_RESULT_BYTES,
    DelegationManager,
    validate_delegation_arguments,
)
from mini_agent.tools.base import Tool


_CHILD_SESSION_ID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)


def _contract_tool(parent_state: Any, manager: DelegationManager, *, background: bool) -> Tool:
    def validate(arguments: dict[str, Any]) -> None:
        normalized = validate_delegation_arguments(
            arguments, parent_state, manager.provider_catalog,
            manager.agent_profile_catalog,
        )
        if background:
            if normalized.get("purpose") != "investigation":
                raise ValueError("spawn_subagent 的 purpose 固定为 investigation")
            if not normalized.get("agent_profile"):
                raise ValueError("spawn_subagent 必须显式提供 agent_profile")

    def run(**arguments: Any) -> str:
        if background:
            return manager.spawn_background(arguments, parent_state)
        return manager.run(arguments, parent_state).to_json()

    profile_schema: dict[str, Any] = {"type": "string", "maxLength": 120}
    if manager.provider_catalog is not None:
        profile_schema["enum"] = list(manager.provider_catalog.subagent_allowed_profiles)
    agent_profile_schema: dict[str, Any] = {
        "type": "string", "maxLength": 64,
        "enum": [item.profile_id for item in manager.agent_profile_catalog.profiles],
        "description": "选择调查角色；各角色的用途见工具说明。",
    }
    profile_descriptions = "\n".join(
        f"- {item.profile_id}: {item.description}"
        for item in manager.agent_profile_catalog.profiles
    )
    name = "spawn_subagent" if background else "delegate_task"
    if background:
        purpose_schema = {"type": "string", "enum": ["investigation"]}
        purpose_description = "purpose 固定为 investigation。"
        required_role = ["agent_profile"]
        description = (
            "启动一项进程内后台只读调查并立即返回唯一启动确认。必须提供 agent_profile；"
            "父 Agent 可继续工作，之后用 get_subagent_status 查询并用 get_subagent_result 领取。"
            "结果不会自动进入父任务或成为父侧 verification evidence。角色如下：\n"
            + profile_descriptions
        )
    else:
        purpose_schema = {
            "type": "string",
            "enum": ["investigation", "diagnosis", "crash_investigation"],
        }
        purpose_description = "选择调查、失败诊断或崩溃恢复调查用途。"
        required_role = []
        description = (
            "同步委派一个单层、只读子代理。可选 agent_profile 指定具名角色，"
            "model_profile 指定子模型；未指定角色时保留通用兼容合同。角色如下：\n"
            + profile_descriptions
        )

    return Tool(
        name=name,
        description=description,
        parameters={
            "type": "object", "additionalProperties": False,
            "properties": {
                "goal": {"type": "string", "minLength": 1, "maxLength": 4000},
                "scope": {"type": "array", "minItems": 1, "maxItems": 8,
                          "items": {"type": "string", "maxLength": 500}},
                "constraints": {"type": "array", "maxItems": 32,
                                "items": {"type": "string", "maxLength": 2000}},
                "expected_findings": {"type": "array", "maxItems": 32,
                                      "items": {"type": "string", "maxLength": 2000}},
                "requested_tools": {
                    "type": "array", "minItems": 1, "maxItems": 4,
                    "items": {"type": "string", "enum": [
                        "calculate", "read_file", "list_dir", "grep",
                    ]},
                },
                "selected_parent_facts": {"type": "array", "maxItems": 32,
                                           "items": {"type": "string", "maxLength": 2000}},
                "purpose": {**purpose_schema, "description": purpose_description},
                "source_id": {"type": "string", "maxLength": 200},
                "model_profile": profile_schema,
                "agent_profile": agent_profile_schema,
                "budget": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "max_rounds": {"type": "integer", "minimum": 1, "maximum": 8},
                        "max_llm_calls": {"type": "integer", "minimum": 1, "maximum": 8},
                        "max_tool_calls": {"type": "integer", "minimum": 1, "maximum": 24},
                        "max_tokens": {"type": "integer", "minimum": 1, "maximum": 32000},
                        "max_result_bytes": {"type": "integer",
                                              "minimum": DELEGATION_MIN_RESULT_BYTES,
                                              "maximum": 12288},
                        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120},
                    },
                },
            },
            "required": [
                "goal", "scope", "constraints", "expected_findings", "requested_tools",
                "selected_parent_facts", "purpose", *required_role,
            ],
        },
        handler=run,
        argument_validator=validate,
    )


def make_delegate_task_tool(parent_state: Any, manager: DelegationManager) -> Tool:
    return _contract_tool(parent_state, manager, background=False)


def make_background_subagent_tools(parent_state: Any, manager: DelegationManager) -> tuple[Tool, ...]:
    """Build the parent-only tools for asynchronous investigation."""
    spawn = _contract_tool(parent_state, manager, background=True)

    def validate_id(arguments: dict[str, Any]) -> None:
        value = arguments.get("child_session_id")
        if not isinstance(value, str) or _CHILD_SESSION_ID.fullmatch(value) is None:
            raise ValueError("child_session_id 必须是 UUID")

    id_schema = {
        "type": "object", "additionalProperties": False,
        "properties": {"child_session_id": {
            "type": "string", "minLength": 36, "maxLength": 36,
            "pattern": _CHILD_SESSION_ID.pattern,
        }},
        "required": ["child_session_id"],
    }

    def get_status(child_session_id: str) -> str:
        return json.dumps(
            manager.background_status(child_session_id, parent_state),
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )

    def get_result(child_session_id: str) -> str:
        return manager.background_result(child_session_id, parent_state)

    def cancel(child_session_id: str) -> str:
        return json.dumps(
            manager.cancel_background(child_session_id),
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )

    followup_parameters = json.loads(json.dumps(spawn.parameters))
    followup_parameters["properties"].pop("agent_profile", None)
    followup_parameters["properties"].pop("model_profile", None)
    followup_parameters["properties"].pop("source_id", None)
    followup_parameters["properties"]["child_session_id"] = {
        "type": "string", "minLength": 36, "maxLength": 36,
        "pattern": _CHILD_SESSION_ID.pattern,
    }
    followup_parameters["required"] = [
        "child_session_id",
        *(name for name in followup_parameters["required"]
          if name not in {"agent_profile", "model_profile", "source_id"}),
    ]

    def validate_followup(arguments: dict[str, Any]) -> None:
        validate_id(arguments)
        manager.validate_followup_arguments(arguments, parent_state)

    def followup(**arguments: Any) -> str:
        return manager.followup_background(arguments, parent_state)

    return (
        spawn,
        Tool(
            name="followup_subagent",
            description=(
                "为已领取 completed 结果的 child_session_id 提交一份完整的新调查合同并启动下一轮。"
                "角色、模型沿用子会话；本轮 Skill 权限会重新申请。之后仍用 get_subagent_result 领取结果。"
            ),
            parameters=followup_parameters, handler=followup,
            argument_validator=validate_followup,
        ),
        Tool(
            name="get_subagent_status",
            description="按 child_session_id 查询有界状态与 result_id，不返回调查正文。",
            parameters=id_schema, handler=get_status, argument_validator=validate_id,
        ),
        Tool(
            name="get_subagent_result",
            description=("按 child_session_id 领取已收束的 SubagentResult；未收束时只返回状态。"
                         "重复领取返回同一结果，不重跑且不重复结算。"),
            parameters=id_schema, handler=get_result, argument_validator=validate_id,
        ),
        Tool(
            name="cancel_subagent",
            description="按 child_session_id 发出协作式取消请求；最终结果仍由 get_subagent_result 领取。",
            parameters=id_schema, handler=cancel, argument_validator=validate_id,
        ),
    )
