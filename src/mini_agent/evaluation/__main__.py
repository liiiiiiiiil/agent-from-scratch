"""CLI for validating cases, running trials, and rebuilding reports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile

from mini_agent.evaluation.report import build_report
from mini_agent.evaluation.report import build_suite_report
from mini_agent.evaluation.runner import (
    EvaluationRunner, _copy_fixture, run_grader_for_workspace,
)
from mini_agent.evaluation.schema import load_case, validate_fixture_directory
from mini_agent.evaluation.benchmark import load_suite, run_suite, suite_plan, validate_suite_baselines


def _default_case() -> Path:
    return Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "evaluation" / "smoke" / "case.json"


def _fixture_responses() -> tuple[dict, ...]:
    import json as json_module

    return (
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "fixture-edit-1",
                "type": "function",
                "function": {
                    "name": "edit_file",
                    "arguments": json_module.dumps({
                        "path": "src/scale.py",
                        "old_string": "return value + 2",
                        "new_string": "return value * 2",
                    }),
                },
            }],
        },
        {"role": "assistant", "content": "已按要求修复函数。", "tool_calls": []},
    )


def _verify_smoke_grader(case) -> None:
    baseline = Path(tempfile.mkdtemp(prefix="mini-agent-eval-baseline-"))
    repaired = Path(tempfile.mkdtemp(prefix="mini-agent-eval-repaired-"))
    try:
        _copy_fixture(Path(case.case_dir) / case.fixture_dir, baseline)
        _copy_fixture(Path(case.case_dir) / case.fixture_dir, repaired)
        baseline_passed, baseline_error = run_grader_for_workspace(case, baseline, timeout=case.grader_timeout_seconds)
        if baseline_error is not None or baseline_passed is not False:
            raise RuntimeError("smoke grader must reject the original fixture")
        target = repaired / "src" / "scale.py"
        target.write_text("def scale(value):\n    return value * 2\n", encoding="utf-8")
        repaired_passed, repaired_error = run_grader_for_workspace(case, repaired, timeout=case.grader_timeout_seconds)
        if repaired_error is not None or repaired_passed is not True:
            raise RuntimeError("smoke grader must accept the known correct repair")
    finally:
        shutil.rmtree(baseline, ignore_errors=True)
        shutil.rmtree(repaired, ignore_errors=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m mini_agent.evaluation")
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="校验题目、fixture 与评分脚本")
    validate.add_argument("case_json")

    run = commands.add_parser("run", help="运行一次 Agent trial")
    run.add_argument("case_json")
    run.add_argument("--live", action="store_true", help="明确启用真实模型调用")
    run.add_argument("--output", required=True, help="独立 trial 结果目录")

    self_test = commands.add_parser("self-test", help="运行两次固定响应的离线试跑")
    self_test.add_argument("--output", required=True, help="独立 trial 结果目录")

    report = commands.add_parser("report", help="从原始 trial JSON 重建汇总")
    report.add_argument("output_dir")

    validate_suite = commands.add_parser("validate-suite", help="校验固定题集摘要和原始/正确版本评分")
    validate_suite.add_argument("suite_json")

    run_suite_command = commands.add_parser("run-suite", help="按固定顺序运行编码题集")
    run_suite_command.add_argument("suite_json")
    run_suite_command.add_argument("--live", action="store_true", help="明确启用真实模型调用")
    run_suite_command.add_argument("--repeats", type=int, default=None, help="每题重复次数，默认使用 suite 配置")
    run_suite_command.add_argument("--output", required=True, help="新的独立 suite run 目录")

    report_suite = commands.add_parser("report-suite", help="从 suite-run 和原始 trial 重建报告")
    report_suite.add_argument("suite_run_dir")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    runner = EvaluationRunner()
    try:
        if args.command == "validate":
            case = runner.validate(args.case_json)
            fixture_count, fixture_bytes = validate_fixture_directory(Path(case.case_dir) / case.fixture_dir)
            grader = Path(case.case_dir) / case.grader_script
            print(json.dumps({
                "valid": True,
                "case_id": case.case_id,
                "version": case.version,
                "max_rounds": case.max_rounds,
                "agent_timeout_seconds": case.agent_timeout_seconds,
                "grader_timeout_seconds": case.grader_timeout_seconds,
                "allowed_tools": list(case.allowed_tools),
                "authorized_tools": list(case.authorized_tools),
                "fixture_files": fixture_count,
                "fixture_bytes": fixture_bytes,
                "grader_bytes": grader.stat().st_size,
            }, ensure_ascii=False, indent=2))
            return 0
        if args.command == "run":
            if not args.live:
                raise ValueError("真实试跑必须显式传 --live")
            case = runner.validate(args.case_json)
            runner.validate_live_configuration(case)
            print(
                f"LIVE trial: case={case.case_id}@{case.version}; rounds<={case.max_rounds}; "
                f"agent_timeout={case.agent_timeout_seconds}s; grader_timeout={case.grader_timeout_seconds}s"
            )
            print("Task: " + case.task)
            print("Authorized tools: " + ", ".join(case.authorized_tools))
            trial_dir = runner.run_case(case, args.output, run_kind="live", live_confirmed=True)
            print(f"Trial saved: {trial_dir}")
            print((trial_dir / "trial.json").read_text(encoding="utf-8"))
            return 0
        if args.command == "self-test":
            case = runner.validate(_default_case())
            _verify_smoke_grader(case)
            results = []
            for _ in range(2):
                results.append(runner.run_case(
                    case, args.output, run_kind="fixture", responses=_fixture_responses(),
                ))
            raw = [json.loads((path / "trial.json").read_text(encoding="utf-8")) for path in results]
            if not all(item["success"] and item["run_kind"] == "fixture" for item in raw):
                print(json.dumps({"self_test": "failed", "trials": [str(path) for path in results]}, indent=2))
                return 1
            print(json.dumps({
                "self_test": "passed",
                "trials": [str(path) for path in results],
                "trial_ids": [item["trial_id"] for item in raw],
                "report": build_report(args.output),
            }, ensure_ascii=False, indent=2))
            return 0
        if args.command == "report":
            print(json.dumps(build_report(args.output_dir), ensure_ascii=False, indent=2))
            return 0
        if args.command == "validate-suite":
            suite = load_suite(args.suite_json)
            baselines = validate_suite_baselines(suite)
            print(json.dumps({
                "valid": True, "suite_id": suite.suite_id, "version": suite.version,
                "suite_sha256": suite.suite_sha256, "planned_trials_default": len(suite_plan(suite)),
                "cases": [
                    {
                        "case_id": item.case.case_id, "case_path": str(item.case_path),
                        "task": item.case.task, "max_rounds": item.case.max_rounds,
                        "agent_timeout_seconds": item.case.agent_timeout_seconds,
                        "grader_timeout_seconds": item.case.grader_timeout_seconds,
                        "authorized_tools": list(item.case.authorized_tools),
                        "initial_sha256": item.initial_sha256,
                        "grader_sha256": item.grader_sha256,
                        "known_good_sha256": item.known_good_sha256,
                        "regression_test_required": item.regression_test_required,
                    }
                    for item in suite.cases
                ],
                "offline_baselines": baselines,
            }, ensure_ascii=False, indent=2))
            return 0
        if args.command == "run-suite":
            if not args.live:
                raise ValueError("真实模型套件运行必须显式传 --live")
            suite = load_suite(args.suite_json)
            plan = suite_plan(suite, args.repeats)
            # All topic, oracle, and provider checks happen before the first worker.
            validate_suite_baselines(suite)
            bindings = [runner.validate_live_configuration(item.case)[0] for item in suite.cases]
            if any(value != bindings[0] for value in bindings[1:]):
                raise ValueError("coding suite 的所有题目必须使用同一个冻结 model binding")
            print(
                f"LIVE suite: {suite.suite_id}@{suite.version}; suite_sha256={suite.suite_sha256}; "
                f"planned_trials={len(plan)}; repeats_per_case={len(plan) // len(suite.cases)}"
            )
            for item in suite.cases:
                case = item.case
                count = sum(slot["case_id"] == case.case_id for slot in plan)
                print(
                    f"- {case.case_id}: {count} trial(s); rounds<={case.max_rounds}; "
                    f"agent_timeout={case.agent_timeout_seconds}s; grader_timeout={case.grader_timeout_seconds}s; "
                    f"authorized_tools={','.join(case.authorized_tools)}"
                )
                print("  Task: " + case.task)
            ledger = run_suite(
                suite, args.output, repeats=args.repeats, run_kind="live", live_confirmed=True,
                runner=runner,
            )
            print(f"Suite run saved: {ledger.parent}")
            print(json.dumps(build_suite_report(ledger.parent), ensure_ascii=False, indent=2))
            return 0
        if args.command == "report-suite":
            print(json.dumps(build_suite_report(args.suite_run_dir), ensure_ascii=False, indent=2))
            return 0
    except Exception as error:
        print(f"evaluation error ({type(error).__name__}): {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
