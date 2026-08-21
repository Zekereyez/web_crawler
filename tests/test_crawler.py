"""End-to-end tests against a local HTTP server.

Uses Python's stdlib http.server so no external network is touched.
"""
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from crawler.config import CrawlerConfig
from crawler.crawler import Crawler


PAGES = {
    "/": b"<html><a href='/a'>a</a><a href='/b'>b</a></html>",
    "/a": b"<html><a href='/c'>c</a><a href='/'>root</a></html>",
    "/b": b"<html><a href='/a'>a</a><a href='http://other.example/x'>ext</a></html>",
    "/c": b"<html>leaf</html>",
    "/redir": b"",  # handled specially
    "/dst": b"<html>dst</html>",
    "/big": b"x" * (10 * 1024 * 1024),
    "/robots.txt": b"User-agent: *\nDisallow: /nope\n",
    "/nope": b"<html>secret</html>",
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a, **_kw):
        return

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/redir":
            self.send_response(301)
            self.send_header("Location", "/dst")
            self.end_headers()
            return
        if path not in PAGES:
            self.send_response(404)
            self.end_headers()
            return
        body = PAGES[path]
        self.send_response(200)
        if path == "/robots.txt":
            self.send_header("Content-Type", "text/plain")
        elif path == "/big":
            self.send_header("Content-Type", "text/html")
        else:
            self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ServerFixture:
    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.host, self.port = self.server.server_address
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return f"http://{self.host}:{self.port}"

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()


class TestCrawlerE2E(unittest.TestCase):
    def _config(self, base_url, **overrides):
        cfg = CrawlerConfig(
            max_workers=4,
            max_depth=3,
            max_pages=50,
            same_origin=True,
            per_host_delay=0.0,
            request_timeout=2.0,
            respect_robots=True,
        )
        for k, v in overrides.items():
            setattr(cfg, k, v)
        return cfg

    def test_full_crawl_same_origin(self):
        with ServerFixture() as base:
            c = Crawler(self._config(base))
            result = c.crawl([base])
        # We should have fetched /, /a, /b, /c (not /nope since robots blocks it,
        # not http://other.example/x since it's off-origin).
        paths = {urlparse(u).path for u in result.pages}
        self.assertIn("/", paths)
        self.assertIn("/a", paths)
        self.assertIn("/b", paths)
        self.assertIn("/c", paths)
        self.assertNotIn("/nope", paths)

    def test_robots_disallow(self):
        with ServerFixture() as base:
            c = Crawler(self._config(base))
            # Directly seed with /nope to prove it's blocked.
            result = c.crawl([base + "/nope"])
        self.assertEqual(result.pages, {})
        self.assertGreaterEqual(result.metrics.get("robots_disallowed", 0), 1)

    def test_ignore_robots(self):
        with ServerFixture() as base:
            c = Crawler(self._config(base, respect_robots=False))
            result = c.crawl([base + "/nope"])
        paths = {urlparse(u).path for u in result.pages}
        self.assertIn("/nope", paths)

    def test_redirect_dedup(self):
        with ServerFixture() as base:
            c = Crawler(self._config(base, max_depth=0))
            result = c.crawl([base + "/redir", base + "/dst"])
        paths = {urlparse(u).path for u in result.pages}
        # /redir folds into /dst — we should see /dst exactly once.
        self.assertIn("/dst", paths)
        self.assertNotIn("/redir", paths)

    def test_max_pages_cap(self):
        with ServerFixture() as base:
            c = Crawler(self._config(base, max_pages=2))
            result = c.crawl([base])
        self.assertLessEqual(result.metrics["unique_urls_seen"], 2)

    def test_oversized_body_skipped(self):
        with ServerFixture() as base:
            c = Crawler(self._config(base, max_bytes=1024))
            result = c.crawl([base + "/big"])
        # /big is served with content-length exceeding max_bytes → no body,
        # but it still counts as a fetch attempt.
        self.assertGreaterEqual(result.metrics.get("non_html_fetched", 0), 1)
        page = next(iter(result.pages.values()))
        self.assertEqual(page["links"], [])

    def test_same_origin_blocks_offhost(self):
        with ServerFixture() as base:
            c = Crawler(self._config(base))
            result = c.crawl([base + "/b"])
        # /b links to other.example — must not appear.
        hosts = {urlparse(u).hostname for u in result.pages}
        self.assertNotIn("other.example", hosts)


if __name__ == "__main__":
    unittest.main()
