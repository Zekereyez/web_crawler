import logging
import threading
from collections import defaultdict
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests

logger = logging.getLogger(__name__)


class RobotsCache:
    """Per-host robots.txt cache.

    Semantics chosen to match common practice:
      - 2xx: parse and honor rules + crawl-delay
      - 4xx (incl. 404): fully allow
      - 5xx or network error: fully disallow (fail-closed, so a broken host
        doesn't turn into a hammering situation)

    Concurrency:
      - `_cache_lock` guards the dict read/write.
      - `_host_locks[host]` prevents a thundering herd on first fetch — many
        workers hitting the same host see one fetch and share the result.
    """

    def __init__(
        self,
        user_agent: str,
        timeout: float = 10.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        self._user_agent = user_agent
        self._timeout = timeout
        self._session = session or requests.Session()
        self._cache: Dict[str, Tuple[RobotFileParser, Optional[float]]] = {}
        self._cache_lock = threading.Lock()
        self._host_locks: Dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._host_locks_lock = threading.Lock()

    def _host_key(self, url: str) -> str:
        parts = urlparse(url)
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme}://{parts.hostname}{port}"

    def _fetch(self, host_key: str) -> Tuple[RobotFileParser, Optional[float]]:
        parser = RobotFileParser()
        robots_url = f"{host_key}/robots.txt"
        try:
            resp = self._session.get(
                robots_url,
                timeout=self._timeout,
                headers={"User-Agent": self._user_agent},
                allow_redirects=True,
            )
        except requests.RequestException as e:
            logger.debug("robots fetch error %s: %s", robots_url, e)
            parser.parse(["User-agent: *", "Disallow: /"])
            return parser, None

        if 400 <= resp.status_code < 500:
            parser.parse([""])  # allow all
            return parser, None
        if resp.status_code >= 500:
            parser.parse(["User-agent: *", "Disallow: /"])
            return parser, None

        try:
            lines = resp.text.splitlines()
            parser.parse(lines)
        except Exception:
            logger.exception("failed to parse robots.txt from %s", robots_url)
            parser.parse([""])
            return parser, None

        delay = parser.crawl_delay(self._user_agent)
        return parser, (float(delay) if delay else None)

    def _lock_for(self, host_key: str) -> threading.Lock:
        with self._host_locks_lock:
            return self._host_locks[host_key]

    def get(self, url: str) -> Tuple[RobotFileParser, Optional[float]]:
        host_key = self._host_key(url)
        with self._cache_lock:
            hit = self._cache.get(host_key)
            if hit is not None:
                return hit

        host_lock = self._lock_for(host_key)
        with host_lock:
            with self._cache_lock:
                hit = self._cache.get(host_key)
                if hit is not None:
                    return hit
            result = self._fetch(host_key)
            with self._cache_lock:
                self._cache[host_key] = result
            return result

    def can_fetch(self, url: str) -> bool:
        parser, _ = self.get(url)
        return parser.can_fetch(self._user_agent, url)

    def crawl_delay(self, url: str) -> Optional[float]:
        _, delay = self.get(url)
        return delay
