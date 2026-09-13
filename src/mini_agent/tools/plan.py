"""State-bound Plan Contract tools."""
from __future__ import annotations

import json

from mini_agent.state import AgentState
from mini_agent.tools.base import Tool


def _result(status: str, **fields: object) -> str:
    return json.dumps({"status": status, **fields}, ensure_ascii=False)


def make_begin_plan_tool(state: AgentState) -> Tool:
    def begin_plan():
        planning = state.begin_plan()
        return _result("exploring", phase=planning.phase)

    return Tool(
        name="begin_plan",
        description="普通任务需要先调查时，进入 Runtime 强制的只读规划阶段。",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=begin_plan,
    )


def make_cancel_planning_tool(state: AgentState) -> Tool:
    def cancel_planning():
        planning = state.cancel_planning()
        return _result("planning_cancelled", phase=planning.phase)

    return Tool(
        name="cancel_planning",
        description="普通任务尚未提交计划时，退出只读规划并回到 Direct Path。",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=cancel_planning,
    )


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
            trigger_id=revision.trigger_id,
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
                "trigger_id": {"type": "integer", "minimum": 1},
            },
            "required": ["goal", "constraints", "success_criteria", "steps", "reason"],
            "additionalProperties": False,
        },
        handler=commit_plan,
    )


def make_request_replan_tool(state: AgentState) -> Tool:
    def request_replan(kind, source_id, reason):
        trigger = state.request_replan(kind, source_id, reason)
        return _result(
            "replan_requested",
            trigger_id=trigger.trigger_id,
            kind=trigger.kind,
            source_id=source_id,
            phase=state.snapshot()["planning_state"]["phase"],
            replans_remaining=state.snapshot()["planning_state"]["replans_remaining"],
        )

    return Tool(
        name="request_replan",
        description=(
            "引用真实 failure 或成功只读观察，请求进入只读 Explore 并提交修订计划；"
            "不能伪造用户反馈或 blocked 恢复来源。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["failure", "observation"]},
                "source_id": {"type": "string", "minLength": 1, "maxLength": 120},
                "reason": {"type": "string", "minLength": 1, "maxLength": 600},
            },
            "required": ["kind", "source_id", "reason"],
            "additionalProperties": False,
        },
        handler=request_replan,
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
