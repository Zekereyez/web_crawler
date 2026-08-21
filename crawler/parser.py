from html.parser import HTMLParser
from typing import List


class _LinkExtractor(HTMLParser):
    """Pulls href values out of <a> tags.

    - Skips rel="nofollow".
    - Uses stdlib HTMLParser so we don't need a heavy dependency; malformed
      HTML is tolerated (feed() swallows most quirks) and we defensively
      catch parser errors at the call site.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag != "a":
            return
        rel = ""
        href = None
        for k, v in attrs:
            if v is None:
                continue
            if k == "rel":
                rel = v.lower()
            elif k == "href":
                href = v
        if href and "nofollow" not in rel.split():
            self.links.append(href)


def extract_links(html: str) -> List[str]:
    parser = _LinkExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # A single malformed page shouldn't take down its worker; return what
        # we managed to collect.
        pass
    return parser.links
