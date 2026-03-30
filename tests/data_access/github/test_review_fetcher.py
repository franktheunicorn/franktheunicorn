"""Tests for ReviewFetcher (API + scrape paths)."""

from __future__ import annotations

from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.base import FetchMethod, NotFoundError, RateLimitError
from franktheunicorn.data_access.github.review_fetcher import ReviewFetcher
from franktheunicorn.data_access.github.types import PRReview


class TestReviewFetcherAPI:
    def test_fetches_reviews_with_comments(
        self,
        httpx_mock: HTTPXMock,
        review_fetcher: ReviewFetcher,
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
        result = review_fetcher.fetch_via_api("apache", "spark", 42)

        assert isinstance(result, PRReview)
        assert result.pr_number == 42
        assert result.fetched_via == FetchMethod.API
        assert len(result.reviews) == 2

        review1 = result.reviews[0]
        assert review1.author == "holdenk"
        assert review1.state == "CHANGES_REQUESTED"
        assert len(review1.comments) == 2
        assert review1.comments[0].path == "core/src/test/scala/SchedulerSuite.scala"

        assert result.reviews[1].author == "cloud-fan"
        assert result.reviews[1].state == "APPROVED"
        assert len(result.reviews[1].comments) == 0

    def test_404_raises_not_found(
        self, httpx_mock: HTTPXMock, review_fetcher: ReviewFetcher
    ) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/999/reviews", status_code=404
        )
        with pytest.raises(NotFoundError):
            review_fetcher.fetch_via_api("apache", "spark", 999)

    def test_403_raises_rate_limit(
        self, httpx_mock: HTTPXMock, review_fetcher: ReviewFetcher
    ) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42/reviews", status_code=403
        )
        with pytest.raises(RateLimitError):
            review_fetcher.fetch_via_api("apache", "spark", 42)

    def test_empty_reviews(self, httpx_mock: HTTPXMock, review_fetcher: ReviewFetcher) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42/reviews", json=[]
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42/comments?per_page=100", json=[]
        )
        assert len(review_fetcher.fetch_via_api("apache", "spark", 42).reviews) == 0


class TestReviewFetcherScrape:
    def test_parses_reviews_from_html(
        self,
        httpx_mock: HTTPXMock,
        review_fetcher: ReviewFetcher,
        pr_scrape_html: str,
    ) -> None:
        httpx_mock.add_response(url="https://github.com/apache/spark/pull/42", text=pr_scrape_html)
        result = review_fetcher.fetch_via_scrape("apache", "spark", 42)

        assert isinstance(result, PRReview)
        assert result.pr_number == 42
        assert result.fetched_via == FetchMethod.SCRAPE
        assert len(result.reviews) >= 1
        assert any(r.author in ("holdenk", "cloud-fan") for r in result.reviews)

    def test_404_raises_not_found(
        self, httpx_mock: HTTPXMock, review_fetcher: ReviewFetcher
    ) -> None:
        httpx_mock.add_response(url="https://github.com/apache/spark/pull/999", status_code=404)
        with pytest.raises(NotFoundError):
            review_fetcher.fetch_via_scrape("apache", "spark", 999)

    def test_empty_page(self, httpx_mock: HTTPXMock, review_fetcher: ReviewFetcher) -> None:
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pull/42", text="<html><body></body></html>"
        )
        assert len(review_fetcher.fetch_via_scrape("apache", "spark", 42).reviews) == 0


class TestReviewFetcherFallback:
    def test_falls_back_on_429(
        self,
        httpx_mock: HTTPXMock,
        review_fetcher: ReviewFetcher,
        pr_scrape_html: str,
    ) -> None:
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42/reviews", status_code=429
        )
        httpx_mock.add_response(url="https://github.com/apache/spark/pull/42", text=pr_scrape_html)
        result = review_fetcher.fetch("apache", "spark", 42)

        assert result.fetched_via == FetchMethod.SCRAPE
