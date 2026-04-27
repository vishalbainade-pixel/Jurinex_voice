"""Lightweight in-memory counters — placeholder for a real metrics backend."""

from __future__ import annotations

from collections import defaultdict
from threading import Lock


class Metrics:
    def __init__(self) -> None:
        self._counters: dict[str, int] = defaultdict(int)
        self._lock = Lock()

    def incr(self, name: str, value: int = 1) -> None:
        with self._lock:
            self._counters[name] += value

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counters)


metrics = Metrics()
