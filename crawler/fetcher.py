import logging
from dataclasses import dataclass
from typing import Optional

import requests

from .config import CrawlerConfig

logger = logging.getLogger(__name__)


@dataclass
class FetchResult:
    final_url: str
    status: int
    content_type: str
    body: Optional[str]  # None if filtered by content type or truncated
    retry_after: Optional[float] = None
    error: Optional[str] = None


class FetchError(Exception):
    pass


class Fetcher:
    """HTTP GET with:
      - streaming so we can decide before downloading the body,
      - content-type filter (fetch the header, discard the body if not HTML),
      - byte cap (defense against crawler traps that serve giant files),
      - Retry-After extraction on 429/503,
      - `requests`-level redirect following (final URL is exposed for redirect dedup).

    The socket is always closed via the context manager — critical: an
    unclosed streaming response leaks a connection back to the pool.
    """

    def __init__(self, config: CrawlerConfig, session: Optional[requests.Session] = None) -> None:
        self._config = config
        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": config.user_agent})

    def _parse_retry_after(self, header_value: Optional[str]) -> Optional[float]:
        if not header_value:
            return None
        try:
            v = float(header_value)
        except ValueError:
            # HTTP-date form is legal but rare in practice; ignore and let
            # the caller apply its own backoff.
            return None
        return min(v, self._config.max_retry_after) if v > 0 else None

    def fetch(self, url: str) -> FetchResult:
        try:
            resp = self._session.get(
                url,
                timeout=self._config.request_timeout,
                allow_redirects=True,
                stream=True,
            )
        except requests.RequestException as e:
            return FetchResult(
                final_url=url,
                status=0,
                content_type="",
                body=None,
                error=str(e),
            )

        with resp:
            retry_after = self._parse_retry_after(resp.headers.get("Retry-After"))
            content_type = (
                resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
            )

            if not self._is_allowed_content_type(content_type):
                return FetchResult(
                    final_url=resp.url,
                    status=resp.status_code,
                    content_type=content_type,
                    body=None,
                    retry_after=retry_after,
                )

            cl = resp.headers.get("Content-Length")
            if cl:
                try:
                    if int(cl) > self._config.max_bytes:
                        return FetchResult(
                            final_url=resp.url,
                            status=resp.status_code,
                            content_type=content_type,
                            body=None,
                            retry_after=retry_after,
                            error="oversized",
                        )
                except ValueError:
                    pass

            chunks = []
            total = 0
            try:
                for chunk in resp.iter_content(chunk_size=8192):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > self._config.max_bytes:
                        return FetchResult(
                            final_url=resp.url,
                            status=resp.status_code,
                            content_type=content_type,
                            body=None,
                            retry_after=retry_after,
                            error="oversized",
                        )
                    chunks.append(chunk)
            except requests.RequestException as e:
                return FetchResult(
                    final_url=resp.url,
                    status=resp.status_code,
                    content_type=content_type,
                    body=None,
                    retry_after=retry_after,
                    error=str(e),
                )

            raw = b"".join(chunks)
            encoding = resp.encoding or resp.apparent_encoding or "utf-8"
            try:
                body = raw.decode(encoding, errors="replace")
            except (LookupError, TypeError):
                body = raw.decode("utf-8", errors="replace")

            return FetchResult(
                final_url=resp.url,
                status=resp.status_code,
                content_type=content_type,
                body=body,
                retry_after=retry_after,
            )

    def _is_allowed_content_type(self, content_type: str) -> bool:
        if not content_type:
            return False
        return content_type in self._config.allowed_content_types
