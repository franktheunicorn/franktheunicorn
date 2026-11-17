"""The cycle's review runners: ordering, the front of the queue, and shutdown."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

from franktheunicorn.worker.review_pool import ReviewItem, ReviewPool


def _item(number: int) -> ReviewItem:
    pr: Any = SimpleNamespace(number=number)
    pc: Any = SimpleNamespace(full_name="apache/spark")
    return ReviewItem(pr, pc, None, None)


class TestInline:
    def test_size_one_runs_on_the_submitting_thread(self) -> None:
        seen: list[tuple[int, str]] = []
        pool = ReviewPool(1, lambda i: seen.append((i.pr.number, threading.current_thread().name)))
        with pool:
            pool.start()
            pool.submit(_item(1))
            pool.submit(_item(2), front=True)
            assert pool.pending() == 0
        me = threading.current_thread().name
        assert seen == [(1, me), (2, me)]

    def test_wait_idle_still_ticks_once(self) -> None:
        ticks = []
        pool = ReviewPool(1, lambda i: None)
        pool.wait_idle(lambda: ticks.append(1), interval=0.01)
        assert ticks == [1]


class TestThreaded:
    def _gated_pool(self, size: int) -> tuple[ReviewPool, threading.Event, list[int]]:
        gate = threading.Event()
        done: list[int] = []
        lock = threading.Lock()

        def work(item: ReviewItem) -> None:
            gate.wait(5)
            with lock:
                done.append(item.pr.number)

        pool = ReviewPool(size, work)
        pool.start()
        return pool, gate, done

    def test_runs_concurrently_and_waits_for_all(self) -> None:
        size = 4
        barrier = threading.Barrier(size, timeout=5)
        pool = ReviewPool(size, lambda i: barrier.wait())
        with pool:
            pool.start()
            for n in range(size):
                pool.submit(_item(n))
            # Every runner has to be inside work() at once for the barrier to
            # release; serial execution would time out and break it.
            pool.wait_idle(interval=0.05)
        assert not barrier.broken

    def test_front_jumps_the_queue(self) -> None:
        pool, gate, done = self._gated_pool(1 + 1)  # two runners, both parked
        try:
            for n in (1, 2):
                pool.submit(_item(n))  # occupy both runners
            while pool.pending() and len(pool._items):  # wait for both to be picked up
                pass
            pool.submit(_item(3))
            pool.submit(_item(4))
            pool.submit(_item(99), front=True)
            assert [i.pr.number for i in pool._items] == [99, 3, 4]
            gate.set()
            pool.wait_idle(interval=0.05)
        finally:
            gate.set()
            pool.close()
        assert sorted(done) == [1, 2, 3, 4, 99]

    def test_tick_can_submit_more_and_the_wait_covers_it(self) -> None:
        pool, gate, done = self._gated_pool(2)
        gate.set()
        submitted = []

        def tick() -> None:
            if not submitted:
                submitted.append(1)
                pool.submit(_item(7))

        with pool:
            pool.wait_idle(tick, interval=0.05)
        assert done == [7]

    def test_a_failing_review_does_not_stop_the_runner(self) -> None:
        done: list[int] = []

        def work(item: ReviewItem) -> None:
            if item.pr.number == 1:
                msg = "boom"
                raise RuntimeError(msg)
            done.append(item.pr.number)

        with ReviewPool(2, work) as pool:
            pool.start()
            for n in (1, 2, 3):
                pool.submit(_item(n))
            pool.wait_idle(interval=0.05)
        assert sorted(done) == [2, 3]
        assert pool.pending() == 0

    def test_close_stops_the_threads(self) -> None:
        pool = ReviewPool(3, lambda i: None)
        pool.start()
        threads = list(pool._threads)
        pool.close()
        assert threads and not any(t.is_alive() for t in threads)

    def test_runners_use_their_own_checkout_lane(self) -> None:
        from franktheunicorn.review.tool_executor import current_workspace_lane

        lanes: set[str] = set()
        lock = threading.Lock()
        barrier = threading.Barrier(2, timeout=5)

        def work(item: ReviewItem) -> None:
            with lock:
                lanes.add(current_workspace_lane())
            barrier.wait()

        with ReviewPool(2, work) as pool:
            pool.start()
            pool.submit(_item(1))
            pool.submit(_item(2))
            pool.wait_idle(interval=0.05)
        assert lanes == {"review-0", "review-1"}
