import unittest

from crawler.normalizer import host_of, normalize_url, origin, same_origin


class TestNormalize(unittest.TestCase):
    def test_lowercases_scheme_and_host(self):
        self.assertEqual(
            normalize_url("HTTP://Example.COM/Path"),
            "http://example.com/Path",
        )

    def test_drops_default_ports(self):
        self.assertEqual(normalize_url("http://x.com:80/a"), "http://x.com/a")
        self.assertEqual(normalize_url("https://x.com:443/a"), "https://x.com/a")

    def test_keeps_non_default_ports(self):
        self.assertEqual(normalize_url("http://x.com:8080/a"), "http://x.com:8080/a")

    def test_drops_fragment(self):
        self.assertEqual(normalize_url("http://x.com/a#frag"), "http://x.com/a")

    def test_empty_path_becomes_slash(self):
        self.assertEqual(normalize_url("http://x.com"), "http://x.com/")

    def test_sorts_query_params(self):
        self.assertEqual(
            normalize_url("http://x.com/?b=2&a=1"),
            normalize_url("http://x.com/?a=1&b=2"),
        )

    def test_resolves_relative(self):
        self.assertEqual(
            normalize_url("../b", base="http://x.com/a/c/d"),
            "http://x.com/a/b",
        )

    def test_rejects_non_http(self):
        self.assertIsNone(normalize_url("mailto:foo@bar.com"))
        self.assertIsNone(normalize_url("javascript:alert(1)"))
        self.assertIsNone(normalize_url("ftp://x.com"))

    def test_rejects_junk(self):
        self.assertIsNone(normalize_url(""))
        self.assertIsNone(normalize_url("   "))
        self.assertIsNone(normalize_url(None))
        self.assertIsNone(normalize_url("not a url"))

    def test_same_origin(self):
        self.assertTrue(same_origin("http://x.com/a", "http://x.com/b"))
        self.assertTrue(same_origin("http://x.com:80/a", "http://x.com/b"))
        self.assertFalse(same_origin("http://x.com/a", "https://x.com/a"))
        self.assertFalse(same_origin("http://x.com/a", "http://y.com/a"))
        self.assertFalse(same_origin("http://x.com:8080/a", "http://x.com/a"))

    def test_origin_normalizes_port(self):
        # default port is filled in for comparison
        self.assertEqual(origin("http://x.com/"), origin("http://x.com:80/"))

    def test_host_of(self):
        self.assertEqual(host_of("http://Example.COM/a"), "example.com")


if __name__ == "__main__":
    unittest.main()
