from dataclasses import dataclass, field
from typing import Tuple


@dataclass
class CrawlerConfig:
    """Tunables for a single crawl run.

    Numbers are defaults, not hard limits. Rate limiter, robots cache, and
    fetcher all read from this struct so a run's behavior is fully described
    by one immutable object.
    """

    max_workers: int = 16
    max_depth: int = 3
    max_pages: int = 1000
    same_origin: bool = False

    request_timeout: float = 10.0
    max_bytes: int = 5 * 1024 * 1024  # 5 MiB per response body

    per_host_delay: float = 1.0  # min seconds between requests to a single host
    respect_robots: bool = True
    max_retry_after: float = 30.0  # cap for honoring Retry-After
    max_retries: int = 2

    user_agent: str = "SimpleCrawler/1.0 (+https://example.com/bot)"
    allowed_content_types: Tuple[str, ...] = ("text/html", "application/xhtml+xml")
