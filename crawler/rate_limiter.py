import threading
import time
from typing import Callable, Dict


class HostRateLimiter:
    """At most one request per host per `default_delay` seconds.

    Design notes:
      - The reservation (updating `_next_time`) happens under a lock; the
        actual sleep happens OUTSIDE the lock so one slow host does not block
        acquisitions for other hosts.
      - `set_delay(host, delay)` lets robots.txt Crawl-delay bump a specific
        host's spacing above the default.
      - `time_fn` / `sleep_fn` are injected so tests can drive it with a fake
        clock without real sleeps.
    """

    def __init__(
        self,
        default_delay: float = 1.0,
        time_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._default_delay = default_delay
        self._next_time: Dict[str, float] = {}
        self._delay_overrides: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._time = time_fn
        self._sleep = sleep_fn

    def set_delay(self, host: str, delay: float) -> None:
        """Record a host-specific delay (e.g. from robots.txt Crawl-delay).

        The effective delay used at acquire time is max(default, this),
        so a smaller-than-default override never weakens our politeness.
        """
        if delay is None or delay < 0:
            return
        with self._lock:
            cur = self._delay_overrides.get(host, 0.0)
            if delay > cur:
                self._delay_overrides[host] = delay

    def acquire(self, host: str) -> None:
        """Block until it is safe to fire the next request to `host`."""
        with self._lock:
            now = self._time()
            override = self._delay_overrides.get(host, 0.0)
            delay = max(self._default_delay, override)
            next_ok = self._next_time.get(host, 0.0)
            wait = max(0.0, next_ok - now)
            self._next_time[host] = max(now, next_ok) + delay
        if wait > 0:
            self._sleep(wait)
