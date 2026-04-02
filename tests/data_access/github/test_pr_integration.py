"""Integration tests for PR fetcher — tests unified fetch() with API->scrape fallback."""

from __future__ import annotations

from typing import Any

from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.base import FetchMethod
from franktheunicorn.data_access.github.pr_fetcher import PRFetcher
from franktheunicorn.data_access.github.types import PRSummary


class TestPRFetcherIntegration:
    def test_api_success_uses_api_path(
        self,
        httpx_mock: HTTPXMock,
        pr_fetcher: PRFetcher,
        pr_api_json: dict[str, Any],
        pr_files_api_json: list[dict[str, Any]],
    ) -> None:
        """When API succeeds, fetch() returns API result."""
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42", json=pr_api_json
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42/files?per_page=100",
            json=pr_files_api_json,
        )
        result = pr_fetcher.fetch("apache", "spark", 42)

        assert isinstance(result, PRSummary)
        assert result.fetched_via == FetchMethod.API
        assert result.number == 42
        assert result.title == "Fix flaky test in scheduler module"

    def test_api_500_falls_back_to_scrape(
        self,
        httpx_mock: HTTPXMock,
        pr_fetcher: PRFetcher,
        pr_scrape_html: str,
    ) -> None:
        """When API returns 500, fetch() falls back to scrape."""
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42",
            status_code=500,
        )
        httpx_mock.add_response(url="https://github.com/apache/spark/pull/42", text=pr_scrape_html)
        result = pr_fetcher.fetch("apache", "spark", 42)

        assert isinstance(result, PRSummary)
        assert result.fetched_via == FetchMethod.SCRAPE
        assert result.number == 42

    def test_api_rate_limited_falls_back_to_scrape(
        self,
        httpx_mock: HTTPXMock,
        pr_fetcher: PRFetcher,
        pr_scrape_html: str,
    ) -> None:
        """When API returns 429, fetch() falls back to scrape."""
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42",
            status_code=429,
        )
        httpx_mock.add_response(url="https://github.com/apache/spark/pull/42", text=pr_scrape_html)
        result = pr_fetcher.fetch("apache", "spark", 42)

        assert isinstance(result, PRSummary)
        assert result.fetched_via == FetchMethod.SCRAPE
        assert result.number == 42
