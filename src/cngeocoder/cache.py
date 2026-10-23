"""进程内 LRU + TTL 缓存。单机部署不需要 Redis；多副本时各自缓存即可（数据只读）。"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Hashable
from typing import Any


class LRUCache:
    def __init__(self, maxsize: int = 10000, ttl: float = 3600.0):
        self.maxsize, self.ttl = maxsize, ttl
        self._data: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    def get(self, key: Hashable) -> Any | None:
        if self.maxsize <= 0:
            return None
        with self._lock:
            item = self._data.get(key)
            if item is None or item[0] < time.monotonic():
                if item is not None:
                    del self._data[key]
                self.misses += 1
                return None
            self._data.move_to_end(key)
            self.hits += 1
            return item[1]

    def put(self, key: Hashable, value: Any) -> None:
        if self.maxsize <= 0:
            return
        with self._lock:
            self._data[key] = (time.monotonic() + self.ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    def stats(self) -> dict[str, int]:
        return {"size": len(self._data), "hits": self.hits, "misses": self.misses}
