import contextlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

WORKSPACE = Path(sys.argv[1]).resolve()
REFERENCE = Path(os.environ["MINI_AGENT_EVALUATION_REFERENCE_ROOT"]).resolve()
sys.path.insert(0, str(WORKSPACE / "src"))


class CacheBehavior(unittest.TestCase):
    def test_value_is_available_before_ttl(self):
        from cache import ExpiringCache
        now = [10.0]
        cache = ExpiringCache(5, clock=lambda: now[0])
        cache.put("key", "value")
        now[0] = 14.999
        self.assertEqual(cache.get("key"), "value")

    def test_value_expires_at_ttl_boundary(self):
        from cache import ExpiringCache
        now = [10.0]
        cache = ExpiringCache(5, clock=lambda: now[0])
        cache.put("key", "value")
        now[0] = 15.0
        self.assertIsNone(cache.get("key"))


def run_candidate_tests(root):
    return subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_cache_expiry.py"],
        cwd=root,
        env={"PATH": __import__("os").environ.get("PATH", ""), "PYTHONPATH": "src", "PYTHONNOUSERSITE": "1"},
        capture_output=True,
        text=True,
        timeout=30,
    )


class RegressionTestEvidence(unittest.TestCase):
    def test_submitted_regression_test_passes_current_code_and_fails_original(self):
        test_file = WORKSPACE / "tests" / "test_cache_expiry.py"
        self.assertTrue(test_file.is_file(), "tests/test_cache_expiry.py is required")
        current = run_candidate_tests(WORKSPACE)
        current_text = current.stdout + current.stderr
        self.assertEqual(current.returncode, 0, current_text[-3000:])
        match = re.search(r"Ran (\d+) tests?", current_text)
        self.assertIsNotNone(match, current_text[-3000:])
        self.assertGreater(int(match.group(1)), 0)

        with tempfile.TemporaryDirectory(prefix="cache-regression-original-") as temporary:
            old_root = Path(temporary)
            shutil.copytree(REFERENCE / "initial" / "src", old_root / "src")
            (old_root / "tests").mkdir()
            shutil.copyfile(test_file, old_root / "tests" / "test_cache_expiry.py")
            old = run_candidate_tests(old_root)
            old_text = old.stdout + old.stderr
            self.assertNotEqual(old.returncode, 0, "regression test passed on the original broken code")
            self.assertRegex(old_text, r"(?m)^FAIL: ")
            self.assertIn("AssertionError", old_text)


suite = unittest.TestSuite([
    unittest.defaultTestLoader.loadTestsFromTestCase(CacheBehavior),
    unittest.defaultTestLoader.loadTestsFromTestCase(RegressionTestEvidence),
])
stream = io.StringIO()
with contextlib.redirect_stdout(stream):
    result = unittest.TextTestRunner(stream=stream, verbosity=1).run(suite)
print(json.dumps({"passed": result.wasSuccessful(), "detail": stream.getvalue()[-5000:]}, ensure_ascii=False))
