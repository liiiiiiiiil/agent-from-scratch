from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import sys
import time

import pytest

from mini_agent.evaluation.benchmark import (
    PINNED_SUITE_FINGERPRINTS, build_suite_report, load_suite, run_suite,
    runtime_fingerprint, validate_suite_baselines,
)
from mini_agent.evaluation.runner import EvaluationRunner, _copy_fixture, run_grader_for_workspace
from mini_agent.evaluation.schema import (
    SUITE_SCHEMA_V1, TRIAL_RESULT_SCHEMA_V1, TRIAL_RESULT_SCHEMA_V2,
    load_case, validate_suite, validate_suite_run, validate_trial_result,
)
from mini_agent.evaluation.__main__ import _fixture_responses


SUITE_PATH = Path(__file__).parent / "fixtures" / "evaluation" / "benchmark" / "suite.json"
CASE_IDS = [
    "pagination-boundary", "orders-discount-receipt",
    "cache-expiry-regression", "config-priority-investigation",
]


def _case_dir(case_id: str) -> Path:
    return SUITE_PATH.parent / case_id


def _grader(case_id: str, source: Path, workspace: Path) -> bool | None:
    case = load_case(_case_dir(case_id) / "case.json")
    _copy_fixture(source, workspace)
    passed, error = run_grader_for_workspace(
        case, workspace, timeout=case.grader_timeout_seconds,
        reference_root=_case_dir(case_id),
    )
    assert error is None
    return passed


def _tool_call(name: str, arguments: dict, call_id: str) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _edit_responses(*calls: dict) -> tuple[dict, ...]:
    return (
        {"role": "assistant", "content": None, "tool_calls": list(calls)},
        {"role": "assistant", "content": "修复已经完成。", "tool_calls": []},
    )


def _benchmark_responses() -> dict[str, tuple[dict, ...]]:
    pagination = "def paginate(items, page_size):\n    if page_size < 1:\n        raise ValueError(\"page_size must be positive\")\n    return [items[offset:offset + page_size] for offset in range(0, len(items), page_size)]\n"
    pricing = (
        "def calculate_discount_cents(subtotal_cents, discount_percent):\n"
        "    if subtotal_cents < 0 or not 0 <= discount_percent <= 100:\n"
        "        raise ValueError(\"invalid order discount\")\n"
        "    return (subtotal_cents * discount_percent + 50) // 100\n\n"
        "def amount_due_cents(subtotal_cents, discount_percent):\n"
        "    return subtotal_cents - calculate_discount_cents(subtotal_cents, discount_percent)\n"
    )
    receipt = (
        "from pricing import amount_due_cents, calculate_discount_cents\n\n"
        "def _money(cents):\n    return f\"${cents // 100}.{cents % 100:02d}\"\n\n"
        "def render_receipt(subtotal_cents, discount_percent):\n"
        "    discount = calculate_discount_cents(subtotal_cents, discount_percent)\n"
        "    due = amount_due_cents(subtotal_cents, discount_percent)\n"
        "    return (\n"
        "        f\"Subtotal: {_money(subtotal_cents)}\\n\"\n"
        "        f\"Discount ({discount_percent}%): -{_money(discount)}\\n\"\n"
        "        f\"Amount due: {_money(due)}\\n\"\n"
        "    )\n"
    )
    cache = (
        "import time\n\nclass ExpiringCache:\n"
        "    def __init__(self, ttl_seconds, clock=None):\n"
        "        if ttl_seconds < 0:\n            raise ValueError(\"ttl_seconds must not be negative\")\n"
        "        self.ttl_seconds = ttl_seconds\n        self.clock = clock or time.monotonic\n        self._entries = {}\n\n"
        "    def put(self, key, value):\n        self._entries[key] = (value, self.clock())\n\n"
        "    def get(self, key):\n        entry = self._entries.get(key)\n        if entry is None:\n            return None\n"
        "        value, stored_at = entry\n        if self.clock() - stored_at >= self.ttl_seconds:\n"
        "            del self._entries[key]\n            return None\n        return value\n"
    )
    cache_test = (
        "import unittest\nfrom cache import ExpiringCache\n\n"
        "class CacheExpiryTests(unittest.TestCase):\n"
        "    def test_value_is_available_before_ttl(self):\n"
        "        now = [10.0]\n        cache = ExpiringCache(5, clock=lambda: now[0])\n"
        "        cache.put(\"key\", \"value\")\n        now[0] = 14.999\n"
        "        self.assertEqual(cache.get(\"key\"), \"value\")\n\n"
        "    def test_value_expires_at_ttl_boundary(self):\n"
        "        now = [10.0]\n        cache = ExpiringCache(5, clock=lambda: now[0])\n"
        "        cache.put(\"key\", \"value\")\n        now[0] = 15.0\n"
        "        self.assertIsNone(cache.get(\"key\"))\n"
    )
    config = (
        "def resolve_settings(defaults, project_values, environment):\n"
        "    resolved = dict(defaults)\n    resolved.update(project_values)\n"
        "    resolved.update(environment)\n    return resolved\n"
    )
    return {
        "pagination-boundary": _edit_responses(_tool_call("write_file", {"path": "src/pagination.py", "content": pagination}, "p1")),
        "orders-discount-receipt": _edit_responses(
            _tool_call("write_file", {"path": "src/pricing.py", "content": pricing}, "o1"),
            _tool_call("write_file", {"path": "src/receipt.py", "content": receipt}, "o2"),
        ),
        "cache-expiry-regression": _edit_responses(
            _tool_call("write_file", {"path": "src/cache.py", "content": cache}, "c1"),
            _tool_call("write_file", {"path": "tests/test_cache_expiry.py", "content": cache_test}, "c2"),
        ),
        "config-priority-investigation": _edit_responses(
            _tool_call("write_file", {"path": "src/bootstrap.py", "content": config}, "b1"),
        ),
    }


def test_suite_manifest_and_all_offline_oracles_are_frozen_and_consistent():
    suite = load_suite(SUITE_PATH)
    assert suite.suite_id == "coding-benchmark"
    assert suite.version == "1.1"
    assert suite.suite_sha256 == PINNED_SUITE_FINGERPRINTS[(suite.suite_id, suite.version)]
    assert [item.case.case_id for item in suite.cases] == CASE_IDS
    assert len(validate_suite_baselines(suite)) == 8
    assert SUITE_SCHEMA_V1["properties"]["cases"]["maxItems"] >= 4
    assert TRIAL_RESULT_SCHEMA_V2["properties"]["schema_version"]["const"] == 2
    assert TRIAL_RESULT_SCHEMA_V1["properties"]["schema_version"]["const"] == 1


def test_live_suite_refuses_to_start_without_explicit_confirmation(tmp_path):
    suite = load_suite(SUITE_PATH)
    output = tmp_path / "must-not-start"
    with pytest.raises(ValueError, match="--live"):
        run_suite(suite, output, repeats=3, run_kind="live", live_confirmed=False)
    assert not output.exists()


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_each_original_fails_and_each_known_good_passes(case_id, tmp_path):
    assert _grader(case_id, _case_dir(case_id) / "initial", tmp_path / "initial") is False
    assert _grader(case_id, _case_dir(case_id) / "known_good", tmp_path / "known-good") is True


def test_key_error_variants_are_rejected_by_independent_graders(tmp_path):
    pagination = _case_dir("pagination-boundary")
    workspace = tmp_path / "pagination"
    assert _grader("pagination-boundary", pagination / "known_good", workspace) is True
    (workspace / "src" / "pagination.py").write_text(
        "def paginate(items, page_size):\n"
        "    if page_size < 1: raise ValueError()\n"
        "    return [items[i:i + page_size] for i in range(0, len(items) - page_size, page_size)]\n",
        encoding="utf-8",
    )
    case = load_case(pagination / "case.json")
    assert run_grader_for_workspace(case, workspace, reference_root=pagination)[0] is False

    orders = _case_dir("orders-discount-receipt")
    pricing_only = tmp_path / "pricing-only"
    assert _grader("orders-discount-receipt", orders / "known_good", pricing_only) is True
    shutil.copyfile(orders / "initial" / "src" / "receipt.py", pricing_only / "src" / "receipt.py")
    case = load_case(orders / "case.json")
    assert run_grader_for_workspace(case, pricing_only, reference_root=orders)[0] is False
    receipt_only = tmp_path / "receipt-only"
    assert _grader("orders-discount-receipt", orders / "known_good", receipt_only) is True
    shutil.copyfile(orders / "initial" / "src" / "pricing.py", receipt_only / "src" / "pricing.py")
    assert run_grader_for_workspace(case, receipt_only, reference_root=orders)[0] is False

    cache_case = _case_dir("cache-expiry-regression")
    cache_workspace = tmp_path / "weak-cache-test"
    assert _grader("cache-expiry-regression", cache_case / "known_good", cache_workspace) is True
    (cache_workspace / "tests" / "test_cache_expiry.py").write_text(
        "import unittest\nclass WeakTest(unittest.TestCase):\n"
        "    def test_nothing(self): self.assertEqual(1, 1)\n",
        encoding="utf-8",
    )
    case = load_case(cache_case / "case.json")
    assert run_grader_for_workspace(case, cache_workspace, reference_root=cache_case)[0] is False

    config_case = _case_dir("config-priority-investigation")
    config_workspace = tmp_path / "config"
    assert _grader("config-priority-investigation", config_case / "known_good", config_workspace) is True
    shutil.copyfile(config_case / "initial" / "src" / "bootstrap.py", config_workspace / "src" / "bootstrap.py")
    case = load_case(config_case / "case.json")
    assert run_grader_for_workspace(case, config_workspace, reference_root=config_case)[0] is False


def test_suite_rejects_duplicate_ids_traversal_symlinks_and_changed_summaries(tmp_path):
    raw = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
    assert validate_suite(raw)["suite_id"] == "coding-benchmark"
    duplicate = json.loads(json.dumps(raw))
    duplicate["cases"][1]["case_id"] = duplicate["cases"][0]["case_id"]
    with pytest.raises(ValueError, match="重复 case_id"):
        validate_suite(duplicate)
    traversal = json.loads(json.dumps(raw))
    traversal["cases"][0]["case_path"] = "../outside/case.json"
    with pytest.raises(ValueError, match="不得包含"):
        validate_suite(traversal)

    copied_root = tmp_path / "benchmark"
    shutil.copytree(SUITE_PATH.parent, copied_root)
    suite_path = copied_root / "suite.json"
    changed = json.loads(suite_path.read_text(encoding="utf-8"))
    changed["description"] += " changed"
    suite_path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="Suite 摘要不匹配"):
        load_suite(suite_path)

    copied_root = tmp_path / "changed-tree"
    shutil.copytree(SUITE_PATH.parent, copied_root)
    initial_file = copied_root / CASE_IDS[0] / "initial" / "src" / "pagination.py"
    initial_file.write_text(initial_file.read_text() + "\n# drift\n", encoding="utf-8")
    with pytest.raises(ValueError, match="initial_sha256 摘要不匹配"):
        load_suite(copied_root / "suite.json")

    symlink_root = tmp_path / "symlink-tree"
    shutil.copytree(SUITE_PATH.parent, symlink_root)
    target = symlink_root / CASE_IDS[0] / "initial" / "src" / "pagination.py"
    external = tmp_path / "external.py"
    external.write_text(target.read_text(), encoding="utf-8")
    target.unlink()
    try:
        target.symlink_to(external)
    except (OSError, NotImplementedError):
        pytest.skip("filesystem does not permit symlinks")
    with pytest.raises(ValueError, match="符号链接"):
        load_suite(symlink_root / "suite.json")


def test_schema_one_result_stays_readable(tmp_path):
    case = load_case(Path(__file__).parent / "fixtures" / "evaluation" / "smoke" / "case.json")
    result = EvaluationRunner().run_case(
        case, tmp_path / "compat",
        run_kind="fixture", responses=_fixture_responses(),
    )
    raw = json.loads((result / "trial.json").read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
    assert validate_trial_result(raw)["trial_id"] == raw["trial_id"]


def test_repeated_fixture_slots_start_from_identical_fixtures_and_report_evidence(tmp_path):
    suite = load_suite(SUITE_PATH)
    ledger_path = run_suite(
        suite, tmp_path / "repeat-run", repeats=2, run_kind="fixture",
        fixture_responses=_benchmark_responses(),
    )
    ledger = validate_suite_run(json.loads(ledger_path.read_text(encoding="utf-8")))
    assert ledger["planned_trials"] == 8
    assert all(slot["status"] == "completed" for slot in ledger["slots"])
    report = build_suite_report(ledger_path.parent)
    assert report["run_kind"] == "fixture"
    assert report["planned_trials"] == 8
    assert report["run_trials"] == 8
    assert report["scorable_trials"] == 8
    assert report["live_baseline_complete"] is False
    assert report["independent_acceptance_passed"] == 8
    assert report["final_successes"] == 8
    assert report["cost_usd"] is None
    assert report["code_revision_consistent"] is True
    assert report["runtime_fingerprint_consistent"] is True
    assert report["usage_source_trials"] == {"fixture": 8}
    assert report["manual_review_queue"] == []
    for case_id in CASE_IDS:
        details = report["cases"][case_id]
        assert details["planned_trials"] == details["run_trials"] == details["scorable_trials"] == 2
        assert details["scorable_sample_complete"] is True
        assert len(details["trials"]) == 2
        first, second = [Path(item["trial_path"]) for item in details["trials"]]
        first_diff = (ledger_path.parent / first / "diff.patch").read_bytes()
        second_diff = (ledger_path.parent / second / "diff.patch").read_bytes()
        assert first_diff == second_diff
        for item in details["trials"]:
            row = json.loads((ledger_path.parent / item["trial_path"] / "trial.json").read_text())
            assert row["schema_version"] == 2
            assert row["initial_fixture_sha256"] == next(
                suite_case.initial_sha256 for suite_case in suite.cases if suite_case.case.case_id == case_id
            )
            assert row["suite_run_id"] == ledger["suite_run_id"]
            assert validate_trial_result(row) == row


def test_partial_interruption_preserves_all_planned_slots_without_retry(tmp_path):
    suite = load_suite(SUITE_PATH)

    class InterruptOnce:
        def __init__(self):
            self.calls = 0

        def run_case(self, *args, **kwargs):
            self.calls += 1
            raise KeyboardInterrupt()

    fake = InterruptOnce()
    output = tmp_path / "interrupted"
    with pytest.raises(KeyboardInterrupt):
        run_suite(suite, output, repeats=3, run_kind="fixture", runner=fake)
    raw = json.loads((output / "suite-run.json").read_text(encoding="utf-8"))
    ledger = validate_suite_run(raw)
    assert fake.calls == 1
    assert ledger["status"] == "interrupted"
    assert ledger["planned_trials"] == 12
    assert len(ledger["slots"]) == 12
    assert all(slot["status"] == "not_run" for slot in ledger["slots"])
    report = build_suite_report(output)
    assert report["planned_trials"] == 12
    assert report["run_trials"] == 0
    assert report["scorable_trials"] == 0


def test_prestart_infrastructure_error_keeps_slot_and_report_denominator(tmp_path):
    suite = load_suite(SUITE_PATH)
    real = EvaluationRunner()

    class OneStartupFailure:
        def __init__(self):
            self.calls = 0

        def run_case(self, case, output, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise OSError("worker could not start")
            return real.run_case(case, output, **kwargs)

    output = tmp_path / "startup-failure"
    ledger_path = run_suite(
        suite, output, repeats=2, run_kind="fixture", runner=OneStartupFailure(),
        fixture_responses=_benchmark_responses(),
    )
    ledger = validate_suite_run(json.loads(ledger_path.read_text(encoding="utf-8")))
    assert ledger["planned_trials"] == 8
    assert ledger["slots"][0]["status"] == "infrastructure_error"
    assert ledger["slots"][0]["error_kind"] == "OSError"
    assert sum(slot["status"] == "completed" for slot in ledger["slots"]) == 7
    report = build_suite_report(output)
    assert report["planned_trials"] == 8
    assert report["run_trials"] == 7
    assert report["scorable_trials"] == 7
    assert report["live_baseline_complete"] is False
    assert report["infrastructure_errors"] == 1
    assert report["runtime_fingerprint_consistent"] is True
    assert report["cases"][CASE_IDS[0]]["scorable_trials"] == 1
    assert report["cases"][CASE_IDS[0]]["scorable_sample_complete"] is False


def test_receipt_exact_format_is_public_and_known_good_meets_it(tmp_path):
    case = load_case(_case_dir("orders-discount-receipt") / "case.json")
    assert case.version == "1.1"
    assert "Subtotal: $12.35\\nDiscount (15%): -$1.85\\nAmount due: $10.50\\n" in case.task
    assert "ValueError" in case.task
    assert _grader(case.case_id, Path(case.case_dir) / "known_good", tmp_path / "receipt") is True


def test_cleanup_does_not_follow_grader_symlinks(tmp_path, monkeypatch):
    from mini_agent.evaluation import runner as module

    outside = tmp_path / "outside.txt"
    outside.write_text("external")
    outside.chmod(0o400)
    original = module._run_child

    def leave_link(argv, **kwargs):
        info = original(argv, **kwargs)
        if "mini_agent.evaluation.worker" not in argv:
            (kwargs["cwd"] / "external-link").symlink_to(outside)
        return info

    monkeypatch.setattr(module, "_run_child", leave_link)
    case = load_suite(SUITE_PATH).cases[0]
    trial = EvaluationRunner().run_case(
        case.case, tmp_path / "results", run_kind="fixture",
        responses=_benchmark_responses()[case.case.case_id],
        suite_metadata={
            "suite_id": "coding-benchmark", "suite_version": "1.1",
            "suite_sha256": load_suite(SUITE_PATH).suite_sha256,
            "suite_run_id": "01234567-89ab-cdef-0123-456789abcdef", "repetition": 1,
            "initial_fixture_sha256": case.initial_sha256,
            "grader_sha256": case.grader_sha256,
            "runtime_fingerprint": runtime_fingerprint(),
        },
    )
    assert stat.S_IMODE(outside.stat().st_mode) == 0o400
    assert outside.read_text() == "external"
    assert json.loads((trial / "trial.json").read_text())["cleanup_complete"] is True


@pytest.mark.skipif(os.name != "posix", reason="process group assertion uses POSIX")
@pytest.mark.parametrize("stage", ["agent", "grader"])
def test_real_child_interrupt_is_cleaned_and_published_before_suite_stops(tmp_path, monkeypatch, stage):
    from mini_agent.evaluation import runner as module

    original = module.subprocess.Popen
    children = []

    def launch(argv, **kwargs):
        is_worker = "mini_agent.evaluation.worker" in argv
        is_trial_grader = not is_worker and Path(argv[1]).parent.name.startswith("mini-agent-eval-")
        if not (is_worker if stage == "agent" else is_trial_grader):
            return original(argv, **kwargs)
        marker = Path(argv[-1]) if is_worker else kwargs["cwd"] / "grader-started.marker"
        process = original(
            [sys.executable, "-c", "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text('started'); time.sleep(30)", str(marker)],
            **kwargs,
        )
        children.append(process)
        wait = process.wait
        first = True

        def interrupted_wait(*args, **options):
            nonlocal first
            if first:
                first = False
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert marker.exists()
                raise KeyboardInterrupt()
            return wait(*args, **options)

        process.wait = interrupted_wait
        return process

    monkeypatch.setattr(module.subprocess, "Popen", launch)
    output = tmp_path / "interrupted-child"
    with pytest.raises(KeyboardInterrupt):
        run_suite(
            load_suite(SUITE_PATH), output, repeats=3, run_kind="fixture",
            fixture_responses=_benchmark_responses(),
        )
    assert len(children) == 1 and children[0].poll() is not None
    ledger = validate_suite_run(json.loads((output / "suite-run.json").read_text()))
    assert ledger["status"] == "interrupted"
    assert ledger["slots"][0]["status"] == "completed"
    assert all(slot["status"] == "not_run" for slot in ledger["slots"][1:])
    trial = json.loads((output / ledger["slots"][0]["trial_path"] / "trial.json").read_text())
    assert trial["agent_started"] is True
    assert trial["failure_kind"] == ("agent_interrupted" if stage == "agent" else "grader_infrastructure_error")
    if stage == "grader":
        assert trial["grader_error_kind"] == "interrupted"
    assert trial["cleanup_complete"] is True
    assert build_suite_report(output)["run_trials"] == 1


@pytest.mark.parametrize("field", ["model_binding_ref", "code_revision", "runtime_fingerprint_end"])
def test_source_mismatch_is_excluded_and_baseline_incomplete(tmp_path, field):
    source = Path(__file__).parents[1] / "docs/evaluation/baselines/v0.51/live-20260927"
    output = tmp_path / "archive"
    shutil.copytree(source, output)
    trial_path = next((output / "trials").glob("trial-pagination-*/trial.json"))
    trial = json.loads(trial_path.read_text())
    if field == "model_binding_ref":
        trial[field]["fingerprint"] = "0" * 64
    else:
        trial[field] = "0" * (40 if field == "code_revision" else 64)
    trial_path.write_text(json.dumps(trial))
    report = build_suite_report(output)
    assert report["live_baseline_complete"] is False
    assert report["source_mismatch_trials"] == 1
    assert report["scorable_trials"] == 11
    assert report["final_successes"] == 5
    assert report["run_trials"] == 12
    assert any(item["trial_path"] == trial_path.parent.relative_to(output).as_posix() for item in report["manual_review_queue"])
