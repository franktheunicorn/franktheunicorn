"""Bounded fan-out for per-PR forge work, results handed back in input order.

The per-PR refresh is about seven forge reads and a ``git blame``, almost all of
it waiting on the network, and the poll used to do them one PR at a time on the
main thread — so on a repo the size of Spark the review runners sat idle while
the main thread worked through the listing. :func:`map_ordered` runs *fn* over
*items* on a few threads and yields each outcome in the order the items came in,
so a caller that counts consecutive failures, or stops early, behaves exactly
as it did serially.

``concurrency <= 1`` runs inline and lazily, on the calling thread: nothing runs
ahead of the consumer, which is the old behaviour exactly (and what the tests
that share the test transaction's DB connection need).
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable, Generator, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Outcome[T, R]:
    """What *fn* did with one item: a result, or the exception it raised."""

    item: T
    result: R | None = None
    error: Exception | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _in_worker_thread[T, R](fn: Callable[[T], R], item: T) -> R:
    """Run *fn* and drop this thread's DB connection afterwards.

    Django opens one connection per thread and never closes it outside a
    request; a pool thread that outlives the cycle would hold it forever.
    Reconnecting per item is cheap next to the forge reads the item makes.
    """
    from django.db import connection

    try:
        return fn(item)
    finally:
        connection.close()


def map_ordered[T, R](
    fn: Callable[[T], R],
    items: Iterable[T],
    concurrency: int,
    *,
    name: str = "refresh",
) -> Generator[Outcome[T, R]]:
    """Run *fn* over *items* on up to *concurrency* threads, yielding in input order.

    An exception from *fn* is captured on the :class:`Outcome`, never raised, so
    one bad item never costs the rest. At most *concurrency* items are in flight:
    the next one starts only as the oldest is handed back, so a consumer that
    stops early (``break``, or closing the iterator) has spent at most
    *concurrency* - 1 items' work beyond the one it stopped on — the poll's
    "five failures in a row, stop spending API calls" guard depends on that.
    Items already running when the iterator is closed finish before the close
    returns, so nothing is left writing behind the caller.
    """
    work = iter(items)
    if concurrency <= 1:
        for item in work:
            try:
                yield Outcome(item, result=fn(item))
            except Exception as exc:
                yield Outcome(item, error=exc)
        return

    pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix=name)
    window: deque[tuple[T, Future[R]]] = deque()

    def start_next() -> None:
        for item in work:
            window.append((item, pool.submit(_in_worker_thread, fn, item)))
            return

    try:
        for _ in range(concurrency):
            start_next()
        while window:
            item, future = window.popleft()
            try:
                outcome: Outcome[T, R] = Outcome(item, result=future.result())
            except Exception as exc:
                outcome = Outcome(item, error=exc)
            yield outcome
            # Refilled only once the consumer asks for more, so one that stops on
            # this outcome never started a replacement for it.
            start_next()
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
