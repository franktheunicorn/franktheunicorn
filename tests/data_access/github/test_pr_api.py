"""Tests for PRFetcherAPI."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.base import FetchMethod, NotFoundError, RateLimitError
from franktheunicorn.data_access.github.pr_fetcher import PRFetcherAPI
from franktheunicorn.data_access.github.types import PRSummary


class TestPRFetcherAPI:
    def test_fetches_pr_summary(
        self,
        httpx_mock: HTTPXMock,
        pr_api_json: dict[str, Any],
        pr_files_api_json: list[dict[str, Any]],
    ) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42",
            json=pr_api_json,
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42/files?per_page=100",
            json=pr_files_api_json,
        )
        client = httpx.Client()
        fetcher = PRFetcherAPI(client=client)
        result = fetcher.fetch_via_api("apache", "spark", 42)

        assert isinstance(result, PRSummary)
        assert result.number == 42
        assert result.title == "Fix flaky test in scheduler module"
        assert result.author == "alice-dev"
        assert result.state == "open"
        assert result.fetched_via == FetchMethod.API
        assert result.url == "https://github.com/apache/spark/pull/42"
        assert result.body == "This PR fixes a race condition in the scheduler tests."
        assert result.labels == ("bug", "tests")
        assert result.requested_reviewers == ("holdenk",)
        assert result.is_draft is False
        assert result.additions == 15
        assert result.deletions == 3
        client.close()

    def test_parses_files(
        self,
        httpx_mock: HTTPXMock,
        pr_api_json: dict[str, Any],
        pr_files_api_json: list[dict[str, Any]],
    ) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42",
            json=pr_api_json,
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42/files?per_page=100",
            json=pr_files_api_json,
        )
        client = httpx.Client()
        fetcher = PRFetcherAPI(client=client)
        result = fetcher.fetch_via_api("apache", "spark", 42)

        assert len(result.files) == 2
        assert result.files[0].filename == "core/src/test/scala/SchedulerSuite.scala"
        assert result.files[0].status == "modified"
        assert result.files[0].additions == 12
        assert result.files[0].deletions == 3
        client.close()

    def test_404_raises_not_found(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/999",
            status_code=404,
        )
        client = httpx.Client()
        fetcher = PRFetcherAPI(client=client)

        with pytest.raises(NotFoundError):
            fetcher.fetch_via_api("apache", "spark", 999)
        client.close()

    def test_403_raises_rate_limit(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42",
            status_code=403,
        )
        client = httpx.Client()
        fetcher = PRFetcherAPI(client=client)

        with pytest.raises(RateLimitError):
            fetcher.fetch_via_api("apache", "spark", 42)
        client.close()

    def test_fetch_falls_back_to_scrape_on_403(
        self,
        httpx_mock: HTTPXMock,
        pr_scrape_html: str,
    ) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42",
            status_code=403,
        )
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pull/42",
            text=pr_scrape_html,
        )
        client = httpx.Client()
        fetcher = PRFetcherAPI(client=client)
        result = fetcher.fetch("apache", "spark", 42)

        assert isinstance(result, PRSummary)
        assert result.fetched_via == FetchMethod.SCRAPE
        assert result.number == 42
        client.close()

    def test_handles_null_body(
        self,
        httpx_mock: HTTPXMock,
        pr_api_json: dict[str, Any],
        pr_files_api_json: list[dict[str, Any]],
    ) -> None:
        pr_api_json["body"] = None
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42",
            json=pr_api_json,
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42/files?per_page=100",
            json=pr_files_api_json,
        )
        client = httpx.Client()
        fetcher = PRFetcherAPI(client=client)
        result = fetcher.fetch_via_api("apache", "spark", 42)

        assert result.body == ""
        client.close()
