"""Tests for GitHubRateLimiter."""

from __future__ import annotations

import time
from pathlib import Path

import httpx

from franktheunicorn.data_access.rate_limiter import GitHubRateLimiter


class TestGitHubRateLimiter:
    def test_acquire_succeeds_under_limit(self, tmp_path: Path) -> None:
        limiter = GitHubRateLimiter(db_path=tmp_path / "rl.db", requests_per_hour=100)
        limiter.acquire()  # should not raise
        limiter.close()

    def test_not_rate_limited_initially(self, tmp_path: Path) -> None:
        limiter = GitHubRateLimiter(db_path=tmp_path / "rl.db")
        assert limiter.is_rate_limited() is False
        limiter.close()

    def test_update_from_headers_healthy(self, tmp_path: Path) -> None:
        limiter = GitHubRateLimiter(db_path=tmp_path / "rl.db")
        headers = httpx.Headers(
            {"x-ratelimit-remaining": "4500", "x-ratelimit-reset": "9999999999"}
        )
        limiter.update_from_headers(headers)
        assert limiter.is_rate_limited() is False
        limiter.close()

    def test_update_from_headers_exhausted(self, tmp_path: Path) -> None:
        limiter = GitHubRateLimiter(db_path=tmp_path / "rl.db")
        future_reset = str(int(time.time()) + 3600)
        headers = httpx.Headers({"x-ratelimit-remaining": "0", "x-ratelimit-reset": future_reset})
        limiter.update_from_headers(headers)
        assert limiter.is_rate_limited() is True
        limiter.close()

    def test_rate_limited_clears_after_reset(self, tmp_path: Path) -> None:
        limiter = GitHubRateLimiter(db_path=tmp_path / "rl.db")
        past_reset = str(int(time.time()) - 10)
        headers = httpx.Headers({"x-ratelimit-remaining": "0", "x-ratelimit-reset": past_reset})
        limiter.update_from_headers(headers)
        # Reset time is in the past, so should not be limited
        assert limiter.is_rate_limited() is False
        limiter.close()

    def test_update_from_headers_missing_values(self, tmp_path: Path) -> None:
        limiter = GitHubRateLimiter(db_path=tmp_path / "rl.db")
        headers = httpx.Headers({})
        limiter.update_from_headers(headers)  # should not raise
        assert limiter.is_rate_limited() is False
        limiter.close()

    def test_update_from_headers_invalid_values(self, tmp_path: Path) -> None:
        limiter = GitHubRateLimiter(db_path=tmp_path / "rl.db")
        headers = httpx.Headers(
            {"x-ratelimit-remaining": "not-a-number", "x-ratelimit-reset": "bad"}
        )
        limiter.update_from_headers(headers)  # should not raise
        assert limiter.is_rate_limited() is False
        limiter.close()

    def test_sqlite_persists_across_instances(self, tmp_path: Path) -> None:
        db_path = tmp_path / "rl.db"
        limiter1 = GitHubRateLimiter(db_path=db_path, requests_per_hour=5)
        for _ in range(3):
            limiter1.acquire()
        limiter1.close()

        # Second instance reuses the same DB
        limiter2 = GitHubRateLimiter(db_path=db_path, requests_per_hour=5)
        # Should still work (bucket refills over time, but table exists)
        limiter2.acquire()
        limiter2.close()

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        db_path = tmp_path / "subdir" / "nested" / "rl.db"
        limiter = GitHubRateLimiter(db_path=db_path)
        assert db_path.parent.exists()
        limiter.close()
