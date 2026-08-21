import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Iterable, List, Optional, Set

import requests

from .config import CrawlerConfig
from .fetcher import Fetcher
from .frontier import Frontier, Item
from .metrics import Metrics
from .normalizer import host_of, normalize_url, origin
from .parser import extract_links
from .rate_limiter import HostRateLimiter
from .robots import RobotsCache

logger = logging.getLogger(__name__)


class CrawlResult:
    def __init__(self) -> None:
        self.pages: Dict[str, Dict] = {}  # canonical URL -> {status, content_type, links}
        self.metrics: Dict[str, float] = {}

    def __repr__(self) -> str:
        return f"CrawlResult(pages={len(self.pages)}, metrics={self.metrics})"


class Crawler:
    """Multi-threaded crawler.

    Lifecycle:
        c = Crawler(config)
        result = c.crawl([seed1, seed2])

    All shared state is guarded:
      - `_seen` (canonical dedup set) under `_seen_lock`
      - `_results` under `_results_lock`
      - frontier, robots, and rate limiter have their own internal locking
    """

    def __init__(
        self,
        config: Optional[CrawlerConfig] = None,
        session: Optional[requests.Session] = None,
        fetcher: Optional[Fetcher] = None,
        robots: Optional[RobotsCache] = None,
        rate_limiter: Optional[HostRateLimiter] = None,
    ) -> None:
        self._config = config or CrawlerConfig()
        self._session = session or requests.Session()
        self._fetcher = fetcher or Fetcher(self._config, session=self._session)
        self._robots = robots or RobotsCache(
            self._config.user_agent,
            timeout=self._config.request_timeout,
            session=self._session,
        )
        self._rate_limiter = rate_limiter or HostRateLimiter(
            default_delay=self._config.per_host_delay
        )

        self._frontier = Frontier()
        self._seen: Set[str] = set()
        self._seen_lock = threading.Lock()
        self._results: Dict[str, Dict] = {}
        self._results_lock = threading.Lock()
        self._metrics = Metrics()
        self._stop_event = threading.Event()

        self._seed_origins: List[tuple] = []

    # ---- public API ---------------------------------------------------

    def crawl(self, seeds: Iterable[str]) -> CrawlResult:
        seeds = list(seeds)
        if self._config.same_origin:
            self._seed_origins = [o for o in (origin(s) for s in seeds) if o]

        # Enqueue seeds.
        for seed in seeds:
            canonical = normalize_url(seed)
            if canonical is None:
                logger.warning("skipping unparseable seed: %r", seed)
                continue
            self._try_enqueue(canonical, depth=0)

        with ThreadPoolExecutor(max_workers=self._config.max_workers) as pool:
            for _ in range(self._config.max_workers):
                pool.submit(self._worker_loop)

            # Block main thread until frontier is quiescent (or stopped).
            self._frontier.wait_done()
            self._stop_event.set()
            # Executor's __exit__ joins the workers.

        result = CrawlResult()
        with self._results_lock:
            result.pages = dict(self._results)
        result.metrics = self._metrics.snapshot()
        result.metrics["unique_urls_seen"] = len(self._seen)
        return result

    # ---- worker -------------------------------------------------------

    def _worker_loop(self) -> None:
        # Short poll so workers notice the stop event promptly without
        # busy-looping.
        while not self._stop_event.is_set():
            item = self._frontier.get(timeout=0.25)
            if item is None:
                continue
            try:
                self._process(item)
            except Exception:
                # A crash on one page must not take down the worker.
                logger.exception("worker error processing %s", item.url)
                self._metrics.incr("worker_exceptions")
            finally:
                self._frontier.task_done()

    def _process(self, item: Item) -> None:
        url = item.url
        host = host_of(url)
        if host is None:
            return

        if self._config.respect_robots:
            try:
                if not self._robots.can_fetch(url):
                    self._metrics.incr("robots_disallowed")
                    return
                delay = self._robots.crawl_delay(url)
                if delay:
                    self._rate_limiter.set_delay(host, delay)
            except Exception:
                logger.exception("robots check failed for %s", url)
                self._metrics.incr("robots_errors")
                # Fail-closed on robots error: skip this URL.
                return

        self._rate_limiter.acquire(host)

        self._metrics.incr("fetch_attempts")
        result = self._fetcher.fetch(url)

        if result.status == 0:
            self._metrics.incr("network_errors")
            return

        # Throttled: honor Retry-After via a bounded re-enqueue.
        if result.status in (429, 503):
            self._metrics.incr(f"http_{result.status}")
            if item.attempts < self._config.max_retries:
                wait = result.retry_after or 1.0
                # Sleep here (in the worker) then re-enqueue. This costs one
                # worker for `wait` seconds — acceptable at our thread count.
                time.sleep(min(wait, self._config.max_retry_after))
                self._try_reenqueue(item)
            return

        if 400 <= result.status < 600:
            self._metrics.incr(f"http_{result.status // 100}xx")
            return

        # Redirect: the URL we actually fetched may differ. Dedup on the
        # final canonical URL — if we've already seen it, don't record twice.
        final_canonical = normalize_url(result.final_url)
        if final_canonical is None:
            return
        if final_canonical != url:
            self._metrics.incr("redirects")
            if not self._mark_seen(final_canonical):
                return
            if self._config.same_origin and not self._matches_seed_origin(final_canonical):
                return

        if result.body is None:
            # Non-HTML or oversized — count as fetched but no links to extract.
            self._metrics.incr("non_html_fetched")
            self._record(final_canonical, result.status, result.content_type, [])
            return

        links = extract_links(result.body)
        self._metrics.incr("pages_fetched")
        self._record(final_canonical, result.status, result.content_type, links)

        if item.depth < self._config.max_depth:
            for href in links:
                child = normalize_url(href, base=result.final_url)
                if child is None:
                    continue
                if self._config.same_origin and not self._matches_seed_origin(child):
                    continue
                self._try_enqueue(child, depth=item.depth + 1)

    # ---- helpers ------------------------------------------------------

    def _try_enqueue(self, canonical: str, depth: int) -> bool:
        if depth > self._config.max_depth:
            return False
        if not self._mark_seen(canonical):
            return False
        self._frontier.put(Item(url=canonical, depth=depth))
        return True

    def _try_reenqueue(self, item: Item) -> None:
        """Put a throttled item back on the queue without going through the
        dedup gate — it's already in `_seen`."""
        new_item = Item(url=item.url, depth=item.depth, attempts=item.attempts + 1)
        self._frontier.put(new_item)

    def _mark_seen(self, canonical: str) -> bool:
        """Atomic check-and-add. Returns True if this call added the URL."""
        with self._seen_lock:
            if canonical in self._seen:
                return False
            if len(self._seen) >= self._config.max_pages:
                # Cap reached. Trigger stop and refuse further additions.
                self._frontier.trigger_stop()
                return False
            self._seen.add(canonical)
            return True

    def _matches_seed_origin(self, url: str) -> bool:
        o = origin(url)
        return o is not None and o in self._seed_origins

    def _record(self, url: str, status: int, content_type: str, links: List[str]) -> None:
        with self._results_lock:
            self._results[url] = {
                "status": status,
                "content_type": content_type,
                "links": links,
            }
