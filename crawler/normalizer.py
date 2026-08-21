from typing import Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

_DEFAULT_PORTS = {"http": 80, "https": 443}
_ALLOWED_SCHEMES = {"http", "https"}


def normalize_url(url: str, base: Optional[str] = None) -> Optional[str]:
    """Return a canonical form of `url`, or None if it isn't a crawlable http(s) URL.

    Canonicalization:
      - resolve against `base` if given (for relative hrefs)
      - lowercase scheme and host
      - drop default port (80 for http, 443 for https)
      - drop fragment
      - drop empty path -> "/"
      - stable-sort query params so ?a=1&b=2 and ?b=2&a=1 dedup to the same key
    """
    if url is None:
        return None
    url = url.strip()
    if not url:
        return None
    if base:
        url = urljoin(base, url)
    try:
        parts = urlparse(url)
    except ValueError:
        return None

    scheme = (parts.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        return None

    host = parts.hostname
    if not host:
        return None
    host = host.lower()

    try:
        port = parts.port
    except ValueError:
        return None
    if port == _DEFAULT_PORTS.get(scheme):
        port = None
    netloc = host if port is None else f"{host}:{port}"

    path = parts.path or "/"

    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    query_pairs.sort()
    query = urlencode(query_pairs)

    return urlunparse((scheme, netloc, path, "", query, ""))


def origin(url: str) -> Optional[tuple]:
    """(scheme, host, port) — the same-origin key. None if `url` isn't parseable."""
    try:
        parts = urlparse(url)
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        return None
    host = parts.hostname
    if not host:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if port is None:
        port = _DEFAULT_PORTS.get(scheme)
    return (scheme, host.lower(), port)


def same_origin(url1: str, url2: str) -> bool:
    o1 = origin(url1)
    o2 = origin(url2)
    return o1 is not None and o1 == o2


def host_of(url: str) -> Optional[str]:
    try:
        parts = urlparse(url)
    except ValueError:
        return None
    return parts.hostname.lower() if parts.hostname else None
