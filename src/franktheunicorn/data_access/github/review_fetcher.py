"""Dual-path fetcher for pull request reviews.

NOT WIRED into the live pipeline (like ``review/auto_poster.py``): review
data on the live path comes from the forge clients. Kept as the designed
data_access-layer implementation (design doc §7.3). Before wiring it in,
add pagination (``/reviews`` truncates at GitHub's default 30) and fill
the scrape path's missing fields (review ids, inline comments).
"""

from __future__ import annotations

from typing import Any

from bs4 import BeautifulSoup

from franktheunicorn.data_access.base import (
    GITHUB_API_BASE,
    GITHUB_WEB_BASE,
    DataFetcher,
    FetchMethod,
    get_login,
)
from franktheunicorn.data_access.github.types import PRReview, ReviewComment, SingleReview


class ReviewFetcher(DataFetcher[PRReview]):
    """Fetch PR reviews via API or by scraping the conversation page."""

    def fetch_via_api(  # type: ignore[override]
        self, owner: str, repo: str, pr_number: int
    ) -> PRReview:
        base = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}"
        reviews_json: list[dict[str, Any]] = self._api_get_json(f"{base}/reviews").json()
        comments_json: list[dict[str, Any]] = self._api_get_json(
            f"{base}/comments", per_page=100
        ).json()

        # Group inline comments by review id
        comments_by_review: dict[int, list[ReviewComment]] = {}
        for c in comments_json:
            review_id = c.get("pull_request_review_id", 0)
            comments_by_review.setdefault(review_id, []).append(
                ReviewComment(
                    id=c.get("id", 0),
                    author=get_login(c),
                    body=c.get("body", "") or "",
                    path=c.get("path", ""),
                    line=c.get("line") or c.get("original_line"),
                    created_at=c.get("created_at", ""),
                )
            )

        reviews = tuple(
            SingleReview(
                id=r.get("id", 0),
                author=get_login(r),
                state=r.get("state", ""),
                body=r.get("body", "") or "",
                submitted_at=r.get("submitted_at", ""),
                comments=tuple(comments_by_review.get(r.get("id", 0), [])),
            )
            for r in reviews_json
        )
        return PRReview(fetched_via=FetchMethod.API, pr_number=pr_number, reviews=reviews)

    def fetch_via_scrape(  # type: ignore[override]
        self, owner: str, repo: str, pr_number: int
    ) -> PRReview:
        url = f"{GITHUB_WEB_BASE}/{owner}/{repo}/pull/{pr_number}"
        soup = BeautifulSoup(self._scrape_get(url).text, "html.parser")
        review_els = soup.select("[id^='pullrequestreview-']") or soup.select(
            ".js-timeline-item .review-comment"
        )

        reviews: list[SingleReview] = []
        for el in review_els:
            author_el = el.select_one("a.author")
            state_el = el.select_one(".State, .review-status-label")
            body_el = el.select_one(".comment-body")
            time_el = el.select_one("relative-time")

            state = "COMMENTED"
            if state_el:
                text = state_el.get_text(strip=True).upper()
                if "APPROVED" in text:
                    state = "APPROVED"
                elif "CHANGE" in text:
                    state = "CHANGES_REQUESTED"

            reviews.append(
                SingleReview(
                    author=author_el.get_text(strip=True) if author_el else "",
                    state=state,
                    body=body_el.get_text(strip=True) if body_el else "",
                    submitted_at=str(time_el["datetime"])
                    if time_el and time_el.get("datetime")
                    else "",
                )
            )

        return PRReview(fetched_via=FetchMethod.SCRAPE, pr_number=pr_number, reviews=tuple(reviews))
