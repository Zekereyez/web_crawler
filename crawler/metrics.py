import threading
import time
from collections import Counter
from typing import Dict


class Metrics:
    """Thread-safe counter bag. Cheap enough to hit from every worker.

    In production you would wire this to statsd/Prometheus; here we keep
    it in-process and print a snapshot at end of run.
    """

    def __init__(self) -> None:
        self._counters: Counter = Counter()
        self._lock = threading.Lock()
        self._start = time.monotonic()

    def incr(self, name: str, count: int = 1) -> None:
        with self._lock:
            self._counters[name] += count

    def snapshot(self) -> Dict[str, float]:
        with self._lock:
            data = dict(self._counters)
        data["elapsed_s"] = round(time.monotonic() - self._start, 3)
        return data
