"""State-bound Plan Contract tools."""
from __future__ import annotations

import json

from mini_agent.state import AgentState
from mini_agent.tools.base import Tool


def _result(status: str, **fields: object) -> str:
    return json.dumps({"status": status, **fields}, ensure_ascii=False)


def make_commit_plan_tool(state: AgentState) -> Tool:
    def commit_plan(goal, constraints, success_criteria, steps, reason, **optional):
        revision = state.commit_plan(
            goal, constraints, success_criteria, steps, reason, **optional,
        )
        return _result(
            "committed",
            revision_id=revision.revision_id,
            generation_id=revision.generation_id,
            parent_revision_id=revision.parent_revision_id,
            phase=state.snapshot()["planning_state"]["phase"],
        )

    return Tool(
        name="commit_plan",
        description="提交完整 Plan Contract；结构变化时带当前 parent_revision_id 提交新 revision。纯步骤状态变化请使用 update_plan_progress。",
        parameters={
            "type": "object",
            "properties": {
                "goal": {"type": "string", "minLength": 1, "maxLength": 1200},
                "constraints": {
                    "type": "array", "maxItems": 20,
                    "items": {"type": "string", "minLength": 1, "maxLength": 240},
                },
                "success_criteria": {
                    "type": "array", "minItems": 1, "maxItems": 20,
                    "items": {"type": "string", "minLength": 1, "maxLength": 240},
                },
                "steps": {
                    "type": "array", "minItems": 1, "maxItems": 50,
                    "items": {
                        "type": "object",
                        "properties": {
                            "step_id": {
                                "type": "string", "minLength": 1, "maxLength": 64,
                                "pattern": "^[A-Za-z][A-Za-z0-9_-]{0,63}$",
                            },
                            "content": {"type": "string", "minLength": 1, "maxLength": 240},
                            "depends_on": {
                                "type": "array", "maxItems": 50,
                                "items": {
                                    "type": "string", "minLength": 1, "maxLength": 64,
                                    "pattern": "^[A-Za-z][A-Za-z0-9_-]{0,63}$",
                                },
                            },
                            "success_criteria": {
                                "type": "array", "minItems": 1, "maxItems": 10,
                                "items": {"type": "string", "minLength": 1, "maxLength": 240},
                            },
                            "replaces": {
                                "type": "array", "maxItems": 50,
                                "items": {
                                    "type": "string", "minLength": 1, "maxLength": 64,
                                    "pattern": "^[A-Za-z][A-Za-z0-9_-]{0,63}$",
                                },
                            },
                        },
                        "required": ["step_id", "content", "depends_on", "success_criteria", "replaces"],
                        "additionalProperties": False,
                    },
                },
                "reason": {"type": "string", "minLength": 1, "maxLength": 600},
                "parent_revision_id": {"type": "integer", "minimum": 1},
            },
            "required": ["goal", "constraints", "success_criteria", "steps", "reason"],
            "additionalProperties": False,
        },
        handler=commit_plan,
    )


def make_update_plan_progress_tool(state: AgentState) -> Tool:
    def update_plan_progress(revision_id, step_id, status, reason):
        event = state.update_plan_progress(revision_id, step_id, status, reason)
        return _result(
            "progress_updated",
            progress_id=event.progress_id,
            revision_id=event.revision_id,
            step_id=event.step_id,
            from_status=event.from_status,
            to_status=event.to_status,
            generation_id=event.generation_id,
        )

    return Tool(
        name="update_plan_progress",
        description="推进当前 active Plan Contract 的一个步骤状态；只允许 pending→in_progress→completed。",
        parameters={
            "type": "object",
            "properties": {
                "revision_id": {"type": "integer", "minimum": 1},
                "step_id": {
                    "type": "string", "minLength": 1, "maxLength": 64,
                    "pattern": "^[A-Za-z][A-Za-z0-9_-]{0,63}$",
                },
                "status": {"type": "string", "enum": ["in_progress", "completed"]},
                "reason": {"type": "string", "minLength": 1, "maxLength": 600},
            },
            "required": ["revision_id", "step_id", "status", "reason"],
            "additionalProperties": False,
        },
        handler=update_plan_progress,
    )
