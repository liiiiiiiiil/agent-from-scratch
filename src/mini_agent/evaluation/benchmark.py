"""Frozen coding benchmark manifests, offline oracle checks, and suite runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import uuid
from typing import Any

from mini_agent.evaluation.runner import EvaluationRunner, _code_revision, _copy_fixture, run_grader_for_workspace
from mini_agent.evaluation.schema import (
    MAX_SUITE_BYTES, MAX_SUITE_REPEATS, SUITE_SCHEMA_V1, Case, _no_symlink_path,
    _safe_relative, load_case, validate_fixture_directory, validate_suite,
    validate_suite_run,
)


# Updated only when the suite version changes after a reviewed fixture edit.
PINNED_SUITE_FINGERPRINTS: dict[tuple[str, str], str] = {
    ("coding-benchmark", "1.0"): "bf728faf8cff54e173a73c21946499b48b81fccce73a2b187f3d7b20b7e12d16",
}


@dataclass(frozen=True)
class SuiteCase:
    case: Case
    case_path: Path
    case_sha256: str
    task_sha256: str
    initial_sha256: str
    grader_sha256: str
    known_good_sha256: str
    regression_test_required: bool


@dataclass(frozen=True)
class Suite:
    suite_id: str
    version: str
    description: str
    default_repeats: int
    suite_sha256: str
    suite_path: Path
    suite_bytes: bytes = field(repr=False, compare=False)
    cases: tuple[SuiteCase, ...]


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def tree_sha256(root: Path) -> str:
    """Hash a fixture tree by sorted relative names and exact file bytes."""
    root = root.absolute()
    count, total = validate_fixture_directory(root)
    _ = count, total
    entries: list[dict[str, Any]] = []
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name != "__pycache__")
        for name in sorted(files):
            if name.endswith((".pyc", ".pyo")):
                continue
            path = Path(current) / name
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode):
                raise ValueError(f"fixture 含符号链接或特殊文件: {path.name}")
            relative = path.relative_to(root).as_posix()
            raw = path.read_bytes()
            entries.append({"path": relative, "size": len(raw), "sha256": _sha256(raw)})
    return _sha256(_canonical_json(entries))


def runtime_fingerprint() -> str:
    """Fingerprint Python sources that can affect the isolated Agent runtime."""
    source_root = Path(__file__).resolve().parents[2]
    entries: list[dict[str, str]] = []
    for path in sorted(source_root.rglob("*.py")):
        relative = path.relative_to(source_root)
        if "__pycache__" in relative.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode):
            raise ValueError("Agent runtime source tree contains a link or special file")
        raw = path.read_bytes()
        entries.append({"path": relative.as_posix(), "sha256": _sha256(raw)})
    return _sha256(_canonical_json(entries))


def _read_bytes(path: Path, limit: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{path.name} 必须是普通文件")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError(f"{path.name} 类型错误或超过 {limit} bytes")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)
    data = b"".join(chunks)
    if len(data) > limit:
        raise ValueError(f"{path.name} 超过 {limit} bytes")
    return data


def _read_json(path: Path, limit: int) -> dict[str, Any]:
    try:
        value = json.loads(_read_bytes(path, limit).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"无法读取 {path.name}: {type(error).__name__}") from error
    if not isinstance(value, dict):
        raise ValueError("suite.json 必须是 JSON object")
    return value


def _reject_symlink_ancestors(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                raise ValueError(f"路径不能经过符号链接: {current.name}")
        except FileNotFoundError:
            # The final file check reports missing paths with a more useful error.
            break


def load_suite(path: str | os.PathLike[str], *, require_pinned: bool = True) -> Suite:
    suite_path = Path(path).expanduser().absolute()
    _reject_symlink_ancestors(suite_path)
    suite_bytes = _read_bytes(suite_path, MAX_SUITE_BYTES)
    try:
        raw = validate_suite(json.loads(suite_bytes.decode("utf-8")))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"无法读取 suite.json: {type(error).__name__}") from error
    claimed_digest = raw["suite_sha256"]
    body = dict(raw)
    body.pop("suite_sha256")
    calculated_digest = _sha256(_canonical_json(body))
    if claimed_digest != calculated_digest:
        raise ValueError("Suite 摘要不匹配；请检查冻结题目或提升 suite version")
    pin = PINNED_SUITE_FINGERPRINTS.get((raw["suite_id"], raw["version"]))
    if require_pinned and pin != calculated_digest:
        if pin is None:
            raise ValueError("Suite ID/version 尚未冻结；请在审核并更新版本后登记 suite 指纹")
        raise ValueError("Suite 内容已变化但版本未更新；请提升 suite version 并更新冻结指纹")

    suite_root = suite_path.parent.resolve(strict=True)
    loaded: list[SuiteCase] = []
    for item in raw["cases"]:
        relative_case = _safe_relative(item["case_path"], "case_path")
        case_path = _no_symlink_path(suite_root, relative_case)
        case = load_case(case_path)
        if case.case_id != item["case_id"]:
            raise ValueError(f"Suite case_id 与 case.json 不一致: {item['case_id']}")
        case_root = Path(case.case_dir)
        initial = case_root / case.fixture_dir
        known_good = _no_symlink_path(case_root, "known_good", must_be_dir=True)
        grader_path = _no_symlink_path(case_root, case.grader_script)
        grader_bytes = grader_path.read_bytes()
        case_bytes = case_path.read_bytes()
        task_digest = _sha256(case.task.encode("utf-8"))
        initial_digest = tree_sha256(initial)
        known_digest = tree_sha256(known_good)
        actual = {
            "case_sha256": _sha256(case_bytes),
            "task_sha256": task_digest,
            "initial_sha256": initial_digest,
            "grader_sha256": _sha256(grader_bytes),
            "known_good_sha256": known_digest,
        }
        for key, actual_value in actual.items():
            if item[key] != actual_value:
                raise ValueError(f"{case.case_id} 的 {key} 摘要不匹配；请提升 suite version")
        loaded.append(SuiteCase(
            case=case, case_path=case_path, case_sha256=actual["case_sha256"],
            task_sha256=task_digest, initial_sha256=initial_digest,
            grader_sha256=actual["grader_sha256"], known_good_sha256=known_digest,
            regression_test_required=item["regression_test_required"],
        ))
    return Suite(
        suite_id=raw["suite_id"], version=raw["version"], description=raw["description"],
        default_repeats=raw["default_repeats"], suite_sha256=calculated_digest,
        suite_path=suite_path, suite_bytes=suite_bytes, cases=tuple(loaded),
    )


def validate_suite_baselines(suite: Suite) -> list[dict[str, Any]]:
    """Require every committed original to fail and every known-good tree to pass."""
    outcomes: list[dict[str, Any]] = []
    for suite_case in suite.cases:
        case = suite_case.case
        for label, source, expected in (
            ("initial", Path(case.case_dir) / case.fixture_dir, False),
            ("known_good", Path(case.case_dir) / "known_good", True),
        ):
            with tempfile.TemporaryDirectory(prefix=f"mini-agent-eval-{label}-") as temporary:
                workspace = Path(temporary) / "workspace"
                _copy_fixture(source, workspace)
                passed, error = run_grader_for_workspace(
                    case, workspace, timeout=case.grader_timeout_seconds,
                    reference_root=Path(case.case_dir),
                )
            if error is not None or passed is not expected:
                raise ValueError(
                    f"{case.case_id} {label} grader baseline mismatch: expected passed={expected}, "
                    f"got passed={passed}, error={error}"
                )
            outcomes.append({"case_id": case.case_id, "tree": label, "passed": passed})
    return outcomes


def suite_plan(suite: Suite, repeats: int | None = None) -> list[dict[str, Any]]:
    count = suite.default_repeats if repeats is None else repeats
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= MAX_SUITE_REPEATS:
        raise ValueError(f"repeats 必须在 1 到 {MAX_SUITE_REPEATS} 之间")
    return [
        {"case_id": item.case.case_id, "repetition": repetition}
        for item in suite.cases
        for repetition in range(1, count + 1)
    ]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    validate_suite_run(value)
    if len(encoded) > MAX_SUITE_BYTES:
        raise ValueError(f"SuiteRun 超过 {MAX_SUITE_BYTES} bytes")
    fd, temporary_name = tempfile.mkstemp(prefix=".suite-run-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def run_suite(
    suite: Suite,
    output: str | os.PathLike[str],
    *,
    repeats: int | None = None,
    run_kind: str = "live",
    live_confirmed: bool = False,
    runner: EvaluationRunner | None = None,
    fixture_responses: tuple[dict[str, Any], ...] | dict[str, tuple[dict[str, Any], ...]] = (),
) -> Path:
    """Preflight, then run each planned slot once in fixed manifest order."""
    if run_kind not in {"live", "fixture"}:
        raise ValueError("run_kind must be live or fixture")
    if run_kind == "live" and not live_confirmed:
        raise ValueError("真实模型套件调用需要显式 --live")
    planned = suite_plan(suite, repeats)
    validate_suite_baselines(suite)
    active_runner = runner or EvaluationRunner()
    binding_by_id: dict[str, dict[str, str]] = {}
    frozen_binding: dict[str, str] | None = None
    if run_kind == "live":
        # Validate all model bindings before creating a ledger or launching a trial.
        for item in suite.cases:
            binding_by_id[item.case.case_id] = active_runner.validate_live_configuration(item.case)[0]
        unique_bindings = {_canonical_json(value) for value in binding_by_id.values()}
        if len(unique_bindings) != 1:
            raise ValueError("coding suite 的所有题目必须使用同一个冻结 model binding")
        frozen_binding = next(iter(binding_by_id.values()))
        if _code_revision() is None:
            raise ValueError("无法确定代码 revision；拒绝启动 live suite")

    root = Path(output).expanduser().absolute()
    if root.resolve(strict=False).is_relative_to(suite.suite_path.parent.resolve()):
        raise ValueError("suite output 不能位于 suite/题目目录内")
    root.mkdir(parents=True, exist_ok=True)
    ledger_path = root / "suite-run.json"
    suite_copy = root / "suite.json"
    if ledger_path.exists() or ledger_path.is_symlink() or suite_copy.exists() or suite_copy.is_symlink():
        raise FileExistsError("suite-run.json 已存在；每次 suite run 必须使用新的输出目录")
    trial_root = root / "trials"
    trial_root.mkdir(exist_ok=True)
    run_id = str(uuid.uuid4())
    now = _now()
    frozen_revision = _code_revision()
    frozen_runtime = runtime_fingerprint()
    suite_bytes = suite.suite_bytes
    suite_fd = os.open(suite_copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(suite_fd, "wb", closefd=True) as stream:
            stream.write(suite_bytes)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            suite_copy.unlink()
        except OSError:
            pass
        raise
    slots = [
        {
            "slot_id": f"{slot['case_id']}-{slot['repetition']:02d}",
            "case_id": slot["case_id"], "repetition": slot["repetition"],
            "status": "not_run", "trial_path": None, "error_kind": None,
        }
        for slot in planned
    ]
    ledger = {
        "schema_version": 1, "suite_run_id": run_id,
        "suite_id": suite.suite_id, "suite_version": suite.version,
        "suite_sha256": suite.suite_sha256, "run_kind": run_kind,
        "repeats": len(planned) // len(suite.cases), "planned_trials": len(planned),
        "started_at": now, "updated_at": now, "status": "running", "slots": slots,
        "model_binding_ref": frozen_binding, "code_revision": frozen_revision,
        "runtime_fingerprint": frozen_runtime,
    }
    _atomic_json(ledger_path, ledger)
    case_by_id = {item.case.case_id: item for item in suite.cases}
    for index, slot in enumerate(ledger["slots"]):
        suite_case = case_by_id[slot["case_id"]]
        slot["status"] = "running"
        ledger["updated_at"] = _now()
        _atomic_json(ledger_path, ledger)
        metadata = {
            "suite_id": suite.suite_id,
            "suite_version": suite.version,
            "suite_sha256": suite.suite_sha256,
            "suite_run_id": run_id,
            "repetition": slot["repetition"],
            "initial_fixture_sha256": suite_case.initial_sha256,
            "grader_sha256": suite_case.grader_sha256,
            "runtime_fingerprint": frozen_runtime,
        }
        try:
            if _code_revision() != frozen_revision:
                raise RuntimeError("code_revision_changed_before_trial")
            if runtime_fingerprint() != frozen_runtime:
                raise RuntimeError("runtime_fingerprint_changed_before_trial")
            responses = (
                fixture_responses.get(slot["case_id"], ())
                if isinstance(fixture_responses, dict) else fixture_responses
            )
            trial_dir = active_runner.run_case(
                suite_case.case, trial_root, run_kind=run_kind,
                live_confirmed=live_confirmed, responses=responses,
                suite_metadata=metadata,
                expected_model_binding_ref=frozen_binding,
            )
            slot["status"] = "completed"
            slot["trial_path"] = trial_dir.relative_to(root).as_posix()
            slot["error_kind"] = None
        except (KeyboardInterrupt, SystemExit):
            slot["status"] = "not_run"
            slot["error_kind"] = "interrupted"
            ledger["status"] = "interrupted"
            ledger["updated_at"] = _now()
            _atomic_json(ledger_path, ledger)
            raise
        except Exception as error:
            slot["status"] = "infrastructure_error"
            slot["trial_path"] = None
            slot["error_kind"] = type(error).__name__
        ledger["updated_at"] = _now()
        if index == len(ledger["slots"]) - 1:
            ledger["status"] = "completed"
        _atomic_json(ledger_path, ledger)
    return ledger_path


def _read_trial(root: Path, path_text: str, *, expected: dict[str, Any]) -> dict[str, Any]:
    from mini_agent.evaluation.schema import validate_trial_result

    relative = Path(_safe_relative(path_text, "trial_path"))
    trial_dir = root / relative
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("SuiteRun trial path cannot include symlinks")
    if trial_dir.is_symlink() or not trial_dir.is_dir() or not trial_dir.resolve().is_relative_to(root):
        raise ValueError("SuiteRun 引用无效 trial 目录")
    trial_path = trial_dir / "trial.json"
    if trial_path.is_symlink() or not trial_path.is_file() or trial_path.stat().st_size > 256 * 1024:
        raise ValueError("SuiteRun 引用无效 trial.json")
    row = validate_trial_result(json.loads(trial_path.read_text(encoding="utf-8")))
    for key, value in expected.items():
        if row.get(key) != value:
            raise ValueError(f"trial 与 SuiteRun/槽位不匹配: {key}")
    row = dict(row)
    row["trial_path"] = relative.as_posix()
    return row


def _percentile(values: list[int], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * fraction + 0.999999) - 1))
    return float(ordered[index])


def _observed_total(rows: list[dict[str, Any]], key: str) -> dict[str, int | None]:
    values = [row[key] for row in rows if isinstance(row.get(key), int) and not isinstance(row.get(key), bool)]
    return {"observed_trials": len(values), "total": sum(values) if values else None}


def _case_report(slots: list[dict[str, Any]], rows: list[dict[str, Any]]) -> dict[str, Any]:
    from statistics import median

    row_by_rep = {(row["case_id"], row["repetition"]): row for row in rows}
    graded = [
        row for row in rows
        if isinstance(row.get("grader_passed"), bool)
        and row.get("grader_error_kind") is None
        and row.get("failure_kind") not in {"infrastructure_error", "grader_infrastructure_error"}
    ]
    durations = [row["agent_duration_ms"] for row in rows if isinstance(row.get("agent_duration_ms"), int)]
    grader_durations = [row["grader_duration_ms"] for row in rows if isinstance(row.get("grader_duration_ms"), int)]
    failures: dict[str, int] = {}
    for slot in slots:
        row = row_by_rep.get((slot["case_id"], slot["repetition"]))
        failure = row.get("failure_kind", "none") if row else slot.get("error_kind") or slot["status"]
        failures[str(failure)] = failures.get(str(failure), 0) + 1
    infrastructure = sum(
        slot["status"] == "infrastructure_error"
        or (row_by_rep.get((slot["case_id"], slot["repetition"]), {}).get("failure_kind") in {"infrastructure_error", "grader_infrastructure_error"})
        for slot in slots
    )
    manual_review_queue: list[dict[str, Any]] = []
    for slot in slots:
        key = (slot["case_id"], slot["repetition"])
        row = row_by_rep.get(key)
        reason = None
        if slot["status"] == "infrastructure_error":
            reason = "启动或运行基础设施错误；没有可评分的 trial"
        elif row is not None and row.get("failure_kind") in {"infrastructure_error", "grader_infrastructure_error"}:
            reason = "trial 遇到评测或运行基础设施错误；需检查原始记录"
        elif row is not None and row.get("runtime_fingerprint_end") != row.get("runtime_fingerprint"):
            reason = "Agent 运行期间代码来源摘要发生变化；该样本不能代表冻结实现"
        elif row is not None and row.get("_model_binding_consistent") is False:
            reason = "trial 使用的模型来源与 suite 冻结来源不一致；建议复核并排除该样本"
        elif row is not None and row.get("grader_error_kind") is not None:
            reason = "grader 未产生有效结果；需检查评分基础设施"
        elif row is not None and row.get("agent_stop_reason") == "text" and row.get("grader_passed") is False:
            reason = "Agent 正常停止但独立 grader 未通过；建议复核 diff 与评分细节"
        elif row is not None and row.get("grader_passed") is True and row.get("success") is False:
            reason = "独立 grader 通过但 Agent 未满足最终成功条件；建议复核运行状态"
        if reason is not None:
            manual_review_queue.append({
                "slot_id": slot["slot_id"], "case_id": slot["case_id"],
                "repetition": slot["repetition"], "trial_path": row.get("trial_path") if row else None,
                "reason": reason,
            })
    tokens_input = _observed_total(rows, "input_tokens")
    tokens_output = _observed_total(rows, "output_tokens")
    usage_sources: dict[str, int] = {}
    for row in rows:
        source = str(row.get("usage_source", "unavailable"))
        usage_sources[source] = usage_sources.get(source, 0) + 1
    return {
        "planned_trials": len(slots),
        "run_trials": sum(slot["status"] == "completed" for slot in slots),
        "scorable_trials": len(graded),
        "scorable_sample_complete": len(graded) == len(slots),
        "independent_acceptance_passed": sum(row["grader_passed"] is True for row in graded),
        "final_successes": sum(row.get("success") is True for row in rows),
        "infrastructure_errors": infrastructure,
        "failure_counts": failures,
        "agent_llm_calls": _observed_total(rows, "agent_llm_calls"),
        "successful_model_responses": _observed_total(rows, "successful_model_responses"),
        "tool_calls": _observed_total(rows, "tool_calls"),
        "permission_denials": _observed_total(rows, "permission_denials"),
        "usage_source_trials": usage_sources,
        "input_tokens": tokens_input,
        "output_tokens": tokens_output,
        "agent_duration_ms": {
            "observed_trials": len(durations), "median": median(durations) if durations else None,
            "p95": _percentile(durations, 0.95),
        },
        "grader_duration_ms": {
            "observed_trials": len(grader_durations), "median": median(grader_durations) if grader_durations else None,
            "p95": _percentile(grader_durations, 0.95),
        },
        "cost_usd": None,
        "manual_review_queue": manual_review_queue,
        "trials": [
            {
                "slot_id": slot["slot_id"], "repetition": slot["repetition"],
                "status": slot["status"],
                "trial_path": row_by_rep.get((slot["case_id"], slot["repetition"]), {}).get("trial_path"),
                "trial_id": row_by_rep.get((slot["case_id"], slot["repetition"]), {}).get("trial_id"),
                "grader_passed": row_by_rep.get((slot["case_id"], slot["repetition"]), {}).get("grader_passed"),
                "success": row_by_rep.get((slot["case_id"], slot["repetition"]), {}).get("success"),
                "failure_kind": row_by_rep.get((slot["case_id"], slot["repetition"]), {}).get("failure_kind", slot.get("error_kind")),
                "grader_error_kind": row_by_rep.get((slot["case_id"], slot["repetition"]), {}).get("grader_error_kind"),
                "agent_stop_reason": row_by_rep.get((slot["case_id"], slot["repetition"]), {}).get("agent_stop_reason"),
                "agent_state_status": row_by_rep.get((slot["case_id"], slot["repetition"]), {}).get("agent_state_status"),
                "agent_llm_calls": row_by_rep.get((slot["case_id"], slot["repetition"]), {}).get("agent_llm_calls"),
                "tool_calls": row_by_rep.get((slot["case_id"], slot["repetition"]), {}).get("tool_calls"),
                "permission_denials": row_by_rep.get((slot["case_id"], slot["repetition"]), {}).get("permission_denials"),
                "input_tokens": row_by_rep.get((slot["case_id"], slot["repetition"]), {}).get("input_tokens"),
                "output_tokens": row_by_rep.get((slot["case_id"], slot["repetition"]), {}).get("output_tokens"),
                "agent_duration_ms": row_by_rep.get((slot["case_id"], slot["repetition"]), {}).get("agent_duration_ms"),
                "grader_duration_ms": row_by_rep.get((slot["case_id"], slot["repetition"]), {}).get("grader_duration_ms"),
                "usage_source": row_by_rep.get((slot["case_id"], slot["repetition"]), {}).get("usage_source"),
                "model_binding_ref": row_by_rep.get((slot["case_id"], slot["repetition"]), {}).get("model_binding_ref"),
            }
            for slot in slots
        ],
    }


def build_suite_report(suite_run_dir: str | os.PathLike[str]) -> dict[str, Any]:
    root = Path(suite_run_dir).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("report-suite 路径必须是目录")
    ledger_path = root / "suite-run.json"
    raw_ledger = _read_json(ledger_path, MAX_SUITE_BYTES)
    ledger = validate_suite_run(raw_ledger)
    suite_path = root / "suite.json"
    # Runs copy the canonical manifest beside the ledger so later reports are self-contained.
    if suite_path.is_symlink() or not suite_path.is_file():
        raise ValueError("suite-run 目录缺少冻结的 suite.json")
    raw_suite = validate_suite(_read_json(suite_path, MAX_SUITE_BYTES))
    body = dict(raw_suite)
    claimed = body.pop("suite_sha256")
    if _sha256(_canonical_json(body)) != claimed or claimed != ledger["suite_sha256"]:
        raise ValueError("suite-run 的 suite.json 摘要与账本不一致")
    if raw_suite["suite_id"] != ledger["suite_id"] or raw_suite["version"] != ledger["suite_version"]:
        raise ValueError("suite-run 的 suite ID/version 与清单不一致")
    expected_slots = [
        (entry["case_id"], repetition)
        for entry in raw_suite["cases"]
        for repetition in range(1, ledger["repeats"] + 1)
    ]
    actual_slots = [(slot["case_id"], slot["repetition"]) for slot in ledger["slots"]]
    if actual_slots != expected_slots:
        raise ValueError("SuiteRun 槽位与冻结清单顺序/重复次数不一致")
    if ledger["run_kind"] == "live" and ledger["model_binding_ref"] is None:
        raise ValueError("live SuiteRun 缺少冻结模型来源摘要")
    cases = {entry["case_id"]: entry for entry in raw_suite["cases"]}
    by_case: dict[str, list[dict[str, Any]]] = {case_id: [] for case_id in cases}
    trial_count = 0
    for slot in ledger["slots"]:
        if slot["status"] != "completed" or not slot["trial_path"]:
            continue
        expected = {
            "suite_id": ledger["suite_id"], "suite_version": ledger["suite_version"],
            "suite_sha256": ledger["suite_sha256"], "suite_run_id": ledger["suite_run_id"],
            "case_id": slot["case_id"], "repetition": slot["repetition"],
            "initial_fixture_sha256": cases[slot["case_id"]]["initial_sha256"],
            "grader_sha256": cases[slot["case_id"]]["grader_sha256"],
            "runtime_fingerprint": ledger["runtime_fingerprint"],
            "run_kind": ledger["run_kind"],
        }
        row = _read_trial(root, slot["trial_path"], expected=expected)
        row["_model_binding_consistent"] = row.get("model_binding_ref") == ledger["model_binding_ref"]
        by_case[slot["case_id"]].append(row)
        trial_count += 1
    reports = {
        case_id: _case_report(
            [slot for slot in ledger["slots"] if slot["case_id"] == case_id], by_case[case_id],
        )
        for case_id in cases
    }
    all_slots = ledger["slots"]
    all_rows = [row for rows in by_case.values() for row in rows]
    total_report = _case_report(all_slots, all_rows)
    # A fixture suite is a separate cohort, and no price snapshot means cost stays null.
    return {
        "schema_version": 1,
        "suite_id": ledger["suite_id"], "suite_version": ledger["suite_version"],
        "suite_sha256": ledger["suite_sha256"], "suite_run_id": ledger["suite_run_id"],
        "run_kind": ledger["run_kind"], "run_status": ledger["status"],
        "code_revision": ledger["code_revision"],
        "code_revision_consistent": all(row.get("code_revision") == ledger["code_revision"] for row in all_rows),
        "runtime_fingerprint": ledger["runtime_fingerprint"],
        "runtime_fingerprint_consistent": all(
            row.get("runtime_fingerprint") == ledger["runtime_fingerprint"]
            and row.get("runtime_fingerprint_end") == ledger["runtime_fingerprint"]
            for row in all_rows
        ),
        "model_binding_consistent": all(
            row.get("model_binding_ref") == ledger["model_binding_ref"] for row in all_rows
        ),
        "planned_trials": ledger["planned_trials"], "run_trials": trial_count,
        "scorable_trials": total_report["scorable_trials"],
        "live_baseline_complete": bool(
            ledger["run_kind"] == "live"
            and ledger["status"] == "completed"
            and total_report["scorable_trials"] == ledger["planned_trials"]
        ),
        "independent_acceptance_passed": total_report["independent_acceptance_passed"],
        "final_successes": total_report["final_successes"],
        "infrastructure_errors": total_report["infrastructure_errors"],
        "failure_counts": total_report["failure_counts"],
        "agent_llm_calls": total_report["agent_llm_calls"],
        "successful_model_responses": total_report["successful_model_responses"],
        "tool_calls": total_report["tool_calls"],
        "permission_denials": total_report["permission_denials"],
        "usage_source_trials": total_report["usage_source_trials"],
        "input_tokens": total_report["input_tokens"], "output_tokens": total_report["output_tokens"],
        "agent_duration_ms": total_report["agent_duration_ms"],
        "cost_usd": None,
        "cost_note": "缺少价格快照，成本为 null。",
        "manual_review_queue": total_report["manual_review_queue"],
        "cases": reports,
        "trials": [item for report in reports.values() for item in report["trials"]],
        "sample_limitations": "每题计划重复次数有限；样本用于描述本次冻结题集上的表现，不代表一般编码能力。",
        "model_sources": [
            row.get("model_binding_ref") for row in all_rows if row.get("model_binding_ref") is not None
        ],
        "usage_unavailable_trials": sum(
            row.get("input_tokens") is None or row.get("output_tokens") is None for row in all_rows
        ),
        "fixture_and_live_are_separate": True,
        "unread_trials": 0,
    }
