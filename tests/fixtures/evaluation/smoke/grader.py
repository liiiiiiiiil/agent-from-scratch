"""Hidden standalone grader for the smoke evaluation case."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    workspace = Path(sys.argv[1]).resolve(strict=True)
    target = workspace / "src" / "scale.py"
    if not target.is_file() or target.is_symlink():
        print(json.dumps({"passed": False, "checks": {"file_exists": False}}))
        return 0
    spec = importlib.util.spec_from_file_location("evaluation_candidate", target)
    if spec is None or spec.loader is None:
        print(json.dumps({"passed": False, "checks": {"module_loads": False}}))
        return 0
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        checks = {
            "positive_integer": module.scale(3) == 6,
            "negative_integer": module.scale(-4) == -8,
            "zero": module.scale(0) == 0,
            "float": module.scale(1.5) == 3.0,
        }
    except Exception as error:
        checks = {"candidate_runs": False, "error_kind": type(error).__name__}
    passed = all(value is True for value in checks.values())
    print(json.dumps({"passed": passed, "checks": checks}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
