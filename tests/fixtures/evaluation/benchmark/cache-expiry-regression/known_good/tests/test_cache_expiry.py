from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cache import ExpiringCache


class CacheExpiryTests(unittest.TestCase):
    def test_value_is_available_before_ttl(self):
        now = [10.0]
        cache = ExpiringCache(5, clock=lambda: now[0])
        cache.put("key", "value")
        now[0] = 14.999
        self.assertEqual(cache.get("key"), "value")

    def test_value_expires_at_ttl_boundary(self):
        now = [10.0]
        cache = ExpiringCache(5, clock=lambda: now[0])
        cache.put("key", "value")
        now[0] = 15.0
        self.assertIsNone(cache.get("key"))


if __name__ == "__main__":
    unittest.main()
