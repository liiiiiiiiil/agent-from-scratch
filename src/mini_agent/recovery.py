"""v0.18 bounded recovery policy runtime."""
from __future__ import annotations

import json
from typing import Any

from mini_agent.state import AgentState
from mini_agent.tools.base import ToolExecutor, validate_arguments

MAX_RECOVERY_RESULT_LENGTH = 1200


class RecoveryRuntime:
    def __init__(self, state: AgentState, executor: ToolExecutor):
        self.state, self.executor = state, executor
        self.checkpoint_store = getattr(state, "checkpoint_store", None)

    def bind_executor(self, executor: ToolExecutor) -> None:
        self.executor = executor

    def _reject(self, action, failure_id, reason, detail,
                requested_attempt=None, requested_tool=None,
                requested_arguments=None, checkpoint_id=None,
                block=False) -> str:
        record = self.state.reject_recovery(
            action, failure_id, reason, detail,
            requested_attempt, requested_tool, requested_arguments,
            checkpoint_id, block,
        )
        return self._result("rejected", detail, record.recovery_id)

    def recover(self, action: str, caused_by_failure_id: str, reason: str,
                requested_attempt: str | None = None,
                requested_tool: str | None = None,
                requested_arguments: dict[str, Any] | None = None,
                checkpoint_id: str | None = None) -> str:
        if not isinstance(reason, str) or not (1 <= len(reason) <= 500):
            return self._reject(action, caused_by_failure_id, reason, "reason 长度必须为 1-500",
                                requested_attempt, requested_tool, requested_arguments,
                                checkpoint_id)
        if action == "rollback" and self.checkpoint_store is None:
            return self._reject(action, caused_by_failure_id, reason, "checkpoint store 不可用",
                                requested_attempt, requested_tool, requested_arguments,
                                checkpoint_id, block=True)
        if action != "rollback" and checkpoint_id is not None:
            return self._reject(action, caused_by_failure_id, reason,
                                "checkpoint_id 仅供 rollback 使用",
                                requested_attempt, requested_tool, requested_arguments,
                                checkpoint_id)
        if action == "adjust":
            if requested_tool in ("recover", "commit_plan", "update_plan_progress"):
                return self._reject(action, caused_by_failure_id, reason, "control/plan 工具不能作为恢复目标",
                                    requested_attempt, requested_tool, requested_arguments)
            try:
                tool = self.executor.registry.get(requested_tool)
                if tool.internal:
                    return self._reject(action, caused_by_failure_id, reason,
                                        "internal 工具不能作为恢复目标",
                                        requested_attempt, requested_tool, requested_arguments)
                requested_arguments = validate_arguments(tool.parameters, requested_arguments)
            except (TypeError, ValueError) as exc:
                return self._reject(action, caused_by_failure_id, reason, f"adjust 参数无效: {exc}",
                                    requested_attempt, requested_tool, requested_arguments)
        elif action == "retry" and requested_attempt is not None:
            source_attempt = next(
                (attempt for attempt in self.state.attempts
                 if attempt.attempt_id == requested_attempt), None
            )
            if source_attempt is not None:
                try:
                    source_tool = self.executor.registry.get(source_attempt.tool)
                except (TypeError, ValueError):
                    source_tool = None
                if (source_attempt.tool in ("commit_plan", "update_plan_progress") or
                        (source_tool is not None and source_tool.internal)):
                    return self._reject(action, caused_by_failure_id, reason,
                                        "control/plan 工具不能作为恢复目标",
                                        requested_attempt, requested_tool, requested_arguments)
        target, detail = self.state.recovery_target(
            action, caused_by_failure_id, requested_attempt,
            requested_tool, requested_arguments, checkpoint_id,
        )
        if detail:
            return self._reject(action, caused_by_failure_id, reason, detail,
                                requested_attempt, requested_tool, requested_arguments,
                                checkpoint_id, block=action == "rollback")
        record, reservation, args = self.state.reserve_recovery(
            action, caused_by_failure_id, reason, requested_attempt,
            requested_tool, requested_arguments, defer_generation=True,
            checkpoint_id=checkpoint_id)
        if record.status == "rejected":
            return self._result("rejected", self.state.recovery_notice, record.recovery_id)
        if target is not None:
            target_tool, target_arguments = target
            authorization_arguments = target_arguments
            if action == "rollback":
                checkpoint = self.checkpoint_store.get(record.checkpoint_id)
                authorization_arguments = {"path": checkpoint.path}
            try:
                denied = self.executor.authorize(target_tool, authorization_arguments)
            except Exception as exc:
                denied = f"权限检查失败: {type(exc).__name__}"
            if denied:
                self.state.deny_reserved_recovery(record, denied)
                return self._result("rejected", denied, record.recovery_id)
        record, reservation, args = self.state.activate_recovery(record, args)
        if record.status == "rejected":
            return self._result("rejected", self.state.recovery_notice, record.recovery_id)
        if action in ("ask", "block"):
            return self._result(record.status, record.reason, record.recovery_id, record.generation_id)
        if action == "rollback":
            result = self.executor.execute_internal_result(
                "rollback_checkpoint", {"checkpoint_id": record.checkpoint_id},
                state=self.state, notify=False, reservation=reservation,
                permission_already_checked=True,
            )
        else:
            result = self.executor.execute_result(
                record.requested_tool or requested_tool, args or {},
                state=self.state, notify=False, reservation=reservation,
                permission_already_checked=True,
            )
        self.state.record_execution_result(result)
        action = next(a for a in self.state.recovery_actions if a.recovery_id == record.recovery_id)
        return self._result(
            action.status,
            f"执行事实: {result.output_excerpt}; 未代表任务已修复，请独立验证",
            record.recovery_id, action.result_generation_id, action.result_attempt,
        )

    def _result(self, status, message, recovery_id=None, generation_id=None, attempt_id=None):
        payload = {"status": status, "message": str(message)[:MAX_RECOVERY_RESULT_LENGTH]}
        if recovery_id: payload["recovery_id"] = recovery_id
        if generation_id is not None: payload["generation_id"] = generation_id
        if attempt_id: payload["result_attempt"] = attempt_id
        return json.dumps(payload, ensure_ascii=False)


def make_recover_tool(runtime: RecoveryRuntime):
    from mini_agent.tools.base import Tool
    return Tool(
        name="recover",
        description="根据 Structured State 对最近失败采取受限恢复动作。retry 必须引用精确 attempt；adjust 提供新工具和参数；rollback 仅可引用 ready checkpoint；ask/block 会停止当前任务。",
        parameters={"type":"object", "properties": {
            "action": {"type":"string", "enum":["retry","adjust","ask","block","rollback"]},
            "caused_by_failure_id": {"type":"string"},
            "reason": {"type":"string", "minLength":1, "maxLength":500},
            "requested_attempt": {"type":"string"},
            "requested_tool": {
                "type":"string",
                "description":"严格匹配已注册工具名，例如 edit_file；不要使用 functions.edit_file。",
            },
            "requested_arguments": {
                "type":"object",
                "description":"目标工具的完整参数；例如 edit_file 可提供 path、old_string、new_string 和可选 replace_all。",
            },
            "checkpoint_id": {
                "type":"string",
                "description":"rollback 必填；只能引用当前任务 Structured State 中状态为 ready 的 checkpoint。",
            },
        },
        "required":["action","caused_by_failure_id","reason"],
        "additionalProperties":False,
        # The lightweight validator performs the final action-specific check
        # in RecoveryRuntime; this standard JSON Schema branch tells capable
        # providers that rollback has an extra required field.
        "oneOf":[
            {"properties":{"action":{"enum":["retry","adjust","ask","block"]}}},
            {"properties":{"action":{"enum":["rollback"]}}, "required":["checkpoint_id"]},
        ],
        },
        handler=runtime.recover,
    )
