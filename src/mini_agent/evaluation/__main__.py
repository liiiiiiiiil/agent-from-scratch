"""CLI for validating cases, running trials, and rebuilding reports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile

from mini_agent.evaluation.report import build_report
from mini_agent.evaluation.runner import (
    EvaluationRunner, _copy_fixture, run_grader_for_workspace,
)
from mini_agent.evaluation.schema import load_case, validate_fixture_directory


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
    except Exception as error:
        print(f"evaluation error ({type(error).__name__}): {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
