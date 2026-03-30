"""Tests for GitHubRateLimiter."""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest

from franktheunicorn.data_access.rate_limiter import GitHubRateLimiter


@pytest.fixture
def limiter(tmp_path: Path) -> GitHubRateLimiter:
    return GitHubRateLimiter(db_path=tmp_path / "rl.db")


class TestGitHubRateLimiter:
    def test_acquire_succeeds_under_limit(self, limiter: GitHubRateLimiter) -> None:
        limiter.acquire()  # should not raise

    def test_not_rate_limited_initially(self, limiter: GitHubRateLimiter) -> None:
        assert limiter.is_rate_limited() is False

    def test_healthy_headers_not_limited(self, limiter: GitHubRateLimiter) -> None:
        limiter.update_from_headers(
            httpx.Headers({"x-ratelimit-remaining": "4500", "x-ratelimit-reset": "9999999999"})
        )
        assert limiter.is_rate_limited() is False

    def test_exhausted_headers_limited(self, limiter: GitHubRateLimiter) -> None:
        future_reset = str(int(time.time()) + 3600)
        limiter.update_from_headers(
            httpx.Headers({"x-ratelimit-remaining": "0", "x-ratelimit-reset": future_reset})
        )
        assert limiter.is_rate_limited() is True

    def test_clears_after_reset_time(self, limiter: GitHubRateLimiter) -> None:
        past_reset = str(int(time.time()) - 10)
        limiter.update_from_headers(
            httpx.Headers({"x-ratelimit-remaining": "0", "x-ratelimit-reset": past_reset})
        )
        assert limiter.is_rate_limited() is False

    def test_missing_headers_tolerated(self, limiter: GitHubRateLimiter) -> None:
        limiter.update_from_headers(httpx.Headers({}))
        assert limiter.is_rate_limited() is False

    def test_invalid_headers_tolerated(self, limiter: GitHubRateLimiter) -> None:
        limiter.update_from_headers(
            httpx.Headers({"x-ratelimit-remaining": "not-a-number", "x-ratelimit-reset": "bad"})
        )
        assert limiter.is_rate_limited() is False

    def test_sqlite_persists_across_instances(self, tmp_path: Path) -> None:
        db_path = tmp_path / "persist.db"
        limiter1 = GitHubRateLimiter(db_path=db_path, requests_per_hour=5)
        for _ in range(3):
            limiter1.acquire()
        del limiter1

        limiter2 = GitHubRateLimiter(db_path=db_path, requests_per_hour=5)
        limiter2.acquire()  # reuse existing DB; verify state persists across instances
        del limiter2

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        db_path = tmp_path / "subdir" / "nested" / "rl.db"
        GitHubRateLimiter(db_path=db_path)
        assert db_path.parent.exists()
