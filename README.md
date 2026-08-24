# web_crawler

A concurrent, thread-pool web crawler in ~600 lines of Python. The design
priorities, in order: deterministic termination, per-host politeness, and a
metrics dump you can trust at the end of a run. It's meant to read top to
bottom in one sitting.

- Concurrent fetching via `ThreadPoolExecutor`
- Same-origin option
- `robots.txt` cached per host, honoring `Crawl-delay`
- Per-host politeness rate limiter
- Streaming fetch with content-type and size caps
- Redirect-aware URL dedup
- Quiescence-based termination (no premature "queue is empty, we must be done!")
- Bounded retries on `Retry-After`
- Metrics snapshot at end of run

Docs:
- [`GUIDE.md`](./GUIDE.md). How you'd arrive at this solution from scratch,
  one problem at a time. Aimed at a new engineer, or interview prep.
- [`DESIGN.md`](./DESIGN.md). Reference: architecture diagram, data schemas,
  sequence and state diagrams.

## Install

Requires Python 3.9+.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

```bash
.venv/bin/python -m crawler https://example.com \
    --workers 16 --depth 2 --max-pages 200 --same-origin
```

CLI options:

| Flag | Default | Description |
| --- | --- | --- |
| `--workers N` | 16 | Thread-pool size. |
| `--depth N` | 3 | Max link depth from any seed. |
| `--max-pages N` | 1000 | Global cap on distinct URLs. |
| `--same-origin` | off | Restrict to `(scheme, host, port)` of each seed. |
| `--delay S` | 1.0 | Min seconds between requests to a single host. |
| `--timeout S` | 10.0 | Per-request timeout. |
| `--ignore-robots` | off | Skip `robots.txt` fetch/apply. |
| `--user-agent UA` | `SimpleCrawler/1.0` | Sent on every request. |
| `--output FILE` | stdout | Write a JSON summary here. |

## Test

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Runs 27+ tests: URL normalization, rate limiting (with an injected clock, so
no real sleeps), frontier quiescence, HTML parsing, and end-to-end against a
local `http.server` fixture that exercises `robots.txt`, redirects, oversized
bodies, `same-origin` filtering, and `max-pages`.

## Layout

```
crawler/
├── config.py        CrawlerConfig, all tunables in one dataclass
├── normalizer.py    canonical URL key, same-origin check
├── rate_limiter.py  per-host next-allowed-time (sleep outside the lock)
├── robots.py        cached RobotFileParser per host, no thundering herd
├── parser.py        stdlib HTMLParser link extractor (rel="nofollow" aware)
├── fetcher.py       streaming GET with content-type and byte cap
├── frontier.py      Queue plus in-flight counter for quiescence detection
├── metrics.py       thread-safe counter bag
├── crawler.py       orchestrator: worker loop, dedup, redirect fold
├── __main__.py      CLI
└── __init__.py

tests/
├── test_normalizer.py     canonicalization corner cases
├── test_rate_limiter.py   fake clock, no real sleeps
├── test_frontier.py       quiescence, stop, put-after-stop
├── test_parser.py         link extraction, nofollow, broken HTML
└── test_crawler.py        end-to-end against a local http.server
```

## Design highlights

The bits an interviewer would push on.

**Concurrency model.** Threads, because the workload is I/O-bound. A fetch
spends over 99% of its wall clock in `recv()`, and threads block on I/O
without burning CPU. `asyncio` would give higher connection density per
process, but it demands async everything: DNS, parser, robots. Threads are
the simpler correct choice at this scale (10^4 to 10^6 URLs on one box).

**Dedup key.** A single canonical string per URL. See
[`normalizer.py`](./crawler/normalizer.py):
- lowercase scheme and host
- drop default ports (`:80` on http, `:443` on https)
- drop fragment
- empty path becomes `/`
- sort query params

Dedup happens under `_seen_lock` at two points: at enqueue time, and, after a
fetch, on the final (post-redirect) URL. Same key, both places.

**Termination.** An empty queue is not enough. A worker mid-fetch may be
about to enqueue children. The frontier keeps an `in_flight` counter: every
`put` increments it, every `task_done` decrements, and a `done` Event fires
when it hits zero. The main thread blocks on that event, then flips a stop
flag so workers drain out of their poll loop. `max_pages` triggers the same
stop path.

**Politeness.** In this order per URL:
1. `RobotsCache.can_fetch(url)`. Lazy per-host, one HTTP request even under
   fan-in (a per-host lock prevents the herd).
2. `Crawl-delay` from robots, if any, is fed into the rate limiter as a
   per-host override. Combined via `max(default, override)` so a smaller
   `Crawl-delay` never weakens the default politeness.
3. `HostRateLimiter.acquire(host)`. Reserves the next slot under a lock, then
   sleeps outside the lock. A slow host doesn't stall other hosts.
4. `Fetcher.fetch(url)`. Streams the response, checks Content-Type before
   downloading the body, and caps at `max_bytes` to survive traps.
5. On 429/503 with `Retry-After`, the worker sleeps for up to
   `max_retry_after` and re-enqueues, bounded by `max_retries`.

**Robustness.** A worker exception is caught at the top of `_worker_loop`,
logged, and counted in metrics; the worker keeps running. Sockets always
close (streaming responses live inside a `with` block). Malformed HTML is
tolerated: `HTMLParser.feed()` is wrapped so a bad page returns whatever
links it managed to extract.

## Observability

Every run returns a `CrawlResult` with:

- `pages`: `{ canonical_url: {status, content_type, links} }`
- `metrics`: counter snapshot. `pages_fetched`, `fetch_attempts`,
  `network_errors`, `redirects`, `robots_disallowed`, `http_4xx`, `http_5xx`,
  `http_429`, `http_503`, `worker_exceptions`, `unique_urls_seen`,
  `elapsed_s`.

In production these would go to statsd or Prometheus. Alerts I would wire:

- `network_errors / fetch_attempts > 5%` for 5m: sustained network issues.
- `worker_exceptions > 0`: a bug or unexpected input; page a human.
- `elapsed_s > SLA and pages_fetched < expected * 0.9`: coverage regression.
- `http_429 + http_503 > threshold` for a single host: we're being throttled;
  raise per-host delay.

## Testing strategy

- Unit: normalizer, rate limiter (fake clock, no real sleeps), parser,
  frontier semantics.
- Concurrency: frontier quiescence with in-flight items, put-after-stop.
- Integration: end-to-end against a local `http.server` that serves linked
  pages, a robots.txt, a redirect, and an oversized body. Confirms
  same-origin, robots enforcement, redirect dedup, and page cap.
- Fault injection (extensions I would add for prod): a stub session that
  returns 5xx, 429, or timeouts on a schedule; a stub that returns 10 MB of
  `<a href='/loop?p=N'>` to prove trap resistance; a stub that returns
  `Content-Length: 0` then hangs to prove timeouts fire.

## Known limitations, and where they'd break at scale

- `_seen: set[str]` in memory. Fine to ~10^6 URLs; that's the first RAM
  ceiling. Swap in a bloom filter plus an on-disk exact set (rocksdb or
  sqlite) for larger runs. Accept a tiny false-positive rate on the frontier
  side.
- Single process. Per-host politeness is trivially correct only inside one
  process. A distributed crawler needs a coordinator, or consistent hashing,
  so one host is owned by one node.
- In-memory frontier. No restart survivability. Move to a durable queue
  (SQS, Redis Streams, Postgres) if a mid-run process death must not cost
  the whole crawl.
- `Retry-After` sleeps in a worker. Costs one worker slot for the duration.
  Fine at high concurrency, noticeable at very low. A dedicated
  delay-scheduler thread with a heap would be strictly better, and is where
  I'd go next.

See [`DESIGN.md`](./DESIGN.md) for the full picture.
