"""Integration tests for Diff fetcher — tests unified fetch() with API->scrape fallback."""

from __future__ import annotations

from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.base import FetchMethod
from franktheunicorn.data_access.github.diff_fetcher import DiffFetcher
from franktheunicorn.data_access.github.types import PRDiff


class TestDiffFetcherIntegration:
    def test_api_success_uses_api_path(
        self,
        httpx_mock: HTTPXMock,
        diff_fetcher: DiffFetcher,
        pr_diff_text: str,
    ) -> None:
        """When API succeeds, fetch() returns API result."""
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42", text=pr_diff_text
        )
        result = diff_fetcher.fetch("apache", "spark", 42)

        assert isinstance(result, PRDiff)
        assert result.fetched_via == FetchMethod.API
        assert result.pr_number == 42
        assert len(result.files) == 2

    def test_api_500_falls_back_to_scrape(
        self,
        httpx_mock: HTTPXMock,
        diff_fetcher: DiffFetcher,
        pr_diff_text: str,
    ) -> None:
        """When API returns 500, fetch() falls back to scrape."""
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42",
            status_code=500,
        )
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pull/42.diff", text=pr_diff_text
        )
        result = diff_fetcher.fetch("apache", "spark", 42)

        assert isinstance(result, PRDiff)
        assert result.fetched_via == FetchMethod.SCRAPE
        assert result.pr_number == 42
        assert len(result.files) == 2

    def test_api_rate_limited_falls_back_to_scrape(
        self,
        httpx_mock: HTTPXMock,
        diff_fetcher: DiffFetcher,
        pr_diff_text: str,
    ) -> None:
        """When API returns 429, fetch() falls back to scrape."""
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42",
            status_code=429,
        )
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pull/42.diff", text=pr_diff_text
        )
        result = diff_fetcher.fetch("apache", "spark", 42)

        assert isinstance(result, PRDiff)
        assert result.fetched_via == FetchMethod.SCRAPE
        assert result.pr_number == 42
        assert len(result.files) == 2
