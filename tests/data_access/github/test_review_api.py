"""Tests for ReviewFetcherAPI."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.base import FetchMethod, NotFoundError, RateLimitError
from franktheunicorn.data_access.github.review_fetcher import ReviewFetcherAPI
from franktheunicorn.data_access.github.types import PRReview


class TestReviewFetcherAPI:
    def test_fetches_reviews_with_comments(
        self,
        httpx_mock: HTTPXMock,
        pr_reviews_api_json: list[dict[str, Any]],
        pr_comments_api_json: list[dict[str, Any]],
    ) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42/reviews",
            json=pr_reviews_api_json,
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42/comments?per_page=100",
            json=pr_comments_api_json,
        )
        client = httpx.Client()
        fetcher = ReviewFetcherAPI(client=client)
        result = fetcher.fetch_via_api("apache", "spark", 42)

        assert isinstance(result, PRReview)
        assert result.pr_number == 42
        assert result.fetched_via == FetchMethod.API
        assert len(result.reviews) == 2

        # First review: holdenk with CHANGES_REQUESTED + 2 inline comments
        review1 = result.reviews[0]
        assert review1.author == "holdenk"
        assert review1.state == "CHANGES_REQUESTED"
        assert review1.body == "Good direction, but needs a few changes."
        assert len(review1.comments) == 2
        assert review1.comments[0].path == "core/src/test/scala/SchedulerSuite.scala"
        assert review1.comments[0].line == 102

        # Second review: cloud-fan with APPROVED, no inline comments
        review2 = result.reviews[1]
        assert review2.author == "cloud-fan"
        assert review2.state == "APPROVED"
        assert len(review2.comments) == 0
        client.close()

    def test_404_raises_not_found(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/999/reviews",
            status_code=404,
        )
        client = httpx.Client()
        fetcher = ReviewFetcherAPI(client=client)

        with pytest.raises(NotFoundError):
            fetcher.fetch_via_api("apache", "spark", 999)
        client.close()

    def test_403_raises_rate_limit(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42/reviews",
            status_code=403,
        )
        client = httpx.Client()
        fetcher = ReviewFetcherAPI(client=client)

        with pytest.raises(RateLimitError):
            fetcher.fetch_via_api("apache", "spark", 42)
        client.close()

    def test_empty_reviews(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42/reviews",
            json=[],
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42/comments?per_page=100",
            json=[],
        )
        client = httpx.Client()
        fetcher = ReviewFetcherAPI(client=client)
        result = fetcher.fetch_via_api("apache", "spark", 42)

        assert len(result.reviews) == 0
        client.close()

    def test_fetch_falls_back_to_scrape_on_rate_limit(
        self,
        httpx_mock: HTTPXMock,
        pr_scrape_html: str,
    ) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42/reviews",
            status_code=429,
        )
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pull/42",
            text=pr_scrape_html,
        )
        client = httpx.Client()
        fetcher = ReviewFetcherAPI(client=client)
        result = fetcher.fetch("apache", "spark", 42)

        assert isinstance(result, PRReview)
        assert result.fetched_via == FetchMethod.SCRAPE
        client.close()
