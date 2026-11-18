"""map_ordered: bounded fan-out that behaves like the serial loop it replaced."""

from __future__ import annotations

import threading
import time

from franktheunicorn.core.concurrency import map_ordered


def _square(n: int) -> int:
    return n * n


class TestMapOrdered:
    def test_results_come_back_in_input_order_whatever_finishes_first(self) -> None:
        def slow_for_small(n: int) -> int:
            time.sleep(0.02 * (5 - n))
            return n

        got = [o.result for o in map_ordered(slow_for_small, range(5), 5)]

        assert got == [0, 1, 2, 3, 4]

    def test_it_actually_runs_concurrently(self) -> None:
        """Four 0.1s waits on four threads take ~0.1s, not 0.4s."""
        barrier = threading.Barrier(4, timeout=2)

        def wait_for_the_others(n: int) -> int:
            barrier.wait()  # deadlocks (and times out) unless all four run at once
            return n

        got = [o.result for o in map_ordered(wait_for_the_others, range(4), 4)]

        assert got == [0, 1, 2, 3]

    def test_an_exception_is_captured_not_raised(self) -> None:
        def boom_on_two(n: int) -> int:
            if n == 2:
                msg = "boom"
                raise RuntimeError(msg)
            return n

        for concurrency in (1, 3):
            outcomes = list(map_ordered(boom_on_two, range(4), concurrency))
            assert [o.ok for o in outcomes] == [True, True, False, True]
            assert isinstance(outcomes[2].error, RuntimeError)
            assert outcomes[3].result == 3, "one bad item must not cost the rest"

    def test_inline_is_lazy_so_an_early_stop_runs_nothing_more(self) -> None:
        """concurrency=1 is the old serial loop: stopping after an item means the
        items after it never ran — what the poll's failure bail-out relies on."""
        ran: list[int] = []

        def record(n: int) -> int:
            ran.append(n)
            return n

        for outcome in map_ordered(record, range(10), 1):
            if outcome.item == 2:
                break

        assert ran == [0, 1, 2]

    def test_closing_early_cancels_what_has_not_started(self) -> None:
        ran: list[int] = []
        lock = threading.Lock()

        def record(n: int) -> int:
            time.sleep(0.01)
            with lock:
                ran.append(n)
            return n

        outcomes = map_ordered(record, range(100), 2)
        for outcome in outcomes:
            if outcome.item == 1:
                outcomes.close()
                break

        # Items 0 and 1, plus at most the window's worth that was in flight.
        assert len(ran) <= 4

    def test_worker_threads_drop_their_db_connection(self) -> None:
        """A pool thread must not leave a Django connection open behind it."""
        from unittest.mock import patch

        with patch("django.db.connection") as conn:
            list(map_ordered(_square, range(3), 3))

        assert conn.close.call_count == 3

    def test_inline_leaves_the_callers_connection_alone(self) -> None:
        from unittest.mock import patch

        with patch("django.db.connection") as conn:
            list(map_ordered(_square, range(3), 1))

        conn.close.assert_not_called()
