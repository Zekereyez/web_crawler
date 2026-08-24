# Guide: how we arrived at this solution

A walkthrough for a new engineer picking up the project, or someone prepping
for the same interview question. It builds the solution the way you'd build
it at a whiteboard: naive first, then one problem at a time.

If you just want the shipped design, read [`DESIGN.md`](./DESIGN.md). If you
want to understand *why the design is shaped the way it is*, read this doc
first.

---

## The problem, in one sentence

> Design and implement a concurrent web crawler using a thread pool. It
> should be correct, robust, observable, and reasonably simple.

That's it. Everything else (depth caps, robots, politeness, dedup) is a
consequence of taking that sentence seriously.

---

## Step 0. Before writing any code, ask questions

Interviewers score you on *scoping*, not just coding. The exercise gives a
long list of hints ("Same-origin matching", "Why threads here",
"Normalization is the lever", and so on). Don't skip past them; they're
the requirements in disguise.

Questions worth asking out loud:

1. **What are we crawling?** A single site (same-origin), or the open web
   from arbitrary seeds?
2. **How big?** Rough page count and wall-clock budget?
3. **How polite?** Must we honor `robots.txt`? What per-host default?
4. **What does "done" mean?** All seeds exhausted, or a page/byte/time cap?
5. **Persistence?** One-shot in-memory, or must the frontier survive a
   crash?
6. **Output format?** Link graph? Raw HTML? Metadata?
7. **Environment?** Single process today, distributed later? Libraries
   allowed?

The answers set defaults for your `CrawlerConfig`. Even if the interviewer
says "use your judgment," picking a default *and stating why* is worth
points. In this project we picked: same-origin optional, ~10^6 URLs single
process, honor robots by default, 1s per-host, in-memory, emit a link graph
plus metrics, `requests` allowed.

---

## Step 1. Why threads, not asyncio, not multiprocessing

Establish this before you start typing. The interviewer wants to hear you
reason about the workload.

- A single fetch spends over 99% of its wall clock in `recv()` waiting for
  bytes. That's I/O-bound.
- CPU work per page (HTML parsing, URL normalization) is small.
- Multiprocessing is for CPU-bound work. Wrong tool. Adds IPC cost.
- Asyncio wins when you need tens of thousands of concurrent sockets in one
  process, but it demands async everything: HTTP, robots, DNS. Mixing sync
  (`requests`, stdlib `RobotFileParser`) into async defeats the point.
- Threads block cheaply on I/O and let you use the sync ecosystem. For 16 to
  64 workers on one box handling 10^4 to 10^6 URLs, threads are the simplest
  correct choice.

Rule of thumb: threads until you outgrow them (~10k concurrent in-flight
requests per process), then rethink.

---

## Step 2. The naive sketch

Before adding anything, write down the shape you're heading toward:

```python
def crawl(seed):
    seen = {seed}
    q = [seed]
    while q:
        url = q.pop()
        html = requests.get(url).text
        for link in extract_links(html):
            if link not in seen:
                seen.add(link)
                q.append(link)
```

This is wrong in about eight ways. Each way is a real requirement.

1. Serial, so slow. Need concurrency.
2. `seen` uses raw URLs, so `HTTP://X/` and `http://x` are two different
   entries. Need normalization.
3. Hammers every host. Need per-host politeness.
4. Ignores robots. Need `robots.txt`.
5. Any exception kills the crawl. Need robustness.
6. Follows any link forever. Need depth and page caps.
7. Doesn't handle redirects, so the same page is fetched multiple times.
   Need post-redirect dedup.
8. In a thread pool, "queue empty" doesn't mean "done." A worker might be
   mid-fetch, about to enqueue children. Need quiescence detection.

We'll knock these down in order.

---

## Step 3. Normalization is the lever

If you dedup on raw URL strings, you'll fetch the same page four times
because it's linked as `/a`, `/a#top`, `/a?b=2&a=1`, and `/a?a=1&b=2`.

The trick: define one canonical string per logical URL, and dedup only on
that. Everything downstream uses it as a key.

Canonicalization rules (see [`crawler/normalizer.py`](./crawler/normalizer.py)):

- Resolve relative hrefs against the source page.
- Lowercase the scheme and host.
- Drop the default port (`:80` on http, `:443` on https).
- Drop the fragment (`#anchor` is a browser concern, not a URL).
- Empty path becomes `/`.
- Sort query params so `?b=2&a=1` and `?a=1&b=2` collide.

Why this is the lever: get canonicalization right and your dedup is
trivially a `set.add`. Get it wrong and no amount of clever locking saves
you.

Reject non-`http(s)` schemes early. `mailto:`, `javascript:`, and `data:`
are not crawlable.

---

## Step 4. The concurrency layer

Now we add threads. Two shapes.

**A. `ThreadPoolExecutor.submit` per URL.** Every discovered link becomes a
`Future`. Simple, but you lose control: you can't cap total in-flight work,
and you have no natural place to implement per-host politeness or
termination.

**B. A shared frontier queue with N long-lived workers.** Workers pull,
process, enqueue children, and repeat. Bounded workers, one central place
for cross-cutting concerns.

We picked B. It's the standard shape for a crawler; every real-world crawler
ends up here for the same reasons.

```python
def worker_loop():
    while not stop_event.is_set():
        item = frontier.get(timeout=0.25)
        if item is None:
            continue
        try:
            process(item)
        finally:
            frontier.task_done()
```

The 250 ms poll is deliberate: workers wake regularly to notice a stop
signal without busy-spinning.

---

## Step 5. The subtle bug: when are we done?

This is the trap the interview question keeps flagging. New engineers
write:

```python
while not queue.empty():  # WRONG
    ...
```

Consider this sequence with 2 workers:

```
t=0   queue: [A]                  worker1 gets A
t=1   queue: []                   worker2 sees empty queue and EXITS
t=2   worker1 finishes A, enqueues B, C
t=3   queue: [B, C]  ... but only worker1 is left. Or worse: the
      main thread called `queue.empty()` at t=1 and decided we're done.
```

Fix: track two numbers.

- `in_flight` = (items currently in queue) + (items checked out by workers).
- We're done when `in_flight == 0` and no worker is holding an item.

Simpler mental model: one counter that goes up on `put` and down on
`task_done`. Both moves are under a lock. When it hits zero, a `done` Event
fires, and the main thread (which was blocking on that event) wakes up and
shuts down the workers.

Everything else in `Frontier` is bookkeeping around this idea:

```python
def put(item):
    with lock:
        in_flight += 1
    queue.put(item)

def task_done():
    with lock:
        in_flight -= 1
        if in_flight == 0:
            done.set()
```

Test this invariant explicitly. See
[`tests/test_frontier.py::test_quiescence_survives_child_enqueue`](./tests/test_frontier.py).
It simulates the exact race: `get()` an item, `put()` its children *before*
`task_done()`, then verify `wait_done()` does NOT fire.

---

## Step 6. Dedup at two moments (the redirect gotcha)

Naive intuition: dedup at enqueue time. Add to `seen` before adding to the
queue.

That's correct, but incomplete. Consider:

```
Seed A: 301 to B
Seed C: 200, links to B directly
```

If both seeds get enqueued, both go through the "not in `seen`" check and
get queued. When worker fetches A, it lands on B. When worker fetches C, C
links to B, but B might already be seen (from A's redirect) or might not
(if C finished first).

Rule: dedup on the canonical form of the *final* URL after the fetch. If
the final URL differs from what we asked for, `_mark_seen(final)`. If it's
already there, drop the response silently.

```python
final = normalize(response.url)
if final != url:
    if not mark_seen(final):
        return  # someone else already recorded this
```

One `set` under one lock, checked at two moments: enqueue and
post-redirect. This handles all four dedup cases (see DESIGN §5).

---

## Step 7. Politeness: robots and rate limiting

Two concerns that people often collapse into one. Keep them separate.

### 7a. `robots.txt`

- One fetch per host per crawl. Cache it.
- Under fan-in (16 workers hitting the same new host), only one should
  fetch robots; the rest wait. Per-host lock, not a global lock. Fetching
  robots for `foo.com` shouldn't block workers on `bar.com`.
- Failure modes:
  - `2xx`: parse and honor.
  - `4xx` (especially 404): allow everything. Standard practice.
  - `5xx` or network error: disallow everything for that host. This is
    fail-closed. A broken robots endpoint should not become a hammering
    situation. It's a defensible default; some crawlers pick fail-open
    instead. State your choice.
- `Crawl-delay` from robots is treated as an *upgrade* to your default
  spacing, never a downgrade. `effective_delay = max(default, robots)`.
  Why: robots states the minimum politeness required; your default states
  the minimum you promised your ops team.

### 7b. Per-host rate limiting

Design goals:
- At most one request per host per `delay` seconds.
- A slow host should not block requests to other hosts.

The trick: compute the next allowed time under a lock, then sleep outside
it.

```python
def acquire(host):
    with lock:                                      # brief critical section
        now = time.monotonic()
        wait = max(0, next_time.get(host, 0) - now)
        next_time[host] = max(now, next_time[host]) + delay
    if wait > 0:
        time.sleep(wait)                            # sleep is OUTSIDE the lock
```

If you sleep under the lock, one slow host stops everyone. Get this detail
right and per-host politeness works cleanly across N workers.

Inject the clock. Real `time.sleep()` in tests is slow and flaky. The rate
limiter takes `time_fn` and `sleep_fn` so tests use a fake clock. See
[`tests/test_rate_limiter.py`](./tests/test_rate_limiter.py).

### 7c. `Retry-After` (429 and 503)

Servers throttle. Don't fight them.

- Parse `Retry-After`, and cap it (some servers send absurd values).
- Sleep in the worker, then re-enqueue with `attempts+1`, bounded by
  `max_retries`.
- Alternative: a dedicated delay-scheduler thread with a heap. Better at
  scale, unnecessary here.

---

## Step 8. Robustness: the boring things that decide whether it ships

The exercise flags "don't leak sockets" and "one bad page must not kill a
worker." Boring, but load-bearing.

### 8a. Content-type filter *before* download

Streaming lets us look at headers before pulling the body:

```python
with session.get(url, stream=True, timeout=T) as resp:
    if not resp.headers["Content-Type"].startswith("text/html"):
        return  # body never leaves the wire; the `with` closes the socket
    for chunk in resp.iter_content(...):
        ...
```

Without `stream=True`, `requests` downloads the whole body before you can
see the type. On a 100 MB PDF, that's disastrous.

### 8b. Byte cap

Check `Content-Length` first. If missing (chunked), track bytes during
streaming and bail if you exceed `max_bytes`. Bailing closes the socket;
that's exactly right. The alternative is a 10 GB stream from a hostile
server.

### 8c. Sockets close

Always. Every `session.get(..., stream=True)` lives inside `with resp:`.
An unclosed streaming response leaks a connection back to the pool and
eventually you run out.

### 8d. Worker never dies

```python
def worker_loop():
    while not stop_event.is_set():
        item = frontier.get(timeout=0.25)
        if item is None:
            continue
        try:
            process(item)
        except Exception:
            logger.exception("worker error")
            metrics.incr("worker_exceptions")
        finally:
            frontier.task_done()
```

The `try` is *at the top of the loop*, wrapping the whole page. A single
malformed page must never bring down its worker. Metrics count how often it
happens so you notice.

### 8e. Bound the frontier

- `max_depth`: hop distance from the seed.
- `max_pages`: global cap on distinct URLs. Prevents runaway on a crawler
  trap that generates infinite URLs (`/?p=1`, `/?p=2`, ...).
  Canonicalization already collapses duplicates; the cap is belt-and-braces
  for genuinely-distinct-but-useless URL spaces.

---

## Step 9. Observability

If you can't measure it, you don't know if it works.

Minimum I would ship with:

- **Counters.** `fetch_attempts`, `pages_fetched`, `network_errors`,
  `http_4xx`, `http_5xx`, `http_429`, `http_503`, `robots_disallowed`,
  `redirects`, `worker_exceptions`, `elapsed_s`.
- **Logs.** INFO on start and stop; WARNING on unparseable seeds; DEBUG on
  fetch and robots errors (noisy, off by default).
- **End-of-run summary.** JSON dump of pages plus metrics for downstream
  tooling.

Production would additionally export latency histograms and gauges for
`frontier_size` and `in_flight`. Alerts I'd wire:

- `network_errors / fetch_attempts > 5%` for 5 min: sustained issues.
- `worker_exceptions > 0`: bug or unexpected input; page a human.
- `http_429 + http_503` spike on one host: we're being throttled; raise
  per-host delay.

State the metrics before writing code that uses them. It's easier to build
in the right hooks than retrofit them.

---

## Step 10. Testability by design

Two habits that pay for themselves within an hour.

**Inject time.** `HostRateLimiter(time_fn=..., sleep_fn=...)`. Tests run in
milliseconds with a fake clock. No `time.sleep(1)` in your test suite.

**Local HTTP fixture.** `test_crawler.py` spins up a `ThreadingHTTPServer`
on `127.0.0.1:0` that serves a handful of pages, a `robots.txt`, a
redirect, and an oversized body. All end-to-end concurrency, robots,
redirects, and content-type/size behavior tested without a network.

The test matrix (see DESIGN §10):

| Layer | Purpose |
| --- | --- |
| unit, normalizer | canonicalization corner cases |
| unit, rate limiter | spacing, per-host independence, on a fake clock |
| unit, frontier | the in-flight invariant (this is the subtle one) |
| unit, parser | malformed HTML tolerance, nofollow |
| integration, crawler | same-origin, robots, redirect fold, page cap, oversized body |

If you only have time for one test file, write `test_frontier.py`. The
quiescence invariant is the load-bearing correctness property and the
easiest to break.

---

## Step 11. Putting it together

By this point the components are inevitable.

- **`Normalizer`.** The canonical-URL machine.
- **`Frontier`.** Queue, in-flight counter, done Event.
- **`RobotsCache`.** Per-host memo with per-host lock.
- **`HostRateLimiter`.** Per-host next-allowed-time; sleep outside the lock.
- **`Fetcher`.** Streaming GET, content-type and byte cap, Retry-After.
- **`LinkExtractor`.** stdlib HTMLParser, nofollow-aware.
- **`Crawler`.** Orchestrator: enqueue-time and post-redirect dedup, worker
  loop, catch-all exception handling.
- **`Metrics`.** Thread-safe counter bag.

One file each. One responsibility each. Locks live inside the component
that owns the state; components talk only through their public methods.
See [`DESIGN.md`](./DESIGN.md) for schemas, diagrams, and the sequence of a
single request.

---

## Common questions from new engineers on this codebase

**Q. Why not `queue.Queue.join()` for termination?**
`join()` waits for `task_done()` to match `put()`, which is the same idea
as our counter. We rolled our own so we could *also* trigger stop
externally (page cap, kill switch) with the same latch. If we only needed
quiescence, `queue.join()` would be fine.

**Q. Why is `_seen` a set and not a bloom filter?**
At 10^6 URLs, ~130 MB. Tight but fits. Beyond that, swap in a bloom filter
(fast, false-positive-only, memory-efficient) backed by an on-disk exact
set for correctness. Flagged in
[README §Known limitations](./README.md#known-limitations-and-where-theyd-break-at-scale).

**Q. Why fail-closed on robots errors instead of fail-open?**
A broken host that intermittently 500s on `robots.txt` should not become a
hammering situation. Fail-closed is safer at the cost of under-crawling.
Reasonable people disagree; state your choice, don't default silently.

**Q. Why does `Retry-After` sleep block a worker?**
Simplicity. At 16 workers, losing one for a few seconds is fine. At 1000
workers, a dedicated delay-scheduler thread with a heap of
`(wake_at, item)` pairs is strictly better. Move to it when it starts
mattering.

**Q. Where would this design break first when I 10x it?**
The `_seen` set and the in-memory `_results` dict. See DESIGN §11.

**Q. How would you make this distributed?**
Consistent-hash by host across N crawler nodes so per-host politeness
stays correct (one node owns `example.com`; no coordination needed for
rate limiting). Move the frontier to a durable queue (SQS, Redis Streams).
`_seen` becomes a shared bloom filter plus a partitioned KV store. The
single-node code changes very little; the interfaces stay the same, the
storage backends swap.

---

## What to read next

1. [`DESIGN.md`](./DESIGN.md). The reference doc: architecture, schemas,
   sequence diagrams, state machines.
2. [`crawler/crawler.py`](./crawler/crawler.py). The orchestrator; the
   whole thing is under 200 lines.
3. [`tests/test_frontier.py`](./tests/test_frontier.py). The tests for the
   load-bearing invariant.
4. [`tests/test_crawler.py`](./tests/test_crawler.py). The end-to-end test
   to see the pieces working together.
