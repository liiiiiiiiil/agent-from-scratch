"""v0.18 recovery-policy regression tests."""

import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mini_agent.agent import agent_loop
from mini_agent.context import ContextManager
from mini_agent.permission import ALLOW, DENY, PermissionGate, PermissionPolicy
from mini_agent.state import AgentState
from mini_agent.tools import create_registry
from mini_agent.tools.base import Tool, ToolExecutor, ToolRegistry
from mini_agent.tools.file import (
    EditMultipleMatchesError,
    EditNoMatchError,
    edit_file,
)


def _executor(state, registry, rules):
    return ToolExecutor(registry, PermissionGate(PermissionPolicy(rules)))


def _nonzero_failure(state):
    registry = create_registry(state)
    executor = _executor(state, registry, {"run_shell": ALLOW, "recover": ALLOW})
    result = executor.execute_result(
        "run_shell", {"command": "false", "purpose": "execution"}, state
    )
    state.record_execution_result(result)
    return registry, executor


def test_edit_preconditions_are_typed_and_deterministic():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "sample.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("foo\nfoo\n")

        try:
            edit_file(path, "missing", "new")
            assert False, "没有匹配应抛出专用前置条件异常"
        except EditNoMatchError as error:
            assert isinstance(error, ValueError)
        try:
            edit_file(path, "foo", "bar")
            assert False, "多处匹配应抛出专用前置条件异常"
        except EditMultipleMatchesError as error:
            assert isinstance(error, ValueError)

        state = AgentState()
        state.begin_task("adjust edit")
        registry = create_registry(state)
        executor = _executor(state, registry, {"edit_file": ALLOW})
        result = executor.execute_result(
            "edit_file",
            {"path": path, "old_string": "missing", "new_string": "new"},
            state,
        )
        state.record_execution_result(result)

        with open(path, "r", encoding="utf-8") as handle:
            assert handle.read() == "foo\nfoo\n"
        assert result.error_kind == "edit_no_match"
        assert state.snapshot()["failures"][0]["category"] == "deterministic"
        assert state.snapshot()["current_generation_id"] == 1
        assert state.status == "running"

        # v0.20 requires diagnosis/recovery before another possible effect;
        # use a fresh task to exercise the file handler's second precondition.
        state = AgentState()
        state.begin_task("adjust edit second precondition")
        registry = create_registry(state)
        executor = _executor(state, registry, {"edit_file": ALLOW})
        multi = executor.execute_result(
            "edit_file",
            {"path": path, "old_string": "foo", "new_string": "bar"},
            state,
        )
        state.record_execution_result(multi)
        assert multi.error_kind == "edit_multiple_matches"
        assert state.snapshot()["failures"][-1]["category"] == "deterministic"
        assert state.status == "running"


def test_invalid_recovery_is_recorded_once_without_generation():
    state = AgentState()
    state.begin_task("recover")
    _, executor = _nonzero_failure(state)
    before_generation = state.current_generation_id

    result = executor.execute_result(
        "recover",
        {
            "action": "adjust",
            "caused_by_failure_id": "f-1",
            "reason": "missing target arguments",
        },
        state,
    )
    payload = json.loads(result.output)
    snapshot = state.snapshot()
    assert payload["status"] == "rejected"
    assert payload["recovery_id"] == "r-1"
    assert "参数" in payload["message"]
    assert len(snapshot["recovery_actions"]) == 1
    assert snapshot["recovery_actions"][0]["status"] == "rejected"
    assert snapshot["current_generation_id"] == before_generation
    assert snapshot["budgets"]["recovery_actions_remaining"] == 7


def test_recovery_schema_rejection_is_recorded_once_in_agent_protocol():
    state = AgentState()
    state.begin_task("schema")
    registry, executor = _nonzero_failure(state)
    context = ContextManager(state, [{"role": "user", "content": "schema"}])
    responses = [
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "recover-bad", "type": "function", "function": {
                "name": "recover",
                "arguments": json.dumps({
                    "action": "adjust", "caused_by_failure_id": "f-1",
                    "reason": "bad schema", "unexpected": True,
                }),
            },
        }]},
        {"role": "assistant", "content": "explain"},
        {"role": "assistant", "content": "explain"},
    ]
    with patch("mini_agent.agent.call_llm", side_effect=responses):
        with redirect_stdout(StringIO()):
            agent_loop(context, executor)
    assert len(state.snapshot()["recovery_actions"]) == 1
    assert state.snapshot()["recovery_actions"][0]["status"] == "rejected"
    assert "recovery_id" in context.history[2]["content"]


def test_recovery_target_uses_same_gate_before_reservation():
    state = AgentState()
    state.begin_task("permission")
    registry = create_registry(state)
    calls = []
    registry.register(Tool(
        "target", "target", {"type": "object", "properties": {}},
        lambda: calls.append("called") or "done", effect_class="possible",
    ))
    executor = _executor(
        state, registry, {"target": DENY, "recover": ALLOW, "run_shell": ALLOW}
    )
    failed = executor.execute_result(
        "run_shell", {"command": "false", "purpose": "execution"}, state
    )
    state.record_execution_result(failed)
    before_generation = state.current_generation_id

    result = executor.execute_result(
        "recover",
        {
            "action": "adjust",
            "caused_by_failure_id": "f-1",
            "reason": "try target",
            "requested_tool": "target",
            "requested_arguments": {},
        },
        state,
    )
    payload = json.loads(result.output)
    assert payload["status"] == "rejected"
    assert payload["recovery_id"] == "r-1"
    assert state.current_generation_id == before_generation
    assert calls == []
    assert state.snapshot()["recovery_actions"][0]["status"] == "rejected"


def test_retry_reuses_original_arguments_and_rejects_non_retryable_failure():
    state = AgentState()
    state.begin_task("retry")
    registry = create_registry(state)
    seen = []
    responses = iter(("[timeout] first", "ok"))
    registry.register(Tool(
        "flaky", "flaky", {
            "type": "object", "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        lambda value: seen.append(value) or next(responses), effect_class="none",
    ))
    executor = _executor(state, registry, {"flaky": ALLOW, "recover": ALLOW})
    first = executor.execute_result("flaky", {"value": "original"}, state)
    state.record_execution_result(first)
    first.arguments["value"] = "mutated outside state"

    retried = executor.execute_result(
        "recover",
        {
            "action": "retry",
            "caused_by_failure_id": "f-1",
            "reason": "temporary failure",
            "requested_attempt": "a-1",
        },
        state,
    )
    assert json.loads(retried.output)["status"] == "executed"
    assert seen == ["original", "original"]
    assert state.snapshot()["attempts"][-1]["caused_by_failure_id"] == "f-1"
    assert state.current_generation_id == 1

    state2 = AgentState()
    state2.begin_task("non retryable")
    _, executor2 = _nonzero_failure(state2)
    before_generation = state2.current_generation_id
    rejected = executor2.execute_result(
        "recover",
        {
            "action": "retry",
            "caused_by_failure_id": "f-1",
            "reason": "retry anyway",
            "requested_attempt": "a-1",
        },
        state2,
    )
    assert json.loads(rejected.output)["status"] == "rejected"
    assert state2.current_generation_id == before_generation


def test_terminal_batch_returns_results_without_entering_later_handlers():
    state = AgentState()
    state.begin_task("terminal batch")
    registry = ToolRegistry()
    called = []

    def fail():
        called.append("fail")
        raise RuntimeError("unknown side effect")

    def later():
        called.append("later")
        return "must not run"

    registry.register(Tool("fail", "fail", {"type": "object", "properties": {}}, fail, effect_class="possible"))
    registry.register(Tool("later", "later", {"type": "object", "properties": {}}, later, effect_class="possible"))
    executor = _executor(state, registry, {"fail": ALLOW, "later": ALLOW})
    context = ContextManager(state, [{"role": "user", "content": "terminal batch"}])
    responses = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "one", "type": "function", "function": {"name": "fail", "arguments": "{}"}},
            {"id": "two", "type": "function", "function": {"name": "later", "arguments": "{}"}},
        ]},
        {"role": "assistant", "content": "blocked explanation"},
    ]
    with patch("mini_agent.agent.call_llm", side_effect=responses):
        with redirect_stdout(StringIO()):
            result = agent_loop(context, executor)
    assert result.startswith("任务已阻塞：")
    assert "副作用范围未知" in result
    assert called == ["fail"]
    assert "task_terminal" in context.history[3]["content"]
    assert state.status == "blocked"


def test_structured_state_shows_running_recovery_facts_and_budget():
    state = AgentState()
    state.begin_task("show facts")
    _, executor = _nonzero_failure(state)
    executor.execute_result(
        "recover",
        {
            "action": "adjust",
            "caused_by_failure_id": "f-1",
            "reason": "invalid on purpose",
        },
        state,
    )
    rendered = ContextManager(
        state, [{"role": "user", "content": "show facts"}]
    )._render_state()["content"]
    assert state.status == "running"
    assert "Recovery notice:" in rendered
    assert "Recent failures:" in rendered
    assert "f-1" in rendered and "a-1" in rendered
    assert "Recent recovery actions:" in rendered
    assert "recovery_actions_remaining=7" in rendered


def test_repeated_invalid_recovery_blocks_at_budget_without_generation():
    from mini_agent.config import MAX_RECOVERY_ACTIONS
    state = AgentState(); state.begin_task("budget")
    _, executor = _nonzero_failure(state)
    generation = state.current_generation_id
    for _ in range(MAX_RECOVERY_ACTIONS):
        result = executor.execute_result("recover", {
            "action": "adjust", "caused_by_failure_id": "f-1", "reason": "bad target",
            "requested_tool": "functions.edit_file", "requested_arguments": {},
        }, state)
        assert json.loads(result.output)["status"] == "rejected"
    assert len(state.recovery_actions) == MAX_RECOVERY_ACTIONS
    assert state.status == "blocked"
    assert state.current_generation_id == generation
    with patch.object(executor.gate, "guard", side_effect=AssertionError("must not authorize")):
        result = executor.execute_result("run_shell", {"command": "false"}, state)
    assert result.error_kind == "task_terminal"
    assert not result.handler_admitted


def test_recovery_reserves_quota_before_permission_and_releases_denied_target():
    from mini_agent.state import canonical_arguments_hash
    state = AgentState(); state.begin_task("quota")
    registry, executor = _nonzero_failure(state)
    registry.register(Tool("target", "test", {"type": "object", "properties": {}},
                           lambda: "ok", effect_class="possible"))
    generation = state.current_generation_id
    fingerprint = ("target", canonical_arguments_hash({}))
    def guard(name, arguments):
        if name == "target":
            assert state.recovery_actions[-1].status == "proposed"
            assert state._fingerprint_counts[fingerprint] == 1
            assert state.current_generation_id == generation
            assert state.recovery_actions[-1].result_generation_id is None
            return "denied"
        return None
    with patch.object(executor.gate, "guard", side_effect=guard):
        result = executor.execute_result("recover", {
            "action": "adjust", "caused_by_failure_id": "f-1", "reason": "try target",
            "requested_tool": "target", "requested_arguments": {},
        }, state)
    assert json.loads(result.output)["status"] == "rejected"
    assert len(state.recovery_actions) == 1
    assert state._fingerprint_counts[fingerprint] == 0
    assert state.current_generation_id == generation


def test_recovery_quota_is_consumed_once_and_exhaustion_skips_authorization():
    state = AgentState(); state.begin_task("one execution")
    registry, executor = _nonzero_failure(state)
    calls = []
    registry.register(Tool("target", "test", {"type": "object", "properties": {}},
                           lambda: calls.append("executed") or "ok", effect_class="possible"))
    args = {"action": "adjust", "caused_by_failure_id": "f-1", "reason": "try",
            "requested_tool": "target", "requested_arguments": {}}
    generation = state.current_generation_id
    target_authorizations = []
    def guard(name, arguments):
        if name == "target":
            target_authorizations.append(name)
            assert state.current_generation_id == generation
            assert state.recovery_actions[-1].status == "proposed"
        return None
    with patch("mini_agent.state.MAX_ATTEMPT_FINGERPRINTS", 1):
        with patch.object(executor.gate, "guard", side_effect=guard):
            first = executor.execute_result("recover", args, state)
            second = executor.execute_result("recover", args, state)
    assert json.loads(first.output)["status"] == "executed"
    assert json.loads(second.output)["status"] == "rejected"
    assert target_authorizations == ["target"]
    assert calls == ["executed"]
    assert state.current_generation_id == generation + 1
    # The repeated request is rejected while the successor generation awaits
    # independent verification; a rejected request does not consume another
    # repair cycle or silently block the task.
    assert state.status == "running"
    assert state.snapshot()["repair_loop"]["cycles_used"] == 1
    assert state.snapshot()["verification_required"]


def test_schema_rejections_also_exhaust_budget():
    from mini_agent.config import MAX_RECOVERY_ACTIONS
    state = AgentState(); state.begin_task("schema budget")
    _, executor = _nonzero_failure(state)
    with patch.object(executor.gate, "guard", side_effect=AssertionError("schema precedes permission")):
        for _ in range(MAX_RECOVERY_ACTIONS):
            result = executor.execute_result("recover", {}, state)
            assert json.loads(result.output)["status"] == "rejected"
    assert state.status == "blocked"
    assert len(state.recovery_actions) == MAX_RECOVERY_ACTIONS
