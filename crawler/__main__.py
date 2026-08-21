import argparse
import json
import logging
import sys

from .config import CrawlerConfig
from .crawler import Crawler


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m crawler",
        description="Concurrent web crawler using a thread pool.",
    )
    p.add_argument("seeds", nargs="+", help="one or more seed URLs")
    p.add_argument("--workers", type=int, default=16, help="max worker threads")
    p.add_argument("--depth", type=int, default=3, help="max link-depth from each seed")
    p.add_argument("--max-pages", type=int, default=1000, help="cap on unique URLs")
    p.add_argument("--same-origin", action="store_true",
                   help="restrict crawl to the origin of each seed")
    p.add_argument("--delay", type=float, default=1.0,
                   help="default per-host politeness delay (seconds)")
    p.add_argument("--timeout", type=float, default=10.0, help="per-request timeout (seconds)")
    p.add_argument("--ignore-robots", action="store_true", help="do not fetch/apply robots.txt")
    p.add_argument("--user-agent", default="SimpleCrawler/1.0 (+https://example.com/bot)")
    p.add_argument("--output", default="-",
                   help="write JSON result to this file (default: stdout)")
    p.add_argument("--verbose", "-v", action="count", default=0)
    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    level = logging.WARNING - (10 * min(args.verbose, 2))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = CrawlerConfig(
        max_workers=args.workers,
        max_depth=args.depth,
        max_pages=args.max_pages,
        same_origin=args.same_origin,
        request_timeout=args.timeout,
        per_host_delay=args.delay,
        respect_robots=not args.ignore_robots,
        user_agent=args.user_agent,
    )

    crawler = Crawler(config)
    result = crawler.crawl(args.seeds)

    payload = {
        "metrics": result.metrics,
        "pages": result.pages,
    }
    text = json.dumps(payload, indent=2, sort_keys=True)
    if args.output == "-":
        print(text)
    else:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
