"""Contract tests: ReviewFetcher API and scrape paths produce compatible structure."""

from __future__ import annotations

from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.data_access.base import FetchMethod
from franktheunicorn.data_access.github.review_fetcher import ReviewFetcher
from franktheunicorn.data_access.github.types import PRReview, SingleReview


@pytest.fixture(params=["api", "scrape"])
def review_result(
    request: pytest.FixtureRequest,
    httpx_mock: HTTPXMock,
    review_fetcher: ReviewFetcher,
    pr_reviews_api_json: list[dict[str, Any]],
    pr_comments_api_json: list[dict[str, Any]],
    pr_scrape_html: str,
) -> PRReview:
    if request.param == "api":
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42/reviews",
            json=pr_reviews_api_json,
        )
        httpx_mock.add_response(
            url="https://api.github.com/repos/apache/spark/pulls/42/comments?per_page=100",
            json=pr_comments_api_json,
        )
        return review_fetcher.fetch_via_api("apache", "spark", 42)
    httpx_mock.add_response(url="https://github.com/apache/spark/pull/42", text=pr_scrape_html)
    return review_fetcher.fetch_via_scrape("apache", "spark", 42)


class TestReviewContract:
    def test_core_fields(self, review_result: PRReview) -> None:
        assert isinstance(review_result, PRReview)
        assert review_result.pr_number == 42
        assert review_result.fetched_via in (FetchMethod.API, FetchMethod.SCRAPE)
        assert len(review_result.reviews) >= 1
        for r in review_result.reviews:
            assert isinstance(r, SingleReview)
            assert len(r.author) > 0
            assert r.state in {"APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED", ""}
