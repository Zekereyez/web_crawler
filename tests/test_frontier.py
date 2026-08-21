import threading
import time
import unittest

from crawler.frontier import Frontier, Item


class TestFrontier(unittest.TestCase):
    def test_wait_done_blocks_until_task_done(self):
        f = Frontier()
        f.put(Item("http://a/", 0))
        # Not done yet.
        self.assertFalse(f.wait_done(timeout=0.05))

        got = f.get(timeout=0.1)
        self.assertIsNotNone(got)
        # Still not done — the item is checked out.
        self.assertFalse(f.wait_done(timeout=0.05))

        f.task_done()
        self.assertTrue(f.wait_done(timeout=0.5))

    def test_quiescence_survives_child_enqueue(self):
        """Wait_done must not fire between get() and put(children)."""
        f = Frontier()
        f.put(Item("http://a/", 0))
        got = f.get(timeout=0.1)
        # simulate: worker enqueues children before task_done
        f.put(Item("http://a/b", 1))
        f.put(Item("http://a/c", 1))
        f.task_done()
        # in_flight should be 2 now, not 0
        self.assertFalse(f.wait_done(timeout=0.05))
        f.get(timeout=0.1); f.task_done()
        self.assertFalse(f.wait_done(timeout=0.05))
        f.get(timeout=0.1); f.task_done()
        self.assertTrue(f.wait_done(timeout=0.5))

    def test_trigger_stop_wakes_waiter(self):
        f = Frontier()
        f.put(Item("http://a/", 0))

        def _stop_soon():
            time.sleep(0.05)
            f.trigger_stop()

        threading.Thread(target=_stop_soon, daemon=True).start()
        self.assertTrue(f.wait_done(timeout=1.0))

    def test_put_after_stop_is_noop(self):
        f = Frontier()
        f.trigger_stop()
        f.put(Item("http://a/", 0))
        self.assertIsNone(f.get(timeout=0.05))


if __name__ == "__main__":
    unittest.main()
