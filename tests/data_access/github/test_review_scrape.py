"""Tests for ReviewFetcherScrape."""

from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.base import FetchMethod, NotFoundError
from franktheunicorn.data_access.github.review_fetcher import ReviewFetcherScrape
from franktheunicorn.data_access.github.types import PRReview


class TestReviewFetcherScrape:
    def test_parses_reviews_from_html(self, httpx_mock: HTTPXMock, pr_scrape_html: str) -> None:
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pull/42",
            text=pr_scrape_html,
        )
        client = httpx.Client()
        fetcher = ReviewFetcherScrape(client=client)
        result = fetcher.fetch_via_scrape("apache", "spark", 42)

        assert isinstance(result, PRReview)
        assert result.pr_number == 42
        assert result.fetched_via == FetchMethod.SCRAPE
        assert len(result.reviews) >= 1
        client.close()

    def test_parses_review_authors(self, httpx_mock: HTTPXMock, pr_scrape_html: str) -> None:
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pull/42",
            text=pr_scrape_html,
        )
        client = httpx.Client()
        fetcher = ReviewFetcherScrape(client=client)
        result = fetcher.fetch_via_scrape("apache", "spark", 42)

        authors = {r.author for r in result.reviews}
        assert "holdenk" in authors or "cloud-fan" in authors
        client.close()

    def test_404_raises_not_found(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pull/999",
            status_code=404,
        )
        client = httpx.Client()
        fetcher = ReviewFetcherScrape(client=client)

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
        fetcher = ReviewFetcherScrape(client=client)
        result = fetcher.fetch("apache", "spark", 42)

        assert result.fetched_via == FetchMethod.SCRAPE
        client.close()

    def test_empty_page_returns_empty_reviews(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pull/42",
            text="<html><body>No reviews</body></html>",
        )
        client = httpx.Client()
        fetcher = ReviewFetcherScrape(client=client)
        result = fetcher.fetch_via_scrape("apache", "spark", 42)

        assert result.pr_number == 42
        assert len(result.reviews) == 0
        client.close()
