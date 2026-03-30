"""Tests for PRFetcher (API + scrape paths)."""

from __future__ import annotations

from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.base import FetchMethod, NotFoundError, RateLimitError
from franktheunicorn.data_access.github.pr_fetcher import PRFetcher
from franktheunicorn.data_access.github.types import PRSummary


class TestPRFetcherAPI:
    def test_fetches_pr_summary(
        self,
        httpx_mock: HTTPXMock,
        pr_fetcher: PRFetcher,
        pr_api_json: dict[str, Any],
        pr_files_api_json: list[dict[str, Any]],
    ) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42", json=pr_api_json
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42/files?per_page=100",
            json=pr_files_api_json,
        )
        result = pr_fetcher.fetch_via_api("apache", "spark", 42)

        assert isinstance(result, PRSummary)
        assert result.number == 42
        assert result.title == "Fix flaky test in scheduler module"
        assert result.author == "alice-dev"
        assert result.state == "open"
        assert result.fetched_via == FetchMethod.API
        assert result.labels == ("bug", "tests")
        assert result.requested_reviewers == ("holdenk",)
        assert result.is_draft is False
        assert result.additions == 15
        assert result.deletions == 3

    def test_parses_files(
        self,
        httpx_mock: HTTPXMock,
        pr_fetcher: PRFetcher,
        pr_api_json: dict[str, Any],
        pr_files_api_json: list[dict[str, Any]],
    ) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42", json=pr_api_json
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42/files?per_page=100",
            json=pr_files_api_json,
        )
        result = pr_fetcher.fetch_via_api("apache", "spark", 42)

        assert len(result.files) == 2
        assert result.files[0].filename == "core/src/test/scala/SchedulerSuite.scala"
        assert result.files[0].additions == 12

    def test_404_raises_not_found(self, httpx_mock: HTTPXMock, pr_fetcher: PRFetcher) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/999", status_code=404
        )
        with pytest.raises(NotFoundError):
            pr_fetcher.fetch_via_api("apache", "spark", 999)

    def test_403_raises_rate_limit(self, httpx_mock: HTTPXMock, pr_fetcher: PRFetcher) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42", status_code=403
        )
        with pytest.raises(RateLimitError):
            pr_fetcher.fetch_via_api("apache", "spark", 42)

    def test_handles_null_body(
        self,
        httpx_mock: HTTPXMock,
        pr_fetcher: PRFetcher,
        pr_api_json: dict[str, Any],
        pr_files_api_json: list[dict[str, Any]],
    ) -> None:
        pr_api_json["body"] = None
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42", json=pr_api_json
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42/files?per_page=100",
            json=pr_files_api_json,
        )
        assert pr_fetcher.fetch_via_api("apache", "spark", 42).body == ""


class TestPRFetcherScrape:
    def test_parses_all_fields(
        self, httpx_mock: HTTPXMock, pr_fetcher: PRFetcher, pr_scrape_html: str
    ) -> None:
        httpx_mock.add_response(url="https://github.com/apache/spark/pull/42", text=pr_scrape_html)
        result = pr_fetcher.fetch_via_scrape("apache", "spark", 42)

        assert isinstance(result, PRSummary)
        assert result.number == 42
        assert result.title == "Fix flaky test in scheduler module"
        assert result.author == "alice-dev"
        assert result.state == "open"
        assert result.fetched_via == FetchMethod.SCRAPE
        assert "bug" in result.labels
        assert "tests" in result.labels
        assert "race condition" in result.body

    def test_404_raises_not_found(self, httpx_mock: HTTPXMock, pr_fetcher: PRFetcher) -> None:
        httpx_mock.add_response(url="https://github.com/apache/spark/pull/999", status_code=404)
        with pytest.raises(NotFoundError):
            pr_fetcher.fetch_via_scrape("apache", "spark", 999)


class TestPRFetcherFallback:
    def test_falls_back_to_scrape_on_403(
        self,
        httpx_mock: HTTPXMock,
        pr_fetcher: PRFetcher,
        pr_scrape_html: str,
    ) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42", status_code=403
        )
        httpx_mock.add_response(url="https://github.com/apache/spark/pull/42", text=pr_scrape_html)
        result = pr_fetcher.fetch("apache", "spark", 42)

        assert result.fetched_via == FetchMethod.SCRAPE
        assert result.number == 42
