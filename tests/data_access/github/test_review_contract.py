"""Contract tests: ReviewFetcherAPI and ReviewFetcherScrape produce compatible structure."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.base import FetchMethod
from franktheunicorn.data_access.github.review_fetcher import (
    ReviewFetcherAPI,
    ReviewFetcherScrape,
)
from franktheunicorn.data_access.github.types import PRReview, SingleReview


@pytest.fixture(params=["api", "scrape"])
def review_result(
    request: pytest.FixtureRequest,
    httpx_mock: HTTPXMock,
    pr_reviews_api_json: list[dict[str, Any]],
    pr_comments_api_json: list[dict[str, Any]],
    pr_scrape_html: str,
) -> PRReview:
    """Fetch PRReview via either path."""
    client = httpx.Client()
    if request.param == "api":
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42/reviews",
            json=pr_reviews_api_json,
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42/comments?per_page=100",
            json=pr_comments_api_json,
        )
        fetcher = ReviewFetcherAPI(client=client)
        result = fetcher.fetch_via_api("apache", "spark", 42)
    else:
        httpx_mock.add_response(
            url="https://github.com/apache/spark/pull/42",
            text=pr_scrape_html,
        )
        fetcher = ReviewFetcherScrape(client=client)
        result = fetcher.fetch_via_scrape("apache", "spark", 42)

    client.close()
    return result


class TestReviewContract:
    def test_returns_pr_review(self, review_result: PRReview) -> None:
        assert isinstance(review_result, PRReview)

    def test_pr_number_set(self, review_result: PRReview) -> None:
        assert review_result.pr_number == 42

    def test_reviews_are_single_reviews(self, review_result: PRReview) -> None:
        for r in review_result.reviews:
            assert isinstance(r, SingleReview)

    def test_reviews_have_authors(self, review_result: PRReview) -> None:
        for r in review_result.reviews:
            assert len(r.author) > 0

    def test_reviews_have_valid_states(self, review_result: PRReview) -> None:
        valid = {"APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED", ""}
        for r in review_result.reviews:
            assert r.state in valid

    def test_fetched_via_is_set(self, review_result: PRReview) -> None:
        assert review_result.fetched_via in (FetchMethod.API, FetchMethod.SCRAPE)

    def test_has_at_least_one_review(self, review_result: PRReview) -> None:
        assert len(review_result.reviews) >= 1
