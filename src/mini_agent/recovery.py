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

    def recover(self, action: str, caused_by_failure_id: str, reason: str,
                requested_attempt: str | None = None,
                requested_tool: str | None = None,
                requested_arguments: dict[str, Any] | None = None) -> str:
        if not isinstance(reason, str) or not (1 <= len(reason) <= 500):
            return self._result("rejected", "reason 长度必须为 1-500")
        if action == "rollback":
            return self._result("rejected", "v0.18 不支持 rollback")
        if action == "adjust":
            try:
                tool = self.executor.registry.get(requested_tool)
                requested_arguments = validate_arguments(tool.parameters, requested_arguments or {})
            except (TypeError, ValueError) as exc:
                return self._result("rejected", f"adjust 参数无效: {exc}")
        record, reservation, args = self.state.reserve_recovery(
            action, caused_by_failure_id, reason, requested_attempt,
            requested_tool, requested_arguments)
        if record.status == "rejected":
            return self._result("rejected", self.state.recovery_notice, record.recovery_id)
        if action in ("ask", "block"):
            return self._result(record.status, record.reason, record.recovery_id, record.generation_id)
        result = self.executor.execute_result(record.requested_tool or requested_tool, args or {},
                                               state=self.state, notify=False, reservation=reservation)
        self.state.record_execution_result(result)
        action = next(a for a in self.state.recovery_actions if a.recovery_id == record.recovery_id)
        return self._result(action.status, result.output_excerpt, record.recovery_id,
                            action.result_generation_id, action.result_attempt)

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
        description="根据 Structured State 对最近失败采取受限恢复动作。retry 必须引用精确 attempt；adjust 提供新工具和参数；ask/block 会停止当前任务。v0.18 不支持 rollback。",
        parameters={"type":"object", "properties": {
            "action": {"type":"string", "enum":["retry","adjust","ask","block"]},
            "caused_by_failure_id": {"type":"string"},
            "reason": {"type":"string", "minLength":1, "maxLength":500},
            "requested_attempt": {"type":"string"},
            "requested_tool": {"type":"string"},
            "requested_arguments": {"type":"object"},
        }, "required":["action","caused_by_failure_id","reason"], "additionalProperties":False},
        handler=runtime.recover,
    )
