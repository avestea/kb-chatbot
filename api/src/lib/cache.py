import time
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class SimpleCache(Generic[K, V]):
    def __init__(self, max_size: int = 100, ttl_seconds: int = 60):
        self._store: dict[K, tuple[V, float]] = {}
        self.max_size = max_size
        self.ttl = ttl_seconds

    def get(self, key: K) -> V | None:
        entry = self._store.get(key)
        if entry and time.time() - entry[1] < self.ttl:
            return entry[0]
        self._store.pop(key, None)
        return None

    def set(self, key: K, value: V) -> None:
        if len(self._store) >= self.max_size:
            oldest = min(self._store, key=lambda k: self._store[k][1])
            del self._store[oldest]
        self._store[key] = (value, time.time())
