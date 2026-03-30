"""Tests for PRFetcherScrape."""

from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.base import FetchMethod, NotFoundError
from franktheunicorn.data_access.github.pr_fetcher import PRFetcherScrape
from franktheunicorn.data_access.github.types import PRSummary


class TestPRFetcherScrape:
    def test_parses_title(self, httpx_mock: HTTPXMock, pr_scrape_html: str) -> None:
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pull/42",
            text=pr_scrape_html,
        )
        client = httpx.Client()
        fetcher = PRFetcherScrape(client=client)
        result = fetcher.fetch_via_scrape("apache", "spark", 42)

        assert isinstance(result, PRSummary)
        assert result.title == "Fix flaky test in scheduler module"
        assert result.fetched_via == FetchMethod.SCRAPE
        client.close()

    def test_parses_author(self, httpx_mock: HTTPXMock, pr_scrape_html: str) -> None:
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pull/42",
            text=pr_scrape_html,
        )
        client = httpx.Client()
        fetcher = PRFetcherScrape(client=client)
        result = fetcher.fetch_via_scrape("apache", "spark", 42)

        assert result.author == "alice-dev"
        client.close()

    def test_parses_state(self, httpx_mock: HTTPXMock, pr_scrape_html: str) -> None:
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pull/42",
            text=pr_scrape_html,
        )
        client = httpx.Client()
        fetcher = PRFetcherScrape(client=client)
        result = fetcher.fetch_via_scrape("apache", "spark", 42)

        assert result.state == "open"
        client.close()

    def test_parses_labels(self, httpx_mock: HTTPXMock, pr_scrape_html: str) -> None:
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pull/42",
            text=pr_scrape_html,
        )
        client = httpx.Client()
        fetcher = PRFetcherScrape(client=client)
        result = fetcher.fetch_via_scrape("apache", "spark", 42)

        assert "bug" in result.labels
        assert "tests" in result.labels
        client.close()

    def test_parses_body(self, httpx_mock: HTTPXMock, pr_scrape_html: str) -> None:
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pull/42",
            text=pr_scrape_html,
        )
        client = httpx.Client()
        fetcher = PRFetcherScrape(client=client)
        result = fetcher.fetch_via_scrape("apache", "spark", 42)

        assert "race condition" in result.body
        client.close()

    def test_404_raises_not_found(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pull/999",
            status_code=404,
        )
        client = httpx.Client()
        fetcher = PRFetcherScrape(client=client)

        with pytest.raises(NotFoundError):
            fetcher.fetch_via_scrape("apache", "spark", 999)
        client.close()

    def test_fetch_goes_directly_to_scrape(
        self, httpx_mock: HTTPXMock, pr_scrape_html: str
    ) -> None:
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pull/42",
            text=pr_scrape_html,
        )
        client = httpx.Client()
        fetcher = PRFetcherScrape(client=client)
        result = fetcher.fetch("apache", "spark", 42)

        assert result.fetched_via == FetchMethod.SCRAPE
        client.close()

    def test_sets_pr_number(self, httpx_mock: HTTPXMock, pr_scrape_html: str) -> None:
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pull/42",
            text=pr_scrape_html,
        )
        client = httpx.Client()
        fetcher = PRFetcherScrape(client=client)
        result = fetcher.fetch_via_scrape("apache", "spark", 42)

        assert result.number == 42
        client.close()
