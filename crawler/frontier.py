import queue
import threading
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass
class Item:
    url: str
    depth: int
    attempts: int = 0


class Frontier:
    """URL queue with an in-flight counter for quiescence detection.

    The interesting part: `_in_flight` counts (items in queue) + (items
    checked out by a worker but not yet done). Every `put` increments;
    every `task_done` decrements. When the counter hits zero we know
    there is genuinely no more work — an empty queue by itself does NOT
    imply we're done, because a worker might be mid-fetch and about to
    enqueue children.

    Termination flow:
      - main thread calls `wait_done()` (blocks on Event)
      - Event fires when in_flight -> 0, or when an external caller
        (page-cap, kill switch) calls `trigger_stop()`
      - main thread then sets a stop flag so workers exit their poll loop
    """

    def __init__(self) -> None:
        self._queue: "queue.Queue[Item]" = queue.Queue()
        self._lock = threading.Lock()
        self._in_flight = 0
        self._done = threading.Event()
        self._stopped = False

    def put(self, item: Item) -> None:
        with self._lock:
            if self._stopped:
                return
            self._in_flight += 1
        self._queue.put(item)

    def get(self, timeout: Optional[float] = None) -> Optional[Item]:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def task_done(self) -> None:
        with self._lock:
            self._in_flight -= 1
            if self._in_flight <= 0:
                self._done.set()

    def wait_done(self, timeout: Optional[float] = None) -> bool:
        return self._done.wait(timeout=timeout)

    def trigger_stop(self) -> None:
        """External stop (e.g. page-cap hit). Latches the done event."""
        with self._lock:
            self._stopped = True
        self._done.set()

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    @property
    def stopped(self) -> bool:
        with self._lock:
            return self._stopped
