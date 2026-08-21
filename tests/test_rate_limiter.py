import unittest

from crawler.rate_limiter import HostRateLimiter


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.slept = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class TestRateLimiter(unittest.TestCase):
    def test_first_call_does_not_sleep(self):
        clock = FakeClock()
        rl = HostRateLimiter(default_delay=1.0, time_fn=clock.time, sleep_fn=clock.sleep)
        rl.acquire("example.com")
        self.assertEqual(clock.slept, [])

    def test_second_call_sleeps_delay(self):
        clock = FakeClock()
        rl = HostRateLimiter(default_delay=1.0, time_fn=clock.time, sleep_fn=clock.sleep)
        rl.acquire("example.com")
        rl.acquire("example.com")
        self.assertEqual(clock.slept, [1.0])

    def test_different_hosts_independent(self):
        clock = FakeClock()
        rl = HostRateLimiter(default_delay=1.0, time_fn=clock.time, sleep_fn=clock.sleep)
        rl.acquire("a.com")
        rl.acquire("b.com")
        self.assertEqual(clock.slept, [])

    def test_crawl_delay_override_uses_higher(self):
        clock = FakeClock()
        rl = HostRateLimiter(default_delay=1.0, time_fn=clock.time, sleep_fn=clock.sleep)
        rl.set_delay("slow.com", 5.0)
        rl.acquire("slow.com")
        rl.acquire("slow.com")
        self.assertEqual(clock.slept, [5.0])

    def test_override_smaller_ignored(self):
        # A robots.txt Crawl-delay smaller than our default should not weaken
        # our politeness.
        clock = FakeClock()
        rl = HostRateLimiter(default_delay=2.0, time_fn=clock.time, sleep_fn=clock.sleep)
        rl.set_delay("x.com", 0.1)
        rl.acquire("x.com")
        rl.acquire("x.com")
        self.assertEqual(clock.slept, [2.0])

    def test_wait_shrinks_after_natural_gap(self):
        clock = FakeClock()
        rl = HostRateLimiter(default_delay=1.0, time_fn=clock.time, sleep_fn=clock.sleep)
        rl.acquire("x.com")
        clock.now += 2.0  # more than delay elapsed on its own
        rl.acquire("x.com")
        self.assertEqual(clock.slept, [])


if __name__ == "__main__":
    unittest.main()
