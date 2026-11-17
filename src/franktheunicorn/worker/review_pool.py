"""The poll cycle's PR-review runners: a work queue and N threads draining it.

A cycle used to review its PRs one at a time on the main thread, and one review
is minutes of LLM waits — so a Spark cycle with a few hundred refreshed PRs ran
for most of a day, and every project after Spark waited for it to be polled at
all. The main thread now only polls and queues; the runners review.

The queue is a deque, not a priority queue, because there are exactly two
positions that matter: the back (normal order) and the front (the operator's own
PRs found mid-cycle — those are the ones someone is waiting on).

Each runner is in its own checkout lane (``review-<n>``), so agent CLIs reviewing
two PRs of the same repo at once get separate worktrees instead of fighting over
one ``git checkout``. A pool of size 1 runs every item inline on the submitting
thread: no thread, no lane, the old behaviour exactly.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from franktheunicorn.config.models import ProjectConfig
    from franktheunicorn.core.models import PullRequest

logger = logging.getLogger(__name__)


@dataclass
class ReviewItem:
    """One PR to review, with what its review needs from the cycle."""

    pr: PullRequest
    pc: ProjectConfig
    client: object | None
    repo_path: Path | None
    #: A backfill item gets the review only — not the differential test run or
    #: the copy-pasta scan, which are for PRs the poll just refreshed.
    backfill: bool = False


class ReviewPool:
    """Run *work* over submitted items on *size* threads; ``size <= 1`` is inline."""

    def __init__(
        self,
        size: int,
        work: Callable[[ReviewItem], None] | None = None,
        *,
        lane_prefix: str = "review",
    ) -> None:
        self.size = max(1, size)
        #: Settable after construction: the cycle builds the HTTP clients the work
        #: closes over inside the ``with`` that owns the pool.
        self.work = work
        self._lane_prefix = lane_prefix
        self._items: deque[ReviewItem] = deque()
        self._cond = threading.Condition()
        self._outstanding = 0  # queued + running
        self._closed = False
        self._threads: list[threading.Thread] = []

    @property
    def inline(self) -> bool:
        return self.size == 1

    def __enter__(self) -> ReviewPool:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def start(self) -> None:
        """Start the runners; needs ``work`` set. A no-op inline or when running."""
        if self.work is None:
            msg = "ReviewPool.start() before work was set"
            raise RuntimeError(msg)
        if self.inline or self._threads:
            return
        for i in range(self.size):
            t = threading.Thread(
                target=self._run, args=(i,), name=f"{self._lane_prefix}-{i}", daemon=True
            )
            t.start()
            self._threads.append(t)

    def submit(self, item: ReviewItem, *, front: bool = False) -> None:
        """Queue *item* — at the front when someone is waiting on it."""
        if self.inline:
            self._run_one(item)
            return
        with self._cond:
            if self._closed:
                msg = "review pool is closed"
                raise RuntimeError(msg)
            if front:
                self._items.appendleft(item)
            else:
                self._items.append(item)
            self._outstanding += 1
            self._cond.notify()

    def pending(self) -> int:
        """Items queued or under review."""
        with self._cond:
            return self._outstanding

    def wait_idle(self, tick: Callable[[], None] | None = None, interval: float = 5.0) -> None:
        """Block until every submitted item is done, calling *tick* every *interval*.

        *tick* runs on the waiting thread and may ``submit`` more work (the
        mid-cycle PR sync does); the wait covers that too. It runs at least once,
        even when there was nothing to wait for, so the inline pool still gets
        the same mid-cycle pass the threaded one does.
        """
        while True:
            if tick is not None:
                try:
                    tick()
                except Exception:
                    logger.exception("Review pool tick failed")
            deadline = time.monotonic() + interval
            with self._cond:
                while self._outstanding and time.monotonic() < deadline:
                    self._cond.wait(timeout=max(0.0, deadline - time.monotonic()))
                if not self._outstanding:
                    return

    def close(self) -> None:
        """Stop the runners once the queue is empty. Idempotent."""
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        for t in self._threads:
            t.join()
        self._threads.clear()

    def _run(self, index: int) -> None:
        from django.db import connection

        from franktheunicorn.review.tool_executor import workspace_lane

        try:
            with workspace_lane(f"{self._lane_prefix}-{index}"):
                while True:
                    with self._cond:
                        while not self._items and not self._closed:
                            self._cond.wait()
                        if not self._items:
                            return
                        item = self._items.popleft()
                    try:
                        self._run_one(item)
                    finally:
                        with self._cond:
                            self._outstanding -= 1
                            self._cond.notify_all()
        except BaseException:
            logger.critical("Review runner %d died", index, exc_info=True)
            raise
        finally:
            connection.close()

    def _run_one(self, item: ReviewItem) -> None:
        assert self.work is not None
        try:
            self.work(item)
        except Exception:
            logger.exception("Error reviewing PR #%d (%s)", item.pr.number, item.pc.full_name)
