"""GitHub-aware rate limiter with SQLite-backed bucket state.

Wraps pyrate-limiter and adapts request rates based on
``X-RateLimit-Remaining`` / ``X-RateLimit-Reset`` response headers.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx
from pyrate_limiter import Duration, Limiter, Rate, SQLiteBucket

logger = logging.getLogger(__name__)

_DEFAULT_REQUESTS_PER_HOUR = 5000  # GitHub authenticated limit


class GitHubRateLimiter:
    """Adaptive rate limiter for GitHub API requests.

    Uses a SQLite-backed token bucket so rate state persists across
    process restarts. Reads GitHub rate-limit response headers to
    tighten or relax the rate dynamically.
    """

    def __init__(
        self,
        db_path: str | Path,
        requests_per_hour: int = _DEFAULT_REQUESTS_PER_HOUR,
        table_name: str = "github_rate_limit",
    ) -> None:
        self._db_path = Path(db_path)
        if self._db_path.suffix != ".sqlite":
            self._db_path = self._db_path.with_suffix(".sqlite")
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._remaining: int | None = None
        self._reset_at: float | None = None

        rate = Rate(requests_per_hour, Duration.HOUR)
        bucket = SQLiteBucket.init_from_file(
            rates=[rate],
            table=table_name,
            db_path=str(self._db_path),
            create_new_table=True,
        )
        self._limiter = Limiter(bucket)

    def acquire(self) -> None:
        """Block until a request slot is available.

        Raises ``BucketFullException`` if the wait would exceed max_delay.
        """
        if self.is_rate_limited():
            wait = self._seconds_until_reset()
            if wait > 0:
                logger.info("Rate-limited by GitHub headers, waiting %.1fs", wait)
                time.sleep(min(wait, 30.0))

        self._limiter.try_acquire("github")

    def update_from_headers(self, headers: httpx.Headers) -> None:
        """Read GitHub rate-limit headers from a response and adapt."""
        remaining_str = headers.get("x-ratelimit-remaining")
        reset_str = headers.get("x-ratelimit-reset")

        if remaining_str is not None:
            try:
                self._remaining = int(remaining_str)
            except ValueError:
                logger.warning("Could not parse X-RateLimit-Remaining: %s", remaining_str)

        if reset_str is not None:
            try:
                self._reset_at = float(reset_str)
            except ValueError:
                logger.warning("Could not parse X-RateLimit-Reset: %s", reset_str)

        if self._remaining is not None and self._remaining <= 100:
            logger.warning("GitHub API rate limit low: %d remaining", self._remaining)

    def is_rate_limited(self) -> bool:
        """Return True if we know the API limit is exhausted."""
        if self._remaining is not None and self._remaining <= 0:
            return self._seconds_until_reset() > 0
        return False

    def _seconds_until_reset(self) -> float:
        if self._reset_at is None:
            return 0.0
        return max(0.0, self._reset_at - time.time())
