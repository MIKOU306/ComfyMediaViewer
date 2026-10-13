"""Byte-budgeted immutable JSON responses, bound by the caller to durable versions."""
from collections import OrderedDict
import json
import threading


class ResponseCache:
    def __init__(self, entries=32, byte_limit=16 << 20):
        self.entries, self.byte_limit = entries, byte_limit
        self._rows = OrderedDict()
        self._bytes = 0
        self._lock = threading.RLock()

    def get(self, key):
        with self._lock:
            payload = self._rows.get(key)
            if payload is None:
                return None
            self._rows.move_to_end(key)
        return json.loads(payload)

    def put(self, key, value):
        payload = json.dumps(value, ensure_ascii=False).encode('utf-8')
        if len(payload) > self.byte_limit // 2:
            return
        with self._lock:
            previous = self._rows.pop(key, None)
            self._bytes -= len(previous) if previous else 0
            self._rows[key] = payload
            self._bytes += len(payload)
            while len(self._rows) > self.entries or self._bytes > self.byte_limit:
                _, old = self._rows.popitem(last=False)
                self._bytes -= len(old)
