"""v0.25 ordered plan trace and evaluation coverage."""

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from mini_agent.permission import ALLOW, PermissionGate, PermissionPolicy
from mini_agent.config import MAX_STAGNANT_ROUNDS
from mini_agent.state import AgentState
from mini_agent.tools import create_registry
from mini_agent.tools.base import ToolExecutor
from mini_agent.trace import TraceQueryError, build_trace, render_trace


def _step(step_id, *, depends_on=None, replaces=None, content=None):
    return {
        "step_id": step_id,
        "content": content or step_id,
        "depends_on": depends_on or [],
        "success_criteria": [f"{step_id} done"],
        "replaces": replaces or [],
    }


def _plan(**overrides):
    value = {
        "goal": "finish trace task",
        "constraints": [],
        "success_criteria": ["verification passes"],
        "steps": [_step("inspect"), _step("edit", depends_on=["inspect"])],
        "reason": "initial plan",
    }
    value.update(overrides)
    return value


def _execute(executor, state, name, arguments):
    result = executor.execute_result(name, arguments, state)
    if name != "recover":
        state.record_execution_result(result)
    return result


def _real_replan_snapshot():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        state = AgentState()
        state.begin_task("old plan fails, revised plan passes")
        state.commit_plan(**_plan())
        state.update_plan_progress(1, "inspect", "in_progress", "inspect first")
        state.update_plan_progress(1, "inspect", "completed", "inspection complete")

        registry = create_registry(state, str(root))
        shell = registry.get("run_shell")
        original = shell.handler
        shell.handler = lambda command, purpose="execution": "[exit=1] old check failed"
        executor = ToolExecutor(
            registry,
            PermissionGate(PermissionPolicy({
                "run_shell": ALLOW, "write_file": ALLOW,
                "request_replan": ALLOW, "recover": ALLOW,
            })),
        )
        try:
            failed = _execute(executor, state, "run_shell", {
                "command": "old-check", "purpose": "verification",
            })
            assert failed.outcome == "failed"
            trigger = state.request_replan("failure", "f-1", "old check disproved the edit path")
            state.commit_plan(
                **_plan(
                    goal="finish trace task with replacement",
                    reason="replace the failed edit path",
                    steps=[
                        _step("inspect"),
                        _step("fix", depends_on=["inspect"], replaces=["edit"]),
                    ],
                    parent_revision_id=1,
                    trigger_id=trigger.trigger_id,
                )
            )
            _execute(executor, state, "write_file", {
                "path": str(root / "result.txt"), "content": "fixed",
            })
            state.update_plan_progress(2, "fix", "in_progress", "apply replacement")
            state.update_plan_progress(2, "fix", "completed", "replacement complete")
            shell.handler = lambda command, purpose="execution": "[exit=0] new check passed"
            _execute(executor, state, "run_shell", {
                "command": "new-check", "purpose": "verification",
            })
            state.status = "done"
            return deepcopy(state.snapshot())
        finally:
            shell.handler = original


def _later_revision_snapshot():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        state = AgentState()
        state.begin_task("revision changes after a new generation starts")
        state.commit_plan(**_plan())
        state.update_plan_progress(1, "inspect", "in_progress", "start inspection")
        registry = create_registry(state, str(root))
        executor = ToolExecutor(
            registry,
            PermissionGate(PermissionPolicy({"write_file": ALLOW, "read_file": ALLOW})),
        )
        _execute(executor, state, "write_file", {
            "path": str(root / "input.txt"), "content": "new fact",
        })
        observation = _execute(executor, state, "read_file", {
            "path": str(root / "input.txt"),
        })
        assert observation.outcome == "succeeded"
        trigger = state.request_replan("observation", "a-2", "new fact changes the plan")
        state.commit_plan(**_plan(
            goal="revised", reason="use the new fact",
            steps=[_step("inspect"), _step("fix", depends_on=["inspect"], replaces=["edit"])],
            parent_revision_id=1, trigger_id=trigger.trigger_id,
        ))
        return state.snapshot()


def test_real_failure_replan_new_generation_trace_is_ordered_and_complete():
    snapshot = _real_replan_snapshot()
    events = snapshot["trace_events"]
    assert [event["sequence_id"] for event in events] == list(range(1, len(events) + 1))
    assert events[0]["kind"] == "task_started"
    assert any(event["kind"] == "failure_recorded" and event["revision_id"] == 1 for event in events)
    assert any(event["kind"] == "plan_committed" and event["revision_id"] == 2 for event in events)

    report = build_trace(snapshot)
    assert report["integrity"] == {"status": "complete", "issues": []}
    assert report["conclusion"]["status"] == "done"
    assert report["conclusion"]["evidence"]["active_revision_id"] == 2
    assert report["conclusion"]["evidence"]["verification_generation"] == 1

    first, second = report["plan_revisions"]
    assert first["failures"][0]["failure_id"] == "f-1"
    assert first["verification_evidence"][0]["outcome"] == "failed"
    assert second["trigger_source"]["failure_id"] == "f-1"
    assert second["diff"]["replaced"] == ["edit"]
    assert [item["attempt_id"] for item in second["attempts"]] == ["a-2", "a-3"]
    assert second["verification_evidence"][0]["generation_id"] == 1
    assert any(edge["type"] == "revision_parent" and edge["status"] == "resolved"
               for edge in report["causal_edges"])
    assert any(edge["type"] == "trigger_revision" and edge["status"] == "resolved"
               for edge in report["causal_edges"])
    assert "Plan chain:" in render_trace(report)
    assert "结论依据:" in render_trace(report)


def test_revision_query_keeps_trigger_predecessor_and_generation_query_is_compatible():
    snapshot = _real_replan_snapshot()
    revision_report = build_trace(snapshot, revision_id=2)
    assert revision_report["query"]["scope"] == "revision"
    assert [item["revision_id"] for item in revision_report["plan_revisions"]] == [2]
    assert revision_report["query"]["generation_ids"] == [0, 1]
    assert any(event.get("record_type") == "failure" for event in revision_report["plan_timeline"])
    assert any(edge["type"] == "trigger_source" and edge["status"] == "resolved"
               for edge in revision_report["causal_edges"])

    generation_report = build_trace(snapshot, 0)
    assert generation_report["query"]["scope"] == "generation"
    assert [item["generation_id"] for item in generation_report["generations"]] == [0]
    assert all(event["generation_id"] == 0 for event in generation_report["trace_events"])
    with pytest.raises(TraceQueryError):
        build_trace(snapshot, 0, revision_id=2)


def test_generation_query_includes_revisions_active_after_their_commit_generation():
    snapshot = _real_replan_snapshot()
    report = build_trace(snapshot, 1)
    assert report["integrity"]["status"] == "complete"
    assert [item["revision_id"] for item in report["plan_revisions"]] == [2]
    assert report["plan_revisions"][0]["generation_id"] == 0
    assert report["plan_revisions"][0]["generation_role"] == "active"
    assert report["generations"][0]["plan_revisions"][0]["generation_role"] == "active"


def test_historical_generation_basis_does_not_import_later_revision_or_progress():
    snapshot = _later_revision_snapshot()
    report = build_trace(snapshot, 0)
    assert report["integrity"]["status"] == "complete"
    assert [item["revision_id"] for item in report["plan_revisions"]] == [1]
    basis = report["generations"][0]["conclusion"]["evidence"]
    assert basis["active_revision_id"] == 1
    assert basis["active_revision_statuses"]["inspect"] == "in_progress"
    assert basis["current_generation_id"] == 0
    assert basis["terminal_summary"]["status"] == "not_recorded"
    assert report["conclusion"]["status"] == "continue"
    assert report["conclusion"]["evidence"]["active_revision_id"] == 1
    assert build_trace(snapshot)["conclusion"]["evidence"]["active_revision_id"] == 2


@pytest.mark.parametrize("record_type,record_id", [
    ("attempt", "a-2"),
    ("verification_history", 1),
])
def test_missing_fact_event_is_unresolved_even_with_contiguous_sequence(record_type, record_id):
    snapshot = _real_replan_snapshot()
    snapshot["trace_events"] = [
        event for event in snapshot["trace_events"]
        if (event["record_type"], event["record_id"]) != (record_type, record_id)
    ]
    for sequence_id, event in enumerate(snapshot["trace_events"], 1):
        event["sequence_id"] = sequence_id
    report = build_trace(snapshot)
    assert report["integrity"]["status"] == "incomplete"
    assert any(f"{record_type} {record_id} 缺少对应 trace event" in issue
               for issue in report["integrity"]["issues"])
    assert any(edge["type"] == "trace_event_missing" and edge["status"] == "unresolved"
               for edge in report["causal_edges"])


def test_plan_only_decisions_review_and_same_generation_revisions_have_events():
    state = AgentState()
    state.begin_task("plan-only", mode="plan_only")
    state.commit_plan(**_plan())
    state.decide_plan("continue_exploring", 1, "inspect the dependency first")
    state.review_current_plan(1)
    state.decide_plan("approved", 1)

    events = state.snapshot()["trace_events"]
    assert [event["kind"] for event in events].count("plan_decision") == 2
    assert any(event["kind"] == "plan_review" for event in events)
    assert any(event["kind"] == "trigger_resolved" for event in events)
    assert build_trace(state.snapshot())["integrity"]["status"] == "complete"

    # A successful read-only observation can create a second revision without
    # opening a generation; the event pointer disambiguates it.  Go through
    # the normal executor so the observation has its own ordered event too.
    with TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "input.txt").write_text("observed\n", encoding="utf-8")
        state = AgentState()
        state.begin_task("same generation revisions")
        state.commit_plan(**_plan())
        registry = create_registry(state, str(root))
        executor = ToolExecutor(
            registry,
            PermissionGate(PermissionPolicy({"read_file": ALLOW})),
        )
        observation = _execute(executor, state, "read_file", {"path": str(root / "input.txt")})
        assert observation.outcome == "succeeded"
        trigger = state.request_replan("observation", "a-1", "new read-only fact")
        state.commit_plan(
            **_plan(
                goal="revised", reason="use the observation",
                steps=[_step("inspect"), _step("replace", depends_on=["inspect"], replaces=["edit"])],
                parent_revision_id=1, trigger_id=trigger.trigger_id,
            )
        )
        snapshot = state.snapshot()
        assert snapshot["plan_revisions"][0]["generation_id"] == snapshot["plan_revisions"][1]["generation_id"] == 0
        assert build_trace(snapshot)["integrity"]["status"] == "complete"
    assert [event["revision_id"] for event in snapshot["trace_events"]
            if event["kind"] == "plan_committed"] == [1, 2]

    rejected = AgentState()
    rejected.begin_task("plan-only rejection", mode="plan_only")
    rejected.commit_plan(**_plan())
    decision = rejected.decide_plan("rejected", 1, "change the edit order")
    trigger = rejected.snapshot()["replan_triggers"][-1]
    assert decision.decision == "rejected"
    rejected.commit_plan(
        **_plan(
            goal="revised after feedback", reason="apply the user's feedback",
            steps=[_step("edit"), _step("inspect", depends_on=["edit"])],
            parent_revision_id=1, trigger_id=trigger["trigger_id"],
        )
    )
    rejected.decide_plan("approved", 2)
    rejected_snapshot = rejected.snapshot()
    assert build_trace(rejected_snapshot)["integrity"]["status"] == "complete"
    assert any(
        event["kind"] == "plan_decision"
        and event["record_id"] == decision.decision_id
        for event in rejected_snapshot["trace_events"]
    )


def test_stagnation_warning_survives_later_progress_and_direct_path_stays_compatible():
    state = AgentState()
    state.begin_task("direct")
    state.observe_tool_round("same", tool_names=("read_file",))
    warning = state.observe_tool_round("same", tool_names=("read_file",))
    assert warning["warning"]
    state.observe_tool_round("new", observation_hashes=("new-fact",), tool_names=("read_file",))
    snapshot = state.snapshot()
    assert snapshot["status"] == "running"
    assert snapshot["trace_events"][-1]["kind"] == "stagnation_warning"
    report = build_trace(snapshot)
    assert report["integrity"]["status"] == "complete"
    assert report["conclusion"]["evidence"]["stagnation"]["stagnation_count"] == 2
    assert report["plan_revisions"] == []

    state = AgentState()
    state.begin_task("stagnation recovery")
    for _ in range(MAX_STAGNANT_ROUNDS):
        result = state.observe_tool_round("same", tool_names=("read_file",))
    assert result["blocked"]
    decision = state.resume_blocked("investigate a new path")
    trigger = state.snapshot()["replan_triggers"][-1]
    state.commit_plan(**_plan(
        reason="resume with a concrete plan",
        trigger_id=trigger["trigger_id"],
    ))
    recovered = state.snapshot()
    assert recovered["status"] == "running"
    assert decision.decision == "resume_blocked"
    assert any(event["kind"] == "stagnation_blocked" for event in recovered["trace_events"])
    assert build_trace(recovered)["integrity"]["status"] == "complete"


@pytest.mark.parametrize("damage", [
    "missing_parent", "parent_cycle", "missing_dependency", "dependency_cycle",
    "wrong_diff", "cross_revision_progress", "duplicate_trigger_consumption",
    "missing_approval_source", "cross_generation_verification", "sequence_gap",
    "wrong_event_reference",
])
def test_corrupt_plan_snapshot_is_incomplete_and_read_only(damage):
    snapshot = _real_replan_snapshot()
    if damage == "missing_parent":
        snapshot["plan_revisions"][1]["parent_revision_id"] = 99
    elif damage == "parent_cycle":
        snapshot["plan_revisions"][1]["parent_revision_id"] = 2
    elif damage == "missing_dependency":
        snapshot["plan_revisions"][1]["steps"][1]["depends_on"] = ["missing"]
    elif damage == "dependency_cycle":
        snapshot["plan_revisions"][1]["steps"][0]["depends_on"] = ["fix"]
    elif damage == "wrong_diff":
        snapshot["plan_revisions"][1]["diff"]["added"] = ["wrong"]
    elif damage == "cross_revision_progress":
        snapshot["plan_progress_history"][0]["revision_id"] = 2
    elif damage == "duplicate_trigger_consumption":
        snapshot["replan_triggers"][0]["result_revision_id"] = 1
    elif damage == "missing_approval_source":
        snapshot["replan_triggers"][0]["caused_by_failure_id"] = "missing"
    elif damage == "cross_generation_verification":
        snapshot["verification_history"][1]["generation_id"] = 0
    elif damage == "sequence_gap":
        snapshot["trace_events"][2]["sequence_id"] = 99
    elif damage == "wrong_event_reference":
        event = next(item for item in snapshot["trace_events"] if item["record_type"] == "plan_revision")
        event["record_id"] = 99

    before_trace = deepcopy(snapshot)
    report = build_trace(snapshot)
    render_trace(report)
    assert report["integrity"]["status"] == "incomplete"
    assert report["integrity"]["issues"]
    assert any(edge["status"] == "unresolved" for edge in report["causal_edges"])
    assert snapshot == before_trace
