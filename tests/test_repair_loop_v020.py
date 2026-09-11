"""v0.20 repair-loop state machine and phase-gate tests."""

import json
from unittest.mock import patch

from mini_agent.permission import ALLOW, PermissionGate, PermissionPolicy
from mini_agent.state import AgentState
from mini_agent.tools import create_registry
from mini_agent.tools.base import Tool, ToolExecutor


def _executor(state, extra=None):
    registry = create_registry(state)
    rules = {"run_shell": ALLOW, "recover": ALLOW}
    if extra:
        registry.register(extra)
        rules[extra.name] = ALLOW
    return ToolExecutor(registry, PermissionGate(PermissionPolicy(rules)))


def _record(executor, state, name, arguments):
    result = executor.execute_result(name, arguments, state)
    if name != "recover":
        state.record_execution_result(result)
    return result


def test_failure_enters_diagnosis_without_consuming_cycle_and_gates_effects():
    state = AgentState()
    state.begin_task("diagnose")
    executor = _executor(state)
    failed = _record(executor, state, "run_shell", {
        "command": "false", "purpose": "verification",
    })

    assert failed.error_kind == "nonzero_exit"
    loop = state.snapshot()["repair_loop"]
    assert loop["phase"] == "diagnosis_required"
    assert loop["active_failure_id"] == "f-1"
    assert loop["cycles_used"] == 0
    gated = executor.execute_result("run_shell", {
        "command": "echo side effect", "purpose": "execution",
    }, state)
    assert gated.error_kind == "repair_phase_gate"
    assert not gated.handler_admitted
    assert state.active_failure_id == "f-1"


def test_recovery_activation_consumes_cycle_then_requires_independent_verification():
    state = AgentState()
    state.begin_task("retry")
    registry = create_registry(state)
    outputs = iter(("[exit=1] bad", "[exit=0] recovered", "[exit=0] verified"))
    shell_tool = registry.get("run_shell")
    original_handler = shell_tool.handler
    shell_tool.handler = lambda command, purpose="execution": next(outputs)
    executor = ToolExecutor(registry, PermissionGate(PermissionPolicy({
        "run_shell": ALLOW, "recover": ALLOW,
    })))
    try:
        _record(executor, state, "run_shell", {"command": "check", "purpose": "verification"})
        recovery = _record(executor, state, "recover", {
            "action": "retry", "caused_by_failure_id": "f-1",
            "reason": "temporary check failure", "requested_attempt": "a-1",
        })
        payload = json.loads(recovery.output)
        assert payload["status"] == "executed"
        loop = state.snapshot()["repair_loop"]
        assert loop["phase"] == "verification_required"
        assert loop["cycles_used"] == 1
        blocked = executor.execute_result("run_shell", {
            "command": "check", "purpose": "execution",
        }, state)
        assert blocked.error_kind == "repair_phase_gate"
        _record(executor, state, "run_shell", {"command": "check", "purpose": "verification"})
        assert state.snapshot()["repair_loop"]["phase"] == "idle"
        assert state.has_verification_evidence()
        assert state.snapshot()["recovery_notice"] == ""
    finally:
        shell_tool.handler = original_handler


def test_recovery_must_reference_active_failure_and_rejection_does_not_consume_cycle():
    state = AgentState()
    state.begin_task("active failure")
    executor = _executor(state)
    _record(executor, state, "run_shell", {"command": "false", "purpose": "verification"})
    result = executor.execute_result("recover", {
        "action": "retry", "caused_by_failure_id": "f-999",
        "reason": "wrong failure", "requested_attempt": "a-1",
    }, state)
    payload = json.loads(result.output)
    assert payload["status"] == "rejected"
    assert state.snapshot()["repair_loop"]["cycles_used"] == 0
    assert state.snapshot()["current_generation_id"] == 0


def test_ordinary_possible_effects_do_not_enter_strict_repair_verification():
    state = AgentState()
    state.begin_task("multi-step mutation")
    calls = []
    tool = Tool(
        "mutate", "mutate", {"type": "object", "properties": {}},
        lambda: calls.append("called") or "ok", effect_class="possible",
    )
    executor = _executor(state, tool)

    _record(executor, state, "mutate", {})
    second = _record(executor, state, "mutate", {})

    assert second.outcome == "succeeded"
    assert calls == ["called", "called"]
    snapshot = state.snapshot()
    assert snapshot["repair_loop"]["phase"] == "idle"
    assert snapshot["verification_required"]
    assert snapshot["current_generation_id"] == 2


def test_last_repair_cycle_failure_immediately_fails_task():
    state = AgentState()
    state.begin_task("exhaust repair")
    registry = create_registry(state)
    outputs = iter(("[exit=1] initial", "[exit=0] recovery", "[exit=1] still broken"))
    shell_tool = registry.get("run_shell")
    original_handler = shell_tool.handler
    shell_tool.handler = lambda command, purpose="execution": next(outputs)
    executor = ToolExecutor(registry, PermissionGate(PermissionPolicy({
        "run_shell": ALLOW, "recover": ALLOW,
    })))
    try:
        with patch("mini_agent.state.MAX_REPAIR_CYCLES", 1):
            _record(executor, state, "run_shell", {"command": "check", "purpose": "verification"})
            _record(executor, state, "recover", {
                "action": "retry", "caused_by_failure_id": "f-1",
                "reason": "retry the failed check", "requested_attempt": "a-1",
            })
            _record(executor, state, "run_shell", {"command": "check", "purpose": "verification"})
    finally:
        shell_tool.handler = original_handler

    assert state.status == "failed"
    assert "Repair cycle" in state.terminal_reason
    assert state.snapshot()["repair_loop"]["cycles_used"] == 1


def test_repair_cycle_is_reserved_before_authorization_and_released_on_denial():
    state = AgentState()
    state.begin_task("atomic repair budget")
    executor = _executor(state)
    _record(executor, state, "run_shell", {"command": "false", "purpose": "verification"})

    with patch("mini_agent.state.MAX_REPAIR_CYCLES", 1):
        first, _, _ = state.reserve_recovery(
            "adjust", "f-1", "first candidate",
            requested_tool="run_shell",
            requested_arguments={"command": "echo first", "purpose": "execution"},
            defer_generation=True,
        )
        assert first.status == "proposed"
        assert state.snapshot()["repair_loop"]["cycles_remaining"] == 0

        second, _, _ = state.reserve_recovery(
            "adjust", "f-1", "concurrent candidate",
            requested_tool="run_shell",
            requested_arguments={"command": "echo second", "purpose": "execution"},
            defer_generation=True,
        )
        assert second.status == "rejected"
        assert state.snapshot()["repair_loop"]["cycles_used"] == 0

        state.deny_reserved_recovery(first, "permission denied")
        assert state.snapshot()["repair_loop"]["cycles_remaining"] == 1
