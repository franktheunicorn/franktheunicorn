"""Dual-path fetcher for pull request reviews."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from bs4 import BeautifulSoup

from franktheunicorn.data_access.base import (
    DataFetcher,
    FetchMethod,
    NotFoundError,
    RateLimitError,
)
from franktheunicorn.data_access.github.types import PRReview, ReviewComment, SingleReview
from franktheunicorn.data_access.rate_limiter import GitHubRateLimiter

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
GITHUB_WEB_BASE = "https://github.com"


def _api_review_to_single(review: dict[str, Any]) -> SingleReview:
    """Convert a single API review JSON to a SingleReview."""
    user = review.get("user", {})
    return SingleReview(
        id=review.get("id", 0),
        author=user.get("login", "") if isinstance(user, dict) else str(user),
        state=review.get("state", ""),
        body=review.get("body", "") or "",
        submitted_at=review.get("submitted_at", ""),
    )


def _api_comment_to_review_comment(comment: dict[str, Any]) -> ReviewComment:
    """Convert an API review comment JSON to a ReviewComment."""
    user = comment.get("user", {})
    return ReviewComment(
        id=comment.get("id", 0),
        author=user.get("login", "") if isinstance(user, dict) else str(user),
        body=comment.get("body", "") or "",
        path=comment.get("path", ""),
        line=comment.get("line") or comment.get("original_line"),
        created_at=comment.get("created_at", ""),
    )


class ReviewFetcherAPI(DataFetcher[PRReview]):
    """Fetch PR reviews via the GitHub REST API."""

    def __init__(
        self,
        client: httpx.Client,
        rate_limiter: GitHubRateLimiter | None = None,
    ) -> None:
        super().__init__(client, rate_limiter)

    def fetch_via_api(  # type: ignore[override]
        self, owner: str, repo: str, pr_number: int
    ) -> PRReview:
        if self._rate_limiter is not None and isinstance(self._rate_limiter, GitHubRateLimiter):
            if self._rate_limiter.is_rate_limited():
                raise RateLimitError("GitHub API rate limited", method=FetchMethod.API)
            self._rate_limiter.acquire()

        # Fetch reviews list
        reviews_url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
        reviews_resp = self._client.get(
            reviews_url, headers={"Accept": "application/vnd.github+json"}
        )

        if self._rate_limiter is not None and isinstance(self._rate_limiter, GitHubRateLimiter):
            self._rate_limiter.update_from_headers(reviews_resp.headers)

        if reviews_resp.status_code == 404:
            raise NotFoundError(
                f"PR #{pr_number} not found in {owner}/{repo}",
                method=FetchMethod.API,
                status_code=404,
            )
        if reviews_resp.status_code in (403, 429):
            raise RateLimitError(
                f"Rate limited ({reviews_resp.status_code})",
                method=FetchMethod.API,
                status_code=reviews_resp.status_code,
            )
        reviews_resp.raise_for_status()
        reviews_json: list[dict[str, Any]] = reviews_resp.json()

        # Fetch review comments (inline comments across all reviews)
        comments_url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}/comments"
        comments_resp = self._client.get(
            comments_url,
            headers={"Accept": "application/vnd.github+json"},
            params={"per_page": 100},
        )

        if self._rate_limiter is not None and isinstance(self._rate_limiter, GitHubRateLimiter):
            self._rate_limiter.update_from_headers(comments_resp.headers)

        comments_resp.raise_for_status()
        comments_json: list[dict[str, Any]] = comments_resp.json()

        # Group comments by pull_request_review_id
        comments_by_review: dict[int, list[ReviewComment]] = {}
        for c in comments_json:
            review_id = c.get("pull_request_review_id", 0)
            comments_by_review.setdefault(review_id, []).append(_api_comment_to_review_comment(c))

        # Build SingleReview objects with their comments
        reviews = tuple(
            SingleReview(
                id=r.get("id", 0),
                author=_api_review_to_single(r).author,
                state=r.get("state", ""),
                body=r.get("body", "") or "",
                submitted_at=r.get("submitted_at", ""),
                comments=tuple(comments_by_review.get(r.get("id", 0), [])),
            )
            for r in reviews_json
        )

        return PRReview(
            fetched_via=FetchMethod.API,
            pr_number=pr_number,
            reviews=reviews,
        )

    def fetch_via_scrape(  # type: ignore[override]
        self, owner: str, repo: str, pr_number: int
    ) -> PRReview:
        return _fetch_reviews_via_scrape(self._client, owner, repo, pr_number)


class ReviewFetcherScrape(DataFetcher[PRReview]):
    """Fetch PR reviews by scraping the GitHub conversation page."""

    def __init__(
        self,
        client: httpx.Client,
        rate_limiter: GitHubRateLimiter | None = None,
    ) -> None:
        super().__init__(client, rate_limiter)

    def fetch_via_api(  # type: ignore[override]
        self, owner: str, repo: str, pr_number: int
    ) -> PRReview:
        raise RateLimitError("Scrape-only fetcher", method=FetchMethod.API)

    def fetch_via_scrape(  # type: ignore[override]
        self, owner: str, repo: str, pr_number: int
    ) -> PRReview:
        return _fetch_reviews_via_scrape(self._client, owner, repo, pr_number)


def _fetch_reviews_via_scrape(
    client: httpx.Client, owner: str, repo: str, pr_number: int
) -> PRReview:
    """Scrape the PR conversation page for review data."""
    url = f"{GITHUB_WEB_BASE}/{owner}/{repo}/pull/{pr_number}"
    response = client.get(url)

    if response.status_code == 404:
        raise NotFoundError(
            f"PR #{pr_number} page not found",
            method=FetchMethod.SCRAPE,
            status_code=404,
        )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    reviews: list[SingleReview] = []

    # Look for review containers in the timeline
    review_els = soup.select(".js-timeline-item .review-comment")
    if not review_els:
        # Try alternate selector for review blocks
        review_els = soup.select("[id^='pullrequestreview-']")

    for el in review_els:
        # Author
        author_el = el.select_one("a.author")
        author = author_el.get_text(strip=True) if author_el else ""

        # State badge
        state = "COMMENTED"
        state_el = el.select_one(".State, .review-status-label")
        if state_el:
            text = state_el.get_text(strip=True).upper()
            if "APPROVED" in text:
                state = "APPROVED"
            elif "CHANGE" in text:
                state = "CHANGES_REQUESTED"

        # Body
        body_el = el.select_one(".comment-body")
        body = body_el.get_text(strip=True) if body_el else ""

        # Timestamp
        time_el = el.select_one("relative-time")
        submitted_at = ""
        if time_el and time_el.get("datetime"):
            submitted_at = str(time_el["datetime"])

        reviews.append(
            SingleReview(
                id=0,  # not reliably available from HTML
                author=author,
                state=state,
                body=body,
                submitted_at=submitted_at,
            )
        )

    return PRReview(
        fetched_via=FetchMethod.SCRAPE,
        pr_number=pr_number,
        reviews=tuple(reviews),
    )
