import time


class ExpiringCache:
    def __init__(self, ttl_seconds, clock=None):
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds must not be negative")
        self.ttl_seconds = ttl_seconds
        self.clock = clock or time.monotonic
        self._entries = {}

    def put(self, key, value):
        self._entries[key] = (value, self.clock())

    def get(self, key):
        entry = self._entries.get(key)
        if entry is None:
            return None
        value, stored_at = entry
        if self.clock() - stored_at > self.ttl_seconds:
            del self._entries[key]
            return None
        return value
