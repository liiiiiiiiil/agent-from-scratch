from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys

import pytest

from mini_agent.evaluation import runner as runner_module
from mini_agent.evaluation.__main__ import _fixture_responses
from mini_agent.evaluation.report import build_report
from mini_agent.evaluation.runner import EvaluationRunner
from mini_agent.evaluation.schema import (
    CASE_SCHEMA_V1, MAX_FIXTURE_BYTES, case_from_dict, load_case,
    validate_trial_result,
)
from mini_agent.evaluation.worker import run_request


SOURCE_CASE = Path(__file__).parent / "fixtures" / "evaluation" / "smoke"


def copied_case(tmp_path: Path) -> Path:
    root = tmp_path / "case"
    root.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SOURCE_CASE, root)
    return root / "case.json"


def _tool_response(name: str, arguments: dict, call_id: str = "tool-1") -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }],
    }


def _finish_response() -> dict:
    return {"role": "assistant", "content": "任务已结束。", "tool_calls": []}


def _direct_request(case_path: Path, workspace: Path, responses: tuple[dict, ...], *, trial_id: str = "01234567-89ab-cdef-0123-456789abcdef"):
    case = load_case(case_path)
    return {
        "schema_version": 1,
        "trial_id": trial_id,
        "run_kind": "fixture",
        "case": case.to_dict(),
        "workspace": str(workspace),
        "responses": list(responses),
    }


def test_case_schema_rejects_bad_budget_paths_and_unknown_fields(tmp_path):
    case_path = copied_case(tmp_path)
    raw = json.loads(case_path.read_text())
    assert CASE_SCHEMA_V1["properties"]["schema_version"]["const"] == 1
    raw["max_rounds"] = 0
    with pytest.raises(ValueError, match="max_rounds"):
        case_from_dict(raw)
    raw = json.loads(case_path.read_text())
    raw["fixture_dir"] = "../outside"
    with pytest.raises(ValueError, match="不得包含"):
        case_from_dict(raw)
    raw = json.loads(case_path.read_text())
    raw["grader_script"] = "grader.py"
    raw["surprise"] = True
    with pytest.raises(ValueError, match="未知字段"):
        case_from_dict(raw)


def test_case_validation_rejects_oversize_fixture_missing_grader_and_symlink(tmp_path):
    case_path = copied_case(tmp_path)
    large = case_path.parent / "initial" / "too-large.bin"
    large.write_bytes(b"x" * (MAX_FIXTURE_BYTES + 1))
    with pytest.raises(ValueError, match="单文件"):
        load_case(case_path)
    large.unlink()
    (case_path.parent / "grader.py").unlink()
    with pytest.raises(ValueError, match="不存在"):
        load_case(case_path)

    case_path = copied_case(tmp_path / "symlink")
    outside = tmp_path / "outside.txt"
    outside.write_text("not in fixture")
    try:
        (case_path.parent / "initial" / "escape.txt").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("filesystem does not permit symlinks")
    with pytest.raises(ValueError, match="符号链接"):
        load_case(case_path)


def test_two_fixture_trials_start_clean_and_report_separately(tmp_path, monkeypatch):
    case_path = copied_case(tmp_path)
    case = load_case(case_path)
    output = tmp_path / "results"
    runner = EvaluationRunner()
    original = runner_module._run_child
    child_order = []

    def record_child(argv, **kwargs):
        child_order.append("agent" if "mini_agent.evaluation.worker" in argv else "grader")
        return original(argv, **kwargs)

    monkeypatch.setattr(runner_module, "_run_child", record_child)
    first = runner.run_case(case, output, run_kind="fixture", responses=_fixture_responses())
    second = runner.run_case(case, output, run_kind="fixture", responses=_fixture_responses())
    a = json.loads((first / "trial.json").read_text())
    b = json.loads((second / "trial.json").read_text())
    assert first != second
    assert a["trial_id"] != b["trial_id"]
    assert a["run_kind"] == b["run_kind"] == "fixture"
    assert child_order == ["agent", "grader", "agent", "grader"]
    assert a["success"] is b["success"] is True
    assert a["grader_passed"] is b["grader_passed"] is True
    assert a["changed_files"] == b["changed_files"] == ["src/scale.py"]
    assert (case_path.parent / "initial" / "src" / "scale.py").read_text().endswith("return value + 2\n")
    assert a["cost_usd"] is None and a["price_snapshot"] is None
    assert a["recovery_success_rate"] is None and a["invalid_repeat_count"] is None
    report = build_report(output)
    assert report["fixture"]["trials"] == 2
    assert report["fixture"]["scored_trials"] == 2
    assert report["fixture"]["independent_acceptance_passed"] == 2
    assert report["live"]["trials"] == 0
    assert not list(output.glob(".evaluation-trial-*"))


def test_noninteractive_permission_gate_rejects_unapproved_mutation(tmp_path):
    case_path = copied_case(tmp_path)
    case = load_case(case_path)
    raw_case = case.to_dict()
    raw_case["authorized_tools"] = ["read_file"]
    workspace = tmp_path / "workspace"
    shutil.copytree(case_path.parent / "initial", workspace)
    request = {
        "schema_version": 1,
        "trial_id": "01234567-89ab-cdef-0123-456789abcdef",
        "run_kind": "fixture",
        "case": raw_case,
        "workspace": str(workspace),
        "responses": [
            _tool_response("write_file", {"path": "src/scale.py", "content": "def scale(value): return value * 2\n"}),
            _finish_response(),
        ],
    }
    result = run_request(request)
    assert result["permission_denials"] == 1
    assert result["agent_started"] is True
    assert "return value + 2" in (workspace / "src" / "scale.py").read_text()


def test_worker_rejects_escape_and_unlisted_shell_without_side_effects(tmp_path):
    case_path = copied_case(tmp_path)
    case = load_case(case_path)
    workspace = tmp_path / "workspace"
    shutil.copytree(case_path.parent / "initial", workspace)
    outside = tmp_path / "outside.txt"
    outside.write_text("safe")
    result = run_request(_direct_request(
        case_path, workspace,
        (_tool_response("write_file", {"path": "../../outside.txt", "content": "changed"}), _finish_response()),
    ))
    assert outside.read_text() == "safe"
    assert result["permission_denials"] == 0

    sentinel = tmp_path / "shell-ran"
    command = f"touch {sentinel}"
    result = run_request(_direct_request(
        case_path, workspace,
        (_tool_response("run_shell", {"command": command}), _finish_response()),
        trial_id="11234567-89ab-cdef-0123-456789abcdef",
    ))
    assert not sentinel.exists()
    assert result["agent_state_status"] in {"blocked", "failed", "done", "running"}


def test_worker_exception_is_a_structured_agent_failure(tmp_path):
    case_path = copied_case(tmp_path)
    case = load_case(case_path)
    workspace = tmp_path / "workspace"
    shutil.copytree(case_path.parent / "initial", workspace)
    result = run_request(_direct_request(case_path, workspace, ()))
    assert result["agent_started"] is True
    assert result["agent_stop_reason"] == "agent_error"
    assert result["agent_error_kind"] == "RuntimeError"
    assert result["cleanup_complete"] is True


def test_grader_exception_is_infrastructure_error_and_not_success(tmp_path):
    case_path = copied_case(tmp_path)
    (case_path.parent / "grader.py").write_text("raise SystemExit(7)\n")
    case = load_case(case_path)
    trial = EvaluationRunner().run_case(case, tmp_path / "results", run_kind="fixture", responses=(_finish_response(),))
    result = json.loads((trial / "trial.json").read_text())
    assert result["grader_error_kind"] == "nonzero_exit"
    assert result["failure_kind"] == "grader_infrastructure_error"
    assert result["success"] is False
    report = build_report(tmp_path / "results")
    assert report["fixture"]["scored_trials"] == 0
    assert report["fixture"]["grader_infrastructure_errors"] == 1


def test_agent_timeout_still_runs_independent_grader_and_is_scored(tmp_path, monkeypatch):
    case_path = copied_case(tmp_path)
    case = load_case(case_path)
    original = runner_module._run_child

    def timeout_worker(argv, **kwargs):
        if "mini_agent.evaluation.worker" in argv:
            kwargs["stdout_path"].write_text("")
            kwargs["stderr_path"].write_text("")
            Path(argv[-1]).write_text("started\n")
            return {
                "started": True, "returncode": -9, "timed_out": True,
                "duration_ms": 1000, "cleanup_complete": True,
                "cleanup_issue": None, "pid": 4242, "launch_error": None,
            }
        return original(argv, **kwargs)

    monkeypatch.setattr(runner_module, "_run_child", timeout_worker)
    trial = EvaluationRunner().run_case(case, tmp_path / "results", run_kind="fixture", responses=(_finish_response(),))
    result = json.loads((trial / "trial.json").read_text())
    assert result["failure_kind"] == "agent_timeout"
    assert result["agent_started"] is True
    assert result["grader_passed"] is False
    assert result["agent_llm_calls"] is None
    assert build_report(tmp_path / "results")["fixture"]["scored_trials"] == 1


def test_result_is_atomic_schema_bounded_and_fixture_never_counts_as_live(tmp_path):
    case = load_case(copied_case(tmp_path))
    trial = EvaluationRunner().run_case(case, tmp_path / "results", run_kind="fixture", responses=_fixture_responses())
    result_path = trial / "trial.json"
    raw = json.loads(result_path.read_text())
    validate_trial_result(raw)
    assert result_path.stat().st_size <= 256 * 1024
    assert sorted(item.name for item in trial.iterdir()) == ["agent.log", "diff.patch", "grader.log", "trial.json"]
    assert "sk-" not in result_path.read_text()
    assert "Authorization" not in result_path.read_text()


def test_result_redaction_never_persists_config_secret(tmp_path):
    secret = "sk-test-secret-value-123456789"
    redacted = runner_module._redact(f"provider error Authorization: Bearer {secret}; api_key={secret}", (secret,))
    assert secret not in redacted
    assert "<redacted>" in redacted


def test_live_trial_redacts_credentials_from_diff_and_logs(tmp_path, monkeypatch):
    case = load_case(copied_case(tmp_path))
    secret = "sk-test-secret-value-123456789"
    monkeypatch.setattr(runner_module, "_load_live_binding", lambda _case: (
        {"profile": "local", "provider": "p", "protocol": "openai_chat", "fingerprint": "0" * 64},
        (secret,),
    ))

    def fake_child(argv, **kwargs):
        is_worker = "mini_agent.evaluation.worker" in argv
        if is_worker:
            request = json.loads(Path(argv[-2]).read_text())
            workspace = Path(kwargs["cwd"])
            (workspace / "src" / "scale.py").write_text(
                "# api_key=" + secret + "\ndef scale(value):\n    return value * 2\n", encoding="utf-8",
            )
            payload = {
                "schema_version": 1, "trial_id": request["trial_id"],
                "agent_started": True, "agent_stop_reason": "text", "agent_state_status": "done",
                "agent_error_kind": None, "agent_duration_ms": 10, "agent_llm_calls": 1,
                "successful_model_responses": 1, "permission_denials": 0, "tool_calls": 1,
                "input_tokens": 100, "output_tokens": 20, "token_accounting": "provider",
                "usage_source": "provider", "model_binding_ref": {"profile": "local", "provider": "p", "protocol": "openai_chat", "fingerprint": "0" * 64},
                "cleanup_complete": True, "cleanup_issue": None,
            }
            kwargs["stdout_path"].write_text(json.dumps(payload), encoding="utf-8")
            kwargs["stderr_path"].write_text("Authorization: Bearer " + secret, encoding="utf-8")
            return {"started": True, "returncode": 0, "timed_out": False, "duration_ms": 10,
                    "cleanup_complete": True, "cleanup_issue": None, "pid": 1234, "launch_error": None}
        kwargs["stdout_path"].write_text(json.dumps({"passed": True, "detail": secret}), encoding="utf-8")
        kwargs["stderr_path"].write_text("", encoding="utf-8")
        return {"started": True, "returncode": 0, "timed_out": False, "duration_ms": 5,
                "cleanup_complete": True, "cleanup_issue": None, "pid": 1235, "launch_error": None}

    monkeypatch.setattr(runner_module, "_run_child", fake_child)
    trial = EvaluationRunner().run_case(case, tmp_path / "live-results", run_kind="live", live_confirmed=True)
    assert json.loads((trial / "trial.json").read_text())["success"] is True
    for artifact in trial.iterdir():
        assert secret not in artifact.read_text(encoding="utf-8")


def test_live_requires_explicit_flag_before_worker_start(tmp_path):
    case = load_case(copied_case(tmp_path))
    with pytest.raises(ValueError, match="--live"):
        EvaluationRunner().run_case(case, tmp_path / "results", run_kind="live", live_confirmed=False)
