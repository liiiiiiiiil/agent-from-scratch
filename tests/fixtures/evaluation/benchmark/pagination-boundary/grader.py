import contextlib
import io
import json
from pathlib import Path
import sys
import unittest

WORKSPACE = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(WORKSPACE / "src"))


class PaginationBehavior(unittest.TestCase):
    def test_empty_input(self):
        from pagination import paginate
        self.assertEqual(paginate([], 3), [])

    def test_exact_page_boundary_keeps_last_page(self):
        from pagination import paginate
        items = list(range(6))
        self.assertEqual(paginate(items, 3), [items[:3], items[3:]])

    def test_partial_last_page(self):
        from pagination import paginate
        items = list(range(7))
        self.assertEqual(paginate(items, 3), [items[:3], items[3:6], items[6:]])

    def test_invalid_page_size(self):
        from pagination import paginate
        with self.assertRaises(ValueError):
            paginate([1], 0)


stream = io.StringIO()
with contextlib.redirect_stdout(stream):
    result = unittest.TextTestRunner(stream=stream, verbosity=1).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(PaginationBehavior)
    )
print(json.dumps({"passed": result.wasSuccessful(), "detail": stream.getvalue()[-4000:]}, ensure_ascii=False))
