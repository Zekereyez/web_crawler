# Design doc: concurrent web crawler

Companion to [README.md](./README.md). This document covers the architecture,
the data schemas, and the flow of a single URL through the system.

---

## 1. Goals and non-goals

**Goals**
- Fetch many URLs concurrently on one machine, up to ~10^6 unique URLs per
  run.
- Never crash on a bad page or a bad host.
- Be a good citizen: robots.txt, per-host rate limiting, Retry-After.
- Terminate deterministically. No "queue looks empty, we must be done."
- Be testable and observable end to end.

**Non-goals** (called out to bound scope)
- Distributed crawling across machines.
- Persistent frontier or crash recovery mid-run.
- JavaScript rendering. We parse static HTML only.
- Indexing or full-text search. We emit a link graph plus status; downstream
  consumers do what they want with it.

---

## 2. Architecture

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

Each component is one file, one responsibility. Locking is local to the
component that owns the state; components communicate only through their
public methods.

---

## 3. Data schemas

### 3.1 `CrawlerConfig`, the immutable run config

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
    max_retries: int              # 429/503 retries per URL
    user_agent: str
    allowed_content_types: tuple  # e.g. ("text/html", "application/xhtml+xml")
)
```

### 3.2 `Item`, the frontier element

```python
Item(
    url: str          # already-canonicalized (dedup key)
    depth: int        # 0 for seed
    attempts: int     # incremented on 429/503 retry
)
```

### 3.3 `FetchResult`, the fetcher output

```python
FetchResult(
    final_url: str            # post-redirect URL from `requests`
    status: int               # 0 = network error, else HTTP status
    content_type: str         # lowercased, ";..." stripped
    body: Optional[str]       # None if filtered, oversized, or non-HTML
    retry_after: Optional[float]
    error: Optional[str]      # short reason on failure
)
```

### 3.4 `CrawlResult`, the end-of-run summary

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

### 3.5 Internal shared state

| State | Owner | Guard | Purpose |
| --- | --- | --- | --- |
| `_seen: set[str]` | `Crawler` | `_seen_lock` | canonical URL dedup |
| `_results: dict` | `Crawler` | `_results_lock` | record per URL |
| queue + `in_flight: int` | `Frontier` | `queue.Queue` lock + `_lock` | frontier + quiescence |
| `_next_time: dict[host, float]` | `HostRateLimiter` | `_lock` | per-host next-allowed |
| `_delay_overrides: dict[host, float]` | `HostRateLimiter` | `_lock` | crawl-delay upgrades |
| `_cache: dict[host_key, (Parser, delay)]` | `RobotsCache` | `_cache_lock` | robots memoization |
| `_host_locks: dict[host_key, Lock]` | `RobotsCache` | `_host_locks_lock` | prevent thundering herd |

---

## 4. Sequence flow: one URL through the system

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

There is exactly one canonical URL string per URL, and one set to dedup on:

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

Two moments call `_mark_seen`:

1. **Enqueue time.** Before `frontier.put` for every discovered link and
   every seed.
2. **After a fetch that redirected.** If `final_canonical != request_canonical`,
   we `_mark_seen(final_canonical)`. If it's already in `_seen`, the redirect
   target was fetched by another route, so we drop this response without
   recording it.

This handles all four dedup cases:
- Same URL enqueued twice. The 2nd fails the `_seen` check.
- Two URLs that differ only in fragment, ordering, or case. They collapse via
  canonicalization.
- Redirect chain lands on an already-seen URL. The 2nd path drops the
  response.
- Two URLs whose canonical forms both redirect to X. Whichever finishes first
  records X; the other returns after `_mark_seen(X)` fails.

---

## 6. URL state machine

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

`in_flight` is incremented on `put` and decremented on `task_done`. It
counts (queue length) + (workers currently in `_process`), so as long as
either is non-zero, the crawl is still live.

---

## 7. Termination flow

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

Two additional exit paths use the same latch:

- **`max_pages` reached.** `_mark_seen` calls `frontier.trigger_stop()`,
  which sets the Event directly. Workers finish their current item and exit.
  `put()` after `trigger_stop` is a no-op.
- **Worker exception.** Caught in `_worker_loop` and counted in metrics; the
  worker keeps running. Exceptions never propagate.

---

## 8. Failure modes and how they're handled

| Failure | Handling |
| --- | --- |
| Timeout / connection error | `FetchResult(status=0, error=...)`; metric `network_errors`; URL is DONE. |
| Non-HTML content-type | Body not downloaded; page recorded with empty links. |
| Oversized body (`Content-Length` or streamed size > cap) | Bail during stream; connection is closed via `with resp:`; no leak. |
| Malformed HTML | `HTMLParser.feed` in try/except; we return whatever links we got. |
| Bad worker exception | Caught at loop top; metric `worker_exceptions`; worker keeps going. |
| robots.txt 5xx / network error | Fail closed for that host (Disallow: /). |
| robots.txt 4xx | Fail open (allow all). |
| `Crawl-delay` smaller than default | `max(default, override)`, so the default wins. |
| Redirect to an already-seen URL | `_mark_seen(final)` returns False; response dropped without recording. |
| Redirect to a different origin (same-origin mode) | Rejected after redirect. |
| Crawler trap (`/loop?p=1 → /loop?p=2 → ...`) | Depth cap plus `max_pages` cap; each canonical URL is fetched at most once. |

---

## 9. Observability contract

Every run produces:

- **Logs.** INFO on start and stop, WARNING on unparseable seeds, DEBUG on
  fetch and robots errors.
- **Metrics snapshot.** See `CrawlResult.metrics` in §3.4.

In production I would additionally export:
- Fetch latency histograms, per-host and global.
- A gauge for `frontier_size` and `in_flight`.
- Alerts (see [README §Observability](./README.md#observability)).

---

## 10. Test matrix

| Layer | Test | Purpose |
| --- | --- | --- |
| unit | `test_normalizer` | scheme/host case, default ports, fragment, query order, relative, junk |
| unit | `test_rate_limiter` | spacing, per-host independence, override precedence, on a fake clock (no real sleep) |
| unit | `test_frontier` | quiescence with mid-processing enqueues, stop-wakes-waiter, put-after-stop |
| unit | `test_parser` | link extraction, nofollow, broken HTML tolerance |
| integration | `test_crawler` | end-to-end against local `http.server`: same-origin, robots, redirect fold, page cap, oversized body |

Concurrency-specific properties are covered by `test_frontier` (in-flight
tracking is the load-bearing invariant) and by the end-to-end test running
with `max_workers=4`.

---

## 11. What breaks first at 10x, 100x, 1000x scale

| Scale | First bottleneck | Fix |
| --- | --- | --- |
| 10x (10^5 URLs) | Nothing structural, just runtime. | Bump `max_workers`, tune `per_host_delay` per target. |
| 100x (10^6 URLs) | `_seen: set[str]` RAM (~130 MB), `_results` grows large. | Move `_results` to a streaming writer (JSONL to disk). |
| 1000x (10^7+ URLs) | `_seen` no longer fits comfortably; the frontier is in-memory (no restart recovery); per-host politeness is process-local. | Bloom filter plus an on-disk exact set (sqlite or rocksdb); durable frontier (SQS or Redis); consistent-hash by host across N crawler nodes so per-host politeness stays correct. |

Past a single machine, the interesting design question is *who owns a host*.
Per-host politeness only holds if all requests to `example.com` are
serialized through one node. Consistent hashing on the canonical host solves
it cleanly.

---

## 12. Where `asyncio` beats threads, and where it doesn't

Threads win here because:
- The workload is dominated by network I/O, and blocking `recv()` is cheap.
- Every dependency (`requests`, stdlib `RobotFileParser`, `HTMLParser`) is
  synchronous. Mixing sync-in-async without pooling defeats the point.
- Debugging and testing threaded code with proper locks is well understood.

`asyncio` starts to pay off when you want tens of thousands of concurrent
in-flight requests per process (threads run out of stack RAM around 10k) and
you're willing to move the whole stack to async (`aiohttp`, an async robots
implementation, an executor for CPU-bound parsing). For a single-machine
crawler up to ~10^6 URLs, threads are the simpler correct choice.
