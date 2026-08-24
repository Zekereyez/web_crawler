# Design doc: concurrent web crawler

Companion to [README.md](./README.md). This document is the reference for the
architecture, the data schemas, and the path a single URL takes through the
system.

---

## 1. Goals and non-goals

Goals:

- Fetch up to ~10^6 unique URLs per run, concurrently, on one machine.
- Survive bad pages and bad hosts without crashing.
- Respect `robots.txt`, per-host rate limits, and `Retry-After`.
- Terminate on quiescence, not on an apparent empty queue.
- Cover the concurrent paths with tests and metrics.

Non-goals, set to bound scope:

- Crawling across machines.
- A persistent frontier, or crash recovery mid-run.
- JavaScript rendering. The crawler parses static HTML only.
- Indexing and full-text search. The output is a link graph and a status per
  URL. Downstream consumers do the rest.

---

## 2. Architecture

The CLI parses arguments into a `CrawlerConfig`, then hands seeds to the
`Crawler`. The `Crawler` owns three pieces of shared state (`_seen`,
`_results`, and the `Frontier`) and a fixed thread pool of workers. Each
worker pulls one URL, runs it through robots, rate limiting, fetch, and
link extraction, and enqueues its children back on the same frontier.

```
                        ┌──────────────────────────────────────┐
                        │             CLI / main               │
                        │  parse args → build CrawlerConfig    │
                        └──────────────────┬───────────────────┘
                                           │ seeds
                                           ▼
     ┌─────────────────────────────────────────────────────────────────┐
     │                          Crawler                                │
     │                                                                 │
     │  ┌────────────┐   ┌────────────────┐   ┌──────────────────────┐ │
     │  │  Frontier  │◄──┤   _seen (set,  │   │      Metrics         │ │
     │  │  Queue +   │   │   under lock)  │   │  (thread-safe        │ │
     │  │  in_flight │   └────────────────┘   │   counter bag)       │ │
     │  └────┬───────┘                        └──────────────────────┘ │
     │       │ Item(url, depth, attempts)                              │
     │       ▼                                                         │
     │  ┌──────────────────────────────────────────────────────────┐   │
     │  │             ThreadPoolExecutor (N workers)               │   │
     │  │                                                          │   │
     │  │   worker_loop:                                           │   │
     │  │     item ← frontier.get(timeout)                         │   │
     │  │     process(item):                                       │   │
     │  │       ┌─────────────┐   ┌───────────────┐                │   │
     │  │       │ RobotsCache │──►│  RateLimiter  │──► Fetcher     │   │
     │  │       │  (per-host  │   │  (per-host    │                │   │
     │  │       │   cached)   │   │   spacing)    │                │   │
     │  │       └─────────────┘   └───────────────┘                │   │
     │  │       parser → normalize → enqueue children              │   │
     │  │     frontier.task_done()                                 │   │
     │  └──────────────────────────────────────────────────────────┘   │
     │                                                                 │
     └─────────────────────────────────────────────────────────────────┘
                                     │
                                     ▼
                     CrawlResult { pages, metrics }
```

Each component is one file with one responsibility. Locks live in the
component that owns the state. Components only talk to each other through
their public methods.

---

## 3. Data schemas

### 3.1 `CrawlerConfig`: immutable run parameters

`CrawlerConfig` is a frozen dataclass. The `Crawler` reads it. No worker
writes back to it.

```python
CrawlerConfig(
    max_workers: int              # thread pool size
    max_depth: int                # link depth from any seed
    max_pages: int                # hard cap on distinct URLs
    same_origin: bool             # restrict to seed origins
    request_timeout: float        # per-request seconds
    max_bytes: int                # per-response body cap
    per_host_delay: float         # min seconds between same-host requests
    respect_robots: bool
    max_retry_after: float        # cap on Retry-After we'll honor
    max_retries: int              # retries per URL on 429 or 503
    user_agent: str
    allowed_content_types: tuple  # e.g. ("text/html", "application/xhtml+xml")
)
```

### 3.2 `Item`: one frontier entry

Each queued URL is one `Item`. The `url` field is already canonical, so it
doubles as the dedup key.

```python
Item(
    url: str          # already-canonicalized (dedup key)
    depth: int        # 0 for seed
    attempts: int     # incremented on retry after 429 or 503
)
```

### 3.3 `FetchResult`: what the `Fetcher` returns

`FetchResult` carries a status of 0 for a network error and the real HTTP
status otherwise. A `body` of `None` means the fetcher chose not to
download it: filtered by content-type, over `max_bytes`, or non-HTML.

```python
FetchResult(
    final_url: str            # post-redirect URL from `requests`
    status: int               # 0 for network error, else HTTP status
    content_type: str         # lowercased, ";..." stripped
    body: Optional[str]       # None if filtered, oversized, or non-HTML
    retry_after: Optional[float]
    error: Optional[str]      # short reason on failure
)
```

### 3.4 `CrawlResult`: the end-of-run summary

One `CrawlResult` is returned to the caller. `pages` is keyed by canonical
URL. `metrics` is the full counter snapshot. Field names match the alert
names in [README §Observability](./README.md#observability).

```python
CrawlResult:
  pages   : { canonical_url: {status, content_type, links: [str]} }
  metrics : {
      fetch_attempts, pages_fetched, non_html_fetched,
      redirects, robots_disallowed, robots_errors,
      network_errors, worker_exceptions,
      http_4xx, http_5xx, http_429, http_503,
      unique_urls_seen, elapsed_s,
  }
```

### 3.5 Shared state and its guards

Every piece of mutable shared state lives in one component under one lock.
No lock is taken across component boundaries.

| State | Owner | Guard | Purpose |
| --- | --- | --- | --- |
| `_seen: set[str]` | `Crawler` | `_seen_lock` | Canonical URL dedup. |
| `_results: dict` | `Crawler` | `_results_lock` | One record per URL. |
| queue and `in_flight: int` | `Frontier` | `queue.Queue` lock, `_lock` | Frontier and quiescence. |
| `_next_time: dict[host, float]` | `HostRateLimiter` | `_lock` | Next allowed time per host. |
| `_delay_overrides: dict[host, float]` | `HostRateLimiter` | `_lock` | Crawl-delay upgrades. |
| `_cache: dict[host_key, (Parser, delay)]` | `RobotsCache` | `_cache_lock` | Robots memoization. |
| `_host_locks: dict[host_key, Lock]` | `RobotsCache` | `_host_locks_lock` | Prevent thundering herd on first fetch. |

---

## 4. Sequence flow: one URL through the system

The worker calls six components in a fixed order:

1. `RobotsCache.can_fetch(url)`. If the host disallows the URL, the worker
   records it and returns.
2. `RobotsCache.crawl_delay(host)`. If robots names a delay, feed it to the
   rate limiter as a per-host upgrade.
3. `HostRateLimiter.acquire(host)`. Compute the wait under a lock, then
   sleep outside the lock.
4. `Fetcher.fetch(url)`. Stream the response with a content-type check and
   a byte cap.
5. `Parser.extract_links(body)`. Return whatever links parsed cleanly.
6. `Frontier.put(child)`, once per surviving link. Same-origin and
   `_mark_seen` filter first.

The diagram below shows the same flow as swim lanes.

```
┌────────┐   ┌──────────┐   ┌────────┐   ┌─────────────┐   ┌──────────┐   ┌────────┐   ┌───────────┐
│Frontier│   │ Crawler  │   │RobotsCache│  │RateLimiter │   │ Fetcher  │   │ Parser │   │ Frontier  │
│  (out) │   │_process()│   │           │  │            │   │          │   │        │   │  (children)│
└───┬────┘   └────┬─────┘   └─────┬─────┘  └─────┬──────┘   └────┬─────┘   └───┬────┘   └────┬──────┘
    │             │               │              │               │              │             │
    │  get()      │               │              │               │              │             │
    ├────────────►│               │              │               │              │             │
    │  Item       │               │              │               │              │             │
    │             │  can_fetch?   │              │               │              │             │
    │             ├──────────────►│              │               │              │             │
    │             │  yes/no       │              │               │              │             │
    │             │◄──────────────┤              │               │              │             │
    │             │               │              │               │              │             │
    │             │  crawl_delay? │              │               │              │             │
    │             ├──────────────►│              │               │              │             │
    │             │  Optional[s]  │              │               │              │             │
    │             │◄──────────────┤              │               │              │             │
    │             │                              │               │              │             │
    │             │  set_delay(host, delay)      │               │              │             │
    │             ├─────────────────────────────►│               │              │             │
    │             │                              │               │              │             │
    │             │  acquire(host)  (sleep OUTSIDE lock)          │              │             │
    │             ├─────────────────────────────►│               │              │             │
    │             │                              │               │              │             │
    │             │  fetch(url)                                  │              │             │
    │             ├─────────────────────────────────────────────►│              │             │
    │             │              FetchResult (streamed, capped)  │              │             │
    │             │◄─────────────────────────────────────────────┤              │             │
    │             │                                                             │             │
    │             │  extract_links(body)                                        │             │
    │             ├────────────────────────────────────────────────────────────►│             │
    │             │                                              [str, ...]     │             │
    │             │◄────────────────────────────────────────────────────────────┤             │
    │             │                                                                           │
    │             │  for each link:  normalize → same-origin? → _mark_seen? → put(Item)       │
    │             ├──────────────────────────────────────────────────────────────────────────►│
    │             │                                                                           │
    │  task_done()│                                                                           │
    │◄────────────┤                                                                           │
    │             │                                                                           │
```

---

## 5. Dedup logic: where and how

Dedup uses one canonical string per URL and one shared set. `_mark_seen`
does both the check and the add under `_seen_lock`.

```
                       ┌────────────────┐
    raw href ────────► │  normalize_url │──── canonical string
    (relative, mixed   │   (fragment    │
     case, default     │    drop, port  │
     ports, ...)       │    drop, sort  │
                       │    query,      │
                       │    lowercase)  │
                       └───────┬────────┘
                               │
                               ▼
                     ┌─────────────────────┐
                     │ Crawler._mark_seen  │  under _seen_lock:
                     │  (check-and-add)    │    if canonical in _seen: skip
                     └─────────┬───────────┘    if |_seen| >= max_pages: STOP
                               │                add(canonical); return True
                               ▼
                          Frontier.put(Item)
```

`_mark_seen` is called at two moments:

1. **At enqueue time.** Before `frontier.put`, for every discovered link
   and every seed.
2. **After a redirect.** If `final_canonical != request_canonical`, the
   worker calls `_mark_seen(final_canonical)`. If the target is already in
   `_seen`, another path recorded it first, and the worker drops this
   response.

Those two checks cover four cases:

- The same URL is enqueued twice. The second attempt fails `_seen`.
- Two URLs differ only in fragment, query ordering, or case. Canonicalization
  collapses them.
- A redirect chain lands on a URL that another worker already fetched. The
  redirected path drops its response.
- Two distinct canonical URLs redirect to the same target X. The first
  finisher records X. The second returns after `_mark_seen(X)` fails.

---

## 6. URL state machine

A URL moves through three states: `QUEUED`, `IN_FLIGHT`, and `DONE`. The
diagram lists the four ways `IN_FLIGHT` can end and the one path back to
`QUEUED`.

```
                       enqueue()
                          │
                          ▼
                    ┌───────────┐
                    │  QUEUED   │  (in Frontier queue, counted in in_flight)
                    └─────┬─────┘
                          │ worker.get()
                          ▼
                    ┌───────────┐
                    │ IN_FLIGHT │  (checked out, being processed)
                    └─────┬─────┘
        ┌─────────────────┼─────────────────┬─────────────────┐
        │                 │                 │                 │
        ▼                 ▼                 ▼                 ▼
  robots.disallow   fetch error /      2xx + parseable    429/503 with
    OR filtered       4xx/5xx            HTML body         Retry-After
        │                 │                 │              (attempts < max)
        │                 │                 │                 │
        ▼                 ▼                 ▼                 ▼
    ┌───────┐         ┌───────┐         ┌───────────┐    ┌─────────────┐
    │  DONE │         │  DONE │         │   DONE    │    │ RE-ENQUEUED │
    │(skip) │         │(error │         │(recorded, │    │ (attempts+1)│
    │       │         │ noted)│         │ children  │    │             │
    │       │         │       │         │ enqueued) │    │             │
    └───┬───┘         └───┬───┘         └─────┬─────┘    └──────┬──────┘
        │                 │                   │                 │
        └─────────────────┴──────┬────────────┘                 │
                                 │                              │
                                 ▼                              │
                          task_done()                           │
                          in_flight--                           │
                          if 0: quiescent                       │
                                                                │
                                          ────────► back to QUEUED
```

`put` increments `in_flight`. `task_done` decrements it. The counter
equals the queue length plus the number of workers currently in
`_process`. While either is non-zero, the crawl is live.

---

## 7. Termination flow

The main thread blocks on the frontier's `done` Event. Workers `put` and
`task_done` on every item. When `in_flight` reaches zero, the frontier sets
the event, the main thread flips `stop_event`, and the workers drop out of
their poll loop on the next `get` timeout.

```
Main thread                     Workers                  Frontier
     │                              │                        │
     │ enqueue seeds ───────────────┼───────────────────────►│  in_flight = S
     │                              │                        │
     │ frontier.wait_done() ────────┼──────────────► block on Event
     │                              │                        │
     │                        ┌─────┴─────┐                  │
     │                        │  process  │                  │
     │                        │  → put(k) │───► in_flight += k
     │                        │  → task_done ─► in_flight -= 1
     │                        └─────┬─────┘                  │
     │                              │  (repeat)              │
     │                              │                        │
     │                              │            when in_flight == 0:
     │                              │              Event.set()
     │                              │                        │
     │◄─────────────────────────────┼── wait_done returns    │
     │                              │                        │
     │ stop_event.set()             │                        │
     │                              │                        │
     │                        workers exit their poll loop   │
     │                              │                        │
     │  executor.shutdown (join)    │                        │
     ▼                              ▼                        ▼
```

Two more exit paths share the same latch:

- **`max_pages` reached.** `_mark_seen` calls `frontier.trigger_stop()`,
  which sets the event directly. Workers finish their current item and
  exit. Any `put()` after `trigger_stop` is a no-op.
- **Worker exception.** The exception is caught in `_worker_loop` and
  counted in `worker_exceptions`. The worker keeps running. Exceptions
  never propagate out of a worker.

---

## 8. Failure modes and how they're handled

Each row is one failure with the code that catches it and the counter that
records it. Every path ends in a `DONE` URL or a re-enqueue. Nothing leaks.

| Failure | Handling |
| --- | --- |
| Timeout or connection error | `FetchResult(status=0, error=...)`. Counted in `network_errors`. URL is `DONE`. |
| Non-HTML content-type | The body is never downloaded. The page is recorded with empty links. |
| Oversized body (`Content-Length` or streamed size over cap) | The fetcher bails during the stream. The `with resp:` block closes the socket. |
| Malformed HTML | `HTMLParser.feed` runs in a try/except. The parser returns whatever links it got. |
| Uncaught worker exception | Caught at the top of `_worker_loop`. Counted in `worker_exceptions`. The worker keeps going. |
| robots.txt 5xx or network error | Fail closed for that host: `Disallow: /`. |
| robots.txt 4xx | Fail open: allow all. |
| `Crawl-delay` smaller than the default | The rate limiter uses `max(default, override)`, so the default wins. |
| Redirect to an already-seen URL | `_mark_seen(final)` returns `False`. The response is dropped without recording. |
| Redirect to a different origin, in same-origin mode | Rejected after redirect. |
| Crawler trap (`/loop?p=1 → /loop?p=2 → ...`) | Depth cap and `max_pages` cap. Each canonical URL is fetched at most once. |

---

## 9. Observability contract

Every run produces two things:

- **Logs.** `INFO` on start and stop. `WARNING` on unparseable seeds.
  `DEBUG` on fetch errors and robots errors.
- **A metrics snapshot.** The field names in `CrawlResult.metrics` (see
  §3.4) match the alert names in
  [README §Observability](./README.md#observability).

Production would also export three things not in this repo:

- Fetch latency histograms, per host and global.
- A gauge for `frontier_size` and one for `in_flight`.
- The alerts listed in [README §Observability](./README.md#observability).

---

## 10. Test matrix

| Layer | Test | Purpose |
| --- | --- | --- |
| unit | `test_normalizer` | Scheme and host case, default ports, fragment, query order, relative URLs, junk input. |
| unit | `test_rate_limiter` | Spacing, per-host independence, and override precedence, on a fake clock. No real sleep. |
| unit | `test_frontier` | Quiescence with mid-processing enqueues, stop wakes a waiter, put after stop is a no-op. |
| unit | `test_parser` | Link extraction, `rel="nofollow"`, tolerance of broken HTML. |
| integration | `test_crawler` | End-to-end against a local `http.server`: same-origin, robots, redirect fold, page cap, oversized body. |

Two tests cover the concurrent paths. `test_frontier` pins the in-flight
invariant, which is the one load-bearing correctness property.
`test_crawler` runs the whole crawler with `max_workers=4`.

---

## 11. What breaks first at 10x, 100x, 1000x scale

| Scale | First bottleneck | Fix |
| --- | --- | --- |
| 10x (10^5 URLs) | Nothing structural. Only runtime. | Raise `max_workers`. Tune `per_host_delay` per target. |
| 100x (10^6 URLs) | `_seen: set[str]` uses ~130 MB. `_results` grows large. | Move `_results` to a streaming JSONL writer on disk. |
| 1000x (10^7+ URLs) | `_seen` no longer fits comfortably. The frontier is in-memory, so a restart loses the run. Per-host politeness is process-local. | Back `_seen` with a bloom filter plus an on-disk exact set (sqlite or rocksdb). Move the frontier to a durable queue (SQS or Redis). Consistent-hash by host across N crawler nodes so per-host politeness stays correct. |

Past a single machine, the load-bearing question is *who owns a host*.
Per-host politeness only holds when every request to `example.com` is
serialized through one node. Consistent hashing on the canonical host name
gives one owner per host with no coordination.

---

## 12. Where `asyncio` beats threads, and where it doesn't

Threads win here for three reasons:

- Network I/O dominates the workload. A blocking `recv()` costs almost
  nothing.
- Every dependency in this repo is synchronous: `requests`, stdlib
  `RobotFileParser`, `HTMLParser`. Calling any of them from an async
  function without a thread pool blocks the event loop.
- Threaded code with per-component locks is straightforward to debug and
  test.

`asyncio` starts to pay off past ~10,000 concurrent in-flight requests per
process, where threads run out of stack RAM. The cost is moving the whole
stack to async: `aiohttp` for HTTP, an async robots parser, and a thread
pool for CPU-bound parsing anyway. For a single-machine crawler up to
~10^6 URLs, threads are the simpler correct choice.
