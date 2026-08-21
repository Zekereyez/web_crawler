import unittest

from crawler.parser import extract_links


class TestExtractLinks(unittest.TestCase):
    def test_basic(self):
        html = '<html><a href="/a">A</a><a href="http://x/y">B</a></html>'
        self.assertEqual(extract_links(html), ["/a", "http://x/y"])

    def test_skips_nofollow(self):
        html = (
            '<a href="/keep">k</a>'
            '<a rel="nofollow" href="/skip">s</a>'
            '<a rel="noopener nofollow" href="/skip2">s</a>'
        )
        self.assertEqual(extract_links(html), ["/keep"])

    def test_ignores_non_anchor(self):
        html = '<link href="/css"/><script src="/js"></script><a href="/keep">k</a>'
        self.assertEqual(extract_links(html), ["/keep"])

    def test_handles_missing_href(self):
        html = '<a>no href</a><a href="/x">x</a>'
        self.assertEqual(extract_links(html), ["/x"])

    def test_tolerates_broken_html(self):
        html = '<a href="/a"><a href="/b"</a>'
        # Should not crash; may extract at least the first.
        links = extract_links(html)
        self.assertIn("/a", links)


if __name__ == "__main__":
    unittest.main()
